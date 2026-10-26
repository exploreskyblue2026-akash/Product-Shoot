"""FastAPI app: serves the approved UI and the API behind it. Start: uvicorn app.main:app"""
import threading
from datetime import datetime, timedelta, timezone
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from starlette.middleware.sessions import SessionMiddleware

from . import config
from .auth import RANK, current_user, hash_pw, login, need, new_token
from .connectors import storage
from .dataset import LIB_TABLES, build_prompts, live_rules, seed
from .db import (Activity, LibraryRow, Product, Repository, RuleVersion, SessionLocal, Shot, Source, Usage, User,
                 get_setting, init_db, log, now, set_setting)
from . import ingest, pipeline, secrets

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(_app):
    init_db()
    with SessionLocal() as s:
        seed(s, config.OWNER_EMAIL.lower(), hash_pw(config.OWNER_PASSWORD))
    if config.RUN_WORKER:
        pipeline.start_background()
    yield


app = FastAPI(title="Skyblue Product Studio", docs_url="/api/docs", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY, same_site="lax",
                   https_only=config.env("COOKIE_SECURE", "true") == "true", max_age=60 * 60 * 24 * 14)

PAGES = {"index": "index.html", "admin": "admin.html", "login": "login.html"}


@app.exception_handler(HTTPException)
def http_err(request: Request, exc: HTTPException):
    if exc.status_code == 401 and not request.url.path.startswith("/api/"):
        return RedirectResponse("/login")
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


# ---------------------------------------------------------------- pages
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if not current_user(request):
        return RedirectResponse("/login")
    return FileResponse(config.WEB_DIR / "index.html")


@app.get("/index.html")
def index_alias():
    return RedirectResponse("/")


@app.get("/admin.html")
def admin_page(u=Depends(need("admin"))):
    return FileResponse(config.WEB_DIR / "admin.html")


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return FileResponse(config.WEB_DIR / "login.html")


@app.get("/studio.css")
def css():
    return FileResponse(config.WEB_DIR / "studio.css", media_type="text/css")


@app.get("/files/{rel:path}")
def files(rel: str, u=Depends(need("viewer"))):
    try:
        p = storage.path(rel)
    except ValueError:
        raise HTTPException(404)
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/health")
def health():
    return {"ok": True, "version": config.APP_VERSION, "provider": config.PROVIDER, "worker": config.RUN_WORKER,
            "vision_model": config.model_name(config.MODEL_VISION)}


# ---------------------------------------------------------------- auth
class LoginIn(BaseModel):
    email: str
    password: str


@app.post("/api/login")
def api_login(body: LoginIn, request: Request):
    u = login(body.email, body.password)
    if not u:
        raise HTTPException(400, "Email or password is wrong")
    request.session["uid"] = u.id
    return {"ok": True, "role": u.role}


@app.post("/api/logout")
def api_logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/me")
def me(u=Depends(need("viewer"))):
    return {"email": u.email, "role": u.role}


class AcceptIn(BaseModel):
    token: str
    password: str


@app.post("/api/invite/accept")
def accept(body: AcceptIn, request: Request):
    if len(body.password) < 10:
        raise HTTPException(400, "Use at least 10 characters")
    with SessionLocal() as s:
        u = s.scalars(select(User).where(User.invite_token == body.token, User.invite_token != "")).first()
        if not u:
            raise HTTPException(400, "This invite link is no longer valid")
        u.password_hash, u.invite_token, u.active = hash_pw(body.password), "", True
        log(s, u.email, "Accepted invite")
        s.commit()
        request.session["uid"] = u.id
    return {"ok": True}


# ---------------------------------------------------------------- studio
def img_url(rel: str) -> str:
    return f"/files/{rel}" if rel else ""


def product_brief(p: Product) -> dict:
    done = sum(1 for sh in p.shots if sh.status in ("pass", "fail", "approved", "unchecked"))
    d = p.data or {}
    return {"id": p.id, "code": p.code, "name": p.name, "brand": d.get("brand_label") or p.brand_id, "stage": p.stage,
            "done": done, "error": p.error, "updated": p.updated.isoformat() if p.updated else "",
            "v2": bool(d.get("v2")), "priority": d.get("priority"), "paused": bool(d.get("paused")),
            "shots_wanted": d.get("shots") or []}


