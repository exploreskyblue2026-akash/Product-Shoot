"""Shopify Admin GraphQL: attach approved shots to the matching store product (by barcode, then SKU); create a DRAFT
product from the master sheet only when nothing matches."""
import httpx

from ..config import SHOPIFY_STORE, SHOPIFY_API_VERSION
from ..secrets import get_secret


def _store() -> str:
    return (get_secret("SHOPIFY_STORE") or SHOPIFY_STORE or "").replace("https://", "").strip("/")


def _call(query: str, variables: dict | None = None) -> dict:
    store, token = _store(), get_secret("SHOPIFY_ADMIN_TOKEN")
    if not store or not token:
        raise RuntimeError("Shopify not connected. Add SHOPIFY_STORE (your-store.myshopify.com) and SHOPIFY_ADMIN_TOKEN.")
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
    d = _call("""query($q:String!){ productVariants(first:2, query:$q){ nodes{ sku barcode
        product{ id title status media(first:50){ nodes{ alt } } } } } }""", {"q": q})
    nodes = d["productVariants"]["nodes"]
    return nodes[0]["product"] if len(nodes) >= 1 else None


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
              {"p": {"title": info["title"], "status": "DRAFT", "vendor": info.get("vendor", ""),
                     "productType": info.get("type", ""), "tags": ["skyblue-studio"] + info.get("tags", [])}})
    errs = d["productCreate"]["userErrors"]
    if errs:
        raise RuntimeError(errs[0]["message"])
    prod = d["productCreate"]["product"]
    variant = {"id": prod["variants"]["nodes"][0]["id"]}
    if info.get("price"):
        variant["price"] = str(info["price"])
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
