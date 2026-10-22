"""Google Gemini: vision analysis, image generation (Nano Banana family) and QC review."""
import io
import json
from PIL import Image
from google import genai
from google.genai import types

from ..config import GEMINI_API_KEY, MODEL_VISION as _MV, IMAGE_SIZE, model_name

MODEL_VISION = model_name(_MV)
from ..secrets import get_secret

QC_CHECKS = ["shape_match", "colour_match", "label_text", "soft_light", "attention_70_30",
             "props_ok", "no_artifacts", "margin_ok"]


def _mime(data: bytes) -> str:
    fmt = (Image.open(io.BytesIO(data)).format or "JPEG").lower()
    return {"jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(fmt, "image/jpeg")


def _json(text: str) -> dict:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`").split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    return json.loads(t)


def _pick(names: list[str], kind: str) -> str | None:
    bad = ("tts", "live", "embedding", "transcribe", "robotics", "lite", "aqa", "omni")
    if kind == "image":
        pref = [n for n in names if ("banana" in n or "image" in n) and "imagen" not in n]
        pref.sort(key=lambda n: (0 if "banana" in n else 1 if "pro-image" in n else 2, n), reverse=False)
        return pref[0] if pref else None
    pref = [n for n in names if "flash" in n and "image" not in n and not any(b in n for b in bad)]
    pref.sort(reverse=True)
    return pref[0] if pref else None


class GeminiProvider:
    _resolved: dict = {}

    def _available(self) -> list[str]:
        if "names" not in self._resolved:
            self._resolved["names"] = [m.name.split("/")[-1] for m in self.client.models.list()]
        return self._resolved["names"]

    def _call(self, model: str, kind: str, **kw):
        """Call a model with retries. Busy/limit errors (503, 429) wait and retry, then try another available
        model of the same kind. 'Not found' switches to the closest available model straight away."""
        import time
        model = self._resolved.get(model, model)
        tried = [model]
        last = ""
        while True:
            for attempt in range(4):
                try:
                    return self.client.models.generate_content(model=model, **kw)
                except Exception as e:
                    msg = str(e)
                    last = f"{model}: {msg[:300]}"
                    low = msg.lower()
                    if ("429" in msg or "resource_exhausted" in low) and ("limit: 0" in low or "free_tier" in low
                                                                         or "billing" in low or "quota" in low):
                        raise RuntimeError(
                            "Gemini quota: this API key has no image-generation quota (free tier allows 0 for image "
                            "models). Fix: Google AI Studio → API keys → set up billing on the key's project, then "
                            f"click Generate now. Google said: {msg[:200]}") from e
                    if "NOT_FOUND" in msg or " 404" in msg or msg.startswith("404"):
                        break
                    busy = any(t in msg for t in ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "overloaded",
                                                  "high demand", "500", "INTERNAL", "DEADLINE"))
                    if not busy:
                        raise
                    if attempt < 3:
                        wait = 8 * (2 ** attempt)          # 8s, 16s, 32s
                        print(f"[gemini] {model} busy ({msg[:60]}), retrying in {wait}s")
                        time.sleep(wait)
            alt = _pick([n for n in self._available() if n not in tried], kind)
            if not alt:
                raise RuntimeError("Every available Gemini model refused the request. Last reply from Google — "
                                   f"{last}. If this says quota/429, enable billing on the key's project in "
                                   "Google AI Studio; if it says 503, Google is overloaded, try again later.")
            print(f"[gemini] switching {model} -> {alt}")
            self._resolved[tried[0]] = alt
            tried.append(alt)
            model = alt

    def __init__(self):
        key = get_secret("GEMINI_API_KEY") or GEMINI_API_KEY
        if not key:
            raise RuntimeError("Gemini API key missing. Add it in Admin → API & models or set GEMINI_API_KEY.")
        self.client = genai.Client(api_key=key)

    def test(self) -> str:
        from .. import config
        names = sorted(m.name.split("/")[-1] for m in self.client.models.list())
        need = {"vision": MODEL_VISION, "image pro": model_name(config.MODEL_IMAGE_PRO), "image fast": model_name(config.MODEL_IMAGE_FAST)}
        status = "; ".join(f"{k}: {v} {'OK' if v in names else 'NOT AVAILABLE to this key'}" for k, v in need.items())
        imgs = [n for n in names if "image" in n or "banana" in n]
        flash = [n for n in names if "flash" in n and "image" not in n][:8]
        return f"Key works. {status}.\nImage models for this key: {', '.join(imgs) or 'none'}.\nVision/flash models: {', '.join(flash)}"

    def analyse(self, raw: bytes, context: dict) -> dict:
        prompt = (
            "You are the product analyst for a product photo studio. Look at the raw product photo and the product data. "
            "Return ONLY JSON matching this schema (use ids from the allowed lists, never invent ids):\n"
            f"{json.dumps(context['schema'])}\n\nAllowed brand_id: {context['brands']}\nAllowed category_id: {context['categories']}\n"
            f"Allowed mood_id: {context['moods']}\nAllowed prop ids per category: {json.dumps(context['props'])}\n"
            f"Product data: {json.dumps(context['product'])}\nSelection rules: {json.dumps(context['rules'])}"
        )
        r = self._call(MODEL_VISION, "vision",
            contents=[types.Part.from_bytes(data=raw, mime_type=_mime(raw)), prompt],
            config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.2))
        return _json(r.text)

    def generate(self, prompt: str, raw: bytes, model: str) -> bytes:
        r = self._call(model_name(model), "image",
            contents=[types.Part.from_bytes(data=raw, mime_type=_mime(raw)),
                      "Reference image above is the real product. " + prompt],
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"],
                image_config=types.ImageConfig(aspect_ratio="4:5", image_size=IMAGE_SIZE)))
        for cand in r.candidates or []:
            for part in (cand.content.parts if cand.content else []) or []:
                if part.inline_data and part.inline_data.data:
                    return part.inline_data.data
        reason = getattr(r.candidates[0], "finish_reason", "no image") if r.candidates else "no candidates"
        raise RuntimeError(f"Model returned no image ({reason})")

    def qc(self, raw: bytes, gen: bytes, shot: dict, rules: dict) -> dict:
        prompt = (
            "You are a strict QC reviewer for e-commerce product photos. Image 1 is the real product, image 2 is the generated "
            f"shot ({shot['kind']}). Check each item and return ONLY JSON: "
            '{"checks":{"shape_match":bool,"colour_match":bool,"label_text":bool,"soft_light":bool,"attention_70_30":bool,'
            '"props_ok":bool,"no_artifacts":bool,"margin_ok":bool},"reasons":{"<failed check>":"short reason"}}. '
            "shape_match: same shape, proportions and item count. colour_match: same product colours. label_text: logo and "
            "printed text identical, nothing invented. soft_light: no hard shadows, hot spots or direct-sun look. "
            "attention_70_30: product is the clear focal point. props_ok: at most 5 props, none branded, realistic scale. "
            "no_artifacts: no extra text, watermark, warped hands or geometry. margin_ok: product not cropped, clear margin."
        )
        r = self._call(MODEL_VISION, "vision",
            contents=[types.Part.from_bytes(data=raw, mime_type=_mime(raw)),
                      types.Part.from_bytes(data=gen, mime_type=_mime(gen)), prompt],
            config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0))
        data = _json(r.text)
        checks = {k: bool(data.get("checks", {}).get(k, False)) for k in QC_CHECKS}
        return {"checks": checks, "reasons": data.get("reasons", {})}
