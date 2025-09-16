# gcs_admin.py
from __future__ import annotations

from typing import Optional, Tuple, Dict
from urllib.parse import quote

import httpx
import google.auth
from google.auth.transport.requests import Request

API_ROOT = "https://storage.googleapis.com/storage/v1"


async def _get_access_token(scope: str = "https://www.googleapis.com/auth/cloud-platform") -> str:
    creds, _ = google.auth.default(scopes=[scope])
    if not creds.valid:
        creds.refresh(Request())
    return creds.token


async def _list_objects(
    bucket: str,
    *,
    prefix: Optional[str] = None,
    page_token: Optional[str] = None,
    user_project: Optional[str] = None,
) -> Dict:
    token = await _get_access_token()
    params = {"maxResults": 1000}
    if prefix:
        params["prefix"] = prefix
    if page_token:
        params["pageToken"] = page_token
    if user_project:
        params["userProject"] = user_project

    url = f"{API_ROOT}/b/{bucket}/o"
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        r = await client.get(url, headers={"Authorization": f"Bearer {token}"}, params=params)
        r.raise_for_status()
        return r.json()


async def _delete_object(bucket: str, object_name: str, *, user_project: Optional[str] = None) -> None:
    token = await _get_access_token()
    url = f"{API_ROOT}/b/{bucket}/o/{quote(object_name, safe='')}"
    params = {}
    if user_project:
        params["userProject"] = user_project
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        r = await client.delete(url, headers={"Authorization": f"Bearer {token}"}, params=params)
        r.raise_for_status()


async def _delete_bucket(bucket: str, *, user_project: Optional[str] = None) -> None:
    token = await _get_access_token()
    url = f"{API_ROOT}/b/{bucket}"
    params = {}
    if user_project:
        params["userProject"] = user_project
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        r = await client.delete(url, headers={"Authorization": f"Bearer {token}"}, params=params)
        r.raise_for_status()


async def gcs_delete_all_objects(bucket: str, *, prefix: Optional[str] = None, user_project: Optional[str] = None) -> int:
    """ลบ objects ทั้งหมดในบัคเก็ต (กรองด้วย prefix ได้) — คืนจำนวนที่ลบสำเร็จ"""
    deleted = 0
    page = None
    while True:
        data = await _list_objects(bucket, prefix=prefix, page_token=page, user_project=user_project)
        items = data.get("items", []) or []
        for obj in items:
            try:
                await _delete_object(bucket, obj.get("name", ""), user_project=user_project)
                deleted += 1
            except httpx.HTTPStatusError:
                # ลบไม่ได้บางชิ้นก็ข้ามไป ทำต่อ
                pass
        page = data.get("nextPageToken")
        if not page:
            break
    return deleted


async def gcs_delete_bucket(
    bucket: str,
    *,
    force: bool = False,
    prefix: Optional[str] = None,
    user_project: Optional[str] = None,
) -> Tuple[bool, str]:
    """
    ลบบัคเก็ต GCS
      - ถ้า force=True จะลบ objects ทั้งหมด (หรือเฉพาะ prefix) ก่อน
      - ถ้าบัคเก็ตเป็น requester-pays ต้องส่ง user_project (PROJECT_ID/NUMBER) มาด้วย
      - ต้องมีสิทธิ์อย่างน้อย roles/storage.admin บนโปรเจกต์เจ้าของบัคเก็ต หรือมอบสิทธิ์ระดับบัคเก็ต
    """
    try:
        n = 0
        if force:
            n = await gcs_delete_all_objects(bucket, prefix=prefix, user_project=user_project)

        await _delete_bucket(bucket, user_project=user_project)

        extra = []
        if force:
            extra.append(f"ลบ objects {n} ชิ้นก่อนหน้า")
        if user_project:
            extra.append(f"userProject={user_project}")
        note = f" ({', '.join(extra)})" if extra else ""

        return True, f"✅ ลบบัคเก็ต `{bucket}` สำเร็จ{note}"

    except httpx.HTTPStatusError as e:
        status = getattr(e.response, "status_code", None)
        body = (e.response.text or "")[:500] if getattr(e, "response", None) else ""

        if status == 409:
            return False, "⚠️ ลบบัคเก็ตไม่สำเร็จ: บัคเก็ตยังไม่ว่าง (ลองใช้ --force เพื่อลบ objects ก่อน)"
        if status == 404:
            return False, "⚠️ ไม่พบบัคเก็ตดังกล่าว"
        if status == 400 and "requester pays" in body.lower():
            return False, "⚠️ บัคเก็ตเป็น *Requester Pays* — โปรดระบุ `--user-project=<PROJECT_ID>`"
        if status == 403:
            return False, (
                "❌ ไม่มีสิทธิ์ (403 Forbidden)\n"
                "- ต้องมีสิทธิ์อย่างน้อย `roles/storage.admin` บนโปรเจกต์เจ้าของบัคเก็ต "
                "หรือมอบสิทธิ์ระดับบัคเก็ตให้ service account/user ของคุณ\n"
                "- ถ้าเป็น *Requester Pays* ให้ใส่ `--user-project=<PROJECT_ID>` ด้วย"
            )

        return False, f"❌ ล้มเหลว (HTTP {status or '??'}) {body or str(e)[:200]}"

    except Exception as e:
        return False, f"❌ Error: {type(e).__name__}: {e}"
