"""gitgit3d — API & UI."""

import hmac
import io
import json
import logging
import time
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner
from PIL import Image
from pydantic import BaseModel, Field

from . import store, worker
from .config import (ANIMATIONS, CUSTOM_ANIM_EXT, MAX_ANIM_MB, APP_PASSWORD, COOKIE_SECURE, MAX_UPLOAD_MB, SECRET_KEY, SESSION_DAYS, VIEWS,
                     WEB_DIR)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("gitgit3d")

app = FastAPI(title="gitgit3d", docs_url=None, redoc_url=None)
signer = TimestampSigner(SECRET_KEY, salt="gitgit3d-session")
COOKIE = "g3d_session"
MARKER_KEYS = ["chin", "groin", "elbow_l", "elbow_r", "wrist_l", "wrist_r", "knee_l", "knee_r"]


@app.middleware("http")
async def _no_stale_ui(request: Request, call_next):
    """Cloudflare meng-cache .js/.css berdasarkan ekstensi; paksa revalidasi agar update UI langsung terlihat."""
    resp = await call_next(request)
    path = request.url.path
    if not path.startswith("/vendor/") and "cache-control" not in resp.headers:
        resp.headers["Cache-Control"] = "no-cache, max-age=0, must-revalidate"
    return resp


@app.on_event("startup")
def _startup():
    if not APP_PASSWORD:
        log.warning("APP_PASSWORD kosong — login dinonaktifkan! Set di .env untuk produksi.")
    store.init()
    worker.start()


# ------------------------------------------------------------------ auth
def authed(request: Request) -> bool:
    if not APP_PASSWORD:
        return True
    tok = request.cookies.get(COOKIE)
    if not tok:
        return False
    try:
        signer.unsign(tok, max_age=SESSION_DAYS * 86400)
        return True
    except (BadSignature, SignatureExpired):
        return False


def require_auth(request: Request):
    if not authed(request):
        raise HTTPException(401, "Belum masuk")


class Login(BaseModel):
    password: str


_fails: dict[str, list[float]] = {}


@app.post("/api/login")
def login(body: Login, request: Request, response: Response):
    ip = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")
    now = time.time()
    recent = [t for t in _fails.get(ip, []) if now - t < 600]
    if len(recent) >= 8:
        raise HTTPException(429, "Terlalu banyak percobaan, coba lagi 10 menit lagi")
    if not APP_PASSWORD or hmac.compare_digest(body.password.encode(), APP_PASSWORD.encode()):
        response.set_cookie(COOKIE, signer.sign(b"ok").decode(), max_age=SESSION_DAYS * 86400, httponly=True,
                            secure=COOKIE_SECURE, samesite="lax")
        _fails.pop(ip, None)
        return {"ok": True}
    _fails[ip] = recent + [now]
    raise HTTPException(401, "Password salah")


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    return {"authed": authed(request), "auth_required": bool(APP_PASSWORD)}


# ------------------------------------------------------------------ aset
FILE_LABELS = {
    "model.glb": "Model (GLB)", "model-obj.zip": "Model (OBJ + tekstur, zip)", "model.stl": "Model (STL, cetak 3D)",
    "remesh/model.glb": "Remesh (GLB)", "remesh/model-obj.zip": "Remesh (OBJ quad + tekstur, zip)",
    "remesh/model.fbx": "Remesh (FBX)",
    "rig/rigged.glb": "Rig + animasi (GLB)", "rig/rigged.fbx": "Rig + animasi (FBX, Unity/Unreal)",
}


