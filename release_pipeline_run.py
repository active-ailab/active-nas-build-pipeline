#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import fnmatch
import json
import os
import shutil
import subprocess
import sys
import tempfile
import ssl
import urllib.parse
import urllib.request
import http.cookiejar
import tarfile
import zipfile
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Pattern
from urllib.parse import quote

SCRIPT_DIR = str(Path(__file__).resolve().parent)
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)
from config_loader import read_json_with_base

# ── 飞书 CLI 适配层 ──
# 替代原有 ~2000 行自建飞书 API 代码
# 前置条件: npx @larksuite/cli@latest install && lark-cli auth login
_this_dir = str(Path(__file__).resolve().parent)
if _this_dir not in sys.path:
    sys.path.insert(0, _this_dir)
try:
    from lark_cli_adapter import (
        lark_auth_ensure,
        lark_publish_release_document,
        lark_create_doc_from_markdown,
        LarkCliError,
    )
    _LARK_CLI_AVAILABLE = True
except ImportError as _e:
    _LARK_CLI_AVAILABLE = False

# ── 多维表格「环节流水」通知 ──
try:
    from feishu_base_notifier import notify_stage as _base_notify_stage
except Exception:  # pragma: no cover
    _base_notify_stage = None


def _notify_base_stage(cfg: Dict[str, Any], stage: str, status: str, doc_url: str = "") -> None:
    """写一条环节流水到多维表格（best-effort，失败只打 WARN，绝不中断流水线）。"""
    if _base_notify_stage is None:
        return
    try:
        _base_notify_stage(cfg=cfg, stage=stage, status=status, doc_url=doc_url)
    except Exception as e:
        print(f"WARN: [多维表格] {stage}/{status} 写入异常: {type(e).__name__}: {e}", file=sys.stderr)
    LarkCliError = RuntimeError  # type: ignore

def _http_json(
    *,
    method: str,
    url: str,
    headers: Dict[str, str],
    body: Optional[Dict[str, Any]] = None,
    timeout_sec: int = 30,
) -> Dict[str, Any]:
    data: Optional[bytes]
    if body is None:
        data = None
    else:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {**headers, "Content-Type": "application/json; charset=utf-8"}

    req = urllib.request.Request(url, method=method.upper(), data=data)
    for k, v in (headers or {}).items():
        req.add_header(k, v)

    try:
        with urllib.request.urlopen(req, timeout=int(timeout_sec)) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raw = e.read() if hasattr(e, "read") else b""
        detail = raw.decode("utf-8", errors="replace")[:2000]
        raise RuntimeError(f"HTTP {e.code} calling {url}: {detail}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error calling {url}: {e}")

    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as e:
        snippet = raw[:2000].decode("utf-8", errors="replace")
        raise RuntimeError(f"Non-JSON response calling {url}: {e}; body={snippet!r}")


def _get_webhook_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    notif = cfg.get("notifications")
    if not isinstance(notif, dict):
        return {}
    wh = notif.get("webhook")
    if not isinstance(wh, dict):
        wh = {}
    # env 兜底：配置里 url 为空时，回退到环境变量 FEISHU_WEBHOOK_URL
    if not str(wh.get("url") or "").strip():
        env_url = os.environ.get("FEISHU_WEBHOOK_URL", "").strip()
        if env_url:
            wh = dict(wh)
            wh["url"] = env_url
    return wh


def _http_post_json(*, url: str, payload: Dict[str, Any], timeout_sec: int, verify_tls: bool) -> Tuple[int, str]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, method="POST", data=data)
    req.add_header("Content-Type", "application/json; charset=utf-8")

    ctx = None
    if url.lower().startswith("https://") and not verify_tls:
        ctx = ssl._create_unverified_context()

    with urllib.request.urlopen(req, timeout=int(timeout_sec), context=ctx) as resp:
        raw = resp.read() or b""
        text = raw.decode("utf-8", errors="replace")
        return int(getattr(resp, "status", resp.getcode())), text[:2000]


def _maybe_notify_webhook_text(*, cfg: Dict[str, Any], text: str, request_timeout_sec: int) -> None:
    wh = _get_webhook_cfg(cfg)
    if not bool(wh.get("enabled", False)):
        return

    url = str(wh.get("url") or "").strip()
    if not url:
        return

    verify_tls = bool(wh.get("verify_tls", True))
    timeout_sec = int(wh.get("timeout_sec", request_timeout_sec or 10))
    payload = {"text": str(text)}

    try:
        code, _resp = _http_post_json(url=url, payload=payload, timeout_sec=timeout_sec, verify_tls=verify_tls)
        print(f"WebHook notified: HTTP {code}")
    except Exception as e:
        print(f"WARN: failed to notify webhook: {e}", file=sys.stderr)


