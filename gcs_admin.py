from __future__ import annotations

import asyncio
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

async def _list_objects(bucket: str, prefix: Optional[str] = None, page_token: Optional[str] = None) -> Dict:
    token = await _get_access_token()
    params = {"maxResults": 1000}
    if prefix:
        params["prefix"] = prefix
    if page_token:
        params["pageToken"] = page_token
    url = f"{API_ROOT}/b/{bucket}/o"
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        r = await client.get(url, headers={"Authorization": f"Bearer {token}"}, params=params)
        r.raise_for_status()
        return r.json()

async def _delete_object(bucket: str, object_name: str) -> None:
    token = await _get_access_token()
    url = f"{API_ROOT}/b/{bucket}/o/{quote(object_name, safe='')}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        r = await client.delete(url, headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()

async def _delete_bucket(bucket: str) -> None:
    token = await _get_access_token()
    url = f"{API_ROOT}/b/{bucket}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        r = await client.delete(url, headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()

async def gcs_delete_all_objects(bucket: str, prefix: Optional[str] = None) -> int:
    deleted = 0
    page = None
    while True:
        data = await _list_objects(bucket, prefix=prefix, page_token=page)
        items = data.get("items", []) or []
        if not items and not data.get("nextPageToken"):
            break
        for obj in items:
            name = obj.get("name", "")
            if not name:
                continue
            try:
                await _delete_object(bucket, name)
                deleted += 1
            except httpx.HTTPStatusError:
                # ข้ามถ้าลบไม่ได้บางชิ้น (เช่น ไม่มีสิทธิ์/ถูกลบไปแล้ว)
                pass
        page = data.get("nextPageToken")
        if not page:
            break
    return deleted

async def gcs_delete_bucket(bucket: str, *, force: bool = False, prefix: Optional[str] = None) -> Tuple[bool, str]:
    try:
        n = 0
        if force:
            n = await gcs_delete_all_objects(bucket, prefix=prefix)
        await _delete_bucket(bucket)
        msg = f"✅ ลบบัคเก็ต `{bucket}` สำเร็จ" + (f" (ลบ objects {n} ชิ้นก่อนหน้า)" if force else "")
        return True, msg
    except httpx.HTTPStatusError as e:
        if e.response is not None and e.response.status_code == 409:
            return False, "⚠️ ลบบัคเก็ตไม่สำเร็จ: บัคเก็ตยังไม่ว่าง (ลองใช้ --force)"
        if e.response is not None and e.response.status_code == 404:
            return False, "⚠️ ไม่พบบัคเก็ตดังกล่าว"
        if e.response is not None and e.response.status_code == 403:
            return False, "❌ ไม่มีสิทธิ์ลบ (403 Forbidden)"
        return False, f"❌ ล้มเหลว (HTTP {getattr(e.response,'status_code','??')}) {str(e)[:200]}"
    except Exception as e:
        return False, f"❌ Error: {type(e).__name__}: {e}"