def asset_view(a: dict, with_jobs=False) -> dict:
    d = worker.asset_dir(a["id"])
    files = {k: f"files/{a['id']}/{k}" for k in
             ["input.png", "input.jpg", "input.webp", "cutout.png", "thumb.png",
              *[f"view_{v}.png" for v in VIEWS], *[f"cutout_{v}.png" for v in VIEWS],
              "sheet.png", "view_side_a.png", "view_side_b.png", "model_original.glb",
              "remesh/model.glb", "remesh/model-obj.zip", "remesh/model.fbx", "remesh/thumb.png", "model.glb", "model-obj.zip",
              "model.stl", "rig/front.png", "rig/thumb.png", "rig/rigged.glb", "rig/rigged.fbx"]
             if (d / k).exists()}
    a = {**a, "files": files,
         "downloads": [{"label": FILE_LABELS[k], "url": files[k], "name": f"{a['name']}-{Path(k).name}"}
                       for k in FILE_LABELS if k in files]}
    if with_jobs:
        jobs = store.jobs_for(a["id"])
        for j in jobs:
            if j["status"] == "queued":
                j["queue_position"] = store.queue_position(j["id"])
        a["jobs"] = jobs
        prep = d / "rig" / "prep.json"
        a["rig_prep"] = json.loads(prep.read_text()) if prep.exists() else None
        rj = d / "rig" / "rig.json"
        a["rig_report"] = json.loads(rj.read_text()) if rj.exists() else None
        cdir = d / "rig" / "custom"
        a["custom_anims"] = sorted(f.name for f in cdir.glob("*") if f.suffix.lower() in CUSTOM_ANIM_EXT) \
            if cdir.exists() else []
        mj = d / "rig" / "markers.json"
        a["rig_markers"] = json.loads(mj.read_text())["markers"] if mj.exists() else None
    return a


@app.get("/api/assets", dependencies=[Depends(require_auth)])
def list_assets():
    return [asset_view(a) for a in store.list_assets()]


async def _read_image(up: UploadFile, label: str) -> Image.Image:
    raw = await up.read(MAX_UPLOAD_MB * 1024 * 1024 + 1)
    if len(raw) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"Gambar {label} maksimal {MAX_UPLOAD_MB} MB")
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception:
        raise HTTPException(400, f"File {label} bukan gambar yang valid (PNG/JPG/WEBP)")
    if min(img.size) < 64:
        raise HTTPException(400, f"Gambar {label} terlalu kecil")
    if max(img.size) > 2048:
        img.thumbnail((2048, 2048))
    return img


VIEW_LABEL = {"back": "belakang", "left": "samping kiri", "right": "samping kanan"}


async def _collect_views(**uploads) -> dict[str, Image.Image]:
    out = {}
    for v in VIEWS:
        up = uploads.get(f"image_{v}")
        if up is not None and up.filename:
            out[v] = await _read_image(up, VIEW_LABEL[v])
    return out


@app.post("/api/assets", dependencies=[Depends(require_auth)])
async def create_asset(
    image: UploadFile = File(...),
    image_back: UploadFile | None = File(None),
    image_left: UploadFile | None = File(None),
    image_right: UploadFile | None = File(None),
    name: str = Form(""),
    resolution: int = Form(256),
    target_faces: int = Form(60000),
    remove_bg: bool = Form(True),
    texture_res: int = Form(1024),
    auto_rig: bool = Form(False),
):
    img = await _read_image(image, "utama")
    views = await _collect_views(image_back=image_back, image_left=image_left, image_right=image_right)
    if views and texture_res == 0:
        texture_res = 1024  # foto tambahan hanya berguna untuk tekstur
    resolution = max(128, min(320, resolution))
    target_faces = 0 if target_faces <= 0 else max(5000, min(200000, target_faces))
    texture_res = texture_res if texture_res in (0, 1024, 2048) else 1024
    name = (name or Path(image.filename or "model").stem).strip()[:60] or "model"
    params = {"resolution": resolution, "target_faces": target_faces, "remove_bg": remove_bg,
              "texture_res": texture_res, "auto_rig": auto_rig}
    a = store.create_asset(name, params)
    d = worker.asset_dir(a["id"])
    d.mkdir(parents=True, exist_ok=True)
    img.save(d / "input.png")  # PNG mempertahankan transparansi bila ada
    for v, vimg in views.items():
        vimg.save(d / f"view_{v}.png")
    job = store.create_job(a["id"], "image_to_3d", params)
    if auto_rig:
        store.create_job(a["id"], "rig_prepare", {"height": 1.75})
    worker.wake()
    return {"asset": asset_view(store.get_asset(a["id"])), "job": job}


