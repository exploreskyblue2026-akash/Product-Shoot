"""OpenRouter: one key for many models (image AND text/vision), used whenever a model name starts with
'openrouter:' — in any shot's model box for image generation, or in the Analysis / SEO model boxes for the
text+vision side (product analysis, shot-prompt drafting, titles, descriptions), so the whole pipeline can run
on OpenRouter instead of Gemini for a side-by-side comparison.

Model examples:
  image:        openrouter:black-forest-labs/flux.2-pro:free · openrouter:black-forest-labs/flux.2-pro ·
                openrouter:openai/gpt-image-1
  text/vision:  openrouter:openai/gpt-5 · openrouter:anthropic/claude-sonnet-4.5 · openrouter:google/gemini-3-pro

A ':free' image model can intermittently 404 with "No endpoint found for model ..." even though the model name
is correct and worked a minute earlier: OpenRouter's free tier is served by whichever provider currently
volunteers to host that model for $0, and that pool's capacity comes and goes. generate() retries once on the
paid (non-':free') version of the same model when this specific error shows up, and reports back which model
actually produced the image so cost/credit tracking stays accurate — see model_used in its return value.
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
# Not every provider on OpenRouter's Images API accepts the same aspect_ratio/resolution values; sending one
# the model doesn't support gets "No provider for ..." (a routing failure, not a validation error - OpenRouter
# checked it against the model's real capabilities and found no match). gpt-image-1/-mini only take
# aspect_ratio in {1:1, 3:2, 2:3, auto} and have no resolution/size parameter at all - "2:3" is the closest
# portrait ratio they support to our default 4:5, and quality stands in for size on these.
QUIRKS = {
    "openai/gpt-image-1": {"aspect_ratio": "2:3", "resolution": False, "quality": {"1K": "low", "2K": "medium", "4K": "high"}},
    "openai/gpt-image-1-mini": {"aspect_ratio": "2:3", "resolution": False, "quality": {"1K": "low", "2K": "medium", "4K": "high"}},
}


def _is_no_endpoint(status: int, body_text: str) -> bool:
    """True for OpenRouter's '404 No endpoint found for model ...' — a routing failure meaning no provider is
    currently serving this exact model string, not that the model name is wrong. Free-tier (':free') models are
    served by whoever currently volunteers to host them for $0, a pool with real capacity limits; that pool can
    empty out between one request and the next with zero code change on our side; the same model string can work
    again minutes later once a provider re-opens. This is distinct from a bad model name (which 404s the same way
    but never used to work) or a missing capability (a different error shape, handled by QUIRKS above)."""
    return status == 404 and "no endpoint found" in body_text.lower()


def generate(model: str, prompt: str, images: list[bytes], size: str = "1K") -> tuple[bytes, float | None]:
    """Uses OpenRouter's unified Images API (POST /api/v1/images), not /chat/completions: several models —
    including OpenAI's gpt-image-1/gpt-image-1-mini — are only served through this dedicated endpoint and
    404 on chat/completions even with modalities:[image]. This endpoint also covers FLUX and the rest, with
    reference photos passed as input_references rather than chat image_url content.
    Returns (image_bytes, real_usd_cost, model_used). The real cost comes straight from OpenRouter's own usage
    figure for this exact request — accurate, unlike any hand-maintained per-model price table — and is None only
    if a provider didn't report one, in which case the caller falls back to its own estimate. model_used is the
    model that actually generated the image: usually == model, but see the free-tier fallback below."""
    tried = [model]
    if model.endswith(":free"):
        tried.append(model[:-len(":free")])   # same model, paid tier — only used if the free pool has no endpoint
    last_err = None
    for i, m in enumerate(tried):
        quirk = QUIRKS.get(m, {})
        body = {"model": m, "prompt": prompt, "aspect_ratio": quirk.get("aspect_ratio", "4:5"), "response_format": "b64_json"}
        if quirk.get("resolution", True):
            body["resolution"] = RESOLUTIONS.get(size, "1K")
        if "quality" in quirk:
            body["quality"] = quirk["quality"].get(size, "medium")
        if images:
            body["input_references"] = [{"type": "image_url", "image_url": {"url": _url(b)}} for b in images]
        r = httpx.post(IMAGES_URL, json=body, timeout=300, headers={"Authorization": f"Bearer {_key()}",
                                                                     "HTTP-Referer": "https://skyblue.in", "X-Title": "Skyblue Product Studio"})
        if r.status_code >= 400:
            # Only fall through to the paid variant for the specific "no endpoint" routing failure, and only
            # when there's a next model to try — any other error (bad request, auth, real content policy
            # block) should surface immediately rather than silently spend money on a retry that won't help.
            if _is_no_endpoint(r.status_code, r.text) and i + 1 < len(tried):
                last_err = f"OpenRouter {m} failed ({r.status_code}): {r.text[:300]}"
                continue
            raise RuntimeError(f"OpenRouter {m} failed ({r.status_code}): {r.text[:300]}")
        j = r.json()
        if j.get("error"):
            raise RuntimeError(f"OpenRouter {m}: {str(j['error'])[:300]}")
        data = j.get("data") or []
        if not data:
            raise RuntimeError(f"OpenRouter {m} returned no image")
        item = data[0]
        cost = (j.get("usage") or {}).get("cost")
        cost = float(cost) if isinstance(cost, (int, float)) else None
        if item.get("b64_json"):
            return base64.b64decode(item["b64_json"]), cost, m
        if item.get("url"):
            return httpx.get(item["url"], timeout=120).content, cost, m
        raise RuntimeError(f"OpenRouter {m} returned no image data")
    raise RuntimeError(last_err or f"OpenRouter {model} failed")


def complete(model: str, prompt: str, images: list[bytes] | None = None, json_mode: bool = False,
             temperature: float = 0.2, max_tokens: int | None = None) -> str:
    """Text (optionally JSON) response from a text/vision model, with 0-N images as vision input. Used for
    product analysis, shot-prompt drafting, SEO titles and descriptions when that box is set to 'openrouter:...'.

    max_tokens matters more than it looks: when it's left unset, OpenRouter assumes the worst case - the model's
    entire max output window (e.g. 65536 tokens for Claude Sonnet 5.5) - and pre-checks whether the account's
    balance could cover THAT, even though a real response is a few hundred to a few thousand tokens. On a modest
    balance this fails upfront with 402 "This request requires more credits, or fewer max_tokens" before the
    model does any work at all - the exact failure the Library's analysis step was hitting. The fix is not to
    truncate the analysis (a lower cap could cut off a real answer); json_mode defaults to a generous 32000, which
    is reserved for the credit pre-check but essentially never actually reached by the shot-plan JSON this calls
    for, and plain text replies (titles, descriptions) default to a small 4000 since those are always short."""
    content = [{"type": "text", "text": prompt}] + [{"type": "image_url", "image_url": {"url": _url(b)}} for b in (images or [])]
    body = {"model": model, "messages": [{"role": "user", "content": content}], "temperature": temperature,
            "max_tokens": max_tokens if max_tokens is not None else (32000 if json_mode else 4000)}
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
    if choices[0].get("finish_reason") == "length":
        # Cut off by the max_tokens cap rather than ending naturally - surface this loudly instead of handing back
        # truncated JSON that would fail to parse (or parse into a silently incomplete plan). If this ever actually
        # fires, the fix is to raise the cap further, not to let a half-finished analysis through.
        raise RuntimeError(f"OpenRouter {model}: response was cut off at the {body['max_tokens']}-token cap before "
                            f"finishing - raise max_tokens rather than accept a truncated analysis")
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