def v2_summary(s) -> dict:
    from .db import LibImage as _LI
    items = get_setting(s, "shoot_list", []) or []
    keys = [i["key"] for i in items if i.get("key")]
    from .library import cutouts_on
    ok = ["ok", "check"] if cutouts_on(s) else ["pending", "ok", "check"]
    ready = sum(1 for k in keys if s.scalar(select(_LI).where(_LI.key == k, _LI.primary == True,  # noqa: E712
                                                                 _LI.cut_status.in_(ok))))
    from .db import SceneAsset
    return {"shoot": len(items), "in_master": len(keys), "ready": ready,
            "scenes": s.scalar(select(func.count(SceneAsset.id)).where(SceneAsset.retired == False)) or 0,  # noqa: E712
            "strategy": get_setting(s, "scene_strategy", "auto"), "cutouts": cutouts_on(s)}


@app.get("/api/state")
def state(u=Depends(need("viewer"))):
    with SessionLocal() as s:
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        spend_today = float(s.scalar(select(func.coalesce(func.sum(Usage.cost), 0.0)).where(Usage.ts >= today)) or 0)
        prods = list(s.scalars(select(Product).where(Product.stage != "rejected").order_by(Product.id)))
        srcs = list(s.scalars(select(Source).order_by(Source.id.desc())))
        mode = get_setting(s, "pipeline_mode", "v1")
        counts = {}
        for p in prods:
            counts.setdefault(p.source_id, 0)
            counts[p.source_id] += 1
        return {
            "user": {"email": u.email, "role": u.role},
            "autopilot": {k: get_setting(s, k) for k in ("autopilot", "auto_approve", "retries", "publish_shopify")},
            "spend_today": round(spend_today, 2), "spend_month": round(pipeline.month_spend(s), 2),
            "usd_inr": get_setting(s, "usd_inr", 88.0), "daily_budget": get_setting(s, "daily_budget", 3.0),
            "cost_mode": get_setting(s, "cost_mode", "economy"),
            "budget": get_setting(s, "budget_month", 150),
            "sources": [{"id": x.id, "type": x.type, "name": x.name, "detail": x.detail, "watch": x.watch,
                         "status": x.status, "error": x.error, "count": counts.get(x.id, 0)} for x in srcs],
            "queue": sorted([product_brief(p) for p in prods if p.stage in ("queue", "gen", "review", "error", "needs_photo")
                             and bool((p.data or {}).get("v2")) == (mode == "v2")],
                            key=lambda q: (q["priority"] if q["priority"] is not None else 10_000, q["id"])),
            "pipeline_mode": mode, "v2": v2_summary(s) if mode == "v2" else None,
            "saved": [product_brief(p) for p in reversed(prods) if p.stage == "saved"][:200],
        }


@app.get("/api/products/{pid}")
def product(pid: int, u=Depends(need("viewer"))):
    with SessionLocal() as s:
        p = s.get(Product, pid)
        if not p:
            raise HTTPException(404, "Product not found")
        shots = {sh.no: sh for sh in p.shots}
        return {**product_brief(p), "raw": img_url(p.raw_path), "analysis": p.analysis, "data": p.data, "cost": round(p.cost, 3),
                "category": p.category_id, "shopify_id": p.shopify_id,
                "shots": [{"no": no, "kind": kind, "status": shots[no].status if no in shots else "wait",
                           "image": img_url(shots[no].image_path) if no in shots else "",
                           "qc": shots[no].qc if no in shots else {}, "attempts": shots[no].attempts if no in shots else 0,
                           "model": shots[no].model if no in shots else ""}
                          for no, kind in pipeline.SHOT_NAMES.items()]}


class SourceIn(BaseModel):
    type: str
    link: str = ""
    brand: str = ""
    code_from: str = ""
    watch: bool = True


TYPE_NAMES = {"drive": "Google Drive", "catalogue": "Brand catalogue", "files": "Uploaded files",
              "sheet": "Sheet / CSV", "shopify": "Shopify", "url": "Web link"}


def _scan_async(sid: int):
    def run():
        with SessionLocal() as s:
            ingest.scan(s, s.get(Source, sid))
    threading.Thread(target=run, daemon=True).start()


