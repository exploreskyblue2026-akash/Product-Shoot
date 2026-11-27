"""All runtime configuration comes from environment variables (set them in Railway → Variables)."""
import os
from pathlib import Path

APP_VERSION = ("5.8.6 (Brand page -> Shot cards: the Angle dropdown had no real 'let the analysis decide' option - "
               "'Same angle as the main real photo' was the default, and filling in ANY other field on a shot card "
               "(scene, background, props, extra instructions) silently pulled that 'same angle' choice along with "
               "it as a forced instruction, even for a brand page nobody had touched the Angle dropdown on. That "
               "overrode the per-product analysis's own camera-angle decision (worked out from the product's real "
               "physical form - how it naturally rests, stands or leans) with whatever angle the reference photo "
               "happened to be shot at. Added a real 'Let the per-product analysis decide (recommended)' option, "
               "now the true default - 'Same angle', 'Top-down flat lay' etc. are explicit overrides you choose "
               "only to force every product of that brand into one fixed angle regardless of shape. Existing brand "
               "pages with other shot-card fields already filled in (e.g. MILAN) are still sending the old forced "
               "'same angle' instruction until their Angle dropdown is switched to the new option and re-saved.\n"
               "5.8.5 (New Cost mode: 'GPT · Image 2.6 Sunburst/Flare, xhigh' - uses OpenAI's GPT Image 2.6 via "
               "OpenRouter instead of Gemini. As of the Artificial Analysis text-to-image arena (Nov 2026), GPT "
               "Image 2.6 Sunburst is the #1 ranked image model overall, well above our Gemini Pro tier - and at "
               "the 'xhigh' quality tier it costs about $0.094/image, noticeably LESS than Gemini Pro's "
               "~$0.134-0.20/image. Sunburst (tuned for prompt adherence/edit consistency) handles hero + detail; "
               "Flare (same price, faster/lower-latency) handles lifestyle + use-case. Needs the OpenRouter switch "
               "on in Admin - picking this Cost mode without it now falls back to Gemini Pro automatically instead "
               "of failing with a missing-key error partway through a run.\n"
               "5.8.4 (Fixed research/analysis failing with OpenRouter 402 'This request requires more credits, or "
               "fewer max_tokens' for Claude models even with a healthy balance: the request never set max_tokens "
               "at all, so OpenRouter pre-checked the balance against the model's entire max output window "
               "(65,536 tokens for Claude Sonnet 5.5) rather than what an analysis actually needs - a few hundred "
               "to a few thousand tokens. Rather than lowering the ceiling and risking a cut-off analysis, the "
               "cap is now a generous 32,000 for analysis/shot-plan calls (JSON) and 4,000 for short text replies "
               "(titles, descriptions) - both far above what a real response ever reaches, so nothing about the "
               "analysis itself is shortened, only the worst case OpenRouter has to check your balance against. "
               "If a response is ever genuinely cut off at that cap, it now fails loudly with a clear error "
               "instead of silently handing back broken JSON.\n"
               "5.8.3 (Two real root causes fixed for codes getting stuck with 'Another library job is still "
               "running' and the queue never visibly moving: (1) adding codes or pressing 'Run the agent' while "
               "any Library job was active used to be flatly refused - including the completely ordinary case of "
               "pasting one more code while a big batch was still being researched, which is exactly what the "
               "screenshot showed: codes sitting in the paste box, Input queue at 0, nothing happening. Requests "
               "are now queued instead of rejected - they run the moment the current job finishes, so nothing is "
               "ever silently dropped or bounced. (2) a single hung website fetch or AI call inside the per-code "
               "research step had no timeout at all, so it could block the entire queue - and the 'job running' "
               "flag that gates new submissions - indefinitely; credits kept burning on a call that would never "
               "come back. Each code's research now runs under a hard 2-4 minute watchdog: if it doesn't finish in "
               "time it's marked as timed-out and the queue moves on to the next code immediately instead of "
               "stalling for hours.\n"
               "5.8.2 (Library page layout: the Input queue and Researched table now sit side by side in two tall, "
               "independently-scrolling columns instead of being stacked full-width one under the other - so a "
               "batch of several dozen pasted codes is a scrollable list you can skim at a glance on the left, with "
               "the research results building up on the right as they come in, instead of a few rows showing before "
               "you have to scroll the whole page. 'Run the agent' now sits above both columns since it drives "
               "both.\n"
               "5.8.1: Found and fixed the real reason the Library queue looked stuck at the same count for hours "
               "while still burning credits: the continuous background worker was calling its own single-code-at-a-"
               "time step every few seconds REGARDLESS of whether 'Run the agent' already had a research job running "
               "in the background - so two independent processes were researching the first code in the queue at "
               "the same time, over and over, neither aware of the other. That is the 'several products researched "
               "at once' problem again, just reintroduced through a second door instead of the one closed last time. "
               "The background worker now stands down completely whenever a Library job (Run the agent, a Drive "
               "scan, a sheet sync) already owns the queue, so there is only ever one thing touching it. Library "
               "page layout: the 'Run the agent' button now only researches the Input queue - it no longer also "
               "auto-sends to Studio in the same click; a separate 'Push ready codes to Studio' button sits right "
               "on the Researched table so sending them is its own deliberate step, with the automatic-send "
               "checkbox still there for anyone who wants that done without clicking it.\n"
               "5.8.0: Library page rebuilt around one button: paste codes and press 'Run the agent' - it adds "
               "them and researches every one start to finish (find it, pull its photos, analyse it, write the shot "
               "plan, including telling each shot directly when an input photo can be used as-is or with a small "
               "edit instead of a fresh generation). The three separate buttons this used to take are gone. Codes "
               "you paste now show up at once in their own 'Input queue' list and disappear from it one by one, "
               "live, the moment the agent starts on each one - so a batch of 20 visibly counts down instead of "
               "just sitting there until the whole thing is done. Sources, presets (research/send automatically, "
               "check the store first, use brand websites) and the photo-filing stats are no longer tucked into "
               "collapsed sections - everything is visible on the page at once. A 'Clear the whole queue' button "
               "empties the list completely so it can be restarted from nothing, without touching the photos and "
               "research already filed in the Library. Generation: 'Max cost per product' is now a floor, not just "
               "a ceiling - it used to default to $0.30 regardless of Cost mode, so Premium/Max (Pro images at 2K) "
               "could hit that cap after the hero and one more shot and silently never make the rest; the agent "
               "now always raises its own working cap enough to finish the shots the Cost mode and shot count you "
               "picked actually call for, and the Admin field only matters if you want a lower cap than that. Also "
               "fixed: the 'Shots generated automatically' dropdown in Admin had its shot names wrong (labelled "
               "lifestyle as 'detail' and vice versa), and 'Hero waits for approval' - which decides whether shots "
               "2-4 start before a person approves the hero - had no setting in Admin to see or change at all.")
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
COST_PLANNER = float(env("COST_PLANNER", "0.03"))      # analysis + 4 shot plans, best Pro model, once per product
MODEL_IMAGE_LITE = env("MODEL_IMAGE_LITE", "gemini-3.1-flash-lite-image")
COST_IMAGE_LITE = float(env("COST_IMAGE_LITE", "0.02"))
# GPT Image 2.6 (via OpenRouter): ranked the #1 text-to-image model on the Artificial Analysis arena as of Nov 2026,
# and at "high"/"xhigh" quality it costs LESS per image than our own Gemini Pro tier above - Sunburst is the variant
# tuned for prompt adherence/edit consistency (used for hero+detail, where matching the real product matters most),
# Flare is the faster/cheaper-latency sibling at the SAME per-image price (used for the two context shots).
MODEL_IMAGE_GPT_SUNBURST = env("MODEL_IMAGE_GPT_SUNBURST", "openrouter:openai/gpt-image-2.6-sunburst")
MODEL_IMAGE_GPT_FLARE = env("MODEL_IMAGE_GPT_FLARE", "openrouter:openai/gpt-image-2.6-flare")
# Estimated USD per image by model + size. Check Google's pricing page and override with COST_* variables.
SIZE_FACTOR = {"1K": 1.0, "2K": 1.5, "4K": 2.5}
COST_MODES = {
    # mode: (model for hero/detail, model for other shots, image size)
    "economy":  ("lite", "lite", "1K"),
    "standard": ("fast", "fast", "1K"),
    "premium":  ("pro", "fast", "2K"),
    "max":      ("pro", "pro", "2K"),
    # GPT Image 2.6 mode: "size" here drives the quality TIER sent to OpenRouter, not pixel resolution (OpenAI's
    # Images API has no resolution knob, it uses quality tiers instead - see QUIRKS in providers/openrouter.py) -
    # 1K->high ($0.053/image), 2K->xhigh ($0.094/image), 4K->max ($0.211/image). 2K/xhigh is the recommended
    # default: noticeably more detail than "high" without jumping to Max's price, and still cheaper than our own
    # Gemini Pro tier.
    "gpt":      ("gpt_sunburst", "gpt_flare", "2K"),
}


