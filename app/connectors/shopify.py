"""Shopify Admin GraphQL: find a product by SKU, create a draft if missing, attach the approved shots."""
import base64
import httpx

from ..config import SHOPIFY_STORE, SHOPIFY_API_VERSION
from ..secrets import get_secret


def _call(query: str, variables: dict | None = None) -> dict:
    store, token = SHOPIFY_STORE, get_secret("SHOPIFY_ADMIN_TOKEN")
    if not store or not token:
        raise RuntimeError("Shopify not connected. Set SHOPIFY_STORE and the Admin API token.")
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


def find_by_sku(sku: str) -> str | None:
    d = _call("query($q:String!){ productVariants(first:1, query:$q){ nodes{ product{ id } } } }", {"q": f"sku:{sku}"})
    nodes = d["productVariants"]["nodes"]
    return nodes[0]["product"]["id"] if nodes else None


def list_products(limit: int = 100) -> list[dict]:
    d = _call("""query($n:Int!){ products(first:$n){ nodes{ id title vendor productType
        featuredMedia{ preview{ image{ url } } } variants(first:1){ nodes{ sku } } } } }""", {"n": limit})
    out = []
    for p in d["products"]["nodes"]:
        sku = (p["variants"]["nodes"] or [{}])[0].get("sku") or ""
        img = ((p.get("featuredMedia") or {}).get("preview") or {}).get("image") or {}
        out.append({"id": p["id"], "title": p["title"], "vendor": p["vendor"], "type": p["productType"],
                    "sku": sku, "image_url": img.get("url", "")})
    return out


def _staged_upload(name: str, data: bytes) -> str:
    d = _call("""mutation($i:[StagedUploadInput!]!){ stagedUploadsCreate(input:$i){
        stagedTargets{ url resourceUrl parameters{ name value } } userErrors{ message } } }""",
              {"i": [{"resource": "IMAGE", "filename": name, "mimeType": "image/jpeg", "httpMethod": "POST",
                      "fileSize": str(len(data))}]})
    t = d["stagedUploadsCreate"]["stagedTargets"][0]
    form = {p["name"]: p["value"] for p in t["parameters"]}
    httpx.post(t["url"], data=form, files={"file": (name, data, "image/jpeg")}, timeout=120).raise_for_status()
    return t["resourceUrl"]


def publish(product, images: list[tuple[str, bytes, str]]) -> str:
    """images: (filename, jpeg bytes, alt text). Returns the Shopify product gid. New products are created as DRAFT."""
    pid = product.shopify_id or find_by_sku(product.code)
    if not pid:
        d = _call("""mutation($p:ProductCreateInput!){ productCreate(product:$p){ product{ id } userErrors{ message } } }""",
                  {"p": {"title": product.name or product.code, "status": "DRAFT",
                         "vendor": (product.analysis or {}).get("brand_name", product.brand_id),
                         "tags": ["product-studio"]}})
        errs = d["productCreate"]["userErrors"]
        if errs:
            raise RuntimeError(errs[0]["message"])
        pid = d["productCreate"]["product"]["id"]
        v = _call("query($id:ID!){ product(id:$id){ variants(first:1){ nodes{ id } } } }", {"id": pid})
        vid = v["product"]["variants"]["nodes"][0]["id"]
        _call("""mutation($p:ID!,$v:[ProductVariantsBulkInput!]!){ productVariantsBulkUpdate(productId:$p, variants:$v){
            userErrors{ message } } }""", {"p": pid, "v": [{"id": vid, "inventoryItem": {"sku": product.code}}]})
    media = [{"originalSource": _staged_upload(n, b), "alt": alt, "mediaContentType": "IMAGE"} for n, b, alt in images]
    d = _call("""mutation($id:ID!,$m:[CreateMediaInput!]!){ productCreateMedia(productId:$id, media:$m){
        mediaUserErrors{ message } } }""", {"id": pid, "m": media})
    errs = d["productCreateMedia"]["mediaUserErrors"]
    if errs:
        raise RuntimeError(errs[0]["message"])
    return pid


def download(url: str) -> bytes:
    r = httpx.get(url, timeout=60, follow_redirects=True)
    r.raise_for_status()
    return r.content