class TextBody(BaseModel):
    prompt: str = Field(min_length=3, max_length=600)
    name: str = Field("", max_length=60)
    kind: str = Field("character", pattern="^(character|object)$")
    style: str = "stylized"
    turnaround: bool = True
    resolution: int = 256
    target_faces: int = 60000
    texture_res: int = 1024
    auto_rig: bool = False


@app.post("/api/assets/text", dependencies=[Depends(require_auth)])
def create_from_text(body: TextBody):
    """Teks -> gambar (Google Flow via gflow) -> 3D. Dengan turnaround, tampak samping/belakang ikut dibuat."""
    from .textgen import STYLES

    params = {
        "resolution": max(128, min(320, body.resolution)),
        "target_faces": 0 if body.target_faces <= 0 else max(5000, min(200000, body.target_faces)),
        "texture_res": body.texture_res if body.texture_res in (1024, 2048) else 1024,
        "remove_bg": True, "auto_rig": body.auto_rig and body.kind == "character",
        "source": "text", "prompt": body.prompt.strip(), "kind": body.kind,
        "style": body.style if body.style in STYLES else "stylized", "turnaround": body.turnaround,
    }
    name = body.name.strip() or " ".join(body.prompt.split()[:4])[:60]
    a = store.create_asset(name, params)
    worker.asset_dir(a["id"]).mkdir(parents=True, exist_ok=True)
    job = store.create_job(a["id"], "text_to_image", params)
    store.create_job(a["id"], "image_to_3d", params)
    if params["auto_rig"]:
        store.create_job(a["id"], "rig_prepare", {"height": 1.75})
    worker.wake()
    return {"asset": asset_view(store.get_asset(a["id"])), "job": job}


@app.post("/api/assets/{aid}/views", dependencies=[Depends(require_auth)])
async def set_views(
    aid: str,
    image_back: UploadFile | None = File(None),
    image_left: UploadFile | None = File(None),
    image_right: UploadFile | None = File(None),
    remove: str = Form(""),
):
    """Tambah/ganti/hapus foto sisi lain lalu buat ulang model + tekstur (rig ikut dibuat ulang otomatis)."""
    a = _asset_or_404(aid)
    if any(j["asset_id"] == aid for j in store.active_jobs()):
        raise HTTPException(409, "Aset sedang diproses, tunggu sampai selesai")
    views = await _collect_views(image_back=image_back, image_left=image_left, image_right=image_right)
    d = worker.asset_dir(aid)
    assign = (a.get("stats") or {}).get("side_assignment") or {}
    a_side = assign.get("side_a")
    side_file = {a_side: "side_a", ("right" if a_side == "left" else "left"): "side_b"} if a_side else {}
    for v in [x for x in remove.split(",") if x in VIEWS]:
        (d / f"view_{v}.png").unlink(missing_ok=True)
        (d / f"cutout_{v}.png").unlink(missing_ok=True)
        if v in side_file:  # sisi yang berasal dari turnaround
            (d / f"view_{side_file[v]}.png").unlink(missing_ok=True)
    for v, vimg in views.items():
        vimg.save(d / f"view_{v}.png")
    params = dict(a["params"])
    if params.get("texture_res", 1024) == 0:
        params["texture_res"] = 1024
    params["auto_rig"] = False
    store.update_asset(aid, params=params, status="queued")
    job = store.create_job(aid, "image_to_3d", params)
    worker.wake()
    return {"asset": asset_view(store.get_asset(aid)), "job": job}


