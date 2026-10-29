"""End-to-end run with the mock AI provider: upload → autopilot → 5 shots → QC → saved. Run: python -m pytest -q"""
import os
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ.update({"AI_PROVIDER": "mock", "DATABASE_URL": os.environ.get("TEST_DATABASE_URL", f"sqlite:///{TMP}/t.db"), "STORAGE_DIR": f"{TMP}/store",
                   "RUN_WORKER": "false", "COOKIE_SECURE": "false", "OWNER_EMAIL": "owner@test.com",
                   "OWNER_PASSWORD": "pw-for-tests", "SECRET_KEY": "test", "SKIP_PRESETS": "1"})

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app import pipeline  # noqa: E402

SAMPLES = Path(__file__).parent / "samples"


def client():
    c = TestClient(app)
    c.__enter__()
    r = c.post("/api/login", json={"email": "owner@test.com", "password": "pw-for-tests"})
    assert r.status_code == 200, r.text
    c.post("/api/autopilot", json={"pipeline_mode": "v1"})   # legacy 'add input' flow; shoot-sheet tests switch to v2
    return c


def wait_job(c, secs=60):
    import time
    for _ in range(int(secs / 0.1)):
        st = c.get("/api/library").json()
        if not st["job"]["running"]:
            return st
        time.sleep(0.1)
    raise AssertionError("library job did not finish")


def test_full_flow():
    c = client()
    assert c.get("/api/health").json()["ok"]
    files = [("files", (p.name, p.read_bytes(), "image/jpeg")) for p in sorted(SAMPLES.iterdir())]
    r = c.post("/api/sources/upload", files=files, data={"brand": "detect"})
    assert r.json()["added"] == 4, r.text
    st = c.get("/api/state").json()
    codes = {q["code"] for q in st["queue"]}
    assert codes == {"WAT-HEM-SSGT", "CAM-AOC-12", "PEB-OIL-SEL10", "FUJ-MINI-EVO"}
    brands = {q["code"]: q["brand"] for q in st["queue"]}
    assert brands["WAT-HEM-SSGT"] == "waterman" and brands["CAM-AOC-12"] == "camel"

    c.put("/api/admin/settings", json={"default_shots": [1, 2, 3, 4], "max_cost_product": 5, "daily_budget": 100,
                                       "retries": 2, "hero_gate": False, "qc_mode": "full"})
    c.post("/api/autopilot", json={"autopilot": True, "auto_approve": True})
    for _ in range(10):
        if not pipeline.tick():
            break
    st = c.get("/api/state").json()
    done = {q["code"]: q["stage"] for q in st["queue"] + st["saved"]}
    assert all(v in ("saved", "review") for v in done.values()), done
    saved = [q for q in st["saved"]]
    assert saved, "autopilot should save products whose QC passes"

    p = c.get(f"/api/products/{saved[0]['id']}").json()
    assert len(p["shots"]) == 4 and all(s["image"] for s in p["shots"])
    img = c.get(p["shots"][0]["image"])
    assert img.status_code == 200 and img.content[:2] == b"\xff\xd8"
    from PIL import Image
    import io
    assert Image.open(io.BytesIO(img.content)).size == (1200, 1500)
    assert "STRICTLY SOFT" in pipeline.SessionLocal().get(pipeline.Shot, 1).prompt

    # regenerate one shot with a note, then approve
    pid = saved[0]["id"]
    c.post("/api/autopilot", json={"autopilot": False})   # manual review mode
    c.post(f"/api/products/{pid}/regenerate", json={"shot": 3, "note": "warmer walnut"})
    assert pipeline.tick()
    p = c.get(f"/api/products/{pid}").json()
    assert p["stage"] == "review"
    assert c.post(f"/api/products/{pid}/approve").json()["ok"]
    assert c.get(f"/api/products/{pid}").json()["stage"] == "saved"


def test_admin():
    c = client()
    rules = c.get("/api/admin/rules").json()
    assert rules["live"]["version"] == 1
    c.put("/api/admin/rules/draft", json={"data": {"strength": {"lighting": "normal"}}})
    pv = c.get("/api/admin/rules/preview?shot=2").json()
    assert "Soft diffused light" in pv["prompt"]
    assert c.post("/api/admin/rules/submit").json()["ok"]
    assert c.post("/api/admin/rules/approve").json()["ok"]   # single admin may approve their own change
    assert c.get("/api/admin/rules").json()["live"]["version"] == 2
    assert len(c.get("/api/admin/library/brands").json()) >= 4
    c.put("/api/admin/library/brands/lamy", json={"brand_name": "LAMY", "detect_keywords": "lamy"})
    assert any(b["brand_id"] == "lamy" for b in c.get("/api/admin/library/brands").json())
    inv = c.post("/api/admin/team", json={"email": "two@test.com", "role": "admin"}).json()
    tok = inv["link"].split("invite=")[1]
    c2 = TestClient(app)
    assert c2.post("/api/invite/accept", json={"token": tok, "password": "long-password-1"}).json()["ok"]
    assert c2.get("/api/me").json()["role"] == "admin"
    assert c.post("/api/admin/repositories", json={"use": "Saved output", "type": "Google Drive", "name": "Final",
                                                    "path": "https://drive.google.com/drive/folders/abc"}).json()["ok"]
    assert c.get("/api/admin/settings").json()["output_drive_folder"].endswith("abc")
    u = c.get("/api/admin/usage").json()
    assert u["images"] >= 5 and u["spend_month"] > 0
    assert c.post("/api/admin/test/gemini").json()["ok"]
    c.put("/api/admin/keys", json={"name": "GEMINI_API_KEY", "value": "secret-value-123456"})
    k = {x["name"]: x for x in c.get("/api/admin/keys").json()}
    assert k["GEMINI_API_KEY"]["set"] and "secret" not in k["GEMINI_API_KEY"]["masked"]