@app.post("/api/sources")
def add_source(body: SourceIn, u=Depends(need("editor"))):
    if body.type not in TYPE_NAMES or body.type == "files":
        raise HTTPException(400, "Unknown input type")
    if body.type != "shopify" and not body.link.strip():
        raise HTTPException(400, "Paste a link first")
    with SessionLocal() as s:
        src = Source(type=body.type, name=TYPE_NAMES[body.type], detail=body.link.replace("https://", "")[:200],
                     config={"link": body.link.strip(), "brand": "" if body.brand.lower() in ("", "detect") else body.brand.lower(),
                             "code_from": body.code_from}, watch=body.watch and body.type in ("drive", "sheet", "shopify"))
        s.add(src)
        log(s, u.email, f"Added {src.name}: {src.detail}")
        s.commit()
        sid = src.id
    _scan_async(sid)
    return {"ok": True, "id": sid}


@app.post("/api/sources/upload")
async def upload(files: list[UploadFile] = File(...), brand: str = Form(""), u=Depends(need("editor"))):
    payload = [(f.filename or "file", await f.read()) for f in files]
    with SessionLocal() as s:
        src = Source(type="files", name="Uploaded files", detail=f"{len(payload)} file(s) · {payload[0][0]}"[:200],
                     config={"brand": "" if brand.lower() in ("", "detect") else brand.lower()}, status="ok", last_scan=now())
        s.add(src)
        s.flush()
        n = ingest.ingest_files(s, src, payload)
        log(s, u.email, f"Uploaded {len(payload)} file(s): {n} product codes")
        s.commit()
    return {"ok": True, "added": n}


@app.delete("/api/sources/{sid}")
def del_source(sid: int, u=Depends(need("editor"))):
    with SessionLocal() as s:
        src = s.get(Source, sid)
        if src:
            src.watch = False
            log(s, u.email, f"Stopped watching {src.name}")
            s.commit()
    return {"ok": True}


@app.post("/api/sources/{sid}/rescan")
def rescan(sid: int, u=Depends(need("editor"))):
    _scan_async(sid)
    return {"ok": True}


class AutoIn(BaseModel):
    pipeline_mode: str | None = None
    autopilot: bool | None = None
    auto_approve: bool | None = None
    publish_shopify: bool | None = None
    retries: int | None = None


@app.post("/api/autopilot")
def autopilot(body: AutoIn, u=Depends(need("editor"))):
    with SessionLocal() as s:
        for k, v in body.model_dump(exclude_none=True).items():
            set_setting(s, k, v)
        if body.pipeline_mode is not None:
            if body.pipeline_mode not in ("v1", "v2"):
                raise HTTPException(400, "pipeline_mode must be v1 or v2")
            log(s, u.email, "Pipeline switched to " + body.pipeline_mode)
        if body.autopilot:
            pipeline.STOP.clear()
        if body.autopilot is not None:
            log(s, u.email, "Autopilot " + ("on" if body.autopilot else "off"))
        s.commit()
    return {"ok": True}


def _get(s, pid) -> Product:
    p = s.get(Product, pid)
    if not p:
        raise HTTPException(404, "Product not found")
    return p


@app.post("/api/products/{pid}/run")
def run_now(pid: int, u=Depends(need("editor"))):
    with SessionLocal() as s:
        p = _get(s, pid)
        if not p.raw_path:
            raise HTTPException(400, "This product has no raw photo yet")
        p.data, p.stage, p.error = {**(p.data or {}), "run_now": True}, "queue", ""
        s.commit()
    pipeline.STOP.clear()
    return {"ok": True}


class ApproveIn(BaseModel):
    save_now: bool = False
    make: list[int] | None = None       # shots the button says it will make (from the screen)


@app.post("/api/products/{pid}/approve")
def approve(pid: int, body: ApproveIn | None = None, u=Depends(need("editor"))):
    body = body or ApproveIn()
    with SessionLocal() as s:
        p = _get(s, pid)
        if not any(sh.image_path for sh in p.shots):
            raise HTTPException(400, "Nothing generated yet")
        missing = [sh.no for sh in p.shots if not sh.image_path]
        if (p.data or {}).get("v2"):
            from .v2 import requested_shots
            have = {sh.no for sh in p.shots if sh.image_path}
            missing = [n for n in requested_shots(s, p) if n not in have]
        if body.make:
            have = {sh.no for sh in p.shots if sh.image_path}
            missing = sorted({int(n) for n in body.make if 1 <= int(n) <= 5 and int(n) not in have})
        if missing and not body.save_now:
            # approve what exists, then make the remaining shots; the product is saved once they are done
            for sh in p.shots:
                if sh.image_path:
                    sh.status = "approved"
            p.data = {**(p.data or {}), "run_now": True, "only_shots": missing}
            p.stage, p.error = "queue", ""
            log(s, u.email, f"Approved {p.code} so far, generating shots {missing}")
            s.commit()
            pipeline.STOP.clear()
            return {"ok": True, "generating": missing}
        pipeline.approve(s, p, u.email)
    return {"ok": True}


