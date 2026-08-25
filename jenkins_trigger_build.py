#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Trigger Jenkins parameterized builds and wait for completion, then write build URLs back into config JSON.

Default behavior:
- Trigger Release build first
- Immediately trigger Debug build (do not wait for Release completion)
- Monitor both builds until both finish
- Require both results to be SUCCESS
- Update these fields in-place:
    - jenkins.builds.release.build_url
    - jenkins.builds.debug.build_url

Config (minimal):
{
  "jenkins": {
    "base_url": "https://jenkins.example.com",
    "auth": {"type": "basic", "username": "...", "password": "..."},
    "builds": {"release": {"build_url": "..."}, "debug": {"build_url": "..."}},
    "triggers": {
      "job_url": "https://jenkins.example.com/job/.../job/HuamiOS_HS3/",
      "release": {"parameters": {"PRODUCT": "x", "TAG_NAME": "..."}},
      "debug": {"parameters": {"PRODUCT": "x", "TAG_NAME": "..."}}
    }
  }
}

Notes:
- Parameters not present in JSON are not submitted, so Jenkins uses job defaults.
- If Jenkins CSRF is enabled, this script will auto-detect crumb and include it.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

SCRIPT_DIR = str(Path(__file__).resolve().parent)
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)
from config_loader import read_json_with_base


@dataclass(frozen=True)
class JenkinsAuth:
    username: str
    password: str


def _now_tag() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _read_json(path: Path) -> Dict[str, Any]:
    return read_json_with_base(path)


def _atomic_write_json(path: Path, obj: Dict[str, Any], *, backup: bool) -> None:
    if backup:
        bak = path.with_suffix(path.suffix + f".bak.{_now_tag()}")
        shutil.copy2(path, bak)

    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _get_by_path(obj: Dict[str, Any], dotted: str) -> Any:
    cur: Any = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            raise KeyError(dotted)
        cur = cur[part]
    return cur


def _expand_jenkins_params(
    *,
    raw_params: Dict[str, Any],
    full_cfg: Dict[str, Any],
) -> Dict[str, Any]:
    expanded_params: Dict[str, Any] = {}
    for key, value in raw_params.items():
        key_str = str(key)
        if not key_str:
            continue
        val_raw = str(value or "")

        # Robust placeholder expansion: find all ${path} and replace them
        def _replacer(match):
            path = match.group(1)
            try:
                resolved = _get_by_path(full_cfg, path)
                return "" if resolved is None else str(resolved)
            except Exception:
                return match.group(0)  # Keep original if not found

        val_expanded = re.sub(r"\$\{([^}]+)\}", _replacer, val_raw)
        expanded_params[key_str] = val_expanded
    return expanded_params


def _normalize_changelog_manifest_file(params: Dict[str, Any], cfg: Dict[str, Any]) -> None:
    """CMP-JIRA-GIT 差分的 MANIFEST_FILE 去 _64m/_32/_64 后缀。

    milan_64m / pamir_64m 应传 milan.xml / pamir.xml，而不是 milan_64m.xml / pamir_64m.xml。
    优先用 release.xml_name，缺失则用 release.project 去 `_数字[m]` 尾缀（与 web 端 xml_name 规则一致）。
    """
    if "MANIFEST_FILE" not in params:
        return
    rel = cfg.get("release") or {}
    xml_name = str(rel.get("xml_name") or "").strip()
    if not xml_name:
        project = str(rel.get("project") or "").strip()
        xml_name = re.sub(r"_\d+m?$", "", project)
    if xml_name:
        params["MANIFEST_FILE"] = f"{xml_name}.xml"


def _set_by_path(obj: Dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur: Any = obj
    for part in parts[:-1]:
        if not isinstance(cur, dict):
            raise KeyError(dotted)
        if part not in cur or not isinstance(cur[part], dict):
            cur[part] = {}
        cur = cur[part]
    if not isinstance(cur, dict):
        raise KeyError(dotted)
    cur[parts[-1]] = value


def _basic_auth_header(auth: JenkinsAuth) -> str:
    token = (auth.username + ":" + auth.password).encode("utf-8")
    return "Basic " + base64.b64encode(token).decode("ascii")


def _output_dir() -> Path:
    return Path(__file__).with_name("output")


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def _request(
    *,
    method: str,
    url: str,
    headers: Optional[Mapping[str, str]] = None,
    body: Optional[bytes] = None,
    timeout_sec: int = 30,
    opener: Optional[urllib.request.OpenerDirector] = None,
) -> Tuple[int, Mapping[str, str], bytes]:
    req = urllib.request.Request(url, method=method.upper(), data=body)
    for k, v in (headers or {}).items():
        req.add_header(k, v)

    try:
        do = opener.open if opener is not None else urllib.request.urlopen
        with do(req, timeout=timeout_sec) as resp:
            status = int(getattr(resp, "status", 200) or 200)
            data = resp.read() or b""
            return status, dict(resp.headers.items()), data
    except urllib.error.HTTPError as e:
        data = e.read() or b""
        return int(e.code), dict(e.headers.items()), data


def _request_json(
    *,
    method: str,
    url: str,
    headers: Optional[Mapping[str, str]] = None,
    body: Optional[bytes] = None,
    timeout_sec: int = 30,
    opener: Optional[urllib.request.OpenerDirector] = None,
) -> Dict[str, Any]:
    status, _hdrs, data = _request(method=method, url=url, headers=headers, body=body, timeout_sec=timeout_sec, opener=opener)
    if status < 200 or status >= 300:
        text = data.decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {status} calling {url}: {text[:2000]}")
    try:
        return json.loads((data or b"{}").decode("utf-8", errors="replace"))
    except json.JSONDecodeError as e:
        snippet = (data or b"")[:2000].decode("utf-8", errors="replace")
        raise RuntimeError(f"Non-JSON response calling {url}: {e}; body={snippet!r}")


def _http_post_json(
    *,
    url: str,
    payload: Dict[str, Any],
    timeout_sec: int,
    verify_tls: bool,
) -> Tuple[int, str]:
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


def _maybe_notify_webhook_text(
    *,
    cfg: Dict[str, Any],
    text: str,
    request_timeout_sec: int,
) -> None:
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


def _build_project_header(cfg: Dict[str, Any]) -> str:
    """从配置中提取项目/阶段/版本信息，用于通知标题。"""
    rel = cfg.get("release") or {}
    project = str(rel.get("project") or "").strip()
    device_name = str(rel.get("device_name") or project).strip()
    stage = str(rel.get("stage") or "").strip()
    version = str(rel.get("version") or "").strip()
    parts = [p for p in [device_name, stage, f"v{version}" if version else ""] if p]
    return " ".join(parts) if parts else "未知项目"


def _send_bot_notification(*, cfg: Dict[str, Any], text: str) -> None:
    """通过飞书 Bot 发送群消息（使用 tenant_access_token，无需每用户授权）。"""
    import time as _t
    feishu_cfg = cfg.get("feishu") or {}
    if not isinstance(feishu_cfg, dict):
        return
    chat_id = str(feishu_cfg.get("notification_chat_id") or "").strip()
    if not chat_id:
        return
    oauth_cfg = feishu_cfg.get("oauth") or {}
    if not isinstance(oauth_cfg, dict):
        return
    app_id = str(oauth_cfg.get("app_id") or os.environ.get("FEISHU_APP_ID", "")).strip()
    app_secret = str(oauth_cfg.get("app_secret") or os.environ.get("FEISHU_APP_SECRET", "")).strip()
    if not app_id or not app_secret:
        return
    try:
        # 1) 获取 tenant_access_token
        token_req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            data=json.dumps({"app_id": app_id, "app_secret": app_secret}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(token_req, timeout=10) as resp:
            token_data = json.loads(resp.read().decode("utf-8", errors="replace"))
        access_token = token_data.get("tenant_access_token", "")
        if not access_token:
            return
        # 2) 发送消息
        receive_id_type = "open_id" if chat_id.startswith("ou_") else "chat_id"
        content = json.dumps({"text": text}, ensure_ascii=False)
        msg_body = json.dumps({
            "receive_id": chat_id,
            "msg_type": "text",
            "content": content,
        }, ensure_ascii=False).encode("utf-8")
        msg_req = urllib.request.Request(
            f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={receive_id_type}",
            data=msg_body,
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(msg_req, timeout=15) as resp:
            resp_data = json.loads(resp.read().decode("utf-8", errors="replace"))
        if resp_data.get("code") == 0:
            print(f"[Bot] 消息已发送到 {receive_id_type}={chat_id}")
        else:
            print(f"[Bot] 消息发送失败: {resp_data.get('msg', 'unknown')[:200]}", file=sys.stderr)
    except Exception as e:
        print(f"[Bot] 通知异常: {e}", file=sys.stderr)


def _normalize_job_url(job_url: str) -> str:
    u = str(job_url or "").strip()
    if not u:
        return ""

    parsed = urllib.parse.urlsplit(u)
    path = str(parsed.path or "")
    if not path.endswith("/"):
        path = path + "/"

    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment)
    )


def _try_get_crumb(
    *,
    base_url: str,
    auth: JenkinsAuth,
    timeout_sec: int,
    opener: Optional[urllib.request.OpenerDirector],
) -> Optional[Tuple[str, str]]:
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return None
    url = base + "/crumbIssuer/api/json"
    headers = {"Authorization": _basic_auth_header(auth)}
    try:
        resp = _request_json(method="GET", url=url, headers=headers, timeout_sec=timeout_sec, opener=opener)
    except Exception:
        return None

    field = str(resp.get("crumbRequestField") or "").strip()
    crumb = str(resp.get("crumb") or "").strip()
    if not field or not crumb:
        return None
    return field, crumb


def _encode_form_fields(params: Mapping[str, Any]) -> bytes:
    pairs: List[Tuple[str, str]] = []

    def add(k: str, v: Any) -> None:
        kk = str(k)
        if v is None:
            return
        if isinstance(v, bool):
            if not v:
                return
            pairs.append((kk, "true"))
            return
        if isinstance(v, (list, tuple)):
            for it in v:
                if it is None:
                    continue
                s = str(it)
                if s == "":
                    continue
                pairs.append((kk, s))
            return
        s = str(v)
        if s == "":
            # Explicit empty string means override Jenkins default to empty.
            pairs.append((kk, ""))
            return
        pairs.append((kk, s))

    for k, v in params.items():
        add(k, v)

    return urllib.parse.urlencode(pairs, doseq=True).encode("utf-8")


def _resolve_location(base_url: str, location: str) -> str:
    loc = str(location or "").strip()
    if not loc:
        return ""
    if loc.startswith("http://") or loc.startswith("https://"):
        return loc
    base = str(base_url or "").strip().rstrip("/") + "/"
    return urllib.parse.urljoin(base, loc.lstrip("/"))


def _append_query_param(url: str, name: str, value: str) -> str:
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query.append((name, value))
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query, doseq=True)))