def test_roles():
    c = TestClient(app)
    assert c.get("/api/state").status_code == 401
    assert c.get("/admin.html", follow_redirects=False).status_code in (302, 307)


def test_review_when_qc_fails():
    """A product with a failing shot must land in review, never be auto-saved."""
    from app.providers import mock
    orig = mock.MockProvider.qc
    mock.MockProvider.qc = lambda self, raw, gen, shot, rules: (
        {"checks": {**{k: True for k in mock.QC_CHECKS}, "label_text": shot["no"] != 3}, "reasons": {"label_text": "x"}})
    try:
        c = client()
        p = (SAMPLES / "CAM-AOC-12_front.png")
        c.post("/api/sources/upload", files=[("files", ("NEW-CODE-1_front.png", p.read_bytes(), "image/png"))])
        c.post("/api/autopilot", json={"autopilot": True})
        while pipeline.tick():
            pass
        st = c.get("/api/state").json()
        item = next(q for q in st["queue"] if q["code"] == "NEW-CODE-1")
        assert item["stage"] == "review"
        prod = c.get(f"/api/products/{item['id']}").json()
        assert prod["shots"][2]["status"] == "fail" and prod["shots"][2]["attempts"] == 3
    finally:
        mock.MockProvider.qc = orig



def test_cost_controls():
    """Defaults: hero only, no retries, hero gate, per-product cap."""
    c = client()
    c.put("/api/admin/settings", json={"default_shots": [1], "retries": 0, "hero_gate": True, "qc_mode": "light",
                                       "max_cost_product": 0.30, "daily_budget": 100, "cost_mode": "economy"})
    p = SAMPLES / "PEB-OIL-SEL10_front.jpg"
    c.post("/api/sources/upload", files=[("files", ("COST-TEST-1_front.jpg", p.read_bytes(), "image/jpeg"))])
    pid = next(q["id"] for q in c.get("/api/state").json()["queue"] if q["code"] == "COST-TEST-1")
    c.post(f"/api/products/{pid}/run")
    pipeline.tick()
    prod = c.get(f"/api/products/{pid}").json()
    made = [s for s in prod["shots"] if s["image"]]
    assert len(made) == 1 and made[0]["attempts"] == 1 and "lite" in made[0]["model"]
    assert prod["cost"] < 0.05
    # on-demand: generate shot 5 only
    c.post(f"/api/products/{pid}/regenerate", json={"shot": 4})
    pipeline.tick()
    prod = c.get(f"/api/products/{pid}").json()
    assert [s["no"] for s in prod["shots"] if s["image"]] == [1, 4]
    # cap: a tiny limit stops generation before spending
    c.put("/api/admin/settings", json={"max_cost_product": 0.001})
    c.post(f"/api/products/{pid}/regenerate", json={"shot": 2})
    pipeline.tick()
    prod = c.get(f"/api/products/{pid}").json()
    assert not prod["shots"][1]["image"] and "cost limit" in prod["error"]


def test_vision_and_qc_failure_do_not_block():
    """If the vision model or QC is refused, the shot is still generated and sent to review."""
    from app.providers import mock
    oa, oq = mock.MockProvider.analyse, mock.MockProvider.qc
    def boom(*a, **k):
        raise RuntimeError("400 INVALID_ARGUMENT models/gemini-x is not supported")
    mock.MockProvider.analyse, mock.MockProvider.qc = boom, boom
    try:
        c = client()
        c.put("/api/admin/settings", json={"default_shots": [1], "max_cost_product": 5, "daily_budget": 100})
        p = SAMPLES / "FUJ-MINI-EVO_front.jpg"
        c.post("/api/sources/upload", files=[("files", ("VIS-FAIL-1_front.jpg", p.read_bytes(), "image/jpeg"))])
        pid = next(q["id"] for q in c.get("/api/state").json()["queue"] if q["code"] == "VIS-FAIL-1")
        c.post(f"/api/products/{pid}/run")
        pipeline.tick()
        prod = c.get(f"/api/products/{pid}").json()
        assert prod["shots"][0]["image"] and prod["shots"][0]["status"] == "unchecked"
        assert prod["stage"] == "review"
    finally:
        mock.MockProvider.analyse, mock.MockProvider.qc = oa, oq


def test_approve_hero_then_rest_stop_and_remove():
    """Approving a hero-only product makes the other shots; Stop halts work; Remove deletes from the queue."""
    c = client()
    c.put("/api/admin/settings", json={"default_shots": [1], "max_cost_product": 5, "daily_budget": 100,
                                       "hero_gate": True, "qc_mode": "light"})
    c.post("/api/autopilot", json={"autopilot": False})
    p = SAMPLES / "WAT-HEM-SSGT_front.png"
    c.post("/api/sources/upload", files=[("files", ("FLOW-TEST-1_front.jpg", p.read_bytes(), "image/jpeg")),
                                         ("files", ("FLOW-TEST-2_front.jpg", p.read_bytes(), "image/jpeg"))])
    q = {x["code"]: x["id"] for x in c.get("/api/state").json()["queue"]}
    pid = q["FLOW-TEST-1"]
    c.post(f"/api/products/{pid}/run"); pipeline.tick()
    assert c.get(f"/api/products/{pid}").json()["stage"] == "review"
    r = c.post(f"/api/products/{pid}/approve", json={}).json()
    assert r["generating"] == [2, 3, 4]
    pipeline.tick()
    prod = c.get(f"/api/products/{pid}").json()
    assert all(s["image"] for s in prod["shots"]) and prod["stage"] == "review"
    assert c.post(f"/api/products/{pid}/approve", json={}).json() == {"ok": True}
    assert c.get(f"/api/products/{pid}").json()["stage"] == "saved"
    # stop: queued run is cancelled and nothing runs
    pid2 = q["FLOW-TEST-2"]
    c.post(f"/api/products/{pid2}/run")
    c.post("/api/stop")
    assert not pipeline.tick()
    assert c.get("/api/state").json()["autopilot"]["autopilot"] is False
    # remove from queue
    assert c.delete(f"/api/products/{pid2}").json()["ok"]
    assert all(x["code"] != "FLOW-TEST-2" for x in c.get("/api/state").json()["queue"])
    c.post("/api/autopilot", json={"autopilot": False})
    pipeline.STOP.clear()


