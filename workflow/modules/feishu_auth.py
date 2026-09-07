"""Shared Feishu app-credential authentication for local workflows."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

import requests

_SESSION = requests.Session()
_LOCK = threading.Lock()
_CACHE: dict[tuple[str, str], tuple[str, float]] = {}
_REFRESH_SKEW_SECONDS = 300

def _load_env_file(path: Path) -> None:
    if not path.exists(): return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))

def credentials() -> tuple[str, str]:
    app_id = os.getenv("FEISHU_APP_ID", "").strip(); app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        _load_env_file(Path(__file__).resolve().parents[1] / "env" / "chats.env")
        app_id = os.getenv("FEISHU_APP_ID", "").strip(); app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
    if not app_id or not app_secret: raise RuntimeError("FEISHU_APP_ID and FEISHU_APP_SECRET are required.")
    return app_id, app_secret

def api_domain() -> str:
    return (os.getenv("FEISHU_OPEN_API_DOMAIN") or os.getenv("LARK_OPEN_API_DOMAIN") or "https://open.feishu.cn").rstrip("/")

def tenant_access_token(*, force_refresh: bool = False) -> str:
    app_id, app_secret = credentials(); key = (api_domain(), app_id); now = time.time()
    with _LOCK:
        cached = _CACHE.get(key)
        if not force_refresh and cached and cached[1] > now: return cached[0]
        response = _SESSION.post(f"{key[0]}/open-apis/auth/v3/tenant_access_token/internal", json={"app_id": app_id, "app_secret": app_secret}, timeout=30)
        if response.status_code >= 400: raise RuntimeError(f"tenant_access_token request failed with HTTP {response.status_code}")
        payload: dict[str, Any] = response.json(); token = str(payload.get("tenant_access_token") or "")
        if int(payload.get("code") or 0) != 0 or not token: raise RuntimeError(f"tenant_access_token request failed: code={payload.get('code')} msg={payload.get('msg') or ''}")
        _CACHE[key] = (token, now + max(600.0, float(payload.get("expire") or 7200)) - _REFRESH_SKEW_SECONDS)
        return token

def request(method: str, path: str, *, params: dict[str, Any] | None = None, json_body: dict[str, Any] | None = None, timeout: int = 120) -> dict[str, Any]:
    for attempt in range(2):
        response = _SESSION.request(method.upper(), f"{api_domain()}{path}", params=params, json=json_body, headers={"Authorization": f"Bearer {tenant_access_token(force_refresh=attempt > 0)}"}, timeout=timeout)
        if response.status_code in {401, 403} and attempt == 0: continue
        if response.status_code >= 400:
            try: payload = response.json()
            except ValueError: payload = {}
            raise RuntimeError(f"Feishu API HTTP {response.status_code}: code={payload.get('code', '')} msg={payload.get('msg', '')}")
        payload = response.json()
        if int(payload.get("code") or 0) != 0: raise RuntimeError(f"Feishu API error: code={payload.get('code')} msg={payload.get('msg') or ''}")
        return payload
    raise RuntimeError("Feishu API authorization refresh failed")