@app.post("/api/stop")
def stop(u=Depends(need("editor"))):
    """Stop button: autopilot off, cancel waiting jobs, current product stops before its next image."""
    pipeline.STOP.set()
    with SessionLocal() as s:
        set_setting(s, "autopilot", False)
        for p in s.scalars(select(Product).where(Product.stage.in_(["queue", "gen"]))):
            if (p.data or {}).get("run_now"):
                p.data = {k: v for k, v in p.data.items() if k not in ("run_now", "only_shots")}
        log(s, u.email, "Stopped all generation")
        s.commit()
    return {"ok": True}


@app.delete("/api/products/{pid}")
def remove(pid: int, u=Depends(need("editor"))):
    with SessionLocal() as s:
        p = _get(s, pid)
        if p.stage == "gen":
            raise HTTPException(400, "This product is generating. Press Stop first, then remove it.")
        log(s, u.email, f"Removed {p.code} from the queue")
        s.delete(p)
        s.commit()
    return {"ok": True}


class RegenIn(BaseModel):
    shot: int
    note: str = ""


@app.post("/api/products/{pid}/regenerate")
def regenerate(pid: int, body: RegenIn, u=Depends(need("editor"))):
    with SessionLocal() as s:
        p = _get(s, pid)
        for sh in p.shots:
            if sh.no == body.shot:
                sh.status, sh.note = "wait", body.note.strip()[:500]
                scene = ((sh.qc or {}).get("v2") or {}).get("scene")
                if scene and "pool" in ((sh.qc or {}).get("v2") or {}).get("how", ""):
                    from .v2 import retire_scene
                    retire_scene(s, scene)          # you didn't like it: never reuse it for other products
        p.data = {**(p.data or {}), "run_now": True, "only_shots": [body.shot]}
        p.stage = "queue"
        log(s, u.email, f"Regenerate {p.code} shot {body.shot}" + (f": {body.note[:80]}" if body.note else ""))
        s.commit()
    pipeline.STOP.clear()
    return {"ok": True}


@app.post("/api/products/{pid}/reject")
def reject(pid: int, u=Depends(need("editor"))):
    with SessionLocal() as s:
        p = _get(s, pid)
        p.stage = "rejected"
        log(s, u.email, f"Rejected {p.code}")
        s.commit()
    return {"ok": True}


# ---------------------------------------------------------------- admin: rules
def _rv(r: RuleVersion) -> dict:
    return {"id": r.id, "version": r.version, "status": r.status, "data": r.data, "created_by": r.created_by,
            "approved_by": r.approved_by, "created": r.created.isoformat()}


@app.get("/api/admin/rules")
def rules(u=Depends(need("admin"))):
    with SessionLocal() as s:
        rows = list(s.scalars(select(RuleVersion).order_by(RuleVersion.version.desc())))
        live = next((r for r in rows if r.status == "live"), None)
        draft = next((r for r in rows if r.status in ("draft", "pending")), None)
        return {"live": _rv(live), "draft": _rv(draft) if draft else None,
                "history": [{"version": r.version, "status": r.status, "by": r.created_by, "approved_by": r.approved_by,
                             "created": r.created.isoformat()} for r in rows]}


class DraftIn(BaseModel):
    data: dict


@app.put("/api/admin/rules/draft")
def save_draft(body: DraftIn, u=Depends(need("admin"))):
    with SessionLocal() as s:
        draft = s.scalars(select(RuleVersion).where(RuleVersion.status.in_(["draft", "pending"]))).first()
        if not draft:
            top = s.scalar(select(func.max(RuleVersion.version)))
            draft = RuleVersion(version=(top or 0) + 1, status="draft", data=live_rules(s).data, created_by=u.email)
            s.add(draft)
        merged = {**draft.data, **body.data}
        draft.data, draft.status, draft.created_by = merged, "draft", u.email
        s.commit()
        return _rv(draft)