def _asset_or_404(aid: str) -> dict:
    a = store.get_asset(aid)
    if not a:
        raise HTTPException(404, "Aset tidak ditemukan")
    return a


@app.get("/api/assets/{aid}", dependencies=[Depends(require_auth)])
def get_asset(aid: str):
    return asset_view(_asset_or_404(aid), with_jobs=True)


class Rename(BaseModel):
    name: str = Field(min_length=1, max_length=60)


@app.patch("/api/assets/{aid}", dependencies=[Depends(require_auth)])
def rename_asset(aid: str, body: Rename):
    _asset_or_404(aid)
    store.update_asset(aid, name=body.name.strip())
    return asset_view(store.get_asset(aid))


@app.delete("/api/assets/{aid}", dependencies=[Depends(require_auth)])
def delete_asset(aid: str):
    _asset_or_404(aid)
    if any(j["asset_id"] == aid and j["status"] == "running" for j in store.active_jobs()):
        raise HTTPException(409, "Aset sedang diproses, tunggu sampai selesai")
    store.delete_asset(aid)
    worker.remove_asset_files(aid)
    return {"ok": True}


class PrepareBody(BaseModel):
    height: float = Field(1.75, ge=0.2, le=10)


@app.post("/api/assets/{aid}/rig/prepare", dependencies=[Depends(require_auth)])
def rig_prepare(aid: str, body: PrepareBody):
    a = _asset_or_404(aid)
    if a["status"] != "ready":
        raise HTTPException(409, "Model 3D belum selesai dibuat")
    job = store.create_job(aid, "rig_prepare", body.model_dump())
    worker.wake()
    return job


class RigBody(BaseModel):
    markers: dict[str, list[float]]
    height: float = Field(1.75, ge=0.2, le=10)
    animations: list[str] = Field(default_factory=lambda: list(ANIMATIONS))


@app.post("/api/assets/{aid}/rig", dependencies=[Depends(require_auth)])
def rig(aid: str, body: RigBody):
    a = _asset_or_404(aid)
    if a["status"] != "ready":
        raise HTTPException(409, "Model 3D belum selesai dibuat")
    missing = [k for k in MARKER_KEYS if k not in body.markers or len(body.markers[k]) != 2]
    if missing:
        raise HTTPException(400, f"Penanda belum lengkap: {', '.join(missing)}")
    anims = [x for x in body.animations if x in ANIMATIONS] or list(ANIMATIONS)
    job = store.create_job(aid, "rig", {"markers": {k: body.markers[k] for k in MARKER_KEYS},
                                        "height": body.height, "animations": anims})
    worker.wake()
    return job


def _safe_anim_name(filename: str) -> str:
    import re

    stem, ext = Path(filename or "anim").stem, Path(filename or "").suffix.lower()
    if ext not in CUSTOM_ANIM_EXT:
        raise HTTPException(400, "Format animasi harus .fbx (Mixamo) atau .bvh")
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_")[:40] or "anim"
    return stem + ext


@app.post("/api/assets/{aid}/rig/animations", dependencies=[Depends(require_auth)])
async def add_animations(aid: str, files: list[UploadFile] = File(...)):
    """Unggah animasi Mixamo FBX / BVH; di-retarget ke rig aset ini (rig dibuat ulang bila sudah ada)."""
    a = _asset_or_404(aid)
    if a["status"] != "ready":
        raise HTTPException(409, "Model 3D belum selesai dibuat")
    cdir = worker.asset_dir(aid) / "rig" / "custom"
    cdir.mkdir(parents=True, exist_ok=True)
    saved = []
    for up in files:
        name = _safe_anim_name(up.filename)
        raw = await up.read(MAX_ANIM_MB * 1024 * 1024 + 1)
        if len(raw) > MAX_ANIM_MB * 1024 * 1024:
            raise HTTPException(413, f"{up.filename}: maksimal {MAX_ANIM_MB} MB")
        head = raw[:32]
        if name.endswith(".bvh") and not head.lstrip().upper().startswith(b"HIERARCHY"):
            raise HTTPException(400, f"{up.filename} bukan file BVH")
        if name.endswith(".fbx") and not (head.startswith(b"Kaydara FBX Binary") or b"FBX" in raw[:400]):
            raise HTTPException(400, f"{up.filename} bukan file FBX")
        (cdir / name).write_bytes(raw)
        saved.append(name)
    job = worker.requeue_rig(aid) if a["rig_status"] == "rigged" and not any(
        j["asset_id"] == aid for j in store.active_jobs()) else None
    return {"saved": saved, "job": job}


