"""Texture bake di CPU: UV atlas (xatlas) + warna dari medan warna TripoSR per texel.

Dibanding vertex color (±30 ribu sampel warna), tekstur 1024² menyimpan ±600 ribu sampel,
sehingga detail warna yang sudah ada di medan NeRF tidak hilang saat mesh disederhanakan.
"""

from __future__ import annotations

import logging
import time

import numpy as np
import torch
import trimesh
from PIL import Image

log = logging.getLogger("gitgit3d.texture")


def unwrap(vertices: np.ndarray, faces: np.ndarray, tex_res: int):
    """UV unwrap dengan xatlas. Kembalikan (vmapping, faces_baru, uv[0..1])."""
    import xatlas

    atlas = xatlas.Atlas()
    atlas.add_mesh(vertices.astype(np.float32), faces.astype(np.uint32))
    chart = xatlas.ChartOptions()
    chart.max_iterations = 1  # cukup untuk mesh organik; iterasi lebih banyak jauh lebih lambat
    pack = xatlas.PackOptions()
    pack.resolution = tex_res
    pack.padding = max(2, tex_res // 256)
    pack.bilinear = True
    pack.bruteForce = False
    atlas.generate(chart_options=chart, pack_options=pack)
    return atlas[0]


def rasterize_positions(vertices: np.ndarray, faces: np.ndarray, uvs: np.ndarray, res: int):
    """Rasterisasi segitiga di ruang UV -> posisi 3D tiap texel.

    Kembalikan (pos[res,res,3], mask[res,res], face_id[res,res]).

    Baris gambar 0 = v=1 (konvensi gambar), sehingga cocok dengan UV OpenGL yang dipakai trimesh.
    """
    pos = np.zeros((res, res, 3), dtype=np.float32)
    mask = np.zeros((res, res), dtype=bool)
    fid = np.zeros((res, res), dtype=np.int32)
    px = np.stack([uvs[:, 0] * res, (1.0 - uvs[:, 1]) * res], axis=1)  # (x, y) piksel
    tri_px = px[faces]            # F,3,2
    tri_v = vertices[faces]       # F,3,3
    lo = np.floor(tri_px.min(1) - 0.5).astype(int).clip(0, res - 1)
    hi = np.ceil(tri_px.max(1) + 0.5).astype(int).clip(0, res - 1)
    a, b, c = tri_px[:, 0], tri_px[:, 1], tri_px[:, 2]
    den = (b[:, 1] - c[:, 1]) * (a[:, 0] - c[:, 0]) + (c[:, 0] - b[:, 0]) * (a[:, 1] - c[:, 1])
    eps = -0.02  # sedikit melebar agar tidak ada celah di tepi segitiga
    for i in np.nonzero(np.abs(den) > 1e-12)[0]:
        x0, y0, x1, y1 = lo[i, 0], lo[i, 1], hi[i, 0], hi[i, 1]
        xs = np.arange(x0, x1 + 1) + 0.5
        ys = np.arange(y0, y1 + 1) + 0.5
        gx, gy = np.meshgrid(xs, ys)
        w0 = ((b[i, 1] - c[i, 1]) * (gx - c[i, 0]) + (c[i, 0] - b[i, 0]) * (gy - c[i, 1])) / den[i]
        w1 = ((c[i, 1] - a[i, 1]) * (gx - c[i, 0]) + (a[i, 0] - c[i, 0]) * (gy - c[i, 1])) / den[i]
        w2 = 1.0 - w0 - w1
        inside = (w0 >= eps) & (w1 >= eps) & (w2 >= eps)
        if not inside.any():
            continue
        p = w0[..., None] * tri_v[i, 0] + w1[..., None] * tri_v[i, 1] + w2[..., None] * tri_v[i, 2]
        sub_m = mask[y0:y1 + 1, x0:x1 + 1]
        sub_p = pos[y0:y1 + 1, x0:x1 + 1]
        sub_p[inside] = p[inside]
        fid[y0:y1 + 1, x0:x1 + 1][inside] = i
        sub_m |= inside
    return pos, mask, fid


# Kamera gambar input TripoSR: di sumbu +X, jarak 1.9, FOV 40°, atas = +Z, kanan gambar = +Y.
CAM_DIST = 1.9
CAM_F = 1.0 / (2.0 * np.tan(np.radians(20.0)))


def _project(p: np.ndarray):
    """Titik TripoSR -> (u, v) gambar input dalam 0..1 (v=0 di atas) + kedalaman."""
    depth = CAM_DIST - p[:, 0]
    return 0.5 + p[:, 1] / depth * CAM_F, 0.5 - p[:, 2] / depth * CAM_F, depth


def project_input_image(img: np.ndarray, pos: np.ndarray, normals: np.ndarray, image_rgba: Image.Image):
    """Timpa texel yang terlihat dari kamera input dengan warna gambar asli (detail resolusi penuh).

    img/pos/normals: array per texel terisi (N,3). Kembalikan (img_baru, info).
    Medan NeRF hanya menyimpan warna buram; gambar asli adalah satu-satunya sumber detail tajam,
    tapi hanya sah untuk permukaan yang menghadap kamera dan tidak terhalang.
    """
    from scipy import ndimage

    rgba = np.asarray(image_rgba.convert("RGBA")).astype(np.float32) / 255.0
    S = rgba.shape[0]
    u, v, depth = _project(pos)

    # Depth buffer dari texel itu sendiri (cukup rapat) untuk menguji keterhalangan.
    R = 384
    iu = np.clip((u * R).astype(int), 0, R - 1)
    iv = np.clip((v * R).astype(int), 0, R - 1)
    zbuf = np.full((R, R), np.inf, dtype=np.float32)
    np.minimum.at(zbuf, (iv, iu), depth)
    sil = np.isfinite(zbuf)
    zbuf = ndimage.minimum_filter(np.where(sil, zbuf, 1e9), size=3)
    visible = depth <= zbuf[iv, iu] + 0.025

    # Pengaman: siluet mesh harus cocok dengan alpha gambar, kalau tidak proyeksi akan bergeser.
    alpha_small = np.asarray(image_rgba.convert("RGBA").resize((R, R)))[:, :, 3] > 127
    sil = ndimage.binary_fill_holes(ndimage.binary_closing(sil, iterations=2))
    iou = float((sil & alpha_small).sum() / max((sil | alpha_small).sum(), 1))
    if iou < 0.6:
        return img, {"projected": False, "iou": round(iou, 3)}

    view = np.array([CAM_DIST, 0, 0], dtype=np.float32) - pos
    view /= np.linalg.norm(view, axis=1, keepdims=True)
    facing = (normals * view).sum(1)
    w = np.clip((facing - 0.25) / 0.35, 0, 1)
    w = w * w * (3 - 2 * w) * visible

    # Jauhi tepi siluet gambar (alpha dikikis) supaya warna background tidak ikut terbawa.
    alpha = ndimage.minimum_filter(rgba[:, :, 3], size=max(3, S // 100))
    x = np.clip(u * S - 0.5, 0, S - 1.001)
    y = np.clip(v * S - 0.5, 0, S - 1.001)
    x0, y0 = x.astype(int), y.astype(int)
    fx, fy = (x - x0)[:, None], (y - y0)[:, None]

    def bil(a):
        return (a[y0, x0] * (1 - fx) * (1 - fy) + a[y0, x0 + 1] * fx * (1 - fy)
                + a[y0 + 1, x0] * (1 - fx) * fy + a[y0 + 1, x0 + 1] * fx * fy)

    w = (w * np.clip((bil(alpha[..., None])[:, 0] - 0.6) / 0.4, 0, 1))[:, None]
    out = img * (1 - w) + bil(rgba[:, :, :3]) * w
    return out, {"projected": True, "iou": round(iou, 3), "coverage": round(float((w > 0.5).mean()), 3)}


def bake_texture(mesh: trimesh.Trimesh, model, scene_code: torch.Tensor, tex_res: int = 1024,
                 image_rgba: Image.Image | None = None) -> tuple[trimesh.Trimesh, dict]:
    """mesh harus masih di sistem koordinat TripoSR (sebelum diputar/diskalakan).

    image_rgba: gambar input persegi tanpa background (yang sama persis dengan masukan model);
    bila diberikan, bagian depan tekstur diambil dari gambar itu.
    """
    t = time.time()
    normals = np.asarray(mesh.vertex_normals)
    vmapping, faces, uvs = unwrap(np.asarray(mesh.vertices), np.asarray(mesh.faces), tex_res)
    verts = np.asarray(mesh.vertices)[vmapping]
    t_unwrap = time.time() - t

    t = time.time()
    pos, mask, fid = rasterize_positions(verts.astype(np.float32), faces.astype(np.int64), uvs, tex_res)
    t_rast = time.time() - t

    t = time.time()
    with torch.no_grad():
        col = model.renderer.query_triplane(model.decoder, torch.from_numpy(pos[mask]), scene_code)["color"]
    colors = col.clamp(0, 1).cpu().numpy()
    info = {"projected": False}
    if image_rgba is not None:
        face_normals = trimesh.Trimesh(vertices=verts, faces=faces, process=False).face_normals
        colors, info = project_input_image(colors, pos[mask], face_normals[fid[mask]].astype(np.float32), image_rgba)
    img = np.zeros((tex_res, tex_res, 3), dtype=np.float32)
    img[mask] = colors

    # Isi texel kosong dengan warna texel terisi terdekat, supaya tidak ada garis gelap di sambungan UV.
    from scipy import ndimage

    idx = ndimage.distance_transform_edt(~mask, return_distances=False, return_indices=True)
    img = img[idx[0], idx[1]]
    texture = Image.fromarray((img * 255).round().astype(np.uint8), "RGB")
    log.info("bake %dpx: unwrap %.1fs, raster %.1fs, warna %.1fs, %d texel terisi, proyeksi %s",
             tex_res, t_unwrap, t_rast, time.time() - t, int(mask.sum()), info)

    material = trimesh.visual.material.PBRMaterial(
        baseColorTexture=texture, baseColorFactor=[255, 255, 255, 255], metallicFactor=0.0, roughnessFactor=0.9)
    out = trimesh.Trimesh(vertices=verts, faces=faces, vertex_normals=normals[vmapping], process=False)
    out.visual = trimesh.visual.TextureVisuals(uv=uvs, material=material)
    return out, info
