"""Turn an input source into queued products, each keyed by its product code."""
import io
import re
import zipfile
from datetime import timezone
from sqlalchemy import select
from sqlalchemy.orm import Session

from .connectors import storage
from .dataset import brand_from_text
from .db import Product, Source, now, log

IMG_EXT = (".jpg", ".jpeg", ".png", ".webp")
ANGLE_ORDER = ["front", "main", "hero", "1", "01", "a"]
CODE_RE = re.compile(r"\b[A-Z]{2,6}[-_][A-Z0-9]{2,}(?:-[A-Z0-9]{2,})*\b")


def code_from_filename(name: str) -> tuple[str, str]:
    stem = re.sub(r"(\.(jpe?g|png|webp|heic|tiff?))+$", "", name.split("/")[-1], flags=re.I)
    parts = stem.split("_")
    code = parts[0].strip().upper()
    angle = parts[1].lower() if len(parts) > 1 else ""
    return code, angle


def _existing(s: Session) -> dict[str, Product]:
    return {p.code: p for p in s.scalars(select(Product).where(Product.stage != "rejected"))}


def _add_product(s: Session, src: Source, code: str, raw: bytes | None, name="", data=None, hint_text="") -> Product | None:
    data = data or {}
    have = _existing(s)
    p = have.get(code)
    brand = src.config.get("brand") or data.get("brand_id") or brand_from_text(s, " ".join([code, name, hint_text]))
    if p:  # enrich existing; attach a photo if it was waiting for one
        p.data = {**(p.data or {}), **{k: v for k, v in data.items() if v}}
        p.name = p.name or name
        if raw and not p.raw_path:
            p.raw_path = storage.save(f"raw/{storage.safe(code)}.jpg", storage.normalise_raw(raw))
            if p.stage == "needs_photo":
                p.stage = "queue"
        return None
    p = Product(code=code, name=name or code, brand_id=brand if brand != "_default" else (data.get("brand_id") or ""),
                category_id=data.get("category_id", ""), colourway=data.get("colourway") or "standard",
                data=data, source_id=src.id, stage="queue" if raw else "needs_photo")
    if raw:
        p.raw_path = storage.save(f"raw/{storage.safe(code)}.jpg", storage.normalise_raw(raw))
    s.add(p)
    return p


def _pick_best(files: list[dict]) -> dict[str, dict]:
    """Group image files by product code; prefer the front/main angle."""
    groups: dict[str, list] = {}
    for f in files:
        if not f["name"].lower().endswith(IMG_EXT):
            continue
        code, angle = code_from_filename(f["name"])
        groups.setdefault(code, []).append((angle, f))
    best = {}
    for code, lst in groups.items():
        lst.sort(key=lambda a: ANGLE_ORDER.index(a[0]) if a[0] in ANGLE_ORDER else 99)
        best[code] = {**lst[0][1], "angles": [a for a, _ in lst]}
    return best


# ------------------------------------------------------------------ per type
def ingest_files(s: Session, src: Source, files: list[tuple[str, bytes]]) -> int:
    n = 0
    flat: list[tuple[str, bytes]] = []
    for name, data in files:
        low = name.lower()
        if low.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                flat += [(i.filename, z.read(i)) for i in z.infolist() if i.filename.lower().endswith(IMG_EXT + (".pdf",))]
        else:
            flat.append((name, data))
    for name, data in flat:
        if name.lower().endswith(".pdf"):
            n += ingest_catalogue_bytes(s, src, data, name)
    best = _pick_best([{"name": n_, "data": d} for n_, d in flat])
    for code, f in best.items():
        if _add_product(s, src, code, f["data"], hint_text=f["name"]):
            n += 1
    return n


def ingest_drive(s: Session, src: Source) -> int:
    from .connectors import google
    files = google.list_images(google.folder_id(src.config["link"]))
    n = 0
    pdfs = [f for f in files if f["mimeType"] == "application/pdf"]
    have = _existing(s)
    best = _pick_best(files)
    for code, f in best.items():
        if code in have and have[code].raw_path:
            continue
        if _add_product(s, src, code, google.download(f["id"]), hint_text=f"{f['folder']} {f['name']}"):
            n += 1
    seen = set(src.config.get("seen_pdfs", []))
    for f in pdfs:
        if f["id"] not in seen:
            n += ingest_catalogue_bytes(s, src, google.download(f["id"]), f["name"])
            seen.add(f["id"])
    src.config = {**src.config, "seen_pdfs": sorted(seen)}
    return n