@app.post("/api/admin/rules/submit")
def submit(u=Depends(need("admin"))):
    with SessionLocal() as s:
        d = s.scalars(select(RuleVersion).where(RuleVersion.status == "draft")).first()
        if not d:
            raise HTTPException(400, "No draft to send")
        d.status = "pending"
        log(s, u.email, f"Sent rules v{d.version} for approval")
        s.commit()
    return {"ok": True}


@app.post("/api/admin/rules/approve")
def approve_rules(u=Depends(need("admin"))):
    with SessionLocal() as s:
        d = s.scalars(select(RuleVersion).where(RuleVersion.status == "pending")).first()
        if not d:
            raise HTTPException(400, "Nothing waiting for approval")
        admins = s.scalar(select(func.count()).select_from(User).where(User.role.in_(["admin", "owner"]), User.active == True))  # noqa
        if d.created_by == u.email and admins > 1:
            raise HTTPException(403, "A second admin must approve this change")
        for r in s.scalars(select(RuleVersion).where(RuleVersion.status == "live")):
            r.status = "archived"
        d.status, d.approved_by = "live", u.email
        log(s, u.email, f"Rules v{d.version} are live")
        s.commit()
    return {"ok": True}


@app.post("/api/admin/rules/rollback/{version}")
def rollback(version: int, u=Depends(need("admin"))):
    with SessionLocal() as s:
        old = s.scalars(select(RuleVersion).where(RuleVersion.version == version)).first()
        if not old:
            raise HTTPException(404, "Version not found")
        top = s.scalar(select(func.max(RuleVersion.version)))
        s.add(RuleVersion(version=top + 1, status="draft", data=old.data, created_by=u.email))
        s.commit()
    return {"ok": True}


@app.get("/api/admin/rules/preview")
def preview(shot: int = 2, product_id: int | None = None, draft: bool = True, u=Depends(need("admin"))):
    with SessionLocal() as s:
        rv = (s.scalars(select(RuleVersion).where(RuleVersion.status.in_(["draft", "pending"]))).first() if draft else None) or live_rules(s)
        p = s.get(Product, product_id) if product_id else s.scalars(select(Product).order_by(Product.id.desc())).first()
        if not p:
            return {"prompt": "Add a product first to preview its prompt."}
        out = build_prompts(s, p, p.analysis or {}, rv)
        return {"product": p.code, "version": rv.version, "prompt": out[max(0, min(4, shot - 1))]["prompt"]}


# ---------------------------------------------------------------- admin: library
@app.get("/api/admin/library/{table}")
def lib_list(table: str, u=Depends(need("admin"))):
    if table not in LIB_TABLES:
        raise HTTPException(404)
    with SessionLocal() as s:
        return [r.data for r in s.scalars(select(LibraryRow).where(LibraryRow.table == table).order_by(LibraryRow.key))]


@app.put("/api/admin/library/{table}/{key}")
def lib_put(table: str, key: str, row: dict, u=Depends(need("admin"))):
    if table not in LIB_TABLES:
        raise HTTPException(404)
    row[LIB_TABLES[table]] = key
    with SessionLocal() as s:
        r = s.scalars(select(LibraryRow).where(LibraryRow.table == table, LibraryRow.key == key)).first()
        if r:
            r.data = {**r.data, **row}
        else:
            s.add(LibraryRow(table=table, key=key, data=row))
        log(s, u.email, f"Saved {table[:-1]} {key}")
        s.commit()
    return {"ok": True}


@app.delete("/api/admin/library/{table}/{key}")
def lib_del(table: str, key: str, u=Depends(need("admin"))):
    with SessionLocal() as s:
        r = s.scalars(select(LibraryRow).where(LibraryRow.table == table, LibraryRow.key == key)).first()
        if r:
            s.delete(r)
            log(s, u.email, f"Deleted {table[:-1]} {key}")
            s.commit()
    return {"ok": True}


# ---------------------------------------------------------------- admin: keys, settings, tests
@app.get("/api/admin/keys")
def keys(u=Depends(need("admin"))):
    return secrets.masked()


class KeyIn(BaseModel):
    name: str
    value: str


@app.put("/api/admin/keys")
def put_key(body: KeyIn, u=Depends(need("owner"))):
    if body.name not in secrets.KNOWN:
        raise HTTPException(400, "Unknown key")
    secrets.set_secret(body.name, body.value.strip())
    with SessionLocal() as s:
        log(s, u.email, f"Updated {body.name}")
        s.commit()
    return {"ok": True}


