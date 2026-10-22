# Skyblue Product Studio

Raw product photos in, five studio-consistent 4:5 shots out. Inputs come from the left column of the studio (Drive folder, brand catalogue PDF, uploaded files, Google Sheet, Shopify, web link). Autopilot takes each product code through **analyse → 5 prompts → generate → QC (with retries) → approve → save** and returns the approved shots to the left column, the output Drive folder and, optionally, Shopify as a draft.

## What's in the repo

| Path | What it is |
| --- | --- |
| `app/main.py` | FastAPI app: serves the UI and the API (`/api/docs` lists every endpoint) |
| `app/pipeline.py` | Autopilot worker: analysis, generation, QC, retries, approval, saving |
| `app/dataset.py` | Loads the rulebook + library and builds the 6-layer prompt for each shot |
| `app/ingest.py` | Turns each input type into queued products keyed by product code |
| `app/providers/gemini.py` | Gemini vision, image generation and QC calls |
| `app/providers/mock.py` | Offline stand-in used by the tests (`AI_PROVIDER=mock`) |
| `app/connectors/` | Google Drive + Sheets, Shopify, local storage |
| `data/static/` | Studio standard, 5 shot directions, prompt template, vision schema, QC rules (seeded as rules v1) |
| `data/dynamic/` | Brands, categories, moods, props (seeded into the library) |
| `web/` | The approved UI: studio, admin, sign-in |
| `tests/test_e2e.py` | End-to-end run with the 4 sample products |

The dataset files are only read on the **first** start. After that the rulebook and library live in the database and are edited from **Admin**.

## How product codes are found

| Input | Code comes from |
| --- | --- |
| Drive folder / uploaded files / ZIP | File name before the first `_` — `WAT-HEM-SSGT_front.jpg` → `WAT-HEM-SSGT`. Several angles of one code are grouped; `front` or `main` is used as the reference. |
| Brand catalogue PDF | First code on each page (pattern `ABC-123…`), else `<file>-P<page>`; the page's largest image is the reference |
| Google Sheet | `sku` column. Optional columns: `name`, `brand_id`, `category_id`, `colourway`, `contents`, `material`, `key_features`, `image_url` |
| Shopify | Variant SKU and the product's featured image |
| Web link | Best effort: the page's `og:image` and the first code in the page |

Brand is detected from the code prefix (`code_prefixes` in the brands table, e.g. `WAT` → Waterman), then from keywords; the vision step fills anything still missing.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in at least SECRET_KEY, OWNER_EMAIL, OWNER_PASSWORD
export $(grep -v '^#' .env | xargs)
export COOKIE_SECURE=false STORAGE_DIR=./storage
uvicorn app.main:app --reload
```

Open http://localhost:8000 and sign in with the owner email and password. Set `AI_PROVIDER=mock` to try the full flow without spending API credits.

Tests: `pip install pytest && python -m pytest -q`

## Deploy on Railway

1. **Push this folder to a GitHub repo.**
2. In Railway: **New project → Deploy from GitHub repo** and pick it. Railway reads `railway.json` and builds the `Dockerfile`.
3. **Add Postgres:** in the project, **New → Database → PostgreSQL**. On the app service add the variable `DATABASE_URL = ${{Postgres.DATABASE_URL}}`.
4. **Add a volume** to the app service mounted at `/data` and set `STORAGE_DIR=/data` (raw photos and shots live here).
5. **Variables** on the app service (see `.env.example`):
   - `SECRET_KEY` — a long random string; keep it stable (it also encrypts keys saved from Admin)
   - `OWNER_EMAIL`, `OWNER_PASSWORD` — the first owner login
   - `GEMINI_API_KEY`
   - `MODEL_VISION` (default `gemini-3.8-flash`), `MODEL_IMAGE_PRO` (default `gemini-3-pro-image`), `MODEL_IMAGE_FAST` (default `gemini-nano-banana-2.1`) — **Admin → Test Gemini** shows which models your key can use
   - `GOOGLE_SERVICE_ACCOUNT_JSON`, `OUTPUT_DRIVE_FOLDER_ID` — for Drive and Sheets
   - `SHOPIFY_STORE`, `SHOPIFY_ADMIN_TOKEN` — only when publishing to Shopify
6. **Settings → Networking → Generate domain.** Open it, sign in, go to **Admin → API & models** and press **Test Gemini**, **Test Google Drive** and **Test Shopify**.
7. Keep **one replica**: the autopilot worker runs inside the web process (`RUN_WORKER=true`).

## Google Drive and Sheets access

1. In Google Cloud (the same project as the Gemini key): **IAM → Service accounts → Create**. Enable the **Google Drive API** and **Google Sheets API**.
2. Create a JSON key for it and paste the whole JSON, as one line, into `GOOGLE_SERVICE_ACCOUNT_JSON`.
3. **Share** every input folder, product sheet and the output folder with the service account's email (Editor on the output folder).

## Shopify

Create a custom app in the store admin (**Settings → Apps → Develop apps**) with `read_products`, `write_products` and `write_files`. Install it and copy the Admin API access token into `SHOPIFY_ADMIN_TOKEN`. New products are always created as **draft**; existing products are matched by SKU and get the approved shots added.

## Roles

| Role | Can |
| --- | --- |
| Owner | Everything, including API keys |
| Admin | Rulebook, library, repositories, autopilot rules, team, usage |
| Editor | Add inputs, run autopilot, approve, regenerate, reject |
| Viewer | Read only |

Invite people from **Admin → Team**; the page gives you a sign-up link to send them. A rule change sent for approval needs a second admin when more than one exists.

## Costs

Each image and QC call is logged with an estimated cost (`COST_IMAGE_PRO`, `COST_IMAGE_FAST`, `COST_VISION` variables). Autopilot pauses at the monthly budget set in **Admin → Autopilot rules**. Check the real prices on Google's pricing page and update those variables.

## Known limits

- The image models are told to keep the product exact, but they can still alter labels; the QC step catches most of this and sends the product to review.
- Catalogue and web-link inputs are best effort. Drive folders with `CODE_angle.jpg` names give the most reliable results.
- Generation runs one product at a time.
