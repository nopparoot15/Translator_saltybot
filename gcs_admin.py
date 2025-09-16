from __future__ import annotations

import asyncio
from typing import Optional, Tuple, List, Dict, Any
from urllib.parse import quote

import httpx
import google.auth
from google.auth.transport.requests import Request

API_ROOT = "https://storage.googleapis.com/storage/v1"
USER_AGENT = "salty-translator-bot/1.0"

# ------------------------------------------------------------
# Auth helpers
# ------------------------------------------------------------
async def _get_access_token(scope: str = "https://www.googleapis.com/auth/cloud-platform") -> str:
    creds, _ = google.auth.default(scopes=[scope])
    if not creds.valid:
        creds.refresh(Request())
    return creds.token

def _get_sa_email_safe() -> str:
    try:
        creds, _ = google.auth.default()
        # service_account.Credentials มี property พวกนี้
        for attr in ("service_account_email", "signer_email", "_service_account_email"):
            v = getattr(creds, attr, None)
            if v:
                return str(v)
    except Exception:
        pass
    return "-"

# ------------------------------------------------------------
# Low-level JSON API wrappers
# ------------------------------------------------------------
async def _request_json(
    method: str,
    url: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    token = await _get_access_token()
    h = {"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}
    if headers:
        h.update(headers)
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        r = await client.request(method, url, params=params, headers=h)
        r.raise_for_status()
        if r.content:
            return r.json()
        return {}

async def _list_objects(
    bucket: str,
    *,
    prefix: Optional[str] = None,
    page_token: Optional[str] = None,
    include_versions: bool = False,
    user_project: Optional[str] = None,
) -> Dict[str, Any]:
    params: Dict[str, Any] = {"maxResults": 1000}
    if prefix:
        params["prefix"] = prefix
    if page_token:
        params["pageToken"] = page_token
    if include_versions:
        params["versions"] = "true"
    if user_project:
        params["userProject"] = user_project
    url = f"{API_ROOT}/b/{bucket}/o"
    return await _request_json("GET", url, params=params)

async def _delete_object(
    bucket: str,
    object_name: str,
    *,
    generation: Optional[str] = None,
    user_project: Optional[str] = None,
) -> None:
    # ต้อง urlencode ชื่อ object
    url = f"{API_ROOT}/b/{bucket}/o/{quote(object_name, safe='')}"
    params: Dict[str, Any] = {}
    if generation:
        params["generation"] = generation
    if user_project:
        params["userProject"] = user_project
    await _request_json("DELETE", url, params=params)

async def _delete_bucket(bucket: str, *, user_project: Optional[str]) -> None:
    url = f"{API_ROOT}/b/{bucket}"
    params: Dict[str, Any] = {}
    if user_project:
        params["userProject"] = user_project
    await _request_json("DELETE", url, params=params)

# ------------------------------------------------------------
# High-level ops
# ------------------------------------------------------------
async def gcs_delete_all_objects(
    bucket: str,
    *,
    prefix: Optional[str] = None,
    user_project: Optional[str] = None,
    include_versions: bool = True,
    concurrency: int = 16,
) -> int:
    """
    ลบ objects ทั้งหมดในบัคเก็ต (รองรับ prefix และ versioned objects)
    คืนจำนวนที่ลบสำเร็จ
    """
    deleted = 0
    sem = asyncio.Semaphore(max(1, int(concurrency)))

    async def _del(obj: Dict[str, Any]):
        nonlocal deleted
        name = obj.get("name", "")
        gen = str(obj.get("generation") or "") or None
        try:
            async with sem:
                await _delete_object(bucket, name, generation=gen, user_project=user_project)
            deleted += 1
        except httpx.HTTPStatusError:
            # ข้ามวัตถุที่ลบไม่ได้ (เช่น สิทธิ์/ถูกลบไปก่อนแล้ว)
            pass

    page_token: Optional[str] = None
    tasks: List[asyncio.Task] = []

    while True:
        data = await _list_objects(
            bucket,
            prefix=prefix,
            page_token=page_token,
            include_versions=include_versions,
            user_project=user_project,
        )
        items = data.get("items") or []
        for obj in items:
            tasks.append(asyncio.create_task(_del(obj)))

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    return deleted

# ------------------------------------------------------------
# Public entry
# ------------------------------------------------------------
async def gcs_delete_bucket(
    bucket: str,
    *,
    force: bool = False,
    prefix: Optional[str] = None,
    user_project: Optional[str] = None,
) -> Tuple[bool, str]:
    """
    ลบบัคเก็ต:
      - ถ้า force=True จะลบ objects (รวมทุกเวอร์ชัน) ก่อน (กรองด้วย prefix ถ้าระบุ)
      - รองรับบัคเก็ตแบบ Requester Pays ผ่าน user_project
      - ต้องมีสิทธิ์อย่างน้อย: roles/storage.admin (หรือรวม objectAdmin + buckets.delete)
    """
    sa_email = _get_sa_email_safe()
    try:
        n = 0
        if force:
            n = await gcs_delete_all_objects(
                bucket, prefix=prefix, user_project=user_project, include_versions=True
            )

        await _delete_bucket(bucket, user_project=user_project)
        msg = f"✅ ลบบัคเก็ต `{bucket}` สำเร็จ"
        if force:
            msg += f" (ลบ objects {n} ชิ้นก่อนหน้า)"
        if user_project:
            msg += f"\n• userProject: `{user_project}`"
        return True, msg

    except httpx.HTTPStatusError as e:
        code = getattr(e.response, "status_code", None)
        detail = ""
        try:
            detail = (e.response.json().get("error", {}).get("message") or "")[:300]
        except Exception:
            detail = (e.response.text or "")[:300]

        # Mapping ที่พบบ่อย
        if code == 409:
            return False, "⚠️ ลบบัคเก็ตไม่สำเร็จ: บัคเก็ตยังไม่ว่าง (ลองใช้ `--force` เพื่อลบ objects ทั้งหมดก่อน)"
        if code == 404:
            return False, "⚠️ ไม่พบบัคเก็ตดังกล่าว"
        if code == 400 and ("user project" in detail.lower() or "requester pays" in detail.lower()):
            return False, (
                "⚠️ บัคเก็ตนี้เป็นแบบ *Requester Pays* จำเป็นต้องระบุ `--user-project=<PROJECT_ID>`\n"
                f"รายละเอียด: {detail or 'userProject missing'}"
            )
        if code == 403:
            return False, (
                "❌ ไม่มีสิทธิ์ลบ (403 Forbidden)\n"
                f"• SA: `{sa_email}`\n"
                f"• userProject: `{user_project or '-'}`\n"
                "จำเป็นต้องมีบทบาทอย่างน้อย `roles/storage.admin` บนโปรเจกต์ของบัคเก็ต หรือบนตัวบัคเก็ต"
            )

        return False, f"❌ ล้มเหลว (HTTP {code}) {detail or str(e)[:200]}"

    except Exception as e:
        return False, f"❌ Error: {type(e).__name__}: {e}"

__all__ = ["gcs_delete_bucket", "gcs_delete_all_objects"]
