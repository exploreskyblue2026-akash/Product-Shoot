"""End-to-end run with the mock AI provider: upload → autopilot → 5 shots → QC → saved. Run: python -m pytest -q"""
import os
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ.update({"AI_PROVIDER": "mock", "DATABASE_URL": f"sqlite:///{TMP}/t.db", "STORAGE_DIR": f"{TMP}/store",
                   "RUN_WORKER": "false", "COOKIE_SECURE": "false", "OWNER_EMAIL": "owner@test.com",
                   "OWNER_PASSWORD": "pw-for-tests", "SECRET_KEY": "test"})

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app import pipeline  # noqa: E402

SAMPLES = Path(__file__).parent / "samples"


def client():
    c = TestClient(app)
    c.__enter__()
    r = c.post("/api/login", json={"email": "owner@test.com", "password": "pw-for-tests"})
    assert r.status_code == 200, r.text
    return c


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

    c.put("/api/admin/settings", json={"default_shots": [1, 2, 3, 4, 5], "max_cost_product": 5, "daily_budget": 100,
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
    assert len(p["shots"]) == 5 and all(s["image"] for s in p["shots"])
    img = c.get(p["shots"][0]["image"])
    assert img.status_code == 200 and img.content[:2] == b"\xff\xd8"
    from PIL import Image
    import io
    assert Image.open(io.BytesIO(img.content)).size == (2000, 2500)
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
    c.post(f"/api/products/{pid}/regenerate", json={"shot": 5})
    pipeline.tick()
    prod = c.get(f"/api/products/{pid}").json()
    assert [s["no"] for s in prod["shots"] if s["image"]] == [1, 5]
    # cap: a tiny limit stops generation before spending
    c.put("/api/admin/settings", json={"max_cost_product": 0.001})
    c.post(f"/api/products/{pid}/regenerate", json={"shot": 2})
    pipeline.tick()
    prod = c.get(f"/api/products/{pid}").json()
    assert not prod["shots"][1]["image"] and "cost limit" in prod["error"]