def mode_model(kind: str) -> str:
    return {"lite": MODEL_IMAGE_LITE, "fast": MODEL_IMAGE_FAST, "pro": MODEL_IMAGE_PRO,
             "gpt_sunburst": MODEL_IMAGE_GPT_SUNBURST, "gpt_flare": MODEL_IMAGE_GPT_FLARE}[kind]


def image_cost(model: str, size: str = "1K") -> float:
    """Pre-flight estimate only, used for the budget check before a call is made. The real per-call cost (what
    actually gets charged to the product) comes from OpenRouter's own usage figure once the image is back —
    see GeminiProvider.last_cost / pipeline.py — so this guess only needs to be in the right ballpark."""
    if str(model).startswith("openrouter:"):
        m = model.lower()
        if m.endswith(":free"):
            return 0.0
        if "gpt-image-2.5" in m or "gpt-image-2.6" in m:
            # Real per-image cost at 1024x1024 for Sunburst/Flare (both priced identically): size here is our own
            # size->quality mapping (see COST_MODES["gpt"] and QUIRKS in providers/openrouter.py), not pixel size.
            return {"1K": 0.053, "2K": 0.094, "4K": 0.211}.get(size, 0.053)
        if "gpt-image" in m:
            return {"1K": 0.04, "2K": 0.07, "4K": 0.17}.get(size, 0.04)
        return 0.075 if "max" in m else 0.02 if "klein" in m else 0.05
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


# ---- idle detection: when nobody uses the tool, background work stops touching the database, so the
# database (Neon) can go to sleep and does not burn its monthly compute quota.
import time as _time
LAST_ACTIVITY = {"t": _time.time()}
IDLE_AFTER = float(env("IDLE_AFTER", "900"))          # 15 minutes without a signed-in request = idle


def touch():
    LAST_ACTIVITY["t"] = _time.time()


def idle() -> bool:
    return _time.time() - LAST_ACTIVITY["t"] > IDLE_AFTER
