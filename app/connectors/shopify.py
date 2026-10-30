"""Shopify Admin GraphQL: attach approved shots to the matching store product (by barcode, then SKU); create a DRAFT
product from the master sheet only when nothing matches."""
import httpx

from ..config import SHOPIFY_STORE, SHOPIFY_API_VERSION
from ..secrets import get_secret


def _store() -> str:
    return (get_secret("SHOPIFY_STORE") or SHOPIFY_STORE or "").replace("https://", "").strip("/")


_tok: dict = {}


def _token(store: str) -> str:
    """A permanent Admin API token (older custom apps) or, for Dev Dashboard apps (2026+), a 24-hour token from the
    client credentials grant, renewed automatically."""
    static = get_secret("SHOPIFY_ADMIN_TOKEN")
    if static:
        return static
    cid, secret = get_secret("SHOPIFY_CLIENT_ID"), get_secret("SHOPIFY_CLIENT_SECRET")
    if not (cid and secret):
        return ""
    import time
    if _tok.get("token") and _tok.get("store") == store and _tok.get("exp", 0) > time.time() + 300:
        return _tok["token"]
    r = httpx.post(f"https://{store}/admin/oauth/access_token", timeout=30,
                   data={"grant_type": "client_credentials", "client_id": cid, "client_secret": secret})
    if r.status_code >= 400:
        raise RuntimeError(f"Shopify refused the app credentials ({r.status_code}): {r.text[:200]}. "
                           "Check the app is installed on this store and the scopes are released.")
    j = r.json()
    _tok.update(token=j["access_token"], store=store, exp=time.time() + int(j.get("expires_in", 86399)))
    return _tok["token"]


def _call(query: str, variables: dict | None = None) -> dict:
    store = _store()
    token = _token(store) if store else ""
    if not store or not token:
        raise RuntimeError("Shopify not connected. Add SHOPIFY_STORE (your-store.myshopify.com) plus SHOPIFY_CLIENT_ID "
                           "and SHOPIFY_CLIENT_SECRET (Dev Dashboard app) in Admin → API & models.")
    r = httpx.post(f"https://{store}/admin/api/{SHOPIFY_API_VERSION}/graphql.json",
                   json={"query": query, "variables": variables or {}},
                   headers={"X-Shopify-Access-Token": token}, timeout=60)
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise RuntimeError(str(body["errors"])[:400])
    return body["data"]


def test() -> str:
    d = _call("{ shop { name myshopifyDomain } }")
    return f"Connected to {d['shop']['name']} ({d['shop']['myshopifyDomain']})"


def _find(q: str) -> dict | None:
    d = _call("""query($q:String!){ productVariants(first:2, query:$q){ nodes{ id sku barcode
        product{ id title status media(first:50){ nodes{ alt } } } } } }""", {"q": q})
    nodes = d["productVariants"]["nodes"]
    if not nodes:
        return None
    return {**nodes[0]["product"], "_variant": nodes[0].get("id", "")}


def find_product(barcode: str = "", skus: list[str] | None = None) -> tuple[dict | None, str]:
    """Barcode (EAN) first: it is unique. Then each known SKU spelling."""
    if barcode:
        p = _find(f"barcode:{barcode}")
        if p:
            return p, f"barcode {barcode}"
    for sku in [x for x in (skus or []) if x]:
        p = _find(f'sku:"{sku}"')
        if p:
            return p, f"SKU {sku}"
    return None, ""


def find_by_sku(sku: str) -> str | None:                    # kept for older callers
    p, _ = find_product("", [sku])
    return p["id"] if p else None


def _staged_upload(name: str, data: bytes) -> str:
    d = _call("""mutation($i:[StagedUploadInput!]!){ stagedUploadsCreate(input:$i){
        stagedTargets{ url resourceUrl parameters{ name value } } userErrors{ message } } }""",
              {"i": [{"resource": "IMAGE", "filename": name, "mimeType": "image/jpeg", "httpMethod": "POST",
                      "fileSize": str(len(data))}]})
    t = d["stagedUploadsCreate"]["stagedTargets"][0]
    form = {p["name"]: p["value"] for p in t["parameters"]}
    httpx.post(t["url"], data=form, files={"file": (name, data, "image/jpeg")}, timeout=120).raise_for_status()
    return t["resourceUrl"]


def create_draft(info: dict) -> str:
    d = _call("""mutation($p:ProductCreateInput!){ productCreate(product:$p){ product{ id variants(first:1){ nodes{ id } } }
        userErrors{ message } } }""",
              {"p": {k: v for k, v in {"title": info["title"], "status": "DRAFT", "vendor": info.get("vendor", ""),
                     "productType": info.get("type", ""), "tags": ["skyblue-studio"] + info.get("tags", []),
                     "descriptionHtml": info.get("description") or None, "category": info.get("category_id") or None,
                     "templateSuffix": info.get("template") or None}.items() if v not in (None, "")}})
    errs = d["productCreate"]["userErrors"]
    if errs:
        raise RuntimeError(errs[0]["message"])
    prod = d["productCreate"]["product"]
    variant = {"id": prod["variants"]["nodes"][0]["id"]}
    # price and inventory are never set by the tool
    if info.get("barcode"):
        variant["barcode"] = info["barcode"]
    if info.get("sku"):
        variant["inventoryItem"] = {"sku": info["sku"]}
    _call("""mutation($p:ID!,$v:[ProductVariantsBulkInput!]!){ productVariantsBulkUpdate(productId:$p, variants:$v){
        userErrors{ message } } }""", {"p": prod["id"], "v": [variant]})
    return prod["id"]