@app.post("/api/admin/test/{what}")
def test_conn(what: str, u=Depends(need("admin"))):
    try:
        if what == "gemini":
            from .providers import get_provider
            return {"ok": True, "message": get_provider().test()}
        if what == "google":
            from .connectors import google
            return {"ok": True, "message": google.test()}
        if what == "shopify":
            from .connectors import shopify
            return {"ok": True, "message": shopify.test()}
    except Exception as e:
        return {"ok": False, "message": str(e)[:400]}
    raise HTTPException(404)


SETTING_KEYS = ["library_focus", "library_drive_copy", "library_cutouts", "pipeline_mode", "scene_strategy", "scene_reuse", "fresh_brands", "auto_approve", "retries", "publish_shopify", "budget_month", "budget_action", "model_hero_detail",
                "model_other", "file_naming", "min_raw_px", "output_drive_folder", "cost_mode", "default_shots",
                "max_cost_product", "daily_budget", "hero_gate", "qc_mode", "usd_inr"]


@app.get("/api/admin/settings")
def get_settings(u=Depends(need("admin"))):
    with SessionLocal() as s:
        return {k: get_setting(s, k) for k in SETTING_KEYS}


@app.put("/api/admin/settings")
def put_settings(body: dict, u=Depends(need("admin"))):
    with SessionLocal() as s:
        for k, v in body.items():
            if k in SETTING_KEYS:
                set_setting(s, k, v)
        log(s, u.email, "Updated settings: " + ", ".join(k for k in body if k in SETTING_KEYS))
        s.commit()
    return {"ok": True}


# ---------------------------------------------------------------- admin: repositories
class RepoIn(BaseModel):
    use: str
    type: str
    name: str
    path: str
    watch: bool = True


@app.get("/api/admin/repositories")
def repos(u=Depends(need("admin"))):
    with SessionLocal() as s:
        return [{"id": r.id, "use": r.use, "type": r.type, "name": r.name, "path": r.path, "watch": r.watch, "status": r.status}
                for r in s.scalars(select(Repository).order_by(Repository.id))]


@app.post("/api/admin/repositories")
def add_repo(body: RepoIn, u=Depends(need("admin"))):
    with SessionLocal() as s:
        r = Repository(**body.model_dump())
        s.add(r)
        if body.use == "Saved output" and body.type == "Google Drive":
            set_setting(s, "output_drive_folder", body.path)
        if body.use.startswith("Input") and body.type in ("Google Drive", "Google Sheet"):
            t = "drive" if body.type == "Google Drive" else "sheet"
            s.add(Source(type=t, name=body.name, detail=body.path[:200], config={"link": body.path}, watch=body.watch))
        r.status = "ok"
        log(s, u.email, f"Added repository {body.name}")
        s.commit()
        return {"ok": True, "id": r.id}


@app.delete("/api/admin/repositories/{rid}")
def del_repo(rid: int, u=Depends(need("admin"))):
    with SessionLocal() as s:
        r = s.get(Repository, rid)
        if r:
            s.delete(r)
            log(s, u.email, f"Removed repository {r.name}")
            s.commit()
    return {"ok": True}


# ---------------------------------------------------------------- admin: team, usage, activity
class InviteIn(BaseModel):
    email: str
    role: str = "editor"


@app.get("/api/admin/team")
def team(u=Depends(need("admin"))):
    with SessionLocal() as s:
        return [{"id": x.id, "email": x.email, "role": x.role, "active": x.active, "pending": bool(x.invite_token)}
                for x in s.scalars(select(User).order_by(User.id))]


@app.post("/api/admin/team")
def invite(body: InviteIn, request: Request, u=Depends(need("admin"))):
    if body.role not in ("admin", "editor", "viewer"):
        raise HTTPException(400, "Role must be admin, editor or viewer")
    email = body.email.strip().lower()
    with SessionLocal() as s:
        x = s.scalars(select(User).where(User.email == email)).first() or User(email=email)
        x.role, x.invite_token, x.active = body.role, new_token(), False
        s.add(x)
        log(s, u.email, f"Invited {email} as {body.role}")
        s.commit()
        link = str(request.base_url).rstrip("/") + f"/login?invite={x.invite_token}"
    return {"ok": True, "link": link}


