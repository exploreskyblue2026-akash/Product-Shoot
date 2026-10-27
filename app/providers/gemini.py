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
        # cheapest first; never switch to the Pro image model automatically
        pref = [n for n in names if ("banana" in n or "image" in n) and "imagen" not in n and "pro" not in n
            and not n.startswith(("gemini-1.", "gemini-2."))]
        pref.sort(key=lambda n: (0 if "lite" in n else 1 if "banana" in n else 2, n))
        return pref[0] if pref else None
    pref = [n for n in names if "flash" in n and "image" not in n and not any(b in n for b in bad)
            and not n.startswith(("gemini-1.", "gemini-2."))]
    pref.sort(key=lambda n: ("preview" in n or "exp" in n, [-int(x) if x.isdigit() else 0 for x in n.replace("-", ".").split(".")]))
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
        errors = []
        while True:
            for attempt in range(3):
                try:
                    r = self.client.models.generate_content(model=model, **kw)
                    self.last_model = model
                    return r
                except Exception as e:
                    msg = str(e)
                    last = f"{model}: {msg[:300]}"
                    if not errors or errors[-1][0] != model:
                        errors.append((model, msg[:300]))
                    low = msg.lower()
                    if msg.startswith("402") or " 402 " in msg or "'code': 402" in msg or "prepay" in low:
                        raise RuntimeError("Gemini credits are used up for this API key (402). Top up in AI Studio → "
                                           "Billing, or check the tool is using the key from the paid project.") from e
                    is429 = "429" in msg or "resource_exhausted" in low
                    if is429 and ("limit: 0" in low or "free_tier" in low):
                        # this model has no quota on this key/tier: treat like "not available", try the next model
                        print(f"[gemini] {model} has no quota on this key: {msg[:160]}")
                        break
                    if is429 and attempt < 2:
                        print(f"[gemini] {model} rate-limited, waiting 30s: {msg[:120]}")
                        time.sleep(30)
                        continue
                    if ("400" in msg or "INVALID_ARGUMENT" in msg) and "models/" in msg and (
                            "not found" in low or "not supported" in low or "is not" in low or "unsupported" in low):
                        print(f"[gemini] {model} rejected as a model: {msg[:160]}")
                        break
                    if "NOT_FOUND" in msg or " 404" in msg or msg.startswith("404"):
                        break
                    busy = any(t in msg for t in ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "overloaded",
                                                  "high demand", "500", "INTERNAL", "DEADLINE"))
                    if not busy:
                        raise
                    if attempt < 2:
                        wait = 10 * (2 ** attempt)         # 10s, 20s (a refused call is not billed)
                        print(f"[gemini] {model} busy ({msg[:60]}), retrying in {wait}s")
                        time.sleep(wait)
            alt = _pick([n for n in self._available() if n not in tried], kind)
            if not alt:
                first = errors[0] if errors else (model, last)
                raise RuntimeError(f"{first[0]} failed: {first[1]}  (also tried: {', '.join(tried[1:]) or 'none'})")
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

    def _vision(self, contents, temperature=0.2):
        """JSON-mode vision call; if Google rejects the config (400), retry once with a plain request."""
        try:
            return self._call(MODEL_VISION, "vision", contents=contents,
                              config=types.GenerateContentConfig(response_mime_type="application/json",
                                                                 temperature=temperature))
        except Exception as e:
            if "400" not in str(e) and "INVALID_ARGUMENT" not in str(e):
                raise
            print(f"[gemini] JSON-mode vision call rejected, retrying plain: {str(e)[:160]}")
            return self._call(MODEL_VISION, "vision", contents=contents)

    def analyse(self, raw: bytes, context: dict) -> dict:
        prompt = (
            "You are the product analyst for a product photo studio. Look at the raw product photo and the product data. "
            "Return ONLY JSON matching this schema (use ids from the allowed lists, never invent ids):\n"
            f"{json.dumps(context['schema'])}\n\nAllowed brand_id: {context['brands']}\nAllowed category_id: {context['categories']}\n"
            f"Allowed mood_id: {context['moods']}\nAllowed prop ids per category: {json.dumps(context['props'])}\n"
            f"Product data: {json.dumps(context['product'])}\nSelection rules: {json.dumps(context['rules'])}"
        )
        contents = [types.Part.from_bytes(data=raw, mime_type=_mime(raw)), prompt + "\nReply with JSON only."]
        r = self._vision(contents, temperature=0.2)
        return _json(r.text)

    def generate(self, prompt: str, raw: bytes, model: str, size: str = IMAGE_SIZE, refs: list | None = None,
                 style: bytes | None = None) -> bytes:
        parts = [types.Part.from_bytes(data=raw, mime_type=_mime(raw))]
        for extra in (refs or [])[:2]:
            parts.append(types.Part.from_bytes(data=extra, mime_type=_mime(extra)))
        n = len(parts)
        lead = ("The first image above is the real product. " if n == 1 else
                f"The first {n} images above all show the SAME real product from different angles; "
                "the first is the main reference. Reproduce its exact shape, colours, logo and printed text. ")
        if style:
            parts.append(types.Part.from_bytes(data=style, mime_type=_mime(style)))
            lead += ("The LAST image is a STYLE REFERENCE ONLY: match its lighting, background, colour mood and "
                     "composition, but do NOT copy any object, product or text from it. ")
        r = self._call(model_name(model), "image",
            contents=parts + [lead + prompt],
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"],
                image_config=types.ImageConfig(aspect_ratio="4:5", image_size=size)))
        for cand in r.candidates or []:
            for part in (cand.content.parts if cand.content else []) or []:
                if part.inline_data and part.inline_data.data:
                    return part.inline_data.data
        reason = getattr(r.candidates[0], "finish_reason", "no image") if r.candidates else "no candidates"
        raise RuntimeError(f"Model returned no image ({reason})")

    def study(self, images: list[bytes], ctx: dict) -> dict:
        """Product memory: study every photo of one product once and return exact facts as JSON."""
        prompt = (
            f"You are a meticulous product cataloguer for an e-commerce studio. The {len(images)} photos above all show the "
            f"SAME product: '{ctx.get('name')}' (brand: {ctx.get('brand') or 'unknown'}, category: {ctx.get('category')}, "
            f"code: {ctx.get('code')}). Describe it so an image generator can reproduce it EXACTLY. Transcribe printed "
            "text letter by letter; only include what you can actually see. Return ONLY JSON with these keys:\n"
            + json.dumps(ctx["schema"]))
        parts = [types.Part.from_bytes(data=b, mime_type=_mime(b)) for b in images[:4]]
        r = self._vision(parts + [prompt + "\nReply with JSON only."], temperature=0)
        return _json(r.text)

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
        if shot.get("facts"):
            prompt += ("\nVerify against these known PRODUCT FACTS (from the real product): " + shot["facts"] +
                       "\nlabel_text fails if any listed text is misspelt, missing or extra text was invented. "
                       "shape_match fails if any listed part or count is wrong. colour_match fails on a different colour.")
        r = self._vision([types.Part.from_bytes(data=raw, mime_type=_mime(raw)),
                          types.Part.from_bytes(data=gen, mime_type=_mime(gen)), prompt + "\nReply with JSON only."],
                         temperature=0)
        data = _json(r.text)
        checks = {k: bool(data.get("checks", {}).get(k, False)) for k in QC_CHECKS}
        return {"checks": checks, "reasons": data.get("reasons", {})}


def _scene(self, prompt: str, model: str, size: str = IMAGE_SIZE) -> bytes:
    """v2: an empty background scene, no product reference image."""
    r = self._call(model_name(model), "image", contents=[prompt],
                   config=types.GenerateContentConfig(response_modalities=["IMAGE"],
                                                      image_config=types.ImageConfig(aspect_ratio="4:5", image_size=size)))
    for cand in r.candidates or []:
        for part in (cand.content.parts if cand.content else []) or []:
            if part.inline_data and part.inline_data.data:
                return part.inline_data.data
    raise RuntimeError("Model returned no scene image")


GeminiProvider.generate_scene = _scene