@app.delete("/api/assets/{aid}/rig/animations/{name}", dependencies=[Depends(require_auth)])
def delete_animation(aid: str, name: str):
    a = _asset_or_404(aid)
    f = worker.asset_dir(aid) / "rig" / "custom" / _safe_anim_name(name)
    if not f.exists():
        raise HTTPException(404, "Animasi tidak ditemukan")
    f.unlink()
    job = worker.requeue_rig(aid) if a["rig_status"] == "rigged" and not any(
        j["asset_id"] == aid for j in store.active_jobs()) else None
    return {"ok": True, "job": job}


class RemeshBody(BaseModel):
    mode: str = Field("quad", pattern="^(quad|tri)$")
    target_faces: int = Field(8000, ge=500, le=200000)
    texture_res: int = 1024
    apply: bool = False


@app.post("/api/assets/{aid}/remesh", dependencies=[Depends(require_auth)])
def remesh(aid: str, body: RemeshBody):
    a = _asset_or_404(aid)
    if a["status"] != "ready":
        raise HTTPException(409, "Model 3D belum selesai dibuat")
    if any(j["asset_id"] == aid for j in store.active_jobs()):
        raise HTTPException(409, "Aset sedang diproses, tunggu sampai selesai")
    params = body.model_dump()
    params["texture_res"] = params["texture_res"] if params["texture_res"] in (1024, 2048) else 1024
    job = store.create_job(aid, "remesh", params)
    worker.wake()
    return job


@app.post("/api/assets/{aid}/remesh/revert", dependencies=[Depends(require_auth)])
def remesh_revert(aid: str):
    _asset_or_404(aid)
    if any(j["asset_id"] == aid for j in store.active_jobs()):
        raise HTTPException(409, "Aset sedang diproses, tunggu sampai selesai")
    if not worker.revert_remesh(aid):
        raise HTTPException(409, "Model asli tidak ada (remesh belum dijadikan model utama)")
    return asset_view(store.get_asset(aid))


@app.get("/api/jobs/{jid}", dependencies=[Depends(require_auth)])
def get_job(jid: str):
    j = store.get_job(jid)
    if not j:
        raise HTTPException(404, "Job tidak ditemukan")
    if j["status"] == "queued":
        j["queue_position"] = store.queue_position(jid)
    return j


@app.get("/api/queue", dependencies=[Depends(require_auth)])
def queue():
    return {"active": store.active_jobs(), "animations": ANIMATIONS}


@app.get("/files/{aid}/{path:path}", dependencies=[Depends(require_auth)])
def files(aid: str, path: str, download: int = 0):
    base = worker.asset_dir(aid).resolve()
    f = (base / path).resolve()
    if base not in f.parents or not f.is_file():
        raise HTTPException(404, "File tidak ditemukan")
    a = store.get_asset(aid)
    name = f"{a['name'] if a else aid}-{f.name}" if download else None
    return FileResponse(f, filename=name, headers={"Cache-Control": "no-cache"})


@app.exception_handler(HTTPException)
async def http_err(_: Request, exc: HTTPException):
    return JSONResponse({"ok": False, "message": exc.detail}, status_code=exc.status_code)


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