@app.patch("/api/admin/team/{uid}")
def set_role(uid: int, body: dict, u=Depends(need("admin"))):
    with SessionLocal() as s:
        x = s.get(User, uid)
        if not x or x.role == "owner":
            raise HTTPException(400, "The owner cannot be changed")
        if body.get("role") in ("admin", "editor", "viewer"):
            x.role = body["role"]
        if body.get("remove"):
            x.active, x.invite_token = False, ""
        log(s, u.email, f"Updated {x.email}")
        s.commit()
    return {"ok": True}


@app.get("/api/admin/usage")
def usage(u=Depends(need("admin"))):
    with SessionLocal() as s:
        start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        by_model = s.execute(select(Usage.model, func.sum(Usage.cost), func.count()).where(Usage.ts >= start)
                             .group_by(Usage.model)).all()
        images = s.scalar(select(func.count()).select_from(Usage).where(Usage.ts >= start, Usage.kind == "image")) or 0
        saved = s.scalar(select(func.count()).select_from(Product).where(Product.stage == "saved")) or 0
        shots = list(s.scalars(select(Shot).where(Shot.image_path != "")))
        first_pass = sum(1 for sh in shots if sh.attempts == 1 and sh.status in ("pass", "approved"))
        spend = pipeline.month_spend(s)
        return {"usd_inr": get_setting(s, "usd_inr", 88.0), "spend_month": round(spend, 2), "budget": get_setting(s, "budget_month", 150), "images": images,
                "saved_products": saved, "cost_per_product": round(spend / saved, 2) if saved else 0,
                "first_pass_rate": round(100 * first_pass / len(shots)) if shots else 0,
                "by_model": [{"model": m, "cost": round(c or 0, 2), "calls": n} for m, c, n in by_model]}


@app.get("/api/admin/activity")
def activity(u=Depends(need("admin"))):
    with SessionLocal() as s:
        return [{"ts": a.ts.isoformat(), "who": a.who, "what": a.what}
                for a in s.scalars(select(Activity).order_by(Activity.id.desc()).limit(200))]


# ---------------------------------------------------------------- v2 · Product Library
from . import library  # noqa: E402
from .db import LibImage  # noqa: E402
from fastapi.responses import PlainTextResponse  # noqa: E402

_lib_job = {"running": False, "msg": "", "error": ""}


@app.get("/vendor/{name}")
def vendor(name: str):
    p = (config.WEB_DIR / "vendor" / name).resolve()
    if p.parent != (config.WEB_DIR / "vendor").resolve() or not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="text/javascript" if name.endswith(".mjs") else None,
                        headers={"Cache-Control": "public, max-age=604800"})


@app.get("/library")
def library_page(u=Depends(need("editor"))):
    return FileResponse(config.WEB_DIR / "library.html")


def _bg(name: str, fn):
    """Long jobs (Drive scans, big catalogues) run in the background so the page never times out."""
    if _lib_job["running"]:
        raise HTTPException(409, "Another library job is still running. Wait for it to finish.")
    _lib_job.update(running=True, msg=name + "…", error="")

    def run():
        try:
            with SessionLocal() as s:
                r = fn(s)
            _lib_job["msg"] = f"{name}: done · {r}"
        except Exception as e:
            _lib_job["error"] = f"{name} failed: {str(e)[:300]}"
        finally:
            _lib_job["running"] = False
    threading.Thread(target=run, daemon=True).start()


@app.get("/api/library")
def library_state(u=Depends(need("viewer"))):
    with SessionLocal() as s:
        r = library.report(s)
        r["job"] = dict(_lib_job)
        r["drive_link"] = get_setting(s, "library_drive_link", "")
        r["cutouts_folder"] = bool(get_setting(s, "cutouts_drive_folder", ""))
        return r


@app.post("/api/library/upload")
async def library_upload(files: list[UploadFile] = File(...), u=Depends(need("editor"))):
    payload = [(f.filename or "file", await f.read()) for f in files]
    _bg(f"Upload of {len(payload)} file(s)", lambda s: library.add_files(s, payload))
    return {"ok": True}


class LinkIn(BaseModel):
    link: str


@app.post("/api/library/scan")
def library_scan(body: LinkIn, u=Depends(need("editor"))):
    _bg("Drive scan", lambda s: library.scan_drive(s, body.link))
    return {"ok": True}


