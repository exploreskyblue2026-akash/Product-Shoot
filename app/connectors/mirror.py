"""Keep every stored file safe in Google Drive ("Studio data" folder) and bring it back when the local disk is wiped.

Render's free disk is erased on every redeploy/restart. With a Postgres DATABASE_URL the records survive, and this
mirror makes the files (raw photos, shots, finals) survive too: uploads run in the background, reads restore on demand.
"""
import queue
import threading

from ..config import STORAGE_DIR, env

_q: "queue.Queue[str]" = queue.Queue()
_started = False
_folder_cache: dict = {}


def enabled() -> bool:
    if env("STORAGE_MIRROR", "true").lower() != "true":
        return False
    try:
        from ..secrets import get_secret
        return bool(get_secret("GOOGLE_SERVICE_ACCOUNT_JSON")) and bool(_folder())
    except Exception:
        return False


def _folder() -> str:
    """'Studio data' folder: from STORAGE_DRIVE_FOLDER_ID, or created next to the Final output folder."""
    if "id" in _folder_cache:
        return _folder_cache["id"]
    fid = env("STORAGE_DRIVE_FOLDER_ID")
    if not fid:
        from ..config import OUTPUT_DRIVE_FOLDER_ID
        out = OUTPUT_DRIVE_FOLDER_ID
        if not out:
            try:
                from ..db import SessionLocal, get_setting
                with SessionLocal() as s:
                    out = get_setting(s, "output_drive_folder", "") or ""
            except Exception:
                out = ""
        if out:
            from . import google
            out = google.folder_id(out)
            parent = google.drive().files().get(fileId=out, fields="parents", supportsAllDrives=True).execute()
            par = (parent.get("parents") or [out])[0]
            fid = google.ensure_subfolder(par, "Studio data")
    _folder_cache["id"] = fid or ""
    return _folder_cache["id"]


def _name(rel: str) -> str:
    return rel.replace("/", "__")[-250:]


def queue_upload(rel: str):
    global _started
    if not enabled():
        return
    _q.put(rel)
    if not _started:
        _started = True
        threading.Thread(target=_worker, daemon=True, name="drive-mirror").start()


def _worker():
    from ..db import FileMirror, SessionLocal
    from . import google
    while True:
        rel = _q.get()
        try:
            p = STORAGE_DIR / rel
            if not p.exists():
                continue
            with SessionLocal() as s:
                if s.get(FileMirror, rel):
                    continue
                mime = "image/png" if rel.endswith(".png") else "image/jpeg" if rel.endswith((".jpg", ".jpeg")) else "application/octet-stream"
                did = google.upload(_folder(), _name(rel), p.read_bytes(), mime)
                s.add(FileMirror(rel=rel, drive_id=did))
                s.commit()
        except Exception as e:
            print(f"[mirror] upload failed for {rel}: {str(e)[:160]}")


def restore(rel: str) -> bool:
    """Bring a file back from Drive if the local copy is gone. Returns True when the file exists afterwards."""
    p = STORAGE_DIR / rel
    if p.exists():
        return True
    try:
        from ..db import FileMirror, SessionLocal
        with SessionLocal() as s:
            m = s.get(FileMirror, rel)
            if not m:
                return False
            did = m.drive_id
        from . import google
        data = google.download(did)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return True
    except Exception as e:
        print(f"[mirror] restore failed for {rel}: {str(e)[:160]}")
        return False


def pending() -> int:
    return _q.qsize()