def _feishu_notify_user(*, chat_id: str, markdown: str, feishu_cfg: Dict[str, Any]) -> bool:
    """通过飞书 Bot 发送 IM 通知（使用 tenant_access_token，无需每用户授权）。
    chat_id 可以是飞书 chat_id（oc_xxx，群聊）或用户 open_id（ou_xxx，个人）。
    要求 Bot 已被添加到目标群聊 / 用户已与 Bot 交互以获取正确的 open_id。
    返回 bool：是否发送成功（供调用方决定是否回退到群聊）。"""
    import urllib.request as _ur
    try:
        oauth_cfg = (feishu_cfg.get("oauth") or {}) if isinstance(feishu_cfg.get("oauth"), dict) else {}
        # 发 IM 消息必须用「机器人应用」凭证：飞书机器人触发的 open_id 是机器人
        # cli_a974 维度，若用 cli_a928（免登/wiki 应用）的 token 会报 99992361 open_id cross app。
        # 优先 FEISHU_BOT_APP_ID/SECRET，回退 FEISHU_APP_ID/SECRET（旧行为，向后兼容）。
        import os as _os
        bot_app_id = _os.environ.get("FEISHU_BOT_APP_ID", "").strip()
        bot_app_secret = _os.environ.get("FEISHU_BOT_APP_SECRET", "").strip()
        app_id = bot_app_id or _feishu_resolve_app_id(oauth_cfg)
        app_secret = bot_app_secret or _feishu_resolve_app_secret(oauth_cfg)
        if not app_id or not app_secret:
            print(f"[Notify] Bot skipped: feishu.oauth.app_id/app_secret not set "
                  f"(set env FEISHU_BOT_APP_ID/FEISHU_BOT_APP_SECRET 或 FEISHU_APP_ID/FEISHU_APP_SECRET)", file=sys.stderr)
            return False
        token, _ = _feishu_get_tenant_access_token_internal(
            app_id=app_id, app_secret=app_secret, timeout_sec=10)
        content = json.dumps({"text": markdown}, ensure_ascii=False)
        receive_id_type = "open_id" if chat_id.startswith("ou_") else "chat_id"
        data = json.dumps({"receive_id": chat_id, "msg_type": "text", "content": content}, ensure_ascii=False).encode("utf-8")
        req = _ur.Request(
            f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={receive_id_type}",
            data=data,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with _ur.urlopen(req, timeout=15) as resp:
            resp_data = json.loads(resp.read().decode("utf-8", errors="replace"))
        if resp_data.get("code") == 0:
            print(f"[Notify] Bot message sent to {receive_id_type}={chat_id}")
            return True
        print(f"[Notify] Bot send FAILED to {receive_id_type}={chat_id}: "
              f"code={resp_data.get('code')} msg={resp_data.get('msg', 'unknown')[:200]}", file=sys.stderr)
        return False
    except _ur.HTTPError as e:
        # 读取响应体，暴露飞书返回的具体 code/msg（HTTPError 不抛 body，需手动读）
        try:
            _body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
        except Exception:
            _body = ""
        print(f"[Notify] Bot send EXCEPTION to {chat_id}: HTTPError {e.code} {_body[:300]}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"[Notify] Bot send EXCEPTION to {chat_id}: {type(e).__name__}: {e}", file=sys.stderr)
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# [DEPRECATED] 以下 _feishu_* 函数已由 lark_cli_adapter.py 替代。
# 仅保留用于 --feishu-preauth / --feishu-refresh 等边缘 CLI 模式。
# 主流水线中的飞书文档生成已迁移到 lark_publish_release_document()。
# 未来版本将逐步移除这些函数。
# ═══════════════════════════════════════════════════════════════════════════════

def _feishu_user_token_from_cfg(cfg: Dict[str, Any]) -> str:
    tok = str(cfg.get("user_access_token") or "").strip()
    if tok:
        return tok
    env_name = str(cfg.get("user_access_token_env") or "FEISHU_USER_ACCESS_TOKEN").strip() or "FEISHU_USER_ACCESS_TOKEN"
    return (os.environ.get(env_name) or "").strip()


def _feishu_oauth_normalize_scopes(scopes_raw: str) -> str:
    s = str(scopes_raw or "").strip()
    if not s:
        return ""
    parts: List[str] = []
    for chunk in s.replace(",", " ").split():
        c = chunk.strip()
        if c:
            parts.append(c)
    # OAuth2 scope is space-delimited.
    return " ".join(dict.fromkeys(parts))


def _feishu_oauth_build_authorize_url(*, app_id: str, redirect_uri: str, state: str, scope: str = "") -> str:
    q = {
        "app_id": app_id,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    if str(scope or "").strip():
        q["scope"] = str(scope).strip()
    return "https://open.feishu.cn/open-apis/authen/v1/index?" + urllib.parse.urlencode(q)


class _FeishuOAuthCallbackHandler(BaseHTTPRequestHandler):
    server_version = "FeishuOAuth/1.0"

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        callback_path = getattr(self.server, "_callback_path", "/callback")  # type: ignore[attr-defined]
        if parsed.path != callback_path:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")
            return

        qs = urllib.parse.parse_qs(parsed.query)
        code = (qs.get("code") or [""])[0]
        state = (qs.get("state") or [""])[0]

        self.server._result = (code, state)  # type: ignore[attr-defined]

        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("OK. You can close this tab and return to the terminal.\n".encode("utf-8"))

    def log_message(self, fmt: str, *args):
        # Keep quiet.
        return


def _feishu_oauth_wait_for_code(*, host: str, port: int, callback_path: str, timeout_sec: int) -> Tuple[str, str]:
    httpd = HTTPServer((host, port), _FeishuOAuthCallbackHandler)
    httpd._result = ("", "")  # type: ignore[attr-defined]
    httpd._callback_path = str(callback_path or "/callback")  # type: ignore[attr-defined]

    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()

    try:
        start = time.time()
        timeout_i = int(timeout_sec)
        deadline = None if timeout_i <= 0 else (start + float(timeout_i))
        while True:
            code, state = httpd._result  # type: ignore[attr-defined]
            if code:
                return str(code), str(state)
            if deadline is not None and time.time() >= deadline:
                raise TimeoutError("Timed out waiting for OAuth callback.")
            time.sleep(0.1)
    finally:
        httpd.shutdown()


def _feishu_get_tenant_access_token_internal(*, app_id: str, app_secret: str, timeout_sec: int) -> Tuple[str, int]:
    if not app_id or not app_secret:
        raise RuntimeError("Missing app_id/app_secret for tenant_access_token")
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    resp = _http_json(
        method="POST",
        url=url,
        headers={},
        body={"app_id": app_id, "app_secret": app_secret},
        timeout_sec=timeout_sec,
    )
    if int(resp.get("code") or 0) != 0:
        raise RuntimeError(f"Feishu tenant_access_token failed: {resp}")
    token = str(resp.get("tenant_access_token") or "").strip()
    expire = int(resp.get("expire") or 0)
    if not token or not expire:
        raise RuntimeError(f"Feishu tenant_access_token missing fields: {resp}")
    expires_at = int(time.time()) + int(expire)
    return token, expires_at


def _feishu_get_tenant_access_token_cached(*, cfg_path: Path, feishu_cfg: Dict[str, Any], timeout_sec: int) -> str:
    oauth_cfg = (feishu_cfg.get("oauth") or {}) if isinstance(feishu_cfg.get("oauth"), dict) else {}
    from credential_resolver import resolve_app_secret
    app_id = _feishu_resolve_app_id(oauth_cfg)
    app_secret = resolve_app_secret(str(oauth_cfg.get("app_secret") or "").strip())
    cache_file = _feishu_tenant_token_cache_file(cfg_path=cfg_path, feishu_cfg=feishu_cfg)
    tok, _exp = _feishu_try_load_cached_tenant_token(cache_file=cache_file)
    if tok:
        return tok
    token, expires_at = _feishu_get_tenant_access_token_internal(app_id=app_id, app_secret=app_secret, timeout_sec=timeout_sec)
    _feishu_save_cached_tenant_token(cache_file=cache_file, tenant_access_token=token, expires_at=expires_at)
    return token


def _feishu_resolve_app_id(oauth_cfg: Dict[str, Any]) -> str:
    """Resolve Feishu app_id: env FEISHU_APP_ID > config value"""
    import os as _os
    env_val = _os.environ.get("FEISHU_APP_ID", "").strip()
    if env_val:
        return env_val
    return str(oauth_cfg.get("app_id") or "").strip()


def _feishu_resolve_app_secret(oauth_cfg: Dict[str, Any]) -> str:
    """Resolve Feishu app_secret: env FEISHU_APP_SECRET > config value"""
    import os as _os
    env_val = _os.environ.get("FEISHU_APP_SECRET", "").strip()
    if env_val:
        return env_val
    return str(oauth_cfg.get("app_secret") or "").strip()


def _feishu_oauth_exchange_code_for_user_token(
    *,
    app_id: str,
    app_secret: str,
    code: str,
    redirect_uri: str,
    timeout_sec: int,
) -> Dict[str, Any]:
    # 1. Fetch tenant_access_token (required for internal apps 'cli_xxx')
    tenant_token: Optional[str] = None
    try:
        t, _ = _feishu_get_tenant_access_token_internal(app_id=app_id, app_secret=app_secret, timeout_sec=timeout_sec)
        tenant_token = t
    except Exception as e:
        print(f"WARN: Failed to get tenant_access_token for code exchange: {e}", file=sys.stderr)

    attempts = []
    
    # Internal app flow (priority if tenant_token available)
    if tenant_token:
        attempts.append((
            "https://open.feishu.cn/open-apis/authen/v1/access_token",
            {"grant_type": "authorization_code", "code": code},
            {"Authorization": f"Bearer {tenant_token}"}
        ))
        attempts.append((
            "https://open.feishu.cn/open-apis/authen/v2/oauth/token",
            {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
            {"Authorization": f"Bearer {tenant_token}"}
        ))

    # Store app flow or fallback (client_id/secret in body)
    attempts.extend([
        (
            "https://open.feishu.cn/open-apis/authen/v2/oauth/token",
            {
                "grant_type": "authorization_code",
                "client_id": app_id,
                "client_secret": app_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
            {}
        ),
        (
            "https://open.feishu.cn/open-apis/authen/v1/oidc/access_token",
            {
                "grant_type": "authorization_code",
                "client_id": app_id,
                "client_secret": app_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
            {}
        ),
    ])

    last_err: Optional[Exception] = None
    for url, payload, headers in attempts:
        try:
            resp = _http_json(method="POST", url=url, headers=headers, body=payload, timeout_sec=timeout_sec)
        except Exception as e:
            last_err = e
            continue

        # Wrapper: {code,msg,data:{...}}
        if isinstance(resp, dict) and "code" in resp and "data" in resp:
            if int(resp.get("code") or 0) != 0:
                last_err = RuntimeError(f"Token exchange failed at {url}: {resp}")
                continue
            return resp

        # Plain OAuth JSON
        if isinstance(resp, dict) and ("access_token" in resp or "user_access_token" in resp):
            return resp

        last_err = RuntimeError(f"Unrecognized token response at {url}: {resp}")

    if last_err:
        raise last_err
    raise RuntimeError("Token exchange failed: unknown error")


def _jwt_exp_ts(token: str) -> int:
    """Best-effort parse JWT exp (seconds since epoch). Returns 0 if unknown."""
    t = str(token or "").strip()
    parts = t.split(".")
    if len(parts) != 3:
        return 0
    payload_b64 = parts[1]
    # base64url padding
    pad = "=" * ((4 - len(payload_b64) % 4) % 4)
    try:
        raw = base64.urlsafe_b64decode((payload_b64 + pad).encode("utf-8"))
        data = json.loads(raw.decode("utf-8", errors="replace"))
        exp = int(data.get("exp") or 0)
        return exp if exp > 0 else 0
    except Exception:
        return 0


def _feishu_extract_oauth_token_fields(resp: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize token exchange/refresh response to a common dict."""
    if isinstance(resp, dict) and isinstance(resp.get("data"), dict):
        data = resp.get("data") or {}
    else:
        data = resp or {}

    access_token = str(data.get("access_token") or data.get("user_access_token") or "").strip()
    refresh_token = str(data.get("refresh_token") or "").strip()
    expires_in = data.get("expires_in")
    refresh_expires_in = data.get("refresh_expires_in")
    try:
        expires_in_i = int(expires_in) if expires_in is not None else 0
    except Exception:
        expires_in_i = 0
    try:
        refresh_expires_in_i = int(refresh_expires_in) if refresh_expires_in is not None else 0
    except Exception:
        refresh_expires_in_i = 0

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": expires_in_i,
        "refresh_expires_in": refresh_expires_in_i,
    }


def _feishu_oauth_refresh_user_access_token(*, app_id: str, app_secret: str, refresh_token: str, timeout_sec: int) -> Dict[str, Any]:
    # Use the same endpoint family as initial exchange.
    url = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
    payload = {
        "grant_type": "refresh_token",
        "client_id": app_id,
        "client_secret": app_secret,
        "refresh_token": str(refresh_token),
    }
    resp = _http_json(method="POST", url=url, headers={}, body=payload, timeout_sec=timeout_sec)
    # Wrapper: {code,msg,data:{...}}
    if isinstance(resp, dict) and "code" in resp and "data" in resp:
        if int(resp.get("code") or 0) != 0:
            raise RuntimeError(f"Token refresh failed: {resp}")
    return resp


def _feishu_maybe_save_user_oauth_tokens(
    *,
    cfg_path: Path,
    cfg_raw: Dict[str, Any],
    access_token: str,
    refresh_token: str,
    access_expires_at: int,
    refresh_expires_at: int,
    enabled: bool,
) -> None:
    _feishu_sync_user_oauth_state_across_configs(
        cfg_path=cfg_path,
        cfg_raw=cfg_raw,
        access_token=access_token,
        refresh_token=refresh_token,
        access_expires_at=access_expires_at,
        refresh_expires_at=refresh_expires_at,
        enabled=enabled,
    )


def _feishu_sync_user_oauth_state_across_configs(
    *,
    cfg_path: Path,
    cfg_raw: Dict[str, Any],
    access_token: str,
    refresh_token: str,
    access_expires_at: int,
    refresh_expires_at: int,
    enabled: bool,
) -> None:
    if not enabled:
        return

    def _ensure_feishu_section(raw_cfg: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        if not isinstance(raw_cfg.get("feishu"), dict):
            raw_cfg["feishu"] = {}
        fei = raw_cfg["feishu"]  # type: ignore[assignment]
        if not isinstance(fei.get("oauth"), dict):
            fei["oauth"] = {}
        oauth = fei["oauth"]  # type: ignore[assignment]
        return fei, oauth

    def _apply_auth_state(raw_cfg: Dict[str, Any]) -> bool:
        fei, oauth = _ensure_feishu_section(raw_cfg)
        changed = False

        if access_token and fei.get("user_access_token") != access_token:
            fei["user_access_token"] = access_token  # type: ignore[index]
            changed = True
        if refresh_token and fei.get("user_refresh_token") != refresh_token:
            fei["user_refresh_token"] = refresh_token  # type: ignore[index]
            changed = True
        if int(access_expires_at or 0) > 0 and int(fei.get("user_access_token_expires_at") or 0) != int(access_expires_at):
            fei["user_access_token_expires_at"] = int(access_expires_at)  # type: ignore[index]
            changed = True
        if int(refresh_expires_at or 0) > 0 and int(fei.get("user_refresh_token_expires_at") or 0) != int(refresh_expires_at):
            fei["user_refresh_token_expires_at"] = int(refresh_expires_at)  # type: ignore[index]
            changed = True

        saved_at = int(time.time())
        if int(fei.get("user_token_saved_at") or 0) != saved_at:
            fei["user_token_saved_at"] = saved_at  # type: ignore[index]
            changed = True

        for key, value in (
            ("open_browser", True),
            ("auto_oauth_on_99991677", True),
            ("auto_refresh_user_token_on_expiry", True),
            ("auto_save_user_access_token", True),
        ):
            if oauth.get(key) != value:
                oauth[key] = value  # type: ignore[index]
                changed = True

        return changed

    current_app_id = _feishu_resolve_app_id(((cfg_raw.get("feishu") or {}).get("oauth") or {}))
    _apply_auth_state(cfg_raw)
    # Preserve base_config from original file (it was stripped during read_json_with_base merge)
    try:
        original_raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        if isinstance(original_raw, dict):
            for base_key in ("base_config", "base_configs"):
                if base_key in original_raw:
                    cfg_raw[base_key] = original_raw[base_key]
    except Exception:
        pass
    cfg_path.write_text(json.dumps(cfg_raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if not current_app_id:
        return

    for peer_path in sorted(cfg_path.parent.glob("gqf_*.json")):
        try:
            if peer_path.resolve() == cfg_path.resolve():
                continue
        except Exception:
            if str(peer_path) == str(cfg_path):
                continue

        try:
            peer_raw = json.loads(peer_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(peer_raw, dict):
            continue

        peer_feishu = peer_raw.get("feishu")
        if not isinstance(peer_feishu, dict):
            continue
        peer_oauth = peer_feishu.get("oauth")
        if not isinstance(peer_oauth, dict):
            continue

        peer_app_id = _feishu_resolve_app_id(peer_oauth)
        if peer_app_id != current_app_id:
            continue

        if _apply_auth_state(peer_raw):
            peer_path.write_text(json.dumps(peer_raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _feishu_oauth_get_user_access_token_localhost(
    *,
    oauth_cfg: Dict[str, Any],
    timeout_sec: int,
    notify_cfg: Optional[Dict[str, Any]] = None,
) -> str:
    app_id = _feishu_resolve_app_id(oauth_cfg)
    app_secret = _feishu_resolve_app_secret(oauth_cfg)
    redirect_uri = str(oauth_cfg.get("redirect_uri") or "http://localhost:8000/callback").strip()
    scopes = _feishu_oauth_normalize_scopes(str(oauth_cfg.get("scopes") or ""))
    open_browser = True
    strict_state = bool(oauth_cfg.get("strict_state", True))
    bind_localhost_only = bool(oauth_cfg.get("bind_localhost_only", True))

    if not app_id or not app_secret:
        raise RuntimeError("Feishu OAuth enabled but oauth.app_id/app_secret is missing in config or env FEISHU_APP_ID/FEISHU_APP_SECRET")

    u = urllib.parse.urlsplit(redirect_uri)
    host = u.hostname or "localhost"
    port = int(u.port or 8000)
    callback_path = u.path or "/callback"

    state = secrets.token_urlsafe(16)
    auth_url = _feishu_oauth_build_authorize_url(app_id=app_id, redirect_uri=redirect_uri, state=state, scope=scopes)

    print("Feishu OAuth: open this URL in a browser and authorize:")
    print(auth_url)

    # Optional: push the auth URL to Feishu via the configured webhook.
    # NOTE: if redirect_uri is localhost, the link must be opened on the same machine
    # that is running this script (opening on mobile will redirect to mobile localhost).
    if notify_cfg is not None:
        try:
            tips = "(open on the SAME PC running the script)" if (u.hostname in ("localhost", "127.0.0.1") or "localhost" in redirect_uri) else ""
            _maybe_notify_webhook_text(
                cfg=notify_cfg,
                text="Feishu OAuth required\n" + tips + "\n" + auth_url,
                request_timeout_sec=10,
            )
        except Exception:
            pass

    if open_browser:
        try:
            webbrowser.open(auth_url)
        except Exception:
            pass

    bind_host = host
    if bind_localhost_only and host in ("localhost", "127.0.0.1", "::1"):
        bind_host = "127.0.0.1"

    print(f"Feishu OAuth: waiting for callback on {host}:{port}{callback_path} ...")
    code, got_state = _feishu_oauth_wait_for_code(
        host=bind_host,
        port=port,
        callback_path=callback_path,
        timeout_sec=int(oauth_cfg.get("timeout_sec") or timeout_sec),
    )
    if got_state and got_state != state:
        msg = "Feishu OAuth: state mismatch (possible multiple tabs or spoofed callback)."
        if strict_state:
            raise RuntimeError(msg)
        print("WARN: " + msg + " Proceeding because strict_state=false.", file=sys.stderr)

    token_resp = _feishu_oauth_exchange_code_for_user_token(
        app_id=app_id,
        app_secret=app_secret,
        code=code,
        redirect_uri=redirect_uri,
        timeout_sec=int(oauth_cfg.get("token_timeout_sec") or timeout_sec),
    )

    fields = _feishu_extract_oauth_token_fields(token_resp if isinstance(token_resp, dict) else {})

    access_token = str(fields.get("access_token") or "").strip()
    if not access_token:
        raise RuntimeError(f"Feishu OAuth: token exchange succeeded but access_token missing: {token_resp}")
    return access_token


def _feishu_oauth_get_user_tokens_localhost(
    *,
    oauth_cfg: Dict[str, Any],
    timeout_sec: int,
    notify_cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run interactive OAuth and return token fields (access/refresh/expiry)."""

    app_id = _feishu_resolve_app_id(oauth_cfg)
    app_secret = _feishu_resolve_app_secret(oauth_cfg)
    redirect_uri = str(oauth_cfg.get("redirect_uri") or "http://localhost:8000/callback").strip()
    if not app_id or not app_secret:
        raise RuntimeError("Feishu OAuth enabled but oauth.app_id/app_secret is missing in config or env FEISHU_APP_ID/FEISHU_APP_SECRET")

    # Use the existing function to drive browser + callback + exchange.
    # Then immediately refresh via refresh_token path if the exchange returned it.
    # (We cannot access the raw token_resp from the wrapper, so re-run exchange inline here.)

    u = urllib.parse.urlsplit(redirect_uri)
    host = u.hostname or "localhost"
    port = int(u.port or 8000)
    callback_path = u.path or "/callback"
    scopes = _feishu_oauth_normalize_scopes(str(oauth_cfg.get("scopes") or ""))
    open_browser = True
    strict_state = bool(oauth_cfg.get("strict_state", True))
    bind_localhost_only = bool(oauth_cfg.get("bind_localhost_only", True))

    state = secrets.token_urlsafe(16)
    auth_url = _feishu_oauth_build_authorize_url(app_id=app_id, redirect_uri=redirect_uri, state=state, scope=scopes)

    print("Feishu OAuth: open this URL in a browser and authorize:")
    print(auth_url)
    if notify_cfg is not None:
        try:
            tips = "(open on the SAME PC running the script)" if (u.hostname in ("localhost", "127.0.0.1") or "localhost" in redirect_uri) else ""
            _maybe_notify_webhook_text(cfg=notify_cfg, text="Feishu OAuth required\n" + tips + "\n" + auth_url, request_timeout_sec=10)
        except Exception:
            pass

    if open_browser:
        try:
            webbrowser.open(auth_url)
        except Exception:
            pass

    bind_host = host
    if bind_localhost_only and host in ("localhost", "127.0.0.1", "::1"):
        bind_host = "127.0.0.1"

    print(f"Feishu OAuth: waiting for callback on {host}:{port}{callback_path} ...")
    code, got_state = _feishu_oauth_wait_for_code(host=bind_host, port=port, callback_path=callback_path, timeout_sec=int(oauth_cfg.get("timeout_sec") or timeout_sec))
    if got_state and got_state != state:
        msg = "Feishu OAuth: state mismatch (possible multiple tabs or spoofed callback)."
        if strict_state:
            raise RuntimeError(msg)
        print("WARN: " + msg + " Proceeding because strict_state=false.", file=sys.stderr)

    token_resp = _feishu_oauth_exchange_code_for_user_token(
        app_id=app_id,
        app_secret=app_secret,
        code=code,
        redirect_uri=redirect_uri,
        timeout_sec=int(oauth_cfg.get("token_timeout_sec") or timeout_sec),
    )
    fields = _feishu_extract_oauth_token_fields(token_resp if isinstance(token_resp, dict) else {})
    access_token = str(fields.get("access_token") or "").strip()
    refresh_token = str(fields.get("refresh_token") or "").strip()
    if not access_token:
        raise RuntimeError(f"Feishu OAuth: token exchange succeeded but access_token missing: {token_resp}")

    if not refresh_token:
        # Some Feishu auth flows return short-lived access_token without refresh_token.
        # Do not print token values; only print structural hints.
        keys = []
        try:
            if isinstance(token_resp, dict):
                keys = sorted([str(k) for k in token_resp.keys()])
        except Exception:
            keys = []
        data_keys = []
        try:
            if isinstance(token_resp, dict) and isinstance(token_resp.get("data"), dict):
                data_keys = sorted([str(k) for k in (token_resp.get("data") or {}).keys()])
        except Exception:
            data_keys = []
        print(
            "WARN: Feishu OAuth response did not include refresh_token; "
            "long-term auto-refresh will not work. "
            "Check whether the app/tenant supports refresh tokens for this OAuth flow (offline_access). "
            f"resp_keys={keys} data_keys={data_keys}",
            file=sys.stderr,
        )

    now = int(time.time())
    access_expires_at = now + int(fields.get("expires_in") or 0) if int(fields.get("expires_in") or 0) > 0 else _jwt_exp_ts(access_token)
    refresh_expires_at = now + int(fields.get("refresh_expires_in") or 0) if int(fields.get("refresh_expires_in") or 0) > 0 else 0

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "access_expires_at": int(access_expires_at or 0),
        "refresh_expires_at": int(refresh_expires_at or 0),
        "raw": token_resp,
    }


def _feishu_maybe_save_user_access_token(*, cfg_path: Path, cfg_raw: Dict[str, Any], access_token: str, enabled: bool) -> None:
    if not enabled or not access_token:
        return
    _feishu_sync_user_oauth_state_across_configs(
        cfg_path=cfg_path,
        cfg_raw=cfg_raw,
        access_token=access_token,
        refresh_token="",
        access_expires_at=0,
        refresh_expires_at=0,
        enabled=enabled,
    )


def _feishu_user_refresh_token_from_cfg(cfg: Dict[str, Any]) -> str:
    tok = str(cfg.get("user_refresh_token") or "").strip()
    if tok:
        return tok
    env_name = str(cfg.get("user_refresh_token_env") or "FEISHU_USER_REFRESH_TOKEN").strip() or "FEISHU_USER_REFRESH_TOKEN"
    return (os.environ.get(env_name) or "").strip()


def _feishu_user_access_token_expires_at_from_cfg(cfg: Dict[str, Any]) -> int:
    v = cfg.get("user_access_token_expires_at")
    try:
        return int(v) if v is not None else 0
    except Exception:
        return 0


def _feishu_user_refresh_token_expires_at_from_cfg(cfg: Dict[str, Any]) -> int:
    v = cfg.get("user_refresh_token_expires_at")
    try:
        return int(v) if v is not None else 0
    except Exception:
        return 0


def _feishu_ensure_fresh_user_access_token(
    *,
    cfg_path: Path,
    cfg_raw: Dict[str, Any],
    feishu_cfg: Dict[str, Any],
    feishu_oauth_cfg: Dict[str, Any],
    timeout_sec: int,
    auto_save: bool,
    notify_cfg: Optional[Dict[str, Any]] = None,
) -> str:
    """Return a usable user_access_token; refresh automatically when possible."""

    # 1) Existing access token
    access_token = _feishu_user_token_from_cfg(feishu_cfg)
    expires_at = _feishu_user_access_token_expires_at_from_cfg(feishu_cfg)
    if expires_at <= 0 and access_token:
        # Try JWT exp fallback.
        expires_at = _jwt_exp_ts(access_token)

    now = int(time.time())
    # Refresh a bit early.
    refresh_skew = int(feishu_oauth_cfg.get("refresh_skew_sec") or 300)

    if access_token and (expires_at <= 0 or now < (expires_at - refresh_skew)):
        return access_token

    # 2) Refresh token path (non-interactive)
    refresh_token = _feishu_user_refresh_token_from_cfg(feishu_cfg)
    refresh_exp_at = _feishu_user_refresh_token_expires_at_from_cfg(feishu_cfg)
    if refresh_token and (refresh_exp_at <= 0 or now < (refresh_exp_at - 60)):
        app_id = str(feishu_oauth_cfg.get("app_id") or "").strip()
        app_secret = str(feishu_oauth_cfg.get("app_secret") or "").strip()
        if app_id and app_secret:
            try:
                resp = _feishu_oauth_refresh_user_access_token(
                    app_id=app_id,
                    app_secret=app_secret,
                    refresh_token=refresh_token,
                    timeout_sec=int(feishu_oauth_cfg.get("token_timeout_sec") or timeout_sec),
                )
                fields = _feishu_extract_oauth_token_fields(resp if isinstance(resp, dict) else {})
                new_access = str(fields.get("access_token") or "").strip()
                new_refresh = str(fields.get("refresh_token") or refresh_token).strip()
                if not new_access:
                    raise RuntimeError(f"refresh succeeded but access_token missing: {resp}")

                new_expires_at = 0
                if int(fields.get("expires_in") or 0) > 0:
                    new_expires_at = now + int(fields.get("expires_in") or 0)
                else:
                    new_expires_at = _jwt_exp_ts(new_access)

                new_refresh_expires_at = 0
                if int(fields.get("refresh_expires_in") or 0) > 0:
                    new_refresh_expires_at = now + int(fields.get("refresh_expires_in") or 0)

                feishu_cfg["user_access_token"] = new_access
                feishu_cfg["user_refresh_token"] = new_refresh
                if new_expires_at > 0:
                    feishu_cfg["user_access_token_expires_at"] = int(new_expires_at)
                if new_refresh_expires_at > 0:
                    feishu_cfg["user_refresh_token_expires_at"] = int(new_refresh_expires_at)

                _feishu_maybe_save_user_oauth_tokens(
                    cfg_path=cfg_path,
                    cfg_raw=cfg_raw,
                    access_token=new_access,
                    refresh_token=new_refresh,
                    access_expires_at=new_expires_at,
                    refresh_expires_at=new_refresh_expires_at,
                    enabled=bool(auto_save),
                )

                return new_access
            except Exception as e:
                print(f"WARN: Feishu OAuth refresh_token refresh failed; will fall back to interactive OAuth if enabled: {e}", file=sys.stderr)

    # 3) Interactive OAuth fallback (only if enabled)
    if bool(feishu_oauth_cfg.get("enabled", False)):
        tok = _feishu_oauth_get_user_tokens_localhost(
            oauth_cfg=feishu_oauth_cfg,
            timeout_sec=timeout_sec,
            notify_cfg=notify_cfg,
        )
        new_access = str(tok.get("access_token") or "").strip()
        new_refresh = str(tok.get("refresh_token") or "").strip()
        feishu_cfg["user_access_token"] = new_access
        if new_refresh:
            feishu_cfg["user_refresh_token"] = new_refresh
        if int(tok.get("access_expires_at") or 0) > 0:
            feishu_cfg["user_access_token_expires_at"] = int(tok.get("access_expires_at") or 0)
        if int(tok.get("refresh_expires_at") or 0) > 0:
            feishu_cfg["user_refresh_token_expires_at"] = int(tok.get("refresh_expires_at") or 0)

        _feishu_maybe_save_user_oauth_tokens(
            cfg_path=cfg_path,
            cfg_raw=cfg_raw,
            access_token=new_access,
            refresh_token=new_refresh,
            access_expires_at=int(tok.get("access_expires_at") or 0),
            refresh_expires_at=int(tok.get("refresh_expires_at") or 0),
            enabled=bool(auto_save),
        )
        return new_access

    return access_token


def _feishu_tenant_token_cache_file(*, cfg_path: Path, feishu_cfg: Dict[str, Any]) -> Path:
    p = str(feishu_cfg.get("tenant_access_token_cache_file") or "").strip()
    if p:
        pp = Path(p).expanduser()
        if not pp.is_absolute():
            pp = (cfg_path.parent / pp)
        return pp.resolve()
    return (cfg_path.parent / ".feishu_tenant_access_token.json").resolve()


def _feishu_try_load_cached_tenant_token(*, cache_file: Path) -> Tuple[str, int]:
    try:
        if not cache_file.exists():
            return "", 0
        raw = json.loads(cache_file.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return "", 0
        token = str(raw.get("tenant_access_token") or raw.get("access_token") or "").strip()
        expires_at = int(raw.get("expires_at") or 0)
        if not token or not expires_at:
            return "", 0
        if int(time.time()) >= (expires_at - 60):
            return "", 0
        return token, expires_at
    except Exception:
        return "", 0


def _feishu_save_cached_tenant_token(*, cache_file: Path, tenant_access_token: str, expires_at: int) -> None:
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "tenant_access_token": str(tenant_access_token),
        "expires_at": int(expires_at),
        "saved_at": int(time.time()),
    }
    cache_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _feishu_extract_docx_token(*, value: str) -> str:
    s = str(value or "").strip()
    if not s:
        return ""
    if s.startswith("http://") or s.startswith("https://"):
        # https://<domain>/docx/<token>
        m = re.search(r"/docx/([A-Za-z0-9]+)", s)
        if m:
            return m.group(1)
    return s


def _feishu_extract_drive_folder_token(*, value: str) -> str:
    s = str(value or "").strip()
    if not s:
        return ""
    if s.startswith("http://") or s.startswith("https://"):
        # https://<domain>/drive/folder/<token>
        m = re.search(r"/drive/folder/([A-Za-z0-9]+)", s)
        if m:
            return m.group(1)
    return s


def _feishu_docx_copy_template_best_effort(
    *,
    user_access_token: str,
    template_document_id: str,
    name: str,
    target_folder_token: str,
    timeout_sec: int,
) -> Optional[Tuple[str, str]]:
    """Try to copy a Docx template via Docx API (best-effort).

    Some tenants allow omitting folder_token to copy into app space.
    Returns (new_document_id, url) or None if unsupported/failed.
    """
    doc_id = str(template_document_id or "").strip()
    if not doc_id:
        return None
    url = f"https://open.feishu.cn/open-apis/docx/v1/documents/{quote(doc_id)}/copy"
    payload: Dict[str, Any] = {"title": str(name or "").strip()}
    folder = str(target_folder_token or "").strip()
    if folder:
        payload["folder_token"] = folder
    try:
        resp = _http_json(
            method="POST",
            url=url,
            headers={"Authorization": f"Bearer {user_access_token}"},
            body=payload,
            timeout_sec=timeout_sec,
        )
    except Exception as e:
        print(f"WARN: Feishu Docx copy (docx API) failed: {e}", file=sys.stderr)
        return None
    if int(resp.get("code") or 0) != 0:
        print(f"WARN: Feishu Docx copy (docx API) returned error: {resp}", file=sys.stderr)
        return None
    data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
    doc = data.get("document") if isinstance(data.get("document"), dict) else data
    new_id = str(doc.get("document_id") or doc.get("id") or data.get("document_id") or "").strip()
    link = str(doc.get("url") or doc.get("link") or data.get("url") or data.get("link") or "").strip()
    if not new_id:
        return None
    return new_id, link


def _feishu_drive_add_permission_member_by_email(
    *,
    user_access_token: str,
    file_token: str,
    file_type: str,
    email: str,
    perm: str,
    timeout_sec: int,
) -> Dict[str, Any]:
    url = f"https://open.feishu.cn/open-apis/drive/v1/permissions/{quote(str(file_token))}/members?type={urllib.parse.quote(str(file_type))}"
    perm2 = str(perm)
    # Some tenants/APIs accept only view/edit for files; normalize common alias.
    if perm2 == "full_access":
        perm2 = "edit"
    payload = {
        "member_type": "email",
        "member_id": str(email),
        "perm": perm2,
    }
    return _http_json(
        method="POST",
        url=url,
        headers={"Authorization": f"Bearer {user_access_token}"},
        body=payload,
        timeout_sec=timeout_sec,
    )


def _feishu_drive_add_permission_member_by_id(
    *,
    user_access_token: str,
    file_token: str,
    file_type: str,
    member_type: str,
    member_id: str,
    perm: str,
    timeout_sec: int,
) -> Dict[str, Any]:
    url = f"https://open.feishu.cn/open-apis/drive/v1/permissions/{quote(str(file_token))}/members?type={urllib.parse.quote(str(file_type))}"
    perm2 = str(perm)
    if perm2 == "full_access":
        perm2 = "edit"
    payload = {
        "member_type": str(member_type),
        "member_id": str(member_id),
        "perm": perm2,
    }
    return _http_json(
        method="POST",
        url=url,
        headers={"Authorization": f"Bearer {user_access_token}"},
        body=payload,
        timeout_sec=timeout_sec,
    )


def _feishu_contact_get_open_id_by_email(*, user_access_token: str, email: str, timeout_sec: int) -> str:
    """Resolve an email to open_id (user_id) for permission APIs.

    Many Feishu permission endpoints are most reliable with member_type=open_id/openid.
    """

    email2 = str(email or "").strip()
    if not email2:
        return ""

    url = "https://open.feishu.cn/open-apis/contact/v3/users/batch_get_id?user_id_type=open_id"
    body = {"emails": [email2]}
    resp = _http_json(
        method="POST",
        url=url,
        headers={"Authorization": f"Bearer {user_access_token}"},
        body=body,
        timeout_sec=timeout_sec,
    )
    if not isinstance(resp, dict) or int(resp.get("code") or 0) != 0:
        return ""
    data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
    users = data.get("user_list") if isinstance(data.get("user_list"), list) else []
    for u in users:
        if not isinstance(u, dict):
            continue
        if str(u.get("email") or "").strip().lower() == email2.lower():
            return str(u.get("user_id") or "").strip()
    return ""


def _feishu_drive_meta_batch_query_best_effort(
    *,
    user_access_token: str,
    token: str,
    timeout_sec: int,
    with_url: bool,
) -> Optional[Dict[str, Any]]:
    """Best-effort resolve Drive meta for a token.

    The Drive permission-member API requires a correct `type` query parameter.
    Tenants may expect `doc`, `docx`, or `file` for the same token.
    """

    url = "https://open.feishu.cn/open-apis/drive/v1/metas/batch_query"
    token2 = str(token or "").strip()
    if not token2:
        return None

    # Try common doc types for docx templates.
    for doc_type in ("docx", "doc", "file"):
        body = {
            "request_docs": [{"doc_token": token2, "doc_type": doc_type}],
            "with_url": bool(with_url),
        }
        try:
            resp = _http_json(
                method="POST",
                url=url,
                headers={"Authorization": f"Bearer {user_access_token}"},
                body=body,
                timeout_sec=timeout_sec,
            )
        except Exception:
            continue

        if not isinstance(resp, dict) or int(resp.get("code") or 0) != 0:
            continue
        data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
        metas = data.get("metas") if isinstance(data.get("metas"), list) else []
        if metas and isinstance(metas[0], dict):
            meta = metas[0]
            meta["_input_doc_type"] = doc_type
            return meta

    return None


def _feishu_drive_resolve_url_best_effort(*, user_access_token: str, token: str, timeout_sec: int) -> str:
    meta = _feishu_drive_meta_batch_query_best_effort(
        user_access_token=user_access_token,
        token=token,
        timeout_sec=timeout_sec,
        with_url=True,
    )
    if not isinstance(meta, dict):
        return ""
    return str(meta.get("url") or "").strip()


def _feishu_drive_add_permission_member_by_email_best_effort(
    *,
    user_access_token: str,
    file_token: str,
    preferred_file_type: str,
    email: str,
    perm: str,
    timeout_sec: int,
) -> Dict[str, Any]:
    """Add permission member and auto-fix mismatched file_type.

    We try, in order:
    - preferred_file_type (from config)
    - inferred type from drive metas/batch_query (docx/doc/file)
    - fallbacks: docx/doc/file
    """

    token2 = str(file_token or "").strip()
    if not token2:
        raise RuntimeError("Missing file_token")

    preferred = str(preferred_file_type or "").strip()
    candidates: List[str] = []
    if preferred:
        candidates.append(preferred)

    meta = _feishu_drive_meta_batch_query_best_effort(
        user_access_token=user_access_token,
        token=token2,
        timeout_sec=timeout_sec,
        with_url=False,
    )
    inferred = str((meta or {}).get("_input_doc_type") or "").strip()
    if inferred:
        candidates.append(inferred)

    # Known safe fallbacks for docx-ish resources.
    candidates.extend(["docx", "doc", "file"])

    # Deduplicate while preserving order
    seen: set[str] = set()
    ordered: List[str] = []
    for t in candidates:
        tt = str(t or "").strip()
        if not tt or tt in seen:
            continue
        seen.add(tt)
        ordered.append(tt)

    last_resp: Optional[Dict[str, Any]] = None
    last_exc: Optional[Exception] = None
    for t in ordered:
        try:
            resp = _feishu_drive_add_permission_member_by_email(
                user_access_token=user_access_token,
                file_token=token2,
                file_type=t,
                email=email,
                perm=perm,
                timeout_sec=timeout_sec,
            )
            last_resp = resp
            if int(resp.get("code") or 0) == 0:
                return resp
        except Exception as e:
            last_exc = e
            continue

    if last_resp is not None:
        return last_resp
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("Feishu add permission member failed")


def _feishu_drive_add_permission_member_best_effort(
    *,
    user_access_token: str,
    file_token: str,
    preferred_file_type: str,
    member_type: str,
    member_id: str,
    perm: str,
    timeout_sec: int,
) -> Dict[str, Any]:
    """Same as _feishu_drive_add_permission_member_by_email_best_effort but supports open_id."""

    token2 = str(file_token or "").strip()
    if not token2:
        raise RuntimeError("Missing file_token")

    preferred = str(preferred_file_type or "").strip()
    candidates: List[str] = []
    if preferred:
        candidates.append(preferred)

    meta = _feishu_drive_meta_batch_query_best_effort(
        user_access_token=user_access_token,
        token=token2,
        timeout_sec=timeout_sec,
        with_url=False,
    )
    inferred = str((meta or {}).get("_input_doc_type") or "").strip()
    if inferred:
        candidates.append(inferred)
    candidates.extend(["docx", "doc", "file"])

    seen: set[str] = set()
    ordered: List[str] = []
    for t in candidates:
        tt = str(t or "").strip()
        if not tt or tt in seen:
            continue
        seen.add(tt)
        ordered.append(tt)

    last_resp: Optional[Dict[str, Any]] = None
    last_exc: Optional[Exception] = None
    for t in ordered:
        try:
            resp = _feishu_drive_add_permission_member_by_id(
                user_access_token=user_access_token,
                file_token=token2,
                file_type=t,
                member_type=member_type,
                member_id=member_id,
                perm=perm,
                timeout_sec=timeout_sec,
            )
            last_resp = resp
            if int(resp.get("code") or 0) == 0:
                return resp
        except Exception as e:
            last_exc = e
            continue

    if last_resp is not None:
        return last_resp
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("Feishu add permission member failed")


def _feishu_copy_template_docx(
    *,
    user_access_token: str,
    template_file_token: str,
    target_folder_token: str,
    name: str,
    timeout_sec: int,
) -> Tuple[str, str]:
    url = f"https://open.feishu.cn/open-apis/drive/v1/files/{template_file_token}/copy"
    payload: Dict[str, Any] = {
        "name": name,
        "type": "docx",
    }
    if str(target_folder_token or "").strip():
        payload["folder_token"] = str(target_folder_token).strip()
    resp = _http_json(
        method="POST",
        url=url,
        headers={"Authorization": f"Bearer {user_access_token}"},
        body=payload,
        timeout_sec=timeout_sec,
    )
    if int(resp.get("code") or 0) != 0:
        raise RuntimeError(f"Feishu copy failed: {resp}")
    data = resp.get("data") or {}
    file_obj = data.get("file") or data
    token = str(file_obj.get("token") or file_obj.get("file_token") or "").strip()
    link = str(file_obj.get("url") or file_obj.get("link") or "").strip()
    if not token:
        raise RuntimeError(f"Feishu copy missing token: {resp}")
    return token, link


def _feishu_create_docx_best_effort(*, user_access_token: str, title: str, folder_token: str, timeout_sec: int) -> Tuple[str, str]:
    """Create a Docx using app/tenant token.

    Mirrors the approach in feishu_app_create_doc_and_share.py: omit folder_token to land in app space.
    Returns (doc_token/document_id, url).
    """

    title2 = str(title or "").strip() or "Untitled"
    folder2 = str(folder_token or "").strip()

    attempts: List[Tuple[str, Dict[str, Any]]] = [
        (
            "https://open.feishu.cn/open-apis/docx/v1/documents",
            {"title": title2, "folder_token": folder2} if folder2 else {"title": title2},
        ),
        (
            "https://open.feishu.cn/open-apis/drive/v1/files/create",
            {
                "type": "docx",
                "file_type": "docx",
                "title": title2,
                "name": title2,
                "folder_token": folder2,
            },
        ),
    ]

    last_resp: Optional[Dict[str, Any]] = None
    for url, payload in attempts:
        resp = _http_json(
            method="POST",
            url=url,
            headers={"Authorization": f"Bearer {user_access_token}"},
            body=payload,
            timeout_sec=timeout_sec,
        )
        last_resp = resp
        if isinstance(resp, dict) and "code" in resp and int(resp.get("code") or 0) != 0:
            continue
        data = resp.get("data") if isinstance(resp, dict) and isinstance(resp.get("data"), dict) else resp
        if not isinstance(data, dict):
            continue

        token = str(
            data.get("token")
            or (data.get("document") or {}).get("document_id")
            or (data.get("file") or {}).get("token")
            or (data.get("file") or {}).get("file_token")
            or data.get("document_id")
            or data.get("file_token")
            or ""
        ).strip()
        link = str(
            data.get("url")
            or data.get("link")
            or (data.get("document") or {}).get("url")
            or (data.get("file") or {}).get("url")
            or ""
        ).strip()

        if token:
            return token, link

    raise RuntimeError(f"Feishu create docx failed: {last_resp}")


def _feishu_convert_markdown_to_blocks(
    *,
    user_access_token: str,
    markdown: str,
    timeout_sec: int,
) -> Dict[str, Any]:
    url = "https://open.feishu.cn/open-apis/docx/v1/documents/blocks/convert"
    payload = {
        "content_type": "markdown",
        "content": markdown,
    }
    resp = _http_json(
        method="POST",
        url=url,
        headers={"Authorization": f"Bearer {user_access_token}"},
        body=payload,
        timeout_sec=timeout_sec,
    )
    if int(resp.get("code") or 0) != 0:
        raise RuntimeError(f"Feishu convert failed: {resp}")
    return resp


def _feishu_extract_descendant_payload(convert_resp: Dict[str, Any]) -> Dict[str, Any]:
    """Best-effort extraction of payload for the Create nested blocks API.

    The convert API response shape has changed across doc versions. This attempts
    a few common shapes and returns {children_id, descendants}.
    """

    data = convert_resp.get("data") if isinstance(convert_resp, dict) else None
    if isinstance(data, dict):
        # Common shape: data has descendants + children_id
        if isinstance(data.get("descendants"), list) and isinstance(data.get("children_id"), list):
            return {
                "children_id": data["children_id"],
                "descendants": data["descendants"],
            }

        # Another shape: data has blocks (list) and first_level_block_ids
        blocks = data.get("blocks")
        first = data.get("first_level_block_ids")
        if isinstance(blocks, list) and isinstance(first, list):
            by_id: Dict[str, Any] = {}
            for b in blocks:
                if isinstance(b, dict) and b.get("block_id"):
                    by_id[str(b["block_id"])] = b

            descendants: List[Dict[str, Any]] = []
            for k, v in by_id.items():
                if isinstance(v, dict):
                    descendants.append(v)
            # children_id for create-descendant is the *temporary* ids of first level.
            # If convert produced real ids, Feishu will still accept them as temporary
            # ids for mapping (server will remap). This is best-effort.
            return {
                "children_id": [str(x) for x in first],
                "descendants": descendants,
            }

    # Fallback: maybe convert returns these at top-level
    if isinstance(convert_resp.get("descendants"), list) and isinstance(convert_resp.get("children_id"), list):
        return {
            "children_id": convert_resp["children_id"],
            "descendants": convert_resp["descendants"],
        }

    raise RuntimeError(
        "Unrecognized Feishu convert response shape; cannot build descendant payload. "
        "Tip: run once and share the printed response keys from feishu.debug_dump_response=true."
    )


def _feishu_insert_blocks_descendant(
    *,
    user_access_token: str,
    document_id: str,
    parent_block_id: str,
    descendant_payload: Dict[str, Any],
    index: int,
    timeout_sec: int,
) -> Dict[str, Any]:
    url = (
        f"https://open.feishu.cn/open-apis/docx/v1/documents/{document_id}"
        f"/blocks/{parent_block_id}/descendant?document_revision_id=-1"
    )
    payload = {
        "index": int(index),
        "children_id": descendant_payload.get("children_id") or [],
        "descendants": descendant_payload.get("descendants") or [],
    }
    resp = _http_json(
        method="POST",
        url=url,
        headers={"Authorization": f"Bearer {user_access_token}"},
        body=payload,
        timeout_sec=timeout_sec,
    )
    if int(resp.get("code") or 0) != 0:
        raise RuntimeError(f"Feishu insert failed: {resp}")
    return resp


def _feishu_docx_list_all_blocks(
    *,
    user_access_token: str,
    document_id: str,
    timeout_sec: int,
) -> List[Dict[str, Any]]:
    """List all blocks in a Docx document (paged)."""

    items: List[Dict[str, Any]] = []
    page_token = ""
    while True:
        q = {
            "page_size": 500,
            "document_revision_id": -1,
        }
        if page_token:
            q["page_token"] = page_token
        url = f"https://open.feishu.cn/open-apis/docx/v1/documents/{quote(document_id)}/blocks?{urllib.parse.urlencode(q)}"
        resp = _http_json(
            method="GET",
            url=url,
            headers={"Authorization": f"Bearer {user_access_token}"},
            timeout_sec=timeout_sec,
        )
        if int(resp.get("code") or 0) != 0:
            raise RuntimeError(f"Feishu docx list blocks failed: {resp}")
        data = resp.get("data") or {}
        page_items = data.get("items") or []
        if isinstance(page_items, list):
            for it in page_items:
                if isinstance(it, dict) and it.get("block_id"):
                    items.append(it)
        if not bool(data.get("has_more")):
            break
        page_token = str(data.get("page_token") or "").strip()
        if not page_token:
            break
    return items


def _feishu_docx_document_has_any_content(
    *,
    user_access_token: str,
    document_id: str,
    timeout_sec: int,
) -> bool:
    blocks = _feishu_docx_list_all_blocks(
        user_access_token=user_access_token,
        document_id=document_id,
        timeout_sec=timeout_sec,
    )
    # Best-effort: a doc normally has a single root/page block when empty.
    return len(blocks) > 1


def _feishu_docx_find_root_page_block_id_best_effort(
    *,
    user_access_token: str,
    document_id: str,
    timeout_sec: int,
) -> str:
    """Return a block_id that supports descendant insertion.

    In Feishu Docx APIs, `document_id` is not always a valid `block_id` for
    descendant creation. Typically the root `page` block should be used.
    """

    doc_id = str(document_id or "").strip()
    if not doc_id:
        return ""

    blocks = _feishu_docx_list_all_blocks(
        user_access_token=user_access_token,
        document_id=doc_id,
        timeout_sec=timeout_sec,
    )
    if not blocks:
        return doc_id

    # Prefer a top-level page block.
    for b in blocks:
        if not isinstance(b, dict):
            continue
        if str(b.get("block_type") or "").strip().lower() != "page":
            continue
        parent = str(b.get("parent_id") or b.get("parent_block_id") or "").strip()
        if not parent:
            bid = str(b.get("block_id") or "").strip()
            if bid:
                return bid

    # Fallback: any page block.
    for b in blocks:
        if not isinstance(b, dict):
            continue
        if str(b.get("block_type") or "").strip().lower() == "page":
            bid = str(b.get("block_id") or "").strip()
            if bid:
                return bid

    # Final fallback: first block.
    bid0 = str(blocks[0].get("block_id") or "").strip() if isinstance(blocks[0], dict) else ""
    return bid0 or doc_id


def _feishu_docx_build_descendant_payload_from_template_blocks(
    *,
    template_blocks: List[Dict[str, Any]],
    template_document_id: str,
) -> Dict[str, Any]:
    if not template_blocks:
        raise RuntimeError("Template docx has no blocks; cannot clone content")

    by_id: Dict[str, Dict[str, Any]] = {}
    for b in template_blocks:
        if isinstance(b, dict) and b.get("block_id"):
            by_id[str(b["block_id"])] = b

    root_old_id = ""
    if template_document_id and template_document_id in by_id:
        root_old_id = template_document_id
    else:
        for b in template_blocks:
            if not isinstance(b, dict):
                continue
            if str(b.get("block_type") or "") == "page" and not (b.get("parent_id") or b.get("parent_block_id")):
                root_old_id = str(b.get("block_id") or "").strip()
                break
    if not root_old_id:
        root_old_id = str(template_blocks[0].get("block_id") or "").strip()

    def _children_ids(block: Dict[str, Any]) -> List[str]:
        for key in ("children", "children_id", "children_ids", "childrenIds"):
            v = block.get(key)
            if isinstance(v, list):
                return [str(x) for x in v if x]
        return []

    first_level_old = _children_ids(by_id.get(root_old_id, {}) if root_old_id in by_id else {})
    if not first_level_old:
        # Fallback: infer from parent_id ordering.
        for b in template_blocks:
            if not isinstance(b, dict):
                continue
            pid = str(b.get("parent_id") or b.get("parent_block_id") or "").strip()
            if pid == root_old_id and b.get("block_id"):
                first_level_old.append(str(b["block_id"]))

    # Map all non-root block ids to temporary ids.
    id_map: Dict[str, str] = {}
    for old_id in by_id.keys():
        if old_id == root_old_id:
            continue
        # Feishu accepts temporary ids; use hex to keep it compact.
        id_map[old_id] = secrets.token_hex(12)

    # Build descendants by reusing list-block items, removing fields that are
    # known to be server-generated.
    descendants: List[Dict[str, Any]] = []
    remove_keys = {
        "parent_id",
        "parent_block_id",
        "document_id",
        "document_revision_id",
        "revision_id",
        "create_time",
        "update_time",
        "is_delete",
    }

    for b in template_blocks:
        if not isinstance(b, dict) or not b.get("block_id"):
            continue
        old_id = str(b["block_id"])
        if old_id == root_old_id:
            continue
        if old_id not in id_map:
            continue
        clone = json.loads(json.dumps(b, ensure_ascii=False))
        clone["block_id"] = id_map[old_id]
        for k in list(clone.keys()):
            if k in remove_keys:
                clone.pop(k, None)

        # Remap children ids.
        for key in ("children", "children_id", "children_ids", "childrenIds"):
            if isinstance(clone.get(key), list):
                clone[key] = [id_map.get(str(x), str(x)) for x in clone[key] if x]

        descendants.append(clone)

    children_id = [id_map.get(x, x) for x in first_level_old if x in id_map]
    return {"children_id": children_id, "descendants": descendants}


def _feishu_docx_clone_template_content_into_document_best_effort(
    *,
    user_access_token: str,
    template_document_id: str,
    target_document_id: str,
    target_parent_block_id: str,
    index: int,
    timeout_sec: int,
) -> None:
    template_blocks = _feishu_docx_list_all_blocks(
        user_access_token=user_access_token,
        document_id=template_document_id,
        timeout_sec=timeout_sec,
    )
    payload = _feishu_docx_build_descendant_payload_from_template_blocks(
        template_blocks=template_blocks,
        template_document_id=template_document_id,
    )
    if not payload.get("descendants"):
        raise RuntimeError("Template docx appears to have no cloneable descendants")

    try:
        _ = _feishu_insert_blocks_descendant(
            user_access_token=user_access_token,
            document_id=target_document_id,
            parent_block_id=target_parent_block_id,
            descendant_payload=payload,
            index=index,
            timeout_sec=timeout_sec,
        )
        return
    except RuntimeError as e:
        msg = str(e)
        if "1770029" not in msg and "block not support to create" not in msg.lower():
            raise

    # Retry: insert only first-level children and their descendant subtree.
    # Build parent map from template_blocks
    parent_map: Dict[str, str] = {}
    for b in template_blocks:
        if not isinstance(b, dict) or not b.get("block_id"):
            continue
        bid = str(b.get("block_id") or "")
        pid = str(b.get("parent_id") or b.get("parent_block_id") or "").strip()
        parent_map[bid] = pid

    # Determine the root_old_id and first_level_old as in build function
    # Recompute using helper logic to be robust.
    root_old_id = ""
    by_id: Dict[str, Dict[str, Any]] = {}
    for b in template_blocks:
        if isinstance(b, dict) and b.get("block_id"):
            by_id[str(b["block_id"])]=b
    if template_document_id and template_document_id in by_id:
        root_old_id = template_document_id
    else:
        for b in template_blocks:
            if not isinstance(b, dict):
                continue
            if str(b.get("block_type") or "").lower() == "page" and not (b.get("parent_id") or b.get("parent_block_id")):
                root_old_id = str(b.get("block_id") or "").strip()
                break
    if not root_old_id and template_blocks:
        root_old_id = str(template_blocks[0].get("block_id") or "").strip()

    first_level_old: List[str] = []
    def _children_ids_of(old_id: str) -> List[str]:
        for key in ("children", "children_id", "children_ids", "childrenIds"):
            v = (by_id.get(old_id) or {}).get(key)
            if isinstance(v, list):
                return [str(x) for x in v if x]
        # fallback: scan parent_map
        out: List[str] = []
        for k, p in parent_map.items():
            if p == old_id:
                out.append(k)
        return out

    first_level_old = _children_ids_of(root_old_id)
    if not first_level_old:
        # fallback: infer
        for b in template_blocks:
            if not isinstance(b, dict):
                continue
            pid = str(b.get("parent_id") or b.get("parent_block_id") or "").strip()
            if pid == root_old_id and b.get("block_id"):
                first_level_old.append(str(b["block_id"]))

    # Build allowed set: first level + all their descendants
    allowed: set = set()
    stack: List[str] = [x for x in first_level_old]
    while stack:
        cur = stack.pop()
        if not cur or cur in allowed:
            continue
        allowed.add(cur)
        # find children
        for kid, par in parent_map.items():
            if par == cur:
                stack.append(kid)

    # Filter descendants from original payload to include only allowed old_ids
    # We need to map back to original old_id — payload descendants currently have remapped ids.
    # To correlate, we will rebuild a mapping from old_id -> cloned block in payload by comparing a subset of fields.
    # Simpler: rebuild a fresh payload by selecting from template_blocks directly.

    # Recreate id_map consistent with original builder
    id_map: Dict[str, str] = {}
    for b in template_blocks:
        oid = str(b.get("block_id") or "")
        if oid == root_old_id:
            continue
        id_map[oid] = secrets.token_hex(12)

    descendants_filtered: List[Dict[str, Any]] = []
    remove_keys = {
        "parent_id",
        "parent_block_id",
        "document_id",
        "document_revision_id",
        "revision_id",
        "create_time",
        "update_time",
        "is_delete",
    }

    for b in template_blocks:
        if not isinstance(b, dict) or not b.get("block_id"):
            continue
        old_id = str(b["block_id"])
        if old_id == root_old_id or old_id not in allowed:
            continue
        clone = json.loads(json.dumps(b, ensure_ascii=False))
        clone["block_id"] = id_map.get(old_id, old_id)
        for k in list(clone.keys()):
            if k in remove_keys:
                clone.pop(k, None)
        # remap children lists
        for key in ("children", "children_id", "children_ids", "childrenIds"):
            if isinstance(clone.get(key), list):
                clone[key] = [id_map.get(str(x), str(x)) for x in clone[key] if x and str(x) in id_map]
        descendants_filtered.append(clone)

    children_id_filtered = [id_map.get(x, x) for x in first_level_old if x in id_map]
    payload2 = {"children_id": children_id_filtered, "descendants": descendants_filtered}

    if not payload2.get("descendants"):
        raise RuntimeError("Template clone retry (first-level) produced no descendants to insert")

    # Second attempt
    _ = _feishu_insert_blocks_descendant(
        user_access_token=user_access_token,
        document_id=target_document_id,
        parent_block_id=target_parent_block_id,
        descendant_payload=payload2,
        index=index,
        timeout_sec=timeout_sec,
    )


def _feishu_docx_batch_update_blocks(
    *,
    user_access_token: str,
    document_id: str,
    requests: List[Dict[str, Any]],
    timeout_sec: int,
) -> Dict[str, Any]:
    url = f"https://open.feishu.cn/open-apis/docx/v1/documents/{quote(document_id)}/blocks/batch_update?document_revision_id=-1"
    payload = {"requests": requests}
    resp = _http_json(
        method="PATCH",
        url=url,
        headers={"Authorization": f"Bearer {user_access_token}"},
        body=payload,
        timeout_sec=timeout_sec,
    )
    if int(resp.get("code") or 0) != 0:
        raise RuntimeError(f"Feishu docx batch_update failed: {resp}")
    return resp


def _feishu_docx_replace_placeholders_in_document(
    *,
    user_access_token: str,
    document_id: str,
    mapping: Dict[str, str],
    timeout_sec: int,
    dry_run: bool,
    max_requests_per_batch: int = 200,
) -> Dict[str, Any]:
    """Replace placeholders in all text blocks of a Docx document.

    Strategy:
    - List all blocks.
    - For each block with text.elements, replace within text_run.content.
    - If placeholders span multiple text runs (rare, but possible), and the block
      only contains text_run elements, we flatten to a single run to ensure
      replacement succeeds.
    - Write back via batch_update in chunks (<=200 per request).
    """

    non_empty = {str(k): str(v) for k, v in (mapping or {}).items() if str(k).strip() and str(v).strip()}
    if not non_empty:
        return {"updated_blocks": 0, "scanned_blocks": 0, "note": "no non-empty mapping values"}

    def _apply(s: str) -> str:
        out = str(s)
        # Replace longer keys first to avoid accidental partial overlaps.
        for kk in sorted(non_empty.keys(), key=len, reverse=True):
            val = non_empty[kk]
            # If it's a link-name pair, use the name for the plain text comparison
            if "|" in val:
                val = val.split("|", 0)[0] if val.split("|", 1)[0] else val.split("|", 1)[1]
                # Wait, if it's "name|url", the plain text should probably be "name".
                # But for the purpose of 'target_plain', we just need a consistent replacement.
                val = val.split("|")[0]
            out = out.replace(kk, val)
        return out

    def _apply_with_links(content: str, style: Dict[str, Any]) -> List[Dict[str, Any]]:
        sorted_keys = sorted(non_empty.keys(), key=len, reverse=True)
        parts = [{"text": content, "is_placeholder": False}]
        for k in sorted_keys:
            new_parts = []
            for p in parts:
                if p["is_placeholder"]:
                    new_parts.append(p)
                    continue
                text = p["text"]
                if k in text:
                    sub_parts = text.split(k)
                    for i, sp in enumerate(sub_parts):
                        if sp:
                            new_parts.append({"text": sp, "is_placeholder": False})
                        if i < len(sub_parts) - 1:
                            new_parts.append({"text": k, "is_placeholder": True, "key": k})
                else:
                    new_parts.append(p)
            parts = new_parts
        res = []
        for p in parts:
            if p["is_placeholder"]:
                val = non_empty[p["key"]]
                if "|" in val:
                    parts_val = val.split("|", 1)
                    name = parts_val[0]
                    url = parts_val[1]
                    new_style = dict(style or {})
                    new_style["link"] = {"url": url}
                    res.append({"text_run": {"content": name, "text_element_style": new_style}})
                else:
                    res.append({"text_run": {"content": val, "text_element_style": style}})
            else:
                res.append({"text_run": {"content": p["text"], "text_element_style": style}})
        return res

    blocks = _feishu_docx_list_all_blocks(user_access_token=user_access_token, document_id=document_id, timeout_sec=timeout_sec)
    update_reqs: List[Dict[str, Any]] = []
    scanned = 0
    changed = 0
    skipped = 0

    for b in blocks:
        scanned += 1
        block_id = str(b.get("block_id") or "").strip()
        text = b.get("text")
        if not block_id or not isinstance(text, dict):
            continue
        elements = text.get("elements")
        if not isinstance(elements, list) or not elements:
            continue

        # Quick check: only proceed if the combined text contains any placeholder key.
        plain_parts: List[str] = []
        for el in elements:
            if isinstance(el, dict) and isinstance(el.get("text_run"), dict):
                plain_parts.append(str((el.get("text_run") or {}).get("content") or ""))
        plain = "".join(plain_parts)
        if not plain:
            continue
        if not any(k in plain for k in non_empty.keys()):
            continue

        target_plain = _apply(plain)
        if target_plain == plain:
            continue

        # First attempt: per-run replacement to preserve existing styles and non-text elements.
        new_elements: List[Dict[str, Any]] = []
        any_non_text = False
        for el in elements:
            if not isinstance(el, dict):
                continue
            if isinstance(el.get("text_run"), dict):
                tr = el.get("text_run") or {}
                content = str(tr.get("content") or "")
                style = tr.get("text_element_style") or {}
                
                # Use the new helper to handle smart link splitting
                replaced_runs = _apply_with_links(content, style)
                new_elements.extend(replaced_runs)
            else:
                any_non_text = True
                new_elements.append(el)

        # Detect cross-run placeholder: per-run replacement didn't reach full replacement.
        after_plain_parts: List[str] = []
        for el in new_elements:
            if isinstance(el, dict) and isinstance(el.get("text_run"), dict):
                after_plain_parts.append(str((el.get("text_run") or {}).get("content") or ""))
        after_plain = "".join(after_plain_parts)

        if after_plain != target_plain:
            # If only text runs, we can flatten safely.
            if not any_non_text and all(isinstance(el, dict) and isinstance(el.get("text_run"), dict) for el in elements):
                first_style: Dict[str, Any] = {}
                for el in elements:
                    tr0 = el.get("text_run") or {}
                    style0 = tr0.get("text_element_style")
                    if isinstance(style0, dict):
                        first_style = style0
                    break
                new_elements = _apply_with_links(plain, first_style)
            else:
                # We can't reliably replace across runs without changing rich elements.
                skipped += 1
                continue

        changed += 1
        update_reqs.append({"block_id": block_id, "update_text_elements": {"elements": new_elements}})

    if dry_run:
        return {
            "updated_blocks": len(update_reqs),
            "scanned_blocks": scanned,
            "skipped_blocks": skipped,
            "note": "dry-run; no updates sent",
        }

    # Batch update (<=200 requests per call)
    max_n = max(1, min(int(max_requests_per_batch), 200))
    sent = 0
    for i in range(0, len(update_reqs), max_n):
        chunk = update_reqs[i : i + max_n]
        _ = _feishu_docx_batch_update_blocks(
            user_access_token=user_access_token,
            document_id=document_id,
            requests=chunk,
            timeout_sec=timeout_sec,
        )
        sent += len(chunk)
        # Respect doc rate limits (3 edits/sec). This endpoint counts as an edit.
        time.sleep(0.4)

    return {
        "updated_blocks": sent,
        "scanned_blocks": scanned,
        "skipped_blocks": skipped,
    }


def _feishu_copy_wiki_page(
    *,
    user_access_token: str,
    template_page_id: str,
    target_space_id: str,
    title: str,
    timeout_sec: int,
) -> Tuple[str, str]:
    url = "https://open.feishu.cn/open-apis/knowledge/v1/page/copy"
    payload = {
        "page_id": template_page_id,
        "space_id": target_space_id,
        "title": title,
    }
    resp = _http_json(
        method="POST",
        url=url,
        headers={"Authorization": f"Bearer {user_access_token}"},
        body=payload,
        timeout_sec=timeout_sec,
    )
    if int(resp.get("code") or 0) != 0:
        raise RuntimeError(f"Feishu wiki copy failed: {resp}")
    data = resp.get("data") or {}
    new_page_id = str(data.get("page", {}).get("page_id") or data.get("page_id") or "").strip()
    new_url = str(data.get("page", {}).get("url") or data.get("url") or "").strip()
    if not new_page_id:
        # Some responses return page_id at top-level data
        new_page_id = str(data.get("id") or data.get("node_id") or "").strip()
    if not new_page_id:
        raise RuntimeError(f"Feishu wiki copy missing new page id: {resp}")
    return new_page_id, new_url or f"https://zepp.feishu.cn/wiki/{new_page_id}"


def _feishu_get_wiki_page(*, user_access_token: str, page_id: str, timeout_sec: int) -> Dict[str, Any]:
    url = f"https://open.feishu.cn/open-apis/knowledge/v1/page/get?page_id={page_id}"
    resp = _http_json(method="GET", url=url, headers={"Authorization": f"Bearer {user_access_token}"}, timeout_sec=timeout_sec)
    if int(resp.get("code") or 0) != 0:
        raise RuntimeError(f"Feishu wiki get failed: {resp}")
    return resp.get("data") or {}


def _feishu_update_wiki_page(*, user_access_token: str, page_id: str, content: Any, timeout_sec: int) -> Dict[str, Any]:
    url = "https://open.feishu.cn/open-apis/knowledge/v1/page/update"
    payload = {
        "page_id": page_id,
        "content": content,
    }
    resp = _http_json(method="POST", url=url, headers={"Authorization": f"Bearer {user_access_token}"}, body=payload, timeout_sec=timeout_sec)
    if int(resp.get("code") or 0) != 0:
        raise RuntimeError(f"Feishu wiki update failed: {resp}")
    return resp.get("data") or {}


def _feishu_extract_wiki_content(get_resp_data: Dict[str, Any]) -> Any:
    """Best-effort extract wiki page content payload from knowledge/v1/page/get response data."""

    if not isinstance(get_resp_data, dict):
        return None
    # Common shapes: data.page.content or data.content
    page = get_resp_data.get("page")
    if isinstance(page, dict) and "content" in page:
        return page.get("content")
    if "content" in get_resp_data:
        return get_resp_data.get("content")
    return None


def _feishu_wiki_get_node(
    *,
    user_access_token: str,
    token: str,
    obj_type: str = "wiki",
    timeout_sec: int,
) -> Dict[str, Any]:
    """Resolve a wiki node token to node metadata (space_id, obj_token, obj_type, etc)."""

    token = str(token or "").strip()
    if not token:
        raise ValueError("missing wiki node token")

    q = {"token": token}
    if obj_type and str(obj_type).strip() and str(obj_type).strip().lower() != "wiki":
        q["obj_type"] = str(obj_type).strip()
    url = f"https://open.feishu.cn/open-apis/wiki/v2/spaces/get_node?{urllib.parse.urlencode(q)}"
    resp = _http_json(
        method="GET",
        url=url,
        headers={"Authorization": f"Bearer {user_access_token}"},
        timeout_sec=timeout_sec,
    )
    if int(resp.get("code") or 0) != 0:
        raise RuntimeError(f"Feishu wiki get_node failed: {resp}")
    data = resp.get("data") or {}
    node = data.get("node") or {}
    if not isinstance(node, dict):
        raise RuntimeError(f"Feishu wiki get_node missing node: {resp}")
    return node


def _feishu_wiki_copy_node(
    *,
    user_access_token: str,
    space_id: str,
    node_token: str,
    target_space_id: str,
    target_parent_token: str,
    title: str,
    timeout_sec: int,
) -> Dict[str, Any]:
    """Copy a wiki node to target space/parent. Returns the new node object."""

    space_id = str(space_id or "").strip()
    node_token = str(node_token or "").strip()
    if not space_id or not node_token:
        raise ValueError("missing space_id/node_token for wiki copy")

    url = f"https://open.feishu.cn/open-apis/wiki/v2/spaces/{space_id}/nodes/{node_token}/copy"
    payload: Dict[str, Any] = {}
    if str(target_space_id or "").strip():
        payload["target_space_id"] = str(target_space_id).strip()
    if str(target_parent_token or "").strip():
        payload["target_parent_token"] = str(target_parent_token).strip()
    if str(title or "").strip():
        payload["title"] = str(title).strip()

    resp = _http_json(
        method="POST",
        url=url,
        headers={"Authorization": f"Bearer {user_access_token}"},
        body=payload,
        timeout_sec=timeout_sec,
    )
    if int(resp.get("code") or 0) != 0:
        raise RuntimeError(f"Feishu wiki copy node failed: {resp}")
    data = resp.get("data") or {}
    node = data.get("node") or {}
    if not isinstance(node, dict):
        raise RuntimeError(f"Feishu wiki copy node missing node: {resp}")
    return node


def _replace_placeholders_in_obj(obj: Any, replacements: Dict[str, str]) -> Any:
    # Recursively replace placeholders in all string values of a nested structure.
    if isinstance(obj, str):
        out = obj
        for k, v in replacements.items():
            out = out.replace(k, v)
        return out
    if isinstance(obj, list):
        return [_replace_placeholders_in_obj(x, replacements) for x in obj]
    if isinstance(obj, dict):
        new: Dict[str, Any] = {}
        for key, val in obj.items():
            new[key] = _replace_placeholders_in_obj(val, replacements)
        return new
    return obj



def _curl_bin() -> str:
    # On Windows, prefer curl.exe to avoid any PATH shadowing.
    return "curl.exe" if os.name == "nt" else "curl"


def _safe_filename(name: str, replacement: str = "_") -> str:
    # Windows file name restrictions + some common unsafe chars.
    bad = '<>:/\\|?*\n\r\t'
    out = "".join((c if c not in bad else replacement) for c in (name or ""))
    out = re.sub(r"\s+", " ", out).strip()
    return out or "document"


def _now_timestamp() -> str:
    # Keep it file-name friendly.
    import datetime as _dt

    return _dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def _render_version_doc_markdown(
    *,
    release_cfg: Dict[str, Any],
    pipeline_cfg_path: Path,
    runs: List[Dict[str, Any]],
    remote_variant_base: str,
) -> str:
    project = str(release_cfg.get("project") or "")
    version = str(release_cfg.get("version") or "")
    variant = str(release_cfg.get("variant") or "")
    notes = str(release_cfg.get("notes") or "").strip()

    ts = _now_timestamp()
    title = f"{project} {version} 版本文档"
    lines: List[str] = []
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"- 项目：{project}")
    lines.append(f"- 版本：{version}")
    if variant:
        lines.append(f"- Variant：{variant}")
    lines.append(f"- 生成时间：{ts}")
    lines.append(f"- Pipeline 配置：{pipeline_cfg_path}")
    lines.append("")
    if notes:
        lines.append("## 版本说明")
        lines.append(notes)
        lines.append("")

    lines.append("## NAS 目录")
    lines.append(f"- 远端基准目录：{remote_variant_base.rstrip('/')}/")
    lines.append("")

    for r in runs:
        name = str(r.get("name") or "")
        build_url = str(r.get("build_url") or "")
        out_dir = str(r.get("out_dir") or "")
        remote_dir = str(r.get("remote_dir") or "")
        local_dir = str(r.get("local_dir") or "")
        extra_dirs = r.get("extra_dirs") or []
        share_links_main: Dict[str, str] = r.get("share_links_main") or {}
        share_links_extra: Dict[str, Dict[str, str]] = r.get("share_links_extra") or {}

        lines.append(f"## {name.upper()}")
        if build_url:
            lines.append(f"- Jenkins：{build_url}")
        if out_dir:
            lines.append(f"- 下载目录：{out_dir}")
        if remote_dir:
            lines.append(f"- 上传目录：{remote_dir.rstrip('/')}/")
        if local_dir:
            lines.append(f"- 上传本地目录：{local_dir}")
        if extra_dirs:
            lines.append("- 额外上传目录：")
            for p in extra_dirs:
                lines.append(f"  - {p}")
        lines.append("")

        # Share links
        lines.append("### 文件分享链接")
        if share_links_main:
            lines.append("**主目录**")
            for rel, url in sorted(share_links_main.items()):
                lines.append(f"- {rel}: {url}")
        else:
            lines.append("- （主目录未生成分享链接：可能是 dry-run 或 --skip-share，或 DSM 凭据缺失）")

        for extra_path, links in sorted(share_links_extra.items()):
            if links:
                lines.append(f"**额外目录：{extra_path}**")
                for rel, url in sorted(links.items()):
                    lines.append(f"- {rel}: {url}")
            else:
                lines.append(f"- （额外目录未生成分享链接：{extra_path}）")
        lines.append("")

    return "\n".join(lines) + "\n"


def _encode_path_keep_slash(path: str) -> str:
    return quote(path, safe="/")


def _mask(s: str, keep: int = 2) -> str:
    s = s or ""
    if len(s) <= keep:
        return "*" * len(s)
    return s[:keep] + "*" * (len(s) - keep)


def _iter_local_files(local_root: Path) -> Iterable[Tuple[Path, str]]:
    # Yields (absolute_path, relative_posix_path)
    for p in sorted(local_root.rglob("*")):
        if p.is_file():
            yield p, p.relative_to(local_root).as_posix()


def _safe_extract_tar(tf: tarfile.TarFile, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_real = dest_dir.resolve()

    for member in tf.getmembers():
        member_path = dest_dir / member.name
        try:
            member_real = member_path.resolve()
        except FileNotFoundError:
            # For nested paths that don't exist yet, resolve the parent.
            member_real = (member_path.parent.resolve() / member_path.name)
        if dest_real not in member_real.parents and member_real != dest_real:
            raise RuntimeError(f"Unsafe tar member path: {member.name}")

    tf.extractall(dest_dir)


def _find_files_by_regex(root: Path, pattern: Pattern) -> List[Path]:
    matches: List[Path] = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and pattern.search(p.name):
            matches.append(p)
    return matches


def _pick_one(paths: List[Path], *, what: str) -> Path:
    if not paths:
        raise RuntimeError(f"Not found: {what}")
    if len(paths) == 1:
        return paths[0]
    # Prefer shortest path (least nesting), then lexicographic.
    paths_sorted = sorted(paths, key=lambda p: (len(p.as_posix().split("/")), p.as_posix()))
    return paths_sorted[0]


def _prepare_release_extract(*, input_dir: Path, workspace_dir: Path, clean: bool, dry_run: bool, project: str = "") -> Path:
    """Extract two target files from nested tgz/tar archives.

    Rules (version/date suffixes vary; use regex):
    - archive_<project>_*.tgz -> contains archive_<project>_*.tar
    - extract tar -> yields archive_OTA_CLOUD_*.tgz and archive_OTA_*.tgz
    - archive_OTA_CLOUD_*.tgz -> ... -> contains watch@mhs003_ota_sign.zip (upload this)
    - archive_OTA_*.tgz -> ... -> contains a folder; upload binary/watch@mhs003.elf

    Additional rules:
    - step1_archive_<project>/archive_BOOT_*.tgz -> (optional tar) -> manifest_BOOT_*.xml (upload this)
    - step1_archive_<project>/archive_RECOVERY_*.tgz -> (optional tar) -> manifest_RECOVERY_*.xml (upload this)
    """

    input_dir = input_dir.expanduser().resolve()
    workspace_dir = workspace_dir.expanduser().resolve()
    stage_dir = workspace_dir / "upload_payload"
    work_dir = workspace_dir / "_work"

    # In dry-run, only report actions; do not require real artifacts to be present.
    if dry_run:
        proj = (project or "").strip()
        proj_safe = re.sub(r"[^A-Za-z0-9_-]", "_", proj.lower()) if proj else "cologne"
        stage_dir.mkdir(parents=True, exist_ok=True)
        work_dir.mkdir(parents=True, exist_ok=True)
        print(f"Prepare: dry-run; would find archive_{proj_safe}_*.tgz under {input_dir} and extract into {workspace_dir}")
        return stage_dir

    if clean and stage_dir.exists():
        shutil.rmtree(stage_dir)
    if clean and work_dir.exists():
        shutil.rmtree(work_dir)

    stage_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    # Use project name (if provided) to scope archive filenames and internal dirs.
    proj = (project or "").strip()
    proj_safe = re.sub(r"[^A-Za-z0-9_-]", "_", proj.lower()) if proj else "cologne"
    re_archive_cologne = re.compile(rf"^archive_{re.escape(proj_safe)}_.*\.tgz$", re.IGNORECASE)
    re_cloud_tgz = re.compile(r"^archive_OTA_CLOUD_.*\.tgz$", re.IGNORECASE)
    re_ota_tgz = re.compile(r"^archive_OTA_(?!CLOUD).*\.tgz$", re.IGNORECASE)
    re_boot_tgz = re.compile(r"^archive_BOOT_.*\.tgz$", re.IGNORECASE)
    re_recovery_tgz = re.compile(r"^archive_RECOVERY_.*\.tgz$", re.IGNORECASE)
    re_boot_manifest = re.compile(r"^manifest_BOOT_.*\.xml$", re.IGNORECASE)
    re_recovery_manifest = re.compile(r"^manifest_RECOVERY_.*\.xml$", re.IGNORECASE)

    # 1) Find archive_<project>_*.tgz
    tgz_paths = _find_files_by_regex(input_dir, re_archive_cologne)
    src_tgz = _pick_one(tgz_paths, what=f"archive_{proj_safe}_*.tgz")

    def extract_tgz_to_dir(tgz_path: Path, dest: Path) -> Path:
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(tgz_path, mode="r:gz") as tf:
            _safe_extract_tar(tf, dest)
        return dest

    def maybe_extract_single_tar_layer(from_dir: Path, dest: Path) -> Path:
        # If there are .tar files in from_dir, extract the most plausible one (shortest path).
        tar_candidates = [p for p in from_dir.rglob("*.tar") if p.is_file()]
        if not tar_candidates:
            return from_dir
        tar_path = _pick_one(tar_candidates, what="*.tar (inner layer)")
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(tar_path, mode="r:") as tf:
            _safe_extract_tar(tf, dest)
        return dest

    # 2) Extract archive_<project> tgz
    step1_dir = extract_tgz_to_dir(src_tgz, work_dir / f"step1_archive_{proj_safe}")

    # Some builds wrap a tar inside archive_<project>.tgz; some directly include archive_OTA_*.tgz.
    step1_content_dir = maybe_extract_single_tar_layer(step1_dir, work_dir / f"step1_archive_{proj_safe}_tar")

    def extract_manifest_from_step1(*, tgz_pat: Pattern, manifest_pat: Pattern, kind: str) -> Optional[Path]:
        """Extract manifest_<KIND>_*.xml from archive_<KIND>_*.tgz under step1 archive."""
        # Prefer finding the archive in step1_dir (as user described). If not present, fall back to step1_content_dir.
        tgz_candidates = _find_files_by_regex(step1_dir, tgz_pat)
        if not tgz_candidates:
            tgz_candidates = _find_files_by_regex(step1_content_dir, tgz_pat)
        if not tgz_candidates:
            print(f"Prepare: {kind}: archive not found; skip.")
            return None

        tgz_path = _pick_one(tgz_candidates, what=f"archive_{kind}_*.tgz")
        out1 = extract_tgz_to_dir(tgz_path, work_dir / f"step1_{kind.lower()}_tgz")
        out2 = maybe_extract_single_tar_layer(out1, work_dir / f"step1_{kind.lower()}_tar")

        # Find manifest XML (may be in out1 or out2 depending on nesting)
        xml_candidates = _find_files_by_regex(out2, manifest_pat)
        if not xml_candidates:
            xml_candidates = _find_files_by_regex(out1, manifest_pat)
        if not xml_candidates:
            print(f"Prepare: {kind}: manifest XML not found after extraction; skip.")
            return None
        xml_path = _pick_one(xml_candidates, what=f"manifest_{kind}_*.xml")
        dst = stage_dir / xml_path.name
        shutil.copy2(xml_path, dst)
        return dst

    # 2.5) BOOT / RECOVERY manifests
    _ = extract_manifest_from_step1(tgz_pat=re_boot_tgz, manifest_pat=re_boot_manifest, kind="BOOT")
    _ = extract_manifest_from_step1(tgz_pat=re_recovery_tgz, manifest_pat=re_recovery_manifest, kind="RECOVERY")

    cloud_tgz = _pick_one(_find_files_by_regex(step1_content_dir, re_cloud_tgz), what="archive_OTA_CLOUD_*.tgz")
    ota_tgz = _pick_one(_find_files_by_regex(step1_content_dir, re_ota_tgz), what="archive_OTA_*.tgz")

    # 3) CLOUD path: tgz -> (optional tar) -> find zip
    cloud_root = extract_tgz_to_dir(cloud_tgz, work_dir / "step2_cloud_tgz")
    cloud_root = maybe_extract_single_tar_layer(cloud_root, work_dir / "step2_cloud_tar")

    zip_name = "watch@mhs003_ota_sign.zip"
    zip_candidates = [p for p in cloud_root.rglob(zip_name) if p.is_file()]
    zip_path = _pick_one(zip_candidates, what=zip_name)
    shutil.copy2(zip_path, stage_dir / zip_name)

    # 4) OTA path: tgz -> (optional tar) -> find binary/watch@mhs003.elf
    ota_root = extract_tgz_to_dir(ota_tgz, work_dir / "step3_ota_tgz")
    ota_root = maybe_extract_single_tar_layer(ota_root, work_dir / "step3_ota_tar")

    elf_name = "watch@mhs003.elf"
    elf_candidates: List[Path] = []
    for p in ota_root.rglob(elf_name):
        if not p.is_file():
            continue
        if "binary" in [x.name for x in p.parents]:
            elf_candidates.append(p)
    if not elf_candidates:
        elf_candidates = [p for p in ota_root.rglob(elf_name) if p.is_file()]
    elf_path = _pick_one(elf_candidates, what=f"{elf_name} (prefer under binary/)")
    shutil.copy2(elf_path, stage_dir / elf_name)

    return stage_dir


def _get_by_dotted_path(obj: Any, dotted: str) -> Any:
    cur: Any = obj
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            raise KeyError(dotted)
    return cur


def _expand_placeholders(value: Any, ctx: Dict[str, Any]) -> Any:
    if isinstance(value, str):
        out = ""
        i = 0
        while True:
            start = value.find("${", i)
            if start < 0:
                out += value[i:]
                break
            out += value[i:start]
            end = value.find("}", start + 2)
            if end < 0:
                raise ValueError(f"Unclosed placeholder in: {value!r}")
            key = value[start + 2 : end].strip()
            try:
                resolved = _get_by_dotted_path(ctx, key)
            except KeyError:
                # Some placeholders (e.g. runtime.*) are only available at runtime.
                # Keep the placeholder as-is so later expansion can still work.
                out += value[start : end + 1]
            else:
                out += str(resolved)
            i = end + 1
        return out

    if isinstance(value, list):
        return [_expand_placeholders(v, ctx) for v in value]

    if isinstance(value, dict):
        return {k: _expand_placeholders(v, ctx) for k, v in value.items()}

    return value


def _dsl_string_is_var_ref(s: str, vars: Dict[str, Any]) -> bool:
    ss = str(s or "").strip()
    return bool(ss) and (ss in vars)


def _dsl_resolve_ref_or_path(value: Any, *, vars: Dict[str, Any], what: str) -> Path:
    if isinstance(value, str) and _dsl_string_is_var_ref(value, vars):
        v = vars.get(value)
        if isinstance(v, Path):
            return v
        if isinstance(v, str):
            if not v.strip():
                raise RuntimeError(f"DSL: var ref {value!r} for {what} is empty")
            return Path(v)
        raise RuntimeError(f"DSL: var ref {value!r} for {what} is not a path: {type(v)}")
    if not isinstance(value, str) or not str(value).strip():
        raise RuntimeError(f"DSL: missing {what}")
    return Path(str(value)).expanduser().resolve()


def _dsl_pick_one(paths: List[Path], *, expect: str, select: str, what: str) -> Path:
    exp = str(expect or "one").strip().lower() or "one"
    sel = str(select or "shortest_path").strip().lower() or "shortest_path"
    if exp != "one":
        raise RuntimeError(f"DSL: only expect='one' is supported for now (got {expect!r})")
    if sel != "shortest_path":
        raise RuntimeError(f"DSL: only select='shortest_path' is supported for now (got {select!r})")
    return _pick_one(paths, what=what)


def _dsl_match_files(*, root: Path, match: Dict[str, Any], match_on: str) -> List[Path]:
    if not root.exists():
        return []

    mon = str(match_on or "name").strip().lower() or "name"
    if mon not in ("name", "path"):
        raise RuntimeError(f"DSL: unsupported match_on={match_on!r}")

    exact = str(match.get("exact") or "").strip()
    regex = str(match.get("regex") or "").strip()
    fallback_regex = str(match.get("fallback_regex") or "").strip()

    def _target_str(p: Path) -> str:
        if mon == "path":
            try:
                return p.relative_to(root).as_posix()
            except Exception:
                return p.as_posix()
        return p.name

    matches: List[Path] = []
    if exact:
        for p in sorted(root.rglob("*")):
            if p.is_file() and _target_str(p) == exact:
                matches.append(p)
        if matches:
            return matches
        if fallback_regex:
            regex = fallback_regex
        else:
            return []

    if regex:
        pat = re.compile(regex, re.IGNORECASE)
        for p in sorted(root.rglob("*")):
            if p.is_file() and pat.search(_target_str(p)):
                matches.append(p)
        return matches

    # No match criteria => match nothing (safer than match all).
    return []


def _dsl_extract_tgz(*, src: Path, dest: Path, dry_run: bool) -> None:
    if dry_run:
        print(f"DSL Prepare: dry-run; would extract tgz: {src} -> {dest}")
        return
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(src, mode="r:gz") as tf:
        _safe_extract_tar(tf, dest)


def _dsl_extract_tar(*, src: Path, dest: Path, dry_run: bool) -> None:
    if dry_run:
        print(f"DSL Prepare: dry-run; would extract tar: {src} -> {dest}")
        return
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(src, mode="r:") as tf:
        _safe_extract_tar(tf, dest)


    
    
def _dsl_extract_zip(*, src: Path, dest: Path, dry_run: bool) -> None:
    if dry_run:
        print(f"DSL Prepare: dry-run; would extract zip: {src} -> {dest}")
        return
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(src, "r") as zf:
        zf.extractall(dest)
def _dsl_execute_prepare_flow(
    *,
    cfg: Dict[str, Any],
    dsl_cfg: Dict[str, Any],
    dry_run: bool,
    current_run: str,
    existing_vars: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Execute prepare.flow.steps and return a runtime object.

    Returns:
      {
        "vars": {name: Path|str},
        "work_dir": Path,
        "upload_payload_dir": Path,
        "run_summaries_extra_dir": str,
      }
    """

    prep = dsl_cfg.get("prepare") or {}
    if not isinstance(prep, dict):
        raise RuntimeError("DSL: prepare must be an object")
    flow = prep.get("flow") or {}
    if not isinstance(flow, dict):
        raise RuntimeError("DSL: prepare.flow must be an object")

    work_dir = Path(str(flow.get("work_dir") or "")).expanduser().resolve()
    upload_payload_dir = Path(str(flow.get("upload_payload_dir") or "")).expanduser().resolve()
    clean = bool(flow.get("clean", False))
    defaults = flow.get("defaults") or {}
    if not isinstance(defaults, dict):
        defaults = {}

    if not work_dir:
        raise RuntimeError("DSL: prepare.flow.work_dir is missing")
    if not upload_payload_dir:
        raise RuntimeError("DSL: prepare.flow.upload_payload_dir is missing")

    if dry_run:
        print(f"DSL Prepare: dry-run; work_dir={work_dir}")
        print(f"DSL Prepare: dry-run; upload_payload_dir={upload_payload_dir}")
    else:
        if clean and work_dir.exists():
            shutil.rmtree(work_dir)
        if clean and upload_payload_dir.exists():
            shutil.rmtree(upload_payload_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        upload_payload_dir.mkdir(parents=True, exist_ok=True)

    steps = flow.get("steps") or []
    if not isinstance(steps, list):
        raise RuntimeError("DSL: prepare.flow.steps must be an array")

    vars: Dict[str, Any] = dict(existing_vars or {})
    vars.setdefault("work_dir", work_dir)
    vars.setdefault("upload_payload_dir", upload_payload_dir)

    for idx, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            raise RuntimeError(f"DSL: step #{idx} must be an object")
        op = str(step.get("op") or "").strip()
        name = str(step.get("name") or op or f"step_{idx}").strip()
        optional = bool(step.get("optional", False))
        expect = str(step.get("expect") or defaults.get("expect") or "one")
        select = str(step.get("select") or defaults.get("select") or "shortest_path")
        match_on = str(step.get("match_on") or defaults.get("match_on") or "name")

        step_run = str(step.get("run") or defaults.get("run") or "").strip().lower()
        if step_run and step_run != str(current_run or "").strip().lower():
            continue

        try:
            if op == "find":
                root = _dsl_resolve_ref_or_path(step.get("root"), vars=vars, what=f"{name}.root")
                match = step.get("match") or {}
                if not isinstance(match, dict):
                    raise RuntimeError(f"DSL: {name}.match must be an object")
                save_as = str(step.get("save_as") or "").strip()
                if not save_as:
                    raise RuntimeError(f"DSL: {name} missing save_as")
                if dry_run:
                    print(f"DSL Prepare: dry-run; would find under {root} match={match}")
                    exact = str(match.get("exact") or "").strip()
                    dummy_name = exact or f"{save_as}.dryrun"
                    vars[save_as] = (root / dummy_name)
                else:
                    found = _dsl_match_files(root=root, match=match, match_on=match_on)
                    chosen = _dsl_pick_one(found, expect=expect, select=select, what=f"{name} (find)")
                    vars[save_as] = chosen

            elif op == "pick":
                root = _dsl_resolve_ref_or_path(step.get("root"), vars=vars, what=f"{name}.root")
                match = step.get("match") or {}
                if not isinstance(match, dict):
                    raise RuntimeError(f"DSL: {name}.match must be an object")
                save_as = str(step.get("save_as") or "").strip()
                if not save_as:
                    raise RuntimeError(f"DSL: {name} missing save_as")
                if dry_run:
                    print(f"DSL Prepare: dry-run; would pick under {root} match={match}")
                    exact = str(match.get("exact") or "").strip()
                    dummy_name = exact or f"{save_as}.dryrun"
                    vars[save_as] = (root / dummy_name)
                else:
                    found = _dsl_match_files(root=root, match=match, match_on=match_on)
                    chosen = _dsl_pick_one(found, expect=expect, select=select, what=f"{name} (pick)")
                    vars[save_as] = chosen

            elif op == "extract_tgz":
                src = _dsl_resolve_ref_or_path(step.get("from"), vars=vars, what=f"{name}.from")
                dest = _dsl_resolve_ref_or_path(step.get("to"), vars=vars, what=f"{name}.to")
                _dsl_extract_tgz(src=src, dest=dest, dry_run=dry_run)
                save_as = str(step.get("save_as") or "").strip()
                if save_as:
                    vars[save_as] = dest

            elif op == "extract_zip":
                src = _dsl_resolve_ref_or_path(step.get("from"), vars=vars, what=f"{name}.from")
                dest = _dsl_resolve_ref_or_path(step.get("to"), vars=vars, what=f"{name}.to")
                _dsl_extract_zip(src=src, dest=dest, dry_run=dry_run)
                save_as = str(step.get("save_as") or "").strip()
                if save_as:
                    vars[save_as] = dest

            elif op == "extract_if_single_tar":
                root = _dsl_resolve_ref_or_path(step.get("root"), vars=vars, what=f"{name}.root")
                dest = _dsl_resolve_ref_or_path(step.get("to"), vars=vars, what=f"{name}.to")
                tar_files = [p for p in root.rglob("*.tar") if p.is_file()] if root.exists() else []
                if not tar_files:
                    out_dir = root
                else:
                    tar_path = _dsl_pick_one(tar_files, expect=expect, select=select, what=f"{name} (*.tar)")
                    _dsl_extract_tar(src=tar_path, dest=dest, dry_run=dry_run)
                    out_dir = dest
                save_as = str(step.get("save_as") or "").strip()
                if save_as:
                    vars[save_as] = out_dir

            elif op == "copy_to_payload":
                src = _dsl_resolve_ref_or_path(step.get("from"), vars=vars, what=f"{name}.from")
                to_raw = step.get("to")
                if isinstance(to_raw, str) and str(to_raw).strip():
                    dest = _dsl_resolve_ref_or_path(to_raw, vars=vars, what=f"{name}.to")
                else:
                    dest = upload_payload_dir / src.name

                if dry_run:
                    print(f"DSL Prepare: dry-run; would copy: {src} -> {dest}")
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest)

                also = str(step.get("also_save_as") or "").strip()
                if also:
                    vars[also] = dest

            elif op == "download_jenkins_console":
                build_url = str(step.get("build_url") or "").strip()
                if not build_url:
                    # Fallback: use cfg jenkins.builds.<run>.build_url
                    try:
                        build_url = str(_get_by_dotted_path(cfg, f"jenkins.builds.{current_run}.build_url"))
                    except Exception:
                        build_url = ""
                if not build_url:
                    raise RuntimeError(f"DSL: {name} missing build_url")

                to_raw = step.get("to")
                if isinstance(to_raw, str) and str(to_raw).strip():
                    dest = _dsl_resolve_ref_or_path(to_raw, vars=vars, what=f"{name}.to")
                else:
                    dest = upload_payload_dir / f"jenkins_{current_run}_console.log"

                # Jenkins auth from main cfg
                j_auth = (cfg.get("jenkins") or {}).get("auth") or {}
                if not isinstance(j_auth, dict):
                    j_auth = {}
                j_user = str(j_auth.get("username") or "").strip()
                j_pass = str(j_auth.get("password") or "").strip()
                if not j_user or not j_pass:
                    raise RuntimeError("DSL: Jenkins auth missing (jenkins.auth.username/password)")

                timeout_sec = int((cfg.get("jenkins") or {}).get("timeout_sec", 600))
                _jenkins_download_console_text(
                    build_url=build_url,
                    auth=JenkinsAuth(username=j_user, password=j_pass),
                    dest=dest,
                    timeout_sec=timeout_sec,
                    dry_run=dry_run,
                    show_progress=False,
                )

                save_as = str(step.get("save_as") or "").strip()
                if save_as:
                    vars[save_as] = dest

            else:
                raise RuntimeError(f"DSL: unsupported op={op!r} ({name})")

        except Exception as e:
            if optional:
                print(f"DSL Prepare: optional step skipped: {name} ({op}): {e}")
                save_as = str(step.get("save_as") or "").strip()
                if save_as and save_as not in vars:
                    vars[save_as] = ""
                also = str(step.get("also_save_as") or "").strip()
                if also and also not in vars:
                    vars[also] = ""
                continue
            raise

    return {"vars": vars, "work_dir": work_dir, "upload_payload_dir": upload_payload_dir}


def _dsl_find_share_link_for_filename(run_summary: Dict[str, Any], *, filename: str) -> Tuple[str, str]:
    fn = str(filename or "").strip()
    if not fn:
        return "", ""
    for k, url in _iter_all_share_links(run_summary):
        key_str = str(k)
        base = key_str.replace("\\", "/").rsplit("/", 1)[-1]
        if base == fn or key_str == fn:
            return base, str(url)
    return "", ""

# Module-level DSM client cache to avoid repeated login/logout during placeholder resolution.
# Key = (base_url, username), value = SynologyDsmClient (already logged in).
_dsm_client_cache: Dict[Tuple[str, str], Any] = {}

def _dsl_try_dsm_share_link_for_local_path(
    *,
    cfg: Dict[str, Any],
    run_summary: Dict[str, Any],
    local_path: Path,
) -> Tuple[str, str]:
    remote_dir = str(run_summary.get("remote_dir") or "").strip().rstrip("/")
    if not remote_dir:
        return "", ""

    # Build roots from local_dir + out_dir + extra_dirs.
    roots: List[Path] = []
    for key in ("local_dir", "out_dir"):
        v = str(run_summary.get(key) or "").strip()
        if v:
            roots.append(Path(v).expanduser().resolve())
    for x in (run_summary.get("extra_dirs") or []):
        xs = str(x or "").strip()
        if xs:
            roots.append(Path(xs).expanduser().resolve())

    # DSM client (reuse the same logic as _build_placeholder_replacements)
    nas_cfg = cfg.get("nas") or {}
    if not isinstance(nas_cfg, dict):
        return "", ""
    dsm_cfg = nas_cfg.get("dsm") or {}
    if not isinstance(dsm_cfg, dict):
        dsm_cfg = {}
    dsm_base_url = str(dsm_cfg.get("base_url") or "").strip()
    if not dsm_base_url:
        webdav_cfg = nas_cfg.get("webdav") or {}
        if isinstance(webdav_cfg, dict):
            dsm_base_url = _derive_dsm_base_url_from_webdav(str(webdav_cfg.get("base_url") or "").strip())
    if not dsm_base_url:
        return "", ""
    dsm_verify_tls = bool(dsm_cfg.get("verify_tls", False))
    auth_cfg = dsm_cfg.get("auth") or {}
    if not isinstance(auth_cfg, dict):
        auth_cfg = {}
    dsm_user = str(auth_cfg.get("username") or "").strip()
    dsm_pass = str(auth_cfg.get("password") or "").strip()
    if not dsm_pass:
        dsm_pass = (os.environ.get("DSM_PASSWORD") or os.environ.get("SYNO_PASS") or "").strip()
    if not dsm_user or not dsm_pass:
        return "", ""
    timeout_sec = int(((nas_cfg.get("webdav") or {}) if isinstance(nas_cfg.get("webdav"), dict) else {}).get("timeout_sec") or 30)

    rel = ""
    for r in roots:
        try:
            rel = local_path.relative_to(r).as_posix()
            break
        except Exception:
            continue
    candidates: List[str] = []
    if rel:
        candidates.append(f"{remote_dir}/{rel}")
    candidates.append(f"{remote_dir}/{local_path.name}")

    client = SynologyDsmClient(dsm_base_url, verify_tls=dsm_verify_tls, timeout_sec=timeout_sec)
    cache_key = (dsm_base_url, dsm_user)
    if cache_key in _dsm_client_cache:
        client = _dsm_client_cache[cache_key]
    else:
        try:
            client.login(username=dsm_user, password=dsm_pass)
            _dsm_client_cache[cache_key] = client
        except Exception as e:
            print(f"WARN: DSM login failed for {local_path.name}: {e}", file=sys.stderr)
            return "", ""
    try:
        for rp in candidates:
            try:
                link = client.create_share_link(path=rp)
                if link:
                    return local_path.name, link
            except Exception:
                continue
        print(f"WARN: DSM create_share_link failed for all candidates: {candidates}", file=sys.stderr)
        return "", ""
    finally:
        pass  # Don't logout — keep cached client alive for subsequent calls


def _build_placeholder_replacements_from_dsl(
    *,
    cfg: Dict[str, Any],
    run_summaries: List[Dict[str, Any]],
    dsl_cfg: Dict[str, Any],
    dsl_runtime: Dict[str, Any],
) -> Dict[str, str]:
    placeholders = dsl_cfg.get("placeholders") or {}
    if not isinstance(placeholders, dict):
        raise RuntimeError("DSL: placeholders must be an object")
    strategy = placeholders.get("strategy") or {}
    if not isinstance(strategy, dict):
        strategy = {}
    mappings = placeholders.get("mappings") or {}
    if not isinstance(mappings, dict):
        raise RuntimeError("DSL: placeholders.mappings must be an object")

    nas_share_priority = strategy.get("nas_share_priority") or ["local_dsm_create_share_link", "run_summary_share_links"]
    if not isinstance(nas_share_priority, list):
        nas_share_priority = ["local_dsm_create_share_link", "run_summary_share_links"]
    # missing_placeholder policy:
    # - keep (default): keep empty string and continue
    # - warn: warn and continue
    # - error: raise when a required placeholder can't be resolved
    missing_policy = str(strategy.get("missing_placeholder") or "keep").strip().lower() or "keep"

    vars: Dict[str, Any] = dsl_runtime.get("vars") or {}

    def _get_run_sum(name: str) -> Dict[str, Any]:
        try:
            return _get_run_summary(run_summaries, name)
        except Exception:
            return {}

    out: Dict[str, str] = {str(k): "" for k in mappings.keys()}
    # Track filename for each nas_share placeholder so filename_for_share can reference it
    _filename_for_share: Dict[str, str] = {}

    def _dsl_find_share_link_by_match(*, run_sum: Dict[str, Any], match: Dict[str, Any], what: str) -> Tuple[str, str]:
        if not isinstance(match, dict):
            return "", ""

        exact = str(match.get("exact") or "").strip()
        if exact:
            fname, v = _dsl_find_share_link_for_filename(run_sum, filename=exact)
            if v:
                return fname, v

        fb = str(match.get("fallback_regex") or "").strip()
        if fb:
            try:
                return _find_share_link_by_regex(run_sum, filename_regex=re.compile(fb, re.IGNORECASE), what=what)
            except Exception:
                pass

        rx = str(match.get("regex") or "").strip()
        if rx:
            try:
                return _find_share_link_by_regex(run_sum, filename_regex=re.compile(rx, re.IGNORECASE), what=what)
            except Exception:
                pass

        return "", ""

    for ph, rule in mappings.items():
        phs = str(ph)
        if not phs.strip():
            continue
        if not isinstance(rule, dict):
            raise RuntimeError(f"DSL: mapping for {phs} must be an object")

        mode = str(rule.get("mode") or "").strip().lower()
        required = bool(rule.get("required", False))
        run_name = str(rule.get("run") or "").strip().lower() or "release"
        run_sum = _get_run_sum(run_name)

        val = ""
        filename = ""
        if mode == "var":
            path = str(rule.get("path") or "").strip()
            if not path:
                raise RuntimeError(f"DSL: var mapping for {phs} missing path")
            try:
                v = _get_by_dotted_path(cfg, path)
                val = "" if v is None else str(v)
            except Exception:
                val = ""

        elif mode == "const":
            val = "" if rule.get("value") is None else str(rule.get("value"))

        elif mode == "nas_share":
            local_path: Optional[Path] = None
            if rule.get("from"):
                ref = str(rule.get("from") or "").strip()
                v = vars.get(ref)
                if isinstance(v, Path):
                    local_path = v
                elif isinstance(v, str) and v.strip():
                    local_path = Path(v).expanduser().resolve()

            if local_path is None and isinstance(rule.get("match"), dict):
                # Resolve local file by match within run roots.
                m = rule.get("match") or {}
                mon = str(rule.get("match_on") or "name").strip().lower() or "name"
                roots: List[Path] = []
                for key in ("local_dir", "out_dir"):
                    vroot = str(run_sum.get(key) or "").strip()
                    if vroot:
                        roots.append(Path(vroot).expanduser().resolve())
                for x in (run_sum.get("extra_dirs") or []):
                    xs = str(x or "").strip()
                    if xs:
                        roots.append(Path(xs).expanduser().resolve())

                candidates: List[Path] = []
                for r in roots:
                    candidates.extend(_dsl_match_files(root=r, match=m, match_on=mon))
                if candidates:
                    local_path = _dsl_pick_one(candidates, expect=str(rule.get("expect") or "one"), select=str(rule.get("select") or "shortest_path"), what=f"{phs} local match")

            # Resolve share link by priority.
            for how in [str(x) for x in nas_share_priority]:
                if how == "run_summary_share_links":
                    if local_path is not None:
                        filename, val = _dsl_find_share_link_for_filename(run_sum, filename=local_path.name)
                    elif isinstance(rule.get("match"), dict):
                        filename, val = _dsl_find_share_link_by_match(run_sum=run_sum, match=(rule.get("match") or {}), what=phs)
                    if val:
                        break
                elif how == "local_dsm_create_share_link":
                    if local_path is not None:
                        filename, val = _dsl_try_dsm_share_link_for_local_path(cfg=cfg, run_summary=run_sum, local_path=local_path)
                    if val:
                        break
            
            # If we have both filename and URL, use a special format that can be parsed
            # by the Docx replacement logic to create a real hyperlink.
            if val and filename:
                val = f"{filename}|{val}"
            
            # Save filename for corresponding filename_for_share placeholder (legacy support)
            if filename:
                _filename_for_share[phs] = filename

        elif mode == "filename_for_share":
            # Extract filename from corresponding nas_share placeholder by parsing its rule
            share_ph = str(rule.get("share_placeholder") or "").strip()
            if share_ph in mappings:
                share_rule = mappings[share_ph]
                if isinstance(share_rule, dict):
                    # Re-resolve the local path for the corresponding nas_share placeholder
                    share_run_name = str(share_rule.get("run") or "").strip().lower() or "release"
                    share_run_sum = _get_run_sum(share_run_name)
                    
                    local_path_for_share: Optional[Path] = None
                    if share_rule.get("from"):
                        ref = str(share_rule.get("from") or "").strip()
                        v = vars.get(ref)
                        if isinstance(v, Path):
                            local_path_for_share = v
                        elif isinstance(v, str) and v.strip():
                            local_path_for_share = Path(v).expanduser().resolve()
                    
                    if local_path_for_share is None and isinstance(share_rule.get("match"), dict):
                        # Resolve local file by match within run roots
                        m = share_rule.get("match") or {}
                        mon = str(share_rule.get("match_on") or "name").strip().lower() or "name"
                        roots: List[Path] = []
                        for key in ("local_dir", "out_dir"):
                            vroot = str(share_run_sum.get(key) or "").strip()
                            if vroot:
                                roots.append(Path(vroot).expanduser().resolve())
                        for x in (share_run_sum.get("extra_dirs") or []):
                            xs = str(x or "").strip()
                            if xs:
                                roots.append(Path(xs).expanduser().resolve())
                        
                        candidates: List[Path] = []
                        for r in roots:
                            candidates.extend(_dsl_match_files(root=r, match=m, match_on=mon))
                        if candidates:
                            local_path_for_share = _dsl_pick_one(candidates, expect=str(share_rule.get("expect") or "one"), select=str(share_rule.get("select") or "shortest_path"), what=f"{share_ph} local match")
                    
                    # Extract filename from resolved path
                    if local_path_for_share:
                        val = local_path_for_share.name
                    else:
                        val = ""
            else:
                val = ""

        else:
            raise RuntimeError(f"DSL: unsupported placeholder mode={mode!r} for {phs}")

        if required and not str(val or "").strip():
            if missing_policy == "error":
                raise RuntimeError(f"DSL: required placeholder missing: {phs}")
            print(f"WARN: DSL required placeholder missing (skip): {phs}", file=sys.stderr)
            val = ""

        if not str(val or "").strip() and missing_policy == "keep":
            out[phs] = ""
        else:
            out[phs] = "" if val is None else str(val)

    # Add changelog link if enabled
    changelog_link = _build_changelog_link(cfg=cfg)
    if changelog_link:
        out["{{REL_CHANGELOG_LINK}}"] = changelog_link

    # User overrides (same semantics as legacy: empty string does NOT override)
    feishu_cfg = cfg.get("feishu") or {}
    if isinstance(feishu_cfg, dict):
        overrides = feishu_cfg.get("placeholder_overrides")
        if isinstance(overrides, dict):
            for k, v in overrides.items():
                ks = str(k)
                if not ks:
                    continue
                if v is None:
                    continue
                vs = str(v)
                if not vs.strip():
                    continue
                out[ks] = vs

    return {k: ("" if v is None else str(v)) for k, v in out.items() if str(k).strip()}


def _changelog_state_path() -> Path:
    return Path(__file__).with_name("output") / "changelog_job.json"


def _load_changelog_state() -> Dict[str, Any]:
    path = _changelog_state_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_changelog_state(state: Dict[str, Any]) -> None:
    path = _changelog_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _update_changelog_state(**updates: Any) -> Dict[str, Any]:
    state = _load_changelog_state()
    for key, value in updates.items():
        if value is None:
            continue
        state[str(key)] = value
    state["updated_at"] = time.strftime("%Y%m%d_%H%M%S")
    _save_changelog_state(state)
    return state


def _changelog_state_finished_without_diff() -> bool:
    state = _load_changelog_state()
    return str(state.get("status") or "").strip().lower() == "finished_no_diff"


def _trigger_changelog_job_and_get_feishu_link(*, job_url: str, params: Dict[str, str], j_auth: Dict[str, Any], timeout_sec: int = 600) -> str:
    """Trigger Jenkins CMP-JIRA-GIT job and extract Feishu link from console output.
    
    Args:
        job_url: Jenkins job URL (e.g. https://jenkins.huami.com/job/DownStream/job/CMP-JIRA-GIT)
        params: Parameters dict for the job (BEFORE_VERSION_NAME, AFTER_VERSION_NAME, etc.)
        j_auth: Jenkins auth config (username, password)
        timeout_sec: Timeout in seconds (default 600s = 10 minutes)
    
    Returns:
        Feishu link URL if found, empty string if failed/timeout
    """
    import time
    import re
    
    try:
        from urllib.request import Request, urlopen, HTTPError, build_opener, HTTPCookieProcessor
        from urllib.parse import urlencode
        from base64 import b64encode
        from http.cookiejar import CookieJar
        
        j_user = str(j_auth.get("username", ""))
        j_pass = str(j_auth.get("password", ""))
        auth_str = b64encode(f"{j_user}:{j_pass}".encode()).decode()
        
        # Create an opener with cookie support to maintain session for CSRF crumb
        cj = CookieJar()
        opener = build_opener(HTTPCookieProcessor(cj))
        
        base_url = job_url.rstrip("/")
        
        # Step 1: Get Jenkins-Crumb (CSRF token)
        print(f"Changelog: getting Jenkins CSRF token...")
        crumb_paths = [
            f"{job_url.split('/job/')[0]}/crumbIssuer/api/json",
            f"{base_url.rsplit('/job/', 1)[0]}/crumbIssuer/api/json"
        ]
        crumb = ""
        crumb_field = "Jenkins-Crumb"
        
        for cp in list(dict.fromkeys(crumb_paths)): # Unique paths
            try:
                print(f"DEBUG: trying crumb URL: {cp}")
                req_crumb = Request(cp, headers={"Authorization": f"Basic {auth_str}"})
                with opener.open(req_crumb, timeout=10) as resp_crumb:
                    crumb_data = json.loads(resp_crumb.read().decode())
                    crumb = str(crumb_data.get("crumb", ""))
                    crumb_field = str(crumb_data.get("crumbRequestField", "Jenkins-Crumb"))
                    if crumb:
                        print(f"Changelog: got crumb from {cp}: {crumb[:4]}***")
                        break
            except Exception as e:
                print(f"DEBUG: crumb fetch failed for {cp}: {e}")
        
        # Step 2: Get latest build number
        print(f"Changelog: querying latest build number...")
        api_url = f"{base_url}/api/json"
        req_api = Request(api_url, headers={"Authorization": f"Basic {auth_str}"})
        last_build_num = 0
        try:
            with opener.open(req_api, timeout=10) as resp_api:
                job_data = json.loads(resp_api.read().decode())
                last_build_num = job_data.get("lastBuild", {}).get("number", 0)
            print(f"Changelog: current last build number: {last_build_num}")
        except Exception as e:
            print(f"WARN: Could not get last build number: {e}")
        
        # Step 3: Trigger build
        print(f"Changelog: triggering Jenkins job with params...")
        trigger_url = f"{base_url}/buildWithParameters"
        
        filtered_params = {str(k): str(v) for k, v in params.items() if k and v is not None}
        print(f"Changelog: parameters: {json.dumps(filtered_params, ensure_ascii=False, indent=2)}")
        
        query_data = urlencode(filtered_params).encode('utf-8')
        
        headers = {
            "Authorization": f"Basic {auth_str}",
            "Content-Type": "application/x-www-form-urlencoded"
        }
        if crumb:
            headers[crumb_field] = crumb
        
        req_trigger = Request(trigger_url, data=query_data, headers=headers, method="POST")
        
        try:
            with opener.open(req_trigger, timeout=30) as resp_trigger:
                status = resp_trigger.getcode()
                print(f"Changelog: trigger response code: {status}")
        except HTTPError as e:
            if e.code in (200, 201, 302): # Success or redirect
                print(f"Changelog: trigger success (code {e.code})")
            elif e.code == 403:
                print(f"ERROR: Jenkins trigger failed with 403 Forbidden. "
                      "This usually means the user lacks 'Build' permissions or an API Token is required.")
                # Try one more fallback without crumb (sometimes API Token doesn't need crumb)
                print("Changelog: trying trigger without crumb as fallback...")
                req_no_crumb = Request(trigger_url, data=query_data, headers={"Authorization": f"Basic {auth_str}", "Content-Type": "application/x-www-form-urlencoded"}, method="POST")
                try:
                    with opener.open(req_no_crumb, timeout=30) as resp_nc:
                        print(f"Changelog: no-crumb trigger response: {resp_nc.getcode()}")
                except Exception as e_nc:
                    print(f"DEBUG: no-crumb fallback also failed: {e_nc}")
                    raise e
            else:
                raise
        
        # Step 4: Wait for new build to be created
        print(f"Changelog: waiting for build to be queued...")
        time.sleep(3)
        
        build_number = 0
        for attempt in range(15):
            try:
                req_api_poll = Request(api_url, headers={"Authorization": f"Basic {auth_str}"})
                with opener.open(req_api_poll, timeout=10) as resp_api:
                    job_data = json.loads(resp_api.read().decode())
                    current_build_num = job_data.get("lastBuild", {}).get("number", 0)
                    if current_build_num > last_build_num:
                        build_number = current_build_num
                        print(f"Changelog: build #{build_number} created and running")
                        _update_changelog_state(
                            job_url=job_url,
                            build_number=int(build_number),
                            build_url=f"{base_url}/{build_number}/",
                            status="running",
                            result="",
                            triggered_at=time.strftime("%Y%m%d_%H%M%S"),
                        )
                        break
            except Exception:
                pass
            time.sleep(3)
        else:
            # Fallback
            print(f"WARN: Build not started yet; checking again in 10s...")
            time.sleep(10)
            try:
                with opener.open(req_api, timeout=10) as resp_api:
                    job_data = json.loads(resp_api.read().decode())
                    build_number = job_data.get("lastBuild", {}).get("number", 0)
                    if build_number > last_build_num:
                        print(f"Changelog: build #{build_number} detected")
                        _update_changelog_state(
                            job_url=job_url,
                            build_number=int(build_number),
                            build_url=f"{base_url}/{build_number}/",
                            status="running",
                            result="",
                            triggered_at=time.strftime("%Y%m%d_%H%M%S"),
                        )
                    else:
                        return ""
            except Exception:
                return ""
        
        # Step 5: Poll status
        print(f"Changelog: monitoring build #{build_number}...")
        build_api_url = f"{base_url}/{build_number}/api/json"
        console_url = f"{base_url}/{build_number}/consoleText"
        result = ""
        
        start_time = time.time()
        while time.time() - start_time < timeout_sec:
            try:
                req_build_api = Request(build_api_url, headers={"Authorization": f"Basic {auth_str}"})
                with opener.open(req_build_api, timeout=10) as resp_build_api:
                    build_data = json.loads(resp_build_api.read().decode())
                
                if not build_data.get("building", True):
                    result = build_data.get("result", "")
                    print(f"Changelog: build finished: {result}")
                    _update_changelog_state(
                        job_url=job_url,
                        build_number=int(build_number),
                        build_url=f"{base_url}/{build_number}/",
                        status="finished",
                        result=str(result or ""),
                        finished_at=time.strftime("%Y%m%d_%H%M%S"),
                    )
                    break
            except Exception:
                pass
            time.sleep(10)
        else:
            return ""
        
        # Step 6: Extract link
        print(f"Changelog: extracting link from #{build_number}...")
        try:
            req_console = Request(console_url, headers={"Authorization": f"Basic {auth_str}"})
            with opener.open(req_console, timeout=30) as resp_console:
                console_text = resp_console.read().decode(errors='replace')
            
            feishu_match = re.search(r'差分报告飞书链接\s*[:：]\s*(https://zepp\.feishu\.cn/[^\s\n\r]+)', console_text, re.IGNORECASE)
            no_diff_match = re.search(r'未生成差分报告，请检查差分版本间是否无提交记录', console_text, re.IGNORECASE)
            if feishu_match:
                link = feishu_match.group(1).strip().rstrip('.,;:')
                print(f"✓ Changelog link: {link}")
                _update_changelog_state(
                    job_url=job_url,
                    build_number=int(build_number),
                    build_url=f"{base_url}/{build_number}/",
                    link=link,
                    status="finished",
                    result=str(result or ""),
                )
                return link
            if no_diff_match:
                print(f"差分报告：构建 #{build_number} 已完成但未生成差分报告，停止等待")
                _update_changelog_state(
                    job_url=job_url,
                    build_number=int(build_number),
                    build_url=f"{base_url}/{build_number}/",
                    status="finished_no_diff",
                    result=str(result or ""),
                    link="",
                )
                return ""
            print(f"WARN: Link not found in #{build_number}")
            return ""
        except Exception:
            return ""
    
    except Exception as e:
        print(f"WARN: Changelog job trigger failed: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return ""


def _build_changelog_link(*, cfg: Dict[str, Any]) -> str:
    """Build Changelog link by triggering Jenkins job and extracting Feishu link.
    
    Automatically triggers CMP-JIRA-GIT job, waits for completion (~5 mins),
    and extracts the Feishu Changelog document link from the job console output.
    """
    # If jenkins_trigger_build.py already triggered a changelog job, detect and
    # reuse that build here (non-duplicating). This allows the link building to
    # overlap with download/upload time.
    try_reuse_state = True
    changelog_cfg = cfg.get("changelog") or {}
    if not isinstance(changelog_cfg, dict):
        return ""
    
    if not bool(changelog_cfg.get("enabled", False)):
        return ""
    
    job_url = str(changelog_cfg.get("jenkins_job_url") or "").strip()
    if not job_url:
        return ""
    
    params = changelog_cfg.get("params") or {}
    if not isinstance(params, dict):
        return ""

    state = _load_changelog_state()
    if isinstance(changelog_cfg, dict):
        for key in ("job_url", "build_number", "build_url", "status", "result", "triggered_at", "updated_at", "finished_at", "link"):
            value = changelog_cfg.get(key)
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            state[str(key)] = value
    
    # Fast path: try reuse an existing async-triggered job if present.
    def _extract_from_existing(job_url_base: str, build_number: int, j_auth: Dict[str, Any], timeout_sec: int) -> Tuple[str, bool]:
        import re as _re
        from urllib.request import Request as _Req, build_opener as _opener, HTTPCookieProcessor as _HTTPCookieProcessor
        from base64 import b64encode as _b64
        from http.cookiejar import CookieJar as _CJ
        import time as _t
        auth_str = _b64(f"{str(j_auth.get('username') or '')}:{str(j_auth.get('password') or '')}".encode()).decode()
        cj = _CJ()
        op = _opener(_HTTPCookieProcessor(cj))
        console_url = f"{job_url_base.rstrip('/')}/{build_number}/consoleText"
        start = _t.time()
        # Poll consoleText repeatedly until link appears, no-diff is confirmed, or timeout reached.
        while True:
            try:
                req = _Req(console_url, headers={"Authorization": f"Basic {auth_str}"})
                with op.open(req, timeout=30) as resp:
                    txt = resp.read().decode(errors="replace")
                m = _re.search(r'差分报告飞书链接\s*[:：]\s*(https://zepp\.feishu\.cn/[^\s\n\r]+)', txt, _re.IGNORECASE)
                if m:
                    return m.group(1).strip().rstrip('.,;:'), False
                if _re.search(r'未生成差分报告，请检查差分版本间是否无提交记录', txt, _re.IGNORECASE):
                    return "", True
            except Exception:
                pass
            # build 已结束（building=false）就立即退出，不再空转到超时
            try:
                _api_url = f"{job_url_base.rstrip('/')}/{build_number}/api/json"
                _req2 = _Req(_api_url, headers={"Authorization": f"Basic {auth_str}"})
                with op.open(_req2, timeout=30) as _resp2:
                    _data = json.loads(_resp2.read().decode(errors="replace"))
                if not bool(_data.get("building", False)):
                    return "", False
            except Exception:
                pass
            if int(timeout_sec) > 0 and (_t.time() - start) >= int(timeout_sec):
                break
            _t.sleep(5)
        return "", False

    def _get_build_result(job_url_base: str, build_number: int, j_auth: Dict[str, Any]) -> Tuple[str, bool]:
        import re as _re
        from urllib.request import Request as _Req, build_opener as _opener, HTTPCookieProcessor as _HTTPCookieProcessor
        from base64 import b64encode as _b64
        from http.cookiejar import CookieJar as _CJ
        auth_str = _b64(f"{str(j_auth.get('username') or '')}:{str(j_auth.get('password') or '')}".encode()).decode()
        cj = _CJ()
        op = _opener(_HTTPCookieProcessor(cj))
        api_url = f"{job_url_base.rstrip('/')}/{build_number}/api/json"
        try:
            req = _Req(api_url, headers={"Authorization": f"Basic {auth_str}"})
            with op.open(req, timeout=30) as resp:
                data = json.loads(resp.read().decode(errors="replace"))
            return str(data.get("result") or ""), bool(data.get("building", False))
        except Exception:
            return "", False

    if try_reuse_state:
        try:
            if isinstance(state, dict) and int(state.get("build_number") or 0) > 0:
                bn = int(state.get("build_number"))
                base = str(state.get("job_url") or job_url).rstrip("/")
                build_url = str(state.get("build_url") or f"{base}/{bn}/").strip()
                print(f"Changelog: reusing async-triggered build #{bn} -> {build_url}")
                # Guard: skip polling if build already has a terminal result.
                known_result = str(state.get("result") or "").strip().upper()
                known_status = str(state.get("status") or "").strip()
                if known_result in ("FAILURE", "ABORTED", "NOT_BUILT"):
                    print(f"Changelog: build #{bn} already finished with result={known_result}, trying to extract link...")
                    # 即使 job 结果为失败，只要 console 里打印过「差分报告飞书链接」就照常回填；
                    # 拿不到链接才跳过。job 已结束，用短超时即可，无需长时间轮询。
                    link, _no_diff = _extract_from_existing(
                        job_url_base=base,
                        build_number=bn,
                        j_auth=(cfg.get("jenkins") or {}).get("auth") or {},
                        timeout_sec=30,
                    )
                    if link:
                        _update_changelog_state(
                            job_url=base,
                            build_number=bn,
                            build_url=build_url,
                            status="finished",
                            result=known_result,
                            link=link,
                        )
                        print(f"Changelog: extracted link from finished build #{bn}: {link}")
                        return link
                    print(f"Changelog: no link found in finished build #{bn}, skipping")
                    return ""
                if known_status in ("finished_no_diff", "failed"):
                    print(f"Changelog: build #{bn} already {known_status}, skipping")
                    return ""
                _update_changelog_state(
                    job_url=base,
                    build_number=bn,
                    build_url=build_url,
                    status=str(state.get("status") or "running"),
                    result=str(state.get("result") or ""),
                    link=str(state.get("link") or ""),
                    triggered_at=state.get("triggered_at"),
                )
                link, no_diff = _extract_from_existing(
                    job_url_base=base,
                    build_number=bn,
                    j_auth=(cfg.get("jenkins") or {}).get("auth") or {},
                    timeout_sec=int(changelog_cfg.get("timeout_sec", 600)),
                )
                if no_diff:
                    print(f"差分报告：复用的构建 #{bn} 已完成但未生成差分报告，停止等待")
                    result, building = _get_build_result(
                        job_url_base=base,
                        build_number=bn,
                        j_auth=(cfg.get("jenkins") or {}).get("auth") or {},
                    )
                    _update_changelog_state(
                        job_url=base,
                        build_number=bn,
                        build_url=build_url,
                        status="finished_no_diff" if not building else "running",
                        result=result,
                        link="",
                        finished_at=time.strftime("%Y%m%d_%H%M%S") if not building else None,
                    )
                    return ""
                if link:
                    result, building = _get_build_result(
                        job_url_base=base,
                        build_number=bn,
                        j_auth=(cfg.get("jenkins") or {}).get("auth") or {},
                    )
                    _update_changelog_state(
                        job_url=base,
                        build_number=bn,
                        build_url=build_url,
                        status="running" if building else "finished",
                        result=result,
                        link=link,
                        finished_at=time.strftime("%Y%m%d_%H%M%S") if not building else None,
                    )
                    return link
        except Exception:
            # Fall through to normal path
            pass
    
    # Expand placeholders in param values
    expanded_params: Dict[str, str] = {}
    for key, value in params.items():
        key_str = str(key)
        if not key_str:
            continue
        val_raw = str(value or "")
        
        # Robust placeholder expansion: find all ${path} and replace them
        def _replacer(match):
            path = match.group(1)
            try:
                resolved = _get_by_dotted_path(cfg, path)
                return "" if resolved is None else str(resolved)
            except Exception:
                return match.group(0) # Keep original if not found
        
        val_expanded = re.sub(r"\$\{([^}]+)\}", _replacer, val_raw)
        expanded_params[key_str] = val_expanded

    # 特殊处理：CMP-JIRA-GIT 差分的 MANIFEST_FILE 去 _64m/_32/_64 后缀。
    # milan_64m / pamir_64m 应传 milan.xml / pamir.xml，而非 milan_64m.xml / pamir_64m.xml。
    if "MANIFEST_FILE" in expanded_params:
        rel_for_manifest = cfg.get("release") or {}
        xml_name = str(rel_for_manifest.get("xml_name") or "").strip()
        if not xml_name:
            project = str(rel_for_manifest.get("project") or "").strip()
            xml_name = re.sub(r"_\d+m?$", "", project)
        if xml_name:
            expanded_params["MANIFEST_FILE"] = f"{xml_name}.xml"
    
    # Get Jenkins auth from config
    j_auth = (cfg.get("jenkins") or {}).get("auth") or {}
    if not isinstance(j_auth, dict):
        j_auth = {}
    
    # Trigger job and get Feishu link
    timeout = int(changelog_cfg.get("timeout_sec", 600))
    feishu_link = _trigger_changelog_job_and_get_feishu_link(
        job_url=job_url,
        params=expanded_params,
        j_auth=j_auth,
        timeout_sec=timeout
    )
    
    return feishu_link


def _pattern_variants(pat: str) -> List[str]:
    # Jenkins artifacts often include both top-level files (e.g. 'a.zip') and nested paths.
    # Patterns like '**/*' or '**/*.log' should match BOTH cases.
    pat = pat.strip()
    variants = [pat]
    while pat.startswith("**/"):
        pat = pat[3:]
        variants.append(pat)
    return list(dict.fromkeys(v for v in variants if v))


def _posix_glob_match(path_posix: str, patterns: List[str]) -> bool:
    # Match if any pattern matches; empty means match-all.
    if not patterns:
        return True
    for pat in patterns:
        for v in _pattern_variants(pat):
            if fnmatch.fnmatch(path_posix, v):
                return True
    return False


@dataclass(frozen=True)
class JenkinsAuth:
    username: str
    password: str


class CurlNetrc:
    def __init__(self, base_url: str, username: str, password: str):
        self._base_url = base_url
        self._username = username
        self._password = password
        self.path: Optional[str] = None

    def __enter__(self) -> "CurlNetrc":
        tmp = tempfile.NamedTemporaryFile("w", prefix="netrc_", delete=False)
        try:
            host = self._base_url.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0]
            tmp.write(f"machine {host}\n")
            tmp.write(f"  login {self._username}\n")
            tmp.write(f"  password {self._password}\n")
            tmp.flush()
            os.chmod(tmp.name, 0o600)
            self.path = tmp.name
        finally:
            tmp.close()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.path:
            try:
                os.unlink(self.path)
            except OSError:
                pass
            self.path = None


def _collapse_to_single_line(text: str) -> str:
    s = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _read_text_best_effort(path: Path) -> str:
    for enc in ("utf-8", "utf-8-sig", "mbcs", "gbk"):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def _get_run_summary(run_summaries: List[Dict[str, Any]], name: str) -> Dict[str, Any]:
    want = str(name or "").strip().lower()
    for r in run_summaries:
        if str(r.get("name") or "").strip().lower() == want:
            return r
    raise KeyError(f"run summary not found: {name}")


def _format_preserving_double_braces(template: str, ctx: Dict[str, Any]) -> str:
    """Like str.format(**ctx) but preserves doubled braces.

    Python's format() turns '{{x}}' into '{x}', which breaks our '{{REL_*}}'
    placeholder convention. We temporarily replace doubled braces with sentinels,
    run format(), then restore.
    """
    s = str(template or "")
    left = "\x00DBL_LBRACE\x00"
    right = "\x00DBL_RBRACE\x00"
    s = s.replace("{{", left).replace("}}", right)
    s = s.format(**ctx)
    return s.replace(left, "{{").replace(right, "}}")


def _iter_all_share_links(run_summary: Dict[str, Any]) -> Iterable[Tuple[str, str]]:
    main = run_summary.get("share_links_main") or {}
    if isinstance(main, dict):
        for k, v in main.items():
            ks = str(k)
            vs = str(v)
            if ks and vs:
                yield ks, vs

    extra = run_summary.get("share_links_extra") or {}
    if isinstance(extra, dict):
        for extra_key, links in extra.items():
            # special_archives is stored as {filename: {filename: url}}
            if extra_key == "special_archives" and isinstance(links, dict):
                for _fname, one in links.items():
                    if isinstance(one, dict):
                        for k, v in one.items():
                            ks = str(k)
                            vs = str(v)
                            if ks and vs:
                                yield ks, vs
                continue

            if isinstance(links, dict):
                for k, v in links.items():
                    ks = str(k)
                    vs = str(v)
                    if ks and vs:
                        yield ks, vs


def _find_share_link_by_regex(run_summary: Dict[str, Any], *, filename_regex: Pattern, what: str) -> Tuple[str, str]:
    matches: List[Tuple[str, str]] = []
    for k, url in _iter_all_share_links(run_summary):
        # Keys may be relative paths like "binary/foo.zip"; match against both
        # the full key and its basename for robustness.
        key_str = str(k)
        base = key_str.replace("\\", "/").rsplit("/", 1)[-1]
        if filename_regex.search(key_str) or filename_regex.search(base):
            matches.append((base, url))
    if not matches:
        raise RuntimeError(f"Share link not found for {what} (pattern={filename_regex.pattern})")
    # Sort by key length then key string to pick shortest/best match
    matches.sort(key=lambda x: (len(x[0]), x[0]))
    return matches[0]


def _write_placeholder_mapping_file(*, mapping: Dict[str, str], doc_name: str) -> Path:
    out_dir = Path(__file__).with_name("output")
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = _now_timestamp()
    safe = _safe_filename(doc_name or "wiki")
    path = out_dir / f"feishu_placeholder_mapping_{safe}_{ts}.json"
    path.write_text(json.dumps(mapping, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _build_placeholder_replacements(*, cfg: Dict[str, Any], run_summaries: List[Dict[str, Any]]) -> Dict[str, str]:
    release_cfg = cfg.get("release") or {}
    if not isinstance(release_cfg, dict):
        release_cfg = {}

    device_name = str(release_cfg.get("device_name") or release_cfg.get("device") or release_cfg.get("project") or "").strip()
    stage = str(release_cfg.get("stage") or "").strip()
    version = str(release_cfg.get("version") or "").strip()
    branch = str(release_cfg.get("notes") or release_cfg.get("branch") or "").strip()

    # Always emit a stable set of placeholders so the mapping file can be used
    # for manual/automated replacement even if some values are not available.
    expected_placeholders = [
        "{{REL_DEVICE_NAME}}",
        "{{REL_STAGE}}",
        "{{REL_VERSION}}",
        "{{REL_BRANCH}}",
        "{{REL_APP_TAG}}",
        "{{REL_BOOT_TAG}}",
        "{{REL_RECOVERY_TAG}}",
        "{{REL_FCT_TAG}}",
        "{{REL_BOOTLOADER_MANIFEST_INFO}}",
        "{{REL_RECOVERY_MANIFEST_INFO}}",
        "{{REL_APP_MANIFEST_INFO}}",
        "{{REL_RELEASE_FULL_ARCHIVE}}",
        "{{REL_RELEASE_OTA_CLOUD_ARCHIVE}}",
        "{{REL_RELEASE_OTA_SIGN_ZIP}}",
        "{{REL_RELEASE_FACTORY_TOOL_ZIP}}",
        "{{REL_RELEASE_OTA_SLEEP_ARCHIVE}}",
        "{{REL_MONKEY_FULL_ARCHIVE}}",
        "{{REL_MD5SUM_FILE}}",
        "{{REL_CI_BUILD_URL}}",
        "{{REL_BUILD_LOG_FILE}}",
        "{{REL_TSCAN_BOOT}}",
        "{{REL_TSCAN_OTA}}",
        "{{REL_TSCAN_RECOVERY}}",
    ]

    repl: Dict[str, str] = {k: "" for k in expected_placeholders}
    repl.update({
        "{{REL_DEVICE_NAME}}": device_name,
        "{{REL_STAGE}}": stage,
        "{{REL_VERSION}}": version,
        "{{REL_BRANCH}}": branch,
    })

    # Jenkins trigger tags (best-effort)
    def _try_cfg_str(*paths: str) -> str:
        for p in paths:
            try:
                v = _get_by_dotted_path(cfg, p)
                s = "" if v is None else str(v)
                if s.strip():
                    return s.strip()
            except Exception:
                continue
        return ""

    repl["{{REL_APP_TAG}}"] = _try_cfg_str(
        "vars.tag",
        "jenkins.triggers.release.parameters.TAG_NAME",
        "triggers.release.TAG_NAME",
    )
    repl["{{REL_BOOT_TAG}}"] = _try_cfg_str(
        "vars.boot_tag",
        "jenkins.triggers.release.parameters.BOOT_TAG_NAME",
        "triggers.release.BOOT_TAG_NAME",
    )
    repl["{{REL_RECOVERY_TAG}}"] = _try_cfg_str(
        "vars.recovery_tag",
        "jenkins.triggers.release.parameters.RECOVERY_TAG_NAME",
        "triggers.release.RECOVERY_TAG_NAME",
    )
    repl["{{REL_FCT_TAG}}"] = _try_cfg_str(
        "vars.fct_tag",
        "jenkins.triggers.release.parameters.FCT_TAG_NAME",
        "triggers.release.FCT_TAG_NAME",
    )

    # Links / URLs
    try:
        repl["{{REL_CI_BUILD_URL}}"] = str(_get_by_dotted_path(cfg, "jenkins.builds.release.build_url"))
    except Exception:
        pass

    # Share links (best-effort)
    rel_sum: Dict[str, Any] = {}
    dbg_sum: Dict[str, Any] = {}
    tscan_sum: Dict[str, Any] = {}
    try:
        rel_sum = _get_run_summary(run_summaries, "release")
    except Exception:
        pass
    try:
        dbg_sum = _get_run_summary(run_summaries, "debug")
    except Exception:
        pass
    try:
        tscan_sum = _get_run_summary(run_summaries, "tscan")
    except Exception:
        pass

    def _roots_from_run_sum(run_sum: Dict[str, Any]) -> List[Path]:
        roots: List[Path] = []
        if not run_sum:
            return roots
        for key2 in ("out_dir", "local_dir"):
            v = str(run_sum.get(key2) or "").strip()
            if v:
                roots.append(Path(v).expanduser().resolve())
        for x in (run_sum.get("extra_dirs") or []):
            xs = str(x or "").strip()
            if xs:
                roots.append(Path(xs).expanduser().resolve())
        return roots

    roots_release = _roots_from_run_sum(rel_sum)
    roots_debug = _roots_from_run_sum(dbg_sum)

    # If prepare mode is used, special archives are often staged under
    #   <workspace>/_work/...
    # Add the _work folder as an additional search root when present.
    for r in list(roots_release):
        try:
            if r.name.lower() == "upload_payload":
                work_root = r.parent / "_work"
            else:
                continue
            if work_root.exists() and work_root not in roots_release:
                roots_release.append(work_root)
        except Exception:
            continue

    # If share links were skipped/failed during pipeline run, try generating them on-demand
    # for a small set of placeholders by locating the local file and calling DSM(FileStation)
    # create_share_link against the known remote_dir.
    dsm_client: Optional[Any] = None

    def _get_dsm_client() -> Optional[Any]:
        nonlocal dsm_client
        if dsm_client is not None:
            return dsm_client

        nas_cfg = cfg.get("nas") or {}
        if not isinstance(nas_cfg, dict):
            return None

        dsm_cfg = nas_cfg.get("dsm") or {}
        if not isinstance(dsm_cfg, dict):
            dsm_cfg = {}

        dsm_base_url = str(dsm_cfg.get("base_url") or "").strip()
        if not dsm_base_url:
            webdav_cfg = nas_cfg.get("webdav") or {}
            if isinstance(webdav_cfg, dict):
                dsm_base_url = _derive_dsm_base_url_from_webdav(str(webdav_cfg.get("base_url") or "").strip())
        if not dsm_base_url:
            return None

        dsm_verify_tls = bool(dsm_cfg.get("verify_tls", False))
        auth_cfg = dsm_cfg.get("auth") or {}
        if not isinstance(auth_cfg, dict):
            auth_cfg = {}
        dsm_user = str(auth_cfg.get("username") or "").strip()
        dsm_pass = str(auth_cfg.get("password") or "").strip()
        if not dsm_user or not dsm_pass:
            return None

        timeout_sec = int(((nas_cfg.get("webdav") or {}) if isinstance(nas_cfg.get("webdav"), dict) else {}).get("timeout_sec") or 30)

        try:
            dsm_client = SynologyDsmClient(dsm_base_url, verify_tls=dsm_verify_tls, timeout_sec=timeout_sec)
            dsm_client.login(username=dsm_user, password=dsm_pass)
            return dsm_client
        except Exception:
            dsm_client = None
            return None

    def _try_dsm_share_link_from_local_file(
        *,
        run_sum: Dict[str, Any],
        roots: List[Path],
        filename_pat: Pattern,
        what: str,
    ) -> Tuple[str, str]:
        if not run_sum or not roots:
            return "", ""
        remote_dir = str(run_sum.get("remote_dir") or "").strip().rstrip("/")
        if not remote_dir:
            return "", ""

        # Find a local file matching the pattern.
        found_path: Optional[Path] = None
        found_root: Optional[Path] = None
        for root in roots:
            try:
                p = _pick_one(_find_files_by_regex(root, filename_pat), what=f"{what} in {root}")
                found_path = p
                found_root = root
                break
            except Exception:
                continue
        if not found_path or not found_root:
            return "", ""

        client = _get_dsm_client()
        if client is None:
            return "", ""

        rel = ""
        try:
            rel = found_path.relative_to(found_root).as_posix()
        except Exception:
            rel = found_path.name

        candidates = []
        if rel:
            candidates.append(f"{remote_dir}/{rel}")
        if found_path.name:
            candidates.append(f"{remote_dir}/{found_path.name}")

        for rp in candidates:
            try:
                link = client.create_share_link(path=rp)
                if link:
                    return found_path.name, link
            except Exception:
                continue
        return "", ""

    key = device_name or str(release_cfg.get("project") or "").strip()
    key_esc = re.escape(key) if key else ""
    if key_esc:
        dev_pat = re.compile(rf"^archive_{key_esc}[A-Za-z0-9_-]*_.*\.tgz$", re.IGNORECASE)
        ota_cloud_pat = re.compile(rf"^archive_OTA_CLOUD_.*{key_esc}.*\.tgz$|^sensorhub_ota_(?!sleep).+\.zip$", re.IGNORECASE)
        ota_sleep_pat = re.compile(rf"^archive_OTA_SLEEP_.*{key_esc}.*\.tgz$|^sensorhub_ota_sleep_.*\.zip$", re.IGNORECASE)
        factory_zip_pat = re.compile(rf".*{key_esc}.*factory.*\.zip$|^factory.*{key_esc}.*\.zip$", re.IGNORECASE)
        ota_sign_zip_pat = re.compile(r"^watch@.*_ota_sign\.zip$|.*_ota_sign\.zip$", re.IGNORECASE)
        ota_zip_pat = re.compile(rf".*{key_esc}.*ota.*sign.*\.zip$|.*{key_esc}.*ota.*\.zip$|ota.*sign.*\.zip$|ota.*\.zip$", re.IGNORECASE)
        manifest_share_pat = re.compile(rf"^manifest_.*{key_esc}.*\.xml$|^manifest_{key_esc}.*\.xml$", re.IGNORECASE)
        md5_share_pat = re.compile(rf".*{key_esc}.*md5.*\.txt$|md5\.txt$|_md5\.txt$|md5sum\.txt$", re.IGNORECASE)
        boot_manifest_share_pat = re.compile(r"^manifest_BOOT_.*\.xml$", re.IGNORECASE)
        recovery_manifest_share_pat = re.compile(r"^manifest_RECOVERY_.*\.xml$", re.IGNORECASE)
    else:
        dev_pat = re.compile(r"^archive_.*\.tgz$", re.IGNORECASE)
        ota_cloud_pat = re.compile(r"^archive_OTA_CLOUD_.*\.tgz$|^sensorhub_ota_(?!sleep).+\.zip$", re.IGNORECASE)
        ota_sleep_pat = re.compile(r"^archive_OTA_SLEEP_.*\.tgz$|^sensorhub_ota_sleep_.*\.zip$", re.IGNORECASE)
        factory_zip_pat = re.compile(r"factory.*\.zip$", re.IGNORECASE)
        ota_sign_zip_pat = re.compile(r"^watch@.*_ota_sign\.zip$|.*_ota_sign\.zip$", re.IGNORECASE)
        ota_zip_pat = re.compile(r"ota.*sign.*\.zip$|ota.*\.zip$", re.IGNORECASE)
        manifest_share_pat = re.compile(r"manifest_.*\.xml$", re.IGNORECASE)
        md5_share_pat = re.compile(r"md5\.txt$|_md5\.txt$|md5sum\.txt$", re.IGNORECASE)
        boot_manifest_share_pat = re.compile(r"^manifest_BOOT_.*\.xml$", re.IGNORECASE)
        recovery_manifest_share_pat = re.compile(r"^manifest_RECOVERY_.*\.xml$", re.IGNORECASE)

    def _set_share(ph: str, run_sum: Dict[str, Any], pat: Pattern, what: str) -> None:
        if not run_sum:
            return
        try:
            fname, url = _find_share_link_by_regex(run_sum, filename_regex=pat, what=what)
            if url:
                if fname:
                    repl[ph] = f"{fname}|{url}"
                    repl[ph + "_NAME"] = fname
                else:
                    repl[ph] = url
        except Exception:
            return

    _set_share("{{REL_RELEASE_FULL_ARCHIVE}}", rel_sum, dev_pat, "release full archive tgz")
    _set_share("{{REL_RELEASE_OTA_CLOUD_ARCHIVE}}", rel_sum, ota_cloud_pat, "archive_OTA_CLOUD")
    _set_share("{{REL_RELEASE_OTA_SLEEP_ARCHIVE}}", rel_sum, ota_sleep_pat, "archive_OTA_SLEEP")
    _set_share("{{REL_RELEASE_FACTORY_TOOL_ZIP}}", rel_sum, factory_zip_pat, "factory tool zip")
    # Prefer the explicit ota_sign archive (often from prepared extra dirs)
    _set_share("{{REL_RELEASE_OTA_SIGN_ZIP}}", rel_sum, ota_sign_zip_pat, "watch ota_sign zip")
    if not repl.get("{{REL_RELEASE_OTA_SIGN_ZIP}}"):
        _set_share("{{REL_RELEASE_OTA_SIGN_ZIP}}", rel_sum, ota_zip_pat, "ota zip")

    # Monkey/Debug OTA full package: use Debug build's archive_<project>*_*.tgz share link.
    _set_share("{{REL_MONKEY_FULL_ARCHIVE}}", dbg_sum, dev_pat, "debug full archive tgz")

    # Prefer share links for manifest/md5; fall back to single-line file content.
    _set_share("{{REL_BOOTLOADER_MANIFEST_INFO}}", rel_sum, boot_manifest_share_pat, "bootloader manifest xml")
    _set_share("{{REL_RECOVERY_MANIFEST_INFO}}", rel_sum, recovery_manifest_share_pat, "recovery manifest xml")
    _set_share("{{REL_APP_MANIFEST_INFO}}", rel_sum, manifest_share_pat, "manifest xml")
    _set_share("{{REL_MD5SUM_FILE}}", rel_sum, md5_share_pat, "md5 file")

    # ── TSCAN share links ──
    tscan_links = tscan_sum.get("share_links_main", {}) if isinstance(tscan_sum.get("share_links_main"), dict) else {}
    for placeholder_key, tscan_filename in [
        ("{{REL_TSCAN_BOOT}}", "BOOT_tscanResult.zip"),
        ("{{REL_TSCAN_OTA}}", "OTA_tscanResult.zip"),
        ("{{REL_TSCAN_RECOVERY}}", "RECOVERY_tscanResult.zip"),
    ]:
        url = tscan_links.get(tscan_filename, "")
        if url:
            repl[placeholder_key] = f"{tscan_filename}|{url}"

    if not repl.get("{{REL_BOOTLOADER_MANIFEST_INFO}}"):
        fname, url = _try_dsm_share_link_from_local_file(
            run_sum=rel_sum,
            roots=roots_release,
            filename_pat=boot_manifest_share_pat,
            what="bootloader manifest xml",
        )
        if url:
            if fname:
                repl["{{REL_BOOTLOADER_MANIFEST_INFO}}"] = f"{fname}|{url}"
                repl["{{REL_BOOTLOADER_MANIFEST_INFO}}_NAME"] = fname
            else:
                repl["{{REL_BOOTLOADER_MANIFEST_INFO}}"] = url
            
    if not repl.get("{{REL_RECOVERY_MANIFEST_INFO}}"):
        fname, url = _try_dsm_share_link_from_local_file(
            run_sum=rel_sum,
            roots=roots_release,
            filename_pat=recovery_manifest_share_pat,
            what="recovery manifest xml",
        )
        if url:
            if fname:
                repl["{{REL_RECOVERY_MANIFEST_INFO}}"] = f"{fname}|{url}"
                repl["{{REL_RECOVERY_MANIFEST_INFO}}_NAME"] = fname
            else:
                repl["{{REL_RECOVERY_MANIFEST_INFO}}"] = url

    # If share links are still missing, try creating DSM share links directly from local files.
    if not repl.get("{{REL_RELEASE_OTA_CLOUD_ARCHIVE}}"):
        fname, url = _try_dsm_share_link_from_local_file(
            run_sum=rel_sum,
            roots=roots_release,
            filename_pat=ota_cloud_pat,
            what="archive_OTA_CLOUD",
        )
        if url:
            if fname:
                repl["{{REL_RELEASE_OTA_CLOUD_ARCHIVE}}"] = f"{fname}|{url}"
                repl["{{REL_RELEASE_OTA_CLOUD_ARCHIVE}}_NAME"] = fname
            else:
                repl["{{REL_RELEASE_OTA_CLOUD_ARCHIVE}}"] = url
            
    if not repl.get("{{REL_RELEASE_FULL_ARCHIVE}}"):
        fname, url = _try_dsm_share_link_from_local_file(
            run_sum=rel_sum,
            roots=roots_release,
            filename_pat=dev_pat,
            what="release full archive tgz",
        )
        if url:
            if fname:
                repl["{{REL_RELEASE_FULL_ARCHIVE}}"] = f"{fname}|{url}"
                repl["{{REL_RELEASE_FULL_ARCHIVE}}_NAME"] = fname
            else:
                repl["{{REL_RELEASE_FULL_ARCHIVE}}"] = url
            
    if not repl.get("{{REL_RELEASE_OTA_SLEEP_ARCHIVE}}"):
        fname, url = _try_dsm_share_link_from_local_file(
            run_sum=rel_sum,
            roots=roots_release,
            filename_pat=ota_sleep_pat,
            what="archive_OTA_SLEEP",
        )
        if url:
            if fname:
                repl["{{REL_RELEASE_OTA_SLEEP_ARCHIVE}}"] = f"{fname}|{url}"
                repl["{{REL_RELEASE_OTA_SLEEP_ARCHIVE}}_NAME"] = fname
            else:
                repl["{{REL_RELEASE_OTA_SLEEP_ARCHIVE}}"] = url
            
    if not repl.get("{{REL_RELEASE_FACTORY_TOOL_ZIP}}"):
        fname, url = _try_dsm_share_link_from_local_file(
            run_sum=rel_sum,
            roots=roots_release,
            filename_pat=factory_zip_pat,
            what="factory tool zip",
        )
        if url:
            if fname:
                repl["{{REL_RELEASE_FACTORY_TOOL_ZIP}}"] = f"{fname}|{url}"
                repl["{{REL_RELEASE_FACTORY_TOOL_ZIP}}_NAME"] = fname
            else:
                repl["{{REL_RELEASE_FACTORY_TOOL_ZIP}}"] = url
            
    if not repl.get("{{REL_RELEASE_OTA_SIGN_ZIP}}"):
        fname, url = _try_dsm_share_link_from_local_file(
            run_sum=rel_sum,
            roots=roots_release,
            filename_pat=ota_sign_zip_pat,
            what="watch ota_sign zip",
        )
        if url:
            if fname:
                repl["{{REL_RELEASE_OTA_SIGN_ZIP}}"] = f"{fname}|{url}"
                repl["{{REL_RELEASE_OTA_SIGN_ZIP}}_NAME"] = fname
            else:
                repl["{{REL_RELEASE_OTA_SIGN_ZIP}}"] = url
        else:
            fname, url = _try_dsm_share_link_from_local_file(
                run_sum=rel_sum,
                roots=roots_release,
                filename_pat=ota_zip_pat,
                what="ota zip",
            )
            if url:
                if fname:
                    repl["{{REL_RELEASE_OTA_SIGN_ZIP}}"] = f"{fname}|{url}"
                    repl["{{REL_RELEASE_OTA_SIGN_ZIP}}_NAME"] = fname
                else:
                    repl["{{REL_RELEASE_OTA_SIGN_ZIP}}"] = url

    if not repl.get("{{REL_APP_MANIFEST_INFO}}"):
        fname, url = _try_dsm_share_link_from_local_file(
            run_sum=rel_sum,
            roots=roots_release,
            filename_pat=manifest_share_pat,
            what="manifest xml",
        )
        if url:
            if fname:
                repl["{{REL_APP_MANIFEST_INFO}}"] = f"{fname}|{url}"
                repl["{{REL_APP_MANIFEST_INFO}}_NAME"] = fname
            else:
                repl["{{REL_APP_MANIFEST_INFO}}"] = url
            
    if not repl.get("{{REL_MD5SUM_FILE}}"):
        fname, url = _try_dsm_share_link_from_local_file(
            run_sum=rel_sum,
            roots=roots_release,
            filename_pat=md5_share_pat,
            what="md5 file",
        )
        if url:
            if fname:
                repl["{{REL_MD5SUM_FILE}}"] = f"{fname}|{url}"
                repl["{{REL_MD5SUM_FILE}}_NAME"] = fname
            else:
                repl["{{REL_MD5SUM_FILE}}"] = url

    # Single-line file content (fallback)

    def _set_file_single_line(ph: str, roots: List[Path], pat: Pattern) -> None:
        for root in roots:
            try:
                p = _pick_one(_find_files_by_regex(root, pat), what=f"{ph} in {root}")
                repl[ph] = _collapse_to_single_line(_read_text_best_effort(p))
                return
            except Exception:
                continue

    # Fallback to local file content only when a share link wasn't found/generated.
    if not repl.get("{{REL_APP_MANIFEST_INFO}}"):
        _set_file_single_line("{{REL_APP_MANIFEST_INFO}}", roots_release, manifest_share_pat)
    if not repl.get("{{REL_MD5SUM_FILE}}"):
        _set_file_single_line("{{REL_MD5SUM_FILE}}", roots_release, md5_share_pat)

    # Cleanup DSM client if we created one.
    if dsm_client is not None:
        try:
            dsm_client.logout()
        except Exception:
            pass

    # User overrides
    feishu_cfg = cfg.get("feishu") or {}
    if isinstance(feishu_cfg, dict):
        overrides = feishu_cfg.get("placeholder_overrides")
        if isinstance(overrides, dict):
            for k, v in overrides.items():
                ks = str(k)
                if not ks:
                    continue
                if v is None:
                    continue
                vs = str(v)
                if not vs.strip():
                    continue
                repl[ks] = vs

    return {k: ("" if v is None else str(v)) for k, v in repl.items() if str(k).strip()}


class SynologyDsmClient:
    def __init__(self, base_url: str, *, verify_tls: bool, timeout_sec: int):
        self._base_url = base_url.rstrip("/")
        self._timeout_sec = int(timeout_sec)
        self._verify_tls = bool(verify_tls)

        self._cj = http.cookiejar.CookieJar()

        if self._verify_tls:
            ssl_ctx = ssl.create_default_context()
        else:
            ssl_ctx = ssl._create_unverified_context()  # noqa: SLF001

        https_handler = urllib.request.HTTPSHandler(context=ssl_ctx)
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cj),
            https_handler,
        )

        self._sid: Optional[str] = None

    def _request_json(self, url: str) -> Dict[str, Any]:
        req = urllib.request.Request(url, method="GET")
        with self._opener.open(req, timeout=self._timeout_sec) as resp:
            body = resp.read()
        try:
            return json.loads(body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as e:
            raise RuntimeError(f"DSM API returned non-JSON: {e}")

    def login(self, *, username: str, password: str, session: str = "FileStation") -> None:
        # Use auth.cgi for widest compatibility.
        params = {
            "api": "SYNO.API.Auth",
            "version": "6",
            "method": "login",
            "account": username,
            "passwd": password,
            "session": session,
            "format": "sid",
        }
        url = f"{self._base_url}/webapi/auth.cgi?{urllib.parse.urlencode(params)}"
        data = self._request_json(url)
        if not data.get("success"):
            raise RuntimeError(f"DSM login failed: {data}")
        sid = (data.get("data") or {}).get("sid")
        if not sid:
            raise RuntimeError(f"DSM login missing sid: {data}")
        self._sid = str(sid)

    def logout(self, *, session: str = "FileStation") -> None:
        if not self._sid:
            return
        params = {
            "api": "SYNO.API.Auth",
            "version": "6",
            "method": "logout",
            "session": session,
            "_sid": f'"{self._sid}"',
        }
        url = f"{self._base_url}/webapi/auth.cgi?{urllib.parse.urlencode(params)}"
        _ = self._request_json(url)
        self._sid = None

    def create_share_link(self, *, path: str) -> str:
        if not self._sid:
            raise RuntimeError("DSM client not logged in")

        # FileStation Sharing API
        params = {
            "api": "SYNO.FileStation.Sharing",
            "version": "3",
            "method": "create",
            "path": path,
            # DSM 已知坑：login 返回的 sid 可能以数字开头，此时 _sid 若不加双引号，
            # DSM 端会将其误判为数字导致 session 匹配失败（error code 119 "SID not found"）。
            # 用双引号包裹后 urlencode 会变成 %22...%22，DSM 解码得到带引号的 JSON 字符串，可正确识别。
            "_sid": f'"{self._sid}"',
        }
        url = f"{self._base_url}/webapi/entry.cgi?{urllib.parse.urlencode(params)}"
        data = self._request_json(url)
        if not data.get("success"):
            raise RuntimeError(f"Create share link failed for {path}: {data}")
        links = (data.get("data") or {}).get("links") or []
        if not links:
            raise RuntimeError(f"Create share link returned no links for {path}: {data}")
        url_value = links[0].get("url")
        if not url_value:
            raise RuntimeError(f"Create share link missing url for {path}: {data}")
        return str(url_value)


def _curl_bytes(
    *,
    url: str,
    netrc_path: str,
    verify_tls: bool,
    timeout_sec: int,
) -> bytes:
    cmd = [
        _curl_bin(),
        "--globoff",
        "--silent",
        "--show-error",
        "--fail",
        "--location",
        "--compressed",
        "--netrc-file",
        netrc_path,
        "--connect-timeout",
        "10",
        "--max-time",
        str(int(timeout_sec)),
    ]
    if not verify_tls:
        cmd.append("--insecure")
    cmd.append(url)

    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if proc.returncode != 0:
        stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
        raise RuntimeError(f"curl failed rc={proc.returncode} url={url} stderr={stderr.strip()[:800]}")
    return proc.stdout


def _curl_download_file(
    *,
    url: str,
    netrc_path: str,
    verify_tls: bool,
    timeout_sec: int,
    dest: Path,
    show_progress: bool,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        _curl_bin(),
        "--globoff",
        "--show-error",
        "--fail",
        "--location",
        "--compressed",
        "--netrc-file",
        netrc_path,
        "--connect-timeout",
        "10",
        "--max-time",
        str(int(timeout_sec)),
        "--output",
        str(dest),
        url,
    ]
    # Always use --progress-bar; stderr is now streamed via Popen to the pipeline.
    cmd.insert(1, "--progress-bar")
    if not verify_tls:
        cmd.insert(-2, "--insecure")

    # Stream stderr so progress-bar output appears in real-time through the pipeline.
    # \r (carriage return) is curl's way of updating the same line — we strip ANSI
    # and leading/trailing \r, then print each non-empty chunk.
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    for line in iter(proc.stderr.readline, ''):
        # Strip ANSI escape sequences and carriage returns
        clean = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', line)
        clean = clean.strip('\r\n').strip()
        if clean:
            print(clean, flush=True)
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"download failed rc={proc.returncode} url={url} dest={dest}")


def _jenkins_console_text_url(build_url: str) -> str:
    b = str(build_url or "").strip()
    if not b:
        raise RuntimeError("Jenkins build_url is empty")
    return b.rstrip("/") + "/consoleText"


def _jenkins_download_console_text(
    *,
    build_url: str,
    auth: JenkinsAuth,
    dest: Path,
    timeout_sec: int,
    dry_run: bool,
    show_progress: bool,
) -> None:
    url = _jenkins_console_text_url(build_url)
    if dry_run:
        print(f"Jenkins: dry-run; would download console log: {url} -> {dest}")
        return
    # Jenkins is public TLS; keep verify on.
    with CurlNetrc(build_url.rstrip("/") + "/", auth.username, auth.password) as netrc:
        assert netrc.path
        _curl_download_file(
            url=url,
            netrc_path=netrc.path,
            verify_tls=True,
            timeout_sec=timeout_sec,
            dest=dest,
            show_progress=show_progress,
        )


def jenkins_download_artifacts(
    *,
    build_url: str,
    out_dir: Path,
    auth: JenkinsAuth,
    include_globs: List[str],
    exclude_globs: List[str],
    overwrite: bool,
    verify_tls: bool,
    timeout_sec: int,
    dry_run: bool,
    show_progress: bool,
) -> int:
    build_url = build_url.rstrip("/") + "/"
    api_url = build_url + "api/json?tree=number,result,url,displayName,artifacts[fileName,relativePath]"

    if overwrite and out_dir.exists() and out_dir.is_dir() and not dry_run:
        # Windows may transiently lock downloaded artifacts; retry cleanup a few times.
        last_err: Optional[Exception] = None
        for i in range(1, 7):
            try:
                shutil.rmtree(out_dir)
                last_err = None
                break
            except PermissionError as e:
                last_err = e
                time.sleep(0.5 * i)
        if last_err is not None:
            print(
                f"WARN: artifact dir cleanup skipped due to file lock: {out_dir}: {last_err}",
                file=sys.stderr,
            )

    out_dir.mkdir(parents=True, exist_ok=True)

    with CurlNetrc(build_url, auth.username, auth.password) as netrc:
        assert netrc.path
        _dl_t0 = time.time()
        print(f"Jenkins: fetching artifact list: {api_url}")
        raw = _curl_bytes(url=api_url, netrc_path=netrc.path, verify_tls=verify_tls, timeout_sec=timeout_sec)
        _api_elapsed = time.time() - _dl_t0
        print(f"Jenkins: artifact list fetched in {_api_elapsed:.1f}s")
        data = json.loads(raw.decode("utf-8", errors="replace"))

        result = data.get("result") or ""
        if result and result != "SUCCESS":
            print(f"WARN: Jenkins build result is '{result}' (not SUCCESS).", file=sys.stderr)

        artifacts = data.get("artifacts") or []
        if not artifacts:
            raise RuntimeError("No artifacts found in this Jenkins build.")

        # Filter artifacts
        selected: List[Tuple[str, str]] = []  # (rel_path, url)
        for a in artifacts:
            rel = a.get("relativePath") or a.get("fileName")
            if not rel:
                continue
            rel_posix = str(rel)

            if include_globs and not _posix_glob_match(rel_posix, include_globs):
                continue
            if exclude_globs and _posix_glob_match(rel_posix, exclude_globs):
                continue

            rel_q = _encode_path_keep_slash(rel_posix)
            url = f"{build_url}artifact/{rel_q}"
            selected.append((rel_posix, url))

        if not selected:
            raise RuntimeError("After include/exclude filtering, no artifacts remain.")

        print(f"Jenkins: {len(selected)} artifacts selected. Output: {out_dir}")

        # ── 并行下载 ──
        def _download_one(idx_rel_url):
            idx, rel, url = idx_rel_url
            dest = out_dir / rel
            result = {"idx": idx, "rel": rel, "ok": True}
            print(f"  [{idx}/{len(selected)}] {rel}")
            if dry_run:
                return result
            try:
                _curl_download_file(
                    url=url, netrc_path=netrc.path, verify_tls=verify_tls,
                    timeout_sec=timeout_sec, dest=dest, show_progress=False,
                )
            except RuntimeError as e:
                basename = Path(rel).name
                zip_url = f"{build_url}artifact/{_encode_path_keep_slash(rel)}/*zip*/{basename}.zip"
                zip_dest = dest.with_name(dest.name + ".zip_tmp")
                try:
                    _curl_download_file(
                        url=zip_url, netrc_path=netrc.path, verify_tls=verify_tls,
                        timeout_sec=timeout_sec, dest=zip_dest, show_progress=False,
                    )
                    with zipfile.ZipFile(str(zip_dest), "r") as zf:
                        zf.extractall(str(dest))
                    if zip_dest.exists():
                        zip_dest.unlink()
                except Exception as e2:
                    if zip_dest.exists():
                        try: zip_dest.unlink()
                        except Exception: pass
                    result["ok"] = False
                    result["error"] = str(e2)
            except Exception as e:
                result["ok"] = False
                result["error"] = str(e)
            return result

        workers = min(len(selected), 8)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_download_one, (i, r, u)): i for i, (r, u) in enumerate(selected, start=1)}
            for f in as_completed(futures):
                res = f.result()
                if not res["ok"]:
                    print(f"  [{res['idx']}/{len(selected)}] {res['rel']} ERROR: {res.get('error','')[:100]}")

    _dl_total = time.time() - _dl_t0
    print(f"Jenkins: download done in {_dl_total:.1f}s ({len(selected)} files)")
    return len(selected)


def _run_uploader(
    *,
    uploader_script: Path,
    nas_base_url: str,
    nas_username: str,
    nas_password: str,
    verify_tls: bool,
    timeout_sec: int,
    remote_base_dir: str,
    folder_name: str,
    local_dir: str,
    skip_existing: bool,
    dry_run: bool,
    show_progress: bool,
    transport_method: str = "webdav",
    smb_share: str = "",
) -> None:
    cfg = {
        "nas_base_url": nas_base_url,
        "username": nas_username,
        "password": nas_password,
        "remote_base_dir": remote_base_dir,
        "folder_name": folder_name,
        "local_dir": local_dir,
        "verify_tls": verify_tls,
        "timeout_sec": timeout_sec,
        "skip_existing": bool(skip_existing),
        "transport_method": transport_method,
        "smb_share": smb_share,
    }

    with tempfile.NamedTemporaryFile(
        "w",
        prefix="nas_upload_",
        suffix=".json",
        delete=False,
        encoding="utf-8",
    ) as f:
        tmp_path = f.name
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.flush()

    try:
        cmd = [sys.executable, str(uploader_script), "--config", tmp_path]
        if dry_run:
            cmd.append("--dry-run")
        if show_progress:
            cmd.append("--progress")
        if skip_existing:
            cmd.append("--skip-existing")
        print(
            "NAS: upload via nas_webdav_upload.py "
            f"(user={nas_username}, pass={_mask(nas_password)}, local={local_dir})"
        )
        _up_t0 = time.time()
        proc = subprocess.run(cmd, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"NAS upload failed rc={proc.returncode}")
        _up_elapsed = time.time() - _up_t0
        print(f"NAS: upload done in {_up_elapsed:.1f}s")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _stage_unique_extra_upload_dir(
    *,
    extra_dir: Path,
    seen_relpaths: Dict[str, int],
) -> Tuple[str, int, int]:
    """Create a temp dir containing only files whose relpath wasn't seen.

    - seen_relpaths maps rel_posix -> size_bytes of the first occurrence.
    - If a duplicate relpath has a different size, raise (real conflict).
    - Uses hardlink when possible to avoid copying large files.

    Returns (staged_dir, kept_count, skipped_count). staged_dir is '' if empty.
    """

    extra_dir = Path(extra_dir).expanduser().resolve()
    if not extra_dir.exists() or not extra_dir.is_dir():
        return "", 0, 0

    tmp_root = Path(tempfile.mkdtemp(prefix="nas_extra_unique_"))
    kept = 0
    skipped = 0

    try:
        for src, rel in _iter_local_files(extra_dir):
            try:
                size = int(src.stat().st_size)
            except Exception:
                # If we can't stat, treat as unique and try uploading.
                size = -1

            if rel in seen_relpaths:
                prev_size = int(seen_relpaths.get(rel, -1))
                # Only treat as conflict when both sides have known sizes.
                if size >= 0 and prev_size >= 0 and prev_size != size:
                    raise RuntimeError(
                        "NAS: duplicate remote path with different content size: "
                        f"{rel} (prev_size={prev_size}, new_size={size}, src={src})"
                    )
                # Upgrade unknown cached size to known size for later checks.
                if prev_size < 0 and size >= 0:
                    seen_relpaths[rel] = size
                skipped += 1
                continue

            seen_relpaths[rel] = size
            dst = tmp_root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(src, dst)
            except Exception:
                shutil.copy2(src, dst)
            kept += 1

        if kept == 0:
            try:
                shutil.rmtree(tmp_root)
            except Exception:
                pass
            return "", 0, skipped

        return str(tmp_root), kept, skipped
    except Exception:
        try:
            shutil.rmtree(tmp_root)
        except Exception:
            pass
        raise


def _derive_dsm_base_url_from_webdav(webdav_base_url: str) -> str:
    # Typical Synology ports: DSM HTTPS=5001, WebDAV HTTPS=5006.
    # If user already points to 5001, keep it.
    u = urllib.parse.urlsplit(webdav_base_url)
    netloc = u.netloc
    if ":" in netloc:
        host, port = netloc.rsplit(":", 1)
        if port == "5006":
            netloc = f"{host}:5001"
    return urllib.parse.urlunsplit((u.scheme, netloc, "", "", ""))


def _generate_and_print_share_links(
    *,
    dsm_base_url: str,
    dsm_verify_tls: bool,
    timeout_sec: int,
    username: str,
    password: str,
    remote_dir: str,
    local_dir: Path,
) -> Dict[str, str]:
    # remote_dir must be absolute DSM path, without trailing file name
    # local_dir is the directory we just uploaded.
    if not local_dir.exists():
        print(f"WARN: local_dir not found for share links: {local_dir}")
        return {}

    remote_dir = remote_dir.rstrip("/")
    files = list(_iter_local_files(local_dir))
    if not files:
        print("No local files found for share links.")
        return {}

    links_by_rel: Dict[str, str] = {}

    print(f"Share links: logging into DSM API: {dsm_base_url}")
    client = SynologyDsmClient(dsm_base_url, verify_tls=dsm_verify_tls, timeout_sec=timeout_sec)
    client.login(username=username, password=password)
    try:
        for idx, (_abs, rel) in enumerate(files, start=1):
            remote_path = f"{remote_dir}/{rel}"
            try:
                share_url = client.create_share_link(path=remote_path)
                print(f"  [{idx}/{len(files)}] {rel}\n    {share_url}")
                links_by_rel[rel] = share_url
            except Exception as e:
                print(f"  [{idx}/{len(files)}] {rel}\n    ERROR: {e}")
    finally:
        client.logout()

    return links_by_rel


def _normalize_extra_local_dirs(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(x) for x in value if str(x).strip()]
    return [str(value)]


def main() -> int:
    ap = argparse.ArgumentParser(description="Run release pipeline: Jenkins download -> NAS upload (Debug/Release).")
    ap.add_argument("--config", required=True, help="Path to release_pipeline_config.json")
    ap.add_argument("--only", choices=["all", "debug", "release"], default="all", help="Run only debug/release")
    ap.add_argument("--skip-download", action="store_true", help="Skip Jenkins download")
    ap.add_argument("--skip-prepare", action="store_true", help="Skip prepare (extraction) stage")
    ap.add_argument("--skip-upload", action="store_true", help="Skip NAS upload")
    ap.add_argument("--dry-run", action="store_true", help="Print actions without downloading/uploading")
    ap.add_argument("--no-progress", action="store_true", help="Disable curl progress/speed output")
    ap.add_argument("--skip-share", action="store_true", help="Skip NAS share link generation")
    ap.add_argument("--skip-doc", action="store_true", help="Skip generating local Feishu version doc")
    ap.add_argument("--skip-feishu", action="store_true", help="Skip generating Feishu cloud doc")
    ap.add_argument(
        "--feishu-preauth",
        action="store_true",
        help="Run Feishu OAuth upfront (before long-running steps) to avoid interactive authorization later.",
    )
    ap.add_argument(
        "--feishu-preauth-only",
        action="store_true",
        help="Only run Feishu OAuth pre-auth and exit (useful before starting background Jenkins/pipeline runs).",
    )
    ap.add_argument(
        "--feishu-preauth-force",
        action="store_true",
        help="Force OAuth even if feishu.user_access_token already exists in config.",
    )
    ap.add_argument(
        "--doc-output",
        default="",
        help="Directory to write the local version doc (default: user home/release_docs)",
    )
    ap.add_argument(
        "--feishu-refresh",
        action="store_true",
        help="Only update placeholders in an existing Feishu Docx/Wiki (no download/upload/copy).",
    )
    ap.add_argument(
        "--feishu-target",
        default="",
        help="Target Docx URL/token or Wiki URL/token to update in --feishu-refresh mode.",
    )
    ap.add_argument(
        "--feishu-keys",
        action="append",
        default=[],
        help="Placeholder keys to update (repeatable or comma-separated). Default: REL_CHANGELOG_LINK",
    )
    ap.add_argument(
        "--feishu-all",
        action="store_true",
        help="Update all placeholders available for the document in --feishu-refresh mode.",
    )
    args = ap.parse_args()

    show_progress = not args.no_progress and not args.dry_run

    cfg_path = Path(args.config).expanduser().resolve()
    cfg_raw = read_json_with_base(cfg_path)

    # Append project name to prepare.workspace_dir early so all downstream
    # placeholder expansions (DSL, extra_local_dirs, etc.) pick it up.
    # This mirrors the download dir pattern: work/download/{project}_debug / {project}_release
    prepare_raw = cfg_raw.get("prepare") or {}
    if isinstance(prepare_raw, dict):
        proj_for_ws = str((cfg_raw.get("release") or {}).get("project") or "").strip()
        if proj_for_ws:
            orig = str(prepare_raw.get("workspace_dir") or "work/prepare")
            prepare_raw["workspace_dir"] = orig.rstrip("/").replace("\\", "/") + "/" + proj_for_ws
            cfg_raw["prepare"] = prepare_raw

    cfg_base_dir = cfg_path.parent

    def _resolve_cfg_path(value: Any) -> Path:
        # Treat strings as filesystem paths. Relative paths are resolved against the config directory
        # (not the current working directory) to keep configs portable across OS/shell.
        p = Path(str(value)).expanduser()
        if not p.is_absolute():
            p = (cfg_base_dir / p)
        return p.resolve()

    # 1) Expand placeholders using the raw config as context.
    cfg = _expand_placeholders(cfg_raw, cfg_raw)

    doc_cfg = (cfg.get("doc") or {}) if isinstance(cfg.get("doc"), dict) else {}
    doc_enabled = bool(doc_cfg.get("enabled", False))
    doc_filename_template = str(doc_cfg.get("filename_template") or "{project}_{version}_{timestamp}_版本文档.md")

    feishu_cfg = (cfg.get("feishu") or {}) if isinstance(cfg.get("feishu"), dict) else {}
    feishu_enabled = bool(feishu_cfg.get("enabled", False))
    feishu_timeout_sec = int(feishu_cfg.get("timeout_sec", 30))
    feishu_parent_block_id = str(feishu_cfg.get("parent_block_id") or "").strip()
    feishu_insert_index = int(feishu_cfg.get("insert_index", -1))
    feishu_debug_dump = bool(feishu_cfg.get("debug_dump_response", False))
    feishu_print_placeholder_mapping = bool(feishu_cfg.get("print_placeholder_mapping", True))

    feishu_docx_replace_placeholders = bool(feishu_cfg.get("docx_replace_placeholders", False))
    feishu_docx_replace_only = bool(feishu_cfg.get("docx_replace_only", False))

    # Final notification summary (sent via notifications.webhook).
    feishu_final_url: str = ""
    feishu_status: str = ""
    local_doc_status: str = ""

    feishu_oauth_cfg = (feishu_cfg.get("oauth") or {}) if isinstance(feishu_cfg.get("oauth"), dict) else {}
    feishu_oauth_enabled = bool(feishu_oauth_cfg.get("enabled", False))
    feishu_oauth_auto_save = bool(feishu_oauth_cfg.get("auto_save_user_access_token", False))
    feishu_oauth_auto_on_expired = True

    if args.feishu_refresh:
        if not feishu_enabled:
            raise SystemExit("feishu.enabled=false; cannot run --feishu-refresh")
        target_raw = str(args.feishu_target or "").strip()
        if not target_raw:
            raise SystemExit("--feishu-target is required for --feishu-refresh")

        access_token = _feishu_ensure_fresh_user_access_token(
            cfg_path=cfg_path,
            cfg_raw=cfg_raw,
            feishu_cfg=feishu_cfg,
            feishu_oauth_cfg=feishu_oauth_cfg,
            timeout_sec=feishu_timeout_sec,
            auto_save=feishu_oauth_auto_save,
            notify_cfg=cfg,
        )

        target_doc_id = ""
        if "/docx/" in target_raw or target_raw.isalnum():
            token0 = _feishu_extract_docx_token(value=target_raw)
            target_doc_id = str(token0 or "").strip()
        else:
            m = re.search(r"/wiki/([A-Za-z0-9]+)", target_raw)
            if m:
                wiki_token = m.group(1)
                node = _feishu_wiki_get_node(user_access_token=access_token, token=wiki_token, obj_type="wiki", timeout_sec=feishu_timeout_sec)
                if str(node.get("obj_type") or "").strip().lower() != "docx":
                    raise SystemExit("target wiki node is not a docx-backed page")
                target_doc_id = str(node.get("obj_token") or "").strip()
        if not target_doc_id:
            raise SystemExit("could not resolve docx document_id from --feishu-target")

        keys: List[str] = []
        for it in (args.feishu_keys or []):
            for p in str(it).replace(",", " ").split():
                if p.strip():
                    k = p.strip()
                    if not k.startswith("{{"):
                        k = "{{" + k + "}}"
                    if not k.endswith("}}"):
                        k = k + "}}"
                    keys.append(k)
        if not keys and not bool(args.feishu_all):
            keys = ["{{REL_CHANGELOG_LINK}}"]

        mapping_all: Dict[str, str] = {}
        if args.feishu_all:
            mapping_all = _build_placeholder_replacements(cfg=cfg, run_summaries=[])
        else:
            if "{{REL_CHANGELOG_LINK}}" in keys:
                link = _build_changelog_link(cfg=cfg)
                mapping_all["{{REL_CHANGELOG_LINK}}"] = link or ""

        mapping_final: Dict[str, str] = {}
        if args.feishu_all:
            mapping_final = {k: v for k, v in mapping_all.items() if str(v).strip()}
        else:
            for k in keys:
                v = mapping_all.get(k)
                if v:
                    mapping_final[k] = v

        if not mapping_final:
            print("Nothing to update (no non-empty values for requested keys).")
            return 0

        _ = _feishu_docx_replace_placeholders_in_document(
            user_access_token=access_token,
            document_id=target_doc_id,
            mapping=mapping_final,
            timeout_sec=feishu_timeout_sec,
            dry_run=args.dry_run,
        )
        print("Feishu refresh completed.")
        return 0

    # Optional: pre-authorize Feishu OAuth now so background runs won't pause for interactive auth.
    if args.feishu_preauth or args.feishu_preauth_only:
        # If Feishu is disabled, preauth is a no-op.
        if not feishu_enabled:
            print("Feishu pre-auth: feishu.enabled=false; nothing to do")
            if args.feishu_preauth_only:
                return 0
        else:
            have_token = bool(_feishu_user_token_from_cfg(feishu_cfg))
            if have_token and (not args.feishu_preauth_force):
                print("Feishu pre-auth: user_access_token already present; skip (use --feishu-preauth-force to re-authorize)")
                if args.feishu_preauth_only:
                    return 0
            else:
                if not feishu_oauth_enabled:
                    if have_token:
                        print("Feishu pre-auth: OAuth disabled but user_access_token is present; proceeding")
                        if args.feishu_preauth_only:
                            return 0
                    raise SystemExit(
                        "Feishu pre-auth requested but feishu.oauth.enabled=false and no user_access_token is present. "
                        "Enable feishu.oauth.* or set feishu.user_access_token in config."
                    )

                print("Feishu pre-auth: starting OAuth now...")
                tok = _feishu_oauth_get_user_tokens_localhost(
                    oauth_cfg=feishu_oauth_cfg,
                    timeout_sec=feishu_timeout_sec,
                    notify_cfg=cfg,
                )
                new_token = str(tok.get("access_token") or "").strip()
                new_refresh = str(tok.get("refresh_token") or "").strip()
                feishu_cfg["user_access_token"] = new_token
                if new_refresh:
                    feishu_cfg["user_refresh_token"] = new_refresh
                if int(tok.get("access_expires_at") or 0) > 0:
                    feishu_cfg["user_access_token_expires_at"] = int(tok.get("access_expires_at") or 0)
                if int(tok.get("refresh_expires_at") or 0) > 0:
                    feishu_cfg["user_refresh_token_expires_at"] = int(tok.get("refresh_expires_at") or 0)

                # In preauth-only mode, default to saving the token so later background runs won't block.
                should_save = bool(feishu_oauth_auto_save) or bool(args.feishu_preauth_only)
                _feishu_maybe_save_user_oauth_tokens(
                    cfg_path=cfg_path,
                    cfg_raw=cfg_raw,
                    access_token=new_token,
                    refresh_token=new_refresh,
                    access_expires_at=int(tok.get("access_expires_at") or 0),
                    refresh_expires_at=int(tok.get("refresh_expires_at") or 0),
                    enabled=should_save,
                )

                if should_save:
                    print("Feishu pre-auth: token saved into config")
                else:
                    print("WARN: Feishu pre-auth: token obtained but NOT saved (set feishu.oauth.auto_save_user_access_token=true)", file=sys.stderr)

                if args.feishu_preauth_only:
                    return 0

    prepare_cfg = cfg.get("prepare", {}) or {}
    prepare_mode = str(prepare_cfg.get("mode", "none")).strip()

    # Optional: JSON-driven prepare/placeholder mapping DSL.
    dsl_cfg: Optional[Dict[str, Any]] = None
    dsl_runtime: Optional[Dict[str, Any]] = None
    if prepare_mode.strip().lower() in ("placeholders_dsl", "dsl"):
        def _auto_pick_dsl_path() -> Path:
            dsl_path_value = (
                prepare_cfg.get("placeholders_config")
                or prepare_cfg.get("flow_config")
                or prepare_cfg.get("placeholders_flow_config")
                or prepare_cfg.get("placeholders_config_path")
            )
            if dsl_path_value:
                p = _resolve_cfg_path(dsl_path_value)
                if not p.exists():
                    raise SystemExit(f"DSL config not found: {p}")
                return p

            # Default placeholders config (shared across projects).
            candidates: List[Path] = [(cfg_base_dir / "placeholders.default.json").resolve()]

            for c in candidates:
                if c.exists():
                    return c

            # Backward-compat: if there's exactly one *.placeholders.json in config dir, use it.
            globbed = sorted(cfg_base_dir.glob("*.placeholders.json"))
            if len(globbed) == 1 and globbed[0].exists():
                print(f"DSL Prepare: auto-picked placeholders config: {globbed[0]}")
                return globbed[0].resolve()

            cand_msg = "\n".join([f"  - {c}" for c in candidates])
            raise SystemExit(
                "prepare.mode=placeholders_dsl requires a placeholders DSL config. "
                "Either set prepare.placeholders_config explicitly, or create the default file:\n" + cand_msg
            )

        dsl_path = _auto_pick_dsl_path()
        dsl_raw = json.loads(dsl_path.read_text(encoding="utf-8"))
        if not isinstance(dsl_raw, dict):
            raise SystemExit(f"Invalid DSL json (expected object): {dsl_path}")

        # Multi-pass expansion:
        # - flow.* may reference prepare.flow.* (self-references) and prepare.* from main config.
        # - We expand flow with a context that includes the current flow, iterating until stable.
        # - Then expand the whole DSL using cfg + expanded flow so ${prepare.flow.work_dir} works.
        flow0 = (((dsl_raw.get("prepare") or {}) if isinstance(dsl_raw.get("prepare"), dict) else {}).get("flow") or {})
        if not isinstance(flow0, dict):
            raise SystemExit(f"Invalid DSL prepare.flow (expected object): {dsl_path}")
        cfg_prepare2 = (cfg.get("prepare") or {}) if isinstance(cfg.get("prepare"), dict) else {}

        flow_cur: Any = flow0
        for _ in range(6):
            ctx_loop = dict(cfg)
            ctx_loop["prepare"] = {**cfg_prepare2, "flow": flow_cur}
            flow_next = _expand_placeholders(flow_cur, ctx_loop)
            if flow_next == flow_cur:
                break
            flow_cur = flow_next
        flow1 = flow_cur

        ctx2 = dict(cfg)
        ctx2["prepare"] = {**cfg_prepare2, "flow": flow1}
        dsl_cfg = _expand_placeholders(dsl_raw, ctx2)
        dsl_runtime = {"dsl_path": str(dsl_path), "vars": {}}

    uploader_script = Path(__file__).with_name("nas_webdav_upload.py")
    if not uploader_script.exists():
        raise SystemExit(f"Missing uploader script: {uploader_script}")

    # Jenkins auth (env var priority)
    from credential_resolver import resolve_credential, CREDENTIAL_ENV_MAP
    j_auth = cfg["jenkins"]["auth"]
    j_user = resolve_credential(
        str(j_auth.get("username", "")),
        CREDENTIAL_ENV_MAP.get("jenkins.auth.username", "JENKINS_USERNAME"),
        required=False, sensitive=False,
    ) or os.environ.get("JENKINS_USERNAME", "")
    j_pass = resolve_credential(
        str(j_auth.get("password", "")),
        CREDENTIAL_ENV_MAP.get("jenkins.auth.password", "JENKINS_PASSWORD"),
        required=False, sensitive=True,
    ) or os.environ.get("JENKINS_PASSWORD", "")
    if not j_user or not j_pass:
        raise SystemExit("Missing Jenkins username/password in config or env JENKINS_USERNAME/JENKINS_PASSWORD")

    jenkins_timeout_sec = int(cfg.get("jenkins", {}).get("timeout_sec", 600))

    # NAS auth (env var priority)
    nas_webdav = cfg["nas"]["webdav"]
    nas_base_url = str(nas_webdav["base_url"])
    nas_verify_tls = bool(nas_webdav.get("verify_tls", False))
    nas_timeout_sec = int(nas_webdav.get("timeout_sec", 60))
    nas_user = resolve_credential(
        str(nas_webdav["auth"].get("username", "")),
        CREDENTIAL_ENV_MAP.get("nas.webdav.auth.username", "NAS_WEBDAV_USERNAME"),
        required=False, sensitive=False,
    ) or os.environ.get("NAS_WEBDAV_USERNAME", "")
    nas_pass = resolve_credential(
        str(nas_webdav["auth"].get("password", "")),
        CREDENTIAL_ENV_MAP.get("nas.webdav.auth.password", "NAS_WEBDAV_PASSWORD"),
        required=False, sensitive=True,
    ) or os.environ.get("NAS_WEBDAV_PASSWORD", "") or os.environ.get("WEBDAV_PASS", "")
    # SMB 直传配置（可选，默认 webdav）
    nas_transport = str(cfg.get("nas", {}).get("transport_method", "webdav")).lower()
    nas_smb_share = str(cfg.get("nas", {}).get("smb_share", ""))
    if not nas_user or not nas_pass:
        raise SystemExit("Missing NAS webdav username/password in config or env NAS_WEBDAV_USERNAME/NAS_WEBDAV_PASSWORD")

    remote_base_dir = str(cfg["nas"]["remote"]["base_dir"])
    remote_folder_name = str(cfg["nas"]["remote"]["folder_name"])
    remote_variant_base = remote_base_dir.rstrip("/") + "/" + remote_folder_name.strip("/")

    # DSM API (for share link generation)
    dsm_cfg = (cfg.get("nas") or {}).get("dsm") or {}
    dsm_base_url = str(dsm_cfg.get("base_url") or _derive_dsm_base_url_from_webdav(nas_base_url))
    dsm_verify_tls = bool(dsm_cfg.get("verify_tls", nas_verify_tls))
    dsm_auth = dsm_cfg.get("auth") or {}
    dsm_user = resolve_credential(
        str(dsm_auth.get("username") or nas_user),
        CREDENTIAL_ENV_MAP.get("nas.dsm.auth.username", "NAS_DSM_USERNAME"),
        required=False, sensitive=False,
    ) or nas_user
    dsm_pass = resolve_credential(
        str(dsm_auth.get("password") or "").strip(),
        CREDENTIAL_ENV_MAP.get("nas.dsm.auth.password", "NAS_DSM_PASSWORD"),
        required=False, sensitive=True,
    )
    if not dsm_pass:
        dsm_pass = (os.environ.get("DSM_PASSWORD") or os.environ.get("SYNO_PASS") or "").strip()

    builds = cfg["jenkins"]["builds"]
    run_summaries: List[Dict[str, Any]] = []

    def _finalize(exit_code: int) -> int:
        # Build a concise final message.
        release_cfg_final = cfg.get("release") or {}
        proj = str(release_cfg_final.get("project") or "").strip()
        ver = str(release_cfg_final.get("version") or "").strip()
        stage = str(release_cfg_final.get("stage") or "").strip()

        parts: List[str] = []
        # 读取触发者信息用于用户归属
        operator = str(release_cfg_final.get("operator") or "").strip()
        header = "Release pipeline complete"
        if proj or ver:
            header += f": {proj} {ver}".rstrip()
        if stage:
            header += f" ({stage})"
        if operator:
            header = f"用户 {operator} 正在编译 — {header}"
        parts.append(header)

        if local_doc_status:
            parts.append(f"Local doc: {local_doc_status}")
        if feishu_status:
            parts.append(f"Feishu doc: {feishu_status}")
        elif feishu_final_url:
            parts.append(f"Feishu doc: {feishu_final_url}")
        else:
            # If Feishu was disabled or skipped, make that explicit.
            if not feishu_enabled:
                parts.append("Feishu doc: disabled")
            elif args.skip_feishu:
                parts.append("Feishu doc: skipped (--skip-feishu)")
            elif args.dry_run:
                parts.append("Feishu doc: skipped (dry-run)")

        _maybe_notify_webhook_text(cfg=cfg, text="\n".join(parts), request_timeout_sec=10)

        # ── 飞书 Bot 通知（已于 2026-06-30 BotBuilder 下线后接管为主动通知） ──
        try:
            ver_tag = f"v{ver}" if ver else ""
            release_url = str(builds.get("release", {}).get("build_url", "") if isinstance(builds, dict) else "").strip()
            debug_url = str(builds.get("debug", {}).get("build_url", "") if isinstance(builds, dict) else "").strip()
            release_num = str(builds.get("release", {}).get("build_number", "") if isinstance(builds, dict) else "") if isinstance(builds, dict) else ""
            debug_num = str(builds.get("debug", {}).get("build_number", "") if isinstance(builds, dict) else "") if isinstance(builds, dict) else ""

            proj_info = f"{proj} {ver_tag}" if ver_tag else (proj or "")
            stage_info = f" ({stage})" if stage else ""
            lines = [f"✅ 打包完成：{proj_info}{stage_info}"]
            if feishu_final_url:
                lines.append(f"📄 飞书文档：{feishu_final_url}")
            if release_url:
                lines.append(f"🔧 Release #{release_num}: {release_url}")
            if debug_url:
                lines.append(f"🐛 Debug #{debug_num}: {debug_url}")
            if operator:
                lines.append(f"👤 操作者: {operator}")
            direct_text = "\n".join(lines)
            cc_text = f"📋 抄送：{proj_info}{stage_info}\n操作者: {operator or '未知'}\n{feishu_final_url or ''}"

            # 1) 发给触发者本人
            chat_id = ""
            operator_open_id = str(release_cfg_final.get("operator_open_id") or "").strip()
            if operator_open_id:
                chat_id = operator_open_id
            elif operator and operator.startswith("feishu_bot_"):
                chat_id = operator[len("feishu_bot_"):]
            if not chat_id:
                chat_id = str(feishu_cfg.get("notification_chat_id") or "").strip()
            if not chat_id:
                chat_id = str(feishu_cfg.get("notification_open_id") or "").strip()
            if chat_id:
                sent = _feishu_notify_user(chat_id=chat_id, markdown=direct_text, feishu_cfg=feishu_cfg)
                # 回退：个人 open_id（ou_）发送失败时，改发群聊 chat_id（oc_）
                if not sent:
                    grp_chat = str(feishu_cfg.get("notification_chat_id") or "").strip()
                    if grp_chat and grp_chat != chat_id and grp_chat.startswith("oc_"):
                        print(f"[Notify] Bot 个人通知失败，回退群聊 {grp_chat} ...", file=sys.stderr)
                        _feishu_notify_user(chat_id=grp_chat, markdown=direct_text, feishu_cfg=feishu_cfg)
            else:
                print("[Notify] Bot skipped: no chat_id resolved "
                      f"(operator_open_id={operator_open_id!r}, operator={operator!r}, "
                      f"notification_chat_id={feishu_cfg.get('notification_chat_id')!r})", file=sys.stderr)

            # 2) 抄送给管理员（去重）
            cc_ids = feishu_cfg.get("notification_cc_open_ids") or []
            if isinstance(cc_ids, list):
                for cc_id in cc_ids:
                    cc_id_str = str(cc_id).strip()
                    if cc_id_str and cc_id_str != chat_id:
                        _feishu_notify_user(chat_id=cc_id_str, markdown=cc_text, feishu_cfg=feishu_cfg)
        except Exception as e:
            # Bot notification is best-effort; log failure for visibility
            print(f"[Notify] Final bot notification EXCEPTION: {type(e).__name__}: {e}", file=sys.stderr)

        _notify_base_stage(cfg, "发布完成", "成功" if int(exit_code) == 0 else "失败")

        print("\nPipeline complete.")
        return int(exit_code)

    # Map upload specs by name for convenience
    uploads_by_name: Dict[str, Dict[str, Any]] = {}
    for u in (cfg.get("nas", {}).get("uploads") or []):
        if isinstance(u, dict) and u.get("name"):
            uploads_by_name[str(u["name"])] = u

    # 多维表格流水：release/debug 会各跑一次 run_one，但表里没有「构建名」列，
    # 只在最后一个 build 完成时记一行，避免重复行。
    _last_build = "release" if args.only in ("all", "release") else "debug"

    def run_one(name: str) -> None:
        _run_t0 = time.time()
        b = builds[name]
        build_url = str(b["build_url"])
        dl = b["download"]
        out_dir = _resolve_cfg_path(dl["output_dir"])
        include_globs = [str(x) for x in (dl.get("include_globs") or [])]
        exclude_globs = [str(x) for x in (dl.get("exclude_globs") or [])]
        overwrite = bool(dl.get("overwrite", False))

        print(f"\n=== {name.upper()} ===")
        print(f"Download -> {out_dir}")

        if not args.skip_download:
            jenkins_download_artifacts(
                build_url=build_url,
                out_dir=out_dir,
                auth=JenkinsAuth(username=j_user, password=j_pass),
                include_globs=include_globs,
                exclude_globs=exclude_globs,
                overwrite=overwrite,
                verify_tls=True,  # Jenkins is public TLS; keep verify on
                timeout_sec=jenkins_timeout_sec,
                dry_run=args.dry_run,
                show_progress=show_progress,
            )
        else:
            print("Skip Jenkins download.")

        if name == _last_build:
            _notify_base_stage(cfg, "下载", "跳过" if args.skip_download else "成功")

        # DSL prepare (per-run): execute only steps matching this run name.
        if not args.skip_prepare and prepare_mode.strip().lower() in ("placeholders_dsl", "dsl"):
            if dsl_cfg is None or dsl_runtime is None:
                raise RuntimeError("prepare.mode=placeholders_dsl but DSL config failed to load")
            print(f"Prepare: placeholders_dsl ({name}) from: {dsl_runtime.get('dsl_path')}")
            res = _dsl_execute_prepare_flow(
                cfg=cfg,
                dsl_cfg=dsl_cfg,
                dry_run=args.dry_run,
                current_run=name,
                existing_vars=(dsl_runtime.get("vars") if isinstance(dsl_runtime.get("vars"), dict) else {}),
            )
            dsl_runtime.update(res)

            # Ensure payload dir is uploaded/shared for both runs.
            try:
                payload_dir = Path(str(res["upload_payload_dir"]))
                if str(payload_dir) not in [str(x) for x in extra_local_dirs]:
                    extra_local_dirs.append(str(payload_dir))
            except Exception:
                pass

        upload_spec = uploads_by_name.get(name)
        if not upload_spec:
            raise RuntimeError(f"Missing nas.uploads entry for '{name}'")

        remote_subdir = str(upload_spec["remote_subdir"])
        local_dir_path = _resolve_cfg_path(upload_spec["local_dir"])
        local_dir = str(local_dir_path)
        extra_local_dirs = [str(_resolve_cfg_path(x)) for x in _normalize_extra_local_dirs(upload_spec.get("extra_local_dirs"))]

        # Prepare stage (legacy release_extract)
        prepared_dir: Optional[Path] = None
        if not args.skip_prepare and name == "release" and prepare_mode == "release_extract":
            workspace_dir = _resolve_cfg_path(prepare_cfg.get("workspace_dir") or (Path(__file__).with_name("work") / "prepare"))
            clean = bool(prepare_cfg.get("clean", True))
            input_dir = _resolve_cfg_path(b["download"]["output_dir"])
            print(f"Prepare: release_extract from {input_dir} -> {workspace_dir}")
            project_name_for_prepare = str((cfg.get("release") or {}).get("project") or "")
            prepared_dir = _prepare_release_extract(
                input_dir=input_dir,
                workspace_dir=workspace_dir,
                clean=clean,
                dry_run=args.dry_run,
                project=project_name_for_prepare,
            )
            print(f"Prepare: staged files in: {prepared_dir}")
            # If user didn't specify extra_local_dirs explicitly, auto-add staged dir.
            if not extra_local_dirs:
                extra_local_dirs = [str(prepared_dir)]

        if prepare_mode.strip().lower() in ("placeholders_dsl", "dsl") and dsl_runtime is not None:
            try:
                prepared_dir = Path(str(dsl_runtime.get("upload_payload_dir") or "")).expanduser().resolve()
                if str(prepared_dir).strip():
                    print(f"Prepare: staged files in: {prepared_dir}")
            except Exception:
                prepared_dir = None

        if args.skip_upload:
            print("Skip NAS upload.")
            remote_dir = (remote_variant_base.rstrip("/") + "/" + remote_subdir.strip("/")).rstrip("/")

            # Even if upload is skipped, we can still try generating share links
            # (useful when remote already has the same files).
            share_links_main: Dict[str, str] = {}
            share_links_extra: Dict[str, Dict[str, str]] = {}
            if not (args.dry_run or args.skip_share):
                if not dsm_user or not dsm_pass:
                    print(
                        "WARN: DSM credentials not provided; skip share link generation. "
                        "Set nas.dsm.auth.username/password in config or env DSM_PASSWORD.",
                        file=sys.stderr,
                    )
                else:
                    try:
                        share_links_main = _generate_and_print_share_links(
                            dsm_base_url=dsm_base_url,
                            dsm_verify_tls=dsm_verify_tls,
                            timeout_sec=nas_timeout_sec,
                            username=dsm_user,
                            password=dsm_pass,
                            remote_dir=remote_dir,
                            local_dir=Path(local_dir).expanduser().resolve(),
                        )
                        for extra in extra_local_dirs:
                            extra_path = Path(str(extra)).expanduser().resolve()
                            share_links_extra[str(extra_path)] = _generate_and_print_share_links(
                                dsm_base_url=dsm_base_url,
                                dsm_verify_tls=dsm_verify_tls,
                                timeout_sec=nas_timeout_sec,
                                username=dsm_user,
                                password=dsm_pass,
                                remote_dir=remote_dir,
                                local_dir=extra_path,
                            )
                    except Exception as e:
                        print(f"WARN: share link generation (skip-upload mode) failed: {e}", file=sys.stderr)

            if name == _last_build:
                _notify_base_stage(cfg, "上传", "跳过")
                _notify_base_stage(cfg, "分享", "跳过" if (args.dry_run or args.skip_share) else "成功")

            run_summaries.append(
                {
                    "name": name,
                    "build_url": build_url,
                    "out_dir": str(out_dir),
                    "local_dir": local_dir,
                    "extra_dirs": extra_local_dirs,
                    "remote_dir": remote_dir,
                    "share_links_main": share_links_main,
                    "share_links_extra": share_links_extra,
                }
            )
            return

        # Use uploader's (remote_base_dir + folder_name) mechanism:
        # remote_base_dir := ${base_dir}/${folder_name}
        # folder_name := Monkey / Release
        try:
            _run_uploader(
                uploader_script=uploader_script,
                nas_base_url=nas_base_url,
                nas_username=nas_user,
                nas_password=nas_pass,
                verify_tls=nas_verify_tls,
                timeout_sec=nas_timeout_sec,
                remote_base_dir=remote_variant_base,
                folder_name=remote_subdir,
                local_dir=local_dir,
                skip_existing=False,
                dry_run=args.dry_run,
                show_progress=show_progress,
                transport_method=nas_transport, smb_share=nas_smb_share,
            )
        except RuntimeError as e:
            # Some WebDAV servers reject overwrite with 403 for existing files.
            # Retry once in skip-existing mode to make reruns resilient.
            if args.dry_run:
                raise
            print(f"WARN: primary NAS upload failed ({e}); retry with skip-existing", file=sys.stderr)
            _run_uploader(
                uploader_script=uploader_script,
                nas_base_url=nas_base_url,
                nas_username=nas_user,
                nas_password=nas_pass,
                verify_tls=nas_verify_tls,
                timeout_sec=nas_timeout_sec,
                remote_base_dir=remote_variant_base,
                folder_name=remote_subdir,
                local_dir=local_dir,
                skip_existing=True,
                dry_run=args.dry_run,
                show_progress=show_progress,
                transport_method=nas_transport, smb_share=nas_smb_share,
            )

        # Upload extra dirs (e.g., extracted files) into the same remote subdir.
        # Root-cause fix: ensure we NEVER upload the same remote-relative path twice.
        seen_relpaths: Dict[str, int] = {}
        try:
            for src, rel in _iter_local_files(Path(local_dir).expanduser().resolve()):
                try:
                    seen_relpaths.setdefault(rel, int(src.stat().st_size))
                except Exception:
                    seen_relpaths.setdefault(rel, -1)
        except Exception:
            pass

        for extra in extra_local_dirs:
            extra_path_raw = str(extra)
            if not extra_path_raw.strip():
                continue

            extra_path = Path(extra_path_raw).expanduser().resolve()
            print(f"NAS: extra upload from: {extra_path}")

            staged_dir, kept, skipped = _stage_unique_extra_upload_dir(
                extra_dir=extra_path,
                seen_relpaths=seen_relpaths,
            )
            if not staged_dir:
                print(f"NAS: extra upload skipped (all duplicates): {extra_path} (skipped={skipped})")
                continue

            print(f"NAS: extra upload unique files: kept={kept}, skipped_duplicates={skipped}")
            try:
                _run_uploader(
                    uploader_script=uploader_script,
                    nas_base_url=nas_base_url,
                    nas_username=nas_user,
                    nas_password=nas_pass,
                    verify_tls=nas_verify_tls,
                    timeout_sec=nas_timeout_sec,
                    remote_base_dir=remote_variant_base,
                    folder_name=remote_subdir,
                    local_dir=staged_dir,
                    # For extra uploads, keep reruns resilient.
                    skip_existing=True,
                    dry_run=args.dry_run,
                    show_progress=show_progress,
                transport_method=nas_transport, smb_share=nas_smb_share,
                )
            finally:
                try:
                    shutil.rmtree(staged_dir)
                except Exception:
                    pass

        # Track root-level filenames that have already been uploaded into remote_subdir root.
        # Special uploads (below) upload files into remote_subdir root by filename, so if a file
        # already exists at the root of local_dir or any extra_local_dirs, re-uploading is redundant.
        already_uploaded_root_names: set[str] = set()
        for root_dir in [local_dir] + [str(x) for x in extra_local_dirs]:
            try:
                rp = Path(str(root_dir)).expanduser().resolve()
                if not rp.exists() or not rp.is_dir():
                    continue
                for p in rp.iterdir():
                    if p.is_file():
                        already_uploaded_root_names.add(p.name)
            except Exception:
                continue

        # Special-case: for release builds, also upload archive files matching patterns
        # Use patterns so different version timestamps are handled automatically.
        release_cfg_local = cfg.get("release") or {}
        dev_name = str(release_cfg_local.get("device_name") or "").strip()
        proj_name = str(release_cfg_local.get("project") or "").strip()

        # Some devices have suffix variants (e.g. pamir_64m); allow extra chars after device_name.
        key_esc = re.escape(dev_name or proj_name) if (dev_name or proj_name) else ""
        if key_esc:
            special_patterns = [
                re.compile(rf"^archive_{key_esc}[A-Za-z0-9_-]*_.*\.tgz$", re.IGNORECASE),
                re.compile(rf"^archive_OTA_CLOUD_.*{key_esc}.*\.tgz$", re.IGNORECASE),
                re.compile(rf"^archive_OTA_SLEEP_.*{key_esc}.*\.tgz$", re.IGNORECASE),
                re.compile(rf"^archive_FCT_.*{key_esc}.*\.tgz$", re.IGNORECASE),
            ]
        else:
            special_patterns = [
                re.compile(r"^archive_.*\.tgz$", re.IGNORECASE),
                re.compile(r"^archive_OTA_CLOUD_.*\.tgz$", re.IGNORECASE),
                re.compile(r"^archive_OTA_SLEEP_.*\.tgz$", re.IGNORECASE),
                re.compile(r"^archive_FCT_.*\.tgz$", re.IGNORECASE),
            ]
        special_share_links: Dict[str, Dict[str, str]] = {}
        if name == "release":
            # Search in extra/prepared/internal work folders only.
            # NOTE: primary local_dir is already uploaded above; including it here causes duplicate uploads.
            search_paths: List[Path] = [Path(x) for x in extra_local_dirs]
            if prepared_dir:
                search_paths.append(prepared_dir)
            # include workspace internal _work/step1_archive_<project> if prepare used workspace_dir
            try:
                proj_tmp = (cfg.get("release") or {}).get("project") or ""
                proj_safe_tmp = re.sub(r"[^A-Za-z0-9_-]", "_", str(proj_tmp).strip().lower()) if proj_tmp else "cologne"
                work_candidate = (workspace_dir / "_work" / f"step1_archive_{proj_safe_tmp}")
                if work_candidate.exists():
                    search_paths.append(work_candidate)
            except Exception:
                # workspace_dir may not be defined or accessible; ignore
                pass

            found_files: List[Path] = []
            for sp in search_paths:
                try:
                    if not sp.exists():
                        continue
                except Exception:
                    continue
                # Walk files under the search path and match by pattern
                for p in sp.rglob("*"):
                    if not p.is_file():
                        continue
                    for pat in special_patterns:
                        if pat.match(p.name):
                            found_files.append(p)
                            break

            # De-duplicate by filename (these files are uploaded into remote_subdir root).
            unique_by_name: Dict[str, Path] = {}
            for p in found_files:
                try:
                    key = str(p.name)
                except Exception:
                    continue
                if not key:
                    continue
                unique_by_name.setdefault(key, p)
            found_files = [unique_by_name[k] for k in sorted(unique_by_name.keys())]

            # Skip special uploads for files that were already uploaded via main/extra dirs.
            if already_uploaded_root_names:
                skipped = [p for p in found_files if p.name in already_uploaded_root_names]
                if skipped:
                    print(
                        f"NAS: special upload skipped {len(skipped)} already-uploaded file(s): "
                        + ", ".join(sorted({p.name for p in skipped}))
                    )
                found_files = [p for p in found_files if p.name not in already_uploaded_root_names]

            # Upload each found file by copying it into a temp dir and calling uploader
            for fpath in found_files:
                print(f"NAS: special upload file found: {fpath}")
                tmpdir = tempfile.mkdtemp(prefix="archive_upload_")
                try:
                    dst = Path(tmpdir) / fpath.name
                    shutil.copy2(fpath, dst)
                    _run_uploader(
                        uploader_script=uploader_script,
                        nas_base_url=nas_base_url,
                        nas_username=nas_user,
                        nas_password=nas_pass,
                        verify_tls=nas_verify_tls,
                        timeout_sec=nas_timeout_sec,
                        remote_base_dir=remote_variant_base,
                        folder_name=remote_subdir,
                        local_dir=str(tmpdir),
                        # Special uploads are prone to duplicates (already uploaded by main/extra dirs).
                        # If the server rejects overwrite/conflict, skip and continue.
                        skip_existing=True,
                        dry_run=args.dry_run,
                        show_progress=show_progress,
                transport_method=nas_transport, smb_share=nas_smb_share,
                    )

                    # Immediately generate share links for this uploaded file (so it's included in doc)
                    if not (args.dry_run or args.skip_share):
                        try:
                            links = _generate_and_print_share_links(
                                dsm_base_url=dsm_base_url,
                                dsm_verify_tls=dsm_verify_tls,
                                timeout_sec=nas_timeout_sec,
                                username=dsm_user,
                                password=dsm_pass,
                                remote_dir=(remote_variant_base.rstrip("/") + "/" + remote_subdir.strip("/")),
                                local_dir=Path(tmpdir).expanduser().resolve(),
                            )
                            special_share_links[str(fpath.name)] = links
                        except Exception as e:
                            print(f"WARN: special share link generation failed for {fpath}: {e}", file=sys.stderr)
                finally:
                    try:
                        shutil.rmtree(tmpdir)
                    except Exception:
                        pass

        _notify_base_stage_for = name == _last_build
        if _notify_base_stage_for:
            _notify_base_stage(cfg, "上传", "成功")

        remote_dir = (remote_variant_base.rstrip("/") + "/" + remote_subdir.strip("/")).rstrip("/")
        share_links_main: Dict[str, str] = {}
        share_links_extra: Dict[str, Dict[str, str]] = {}

        if not (args.dry_run or args.skip_share):
            # Generate share links for each uploaded file (main + extras)
            if not dsm_user or not dsm_pass:
                print(
                    "WARN: DSM credentials not provided; skip share link generation. "
                    "Set nas.dsm.auth.username/password in config or env DSM_PASSWORD.",
                    file=sys.stderr,
                )
            else:
                try:
                    share_links_main = _generate_and_print_share_links(
                        dsm_base_url=dsm_base_url,
                        dsm_verify_tls=dsm_verify_tls,
                        timeout_sec=nas_timeout_sec,
                        username=dsm_user,
                        password=dsm_pass,
                        remote_dir=remote_dir,
                        local_dir=Path(local_dir).expanduser().resolve(),
                    )

                    for extra in extra_local_dirs:
                        extra_path = Path(str(extra)).expanduser().resolve()
                        share_links_extra[str(extra_path)] = _generate_and_print_share_links(
                            dsm_base_url=dsm_base_url,
                            dsm_verify_tls=dsm_verify_tls,
                            timeout_sec=nas_timeout_sec,
                            username=dsm_user,
                            password=dsm_pass,
                            remote_dir=remote_dir,
                            local_dir=extra_path,
                        )
                except Exception as e:
                    print(
                        "WARN: share link generation failed (upload still OK). "
                        f"Reason: {e}\n"
                        "Hint: DSM(FileStation) API login often needs a DSM account (5001), "
                        "not the WebDAV-only account. Configure nas.dsm.auth.* or env DSM_PASSWORD.",
                        file=sys.stderr,
                    )

        if _notify_base_stage_for:
            _notify_base_stage(cfg, "分享", "跳过" if (args.dry_run or args.skip_share) else "成功")

        _run_elapsed = time.time() - _run_t0
        print(f"run_one({name}): total {_run_elapsed:.1f}s")
        run_summaries.append(
            {
                "name": name,
                "build_url": build_url,
                "out_dir": str(out_dir),
                "local_dir": local_dir,
                "extra_dirs": extra_local_dirs,
                "remote_dir": remote_dir,
                "share_links_main": share_links_main,
                "share_links_extra": (share_links_extra if not special_share_links else {**share_links_extra, "special_archives": special_share_links}),
            }
        )

    if args.only in ("all", "debug"):
        run_one("debug")
    if args.only in ("all", "release"):
        print(f"Switching to release variant...")
        run_one("release")
    # ── TSCAN 产物上传和分享链接（独立于 release/debug builds）──
    if args.only in ("all", "release"):
        # 确定 TSCAN 产物目录
        tscan_local_dir_val = _resolve_cfg_path(Path(
            uploads_by_name["tscan"]["local_dir"] if "tscan" in uploads_by_name
            else f"work/download/{release_cfg.get('project', 'unknown')}_tscan"
        ))
        tscan_up = uploads_by_name.get("tscan")
        # 如果本地无 TSCAN 文件但 Jenkins build_url 存在，则先下载
        if (not tscan_local_dir_val.exists() or not any(tscan_local_dir_val.iterdir())):
            tscan_build_url = str(builds.get("tscan", {}).get("build_url", "")).strip()
            if tscan_build_url and not args.skip_download:
                print(f"\n=== TSCAN DOWNLOAD (before upload) ===")
                print(f"Download from: {tscan_build_url}")
                print(f"Download to:   {tscan_local_dir_val}")
                if not args.dry_run:
                    tscan_local_dir_val.mkdir(parents=True, exist_ok=True)
                    tscan_dl_cfg = builds.get("tscan", {}).get("download", {})
                    tscan_include = [str(x) for x in (tscan_dl_cfg.get("include_globs") or ["**/*"])]
                    tscan_exclude = [str(x) for x in (tscan_dl_cfg.get("exclude_globs") or [])]
                    jenkins_download_artifacts(
                        build_url=tscan_build_url,
                        out_dir=tscan_local_dir_val,
                        auth=JenkinsAuth(username=j_user, password=j_pass),
                        include_globs=tscan_include,
                        exclude_globs=tscan_exclude,
                        overwrite=True,
                        verify_tls=True,
                        timeout_sec=jenkins_timeout_sec,
                        dry_run=args.dry_run,
                        show_progress=show_progress,
                    )
                    print(f"TSCAN download complete: {len(list(tscan_local_dir_val.iterdir()))} files")
        tscan_local_dir = tscan_local_dir_val  # 统一变量名
        if tscan_local_dir.exists() and any(tscan_local_dir.iterdir()):
            tscan_remote_subdir = str(tscan_up["remote_subdir"]) if tscan_up and tscan_up.get("remote_subdir") else "Monkey"
            tscan_overwrite = bool(tscan_up.get("overwrite", False)) if tscan_up else True
            print(f"\n=== TSCAN UPLOAD ===")
            print(f"Upload -> NAS:{tscan_remote_subdir} ({tscan_local_dir})")
            if not args.dry_run:
                _run_uploader(
                    uploader_script=uploader_script,
                    nas_base_url=nas_base_url,
                    nas_username=nas_user,
                    nas_password=nas_pass,
                    verify_tls=nas_verify_tls,
                    timeout_sec=nas_timeout_sec,
                    remote_base_dir=remote_variant_base,
                    folder_name=tscan_remote_subdir,
                    local_dir=str(tscan_local_dir),
                    skip_existing=not tscan_overwrite,
                    dry_run=args.dry_run,
                    show_progress=show_progress,
                transport_method=nas_transport, smb_share=nas_smb_share,
                )
            # 为 TSCAN 产物生成 NAS 分享链接
            tscan_share_links: Dict[str, str] = {}
            if not (args.dry_run or args.skip_share) and dsm_base_url and dsm_user:
                try:
                    links = _generate_and_print_share_links(
                        dsm_base_url=dsm_base_url,
                        dsm_verify_tls=dsm_verify_tls,
                        timeout_sec=nas_timeout_sec,
                        username=dsm_user,
                        password=dsm_pass,
                        remote_dir=f"{remote_variant_base.rstrip('/')}/{tscan_remote_subdir.strip('/')}",
                        local_dir=tscan_local_dir,
                    )
                    tscan_share_links = links or {}
                except Exception as e:
                    print(f"WARN: TSCAN share link generation failed: {e}", file=sys.stderr)
            # 记录到 run_summaries 供占位符替换使用
            run_summaries.append({
                "name": "tscan",
                "local_dir": str(tscan_local_dir),
                "remote_dir": f"{remote_variant_base}/{tscan_remote_subdir}",
                "share_links_main": tscan_share_links,
            })
        else:
            print(f"\n=== TSCAN SKIPPED: no files in {tscan_local_dir} ===")

    # Generate local Feishu-style version doc (Markdown)
    md: Optional[str] = None
    doc_path: Optional[Path] = None
    if doc_enabled and (not args.skip_doc):
        out_dir = Path(args.doc_output).expanduser() if str(args.doc_output).strip() else (Path.home() / "release_docs")
        if not out_dir.is_absolute():
            out_dir = (cfg_base_dir / out_dir)
        out_dir = out_dir.resolve()
        out_dir.mkdir(parents=True, exist_ok=True)

        release_cfg = cfg.get("release") or {}
        ctx = {
            "project": str(release_cfg.get("project") or ""),
            "version": str(release_cfg.get("version") or ""),
            "variant": str(release_cfg.get("variant") or ""),
            "timestamp": _now_timestamp(),
        }
        filename = doc_filename_template.format(**ctx)
        filename = _safe_filename(filename)
        if not filename.lower().endswith(".md"):
            filename += ".md"

        doc_path = out_dir / filename
        md = _render_version_doc_markdown(
            release_cfg=release_cfg,
            pipeline_cfg_path=cfg_path,
            runs=run_summaries,
            remote_variant_base=remote_variant_base,
        )
        doc_path.write_text(md, encoding="utf-8")
        print(f"\nVersion doc generated: {doc_path}")
        local_doc_status = str(doc_path)
        _notify_base_stage(cfg, "版本文档", "成功")
    else:
        if args.skip_doc:
            local_doc_status = "skipped (--skip-doc)"
        elif not doc_enabled:
            local_doc_status = "disabled"
        else:
            local_doc_status = "skipped"
        _notify_base_stage(cfg, "版本文档", "跳过")

    # ═══════════════════════════════════════════════════════════════
    # Generate Feishu cloud doc via Lark CLI adapter
    # ── 替代原有 ~860 行自建飞书 API 代码 ──
    # ═══════════════════════════════════════════════════════════════
    if feishu_enabled and (not args.skip_feishu):
        if not _LARK_CLI_AVAILABLE:
            print("\n" + "=" * 70, file=sys.stderr)
            print("[WARN]️  需要飞书 CLI (Lark CLI)", file=sys.stderr)
            print("=" * 70, file=sys.stderr)
            print("  安装 & 认证:", file=sys.stderr)
            print("    1. npx @larksuite/cli@latest install", file=sys.stderr)
            print("    2. lark-cli auth login", file=sys.stderr)
            print("  或一键初始化:", file=sys.stderr)
            print("    python lark_cli_adapter.py setup", file=sys.stderr)
            print("=" * 70 + "\n", file=sys.stderr)
            feishu_status = "skipped (lark-cli not installed)"
        else:
            # 确保认证
            if not args.dry_run:
                lark_auth_ensure()

            # 生成 Markdown（如果还没有）
            if md is None:
                release_cfg_md = cfg.get("release") or {}
                md = _render_version_doc_markdown(
                    release_cfg=release_cfg_md,
                    pipeline_cfg_path=cfg_path,
                    runs=run_summaries,
                    remote_variant_base=remote_variant_base,
                )

            # 占位符映射（用于模板替换和文档生成）
            mapping: Dict[str, str] = {}
            try:
                if dsl_cfg is not None and dsl_runtime is not None and prepare_mode.strip().lower() in ("placeholders_dsl", "dsl"):
                    mapping = _build_placeholder_replacements_from_dsl(
                        cfg=cfg, run_summaries=run_summaries,
                        dsl_cfg=dsl_cfg, dsl_runtime=dsl_runtime,
                    )
                else:
                    mapping = _build_placeholder_replacements(cfg=cfg, run_summaries=run_summaries)

                if feishu_print_placeholder_mapping and mapping:
                    mp = _write_placeholder_mapping_file(mapping=mapping, doc_name="release_doc")
                    print(f"Feishu placeholder mapping generated: {mp}")
            except Exception as e:
                print(f"WARN: placeholder mapping failed: {e}", file=sys.stderr)

            # 文档标题
            name_tmpl = str(feishu_cfg.get("name_template") or "{project}_{version}_{timestamp}_版本文档").strip()
            release_cfg_fei = cfg.get("release") or {}
            feishu_ctx_title = {
                "project": str(release_cfg_fei.get("project") or ""),
                "version": str(release_cfg_fei.get("version") or ""),
                "variant": str(release_cfg_fei.get("variant") or ""),
                "timestamp": _now_timestamp(),
            }
            import datetime as _dt
            _now = _dt.datetime.now()
            title_repl = {
                "{{REL_DEVICE_NAME}}": str(release_cfg_fei.get("device_name") or release_cfg_fei.get("project") or "").strip(),
                "{{REL_STAGE}}": str(release_cfg_fei.get("stage") or "").strip(),
                "{{REL_VERSION}}": str(release_cfg_fei.get("version") or "").strip(),
                "{{REL_DATE}}": f"{_now.strftime('%y')}/{_now.month}/{_now.day}",
            }
            doc_name_tmpl_formatted = _format_preserving_double_braces(name_tmpl, feishu_ctx_title)
            doc_name = _safe_filename(_replace_placeholders_in_obj(doc_name_tmpl_formatted, title_repl))

            # Wiki 配置
            feishu_use_wiki = bool(feishu_cfg.get("use_wiki", False))
            wiki_config: Optional[Dict[str, str]] = None
            if feishu_use_wiki:
                wiki_config = {
                    "template_node_token": str(feishu_cfg.get("template_node_token") or feishu_cfg.get("template_page_id") or "").strip(),
                    "target_space_id": str(feishu_cfg.get("target_space_id") or "").strip(),
                    "target_parent_token": str(feishu_cfg.get("target_parent_token") or "").strip(),
                }

            # 多维表格配置
            # 注：2026-09-07 起环节流水统一由 feishu_base_notifier.notify_stage() 逐环节追加，
            # 这里不再传 base_config（否则会多出一行没有「环节/状态」的残缺记录）。
            base_config: Optional[Dict[str, Any]] = None

            # 通知群聊
            notification_chat_id = str(feishu_cfg.get("notification_chat_id") or "").strip() or None

            # ── 一键发布 ──
            feishu_oauth_cfg = feishu_cfg.get("oauth") if isinstance(feishu_cfg.get("oauth"), dict) else None
            result = lark_publish_release_document(
                title=doc_name,
                markdown_content=md,
                placeholder_map=mapping if mapping else None,
                wiki_config=wiki_config,
                base_config=base_config,
                notification_chat_id=notification_chat_id,
                notification_title=f"📦 {release_cfg_fei.get('project', '')} {release_cfg_fei.get('version', '')} 发布完成",
                feishu_oauth_cfg=feishu_oauth_cfg,
                dry_run=args.dry_run,
            )

            feishu_final_url = result.doc_url or result.wiki_url or ""
            feishu_status = result.to_status_text()
            if feishu_final_url:
                _notify_base_stage(cfg, "飞书文档", "成功", doc_url=feishu_final_url)
            else:
                _notify_base_stage(cfg, "飞书文档", "失败")

    else:
        if not feishu_enabled:
            feishu_status = "disabled"
        elif args.skip_feishu:
            feishu_status = "skipped (--skip-feishu)"
        elif args.dry_run:
            feishu_status = "skipped (dry-run)"
        _notify_base_stage(cfg, "飞书文档", "跳过")

    return _finalize(0)

if __name__ == "__main__":
    from credential_resolver import load_dotenv
    load_dotenv()
    raise SystemExit(main())

