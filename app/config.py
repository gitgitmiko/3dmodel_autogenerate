import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_env(path: Path) -> None:
    """Muat .env sederhana (KEY=VALUE) tanpa menimpa variabel yang sudah ada."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env(ROOT / ".env")

DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT / "data")).resolve()
ASSETS_DIR = DATA_DIR / "assets"
DB_PATH = DATA_DIR / "gitgit3d.sqlite"
ASSETS_DIR.mkdir(parents=True, exist_ok=True)

BLENDER = os.environ.get("BLENDER_BIN", str(Path.home() / "game-toolchain/bin/blender"))
BLENDER_DIR = ROOT / "engine" / "blender"
WEB_DIR = ROOT / "web"

APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
SESSION_DAYS = int(os.environ.get("SESSION_DAYS", "30"))
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "1") == "1"

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "15"))
# Lepas model TripoSR dari RAM setelah menganggur sekian menit (hemat ~3 GB).
MODEL_IDLE_MINUTES = int(os.environ.get("MODEL_IDLE_MINUTES", "15"))
ANIMATIONS = ["idle", "walk", "run", "wave", "jump"]
VIEWS = ["back", "left", "right"]  # foto tambahan opsional untuk tekstur multi-view
