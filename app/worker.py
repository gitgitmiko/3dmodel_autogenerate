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
        extra_views={v: d / f"view_{v}.png" for v in VIEWS if (d / f"view_{v}.png").exists()},
    )
    store.update_asset(aid, status="processing")

    def progress(frac, msg):
        store.update_job(job["id"], progress=round(frac * 0.9, 3), message=msg)

    src = next(d.glob("input.*"))
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


HANDLERS = {"image_to_3d": run_image_to_3d, "rig_prepare": run_rig_prepare, "rig": run_rig}


def _fail(job: dict, err: Exception):
    msg = str(err).strip() or err.__class__.__name__
    log.error("job %s gagal: %s\n%s", job["id"], msg, traceback.format_exc())
    store.update_job(job["id"], status="error", message=msg[:2000], finished=time.time())
    if job["kind"] == "image_to_3d":
        store.update_asset(job["asset_id"], status="error")
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