def ingest_sheet(s: Session, src: Source) -> int:
    """Rows need a sku column. Optional: name, brand_id, category_id, colourway, contents, material, key_features, image_url."""
    from .connectors import google, shopify
    n = 0
    for row in google.read_sheet(src.config["link"]):
        code = (row.get("sku") or row.get("product_code") or row.get("code") or "").strip().upper()
        if not code:
            continue
        raw = None
        url = row.get("image_url") or row.get("image") or ""
        if url:
            try:
                raw = google.download(google.folder_id(url)) if "drive.google" in url else shopify.download(url)
            except Exception:
                raw = None
        data = {k: v for k, v in row.items() if k not in ("sku", "image_url", "image")}
        if _add_product(s, src, code, raw, name=row.get("product_name") or row.get("name", ""), data=data):
            n += 1
    return n


def ingest_catalogue_bytes(s: Session, src: Source, pdf: bytes, filename: str) -> int:
    """One product per catalogue page: the page's largest image + the first product code found in its text."""
    import pymupdf
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    pattern = re.compile(src.config.get("code_pattern") or CODE_RE.pattern)
    stem = storage.safe(filename.rsplit(".", 1)[0])[:20].upper()
    n = 0
    for i, page in enumerate(doc):
        imgs = []
        for info in page.get_images(full=True):
            try:
                pix = pymupdf.Pixmap(doc, info[0])
                if pix.width >= 300 and pix.height >= 300:
                    if pix.n - pix.alpha >= 4:
                        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
                    imgs.append((pix.width * pix.height, pix.tobytes("png")))
            except Exception:
                continue
        if not imgs:
            continue
        text = page.get_text()
        m = pattern.search(text.upper())
        code = (m.group(0).replace(" ", "-").replace("_", "-") if m else f"{stem}-P{i + 1}")
        name = next((ln.strip() for ln in text.splitlines() if len(ln.strip()) > 6), "")[:120]
        if _add_product(s, src, code, max(imgs)[1], name=name, hint_text=text[:400]):
            n += 1
    return n


def ingest_catalogue_link(s: Session, src: Source) -> int:
    from .connectors import google, shopify
    link = src.config["link"]
    data = google.download(google.folder_id(link)) if "drive.google" in link else shopify.download(link)
    return ingest_catalogue_bytes(s, src, data, link.rsplit("/", 1)[-1] or "catalogue")


def ingest_shopify(s: Session, src: Source) -> int:
    from .connectors import shopify
    n = 0
    for p in shopify.list_products(250):
        code = (p["sku"] or p["id"].rsplit("/", 1)[-1]).upper()
        raw = shopify.download(p["image_url"]) if p["image_url"] else None
        prod = _add_product(s, src, code, raw, name=p["title"], data={"vendor": p["vendor"], "type": p["type"]},
                            hint_text=p["vendor"])
        if prod:
            prod.shopify_id = p["id"]
            n += 1
    return n


def ingest_url(s: Session, src: Source) -> int:
    """Best effort: the page's og:image, product code from the page text or URL slug."""
    from .connectors import shopify
    link = src.config["link"]
    html = shopify.download(link).decode("utf-8", "ignore")
    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', html, re.I)
    if not m:
        raise RuntimeError("No product image found on that page (no og:image). Download the photos and upload them instead.")
    title = re.search(r"<title>([^<]+)", html, re.I)
    code_m = CODE_RE.search(html.upper())
    code = code_m.group(0) if code_m else storage.safe(link.rstrip("/").rsplit("/", 1)[-1]).upper()[:40]
    return 1 if _add_product(s, src, code, shopify.download(m.group(1)), name=(title.group(1).strip() if title else ""),
                             hint_text=html[:2000]) else 0


RUNNERS = {"drive": ingest_drive, "sheet": ingest_sheet, "catalogue": ingest_catalogue_link,
           "shopify": ingest_shopify, "url": ingest_url}


def scan(s: Session, src: Source) -> int:
    """Run (or re-run, for watched sources) a source. Uploaded files are ingested at upload time."""
    if src.type == "files":
        return 0
    try:
        n = RUNNERS[src.type](s, src)
        src.status, src.error = "ok", ""
    except Exception as e:  # keep the error visible in the UI instead of crashing the worker
        n, src.status, src.error = 0, "error", str(e)[:500]
    src.last_scan = now()
    if n:
        log(s, "system", f"{src.name}: {n} new product codes queued")
    s.commit()
    return n