def _trigger_build(
    *,
    job_url: str,
    base_url: str,
    auth: JenkinsAuth,
    parameters: Mapping[str, Any],
    crumb: Optional[Tuple[str, str]],
    timeout_sec: int,
    dry_run: bool,
    opener: Optional[urllib.request.OpenerDirector],
) -> str:
    job = _normalize_job_url(job_url)
    if not job:
        raise RuntimeError("jenkins.triggers.job_url is missing")

    # If the provided URL already points at a build endpoint, use it as-is.
    parsed_job = urllib.parse.urlsplit(job)
    path_lower = str(parsed_job.path or "").rstrip("/").lower()
    if path_lower.endswith("/buildwithparameters"):
        url = job
    elif path_lower.endswith("/build"):
        if parsed_job.query and parameters:
            url = urllib.parse.urlunsplit(
                (
                    parsed_job.scheme,
                    parsed_job.netloc,
                    parsed_job.path.rstrip("/") + "WithParameters",
                    parsed_job.query,
                    parsed_job.fragment,
                )
            )
        else:
            url = job
    else:
        url = job + "buildWithParameters"

    headers: Dict[str, str] = {
        "Authorization": _basic_auth_header(auth),
        "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
    }
    if crumb:
        headers[crumb[0]] = crumb[1]
        if "/build?" in url and crumb[0] == "Jenkins-Crumb":
            # Some Jenkins forms expect crumb in query string for /build endpoints.
            url = _append_query_param(url, crumb[0], crumb[1])

    body = _encode_form_fields(parameters)

    if dry_run:
        print(f"DRY-RUN: POST {url}")
        print("DRY-RUN: parameters submitted:")
        for k in sorted(parameters.keys()):
            v = parameters.get(k)
            if isinstance(v, str) and len(v) > 200:
                vv = v[:200] + "..."
            else:
                vv = v
            print(f"  - {k}={vv!r}")
        return ""

    print(f"  [DEBUG] POST {url}  body_size={len(body) if body else 0}")
    sys.stdout.flush()
    status, hdrs, data = _request(method="POST", url=url, headers=headers, body=body, timeout_sec=timeout_sec, opener=opener)

    if status in (401, 403):
        text = data.decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Jenkins trigger failed (HTTP {status}). "
            f"If CSRF is enabled, ensure crumbIssuer is reachable. body={text[:2000]}"
        )
    if status < 200 or status >= 400:
        text = data.decode("utf-8", errors="replace")
        raise RuntimeError(f"Jenkins trigger failed (HTTP {status}): {text[:2000]}")

    loc = ""
    for hk, hv in hdrs.items():
        if hk.lower() == "location":
            loc = str(hv)
            break

    queue_url = _resolve_location(base_url, loc)
    if not queue_url:
        raise RuntimeError(
            "Jenkins trigger succeeded but did not return Location header for queue item. "
            "Your Jenkins/proxy may be stripping it; fallback matching is not implemented in v1."
        )

    print(f"Queue: {queue_url}")
    return queue_url.rstrip("/") + "/"


def _poll_queue_for_build(
    *,
    queue_url: str,
    auth: JenkinsAuth,
    poll_interval_sec: int,
    queue_timeout_sec: int,
    request_timeout_sec: int,
    opener: Optional[urllib.request.OpenerDirector],
) -> Tuple[int, str]:
    url = queue_url.rstrip("/") + "/api/json"
    headers = {"Authorization": _basic_auth_header(auth)}

    start = time.time()
    while True:
        if time.time() - start > queue_timeout_sec:
            raise TimeoutError(f"Timed out waiting for queue item to start build: {queue_url}")

        resp = _request_json(method="GET", url=url, headers=headers, timeout_sec=request_timeout_sec, opener=opener)
        if bool(resp.get("cancelled")):
            raise RuntimeError(f"Queue item cancelled: {queue_url}")

        execu = resp.get("executable")
        if isinstance(execu, dict) and execu.get("number") is not None:
            num = int(execu.get("number"))
            build_url = str(execu.get("url") or "").strip()
            if build_url:
                return num, build_url.rstrip("/") + "/"
            # If url missing, attempt build_url from task url + number (best effort)
            task = resp.get("task")
            if isinstance(task, dict) and task.get("url"):
                base = str(task.get("url") or "").rstrip("/") + "/"
                return num, base + str(num) + "/"
            return num, ""

        time.sleep(max(1, int(poll_interval_sec)))


def _poll_queues_for_builds(
    *,
    queues: Mapping[str, str],
    auth: JenkinsAuth,
    poll_interval_sec: int,
    queue_timeout_sec: int,
    request_timeout_sec: int,
    opener: Optional[urllib.request.OpenerDirector],
    on_build_started: Optional[Callable[[str, int, str], None]] = None,
) -> Dict[str, Tuple[int, str]]:
    """Poll multiple queue items until each has started a build.

    Returns: {run_name: (build_number, build_url)}
    """

    pending = {str(k): str(v) for k, v in queues.items() if str(k).strip() and str(v).strip()}
    if not pending:
        return {}

    headers = {"Authorization": _basic_auth_header(auth)}
    start = time.time()
    results: Dict[str, Tuple[int, str]] = {}

    while pending:
        if time.time() - start > queue_timeout_sec:
            pretty = ", ".join(f"{k}={v}" for k, v in pending.items())
            raise TimeoutError(f"Timed out waiting for queue items to start builds: {pretty}")

        done: List[str] = []
        # Heartbeat: show we're still polling.
        ts = time.strftime("%H:%M:%S")
        elapsed = int(time.time() - start)
        for run_name, queue_url in pending.items():
            url = queue_url.rstrip("/") + "/api/json"
            resp = _request_json(method="GET", url=url, headers=headers, timeout_sec=request_timeout_sec, opener=opener)
            if bool(resp.get("cancelled")):
                raise RuntimeError(f"Queue item cancelled for {run_name}: {queue_url}")

            execu = resp.get("executable")
            if isinstance(execu, dict) and execu.get("number") is not None:
                num = int(execu.get("number"))
                build_url = str(execu.get("url") or "").strip()
                if not build_url:
                    task = resp.get("task")
                    if isinstance(task, dict) and task.get("url"):
                        base = str(task.get("url") or "").rstrip("/") + "/"
                        build_url = base + str(num) + "/"
                if not build_url:
                    raise RuntimeError(f"Could not determine build_url for {run_name} (build #{num})")
                results[run_name] = (num, build_url.rstrip("/") + "/")
                done.append(run_name)
                if on_build_started is not None:
                    try:
                        on_build_started(run_name, num, results[run_name][1])
                    except Exception:
                        # Best-effort notification; do not break polling.
                        pass

        if pending:
            waiting = ", ".join(f"{k} queue" for k in sorted(pending.keys()))
            print(f"[{ts}] Waiting in queue ({elapsed}s): {waiting}")

        for k in done:
            pending.pop(k, None)

        if pending:
            time.sleep(max(1, int(poll_interval_sec)))

    return results