@app.get("/api/library/pending")
def library_pending(u=Depends(need("editor"))):
    with SessionLocal() as s:
        return [{"id": i.id, "key": i.key, "src": f"/files/{i.path}"} for i in library.pending(s)]


@app.post("/api/library/images/{iid}/cutout")
async def library_cutout(iid: int, file: UploadFile = File(...), u=Depends(need("editor"))):
    data = await file.read()
    with SessionLocal() as s:
        img = s.get(LibImage, iid)
        if not img:
            raise HTTPException(404, "Image not found")
        library.save_cutout(s, img, data)
        s.commit()
        return {"ok": True, "status": img.cut_status, "note": img.cut_note}


class ImgAct(BaseModel):
    action: str          # ok | redo | skip | primary
    code: str = ""


@app.post("/api/library/images/{iid}")
def library_image(iid: int, body: ImgAct, u=Depends(need("editor"))):
    with SessionLocal() as s:
        img = s.get(LibImage, iid)
        if not img:
            raise HTTPException(404, "Image not found")
        if body.action == "ok":
            img.cut_status = "ok"
        elif body.action == "redo":
            img.cut_status, img.cut_path, img.cut_note = "pending", "", ""
        elif body.action == "skip":
            img.cut_status = "skip"
            if not library.cutouts_on(s):
                img.primary = False
                library.pick_primary(s, img.key)        # next-best photo of the same product takes over
        elif body.action == "primary":
            library.pick_primary(s, img.key, force_id=img.id)
        elif body.action == "assign":
            try:
                key = library.assign(s, iid, body.code)
            except ValueError as e:
                raise HTTPException(400, str(e))
            log(s, u.email, f"Library: image {iid} assigned to {key}")
        elif body.action == "discard":
            img.key, img.cut_status, img.primary = "", "skip", False
            img.dup_of = img.dup_of or -1          # hidden from the tray
        else:
            raise HTTPException(400, "Unknown action")
        s.commit()
    return {"ok": True}


@app.get("/api/library/report.csv")
def library_csv(u=Depends(need("viewer"))):
    with SessionLocal() as s:
        return PlainTextResponse(library.report_csv(s), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=library-report.csv"})


@app.post("/api/library/clear")
def library_clear(u=Depends(need("admin"))):
    with SessionLocal() as s:
        library.clear(s)
        log(s, u.email, "Library images cleared")
        s.commit()
    return {"ok": True}



# ---------------------------------------------------------------- v2 · studio queue from the shoot sheet
@app.post("/api/v2/load-shoot")
def v2_load(u=Depends(need("editor"))):
    from . import v2
    with SessionLocal() as s:
        try:
            r = v2.load_shoot_sheet(s)
        except ValueError as e:
            raise HTTPException(400, str(e))
        log(s, u.email, f"Loaded shoot sheet into the queue ({r['total']} codes)")
        s.commit()
    pipeline.STOP.clear()
    return {"ok": True, **r}


class MoveIn(BaseModel):
    to: str = "up"          # up | down | top | bottom


@app.post("/api/products/{pid}/move")
def move(pid: int, body: MoveIn, u=Depends(need("editor"))):
    with SessionLocal() as s:
        p = _get(s, pid)
        v2flag = bool((p.data or {}).get("v2"))
        q = [x for x in s.scalars(select(Product).where(Product.stage.in_(["queue", "gen", "review", "error", "needs_photo"])))
             if bool((x.data or {}).get("v2")) == v2flag]
        q.sort(key=lambda x: ((x.data or {}).get("priority", 10_000), x.id))
        i = q.index(p)
        j = {"up": max(0, i - 1), "down": min(len(q) - 1, i + 1), "top": 0, "bottom": len(q) - 1}.get(body.to, i)
        q.insert(j, q.pop(i))
        for n, x in enumerate(q, 1):
            x.data = {**(x.data or {}), "priority": n}
        s.commit()
    return {"ok": True}


class PauseIn(BaseModel):
    paused: bool


@app.post("/api/products/{pid}/pause")
def pause(pid: int, body: PauseIn, u=Depends(need("editor"))):
    with SessionLocal() as s:
        p = _get(s, pid)
        p.data = {**(p.data or {}), "paused": body.paused}
        log(s, u.email, f"{'Paused' if body.paused else 'Resumed'} {p.code}")
        s.commit()
    return {"ok": True}