def test_library_phase1():
    """Master + shoot sheet + photos: matched by code, duplicates dropped, cut-out saved and checked, tray assign, report."""
    import io, time
    from PIL import Image
    c = client()
    c.put("/api/admin/settings", json={"library_cutouts": True})
    LIB = Path(__file__).parent / "library"
    def png(color, size=(900, 900), alpha=False):
        im = Image.new("RGBA" if alpha else "RGB", size, (255, 255, 255, 0) if alpha else (255, 255, 255))
        from PIL import ImageDraw
        w, h = size
        ImageDraw.Draw(im).rectangle([w * 2 // 9, h * 5 // 18, w * 7 // 9, h * 13 // 18], fill=color + ((255,) if alpha else ()))
        b = io.BytesIO(); im.save(b, "PNG"); return b.getvalue()
    files = [("files", ("master.xlsx", (LIB / "master.xlsx").read_bytes(), "application/octet-stream")),
             ("files", ("shoot.xlsx", (LIB / "shoot.xlsx").read_bytes(), "application/octet-stream")),
             ("files", ("TRV20-GY_1.png", png((20, 20, 20)), "image/png")),
             ("files", ("TRV20-GY_1 copy.png", png((20, 20, 20), (600, 600)), "image/png")),   # duplicate, smaller
             ("files", ("PEN20 SI front.png", png((150, 150, 150)), "image/png")),
             ("files", ("IMG_0042.png", png((90, 30, 30)), "image/png"))]                    # no code -> tray
    assert c.post("/api/library/upload", files=files).json()["ok"]
    for _ in range(100):
        st = c.get("/api/library").json()
        if not st["job"]["running"]:
            break
        time.sleep(0.1)
    assert not st["job"]["error"], st["job"]
    rows = {r["code"]: r for r in st["rows"]}
    assert st["shoot"] == 13
    assert rows["TRV20/GY"]["images"] == 1 and rows["TRV20/GY"]["dups"] == 1
    assert rows["TRV20/GY"]["primary"]["w"] == 900
    assert rows["PEN20/SI"]["status"] == "cutting" and rows["PIP25/BL"]["status"] == "needs_photo"
    assert len(st["unmatched"]) == 1
    pend = c.get("/api/library/pending").json()
    assert {p["key"] for p in pend} == {"TRV20GY", "PEN20SI"}
    gid = next(p["id"] for p in pend if p["key"] == "TRV20GY")
    r = c.post(f"/api/library/images/{gid}/cutout", files={"file": ("x.png", png((20, 20, 20), alpha=True), "image/png")}).json()
    assert r["status"] == "ok", r
    assert c.get("/api/library").json()["counts"]["ready"] == 1
    # tray: assign the unknown photo to PIP25/BL
    uid = st["unmatched"][0]["id"]
    assert c.post(f"/api/library/images/{uid}", json={"action": "assign", "code": "pip25-bl"}).json()["ok"]
    rows = {r["code"]: r for r in c.get("/api/library").json()["rows"]}
    assert rows["PIP25/BL"]["status"] == "cutting"
    csv = c.get("/api/library/report.csv").text
    assert "TRV20/GY" in csv and "ready" in csv
    assert c.get("/library").status_code == 200


def test_v2_shoot_sheet_only():
    """v2: only shoot-sheet codes are queued; only requested shots are made; pool scenes reused; queue editable."""
    import io, time, openpyxl
    from PIL import Image, ImageDraw
    c = client()
    c.put("/api/admin/settings", json={"library_cutouts": True})
    LIB = Path(__file__).parent / "library"
    def photo(color):
        im = Image.new("RGB", (1000, 1000), (255, 255, 255))
        ImageDraw.Draw(im).rectangle([250, 300, 750, 700], fill=color)
        b = io.BytesIO(); im.save(b, "PNG"); return b.getvalue()
    def cut(color):
        im = Image.new("RGBA", (1000, 1000), (0, 0, 0, 0))
        ImageDraw.Draw(im).rectangle([250, 300, 750, 700], fill=color + (255,))
        b = io.BytesIO(); im.save(b, "PNG"); return b.getvalue()
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["SHOOT", "SHOTS", "PRIORITY", "NOTE"])
    ws.append(["TRV20/GY", "1,3", 2, ""]); ws.append(["TRV20/BK", "1,3", 1, "black first"]); ws.append(["PEN20/SI", "", 3, ""])
    ws.append(["NOT-A-CODE", "", 4, ""])
    b = io.BytesIO(); wb.save(b)
    c.post("/api/library/upload", files=[("files", ("master.xlsx", (LIB / "master.xlsx").read_bytes(), "x/y")),
                                         ("files", ("v2shoot.xlsx", b.getvalue(), "x/y")),
                                         ("files", ("TRV20-GY.png", photo((90, 90, 100)), "image/png")),
                                         ("files", ("TRV20-BK.png", photo((10, 10, 10)), "image/png"))])
    for _ in range(100):
        if not c.get("/api/library").json()["job"]["running"]:
            break
        time.sleep(0.1)
    for p in c.get("/api/library/pending").json():
        if p["key"] in ("TRV20GY", "TRV20BK"):
            c.post(f"/api/library/images/{p['id']}/cutout", files={"file": ("c.png", cut((50, 50, 50)), "image/png")})
    c.put("/api/admin/settings", json={"scene_strategy": "auto", "max_cost_product": 5, "daily_budget": 100,
                                       "default_shots": [1, 2, 3, 4]})
    c.post("/api/autopilot", json={"pipeline_mode": "v2", "autopilot": False})
    r = c.post("/api/v2/load-shoot").json()
    assert r["total"] == 4 and r["missing"] == 1 and r["waiting"] == 1, r
    st = c.get("/api/state").json()
    assert st["pipeline_mode"] == "v2"
    codes = [q["code"] for q in st["queue"]]
    assert codes[:2] == ["TRV20/BK", "TRV20/GY"] and len(codes) == 4, codes        # sheet priority order, nothing else
    assert not any(q["code"].startswith(("WAT", "CAM", "FLOW", "COST")) for q in st["queue"])  # v1 items hidden
    # editable queue: move GY to top, then pause it
    gy = next(q["id"] for q in st["queue"] if q["code"] == "TRV20/GY")
    c.post(f"/api/products/{gy}/move", json={"to": "top"})
    assert c.get("/api/state").json()["queue"][0]["code"] == "TRV20/GY"
    c.post(f"/api/products/{gy}/pause", json={"paused": True})
    c.post("/api/autopilot", json={"autopilot": True, "auto_approve": True})
    assert pipeline.tick()                                   # paused GY skipped -> BK runs
    bk = next(q for q in c.get("/api/state").json()["saved"] if q["code"] == "TRV20/BK")
    prod = c.get(f"/api/products/{bk['id']}").json()
    made = [s["no"] for s in prod["shots"] if s["image"]]
    assert made == [1, 3], made                               # only the shots in the sheet
    assert all(s["qc"]["checks"]["product_exact"] for s in prod["shots"] if s["image"])
    c.post(f"/api/products/{gy}/pause", json={"paused": False})
    assert pipeline.tick()
    gyp = c.get(f"/api/products/{gy}").json()
    assert [s["no"] for s in gyp["shots"] if s["image"]] == [1, 3]
    hows = {s["no"]: s["qc"]["v2"]["how"] for s in gyp["shots"] if s["image"]}
    assert hows[1] == "fresh scene" and hows[3].startswith("pool scene"), hows   # hero fresh, detail reused (auto)
    assert gyp["cost"] < prod["cost"] + 1e-9                  # second product cheaper thanks to the pool
    # nothing else runs: PEN20/SI has no cut-out, NOT-A-CODE is not in the master
    assert not pipeline.tick()
    c.post("/api/autopilot", json={"pipeline_mode": "v1", "autopilot": False})


def test_saved_hero_only_can_make_more():
    """A product auto-saved with only the hero can still get shots 2-5 from the approve button."""
    c = client()
    c.post("/api/autopilot", json={"pipeline_mode": "v1", "autopilot": False})
    c.put("/api/admin/settings", json={"default_shots": [1], "max_cost_product": 5, "daily_budget": 100, "hero_gate": False})
    p = SAMPLES / "CAM-AOC-12_front.png"
    c.post("/api/sources/upload", files=[("files", ("MORE-TEST-1_front.png", p.read_bytes(), "image/png"))])
    pid = next(q["id"] for q in c.get("/api/state").json()["queue"] if q["code"] == "MORE-TEST-1")
    c.post(f"/api/products/{pid}/run"); pipeline.tick()
    c.post(f"/api/products/{pid}/approve", json={"save_now": True})
    assert c.get(f"/api/products/{pid}").json()["stage"] == "saved"
    r = c.post(f"/api/products/{pid}/approve", json={"make": [2, 3, 4]}).json()
    assert r["generating"] == [2, 3, 4]
    pipeline.tick()
    prod = c.get(f"/api/products/{pid}").json()
    assert all(s["image"] for s in prod["shots"])



def test_shoot_sheet_with_original_photos():
    """Default (no cut-outs): Library keeps originals grouped by code; shoot sheet drives v1 AI generation with
    several reference photos; only listed codes and shots are generated."""
    import io, time, openpyxl
    from PIL import Image, ImageDraw
    from app.providers import mock
    c = client()
    c.put("/api/admin/settings", json={"library_cutouts": False, "max_cost_product": 5, "daily_budget": 100,
                                       "default_shots": [1, 2, 3, 4], "hero_gate": False, "engine_mode": "classic"})
    c.post("/api/library/clear")
    LIB = Path(__file__).parent / "library"
    def photo(color, size=1200, side=False):
        im = Image.new("RGB", (size, size), (250, 250, 250))
        d = ImageDraw.Draw(im)
        if side:
            d.rectangle([size // 10, size // 2 - size // 20, size * 9 // 10, size // 2 + size // 20], fill=color)
        else:
            d.ellipse([size // 4, size // 4, size * 3 // 4, size * 3 // 4], fill=color)
        b = io.BytesIO(); im.save(b, "JPEG"); return b.getvalue()
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["SHOOT", "SHOTS", "PRIORITY", "NOTE"])
    ws.append(["PEN20/YE", "1,2", 1, ""]); ws.append(["PEN20/RD", "", 2, ""]); ws.append(["PEN20/BK", "", 3, ""])
    b = io.BytesIO(); wb.save(b)
    c.post("/api/library/upload", files=[("files", ("master.xlsx", (LIB / "master.xlsx").read_bytes(), "x/y")),
                                         ("files", ("orig_shoot.xlsx", b.getvalue(), "x/y")),
                                         ("files", ("PEN20-YE front.jpg", photo((240, 200, 0)), "image/jpeg")),
                                         ("files", ("PEN20-YE side.jpg", photo((230, 190, 0), 900, True), "image/jpeg")),
                                         ("files", ("PEN20-YE side copy.jpg", photo((230, 190, 0), 600, True), "image/jpeg")),
                                         ("files", ("PEN20_RD.jpg", photo((200, 0, 0)), "image/jpeg"))])
    for _ in range(100):
        st = c.get("/api/library").json()
        if not st["job"]["running"]:
            break
        time.sleep(0.1)
    assert st["cutouts"] is False and c.get("/api/library/pending").json() == []
    rows = {r["code"]: r for r in st["rows"]}
    assert rows["PEN20/YE"]["status"] == "ready" and rows["PEN20/YE"]["images"] == 2 and rows["PEN20/YE"]["dups"] == 1
    assert rows["PEN20/RD"]["status"] == "ready" and rows["PEN20/BK"]["status"] == "needs_photo"
    seen = {}
    orig = mock.MockProvider.generate
    def spy(self, prompt, raw, model, size="1K", refs=None, style=None):
        seen.setdefault("refs", []).append(len(refs or []))
        return orig(self, prompt, raw, model, size)
    mock.MockProvider.generate = spy
    try:
        c.post("/api/autopilot", json={"pipeline_mode": "v2", "autopilot": True, "auto_approve": True})
        r = c.post("/api/v2/load-shoot").json()
        assert r["total"] == 3 and r["waiting"] == 1, r
        while pipeline.tick():
            pass
    finally:
        mock.MockProvider.generate = orig
        c.post("/api/autopilot", json={"pipeline_mode": "v1", "autopilot": False})
    c.post("/api/autopilot", json={"pipeline_mode": "v2"})
    st = c.get("/api/state").json()
    allp = {q["code"]: q for q in st["queue"] + st["saved"]}
    ye = c.get(f"/api/products/{allp['PEN20/YE']['id']}").json()
    rd = c.get(f"/api/products/{allp['PEN20/RD']['id']}").json()
    assert [s["no"] for s in ye["shots"] if s["image"]] == [1, 2]                  # only the listed shots
    assert [s["no"] for s in rd["shots"] if s["image"]] == [1, 2, 3, 4]            # blank = default (all 4)
    assert allp["PEN20/BK"]["stage"] == "needs_photo"
    assert 1 in seen["refs"]                                                       # YE got its second angle
    c.post("/api/autopilot", json={"pipeline_mode": "v1", "autopilot": False})


def test_files_survive_wipe_via_drive_mirror():
    """Files saved locally are mirrored to Drive and restored when the local disk is wiped (redeploy)."""
    import time
    from app.connectors import mirror, google, storage
    client()                                                # creates the tables
    store = {}
    orig = (google.upload, google.download, mirror.enabled, mirror._folder)
    google.upload = lambda folder, name, data, mime="image/jpeg": store.setdefault(name, data) and f"id-{name}"
    google.download = lambda fid: store[fid[3:]]
    mirror.enabled = lambda: True
    mirror._folder = lambda: "folder"
    try:
        rel = storage.save("shots/TEST/1-hero.jpg", b"jpegbytes")
        from app.db import SessionLocal, FileMirror
        for _ in range(200):                                    # wait for the background upload to be recorded
            with SessionLocal() as s:
                if s.get(FileMirror, rel):
                    break
            time.sleep(0.05)
        (storage.STORAGE_DIR / rel).unlink()                # simulate the redeploy wiping the disk
        assert storage.read(rel) == b"jpegbytes"
    finally:
        google.upload, google.download, mirror.enabled, mirror._folder = orig



def test_queue_drag_order():
    c = client()
    c.post("/api/autopilot", json={"pipeline_mode": "v2"})
    q = c.get("/api/state").json()["queue"]
    if len(q) >= 2:
        ids = [x["id"] for x in q][::-1]
        assert c.post("/api/queue/order", json={"ids": ids}).json()["ok"]
        assert [x["id"] for x in c.get("/api/state").json()["queue"]][:len(ids)] == ids
    c.post("/api/autopilot", json={"pipeline_mode": "v1"})


def test_brand_layer_house_style_unbranded_and_golden():
    import io
    from PIL import Image
    from app.providers import mock
    c = client()
    st = c.get("/api/style").json()
    keys = {b["key"] for b in st["brands"]}
    assert {"_default", "troika", "milan", "apli"} <= keys
    b = io.BytesIO(); Image.new("RGB", (800, 1000), (240, 236, 228)).save(b, "JPEG")
    r = c.post("/api/style/brands/troika/golden", files=[("files", ("gold.jpg", b.getvalue(), "image/jpeg"))]).json()
    assert len(r["golden"]) == 1 and c.get(r["golden"][0]).status_code == 200
    assert c.get("/brands").status_code == 200
    # unbranded product: prompt forbids invented branding; style reference passed for a branded one
    from app.db import SessionLocal, Product
    from app.dataset import build_prompts, live_rules
    from app import pipeline as pl
    with SessionLocal() as s:
        p = Product(code="GEN-1", name="Desk organiser", brand_id="_default", category_id="desk_acc",
                    data={"brand_label": "Made in China"})
        s.add(p); s.flush()
        prompt = build_prompts(s, p, {}, live_rules(s))[0]["prompt"]
        assert "UNBRANDED PRODUCT" in prompt and "SKYBLUE LISTING STANDARD" in prompt
        t = Product(code="TRV20/GY", name="Troika case", brand_id="troika", category_id="bag_travel", data={"brand_label": "TROIKA"})
        s.add(t); s.flush()
        assert "UNBRANDED" not in build_prompts(s, t, {}, live_rules(s))[0]["prompt"]
        assert pl.style_reference(s, t) is not None
        s.rollback()


def test_product_memory_quality_gate_and_learning():
    import io, time, openpyxl
    from PIL import Image, ImageDraw, ImageFilter
    from app.providers import mock
    c = client()
    c.post("/api/library/clear")
    c.put("/api/admin/settings", json={"library_cutouts": False, "max_cost_product": 5, "daily_budget": 100,
                                       "default_shots": [1, 2], "hero_gate": False, "retries": 0, "qc_mode": "light",
                                       "engine_mode": "classic"})
    LIB = Path(__file__).parent / "library"
    def photo(color, size=1400, blur=0):
        im = Image.new("RGB", (size, size), (250, 250, 250))
        ImageDraw.Draw(im).rectangle([size // 3, size // 5, size * 2 // 3, size * 4 // 5], fill=color)
        if blur:
            im = im.filter(ImageFilter.GaussianBlur(blur))
        b = io.BytesIO(); im.save(b, "JPEG"); return b.getvalue()
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["SHOOT", "SHOTS"]); ws.append(["PIP25/BL", "1,2"]); ws.append(["PIP25/YE", "1"])
    b = io.BytesIO(); wb.save(b)
    c.post("/api/library/upload", files=[("files", ("master.xlsx", (LIB / "master.xlsx").read_bytes(), "x/y")),
                                         ("files", ("mem_shoot.xlsx", b.getvalue(), "x/y")),
                                         ("files", ("PIP25-BL front.jpg", photo((20, 60, 200)), "image/jpeg")),
                                         ("files", ("PIP25-YE.jpg", photo((240, 200, 0), 600, blur=4), "image/jpeg"))])
    st = wait_job(c)
    rows = {r["code"]: r for r in st["rows"]}
    assert rows["PIP25/BL"]["status"] == "ready"
    assert rows["PIP25/YE"]["status"] == "low_quality" and "blurry" in rows["PIP25/YE"]["notes"]
    # study + edit + approve memory
    assert c.post("/api/library/study-shoot").status_code == 200
    st = wait_job(c)
    bl = {r["code"]: r for r in st["rows"]}["PIP25/BL"]
    assert bl["memory"]["facts"]["summary"] and bl["primary"]["angle"] == "front"
    m = c.put(f"/api/library/products/{bl['key']}/memory", json={"facts": {"text": ["TROIKA", "LILIPUT"]}, "approved": True}).json()
    assert m["memory"]["approved"] and m["memory"]["facts"]["text"] == ["TROIKA", "LILIPUT"]
    # generation: facts reach the prompt; a QC fail is learned
    seen = []
    og, oq = mock.MockProvider.generate, mock.MockProvider.qc
    def gen(self, prompt, raw, model, size="1K", refs=None, style=None):
        seen.append(prompt); return og(self, prompt, raw, model, size)
    def qc(self, raw, img, shot, rules):
        assert "TROIKA" in shot["facts"]
        return {"checks": {"label_text": shot["no"] != 2, "shape_match": True, "no_artifacts": True},
                "reasons": {"label_text": "logo misspelt as TROlKA"}}
    mock.MockProvider.generate, mock.MockProvider.qc = gen, qc
    try:
        c.post("/api/autopilot", json={"pipeline_mode": "v2", "autopilot": True, "auto_approve": False})
        r = c.post("/api/v2/load-shoot").json()
        assert r["waiting"] == 1                                   # the blurry one is held back, nothing spent
        while pipeline.tick():
            pass
    finally:
        mock.MockProvider.generate, mock.MockProvider.qc = og, oq
        c.post("/api/autopilot", json={"pipeline_mode": "v1", "autopilot": False})
    assert seen and "PRODUCT FACTS" in seen[0] and "TROIKA" in seen[0]
    watch = {r["code"]: r for r in c.get("/api/library").json()["rows"]}["PIP25/BL"]["memory"]["facts"]["watch"]
    assert any("TROlKA" in w for w in watch)


def test_shopify_queue_push_by_barcode_and_draft():
    import time
    from app.connectors import shopify
    c = client()
    calls = []
    store = {"barcode:EXIST": {"id": "gid://shopify/Product/1", "title": "x", "status": "ACTIVE", "media": {"nodes": []}}}
    def fake_call(q, v=None):
        calls.append((q.split("(")[0].strip()[:40], v))
        if "productVariants(first:2" in q:
            p = store.get(v["q"])
            return {"productVariants": {"nodes": [{"sku": "", "barcode": "", "product": p}] if p else []}}
        if "stagedUploadsCreate" in q:
            return {"stagedUploadsCreate": {"stagedTargets": [{"url": "https://up", "resourceUrl": "https://res/1", "parameters": []}]}}
        if "productCreateMedia" in q:
            return {"productCreateMedia": {"mediaUserErrors": []}}
        if "productCreate(" in q:
            return {"productCreate": {"product": {"id": "gid://shopify/Product/2", "variants": {"nodes": [{"id": "v1"}]}}, "userErrors": []}}
        if "productVariantsBulkUpdate" in q:
            return {"productVariantsBulkUpdate": {"userErrors": []}}
        raise AssertionError(q[:80])
    orig_call, orig_post = shopify._call, shopify.httpx.post
    class R:
        def raise_for_status(self): pass
    shopify._call, shopify.httpx.post = fake_call, (lambda *a, **k: R())
    try:
        q = c.get("/api/shopify/queue").json()["items"]
        assert q, "approved products from earlier tests should be waiting"
        ids = [x["id"] for x in q[:2]]
        assert c.post("/api/shopify/push", json={"ids": ids}).json()["ok"]
        for _ in range(100):
            j = c.get("/api/shopify/queue").json()
            if not j["job"]["running"]:
                break
            time.sleep(0.05)
        done = {x["id"]: x for x in c.get("/api/shopify/queue").json()["items"]}
        assert all(done[i]["shopify_status"] == "pushed" for i in ids), [(done[i].get("shopify_status"), done[i].get("shopify_error"), j["job"]) for i in ids]
        assert any("new draft" in done[i]["shopify_how"] for i in ids)
        # pushing again never re-uploads shots that are already on the product
        info = {"title": "T", "barcode": "EXIST", "skus": []}
        store["barcode:EXIST"]["media"]["nodes"] = [{"alt": "A | Hero"}]
        r = shopify.push(info, [("a.jpg", b"x", "A | Hero"), ("b.jpg", b"y", "A | Detail")])
        assert r == {"product_id": "gid://shopify/Product/1", "how": "barcode EXIST", "added": 1, "skipped": 1}
        assert c.get("/shopify").status_code == 200
    finally:
        shopify._call, shopify.httpx.post = orig_call, orig_post


def test_brand_workspace_queue_and_order():
    import io, openpyxl
    c = client()
    LIB = Path(__file__).parent / "library"
    c.post("/api/library/upload", files=[("files", ("master.xlsx", (LIB / "master.xlsx").read_bytes(), "x/y"))])
    wait_job(c)
    tiles = {b["key"]: b for b in c.get("/api/brands").json()}
    assert {"milan", "troika", "apli", "orom", "suckuk", "montmarte", "lamy"} <= set(tiles)
    assert tiles["troika"]["in_master"] > 100 and tiles["milan"]["website"].startswith("https://www.milan.es")
    d = c.get("/api/brands/troika?q=pencil").json()
    assert d["available"] and all("PENCIL" in x["name"].upper() for x in d["available"])
    r = c.post("/api/brands/troika/queue/add", json={"codes": ["TRV20/GY", "pen20-si", "NOPE-1"]}).json()
    assert r["added"] == ["TRV20/GY", "PEN20/SI"] and r["unknown"] == ["NOPE-1"]
    q = [x["key"] for x in c.get("/api/brands/troika").json()["queue"]]
    c.put("/api/brands/troika/queue", json={"keys": q[::-1]})                     # drag & drop reorder
    assert [x["code"] for x in c.get("/api/brands/troika").json()["queue"]] == ["PEN20/SI", "TRV20/GY"]
    assert all(x["key"] not in q for x in c.get("/api/brands/troika").json()["available"])   # queued items leave the list
    c.put("/api/brands-order", json={"keys": ["apli", "troika"]})                 # Apli #1, Troika #2
    ranks = {b["key"]: b["rank"] for b in c.get("/api/brands").json()}
    assert ranks["apli"] == 1 and ranks["troika"] == 2 and ranks["milan"] is None
    c.post("/api/autopilot", json={"pipeline_mode": "v2"})
    r = c.post("/api/brands/troika/send").json()
    assert r["total"] == 2
    st = c.get("/api/state").json()["queue"]
    mine = [x for x in st if x["code"] in ("PEN20/SI", "TRV20/GY")]
    assert [x["code"] for x in sorted(mine, key=lambda x: x["priority"])] == ["PEN20/SI", "TRV20/GY"]
    assert all(2000 < x["priority"] < 3000 for x in mine)                         # brand #2 block
    assert c.get("/brands/troika").status_code == 200
    from app.db import SessionLocal
    from app.library import focus_index
    with SessionLocal() as s:
        assert {p.key for p in focus_index(s).values()} >= {"PEN20SI", "TRV20GY"}   # Library scans brand-queue products
    c.post("/api/autopilot", json={"pipeline_mode": "v1"})



def test_accuracy_engine_keeps_real_product():
    """Accurate mode: hero/use case/lifestyle edit the real photo and restore its pixels; detail crops when big enough;
    a photo without a plain background falls back to fresh generation; QC is skipped only for exact shots."""
    import io, time, openpyxl
    from PIL import Image, ImageDraw
    from app.providers import mock
    c = client()
    c.post("/api/library/clear")
    c.put("/api/admin/settings", json={"engine_mode": "accurate", "library_cutouts": False, "max_cost_product": 5,
                                       "daily_budget": 100, "default_shots": [1, 2, 3, 4], "hero_gate": False, "retries": 0})
    LIB = Path(__file__).parent / "library"
    def photo(size=1800, busy=False):
        im = Image.new("RGB", (size, size), (248, 248, 248))
        d = ImageDraw.Draw(im)
        if busy:                                              # product already in a scene: no plain background
            import random
            r = random.Random(3)
            for _ in range(500):
                x, y = r.randrange(size), r.randrange(size)
                d.rectangle([x, y, x + size // 10, y + size // 10], fill=(r.randrange(255), r.randrange(255), r.randrange(255)))
        d.rectangle([size * 3 // 10, size // 5, size * 7 // 10, size * 4 // 5], fill=(20, 60, 200))
        d.text((size // 2 - 40, size // 2), "TROIKA", fill=(255, 255, 255))
        for i in range(0, 60, 6):
            d.line([(size * 3 // 10 + 20, size // 4 + i * 8), (size * 7 // 10 - 20, size // 4 + i * 8)], fill=(200, 220, 255))
        b = io.BytesIO(); im.save(b, "JPEG", quality=95); return b.getvalue()
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["SHOOT", "SHOTS"]); ws.append(["BBG64/GY", ""]); ws.append(["BBL64/BK", "1"])
    b = io.BytesIO(); wb.save(b)
    c.post("/api/library/upload", files=[("files", ("master.xlsx", (LIB / "master.xlsx").read_bytes(), "x/y")),
                                         ("files", ("eng_shoot.xlsx", b.getvalue(), "x/y")),
                                         ("files", ("BBG64-GY front.jpg", photo(), "image/jpeg")),
                                         ("files", ("BBL64-BK scene.jpg", photo(busy=True), "image/jpeg"))])
    wait_job(c)
    calls = {"edit": 0, "generate": 0, "qc": 0}
    oe, og, oq = mock.MockProvider.edit, mock.MockProvider.generate, mock.MockProvider.qc
    def e(self, *a, **k): calls["edit"] += 1; return oe(self, *a, **k)
    def g(self, *a, **k): calls["generate"] += 1; return og(self, a[0], a[1], a[2], a[3] if len(a) > 3 else "1K")
    def q(self, *a, **k): calls["qc"] += 1; return oq(self, *a, **k)
    mock.MockProvider.edit, mock.MockProvider.generate, mock.MockProvider.qc = e, g, q
    try:
        c.post("/api/autopilot", json={"pipeline_mode": "v2", "autopilot": True, "auto_approve": False})
        c.post("/api/v2/load-shoot")
        while pipeline.tick():
            pass
    finally:
        mock.MockProvider.edit, mock.MockProvider.generate, mock.MockProvider.qc = oe, og, oq
        c.post("/api/autopilot", json={"pipeline_mode": "v1", "autopilot": False})
    c.post("/api/autopilot", json={"pipeline_mode": "v2"})
    st = c.get("/api/state").json()
    allp = {x["code"]: x for x in st["queue"] + st["saved"]}
    bl = c.get(f"/api/products/{allp['BBG64/GY']['id']}").json()
    hows = {s["kind"]: s["qc"].get("how", {}) for s in bl["shots"] if s["image"]}
    assert hows["hero"]["method"] == "edit" and hows["hero"]["exact"] is True
    assert hows["detail"]["method"] == "crop" and hows["detail"]["exact"] is True
    assert hows["use_case"]["method"] == "edit" and hows["lifestyle"]["method"] == "edit_zoom"
    ye = c.get(f"/api/products/{allp['BBL64/BK']['id']}").json()
    assert ye["shots"][0]["qc"]["how"]["method"] == "fresh"           # busy background -> fresh generation
    assert calls["edit"] == 3 and calls["generate"] == 1               # detail crop needed no AI at all
    assert calls["qc"] == 1                                            # only the fresh shot needed AI QC
    img = c.get(bl["shots"][0]["image"]).content
    assert Image.open(io.BytesIO(img)).size == (1200, 1500)
    c.post("/api/autopilot", json={"pipeline_mode": "v1"})


def test_product_kit_angles_need_approval():
    import io
    from PIL import Image, ImageDraw
    c = client()
    LIB = Path(__file__).parent / "library"
    im = Image.new("RGB", (1600, 1600), (250, 250, 250)); ImageDraw.Draw(im).ellipse([500, 400, 1100, 1200], fill=(200, 30, 30))
    b = io.BytesIO(); im.save(b, "JPEG")
    c.post("/api/library/upload", files=[("files", ("master.xlsx", (LIB / "master.xlsx").read_bytes(), "x/y")),
                                         ("files", ("KR18-05-BR.jpg", b.getvalue(), "image/jpeg"))])
    wait_job(c)
    row = next(r for r in c.get("/api/library").json()["rows"] if r["code"] == "KR18-05/BR")
    assert set(row["kit"]["missing"]) == {"angle", "side"}
    assert c.post(f"/api/library/products/{row['key']}/kit").json()["ok"]
    wait_job(c)
    row = next(r for r in c.get("/api/library").json()["rows"] if r["code"] == "KR18-05/BR")
    pend = [i for i in row["all"] if i["kit"] == "pending"]
    assert len(pend) == 2 and row["primary"]["kit"] == ""                      # a real photo stays the main one
    from app.db import SessionLocal
    from app.library import refs_with_angles
    with SessionLocal() as s:
        assert all(r["angle"] not in ("angle", "side") for r in refs_with_angles(s, row["key"]))   # unapproved: unused
    c.post(f"/api/library/images/{pend[0]['id']}", json={"action": "kit_approve"})
    c.post(f"/api/library/images/{pend[1]['id']}", json={"action": "kit_reject"})
    with SessionLocal() as s:
        angles = {r["angle"] for r in refs_with_angles(s, row["key"])}
    assert pend[0]["angle"] in angles and pend[1]["angle"] not in angles
    row = next(r for r in c.get("/api/library").json()["rows"] if r["code"] == "KR18-05/BR")
    assert row["kit"]["missing"] == [pend[1]["angle"]] and not row["kit"]["pending"]
