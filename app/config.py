"""All runtime configuration comes from environment variables (set them in Railway → Variables)."""
import os
from pathlib import Path

APP_VERSION = "3.3.0 (accuracy engine: edit real photo + restore, detail crop, Product Kit)"
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
MODEL_VISION = env("MODEL_VISION", "gemini-3.8-flash")
MODEL_IMAGE_PRO = env("MODEL_IMAGE_PRO", "gemini-3-pro-image")
MODEL_IMAGE_FAST = env("MODEL_IMAGE_FAST", "gemini-nano-banana-2.1")
IMAGE_SIZE = env("IMAGE_SIZE", "2K")              # 1K | 2K | 4K
COST_IMAGE_PRO = float(env("COST_IMAGE_PRO", "0.134"))
COST_IMAGE_FAST = float(env("COST_IMAGE_FAST", "0.05"))
COST_VISION = float(env("COST_VISION", "0.002"))
MODEL_IMAGE_LITE = env("MODEL_IMAGE_LITE", "gemini-3.1-flash-lite-image")
COST_IMAGE_LITE = float(env("COST_IMAGE_LITE", "0.02"))
# Estimated USD per image by model + size. Check Google's pricing page and override with COST_* variables.
SIZE_FACTOR = {"1K": 1.0, "2K": 1.5, "4K": 2.5}
COST_MODES = {
    # mode: (model for hero/detail, model for other shots, image size)
    "economy":  ("lite", "lite", "1K"),
    "standard": ("fast", "fast", "1K"),
    "premium":  ("pro", "fast", "2K"),
}


def mode_model(kind: str) -> str:
    return {"lite": MODEL_IMAGE_LITE, "fast": MODEL_IMAGE_FAST, "pro": MODEL_IMAGE_PRO}[kind]


def image_cost(model: str, size: str = "1K") -> float:
    m = model_name(model)
    base = COST_IMAGE_PRO if "pro" in m else COST_IMAGE_LITE if "lite" in m else COST_IMAGE_FAST
    return round(base * SIZE_FACTOR.get(size, 1.0), 4)

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

# Old model names that Google retired or restricted -> current replacements (applied automatically)
MODEL_ALIASES = {
    "gemini-2.5-flash": "gemini-3.8-flash", "gemini-2.0-flash": "gemini-3.8-flash",
    "gemini-3-pro-image-preview": "gemini-3-pro-image",
    "gemini-3.1-flash-image-preview": "gemini-nano-banana-2.1", "gemini-3.1-flash-image": "gemini-nano-banana-2.1",
    "gemini-2.5-flash-image": "gemini-nano-banana-2.1",
}


def model_name(name: str) -> str:
    n = (name or "").strip().removeprefix("models/")
    if n in MODEL_ALIASES:
        return MODEL_ALIASES[n]
    if n.startswith(("gemini-2.", "gemini-1.")):          # any retired 1.x / 2.x name, incl. -preview / -001 variants
        return "gemini-nano-banana-2.1" if "image" in n else "gemini-3.8-flash"
    return n