def _poll_build_result(
    *,
    build_url: str,
    auth: JenkinsAuth,
    poll_interval_sec: int,
    build_timeout_sec: int,
    request_timeout_sec: int,
    opener: Optional[urllib.request.OpenerDirector],
) -> str:
    if not build_url:
        raise RuntimeError("Missing build_url to poll")

    url = build_url.rstrip("/") + "/api/json"
    headers = {"Authorization": _basic_auth_header(auth)}

    start = time.time()
    last_state = ""
    while True:
        if time.time() - start > build_timeout_sec:
            raise TimeoutError(f"Timed out waiting for build to finish: {build_url}")

        resp = _request_json(method="GET", url=url, headers=headers, timeout_sec=request_timeout_sec, opener=opener)
        building = bool(resp.get("building"))
        result = str(resp.get("result") or "").strip()
        display_name = str(resp.get("displayName") or "").strip()
        if building:
            state = display_name or ("building" if not result else f"building({result})")
            if state != last_state:
                print(f"Build running: {build_url} ({state})")
                last_state = state
            time.sleep(max(1, int(poll_interval_sec)))
            continue

        if not result:
            # Some Jenkins may briefly show building=false and result=null. Retry.
            time.sleep(max(1, int(poll_interval_sec)))
            continue

        return result


def _poll_build_results(
    *,
    builds: Mapping[str, Tuple[int, str]],
    auth: JenkinsAuth,
    poll_interval_sec: int,
    build_timeout_sec: int,
    request_timeout_sec: int,
    opener: Optional[urllib.request.OpenerDirector],
    on_build_finished: Optional[Callable[[str, int, str, str], None]] = None,
) -> Dict[str, str]:
    """Poll multiple builds until each has finished.

    builds: {run_name: (build_number, build_url)}
    returns: {run_name: result}
    """

    pending: Dict[str, Tuple[int, str]] = {
        str(k): (int(v[0]), str(v[1]))
        for k, v in builds.items()
        if str(k).strip() and isinstance(v, tuple) and len(v) == 2 and str(v[1]).strip()
    }
    if not pending:
        return {}

    headers = {"Authorization": _basic_auth_header(auth)}
    start = time.time()
    results: Dict[str, str] = {}
    last_states: Dict[str, str] = {}

    while pending:
        if time.time() - start > build_timeout_sec:
            pretty = ", ".join(f"{k}={v[1]}" for k, v in pending.items())
            raise TimeoutError(f"Timed out waiting for builds to finish: {pretty}")

        done: List[str] = []
        ts = time.strftime("%H:%M:%S")
        elapsed = int(time.time() - start)
        heartbeat: Dict[str, str] = {}
        for run_name, (num, build_url) in pending.items():
            url = build_url.rstrip("/") + "/api/json"
            resp = _request_json(method="GET", url=url, headers=headers, timeout_sec=request_timeout_sec, opener=opener)

            building = bool(resp.get("building"))
            result = str(resp.get("result") or "").strip()
            display_name = str(resp.get("displayName") or "").strip()

            if building:
                state = display_name or "building"
                if last_states.get(run_name) != state:
                    print(f"Build running: {run_name} #{num} {build_url} ({state})")
                    last_states[run_name] = state
                heartbeat[run_name] = "building"
                continue

            if not result:
                # Some Jenkins may briefly show building=false and result=null. Retry.
                heartbeat[run_name] = "finishing"
                continue

            results[run_name] = result
            done.append(run_name)
            heartbeat[run_name] = result
            if on_build_finished is not None:
                try:
                    on_build_finished(run_name, num, build_url, result)
                except Exception:
                    # Best-effort notification; do not break polling.
                    pass

        if pending:
            parts = []
            for k in sorted(pending.keys()):
                parts.append(f"{k}={heartbeat.get(k,'polling')}")
            print(f"[{ts}] Polling builds ({elapsed}s): " + ", ".join(parts))

        for k in done:
            pending.pop(k, None)

        if pending:
            time.sleep(max(1, int(poll_interval_sec)))

    return results


def _get_auth_from_cfg(cfg: Dict[str, Any]) -> JenkinsAuth:
    from credential_resolver import resolve_credential, CREDENTIAL_ENV_MAP

    j = cfg.get("jenkins") or {}
    if not isinstance(j, dict):
        j = {}
    a = j.get("auth") or {}
    if not isinstance(a, dict):
        a = {}

    username = resolve_credential(
        str(a.get("username") or "").strip(),
        CREDENTIAL_ENV_MAP.get("jenkins.auth.username", "JENKINS_USERNAME"),
        required=False, sensitive=False,
    )
    password = resolve_credential(
        str(a.get("password") or "").strip(),
        CREDENTIAL_ENV_MAP.get("jenkins.auth.password", "JENKINS_PASSWORD"),
        required=False, sensitive=True,
    )
    # Fallback to old env vars if resolver returned empty
    if not username:
        username = (os.environ.get("JENKINS_USERNAME") or "").strip()
    if not password:
        password = (os.environ.get("JENKINS_PASSWORD") or "").strip()
    if not username or not password:
        raise RuntimeError("Missing Jenkins basic auth (jenkins.auth.username/password or env JENKINS_USERNAME/JENKINS_PASSWORD)")
    return JenkinsAuth(username=username, password=password)


def _get_jenkins_base_url(cfg: Dict[str, Any]) -> str:
    j = cfg.get("jenkins") or {}
    if not isinstance(j, dict):
        j = {}
    return str(j.get("base_url") or "").strip()


def _get_triggers_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    j = cfg.get("jenkins") or {}
    if not isinstance(j, dict):
        j = {}
    t = j.get("triggers") or {}
    if not isinstance(t, dict):
        t = {}
    return t


def _get_trigger_params(cfg: Dict[str, Any], run_name: str, platform_key: Optional[str] = None) -> Dict[str, Any]:
    tcfg = _get_triggers_cfg(cfg)

    common_params: Any = {}
    run_cfg = tcfg.get(run_name) or {}
    if isinstance(run_cfg, dict):
        common_params = run_cfg.get("parameters") or {}

    if not isinstance(common_params, dict):
        raise RuntimeError(f"jenkins.triggers.{run_name}.parameters must be an object")

    if not platform_key:
        return common_params

    platform_params: Any = {}
    platform_cfg = tcfg.get(platform_key) or {}
    if isinstance(platform_cfg, dict):
        platform_run_cfg = platform_cfg.get(run_name) or {}
        if isinstance(platform_run_cfg, dict):
            platform_params = platform_run_cfg.get("parameters") or {}

    if not isinstance(platform_params, dict):
        raise RuntimeError(f"jenkins.triggers.{platform_key}.{run_name}.parameters must be an object")

    merged_params = dict(common_params)
    merged_params.update(platform_params)
    return merged_params


def _get_enabled_platforms(cfg: Dict[str, Any]) -> List[str]:
    plat_cfg = cfg.get("Platform") or {}
    enabled: List[str] = []
    if isinstance(plat_cfg, dict):
        for k, v in plat_cfg.items():
            if str(v or "").strip().upper() == "YES":
                enabled.append(str(k).strip())
    return enabled


def _get_job_url_for_platform(cfg: Dict[str, Any], key: str) -> str:
    tcfg = _get_triggers_cfg(cfg)
    jm = tcfg.get("job_url_map") or {}
    if isinstance(jm, dict) and str(jm.get(key) or "").strip():
        return str(jm.get(key)).strip()

    defaults = {
        "MHS003": "https://jenkins.huami.com/job/firmware_auto_trigger/job/HuamiOS_HS3",
        "MHS003S": "https://jenkins.huami.com/job/firmware_auto_trigger/job/HuamiOS_multi_platform/",
        "NXP595": "https://jenkins.huami.com/job/firmware_auto_trigger/job/HuamiOS/",
        "APOLLO4": "https://jenkins.huami.com/job/firmware_auto_trigger/job/HuamiOS/",
        "MHS_MULTI": "https://jenkins.huami.com/job/firmware_auto_trigger/job/HuamiOS_multi_platform/",
    }
    return defaults.get(key, str(tcfg.get("job_url") or "").strip())


