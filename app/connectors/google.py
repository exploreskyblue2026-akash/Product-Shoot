"""Google Drive + Sheets through a service account. Share the folders/sheets with the service account's email."""
import io
import json
import re
from functools import lru_cache

from ..secrets import get_secret

SCOPES = ["https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/spreadsheets.readonly"]
IMAGE_MIME = ("image/jpeg", "image/png", "image/webp")


def _creds():
    raw = get_secret("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise RuntimeError("Google service account not set. Add GOOGLE_SERVICE_ACCOUNT_JSON in Admin → API & models.")
    from google.oauth2 import service_account
    return service_account.Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES)


def drive():
    from googleapiclient.discovery import build
    return build("drive", "v3", credentials=_creds(), cache_discovery=False)


def sheets():
    from googleapiclient.discovery import build
    return build("sheets", "v4", credentials=_creds(), cache_discovery=False)


def folder_id(link: str) -> str:
    m = re.search(r"/folders/([A-Za-z0-9_-]+)", link) or re.search(r"[?&]id=([A-Za-z0-9_-]+)", link)
    return m.group(1) if m else link.strip()


def sheet_id(link: str) -> str:
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", link)
    return m.group(1) if m else link.strip()


def list_images(fid: str, recursive: bool = True) -> list[dict]:
    """Every image in a folder (and sub-folders); each dict has id, name, folder."""
    svc, out, stack = drive(), [], [(fid, "")]
    while stack:
        cur, prefix = stack.pop()
        token = None
        while True:
            r = svc.files().list(q=f"'{cur}' in parents and trashed=false", pageToken=token, pageSize=200,
                                 fields="nextPageToken, files(id,name,mimeType,modifiedTime)",
                                 supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
            for f in r.get("files", []):
                if f["mimeType"] == "application/vnd.google-apps.folder" and recursive:
                    stack.append((f["id"], f["name"]))
                elif f["mimeType"] in IMAGE_MIME or f["mimeType"] == "application/pdf":
                    out.append({**f, "folder": prefix})
            token = r.get("nextPageToken")
            if not token:
                break
    return out


def download(file_id: str) -> bytes:
    from googleapiclient.http import MediaIoBaseDownload
    buf = io.BytesIO()
    req = drive().files().get_media(fileId=file_id, supportsAllDrives=True)
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue()


def upload(folder: str, name: str, data: bytes, mime="image/jpeg") -> str:
    from googleapiclient.http import MediaIoBaseUpload
    meta = {"name": name, "parents": [folder]}
    f = drive().files().create(body=meta, media_body=MediaIoBaseUpload(io.BytesIO(data), mimetype=mime),
                               fields="id", supportsAllDrives=True).execute()
    return f["id"]


def ensure_subfolder(parent: str, name: str) -> str:
    svc = drive()
    q = f"'{parent}' in parents and name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    r = svc.files().list(q=q, fields="files(id)", supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
    if r.get("files"):
        return r["files"][0]["id"]
    return svc.files().create(body={"name": name, "parents": [parent], "mimeType": "application/vnd.google-apps.folder"},
                              fields="id", supportsAllDrives=True).execute()["id"]


def read_sheet(link: str) -> list[dict]:
    sid = sheet_id(link)
    rng = "A1:Z5000"
    vals = sheets().spreadsheets().values().get(spreadsheetId=sid, range=rng).execute().get("values", [])
    if not vals:
        return []
    head = [h.strip().lower().replace(" ", "_") for h in vals[0]]
    return [dict(zip(head, row + [""] * (len(head) - len(row)))) for row in vals[1:] if any(row)]


def test() -> str:
    info = json.loads(get_secret("GOOGLE_SERVICE_ACCOUNT_JSON") or "{}")
    drive().about().get(fields="user").execute()
    return f"Connected as {info.get('client_email', 'service account')}. Share your folders and sheets with this email."
