"""OpenRouter: one key for many image models (e.g. FLUX.2). Used when a model name starts with 'openrouter:'.

Model examples: openrouter:black-forest-labs/flux.2-pro:free · openrouter:black-forest-labs/flux.2-pro
"""
import base64

import httpx

from ..secrets import get_secret

URL = "https://openrouter.ai/api/v1/chat/completions"


def _key() -> str:
    k = get_secret("OPENROUTER_API_KEY")
    if not k:
        raise RuntimeError("OpenRouter key missing: add OPENROUTER_API_KEY in Admin → API & models.")
    return k


def _mime(b: bytes) -> str:
    return "image/png" if b[:4] == b"\x89PNG" else "image/webp" if b[8:12] == b"WEBP" else "image/jpeg"


def _url(b: bytes) -> str:
    return f"data:{_mime(b)};base64,{base64.b64encode(b).decode()}"


def generate(model: str, prompt: str, images: list[bytes]) -> bytes:
    content = [{"type": "text", "text": prompt}] + [{"type": "image_url", "image_url": {"url": _url(b)}} for b in images]
    body = {"model": model, "messages": [{"role": "user", "content": content}], "modalities": ["image", "text"],
            "image_config": {"aspect_ratio": "4:5"}}
    r = httpx.post(URL, json=body, timeout=300, headers={"Authorization": f"Bearer {_key()}",
                                                          "HTTP-Referer": "https://skyblue.in", "X-Title": "Skyblue Product Studio"})
    if r.status_code >= 400:
        raise RuntimeError(f"OpenRouter {model} failed ({r.status_code}): {r.text[:300]}")
    j = r.json()
    if j.get("error"):
        raise RuntimeError(f"OpenRouter {model}: {str(j['error'])[:300]}")
    for ch in j.get("choices") or []:
        for im in (ch.get("message") or {}).get("images") or []:
            u = (im.get("image_url") or {}).get("url", "")
            if u.startswith("data:"):
                return base64.b64decode(u.split(",", 1)[1])
            if u.startswith("http"):
                return httpx.get(u, timeout=120).content
    raise RuntimeError(f"OpenRouter {model} returned no image")


def test() -> str:
    r = httpx.get("https://openrouter.ai/api/v1/key", timeout=30, headers={"Authorization": f"Bearer {_key()}"})
    if r.status_code >= 400:
        raise RuntimeError(f"OpenRouter refused the key ({r.status_code}): {r.text[:200]}")
    d = r.json().get("data", {})
    left = d.get("limit_remaining")
    return (f"OpenRouter connected. Credits used: ${d.get('usage', 0):.2f}"
            + (f", remaining limit: ${left:.2f}" if isinstance(left, (int, float)) else "")
            + (" · free tier" if d.get("is_free_tier") else ""))
