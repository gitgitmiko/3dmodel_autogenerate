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
# Karakter menghadap kamera depan, jadi sisi kiri karakter = +Y.
# Kamera tambahan diputar mengelilingi sumbu Z: kiri (kamera di sisi kiri karakter) = 90°, belakang = 180°.
CAM_DIST = 1.9
CAM_F = 1.0 / (2.0 * np.tan(np.radians(20.0)))
VIEW_AZIMUTH = {"front": 0.0, "left": 90.0, "back": 180.0, "right": 270.0}
VIEW_LABEL = {"front": "depan", "left": "kiri", "back": "belakang", "right": "kanan"}


def _camera(view: str):
    a = np.radians(VIEW_AZIMUTH[view])
    c = np.array([np.cos(a), np.sin(a), 0.0], dtype=np.float32) * CAM_DIST
    look = -c / CAM_DIST
    right = np.cross(look, [0.0, 0.0, 1.0]).astype(np.float32)
    return c, look, right / np.linalg.norm(right), np.array([0, 0, 1], dtype=np.float32)


def _project(p: np.ndarray, view: str = "front"):
    """Titik TripoSR -> (u, v) gambar kamera `view` dalam 0..1 (v=0 di atas) + kedalaman."""
    c, look, right, up = _camera(view)
    d = p - c
    depth = d @ look
    return 0.5 + (d @ right) / depth * CAM_F, 0.5 - (d @ up) / depth * CAM_F, depth


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    return float((a & b).sum() / max((a | b).sum(), 1))


def _bbox(m: np.ndarray):
    ys, xs = np.nonzero(m)
    return xs.min(), xs.max(), ys.min(), ys.max()


def _fit_view(sil: np.ndarray, alpha: np.ndarray):
    """Cari skala & geser (dalam satuan uv) yang memetakan siluet mesh ke siluet gambar.

    Foto belakang/samping dari pengguna tidak dibingkai persis seperti gambar depan, jadi uv proyeksi
    dikoreksi: uv_gambar = s * (uv_mesh - c_mesh) + c_gambar. Skala awal dari tinggi bbox (lebar berubah
    menurut pose lengan), lalu disempurnakan dengan pencarian kisi kecil yang memaksimalkan IoU.
    """
    from scipy import ndimage

    R = sil.shape[0]
    mx0, mx1, my0, my1 = _bbox(sil)
    ax0, ax1, ay0, ay1 = _bbox(alpha)
    cm = np.array([(mx0 + mx1) / 2, (my0 + my1) / 2]) / R
    ca = np.array([(ax0 + ax1) / 2, (ay0 + ay1) / 2]) / R
    s0 = (ay1 - ay0) / max(my1 - my0, 1)
    yy, xx = np.mgrid[0:R, 0:R]
    gu, gv = (xx + 0.5) / R, (yy + 0.5) / R
    best = (-1.0, s0, 0.0, 0.0)
    for s in s0 * np.linspace(0.92, 1.08, 9):
        for dx in np.linspace(-0.03, 0.03, 7):
            for dy in np.linspace(-0.03, 0.03, 7):
                # titik gambar -> titik mesh (invers), lalu ambil siluet mesh di sana
                mu = (gu - ca[0] - dx) / s + cm[0]
                mv = (gv - ca[1] - dy) / s + cm[1]
                iu = np.clip((mu * R).astype(int), 0, R - 1)
                iv = np.clip((mv * R).astype(int), 0, R - 1)
                inside = (mu >= 0) & (mu < 1) & (mv >= 0) & (mv < 1)
                warped = sil[iv, iu] & inside
                score = _iou(warped, alpha)
                if score > best[0]:
                    best = (score, s, dx, dy)
    score, s, dx, dy = best
    return score, (float(s), cm, ca + np.array([dx, dy]))