def push(info: dict, images: list[tuple[str, bytes, str]]) -> dict:
    """info: title, vendor, type, sku, skus, barcode, price. images: (filename, jpeg, alt) in order (hero first).
    Returns {product_id, how, added, skipped}."""
    prod, how = find_product(info.get("barcode", ""), info.get("skus") or [info.get("sku", "")])
    if prod:
        existing = {m.get("alt") for m in (prod.get("media") or {}).get("nodes", [])}
        pid = prod["id"]
    else:
        pid, how, existing = create_draft(info), "new draft product", set()
    todo = [(n, b, alt) for n, b, alt in images if alt not in existing]        # never upload the same shot twice
    if todo:
        media = [{"originalSource": _staged_upload(n, b), "alt": alt, "mediaContentType": "IMAGE"} for n, b, alt in todo]
        d = _call("""mutation($id:ID!,$m:[CreateMediaInput!]!){ productCreateMedia(productId:$id, media:$m){
            mediaUserErrors{ message } } }""", {"id": pid, "m": media})
        errs = d["productCreateMedia"]["mediaUserErrors"]
        if errs:
            raise RuntimeError(errs[0]["message"])
    return {"product_id": pid, "how": how, "added": len(todo), "skipped": len(images) - len(todo)}


def publish(product, images: list[tuple[str, bytes, str]]) -> str:   # legacy entry point used by approve()
    return push({"title": product.name or product.code, "vendor": product.brand_id, "sku": product.code,
                 "skus": [product.code]}, images)["product_id"]


def download(url: str) -> bytes:
    r = httpx.get(url, timeout=60, follow_redirects=True)
    r.raise_for_status()
    return r.content


# ---------------------------------------------------------------- field-level sync (preview, then apply)
PRODUCT_Q = """query($id:ID!){ product(id:$id){ id title descriptionHtml productType vendor tags templateSuffix status
    category{ id fullName } collections(first:50){ nodes{ id title } }
    variants(first:100){ nodes{ id sku barcode } } media(first:50){ nodes{ alt } } } }"""


def get_product(pid: str) -> dict:
    return _call(PRODUCT_Q, {"id": pid})["product"]


def find_category(name: str) -> dict | None:
    """Shopify's standard product taxonomy: best match for a category name, e.g. 'Pencil Cases'."""
    if not name:
        return None
    d = _call("""query($q:String!){ taxonomy{ categories(first:1, search:$q){ nodes{ id fullName } } } }""", {"q": name})
    n = d["taxonomy"]["categories"]["nodes"]
    return n[0] if n else None


def find_collections(titles: list[str]) -> tuple[list[dict], list[str]]:
    """Existing collections only (never created). Returns (found, not_found)."""
    found, missing = [], []
    for title in titles:
        d = _call("""query($q:String!){ collections(first:5, query:$q){ nodes{ id title } } }""",
                  {"q": f'title:"{title}"'})
        hit = next((c for c in d["collections"]["nodes"] if c["title"].strip().lower() == title.strip().lower()), None)
        (found.append(hit) if hit else missing.append(title))
    return found, missing


def apply(pid: str, variant_id: str, changes: dict, add_collections: list[str]):
    """Write only the planned fields. Never price, never inventory."""
    allowed = {"title", "descriptionHtml", "productType", "vendor", "tags", "category", "templateSuffix"}
    prod = {k: v for k, v in changes.items() if k in allowed}
    if prod:
        d = _call("""mutation($p:ProductUpdateInput!){ productUpdate(product:$p){ userErrors{ field message } } }""",
                  {"p": {"id": pid, **prod}})
        errs = d["productUpdate"]["userErrors"]
        if errs:
            raise RuntimeError(f"{errs[0].get('field')}: {errs[0]['message']}")
    var = {}
    if "barcode" in changes:
        var["barcode"] = changes["barcode"]
    if "sku" in changes:
        var["inventoryItem"] = {"sku": changes["sku"]}
    if var and variant_id:
        d = _call("""mutation($p:ID!,$v:[ProductVariantsBulkInput!]!){ productVariantsBulkUpdate(productId:$p, variants:$v){
            userErrors{ message } } }""", {"p": pid, "v": [{"id": variant_id, **var}]})
        errs = d["productVariantsBulkUpdate"]["userErrors"]
        if errs:
            raise RuntimeError(errs[0]["message"])
    for cid in add_collections:
        d = _call("""mutation($id:ID!,$p:[ID!]!){ collectionAddProducts(id:$id, productIds:$p){ userErrors{ message } } }""",
                  {"id": cid, "p": [pid]})
        errs = d["collectionAddProducts"]["userErrors"]
        if errs and "already" not in errs[0]["message"].lower():
            raise RuntimeError(errs[0]["message"])


def add_media(pid: str, images: list[tuple[str, bytes, str]], existing_alts: set) -> tuple[int, int]:
    todo = [(n, b, alt) for n, b, alt in images if alt not in existing_alts]
    if todo:
        media = [{"originalSource": _staged_upload(n, b), "alt": alt, "mediaContentType": "IMAGE"} for n, b, alt in todo]
        d = _call("""mutation($id:ID!,$m:[CreateMediaInput!]!){ productCreateMedia(productId:$id, media:$m){
            mediaUserErrors{ message } } }""", {"id": pid, "m": media})
        errs = d["productCreateMedia"]["mediaUserErrors"]
        if errs:
            raise RuntimeError(errs[0]["message"])
    return len(todo), len(images) - len(todo)
