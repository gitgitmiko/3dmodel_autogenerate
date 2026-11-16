"""Worker antrean: satu job berjalan dalam satu waktu (CPU tidak sanggup paralel)."""

import json
import logging
import shutil
import subprocess
import threading
import time
import traceback
from pathlib import Path

from . import store
from .config import ASSETS_DIR, BLENDER, BLENDER_DIR, MODEL_IDLE_MINUTES, VIEWS

log = logging.getLogger("gitgit3d.worker")
_wake = threading.Event()
_last_model_use = 0.0


def asset_dir(aid: str) -> Path:
    return ASSETS_DIR / aid


def wake():
    _wake.set()


def _blender(script: str, *args: str, timeout: int = 600) -> str:
    cmd = [BLENDER, "-b", "--factory-startup", "-P", str(BLENDER_DIR / script), "--", *args]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    out = p.stdout + p.stderr
    if p.returncode != 0 or "Traceback" in out:
        tail = "\n".join(line for line in out.splitlines()[-25:] if line.strip())
        raise RuntimeError(f"Blender gagal ({script}):\n{tail}")
    return out


def _warnings(out: str) -> list[str]:
    return [line.split("WARN:", 1)[1].strip() for line in out.splitlines() if "[autorig] WARN:" in line]


# ------------------------------------------------------------------ handler job
def run_image_to_3d(job: dict):
    global _last_model_use
    from engine.image_to_3d import ImageTo3DOptions, image_to_3d

    aid = job["asset_id"]
    d = asset_dir(aid)
    p = job["params"]
    opts = ImageTo3DOptions(
        resolution=int(p.get("resolution", 256)),
        target_faces=int(p.get("target_faces", 60000)),
        remove_bg=bool(p.get("remove_bg", True)),
        foreground_ratio=float(p.get("foreground_ratio", 0.85)),
        smooth_iters=int(p.get("smooth_iters", 5)),
        texture_res=int(p.get("texture_res", 1024)),
        extra_views=_extra_views(d, (store.get_asset(aid)["stats"] or {}).get("side_assignment")),
    )
    store.update_asset(aid, status="processing")

    def progress(frac, msg):
        store.update_job(job["id"], progress=round(frac * 0.9, 3), message=msg)

    src = next(d.glob("input.*"), None)
    if src is None:
        raise RuntimeError("Gambar sumber belum ada (pembuatan gambar dari teks gagal).")
    res = image_to_3d(src, d, opts, progress=progress)
    _last_model_use = time.time()

    store.update_job(job["id"], progress=0.92, message="Ekspor OBJ & STL")
    import trimesh

    mesh = trimesh.load(d / "model.glb", force="mesh")
    mesh.export(d / "model.stl")
    # OBJ bertekstur terdiri dari beberapa file (obj + mtl + png) -> dikemas jadi satu zip.
    import tempfile
    import zipfile

    with tempfile.TemporaryDirectory() as tmp:
        mesh.export(Path(tmp) / "model.obj")
        with zipfile.ZipFile(d / "model-obj.zip", "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(Path(tmp).iterdir()):
                z.write(f, f.name)

    store.update_job(job["id"], progress=0.96, message="Membuat thumbnail")
    try:
        _blender("render_preview.py", str(d / "model.glb"), str(d / "thumb.png"), "--views", "34", "--size", "384",
                 timeout=180)
        (d / "thumb_34.png").unlink(missing_ok=True)
    except Exception as e:  # thumbnail tidak wajib
        log.warning("thumbnail gagal: %s", e)
    store.update_asset(aid, status="ready", stats=res["stats"])
    _requeue_rig_after_retexture(aid)
    return res["stats"]


def _requeue_rig_after_retexture(aid: str):
    """Setelah tekstur dibuat ulang, rig lama masih memakai tekstur lama -> buat ulang otomatis.

    Geometri TripoSR deterministik untuk gambar & parameter yang sama, jadi penanda lama tetap berlaku.
    """
    a = store.get_asset(aid)
    if store.active_jobs() and any(j["asset_id"] == aid and j["kind"] != "image_to_3d" and j["status"] == "queued"
                                   for j in store.active_jobs()):
        return  # sudah ada job rig dalam antrean (mis. auto_rig saat unggah)
    last_rig = next((j for j in store.jobs_for(aid) if j["kind"] == "rig" and j["status"] == "done"), None)
    if a["rig_status"] == "rigged" and last_rig:
        store.create_job(aid, "rig", last_rig["params"])
    elif a["rig_status"] in ("prepared", "error") and (asset_dir(aid) / "rig" / "prep.json").exists():
        prep = json.loads((asset_dir(aid) / "rig" / "prep.json").read_text())
        store.create_job(aid, "rig_prepare", {"height": prep.get("height_m", 1.75)})


def _extra_views(d: Path, side_assignment: dict | None = None) -> dict:
    views = {v: d / f"view_{v}.png" for v in VIEWS if (d / f"view_{v}.png").exists()}
    sides = {v: d / f"view_{v}.png" for v in ("side_a", "side_b") if (d / f"view_{v}.png").exists()}
    manual = {"left", "right"} & views.keys()
    if not manual:
        views.update(sides)  # kiri/kanan ditentukan otomatis saat bake
    elif side_assignment and len(manual) == 1:
        # Pengguna mengganti satu sisi; sisi lain tetap dari turnaround sesuai penempatan sebelumnya.
        a_side = side_assignment.get("side_a")
        mapping = {a_side: "side_a", ("right" if a_side == "left" else "left"): "side_b"}
        other = ({"left", "right"} - manual).pop()
        if mapping.get(other) in sides:
            views[other] = sides[mapping[other]]
    return views


def run_text_to_image(job: dict):
    from engine.image_to_3d import _load_rembg
    from engine.sheet import split_turnaround
    from PIL import Image

    from . import textgen

    aid = job["asset_id"]
    d = asset_dir(aid)
    d.mkdir(parents=True, exist_ok=True)
    p = job["params"]
    store.update_asset(aid, status="processing")
    prompt = textgen.build_prompt(p["prompt"], p.get("kind", "character"), p.get("style", "stylized"),
                                  bool(p.get("turnaround", True)))
    (d / "prompt.txt").write_text(prompt)

    def msg(m, frac=0.2):
        store.update_job(job["id"], message=m, progress=frac)

    msg("Menyiapkan Google Flow", 0.05)
    src = textgen.generate(prompt, d / "gen", "16:9" if p.get("turnaround", True) else "1:1",
                           progress=lambda m: msg(m, 0.15))
    img = Image.open(src)
    img.load()
    warnings = []
    if p.get("turnaround", True):
        img.save(d / "sheet.png")
        msg("Memotong turnaround menjadi tampak depan/samping/belakang", 0.85)
        figs = split_turnaround(img, session=_load_rembg())
        if not figs:
            raise RuntimeError("Tidak ada objek yang terdeteksi di gambar hasil Google Flow.")
        figs[0].save(d / "input.png")
        if len(figs) >= 4:
            figs[1].save(d / "view_side_a.png")
            figs[2].save(d / "view_back.png")
            figs[3].save(d / "view_side_b.png")
        else:
            warnings.append(f"Turnaround hanya berisi {len(figs)} figur; hanya tampak depan yang dipakai.")
    else:
        img.save(d / "input.png")
    import shutil

    shutil.rmtree(d / "gen", ignore_errors=True)
    store.update_asset(aid, status="queued")
    return {"prompt": prompt, "warnings": warnings}


def run_rig_prepare(job: dict):
    aid = job["asset_id"]
    d = asset_dir(aid)
    rig = d / "rig"
    rig.mkdir(exist_ok=True)
    store.update_asset(aid, rig_status="preparing")
    store.update_job(job["id"], progress=0.2, message="Menebak posisi sendi")
    height = str(job["params"].get("height", 1.75))
    out = _blender("autorig.py", "prepare", str(d / "model.glb"), str(rig), height, timeout=300)
    prep = json.loads((rig / "prep.json").read_text())
    store.update_asset(aid, rig_status="prepared")
    return {"warnings": _warnings(out), "markers": prep["markers"]}


def run_rig(job: dict):
    aid = job["asset_id"]
    d = asset_dir(aid)
    rig = d / "rig"
    rig.mkdir(exist_ok=True)
    p = job["params"]
    store.update_asset(aid, rig_status="rigging")
    (rig / "markers.json").write_text(json.dumps({"markers": p["markers"]}, indent=2))
    store.update_job(job["id"], progress=0.15, message="Membangun kerangka, skinning & animasi")
    out = _blender("autorig.py", "rig", str(d / "model.glb"), str(rig), str(p.get("height", 1.75)),
                   str(rig / "markers.json"), ",".join(p.get("animations") or []), timeout=900)
    report = json.loads((rig / "rig.json").read_text())
    store.update_job(job["id"], progress=0.9, message="Membuat thumbnail")
    try:
        _blender("render_preview.py", str(rig / "rigged.glb"), str(rig / "thumb.png"), "--views", "34",
                 "--size", "384", "--anim", (p.get("animations") or ["idle"])[0], "--frame", "6", timeout=180)
        (rig / "thumb_34.png").unlink(missing_ok=True)
    except Exception as e:
        log.warning("thumbnail rig gagal: %s", e)
    store.update_asset(aid, rig_status="rigged")
    return {"warnings": report.get("warnings", []), "animations": report.get("animations", [])}


HANDLERS = {"text_to_image": run_text_to_image, "image_to_3d": run_image_to_3d, "rig_prepare": run_rig_prepare, "rig": run_rig}


def _fail(job: dict, err: Exception):
    msg = str(err).strip() or err.__class__.__name__
    log.error("job %s gagal: %s\n%s", job["id"], msg, traceback.format_exc())
    store.update_job(job["id"], status="error", message=msg[:2000], finished=time.time())
    if job["kind"] in ("image_to_3d", "text_to_image"):
        store.update_asset(job["asset_id"], status="error")
        if job["kind"] == "text_to_image":  # job lanjutan tidak ada gunanya tanpa gambar
            for j in store.jobs_for(job["asset_id"]):
                if j["status"] == "queued":
                    store.update_job(j["id"], status="error", message="Dibatalkan: gambar gagal dibuat",
                                     finished=time.time())
    else:
        store.update_asset(job["asset_id"], rig_status="error")


def _maybe_unload():
    global _last_model_use
    if _last_model_use and time.time() - _last_model_use > MODEL_IDLE_MINUTES * 60:
        from engine import image_to_3d as eng

        if eng.unload():
            log.info("Model TripoSR dilepas dari RAM (menganggur %d menit)", MODEL_IDLE_MINUTES)
        _last_model_use = 0.0


def loop():
    while True:
        job = store.next_queued()
        if not job:
            _maybe_unload()
            _wake.wait(timeout=30)
            _wake.clear()
            continue
        store.update_job(job["id"], status="running", started=time.time(), message="Memulai")
        try:
            result = HANDLERS[job["kind"]](job)
            store.update_job(job["id"], status="done", progress=1.0, message="Selesai", result=result or {},
                             finished=time.time())
        except Exception as e:  # noqa: BLE001
            _fail(job, e)


def start():
    t = threading.Thread(target=loop, name="gitgit3d-worker", daemon=True)
    t.start()
    return t


def remove_asset_files(aid: str):
    shutil.rmtree(asset_dir(aid), ignore_errors=True)
