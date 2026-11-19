"""OpenRouter: one key for many models (image AND text/vision), used whenever a model name starts with
'openrouter:' — in any shot's model box for image generation, or in the Analysis / SEO model boxes for the
text+vision side (product analysis, shot-prompt drafting, titles, descriptions), so the whole pipeline can run
on OpenRouter instead of Gemini for a side-by-side comparison.

Model examples:
  image:        openrouter:black-forest-labs/flux.2-pro:free · openrouter:black-forest-labs/flux.2-pro ·
                openrouter:openai/gpt-image-1
  text/vision:  openrouter:openai/gpt-5 · openrouter:anthropic/claude-sonnet-4.5 · openrouter:google/gemini-3-pro
"""
import base64

import httpx

from ..secrets import get_secret

URL = "https://openrouter.ai/api/v1/chat/completions"
IMAGES_URL = "https://openrouter.ai/api/v1/images"


def _key() -> str:
    k = get_secret("OPENROUTER_API_KEY")
    if not k:
        raise RuntimeError("OpenRouter key missing: add OPENROUTER_API_KEY in Admin → API & models.")
    return k


def _mime(b: bytes) -> str:
    return "image/png" if b[:4] == b"\x89PNG" else "image/webp" if b[8:12] == b"WEBP" else "image/jpeg"


def _url(b: bytes) -> str:
    return f"data:{_mime(b)};base64,{base64.b64encode(b).decode()}"


RESOLUTIONS = {"1K": "1K", "2K": "2K", "4K": "4K"}


def generate(model: str, prompt: str, images: list[bytes], size: str = "1K") -> tuple[bytes, float | None]:
    """Uses OpenRouter's unified Images API (POST /api/v1/images), not /chat/completions: several models —
    including OpenAI's gpt-image-1/gpt-image-1-mini — are only served through this dedicated endpoint and
    404 on chat/completions even with modalities:[image]. This endpoint also covers FLUX and the rest, with
    reference photos passed as input_references rather than chat image_url content.
    Returns (image_bytes, real_usd_cost). The real cost comes straight from OpenRouter's own usage figure for
    this exact request — accurate, unlike any hand-maintained per-model price table — and is None only if a
    provider didn't report one, in which case the caller falls back to its own estimate."""
    body = {"model": model, "prompt": prompt, "aspect_ratio": "4:5", "response_format": "b64_json",
            "resolution": RESOLUTIONS.get(size, "1K")}
    if images:
        body["input_references"] = [_url(b) for b in images]
    r = httpx.post(IMAGES_URL, json=body, timeout=300, headers={"Authorization": f"Bearer {_key()}",
                                                                 "HTTP-Referer": "https://skyblue.in", "X-Title": "Skyblue Product Studio"})
    if r.status_code >= 400:
        raise RuntimeError(f"OpenRouter {model} failed ({r.status_code}): {r.text[:300]}")
    j = r.json()
    if j.get("error"):
        raise RuntimeError(f"OpenRouter {model}: {str(j['error'])[:300]}")
    data = j.get("data") or []
    if not data:
        raise RuntimeError(f"OpenRouter {model} returned no image")
    item = data[0]
    cost = (j.get("usage") or {}).get("cost")
    cost = float(cost) if isinstance(cost, (int, float)) else None
    if item.get("b64_json"):
        return base64.b64decode(item["b64_json"]), cost
    if item.get("url"):
        return httpx.get(item["url"], timeout=120).content, cost
    raise RuntimeError(f"OpenRouter {model} returned no image data")


def complete(model: str, prompt: str, images: list[bytes] | None = None, json_mode: bool = False,
             temperature: float = 0.2) -> str:
    """Text (optionally JSON) response from a text/vision model, with 0-N images as vision input. Used for
    product analysis, shot-prompt drafting, SEO titles and descriptions when that box is set to 'openrouter:...'."""
    content = [{"type": "text", "text": prompt}] + [{"type": "image_url", "image_url": {"url": _url(b)}} for b in (images or [])]
    body = {"model": model, "messages": [{"role": "user", "content": content}], "temperature": temperature}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    r = httpx.post(URL, json=body, timeout=300, headers={"Authorization": f"Bearer {_key()}",
                                                          "HTTP-Referer": "https://skyblue.in", "X-Title": "Skyblue Product Studio"})
    if r.status_code >= 400:
        raise RuntimeError(f"OpenRouter {model} failed ({r.status_code}): {r.text[:300]}")
    j = r.json()
    if j.get("error"):
        raise RuntimeError(f"OpenRouter {model}: {str(j['error'])[:300]}")
    choices = j.get("choices") or []
    if not choices:
        raise RuntimeError(f"OpenRouter {model} returned no response")
    text = (choices[0].get("message") or {}).get("content") or ""
    if isinstance(text, list):                            # some models return content as a list of parts
        text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
    if not text:
        raise RuntimeError(f"OpenRouter {model} returned an empty response")
    return text


def test() -> str:
    r = httpx.get("https://openrouter.ai/api/v1/key", timeout=30, headers={"Authorization": f"Bearer {_key()}"})
    if r.status_code >= 400:
        raise RuntimeError(f"OpenRouter refused the key ({r.status_code}): {r.text[:200]}")
    d = r.json().get("data", {})
    left = d.get("limit_remaining")
    return (f"OpenRouter connected. Credits used: ${d.get('usage', 0):.2f}"
            + (f", remaining limit: ${left:.2f}" if isinstance(left, (int, float)) else "")
            + (" · free tier" if d.get("is_free_tier") else ""))
