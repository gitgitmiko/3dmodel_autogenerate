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
from .config import (ANIMATIONS, APP_PASSWORD, COOKIE_SECURE, MAX_UPLOAD_MB, SECRET_KEY, SESSION_DAYS,
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
    "rig/rigged.glb": "Rig + animasi (GLB)", "rig/rigged.fbx": "Rig + animasi (FBX, Unity/Unreal)",
}


def asset_view(a: dict, with_jobs=False) -> dict:
    d = worker.asset_dir(a["id"])
    files = {k: f"files/{a['id']}/{k}" for k in
             ["input.png", "input.jpg", "input.webp", "cutout.png", "thumb.png", "model.glb", "model-obj.zip",
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
        mj = d / "rig" / "markers.json"
        a["rig_markers"] = json.loads(mj.read_text())["markers"] if mj.exists() else None
    return a


@app.get("/api/assets", dependencies=[Depends(require_auth)])
def list_assets():
    return [asset_view(a) for a in store.list_assets()]


@app.post("/api/assets", dependencies=[Depends(require_auth)])
async def create_asset(
    image: UploadFile = File(...),
    name: str = Form(""),
    resolution: int = Form(256),
    target_faces: int = Form(60000),
    remove_bg: bool = Form(True),
    texture_res: int = Form(1024),
    auto_rig: bool = Form(False),
):
    raw = await image.read(MAX_UPLOAD_MB * 1024 * 1024 + 1)
    if len(raw) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"Gambar maksimal {MAX_UPLOAD_MB} MB")
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception:
        raise HTTPException(400, "File bukan gambar yang valid (PNG/JPG/WEBP)")
    if min(img.size) < 64:
        raise HTTPException(400, "Gambar terlalu kecil")
    resolution = max(128, min(320, resolution))
    target_faces = 0 if target_faces <= 0 else max(5000, min(200000, target_faces))
    texture_res = texture_res if texture_res in (0, 1024, 2048) else 1024
    name = (name or Path(image.filename or "model").stem).strip()[:60] or "model"
    params = {"resolution": resolution, "target_faces": target_faces, "remove_bg": remove_bg,
              "texture_res": texture_res, "auto_rig": auto_rig}
    a = store.create_asset(name, params)
    d = worker.asset_dir(a["id"])
    d.mkdir(parents=True, exist_ok=True)
    # Simpan sebagai PNG (mempertahankan transparansi bila ada); perkecil gambar raksasa.
    if max(img.size) > 2048:
        img.thumbnail((2048, 2048))
    img.save(d / "input.png")
    job = store.create_job(a["id"], "image_to_3d", params)
    if auto_rig:
        store.create_job(a["id"], "rig_prepare", {"height": 1.75})
    worker.wake()
    return {"asset": asset_view(store.get_asset(a["id"])), "job": job}


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
