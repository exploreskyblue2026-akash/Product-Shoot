"""All runtime configuration comes from environment variables (set them in Railway → Variables)."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"          # dataset shipped in the repo
WEB_DIR = BASE_DIR / "web"            # approved UI


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# --- core -------------------------------------------------------------
SECRET_KEY = env("SECRET_KEY", "dev-only-change-me")            # sessions + key encryption
DATABASE_URL = env("DATABASE_URL", f"sqlite:///{BASE_DIR / 'studio.db'}")
if DATABASE_URL.startswith("postgres://"):                         # Railway gives postgres://
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

STORAGE_DIR = Path(env("STORAGE_DIR", str(BASE_DIR / "storage")))  # Railway volume mount, e.g. /data
STORAGE_DIR.mkdir(parents=True, exist_ok=True)

OWNER_EMAIL = env("OWNER_EMAIL", "owner@example.com")
OWNER_PASSWORD = env("OWNER_PASSWORD", "change-me")

# --- AI ---------------------------------------------------------------
PROVIDER = env("AI_PROVIDER", "gemini")           # "gemini" or "mock" (offline testing)
GEMINI_API_KEY = env("GEMINI_API_KEY")
MODEL_VISION = env("MODEL_VISION", "gemini-2.5-flash")
MODEL_IMAGE_PRO = env("MODEL_IMAGE_PRO", "gemini-3-pro-image-preview")
MODEL_IMAGE_FAST = env("MODEL_IMAGE_FAST", "gemini-3.1-flash-image-preview")
IMAGE_SIZE = env("IMAGE_SIZE", "2K")              # 1K | 2K | 4K
COST_IMAGE_PRO = float(env("COST_IMAGE_PRO", "0.134"))
COST_IMAGE_FAST = float(env("COST_IMAGE_FAST", "0.067"))
COST_VISION = float(env("COST_VISION", "0.002"))

# --- Google Drive / Sheets (service account JSON pasted as one line) ----
GOOGLE_SERVICE_ACCOUNT_JSON = env("GOOGLE_SERVICE_ACCOUNT_JSON")
OUTPUT_DRIVE_FOLDER_ID = env("OUTPUT_DRIVE_FOLDER_ID")      # optional: copy saved shots to Drive

# --- Shopify ------------------------------------------------------------
SHOPIFY_STORE = env("SHOPIFY_STORE")              # your-store.myshopify.com
SHOPIFY_TOKEN = env("SHOPIFY_ADMIN_TOKEN")
SHOPIFY_API_VERSION = env("SHOPIFY_API_VERSION", "2025-07")

# --- worker -------------------------------------------------------------
RUN_WORKER = env("RUN_WORKER", "true").lower() == "true"   # run autopilot inside the web process
WORKER_INTERVAL = float(env("WORKER_INTERVAL", "3"))
WATCH_INTERVAL_MIN = int(env("WATCH_INTERVAL_MIN", "30"))
