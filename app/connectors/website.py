"""Official brand websites as a product source.

Many brand sites (e.g. milan.es) have no search box, so the tool builds an index once:
  sitemap(s) -> product pages -> the reference code and EAN printed on each page.
Then a product from the master sheet is found by EAN first, then by its exact reference. A page is only accepted when
its text really contains that EAN or reference. Images are taken from that page only (gallery images whose file name
contains the reference, plus the page's declared product images), never from search engines or other sites.

Per-brand settings (Brands page): website, web_locale (e.g. /en/), web_search (optional, e.g. https://site/search?q={code}).
"""
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import httpx

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0 Safari/537.36 SkyblueStudio/1.0", "Accept-Language": "en"}
IMG_RE = re.compile(r"""(?:src|data-src|data-zoom-image|data-large_image|href|content)\s*=\s*["']([^"']+\.(?:jpe?g|png|webp)(?:\?[^"']*)?)["']""", re.I)
SRCSET_RE = re.compile(r"""srcset\s*=\s*["']([^"']+)["']""", re.I)
REF_RE = re.compile(r"""(?:\bRef(?:erence)?|\bArt(?:icle)?\.?\s*(?:No\.?|Nr\.?|number)?|\bSKU|\bItem\s*(?:no|code)|\bModel|Artikelnummer|Art\.-Nr\.?)\s*[:#.]?\s*([A-Z0-9][A-Z0-9/\-\.]{2,24})""", re.I)
EAN_RE = re.compile(r"""(?:EAN|GTIN|barcode|gtin13)["'\s:=]*?(\d{12,14})""", re.I)
DIM_RE = re.compile(r"""(\d+(?:[.,]\d+)?\s*(?:x|×)\s*\d+(?:[.,]\d+)?(?:\s*(?:x|×)\s*\d+(?:[.,]\d+)?)?\s*(?:mm|cm|m)\b)""", re.I)


def norm(x) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(x or "").upper())


def _get(url: str, client: httpx.Client) -> str:
    r = client.get(url, headers=UA, timeout=30, follow_redirects=True)
    r.raise_for_status()
    return r.text


def _text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    return re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", html))


def _jsonld(html: str) -> list[dict]:
    out = []
    for m in re.finditer(r"""(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>""", html):
        try:
            d = json.loads(m.group(1).strip())
        except ValueError:
            continue
        for x in (d if isinstance(d, list) else d.get("@graph", [d]) if isinstance(d, dict) else []):
            if isinstance(x, dict) and "Product" in str(x.get("@type", "")):
                out.append(x)
    return out


def page_facts(html: str) -> dict:
    """Reference codes, EANs, title, description and dimensions printed on a product page."""
    text = _text(html)
    refs = {norm(m.group(1)) for m in REF_RE.finditer(text) if len(norm(m.group(1))) >= 4}
    eans = set(EAN_RE.findall(html)) | set(EAN_RE.findall(text))
    for p in _jsonld(html):
        for k in ("sku", "mpn", "productID"):
            if p.get(k):
                refs.add(norm(p[k]))
        for k in ("gtin13", "gtin", "gtin12", "gtin14", "ean"):
            if p.get(k):
                eans.add(re.sub(r"\D", "", str(p[k])))
    title = (re.search(r"""(?is)<meta[^>]+property=["']og:title["'][^>]+content=["']([^"']+)""", html) or
             re.search(r"(?is)<title>(.*?)</title>", html))
    desc = re.search(r"""(?is)<meta[^>]+name=["']description["'][^>]+content=["']([^"']+)""", html)
    return {"refs": sorted(r for r in refs if r), "eans": sorted(e.lstrip("0") for e in eans if e),
            "title": (title.group(1).strip() if title else "")[:200], "description": (desc.group(1).strip() if desc else "")[:600],
            "dimensions": list(dict.fromkeys(m.group(1) for m in DIM_RE.finditer(text)))[:4]}


def page_images(html: str, url: str, codes: list[str]) -> list[str]:
    """Product images on this page: files whose name contains the product's reference, else the declared ones."""
    found = []
    for m in IMG_RE.finditer(html):
        found.append(m.group(1))
    for m in SRCSET_RE.finditer(html):
        found += [part.strip().split(" ")[0] for part in m.group(1).split(",") if part.strip()]
    declared = []
    og = re.search(r"""(?is)<meta[^>]+property=["']og:image["'][^>]+content=["']([^"']+)""", html)
    if og:
        declared.append(og.group(1))
    for p in _jsonld(html):
        im = p.get("image")
        declared += im if isinstance(im, list) else [im] if isinstance(im, str) else []
    absu = lambda u: urljoin(url, u.replace("&amp;", "&"))  # noqa: E731
    low = [c.lower() for c in codes if c]
    mine = [absu(u) for u in found if any(c in re.sub(r"[^a-z0-9]", "", urlparse(u).path.lower()) for c in low)]
    pool = mine or [absu(u) for u in declared]
    best = {}
    for u in pool:                                   # one per picture: prefer the large variant of the same file
        stem = re.sub(r"(_(?:l|m|s|xl|adl|thumb|small|medium|large)|-\d+x\d+)(?=\.\w+(?:\?|$))", "", urlparse(u).path.lower())
        name = stem.rsplit("/", 1)[-1]
        flat = re.sub(r"[^a-z0-9]", "", name)
        hit = next((c for c in low if c in flat), None)
        if hit:                                      # same picture = same text from the reference onwards
            stem = flat[flat.index(hit):]
        score = (2 if re.search(r"_(l|xl|large)\.\w+", u.lower()) else 1 if re.search(r"_adl\.\w+", u.lower()) else 0)
        if stem not in best or score > best[stem][0]:
            best[stem] = (score, u)
    return [u for _, u in best.values()][:10]


