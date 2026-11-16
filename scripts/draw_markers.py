"""Debug: gambar penanda prep.json di atas front.png -> markers.png"""
import json, sys
from pathlib import Path
from PIL import Image, ImageDraw
d = Path(sys.argv[1]); prep = json.loads((d / "prep.json").read_text())
mk = json.loads(Path(sys.argv[2]).read_text())["markers"] if len(sys.argv) > 2 else prep["markers"]
f = prep["frame"]; im = Image.open(d / "front.png").convert("RGB"); dr = ImageDraw.Draw(im)
def px(x, z): return ((x - f["cx"]) / f["size"] * f["px"] + f["px"] / 2, f["px"] / 2 - (z - f["cz"]) / f["size"] * f["px"])
for k, (x, z) in mk.items():
    u, v = px(x, z); dr.ellipse([u - 7, v - 7, u + 7, v + 7], outline=(255, 60, 40), width=3); dr.text((u + 9, v - 6), k, fill=(255, 230, 0))
im.save(d / "markers.png")
