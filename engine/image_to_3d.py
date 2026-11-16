"""Image -> 3D (GLB) memakai TripoSR di CPU.

Alur: hapus background (rembg) -> crop & pusatkan objek -> TripoSR (triplane NeRF)
-> marching cubes -> bersihkan mesh -> haluskan -> kurangi poligon -> GLB berwarna (vertex color).
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import torch
import trimesh
from PIL import Image

log = logging.getLogger("gitgit3d.image_to_3d")

# Config TripoSR menyebut kelas sebagai "tsr.models....", jadi folder engine/ harus ada di sys.path.
_ENGINE_DIR = str(Path(__file__).resolve().parent)
if _ENGINE_DIR not in sys.path:
    sys.path.insert(0, _ENGINE_DIR)

MODEL_ID = os.environ.get("TRIPOSR_MODEL", "stabilityai/TripoSR")
# Batasi thread agar tidak mengganggu layanan lain di server (Frigate, dsb).
TORCH_THREADS = int(os.environ.get("TORCH_THREADS", "4"))

ProgressFn = Callable[[float, str], None]


@dataclass
class ImageTo3DOptions:
    resolution: int = 256          # resolusi marching cubes (128-320); makin tinggi makin detail & lambat
    target_faces: int = 60000      # 0 = tanpa decimation
    remove_bg: bool = True
    foreground_ratio: float = 0.85
    smooth_iters: int = 5          # Taubin smoothing, 0 = mati
    height_m: float = 1.0          # tinggi akhir model (meter); rigging menskalakan ulang sendiri
    texture_res: int = 1024        # 0 = vertex color saja; 1024/2048 = bake tekstur UV
    project_image: bool = True     # tempel gambar asli ke sisi depan tekstur (lebih tajam)


_model = None
_rembg_session = None
_lock = threading.Lock()


def _load_model():
    global _model
    if _model is None:
        from tsr.system import TSR

        torch.set_num_threads(TORCH_THREADS)
        t = time.time()
        _model = TSR.from_pretrained(MODEL_ID, config_name="config.yaml", weight_name="model.ckpt")
        _model.renderer.set_chunk_size(8192)
        _model.to("cpu").eval()
        log.info("TripoSR dimuat dalam %.1fs", time.time() - t)
    return _model


def unload() -> bool:
    """Lepas model dari RAM. Kembalikan True bila ada yang dilepas."""
    global _model
    with _lock:
        if _model is None:
            return False
        _model = None
    import gc

    gc.collect()
    return True


def _load_rembg():
    global _rembg_session
    if _rembg_session is None:
        import rembg

        _rembg_session = rembg.new_session(os.environ.get("REMBG_MODEL", "isnet-general-use"))
    return _rembg_session


def preprocess(image: Image.Image, opts: ImageTo3DOptions) -> tuple[Image.Image, Image.Image]:
    """Kembalikan (gambar RGB siap model, gambar RGBA tanpa background untuk pratinjau)."""
    from tsr.utils import resize_foreground

    image = image.convert("RGBA")
    alpha = np.array(image)[:, :, 3]
    already_cut = alpha.min() < 250  # gambar sudah transparan -> lewati rembg
    if opts.remove_bg and not already_cut:
        import rembg

        image = rembg.remove(image.convert("RGB"), session=_load_rembg())
    if np.array(image)[:, :, 3].max() == 0:
        raise ValueError("Objek tidak terdeteksi di gambar (hasil hapus background kosong).")
    image = resize_foreground(image, opts.foreground_ratio)
    cutout = image.copy()
    arr = np.array(image).astype(np.float32) / 255.0
    arr = arr[:, :, :3] * arr[:, :, 3:4] + (1 - arr[:, :, 3:4]) * 0.5
    return Image.fromarray((arr * 255.0).astype(np.uint8)), cutout


def _clean(mesh: trimesh.Trimesh, opts: ImageTo3DOptions) -> trimesh.Trimesh:
    # Buang potongan kecil yang melayang (artefak NeRF), simpan komponen besar saja.
    parts = mesh.split(only_watertight=False)
    if len(parts) > 1:
        biggest = max(len(p.faces) for p in parts)
        parts = [p for p in parts if len(p.faces) >= biggest * 0.05]
        mesh = trimesh.util.concatenate(parts)
    mesh.merge_vertices()
    mesh.update_faces(mesh.nondegenerate_faces())
    mesh.remove_unreferenced_vertices()

    if opts.smooth_iters > 0:
        trimesh.smoothing.filter_taubin(mesh, iterations=opts.smooth_iters)

    if opts.target_faces and len(mesh.faces) > opts.target_faces:
        colors = mesh.visual.vertex_colors.copy()
        src_vertices = mesh.vertices.copy()
        mesh = mesh.simplify_quadric_decimation(face_count=opts.target_faces)
        # Decimation membuang vertex color; ambil lagi dari vertex asli terdekat.
        from scipy.spatial import cKDTree

        _, idx = cKDTree(src_vertices).query(mesh.vertices)
        mesh.visual = trimesh.visual.ColorVisuals(mesh, vertex_colors=colors[idx])

    mesh.fix_normals()
    return mesh


def _orient_and_normalize(mesh: trimesh.Trimesh, height_m: float) -> trimesh.Trimesh:
    # Sistem koordinat TripoSR: Z ke atas, kamera input di sumbu +X.
    # glTF: Y ke atas, depan model menghadap +Z (rotasi gradio TripoSR justru membuatnya menghadap -Z).
    mesh.apply_transform(trimesh.transformations.rotation_matrix(-np.pi / 2, [1, 0, 0]))
    mesh.apply_transform(trimesh.transformations.rotation_matrix(-np.pi / 2, [0, 1, 0]))
    lo, hi = mesh.bounds
    center = (lo + hi) / 2
    mesh.apply_translation([-center[0], -lo[1], -center[2]])  # pusat di XZ, kaki di lantai
    h = hi[1] - lo[1]
    if h > 0 and height_m > 0:
        mesh.apply_scale(height_m / h)
    return mesh


def image_to_3d(
    image_path: str | Path,
    out_dir: str | Path,
    opts: ImageTo3DOptions | None = None,
    progress: ProgressFn | None = None,
) -> dict:
    opts = opts or ImageTo3DOptions()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = progress or (lambda p, m: None)
    t0 = time.time()
    tex_info: dict = {}

    with _lock:  # satu inferensi per proses; CPU tidak sanggup paralel
        report(0.05, "Memuat model")
        model = _load_model()

        report(0.15, "Menghapus background")
        model_input, cutout = preprocess(Image.open(image_path), opts)
        cutout.save(out_dir / "cutout.png")

        report(0.30, "Membangun representasi 3D (TripoSR)")
        with torch.no_grad():
            scene_codes = model([model_input], device="cpu")

        report(0.55, f"Mengekstrak mesh ({opts.resolution}³)")
        with torch.no_grad():
            mesh = model.extract_mesh(scene_codes, True, resolution=opts.resolution)[0]

        report(0.72, "Membersihkan & menghaluskan mesh")
        raw_faces = len(mesh.faces)
        mesh = _clean(mesh, opts)

        if opts.texture_res > 0:
            report(0.80, f"Bake tekstur {opts.texture_res}px")
            from engine.texture import bake_texture

            mesh, tex_info = bake_texture(mesh, model, scene_codes[0], opts.texture_res,
                                          image_rgba=cutout if opts.project_image else None)
        else:
            # glTF menganggap COLOR_0 linear, sedangkan keluaran TripoSR sRGB -> konversi agar tidak pucat.
            c = np.asarray(mesh.visual.vertex_colors).astype(np.float32) / 255.0
            c[:, :3] = np.where(c[:, :3] <= 0.04045, c[:, :3] / 12.92, ((c[:, :3] + 0.055) / 1.055) ** 2.4)
            mesh.visual = trimesh.visual.ColorVisuals(mesh, vertex_colors=(c * 255).round().astype(np.uint8))

    mesh = _orient_and_normalize(mesh, opts.height_m)

    report(0.94, "Ekspor GLB")
    glb = out_dir / "model.glb"
    mesh.export(glb)

    stats = {
        "vertices": int(len(mesh.vertices)),
        "faces": int(len(mesh.faces)),
        "raw_faces": int(raw_faces),
        "texture": int(opts.texture_res),
        "texture_projected": bool(tex_info.get("projected")),
        "seconds": round(time.time() - t0, 1),
        "bounds": mesh.bounds.round(4).tolist(),
    }
    report(1.0, "Selesai")
    return {"glb": str(glb), "cutout": str(out_dir / "cutout.png"), "stats": stats}


if __name__ == "__main__":  # uji cepat: python -m engine.image_to_3d gambar.png out/
    import json

    logging.basicConfig(level=logging.INFO)
    res = image_to_3d(sys.argv[1], sys.argv[2], progress=lambda p, m: print(f"[{p:4.0%}] {m}", flush=True))
    print(json.dumps(res, indent=2))