def _view_weights(pos: np.ndarray, normals: np.ndarray, image_rgba: Image.Image, view: str):
    """Hitung (warna, bobot, info) proyeksi satu gambar ke texel."""
    from scipy import ndimage

    rgba = np.asarray(image_rgba.convert("RGBA")).astype(np.float32) / 255.0
    S = rgba.shape[0]
    u, v, depth = _project(pos, view)

    # Depth buffer dari texel itu sendiri (cukup rapat) untuk menguji keterhalangan.
    R = 256
    iu = np.clip((u * R).astype(int), 0, R - 1)
    iv = np.clip((v * R).astype(int), 0, R - 1)
    zbuf = np.full((R, R), np.inf, dtype=np.float32)
    np.minimum.at(zbuf, (iv, iu), depth)
    sil = np.isfinite(zbuf)
    zbuf = ndimage.minimum_filter(np.where(sil, zbuf, 1e9), size=3)
    visible = depth <= zbuf[iv, iu] + 0.025
    sil = ndimage.binary_fill_holes(ndimage.binary_closing(sil, iterations=2))
    alpha_small = np.asarray(image_rgba.convert("RGBA").resize((R, R)))[:, :, 3] > 127

    if view == "front":  # gambar depan = masukan model, kamera tepat sama
        score, xf = _iou(sil, alpha_small), None
    else:
        score, xf = _fit_view(sil, alpha_small)
    info = {"iou": round(score, 3)}
    if score < 0.6:
        info["used"] = False
        return None, None, info
    if xf is not None:
        s, cm, ca = xf
        u, v = s * (u - cm[0]) + ca[0], s * (v - cm[1]) + ca[1]
        info["scale"] = round(s, 3)

    c = _camera(view)[0]
    vd = c[None] - pos
    vd /= np.linalg.norm(vd, axis=1, keepdims=True)
    facing = (normals * vd).sum(1)
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

    w = w * np.clip((bil(alpha[..., None])[:, 0] - 0.6) / 0.4, 0, 1)
    info.update(used=True, coverage=round(float((w > 0.5).mean()), 3))
    return bil(rgba[:, :, :3]), w, info


def project_views(base: np.ndarray, pos: np.ndarray, normals: np.ndarray, views: dict[str, Image.Image]):
    """Gabungkan beberapa foto ke warna texel. base = warna NeRF (dipakai di area yang tak terlihat foto mana pun).

    Tiap texel memakai foto yang paling tegak lurus terhadap permukaannya (bobot dipangkatkan agar transisi
    antar foto tidak terlalu lebar), lalu dicampur dengan warna NeRF sesuai cakupan total.
    """
    num = np.zeros_like(base)
    den = np.zeros(len(base), dtype=np.float32)
    cover = np.zeros(len(base), dtype=np.float32)
    infos = {}
    for view, img in views.items():
        col, w, info = _view_weights(pos, normals, img, view)
        infos[view] = info
        if col is None:
            continue
        sharp = w ** 4
        num += col * sharp[:, None]
        den += sharp
        cover = np.maximum(cover, w)
    proj = num / np.maximum(den, 1e-6)[:, None]
    out = base * (1 - cover[:, None]) + proj * cover[:, None]
    return out, {"projected": any(i.get("used") for i in infos.values()), "views": infos}


def bake_texture(mesh: trimesh.Trimesh, model, scene_code: torch.Tensor, tex_res: int = 1024,
                 views: dict[str, Image.Image] | None = None) -> tuple[trimesh.Trimesh, dict]:
    """mesh harus masih di sistem koordinat TripoSR (sebelum diputar/diskalakan).

    views: {"front"|"back"|"left"|"right": gambar RGBA persegi tanpa background}. "front" harus gambar
    yang sama persis dengan masukan model; yang lain diselaraskan otomatis lewat siluet.
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
    if views:
        face_normals = trimesh.Trimesh(vertices=verts, faces=faces, process=False).face_normals
        colors, info = project_views(colors, pos[mask], face_normals[fid[mask]].astype(np.float32), views)
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
