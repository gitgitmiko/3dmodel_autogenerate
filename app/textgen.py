"""Teks -> gambar lewat gflow-cli (Google Flow / Nano Banana) untuk pipeline text-to-3D.

gflow mengendalikan Chrome sungguhan pada akun Google pemilik server, di display virtual Xvfb :99.
Profil Chrome yang sama dipakai otomasi lain (whatif-automation, youtube_short_automation), jadi semua
pemanggil berbagi kunci /tmp/gflow-profile.lock dan yang datang belakangan menunggu.
Mode "classic" dipakai karena kuota gambar harian paket jauh lebih besar daripada kuota mode agen.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Callable

from .config import DATA_DIR

GFLOW = os.environ.get("GFLOW_BIN", str(Path.home() / "github-repos/gflow-cli/.venv/bin/gflow"))
LOCK = "/tmp/gflow-profile.lock"
LOCK_WAIT = int(os.environ.get("GFLOW_LOCK_WAIT", "1800"))
DISPLAY = os.environ.get("GFLOW_DISPLAY", ":99")
PROJECT_FILE = DATA_DIR / "gflow-project.txt"

STYLES = {
    "stylized": "stylized 3D render, smooth clean shapes, game asset",
    "realistic": "realistic 3D render, physically based materials, detailed textures",
    "lowpoly": "low poly 3D render, flat shaded facets, simple colors, game asset",
    "chibi": "cute chibi 3D figure, big head, small body, vinyl toy look",
    "anime": "anime style 3D figure, cel-shaded look, clean colors",
}


class TextGenError(RuntimeError):
    pass


def build_prompt(subject: str, kind: str, style: str, turnaround: bool) -> str:
    style_txt = STYLES.get(style, STYLES["stylized"])
    if kind == "character":
        pose = ("identical relaxed A-pose in every view with arms slightly away from the body and legs slightly "
                "apart, feet flat on the ground") if turnaround else (
                "standing in a relaxed A-pose with arms slightly away from the body and legs slightly apart")
        noun = "character"
    else:
        pose = "same orientation and scale in every view" if turnaround else "slightly elevated front view"
        noun = "object"
    if turnaround:
        return (f"Turnaround reference sheet of a single {noun}: {subject}. Exactly four full views of the same "
                f"{noun} side by side in one row, evenly spaced and not overlapping, left to right: front view, "
                f"side view, back view, other side view. {pose.capitalize()}, same scale, aligned on the same "
                f"baseline, each view fully visible. Plain pure white background, no text, no labels, no shadows, "
                f"soft even studio lighting, orthographic, {style_txt}")
    return (f"A single {noun}: {subject}. Full view, {pose}, facing the camera, centered, entirely visible with "
            f"margin around it. Plain pure white background, no text, no shadow, soft even studio lighting, "
            f"{style_txt}")


def ensure_display():
    """Chrome milik gflow butuh X server; Xvfb tidak otomatis hidup lagi setelah reboot."""
    n = DISPLAY.lstrip(":")
    if Path(f"/tmp/.X11-unix/X{n}").exists():
        return
    Path(f"/tmp/.X{n}-lock").unlink(missing_ok=True)
    subprocess.Popen(["Xvfb", DISPLAY, "-screen", "0", "1920x1080x24", "-nolisten", "tcp"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(50):
        if Path(f"/tmp/.X11-unix/X{n}").exists():
            return
        time.sleep(0.2)
    raise TextGenError(f"Xvfb tidak bisa dijalankan di {DISPLAY}")


def _env():
    return {**os.environ, "DISPLAY": DISPLAY, "GFLOW_CLI_UI_MODE": "classic"}


def _lock_busy() -> bool:
    r = subprocess.run(["flock", "-n", LOCK, "true"])
    return r.returncode != 0


def _gflow(*args: str, timeout: int = 900) -> dict:
    r = subprocess.run(["flock", "-w", str(LOCK_WAIT), LOCK, GFLOW, *args, "--json"], capture_output=True,
                       text=True, timeout=timeout + LOCK_WAIT, env=_env())
    out = r.stdout
    data = {}
    if "{" in out:
        try:
            data = json.loads(out[out.index("{"):out.rindex("}") + 1])
        except json.JSONDecodeError:
            data = {}
    if data.get("status") == "ok":
        return data
    said = " ".join(re.sub(r"[^\w\s]", " ", out + r.stderr).lower().split())
    if "batas kuota" in said or "quota" in said:
        raise TextGenError("Kuota gambar harian Google Flow habis; coba lagi besok.")
    err = data.get("error") or {}
    detail = err.get("detail") or err.get("title") or (r.stderr or out)[-600:]
    if "not to the flow app" in said or "auth login" in said:
        detail = ("Sesi Google Flow di server kedaluwarsa. Jalankan di server: "
                  "cd ~/github-repos/gflow-cli && DISPLAY=:99 .venv/bin/gflow auth login. Detail: " + str(detail))
    raise TextGenError(f"gflow gagal: {detail}")


def _project_id() -> str:
    if PROJECT_FILE.exists():
        return PROJECT_FILE.read_text().strip()
    data = _gflow("project", "create", "--name", "gitgit3d", timeout=300)
    pid = data.get("project_id")
    if not pid:
        raise TextGenError("gflow tidak mengembalikan project_id")
    PROJECT_FILE.write_text(pid)
    return pid


def generate(prompt: str, out_dir: Path, aspect: str, progress: Callable[[str], None] = lambda m: None) -> Path:
    """Buat satu gambar; kembalikan path file lokal."""
    ensure_display()
    if _lock_busy():
        progress("Menunggu giliran Google Flow (sedang dipakai otomasi lain)")
    pid = _project_id()
    progress("Membuat gambar dengan Google Flow (Nano Banana)")
    out_dir.mkdir(parents=True, exist_ok=True)
    data = _gflow("image", "t2i", prompt, "--aspect", aspect, "--project", pid, "--out", str(out_dir))
    paths = [im.get("local_path") for im in data.get("images", []) if im.get("local_path")]
    paths = [Path(p) for p in paths if Path(p).exists()]
    if not paths:
        raise TextGenError("gflow selesai tanpa file gambar")
    return paths[0]