# ---------------------------------------------------------------- index (sitemap crawl)
def sitemap_urls(site: str, client: httpx.Client, locale: str = "", cap: int = 6000) -> list[str]:
    host = urlparse(site).netloc
    roots = []
    try:
        roots = [l.split(":", 1)[1].strip() for l in _get(urljoin(site, "/robots.txt"), client).splitlines()
                 if l.lower().startswith("sitemap:")]
    except Exception:
        pass
    roots = roots or [urljoin(site, "/sitemap.xml")]
    seen, urls, todo = set(), [], list(roots)
    while todo and len(urls) < cap:
        sm = todo.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        try:
            xml = _get(sm, client)
        except Exception:
            continue
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)
        if "<sitemapindex" in xml:
            todo += [l for l in locs if not locale or locale.strip("/") in l or "product" in l.lower()][:60]
        else:
            urls += [l for l in locs if urlparse(l).netloc.endswith(host.replace("www.", "")) and (not locale or locale in l)
                     and not re.search(r"\.(jpe?g|png|webp|pdf)$", l, re.I)]
    return list(dict.fromkeys(urls))[:cap]


def crawl(site: str, index: dict, locale: str = "", budget: int = 250, delay: float = 0.4, stop=None) -> dict:
    """Visit up to `budget` not-yet-indexed pages; record the refs/EANs printed on each. Resumable."""
    index = dict(index or {})
    pages = dict(index.get("pages") or {})
    with httpx.Client() as client:
        if not index.get("urls"):
            index["urls"] = sitemap_urls(site, client, locale)
        n = 0
        for u in index["urls"]:
            if u in pages:
                continue
            if n >= budget or (stop and stop()):
                break
            n += 1
            try:
                f = page_facts(_get(u, client))
                if f["refs"] or f["eans"]:
                    pages[u] = {"r": f["refs"][:6], "e": f["eans"][:4]}
                else:
                    pages[u] = {}
            except Exception:
                pages[u] = {"x": 1}
            time.sleep(delay)
    index["pages"] = pages
    index["at"] = datetime.now(timezone.utc).isoformat(timespec="minutes")
    index["done"] = sum(1 for u in index["urls"] if u in pages)
    return index


def lookup(index: dict, ean: str, codes: list[str]) -> str | None:
    pages = (index or {}).get("pages") or {}
    e = re.sub(r"\D", "", str(ean or "")).lstrip("0")
    if e:
        for u, f in pages.items():
            if e in (f.get("e") or []):
                return u
    want = {norm(c) for c in codes if len(norm(c)) >= 4}
    for u, f in pages.items():
        if want & set(f.get("r") or []):
            return u
    return None


def fetch_product(url: str, ean: str, codes: list[str]) -> dict:
    """Open one product page, verify it is this product, return its facts and image URLs (nothing downloaded yet)."""
    with httpx.Client() as client:
        html = _get(url, client)
    f = page_facts(html)
    text = norm(_text(html))
    e = re.sub(r"\D", "", str(ean or "")).lstrip("0")
    ok_ean = bool(e) and (e in f["eans"] or e in text)
    ok_ref = any(norm(c) in f["refs"] or (len(norm(c)) >= 6 and norm(c) in text) for c in codes if c)
    if not (ok_ean or ok_ref):
        raise ValueError("The page does not show this product's EAN or reference, so it was not used")
    return {**f, "url": url, "verified_by": "EAN" if ok_ean else "reference",
            "images": page_images(html, url, [norm(c) for c in codes])}


def search_candidates(template: str, code: str) -> list[str]:
    url = template.replace("{code}", code)
    host = urlparse(url).netloc
    with httpx.Client() as client:
        html = _get(url, client)
    links = [urljoin(url, h) for h in re.findall(r"""href=["']([^"'#]+)["']""", html)]
    return list(dict.fromkeys(l for l in links if urlparse(l).netloc == host and l != url))[:40]


def download(url: str) -> bytes:
    with httpx.Client() as client:
        r = client.get(url, headers=UA, timeout=60, follow_redirects=True)
        r.raise_for_status()
        return r.content
