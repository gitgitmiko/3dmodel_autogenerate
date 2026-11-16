"""Potong "character turnaround sheet" (beberapa tampak berjajar dalam satu gambar) menjadi gambar per tampak."""

from __future__ import annotations

import numpy as np
from PIL import Image


def split_turnaround(image: Image.Image, expected: int = 4, session=None) -> list[Image.Image]:
    """Kembalikan daftar figur RGBA persegi (latar transparan), urut kiri -> kanan.

    Figur dipisahkan lewat komponen terhubung pada alpha hasil rembg. Bagian kecil yang lepas
    (bulu helm, pedang) digabung ke figur terdekat lewat dilasi horizontal terbatas.
    """
    import rembg
    from scipy import ndimage

    rgba = rembg.remove(image.convert("RGB"), session=session)
    a = np.asarray(rgba)[:, :, 3] > 100
    H, W = a.shape
    # Dilasi vertikal kuat (menyatukan bagian satu figur), horizontal lemah (agar figur bertetangga tidak menyatu).
    grown = ndimage.binary_dilation(a, structure=np.ones((max(3, H // 25), max(3, W // 200)), bool))
    labels, n = ndimage.label(grown)
    if n == 0:
        return []
    areas = ndimage.sum(a, labels, index=range(1, n + 1))
    keep = [i + 1 for i in np.argsort(areas)[::-1][:expected] if areas[i] > areas.max() * 0.15]
    figs = []
    for lab in keep:
        ys, xs = np.nonzero((labels == lab) & a)
        figs.append((xs.min(), xs.max(), ys.min(), ys.max(), lab))
    figs.sort(key=lambda f: (f[0] + f[1]) / 2)

    out = []
    arr = np.asarray(rgba).copy()
    for x0, x1, y0, y1, lab in figs:
        crop = arr[y0:y1 + 1, x0:x1 + 1].copy()
        crop[~(labels[y0:y1 + 1, x0:x1 + 1] == lab), 3] = 0  # buang bagian figur tetangga
        h, w = crop.shape[:2]
        side = int(max(h, w) * 1.1)
        canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        canvas.paste(Image.fromarray(crop, "RGBA"), ((side - w) // 2, (side - h) // 2))
        out.append(canvas)
    return out