def _get_job_urls_from_platforms(cfg: Dict[str, Any]) -> Dict[str, str]:
    enabled_plats = _get_enabled_platforms(cfg)
    jobs: Dict[str, str] = {}
    if not enabled_plats:
        return jobs

    if "MHS003" in enabled_plats and "MHS003S" in enabled_plats:
        jobs["MHS_MULTI"] = _get_job_url_for_platform(cfg, "MHS_MULTI")
    else:
        if "MHS003" in enabled_plats:
            jobs["MHS003"] = _get_job_url_for_platform(cfg, "MHS003")
        if "MHS003S" in enabled_plats:
            jobs["MHS003S"] = _get_job_url_for_platform(cfg, "MHS003S")

    if "NXP595" in enabled_plats:
        jobs["NXP595"] = _get_job_url_for_platform(cfg, "NXP595")
    if "APOLLO4" in enabled_plats and "NXP595" not in jobs:
        jobs["APOLLO4"] = _get_job_url_for_platform(cfg, "APOLLO4")

    return jobs


def _maybe_trigger_changelog_async(
    *,
    cfg_path: Path,
    cfg: Dict[str, Any],
    auth: JenkinsAuth,
    crumb: Optional[Tuple[str, str]],
    request_timeout_sec: int,
    opener: Optional[urllib.request.OpenerDirector],
) -> None:
    chg = cfg.get("changelog") or {}
    if not isinstance(chg, dict):
        return
    if not bool(chg.get("enabled", False)):
        return
    # default: trigger early after builds to overlap with download/upload
    if not bool(chg.get("auto_trigger_after_builds", True)):
        return

    job_url = str(chg.get("jenkins_job_url") or "").strip()
    if not job_url:
        return
    job = _normalize_job_url(job_url)

    raw_params = chg.get("params") or {}
    if not isinstance(raw_params, dict):
        raw_params = {}
    params = _expand_jenkins_params(raw_params=raw_params, full_cfg=cfg)
    _normalize_changelog_manifest_file(params, cfg)

    print("\n== Changelog: trigger asynchronously ==")
    try:
        queue_url = _trigger_build(
            job_url=job,
            base_url=_get_jenkins_base_url(cfg),
            auth=auth,
            parameters=params,
            crumb=crumb,
            timeout_sec=int(request_timeout_sec),
            dry_run=False,
            opener=opener,
        )
        # Poll a short while to resolve the build number (non-blocking overall)
        num, build_url = _poll_queue_for_build(
            queue_url=queue_url,
            auth=auth,
            poll_interval_sec=2,
            queue_timeout_sec=60,
            request_timeout_sec=int(request_timeout_sec),
            opener=opener,
        )
        print(f"Changelog: queued -> build #{num} {build_url}")
        # Persist a small state for the pipeline to pick up later
        state = {
            "job_url": job,
            "build_number": int(num),
            "build_url": build_url,
            "triggered_at": _now_tag(),
            "updated_at": _now_tag(),
            "status": "queued",
            "result": "",
            "parameters": params,
        }
        _write_json(_output_dir() / "changelog_job.json", state)

        changelog_cfg = cfg.get("changelog")
        if not isinstance(changelog_cfg, dict):
            changelog_cfg = {}
            cfg["changelog"] = changelog_cfg
        changelog_cfg["build_url"] = build_url
        changelog_cfg["build_number"] = int(num)
        changelog_cfg["status"] = "queued"
        changelog_cfg["result"] = ""
        changelog_cfg["triggered_at"] = state["triggered_at"]
        changelog_cfg["updated_at"] = state["updated_at"]
        _atomic_write_json(cfg_path, cfg, backup=False)
    except Exception as e:
        print(f"WARN: Changelog async trigger failed: {e}", file=sys.stderr)
        # Do not fail the main build triggers
        return


def _tscan_adjust_version(params: Dict[str, Any]) -> Dict[str, Any]:
    """TSCAN 版本号第 2 位减 1，避免与 debug 的 VERSION_NAME 相同导致 Jenkins 取消构建。
    例如: 3.13.1 → 3.12.1
    """
    def _decrement_middle(ver: str) -> str:
        parts = str(ver or "").strip().split(".")
        if len(parts) >= 2:
            try:
                mid = int(parts[1])
                if mid > 0:
                    parts[1] = str(mid - 1)
                else:
                    parts[1] = "66"   # 第 2 位为 0 时设为 66，例如 3.0.1 → 3.66.1
                return ".".join(parts)
            except (ValueError, TypeError):
                pass
        return ver

    modified = False
    for key in ("VERSION_NAME", "FCT_VERSION_NAME"):
        if params.get(key):
            old = str(params[key])
            new = _decrement_middle(old)
            if new != old:
                params[key] = new
                print(f"  TSCAN {key}: {old} → {new}")
                modified = True
    return params


def _download_tscan_artifacts(
    *,
    build_url: str,
    out_dir: Path,
    auth,
    base_url: str,
    verify_tls: bool,
    request_timeout_sec: int,
    opener,
    project: str,
    cfg: Dict[str, Any],
) -> None:
    """Download TSCAN result zip files from Jenkins artifacts."""
    tscan_files = ['BOOT_tscanResult.zip', 'OTA_tscanResult.zip', 'RECOVERY_tscanResult.zip']
    out_dir.mkdir(parents=True, exist_ok=True)

    for fname in tscan_files:
        artifact_url = f"{build_url.rstrip('/')}artifact/{fname}"
        dest = out_dir / fname
        try:
            print(f"  Downloading TSCAN artifact: {fname}")
            _download_file(
                url=artifact_url,
                dest=dest,
                auth=auth,
                verify_tls=verify_tls,
                timeout_sec=request_timeout_sec,
                opener=opener,
            )
            print(f"    -> saved to {dest}")
        except Exception as e:
            print(f"WARN: failed to download {fname}: {e}", file=sys.stderr)

    # 将 TSCAN 构建 URL 和上传配置写入 cfg（pipeline runner 会上传 NAS）
    jcfg = cfg.setdefault("jenkins", {})
    jbuilds = jcfg.setdefault("builds", {})
    tscan_build_cfg = jbuilds.setdefault("tscan", {})
    tscan_build_cfg["build_url"] = build_url.rstrip("/") + "/"
    if "download" not in tscan_build_cfg:
        tscan_build_cfg["download"] = {
            "mode": "artifacts",
            "include_globs": ["**/*"],
            "exclude_globs": ["**/*.log"],
            "output_dir": f"work/download/{project}_tscan",
            "overwrite": True,
        }

    nas_cfg = cfg.setdefault("nas", {})
    uploads = nas_cfg.setdefault("uploads", [])
    tscan_upload = {
        "name": "tscan",
        "remote_subdir": "Monkey",
        "local_dir": f"work/download/{project}_tscan",
        "include_globs": ["**/*.zip"],
        "exclude_globs": [],
        "overwrite": True,
    }
    # 避免重复添加
    if not any(u.get("name") == "tscan" for u in uploads):
        uploads.append(tscan_upload)
        print(f"  Added TSCAN upload config to NAS uploads")


def _download_file(
    *,
    url: str,
    dest: Path,
    auth,
    verify_tls: bool,
    timeout_sec: int,
    opener,
) -> None:
    import urllib.request as _ur
    from pathlib import Path as _P

    ctx = None
    if not verify_tls:
        ctx = ssl._create_unverified_context()

    auth_header = f"Basic {base64.b64encode(f'{auth.username}:{auth.password}'.encode()).decode()}"
    req = _ur.Request(url, method="GET")
    req.add_header("Authorization", auth_header)

    do = opener.open if opener is not None else _ur.urlopen
    with do(req, timeout=timeout_sec, context=ctx) as resp:
        dest.write_bytes(resp.read())


def _run_tscan_standalone(args, cfg, cfg_path, auth, crumb, opener) -> int:
    """TSCAN 独立构建模式：触发 TSCAN Jenkins 构建，轮询，打印结果，退出。"""
    triggers_cfg = cfg.get("jenkins", {}).get("triggers", {})
    tscan_raw = triggers_cfg.get("tscan") or {}

    base_url = _get_jenkins_base_url(cfg)
    if auth is None:
        auth = _get_auth_from_cfg(cfg)

    # Get TSCAN job URL: _tscan.job_url (frontend) → tscan config → platform → triggers.job_url
    tscan_job_url = str((cfg.get("_tscan") or {}).get("job_url") or "").strip()
    if not tscan_job_url:
        tscan_job_url = str(tscan_raw.get("job_url") or "").strip()
    if not tscan_job_url:
        job_urls = _get_job_urls_from_platforms(cfg)
        tscan_job_url = list(job_urls.values())[0] if job_urls else ""
    if not tscan_job_url:
        tscan_job_url = str(triggers_cfg.get("job_url") or "").strip()
    if not tscan_job_url:
        print("ERROR: could not determine TSCAN job URL", file=sys.stderr)
        return 2

    # 构建完整的 TSCAN 触发参数（24 个，缺一不可）
    vars_cfg = cfg.get("vars", {})
    # HMI_CORE_MM_OWNER_DEP：跟随 release 值（从 triggers.Tscan.parameters 读取，默认 2）
    _tscan_trigger = _get_trigger_params(cfg, "Tscan", None) or {}
    hmi_dep = str(_tscan_trigger.get("HMI_CORE_MM_OWNER_DEP") or "2")
    tscan_params = {
        "PRODUCT":                  vars_cfg.get("project_id") or cfg.get("release", {}).get("project", ""),
        "PROJECT_2PD":              "NO",
        "TAG_NAME":                 vars_cfg.get("tag_algo", ""),
        "BOOT_TAG_NAME":            vars_cfg.get("tag_boot", ""),
        "RECOVERY_TAG_NAME":        vars_cfg.get("tag_recovery", ""),
        "FCT_TAG_NAME":             vars_cfg.get("tag_fct", ""),
        "BUILD_MODE":               vars_cfg.get("build_mode", "OTA,BOOT,RECOVERY"),
        "BUILD_RECOVERY":           "是",
        "Translation_CHECK":        "NO",
        "BUILD_TEST_TOOL":          "否",
        "TOOL_BRANCH_OR_TAG":       "",
        "GPS_VERSION":              "",
        "VERSION_NAME":             vars_cfg.get("version_name", ""),
        "FCT_VERSION_NAME":         vars_cfg.get("fct_version_name", ""),
        "RES_TOOL_VERSION":         "NEWEST",
        "TSCANCODE_CHECK":          "YES",
        "BUILD_RELEASE":            "release",
        "HMI_CORE_MM_OWNER_DEP":    hmi_dep,
        "AUTH_CFG_AUTO_BINDING":    "NO",
        "FW_VER_STRATEGY_ENV":      "none",
        "BUILD_REASON":             "开发版本",
        "BUILD_ARGUMENT":           "测试",
        "TEMPKEY":                  "否",
    }
    # 从版本号提取 VERSION_NAME / FCT_VERSION_NAME（前三段）
    if vars_cfg.get("version"):
        ver3 = ".".join(vars_cfg["version"].split(".")[:3])
        if not tscan_params["VERSION_NAME"]:
            tscan_params["VERSION_NAME"] = ver3
        if not tscan_params["FCT_VERSION_NAME"]:
            tscan_params["FCT_VERSION_NAME"] = ver3

    _tscan_adjust_version(tscan_params)

    print(f"\n== Trigger TSCAN (standalone) ==")
    print(f"  Job URL: {tscan_job_url}")
    print(f"  Parameters ({len(tscan_params)}):")
    for k in sorted(tscan_params.keys()):
        print(f"    {k} = {tscan_params[k]!r}")
    sys.stdout.flush()

    q_tscan = _trigger_build(
        job_url=tscan_job_url, base_url=base_url, auth=auth,
        parameters=tscan_params, crumb=crumb,
        timeout_sec=int(args.request_timeout_sec), dry_run=bool(args.dry_run),
        opener=opener,
    )

    if bool(args.dry_run):
        print("DRY-RUN: would trigger TSCAN and poll")
        return 0

    tscan_builds = _poll_queues_for_builds(
        queues={"tscan": q_tscan}, auth=auth,
        poll_interval_sec=int(args.poll_interval_sec),
        queue_timeout_sec=int(args.queue_timeout_sec),
        request_timeout_sec=int(args.request_timeout_sec),
        opener=opener,
    )
    tscan_build_url = str(tscan_builds.get("tscan", (0, ""))[1] or "")
    print(f"TSCAN build: {tscan_build_url}")
    sys.stdout.flush()

    tscan_results = _poll_build_results(
        builds=tscan_builds, auth=auth,
        poll_interval_sec=int(args.poll_interval_sec),
        build_timeout_sec=int(args.build_timeout_sec),
        request_timeout_sec=int(args.request_timeout_sec),
        opener=opener,
    )
    result = tscan_results.get("tscan", "?")
    print(f"TSCAN result: {result}")
    sys.stdout.flush()

    if tscan_build_url and result == "SUCCESS":
        cfg.setdefault("jenkins", {}).setdefault("builds", {})
        cfg["jenkins"]["builds"]["tscan"] = {"build_url": tscan_build_url}
        _atomic_write_json(cfg_path, cfg, backup=False)
        print(f"\n✅ TSCAN 构建成功")
        print(f"可点击查看下载 TSCAN 产物：{tscan_build_url}")
        return 0
    else:
        print(f"\n❌ TSCAN 构建失败 (result={result})", file=sys.stderr)
        return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Trigger Jenkins builds (release then debug) and write build_url back into JSON")
    ap.add_argument("--config", required=True, help="Path to release_pipeline config JSON")
    ap.add_argument("--no-backup", action="store_true", help="Do not write .bak backup before overwriting config")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not trigger Jenkins; only print what would be posted",
    )

    ap.add_argument(
        "--use-existing-build-urls",
        action="store_true",
        help="Do not trigger Jenkins. Use existing jenkins.builds.{release,debug}.build_url from config; poll until finished and then optionally run pipeline.",
    )
    ap.add_argument(
        "--mock-build-success",
        action="store_true",
        help="Do not trigger Jenkins and do not poll. Pretend both builds are SUCCESS (useful to test --run-pipeline handoff).",
    )

    ap.add_argument(
        "--preauth-feishu",
        action="store_true",
        help="Run Feishu OAuth pre-auth now (via release_pipeline_run.py --feishu-preauth-only) before triggering long builds.",
    )
    ap.add_argument(
        "--preauth-feishu-force",
        action="store_true",
        help="Force Feishu OAuth even if user_access_token already exists.",
    )

    ap.add_argument("--poll-interval-sec", type=int, default=5, help="Polling interval for queue/build status")
    ap.add_argument("--queue-timeout-sec", type=int, default=20 * 60, help="Max seconds to wait in Jenkins queue")
    ap.add_argument("--build-timeout-sec", type=int, default=3 * 60 * 60, help="Max seconds to wait for build to finish")
    ap.add_argument("--request-timeout-sec", type=int, default=30, help="Timeout for individual HTTP requests")

    ap.add_argument(
        "--tscan-standalone",
        action="store_true",
        help="Trigger TSCAN build only (standalone mode), then exit.",
    )

    ap.add_argument(
        "--run-pipeline",
        action="store_true",
        help="After builds finish SUCCESS and config is updated, run release_pipeline_run.py to continue download/upload/doc/feishu.",
    )
    ap.add_argument(
        "--pipeline-script",
        default="",
        help="Path to release_pipeline_run.py (default: sibling of this script).",
    )
    ap.add_argument(
        "--pipeline-args",
        action="append",
        default=[],
        help="Extra args passed to release_pipeline_run.py (repeatable). Example: --pipeline-args=--skip-feishu",
    )
    ap.add_argument(
        "--pipeline-subcommand",
        default="",
        help="Subcommand for pipeline script (e.g. 'run' for lark_release.py). Inserted before --config.",
    )

    args = ap.parse_args()

    if bool(args.use_existing_build_urls) and bool(args.mock_build_success):
        print("ERROR: --use-existing-build-urls and --mock-build-success are mutually exclusive", file=sys.stderr)
        return 2

    cfg_path = Path(str(args.config)).expanduser().resolve()
    if not cfg_path.exists():
        print(f"Config not found: {cfg_path}", file=sys.stderr)
        return 2

    # 从配置文件名提取项目名（gqf_cologne.json → cologne）
    project = cfg_path.stem
    if project.startswith("gqf_"):
        project = project[4:]

    cfg = _read_json(cfg_path)

    def _cfg_get(dotted: str, default: Any) -> Any:
        try:
            return _get_by_path(cfg, dotted)
        except KeyError:
            return default

    def _cfg_bool(dotted: str, default: bool = False) -> bool:
        return bool(_cfg_get(dotted, default))

    # Optional: pre-auth Feishu OAuth before doing anything long-running.
    if bool(args.preauth_feishu) or _cfg_bool("jenkins.triggers.preauth_feishu", False):
        script = str(Path(__file__).with_name("release_pipeline_run.py"))
        cmd = [sys.executable, script, "--config", str(cfg_path), "--feishu-preauth-only"]
        if bool(args.preauth_feishu_force):
            cmd.append("--feishu-preauth-force")
        print("\n== Feishu PRE-AUTH ==")
        print(" ".join(cmd))
        p = subprocess.run(cmd)
        if int(p.returncode) != 0:
            raise RuntimeError(f"Feishu pre-auth failed with exit code {p.returncode}")

    wh = _get_webhook_cfg(cfg)
    wh_enabled = bool(wh.get("enabled", False))
    wh_url = str(wh.get("url") or "").strip()
    if wh_enabled and wh_url:
        print(f"WebHook enabled: {wh_url}")
    elif wh_enabled and not wh_url:
        print("WebHook enabled but url is empty (no notifications will be sent)")
    else:
        print("WebHook disabled (set notifications.webhook.enabled=true to enable)")

    # Keep cookies across crumb -> build trigger -> polling requests.
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

    # Validate triggers config (only needed when actually triggering builds)
    if not bool(args.use_existing_build_urls) and not bool(args.mock_build_success):
        tcfg = _get_triggers_cfg(cfg)
        job_urls = _get_job_urls_from_platforms(cfg)
        if not job_urls and not str(tcfg.get("job_url") or "").strip():
            print("Missing jenkins.triggers.job_url or Platform-based job configuration", file=sys.stderr)
            return 2

    # Jenkins auth/crumb are only needed when triggering or polling real builds.
    auth: Optional[JenkinsAuth] = None
    crumb: Optional[Tuple[str, str]] = None
    if not bool(args.mock_build_success):
        base_url = _get_jenkins_base_url(cfg)
        auth = _get_auth_from_cfg(cfg)
        crumb = _try_get_crumb(base_url=base_url, auth=auth, timeout_sec=int(args.request_timeout_sec), opener=opener)
        if crumb:
            print(f"CSRF crumb detected: {crumb[0]}")
        else:
            print("CSRF crumb not detected (continuing)")
    else:
        print("CSRF crumb check skipped (mock mode)")

    try:
        # ── TSCAN standalone mode ──
        if bool(args.tscan_standalone):
            return _run_tscan_standalone(args, cfg, cfg_path, auth, crumb, opener)

        # Trigger builds. Support optional top-level `Platform` config to select
        # which platform-specific Jenkins jobs to trigger in parallel. Backwards
        # compatible: if no Platform is configured, use the single
        # jenkins.triggers.job_url as before (release/debug for that job).
        q_release = ""
        q_debug = ""
        queue_map: Dict[str, str] = {}

        if not bool(args.use_existing_build_urls) and not bool(args.mock_build_success):
            base_url = _get_jenkins_base_url(cfg)
            auth = _get_auth_from_cfg(cfg)

            jobs_to_trigger = _get_job_urls_from_platforms(cfg)
            if not jobs_to_trigger:
                job_url = str(_get_triggers_cfg(cfg).get("job_url") or "").strip()
                if not job_url:
                    raise RuntimeError("Missing jenkins.triggers.job_url")
                jobs_to_trigger["default"] = job_url

            for job_key, job_url in jobs_to_trigger.items():
                platform_key = None if job_key == "default" else job_key

                # Prepare platform_select handling: prefer explicit `platform_select` in cfg;
                # if missing, derive from `Platform` entries with value YES.
                raw_platform_select = cfg.get("platform_select", None)
                if raw_platform_select is None:
                    plat_cfg = cfg.get("Platform") or {}
                    if isinstance(plat_cfg, dict):
                        raw_platform_select = [k for k, v in plat_cfg.items() if str(v or "").strip().upper() == "YES"]
                    else:
                        raw_platform_select = []

                # Normalize into a list of lowercase tokens.
                if isinstance(raw_platform_select, str):
                    raw_list = [x.strip() for x in raw_platform_select.split(",") if x.strip()]
                elif isinstance(raw_platform_select, list):
                    raw_list = raw_platform_select
                else:
                    raw_list = []
                platform_select_list = [str(x).strip().lower() for x in raw_list if str(x).strip()]

                def _maybe_add_platform_select(params: dict) -> dict:
                    # Only add PLATFORM_SELECT when triggering the multi-platform job
                    # (MHS003S or MHS_MULTI) and when the config requests any MHS entries.
                    jobs_using_multi = (job_key == "MHS003S" or job_key == "MHS_MULTI")
                    if not jobs_using_multi:
                        return params
                    if not platform_select_list:
                        return params
                    # only include mhs* tokens for the multi-platform job and preserve
                    # single vs multi selection as configured by the user
                    mhs_list = [p for p in platform_select_list if p.startswith("mhs")]
                    if not mhs_list:
                        return params
                    params = dict(params)
                    params["PLATFORM_SELECT"] = ",".join(mhs_list)
                    return params

                # ── 读取触发模式 ──
                triggers_cfg = _get_triggers_cfg(cfg)
                trigger_mode = str(triggers_cfg.get("modes", "async") or "async").strip().lower()
                is_sync = (trigger_mode == "sync")
                if is_sync:
                    print(f"\n== Trigger mode: SYNC (Release → wait → Debug) ==")
                else:
                    print(f"\n== Trigger mode: ASYNC (Release + Debug parallel) ==")

                print(f"\n== Trigger RELEASE ({job_key}) ==")
                rel_raw = _get_trigger_params(cfg, "release", platform_key)
                rel_params = _expand_jenkins_params(raw_params=rel_raw, full_cfg=cfg)
                rel_params = _maybe_add_platform_select(rel_params)
                q_rel = _trigger_build(
                    job_url=job_url,
                    base_url=base_url,
                    auth=auth,
                    parameters=rel_params,
                    crumb=crumb,
                    timeout_sec=int(args.request_timeout_sec),
                    dry_run=bool(args.dry_run),
                    opener=opener,
                )
                queue_map[f"{job_key}.release"] = q_rel

                if is_sync:
                    # ── SYNC 模式：等待 Release 完成后再触发 Debug ──
                    if not bool(args.dry_run) and str(q_rel).strip():
                        print(f"\n== SYNC: waiting for RELEASE ({job_key}) to complete before triggering DEBUG ==")
                        sys.stdout.flush()
                        rel_queues = {f"{job_key}.release": q_rel}
                        rel_builds = _poll_queues_for_builds(
                            queues=rel_queues,
                            auth=auth,
                            poll_interval_sec=int(args.poll_interval_sec),
                            queue_timeout_sec=int(args.queue_timeout_sec),
                            request_timeout_sec=int(args.request_timeout_sec),
                            opener=opener,
                        )
                        # Save release build info to cfg now (later code will skip release)
                        for run_name, (num, build_url) in rel_builds.items():
                            print(f"  {run_name} build: #{num} {build_url}")
                            # Persist build URL immediately
                            _set_by_path(cfg, f"jenkins.builds.release.build_url", str(build_url))
                            sys.stdout.flush()
                        rel_results = _poll_build_results(
                            builds=rel_builds,
                            auth=auth,
                            poll_interval_sec=int(args.poll_interval_sec),
                            build_timeout_sec=int(args.build_timeout_sec),
                            request_timeout_sec=int(args.request_timeout_sec),
                            opener=opener,
                            on_build_finished=None,  # no parallel callbacks during sync
                        )
                        for run_name, result in rel_results.items():
                            bn, bu = rel_builds.get(run_name, (0, ""))
                            print(f"  {run_name} result: {result}")
                            sys.stdout.flush()
                            if result != "SUCCESS":
                                raise RuntimeError(f"Release build {run_name} failed (result={result}), aborting debug trigger")
                            # Notify release completion
                            hdr = _build_project_header(cfg)
                            _maybe_notify_webhook_text(
                                cfg=cfg,
                                text=f"[{hdr}] Jenkins release finished: result={result} {str(bu)}",
                                request_timeout_sec=int(args.request_timeout_sec),
                            )
                            _send_bot_notification(cfg=cfg, text=f"✅ [{hdr}] 编译完成：{run_name} #{bn} [{result}]\n{str(bu)}")
                        # Trigger changelog after release success (sync mode)
                        try:
                            _maybe_trigger_changelog_async(
                                cfg_path=cfg_path, cfg=cfg, auth=auth,
                                crumb=crumb, request_timeout_sec=int(args.request_timeout_sec), opener=opener)
                        except Exception as chg_err:
                            print(f"WARN: changelog trigger after sync release failed: {chg_err}", file=sys.stderr)
                        # Remove release from queue_map so later polling skips it (already done)
                        queue_map.pop(f"{job_key}.release", None)
                        # Save config with release build_url
                        _atomic_write_json(cfg_path, cfg, backup=not bool(args.no_backup))

                print(f"\n== Trigger DEBUG ({job_key}) ==")
                dbg_raw = _get_trigger_params(cfg, "debug", platform_key)
                dbg_params = _expand_jenkins_params(raw_params=dbg_raw, full_cfg=cfg)
                dbg_params = _maybe_add_platform_select(dbg_params)
                q_dbg = _trigger_build(
                    job_url=job_url,
                    base_url=base_url,
                    auth=auth,
                    parameters=dbg_params,
                    crumb=crumb,
                    timeout_sec=int(args.request_timeout_sec),
                    dry_run=bool(args.dry_run),
                    opener=opener,
                )
                queue_map[f"{job_key}.debug"] = q_dbg

                # ── 触发 TSCAN（与 release/debug 并行） ──
                build_tscan_enabled = str(cfg.get("vars", {}).get("build_tscan") or "").strip().lower() == "yes"
                if build_tscan_enabled and not bool(args.dry_run) and not bool(args.mock_build_success):
                    tscan_raw = _get_trigger_params(cfg, "Tscan", None)
                    if tscan_raw:
                        tscan_job_url = str(_get_triggers_cfg(cfg).get("job_url") or "").strip()
                        if not tscan_job_url:
                            existing_jobs = _get_job_urls_from_platforms(cfg) if jobs_to_trigger else {}
                            tscan_job_url = list(existing_jobs.values())[0] if existing_jobs else ""
                        if tscan_job_url:
                            tscan_params = _expand_jenkins_params(raw_params=dict(tscan_raw), full_cfg=cfg)
                            _tscan_adjust_version(tscan_params)
                            print(f"\n== Trigger TSCAN ({job_key}) — 与 release/debug 并行 ==")
                            sys.stdout.flush()
                            q_tscan = _trigger_build(
                                job_url=tscan_job_url,
                                base_url=base_url,
                                auth=auth,
                                parameters=tscan_params,
                                crumb=crumb,
                                timeout_sec=int(args.request_timeout_sec),
                                dry_run=bool(args.dry_run),
                                opener=opener,
                            )
                            queue_map[f"{job_key}.tscan"] = q_tscan
                            print(f"  TSCAN queued: {q_tscan}")

        auto_run_pipeline = bool(args.run_pipeline) or _cfg_bool("jenkins.triggers.auto_run_pipeline", False)

        def _resolve_pipeline_script() -> str:
            script = str(args.pipeline_script or "").strip()
            if script:
                return script
            script = str(_cfg_get("jenkins.triggers.pipeline_script", "")).strip()
            if script:
                return script
            return str(Path(__file__).with_name("release_pipeline_run.py"))

        def _resolve_pipeline_args() -> List[str]:
            cfg_args = _cfg_get("jenkins.triggers.pipeline_args", [])
            if not isinstance(cfg_args, list):
                cfg_args = []
            cli_args = args.pipeline_args or []
            merged = [str(x) for x in cfg_args if str(x).strip()] + [str(x) for x in cli_args if str(x).strip()]
            return merged

        def _resolve_pipeline_subcommand() -> str:
            """If the pipeline script uses subcommands (like lark_release.py 'run'),
            return it so it's placed before --config."""
            sub = str(args.pipeline_subcommand or "").strip()
            if sub:
                return sub
            sub = str(_cfg_get("jenkins.triggers.pipeline_subcommand", "")).strip()
            return sub

        if args.dry_run:
            if auto_run_pipeline:
                script = _resolve_pipeline_script()
                subcmd = _resolve_pipeline_subcommand()
                cmd = [sys.executable, script] + ([subcmd] if subcmd else []) + ["--config", str(cfg_path)] + _resolve_pipeline_args()
                print("\nDRY-RUN: would run pipeline:")
                print("  " + " ".join(cmd))
            else:
                print("\nDRY-RUN: pipeline auto-run is disabled (use --run-pipeline or set jenkins.triggers.auto_run_pipeline=true)")
            return 0

        def _notify_started(run_name: str, num: int, build_url: str) -> None:
            header = _build_project_header(cfg)
            text = f"{header} 启动编译：\n🔨 编译开始：{run_name} #{num}\n{build_url}"
            _maybe_notify_webhook_text(
                cfg=cfg,
                text=text,
                request_timeout_sec=int(args.request_timeout_sec),
            )
            _send_bot_notification(cfg=cfg, text=text)

        def _notify_finished(run_name: str, num: int, build_url: str, result: str) -> None:
            header = _build_project_header(cfg)
            emoji = "✅" if result.upper() == "SUCCESS" else "❌"
            text = f"{header} 编译结果：\n{emoji} 编译完成：{run_name} #{num} [{result}]\n{build_url}"
            _maybe_notify_webhook_text(
                cfg=cfg,
                text=text,
                request_timeout_sec=int(args.request_timeout_sec),
            )
            _send_bot_notification(cfg=cfg, text=text)

        builds: Dict[str, Tuple[int, str]] = {}
        results: Dict[str, str] = {}

        if bool(args.mock_build_success):
            rel_url = str(((cfg.get("jenkins") or {}).get("builds") or {}).get("release", {}).get("build_url") or "").strip()
            dbg_url = str(((cfg.get("jenkins") or {}).get("builds") or {}).get("debug", {}).get("build_url") or "").strip()
            builds = {
                "release": (0, rel_url),
                "debug": (0, dbg_url),
            }
            results = {
                "release": "SUCCESS",
                "debug": "SUCCESS",
            }
            print("\nMOCK: skipping Jenkins trigger/poll; assuming release=SUCCESS debug=SUCCESS")

            # In mock mode there is no polling loop, so explicitly notify once.
            hdr = _build_project_header(cfg)
            _maybe_notify_webhook_text(
                cfg=cfg,
                text=f"[{hdr}] Jenkins release finished (MOCK): result=SUCCESS {rel_url}",
                request_timeout_sec=int(args.request_timeout_sec),
            )
            _maybe_notify_webhook_text(
                cfg=cfg,
                text=f"[{hdr}] Jenkins debug finished (MOCK): result=SUCCESS {dbg_url}",
                request_timeout_sec=int(args.request_timeout_sec),
            )
            _send_bot_notification(cfg=cfg, text=f"✅ [{hdr}] 编译完成(MOCK)：release\n{rel_url}")
            _send_bot_notification(cfg=cfg, text=f"✅ [{hdr}] 编译完成(MOCK)：debug\n{dbg_url}")

        elif bool(args.use_existing_build_urls):
            # Use existing build URLs from config and poll until finished.
            if auth is None:
                auth = _get_auth_from_cfg(cfg)
            rel_url = str(((cfg.get("jenkins") or {}).get("builds") or {}).get("release", {}).get("build_url") or "").strip()
            dbg_url = str(((cfg.get("jenkins") or {}).get("builds") or {}).get("debug", {}).get("build_url") or "").strip()
            if not rel_url or not dbg_url:
                raise RuntimeError("--use-existing-build-urls requires jenkins.builds.release.build_url and jenkins.builds.debug.build_url in config")

            # Best-effort build numbers from URL suffix.
            def _url_to_num(u: str) -> int:
                parts = str(u or "").strip().rstrip("/").split("/")
                if parts and parts[-1].isdigit():
                    return int(parts[-1])
                return 0

            builds = {
                "release": (_url_to_num(rel_url), rel_url.rstrip("/") + "/"),
                "debug": (_url_to_num(dbg_url), dbg_url.rstrip("/") + "/"),
            }
            print("\n== Using existing build URLs from config ==")
            print(f"- release: {builds['release'][1]}")
            print(f"- debug:   {builds['debug'][1]}")

            # ── 并行流水线回调（use_existing_build_urls 路径） ──
            build_tscan_enabled_2 = str(cfg.get("vars", {}).get("build_tscan") or "").strip().lower() == "yes"
            def _on_existing_build_finished(run_name: str, num: int, build_url: str, result: str) -> None:
                _notify_finished(run_name, num, build_url, result)
                is_debug = run_name.endswith(".debug") or run_name == "debug"
                is_release = run_name.endswith(".release") or run_name == "release"
                if is_debug and result == "SUCCESS" and build_tscan_enabled_2:
                    # 后台触发 TSCAN
                    tscan_raw = _get_trigger_params(cfg, "Tscan", None)
                    if tscan_raw:
                        tscan_job_url = str(_get_triggers_cfg(cfg).get("job_url") or "").strip()
                        if not tscan_job_url:
                            existing_jobs = _get_job_urls_from_platforms(cfg)
                            tscan_job_url = list(existing_jobs.values())[0] if existing_jobs else ""
                        if tscan_job_url:
                            print(f"\n== Trigger TSCAN (build_tscan=yes) — debug 已完成 ==")
                            def _run_tscan_bg():
                                try:
                                    tscan_params = _expand_jenkins_params(raw_params=dict(tscan_raw), full_cfg=cfg)
                                    _tscan_adjust_version(tscan_params)
                                    q = _trigger_build(job_url=tscan_job_url, base_url=_get_jenkins_base_url(cfg),
                                        auth=auth, parameters=tscan_params, crumb=crumb,
                                        timeout_sec=int(args.request_timeout_sec), dry_run=False, opener=opener)
                                    tb = _poll_queues_for_builds(queues={"tscan": q}, auth=auth,
                                        poll_interval_sec=int(args.poll_interval_sec),
                                        queue_timeout_sec=int(args.queue_timeout_sec),
                                        request_timeout_sec=int(args.request_timeout_sec), opener=opener,
                                        on_build_started=_notify_started)
                                    tr = _poll_build_results(builds=tb, auth=auth,
                                        poll_interval_sec=int(args.poll_interval_sec),
                                        build_timeout_sec=int(args.build_timeout_sec),
                                        request_timeout_sec=int(args.request_timeout_sec), opener=opener,
                                        on_build_finished=_notify_finished)
                                    turl = str(tb.get("tscan", (0, ""))[1] or "")
                                    if turl and tr.get("tscan") == "SUCCESS":
                                        _download_tscan_artifacts(build_url=turl,
                                            out_dir=Path("work/download") / f"{project}_tscan",
                                            auth=auth, base_url=_get_jenkins_base_url(cfg),
                                            verify_tls=bool(not getattr(args, 'no_tls_verify', False)),
                                            request_timeout_sec=int(args.request_timeout_sec),
                                            opener=opener, project=project, cfg=cfg)
                                except Exception as e:
                                    print(f"\n[TSCAN-BG] ERROR: {e}", file=sys.stderr)
                            threading.Thread(target=_run_tscan_bg, daemon=True).start()
                if is_release and result == "SUCCESS":
                    try:
                        print("\n== Trigger CHANGELOG — release 已完成 ==")
                        _maybe_trigger_changelog_async(cfg_path=cfg_path, cfg=cfg, auth=auth,
                            crumb=crumb, request_timeout_sec=int(args.request_timeout_sec), opener=opener)
                    except Exception as e:
                        print(f"WARN: changelog trigger failed: {e}", file=sys.stderr)

            results = _poll_build_results(
                builds=builds,
                auth=auth,
                poll_interval_sec=int(args.poll_interval_sec),
                build_timeout_sec=int(args.build_timeout_sec),
                request_timeout_sec=int(args.request_timeout_sec),
                opener=opener,
                on_build_finished=_on_existing_build_finished,
            )

        else:
            if auth is None:
                auth = _get_auth_from_cfg(cfg)

            # Use queue_map if we built multiple platform jobs; otherwise fall back
            # to the legacy single-job variables.
            if queue_map:
                queues_to_poll = {k: v for k, v in queue_map.items() if str(v).strip()}
            else:
                queues_to_poll = {"release": q_release, "debug": q_debug}

            builds = _poll_queues_for_builds(
                queues=queues_to_poll,
                auth=auth,
                poll_interval_sec=int(args.poll_interval_sec),
                queue_timeout_sec=int(args.queue_timeout_sec),
                request_timeout_sec=int(args.request_timeout_sec),
                opener=opener,
                on_build_started=_notify_started,
            )

            print("\nBuilds started:")
            sys.stdout.flush()
            for run_name, (num, url) in builds.items():
                print(f"- {run_name}: #{num} {url}")
            sys.stdout.flush()

            # ── 并行流水线：构建回调中释放 changelog ──
            # release 完成 → 触发 changelog（TSCAN 已与 release/debug 并行触发）

            def _on_build_finished_parallel(run_name: str, num: int, build_url: str, result: str) -> None:
                """单 build 完成回调：release → changelog"""
                _notify_finished(run_name, num, build_url, result)

                # 检查是否是 release 构建完成 → 触发 changelog
                is_release = run_name.endswith(".release") or run_name == "release"
                if is_release and result == "SUCCESS":
                    try:
                        print(f"\n== Trigger CHANGELOG — release 已完成，立即触发 ==")
                        sys.stdout.flush()
                        _maybe_trigger_changelog_async(
                            cfg_path=cfg_path,
                            cfg=cfg,
                            auth=auth,
                            crumb=crumb,
                            request_timeout_sec=int(args.request_timeout_sec),
                            opener=opener,
                        )
                    except Exception as e:
                        print(f"WARN: failed to start async changelog trigger: {e}", file=sys.stderr)

            results = _poll_build_results(
                builds=builds,
                auth=auth,
                poll_interval_sec=int(args.poll_interval_sec),
                build_timeout_sec=int(args.build_timeout_sec),
                request_timeout_sec=int(args.request_timeout_sec),
                opener=opener,
                on_build_finished=_on_build_finished_parallel,
            )

        print("\nBuilds finished:")
        for run_name in sorted(builds.keys()):
            build_url = str(builds.get(run_name, (0, ""))[1] or "")
            print(f"- {run_name}: {build_url} result={results.get(run_name, '')}")
        sys.stdout.flush()

        # Persist build URLs back into config.
        # For single default job keep legacy structure jenkins.builds.release/debug.
        wrote_any = False
        for run_name, (num, build_url) in builds.items():
            if not str(build_url).strip():
                continue
            # run_name format expected: '<job_key>.release' or '<job_key>.debug' or simply 'release'
            if "." in run_name:
                job_key, run_type = run_name.rsplit(".", 1)
                # Map default job_key to legacy keys
                if job_key == "default":
                    _set_by_path(cfg, f"jenkins.builds.{run_type}.build_url", str(build_url))
                else:
                    # write to jenkins.builds.<job_key>.<run_type>.build_url
                    _set_by_path(cfg, f"jenkins.builds.{job_key}.{run_type}.build_url", str(build_url))
            else:
                # legacy single keys
                _set_by_path(cfg, f"jenkins.builds.{run_name}.build_url", str(build_url))
            wrote_any = True

        if wrote_any:
            _atomic_write_json(cfg_path, cfg, backup=not bool(args.no_backup))

        # Verify all triggered builds succeeded
        failed = [r for r, v in results.items() if v != "SUCCESS"]
        if failed:
            if auto_run_pipeline:
                print("\nPipeline was NOT started because Jenkins results are not all SUCCESS.")
            raise RuntimeError(f"Build results not all SUCCESS: {failed}")

        print("\nUpdated config build URLs:")
        for run_name, (num, build_url) in builds.items():
            print(f"- {run_name}: {build_url} (# {int(num)})")

        # ── 下载 TSCAN 产物（TSCAN 已与 release/debug 并行构建完成）──
        tscan_build_url = ""
        tscan_result_val = ""
        for run_name, (num, build_url) in builds.items():
            if run_name.endswith(".tscan") or run_name == "tscan":
                tscan_build_url = str(build_url or "")
                tscan_result_val = str(results.get(run_name, ""))
                break
        if tscan_build_url and tscan_result_val == "SUCCESS":
            print(f"\n[TSCAN:DOWNLOAD] 开始下载 TSCAN 产物: {tscan_build_url}")
            sys.stdout.flush()
            try:
                _download_tscan_artifacts(
                    build_url=tscan_build_url,
                    out_dir=Path("work/download") / f"{project}_tscan",
                    auth=auth,
                    base_url=_get_jenkins_base_url(cfg),
                    verify_tls=bool(not getattr(args, 'no_tls_verify', False)),
                    request_timeout_sec=int(args.request_timeout_sec),
                    opener=opener,
                    project=project,
                    cfg=cfg,
                )
                _atomic_write_json(cfg_path, cfg, backup=False)
                print(f"\n[TSCAN:COMPLETE] TSCAN 产物已下载，config 已更新")
            except Exception as e:
                print(f"\n[TSCAN:DOWNLOAD] WARN: 下载失败: {e}", file=sys.stderr)
        sys.stdout.flush()

        if auto_run_pipeline:
            script = _resolve_pipeline_script()
            subcmd = _resolve_pipeline_subcommand()
            cmd = [sys.executable, script] + ([subcmd] if subcmd else []) + ["--config", str(cfg_path)] + _resolve_pipeline_args()
            print("\n== Run PIPELINE ==")
            print(" ".join(cmd))

            hdr = _build_project_header(cfg)
            _maybe_notify_webhook_text(
                cfg=cfg,
                text=f"[{hdr}] Release pipeline starting: " + " ".join(cmd),
                request_timeout_sec=int(args.request_timeout_sec),
            )
            _send_bot_notification(cfg=cfg, text=f"🚀 [{hdr}] 流水线启动")

            p = subprocess.run(cmd)
            if int(p.returncode) != 0:
                _maybe_notify_webhook_text(
                    cfg=cfg,
                    text=f"[{hdr}] Release pipeline failed: exit_code={int(p.returncode)}",
                    request_timeout_sec=int(args.request_timeout_sec),
                )
                _send_bot_notification(cfg=cfg, text=f"❌ [{hdr}] 流水线失败: exit_code={int(p.returncode)}")
                raise RuntimeError(f"release_pipeline_run.py failed with exit code {p.returncode}")

            _maybe_notify_webhook_text(
                cfg=cfg,
                text=f"[{hdr}] Release pipeline finished: exit_code=0",
                request_timeout_sec=int(args.request_timeout_sec),
            )
        else:
            print("\nPipeline auto-run is disabled; not running release pipeline.")

    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 3

    return 0


if __name__ == "__main__":
    from credential_resolver import load_dotenv
    load_dotenv()
    raise SystemExit(main())
