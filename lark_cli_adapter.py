#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飞书 CLI (Lark CLI) 适配层 v2
=============================
lark-cli v1.0.27 命令映射:
  docs +create --title "..." --markdown @file
  docs +fetch --doc <url> --format pretty
  docs +update --doc <url> --markdown @file --mode overwrite
  wiki nodes copy --params '{...}' --data '{...}' --as user
  im +messages-send --chat-id ... --markdown "..."
  base +record-upsert --base-token ... --table-id ... --json '{...}'
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


# ── 配置 ──
LARK_CLI_BIN = os.environ.get("LARK_CLI_BIN", "lark-cli")
LARK_TIMEOUT_SEC = int(os.environ.get("LARK_CLI_TIMEOUT", "120"))
FEISHU_ADMIN_EMAIL = os.environ.get("FEISHU_ADMIN_EMAIL", "cs-guoqifa@zepp.com")
_auth_checked = False
_auth_ok = False
_auth_last_check = 0.0       # 上次检查时间戳，用于失败后允许重试
_AUTH_CACHE_TTL = 300         # 成功后缓存 5 分钟
_AUTH_RETRY_INTERVAL = 30     # 失败后 30 秒允许重试
_binary_checked = False
_binary_ok = False


class LarkCliError(RuntimeError):
    def __init__(self, msg, **kw):
        super().__init__(msg)
        for k, v in kw.items():
            setattr(self, k, v)


# ── 底层 ──

def _find_lark_bin() -> str:
    if os.name == "nt":
        import shutil
        f = shutil.which(LARK_CLI_BIN + ".cmd") or shutil.which(LARK_CLI_BIN)
        if f:
            print(f"[lark-cli] found via PATH: {f}", file=sys.stderr)
            return f
        # Fallback: search common npm global install locations
        # (subprocess may not inherit full user PATH)
        candidates = [
            os.path.join(os.environ.get("APPDATA", ""), "npm", LARK_CLI_BIN + ".cmd"),
            os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "npm", LARK_CLI_BIN + ".cmd"),
        ]
        for c in candidates:
            if os.path.isfile(c):
                print(f"[lark-cli] found at: {c}", file=sys.stderr)
                return c
        # 未找到：打印诊断信息（PATH 未命中 + APPDATA/home 实际值），便于定位"时有时无"
        print(
            f"[lark-cli] NOT found (PATH miss; APPDATA={os.environ.get('APPDATA', '')!r}, "
            f"home={os.path.expanduser('~')!r}) -> fallback to '{LARK_CLI_BIN}.cmd' (将触发 '未安装')",
            file=sys.stderr,
        )
        return LARK_CLI_BIN + ".cmd"
    return LARK_CLI_BIN

_LARK = _find_lark_bin()


def _run(args: List[str], timeout_sec: int = LARK_TIMEOUT_SEC,
         input_text: Optional[str] = None) -> subprocess.CompletedProcess:
    use_shell = os.name == "nt"
    full_args = [_LARK] + args
    if use_shell:
        # list2cmdline 不转义 |，需手动处理
        cmd = ""
        for a in full_args:
            if cmd:
                cmd += " "
            # 包含 | 但不含空格的参数需要加引号
            if "|" in a and " " not in a:
                cmd += '"' + a + '"'
            elif " " in a:
                cmd += '"' + a.replace('"', '\\"') + '"'
            else:
                cmd += a
    else:
        cmd = full_args
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_sec,
            input=input_text, encoding="utf-8", errors="replace", shell=use_shell,
        )
        err_text = (r.stderr or "").strip()
        out_text = (r.stdout or "").strip()
        if r.returncode != 0:
            msg = err_text or out_text
            raise LarkCliError(f"lark-cli 失败: {msg[:400]}")
        # str_replace 可能 ok=true 但 result=failed
        if out_text.startswith("{"):
            try:
                d = json.loads(out_text)
                if d.get("ok") is False:
                    raise LarkCliError(f"lark-cli 错误: {out_text[:400]}")
                if (d.get("data") or {}).get("result") == "failed":
                    raise LarkCliError(f"lark-cli str_replace 失败: {out_text[:400]}")
            except json.JSONDecodeError:
                pass
        return r
    except FileNotFoundError:
        raise LarkCliError("lark-cli 未安装: npx @larksuite/cli@latest install")
    except subprocess.TimeoutExpired:
        raise LarkCliError(f"lark-cli 超时 ({timeout_sec}s)")


def _check_bin() -> bool:
    global _binary_checked, _binary_ok
    if _binary_checked:
        return _binary_ok
    _binary_checked = True
    try:
        _run(["--version"], timeout_sec=5)
        _binary_ok = True
    except Exception:
        _binary_ok = False
    return _binary_ok


# ── 认证 ──

def lark_auth_ensure(*, domain: Optional[str] = None, silent: bool = False) -> bool:
    """检查 lark-cli 认证状态（带重试，失败允许后续恢复）"""
    global _auth_checked, _auth_ok, _auth_last_check
    import time as _time

    now = _time.time()

    # 成功缓存：短时间内直接复用
    if _auth_checked and _auth_ok and (now - _auth_last_check) < _AUTH_CACHE_TTL:
        return True

    # 失败缓存：超过间隔后允许重试（解决间歇性 auth 问题）
    if _auth_checked and not _auth_ok and (now - _auth_last_check) < _AUTH_RETRY_INTERVAL:
        return False

    _auth_checked = True
    _auth_last_check = now

    if not _check_bin():
        if not silent:
            print("\n⚠️  lark-cli 未安装: npx @larksuite/cli@latest install\n", file=sys.stderr)
        _auth_ok = False
        return False

    # 重试 auth status，最多 3 次，间隔 2 秒（应对网络抖动）
    last_err = None
    for attempt in range(3):
        try:
            _run(["auth", "status"], timeout_sec=10)
            _auth_ok = True
            return True
        except LarkCliError as e:
            last_err = e
            if attempt < 2:
                _time.sleep(2)

    # 三次重试都失败，尝试用 lark-cli auth login 自动恢复（需要已登录过的 profile）
    if not silent:
        print(f"\n⚠️  lark-cli auth status 失败 (重试3次): {last_err}", file=sys.stderr)
        print("  尝试自动恢复登录...", file=sys.stderr)
    try:
        login_args = ["auth", "login"]
        if domain:
            login_args += ["--domain", domain]
        _run(login_args, timeout_sec=30)
        _auth_ok = True
        if not silent:
            print("  ✓ lark-cli 自动恢复成功", file=sys.stderr)
        return True
    except LarkCliError:
        if not silent:
            domain_hint = f" --domain {domain}" if domain else ""
            print(f"\n⚠️  lark-cli 认证失败，请手动执行:\n  lark-cli auth login{domain_hint}\n", file=sys.stderr)
        _auth_ok = False
        return False


# ── 文档 ──

def _feishu_domain() -> str:
    return os.environ.get("FEISHU_DOMAIN", "zepp.feishu.cn")


def lark_create_doc_from_markdown(
    *, title: str, markdown_content: str, folder_token: str = "",
    admin_email: str = "",
) -> Dict[str, str]:
    """
    从 Markdown 创建飞书文档。用 bot 身份创建，然后自动给 admin_email 授权。
    如果 admin_email 为空，跳过授权步骤（用户需手动申请权限）。
    """
    if not lark_auth_ensure(domain="docs", silent=True):
        raise LarkCliError("未授权 docs")
    # bot 身份创建：用户认证未审批时，bot 是唯一可用身份
    args = ["docs", "+create", "--title", title, "--markdown", "-"]
    if folder_token:
        args += ["--folder-token", folder_token]
    r = _run(args, timeout_sec=60, input_text=markdown_content)
    out = (r.stdout or "").strip()
    url = ""
    doc_id = ""
    try:
        d = json.loads(out)
        data = d.get("data", d)
        doc_id = data.get("doc_id", "")
        url = data.get("doc_url", data.get("url", ""))
    except Exception:
        pass
    if not url and out:
        for line in out.split("\n"):
            m = re.search(r'(?:https?://[^\s"]+docx/[^\s"]+)', line)
            if m:
                url = m.group(0).rstrip('"').rstrip(",")
                break
    if url.startswith("{") or not url.startswith("http"):
        url = ""

    # 修正域名：lark-cli 返回 www.feishu.cn，替换为企业域名
    domain = _feishu_domain()
    if url and "www.feishu.cn" in url and domain != "www.feishu.cn":
        url = url.replace("www.feishu.cn", domain)

    # 自动授权：给 admin_email 添加完整权限
    if doc_id and admin_email:
        try:
            lark_grant_doc_permission(doc_token=doc_id, email=admin_email, perm="full_access")
            print(f"  已授权: {admin_email}")
        except LarkCliError as e:
            print(f"  授权失败: {e}", file=sys.stderr)

    return {"url": url or "", "doc_id": doc_id, "raw": out}


def lark_grant_doc_permission(
    *, doc_token: str, email: str, perm: str = "full_access",
) -> bool:
    """
    给飞书文档授权指定用户（通过 email）。
    需要 drive 域授权: lark-cli auth login --domain drive
    """
    if not lark_auth_ensure(domain="drive", silent=True):
        raise LarkCliError("未授权 drive")
    _run([
        "drive", "permission.members", "create",
        "--params", json.dumps({"token": doc_token, "type": "docx"}, ensure_ascii=False),
        "--data", json.dumps({"member_type": "email", "member_id": email, "perm": perm}, ensure_ascii=False),
        "--yes",
    ], timeout_sec=30)
    return True


def lark_update_doc_markdown(*, doc_url: str, markdown_content: str, mode: str = "overwrite") -> bool:
    if not lark_auth_ensure(domain="docs", silent=True):
        raise LarkCliError("未授权 docs")
    _run(["docs", "+update", "--api-version", "v2",
          "--doc", doc_url, "--command", mode, "--doc-format", "markdown",
          "--content", "-"],
         timeout_sec=60, input_text=markdown_content)
    return True


def lark_fetch_doc(*, doc_url: str) -> str:
    """读取飞书文档/docx/知识库内容，自动适配 wiki URL"""
    if "wiki" in doc_url:
        return lark_fetch_wiki(doc_url=doc_url)
    if not lark_auth_ensure(domain="docs", silent=True):
        raise LarkCliError("未授权 docs")
    r = _run(["docs", "+fetch", "--api-version", "v1", "--doc", doc_url, "--format", "pretty"], timeout_sec=30)
    return r.stdout or ""


def lark_fetch_wiki(*, doc_url: str) -> str:
    """通过飞书 API 读取 Wiki 页面内容"""
    token = _get_tenant_token()
    # 从 URL 提取 wiki token：https://xxx.feishu.cn/wiki/{token}
    import re as _re
    m = _re.search(r'/wiki/([A-Za-z0-9]+)', doc_url)
    if not m:
        raise LarkCliError(f"无法从 URL 提取 wiki token: {doc_url}")
    wiki_token = m.group(1)
    # 获取节点信息（含空间 ID）
    req = urllib.request.Request(
        f"https://open.feishu.cn/open-apis/wiki/v2/spaces/get_node?token={wiki_token}",
        headers={"Authorization": f"Bearer {token}"}
    )
    info = json.loads(urllib.request.urlopen(req, timeout=10).read())
    node = info.get("data", {}).get("node", {})
    obj_type = node.get("obj_type", "")
    node_token = node.get("node_token", "")
    space_id = node.get("space_id", "")

    if obj_type == "doc":
        # 复用 lark-cli docs +fetch
        obj_token = node.get("obj_token", "")
        doc_url2 = f"https://zepp.feishu.cn/docx/{obj_token}"
        return lark_fetch_doc(doc_url=doc_url2)
    else:
        # 纯 wiki 节点：读 blocks 内容
        req2 = urllib.request.Request(
            f"https://open.feishu.cn/open-apis/wiki/v2/spaces/{space_id}/nodes/{node_token}",
            headers={"Authorization": f"Bearer {token}"}
        )
        data = json.loads(urllib.request.urlopen(req2, timeout=10).read())
        # 从返回的 blocks 提取文本
        blocks = data.get("data", {}).get("node", {}).get("blocks", [])
        lines = []
        for b in blocks:
            text = b.get("text", "")
            if isinstance(text, dict):
                text = " ".join(str(v) for v in text.values() if v)
            if text:
                lines.append(str(text))
        return "\n".join(lines)


# ═══ Wiki ═══
# lark-cli wiki nodes copy 在处理大整数 space_id 时有 JSON 精度 bug
# 此处用原生 urllib HTTP 绕过

import urllib.request
import urllib.error as _urllib_error


def _get_tenant_token(*, oauth_cfg: Optional[Dict[str, str]] = None) -> str:
    """获取 tenant_access_token。
    优先级: 环境变量 FEISHU_APP_ID/FEISHU_APP_SECRET > 配置文件 feishu.oauth"""
    import os as _os
    app_id = _os.environ.get("FEISHU_APP_ID", "").strip()
    app_secret = _os.environ.get("FEISHU_APP_SECRET", "").strip()
    # 环境变量未设置时，各自独立地从配置文件回退（避免 app_id 缺失时连带覆盖 app_secret）
    if oauth_cfg and isinstance(oauth_cfg, dict):
        if not app_id:
            app_id = str(oauth_cfg.get("app_id") or "").strip()
        if not app_secret:
            app_secret = str(oauth_cfg.get("app_secret") or "").strip()
    # 环境变量与配置均未提供时，直接报错，不再回退到硬编码默认值（避免凭证泄露）
    if not app_id or not app_secret:
        raise LarkCliError("Missing FEISHU_APP_ID/FEISHU_APP_SECRET (set env or config feishu.oauth)")
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    req = urllib.request.Request(url, method="POST",
        data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode(),
        headers={"Content-Type": "application/json"})
    resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
    if resp.get("code") != 0:
        raise LarkCliError(f"获取 tenant_token 失败: {resp}")
    return resp["tenant_access_token"]


def _http_post_json(url: str, body: dict, token: str) -> dict:
    """HTTP POST JSON 请求"""
    req = urllib.request.Request(url, method="POST",
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer %s" % token, "Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        return json.loads(resp.read())
    except _urllib_error.HTTPError as e:
        return json.loads(e.read())


def _get_wiki_user_token() -> str:
    """获取有权访问 wiki 的 token。优先用 lark-cli 的 user token。"""
    # 尝试 lark-cli 中已登录的 user token（--as user）
    try:
        r = _run(["auth", "status"], timeout_sec=5)
        status = json.loads(r.stdout or "{}")
        if status.get("identity") == "user" and status.get("users"):
            # 有用户已登录，用 lark-cli 命令发请求
            return "__LARK_CLI_USER__"  # 特殊标记
    except Exception:
        pass
    return _get_tenant_token()


def lark_wiki_copy_node(
    *, source_node_token: str, target_space_id: str,
    target_parent_token: str = "", new_title: str = "",
    feishu_oauth_cfg: Optional[Dict[str, str]] = None,
) -> dict:
    """复制 Wiki 节点。
    优先用 lark-cli --as user（保留用户身份）；auth 失败时自动降级为
    tenant_access_token HTTP 直连（解决间歇性 "未授权 wiki" 问题）。
    feishu_oauth_cfg: feishu.oauth 配置，用于 tenant_token 获取时的凭证回退"""
    # 先尝试 lark-cli（带 user 身份）
    if lark_auth_ensure(domain="wiki", silent=True):
        try:
            params = {"space_id": str(target_space_id), "node_token": str(source_node_token)}
            body = {}
            if target_space_id:
                body["target_space_id"] = str(target_space_id)
            if target_parent_token:
                body["target_parent_token"] = str(target_parent_token)
            if new_title:
                body["title"] = str(new_title)
            r = _run([
                "wiki", "nodes", "copy", "--as", "user",
                "--params", json.dumps(params, ensure_ascii=False),
                "--data", json.dumps(body, ensure_ascii=False),
            ], timeout_sec=60)
            out = (r.stdout or "").strip()
            nt = dt = url = ""
            try:
                d = json.loads(out)
                node = d.get("node", d.get("data", {}).get("node", {}))
                nt = node.get("node_token", "")
                dt = node.get("obj_token", "")
                if nt:
                    url = "https://%s/wiki/%s" % (_feishu_domain(), nt)
            except Exception:
                pass
            if nt:
                return {"node_token": nt, "url": url, "docx_token": dt}
        except LarkCliError as e:
            print(f"  Wiki (lark-cli) 失败: {e}，尝试 HTTP fallback ...", file=sys.stderr)

    # HTTP fallback: 用 tenant_access_token 直连飞书 API
    print("  Wiki: 切换到 tenant_access_token HTTP 模式 ...", file=sys.stderr)
    try:
        token = _get_tenant_token(oauth_cfg=feishu_oauth_cfg)
        # 校验 space_id 有效性
        _sid = str(target_space_id or "").strip()
        if not _sid or not _sid.isdigit():
            raise LarkCliError(
                "Wiki HTTP fallback: target_space_id 无效 (%s)，请检查配置文件 feishu.target_space_id" % repr(_sid))
        api_url = "https://open.feishu.cn/open-apis/wiki/v2/spaces/%s/nodes/%s/copy" % (
            _sid, source_node_token)
        body: Dict[str, str] = {}
        if _sid:
            body["target_space_id"] = _sid
        if target_parent_token:
            body["target_parent_token"] = str(target_parent_token).strip()
        if new_title:
            body["title"] = new_title
        print("  Wiki HTTP: space_id=%s node_token=%s body=%s" % (
            _sid, source_node_token, json.dumps(body, ensure_ascii=False)), file=sys.stderr)
        resp = _http_post_json(api_url, body, token)
        if resp.get("code") != 0:
            raise LarkCliError("Wiki HTTP fallback failed: code=%s msg=%s" % (
                resp.get("code"), resp.get("msg", "")))
        node = resp.get("data", {}).get("node", {})
        nt = node.get("node_token", "")
        dt = node.get("obj_token", "")
        wiki_url = "https://%s/wiki/%s" % (_feishu_domain(), nt) if nt else ""
        print(f"  Wiki (HTTP): {wiki_url}", file=sys.stderr)
        return {"node_token": nt, "url": wiki_url, "docx_token": dt}
    except Exception as e:
        raise LarkCliError("未授权 wiki（lark-cli 和 HTTP 均失败）: %s" % str(e)[:200])


# ── 消息 ──

def lark_send_message(*, chat_id: str = "", markdown: str = "", silent: bool = False) -> bool:
    ok = lark_auth_ensure(domain="im", silent=silent or True)
    if not ok:
        msg = "WARN: lark-cli IM 未授权，请执行 lark-cli auth login --domain im"
        if not silent:
            print(msg, file=sys.stderr)
        return False
    args = ["im", "+messages-send", "--markdown", markdown]
    if chat_id:
        args += ["--chat-id", chat_id]
    try:
        _run(args, timeout_sec=15); return True
    except LarkCliError as e:
        msg = f"WARN: 消息发送失败: {e}"
        if not silent:
            print(msg, file=sys.stderr)
        return False


# ── 多维表格 ──

def lark_base_upsert_record(*, base_token: str, table_id: str, record: Dict[str, Any]) -> bool:
    if not lark_auth_ensure(domain="base", silent=True):
        print("WARN: 多维表格未授权", file=sys.stderr); return False
    try:
        _run([
            "base", "+record-upsert",
            "--base-token", base_token, "--table-id", table_id,
            "--json", json.dumps(record, ensure_ascii=False),
        ], timeout_sec=30)
        return True
    except LarkCliError as e:
        print(f"WARN: 多维表格写入失败: {e}", file=sys.stderr); return False


# ── 一键发布 ──

@dataclass
class ReleaseDocResult:
    doc_url: str = ""
    wiki_url: str = ""
    base_record_added: bool = False
    notification_sent: bool = False
    def to_status_text(self) -> str:
        parts = []
        if self.doc_url: parts.append(f"Doc: {self.doc_url}")
        if self.wiki_url: parts.append(f"Wiki: {self.wiki_url}")
        return " | ".join(parts) if parts else "未生成"


def lark_publish_release_document(
    *, title: str, markdown_content: str,
    placeholder_map: Optional[Dict[str, str]] = None,
    wiki_config: Optional[Dict[str, str]] = None,
    base_config: Optional[Dict[str, Any]] = None,
    notification_chat_id: Optional[str] = None,
    notification_title: Optional[str] = None,
    feishu_oauth_cfg: Optional[Dict[str, str]] = None,
    dry_run: bool = False,
) -> ReleaseDocResult:
    """
    一键发布飞书版本文档。
    两种模式：
    - wiki_config 非空 → 复制模板+替换占位符（不创建新文档）
    - wiki_config 为空 → 从 Markdown 创建新文档
    feishu_oauth_cfg: feishu.oauth 配置，用于 Wiki 复制时的 tenant_token 凭证回退
    """
    res = ReleaseDocResult()
    if dry_run:
        print(f"\n飞书 CLI (dry-run): {title} ({len(markdown_content)} 字符)")
        return res

    docx_token = ""  # 用于占位符替换的 docx token

    # ── 模式1: Wiki 模板复制 ──
    if wiki_config:
        print(f"\n飞书 CLI: 复制 Wiki 模板...")
        try:
            wr = lark_wiki_copy_node(
                source_node_token=wiki_config["template_node_token"],
                target_space_id=wiki_config["target_space_id"],
                target_parent_token=wiki_config.get("target_parent_token", ""),
                new_title=title,
                feishu_oauth_cfg=feishu_oauth_cfg,
            )
            wiki_url = wr.get("url", "")
            node_token = wr.get("node_token", "")
            docx_token = wr.get("docx_token", "")
            if wiki_url:
                res.wiki_url = wiki_url
                print(f"  Wiki: {wiki_url}")
            if docx_token:
                print(f"  Docx: {docx_token}")
        except LarkCliError as e:
            print(f"  Wiki 失败: {e}", file=sys.stderr)

    # ── 占位符替换（Wiki 模板模式） ──
    if placeholder_map and docx_token:
        print(f"飞书 CLI: 替换文档占位符 ({len(placeholder_map)} 个)...")
        try:
            _replace_placeholders_via_lark(docx_token, placeholder_map)
        except LarkCliError as e:
            print(f"  占位符替换失败: {e}", file=sys.stderr)
    elif placeholder_map and res.doc_url:
        # 新建文档模式下的占位符替换
        print(f"飞书 CLI: 替换文档占位符 ({len(placeholder_map)} 个)...")
        try:
            _replace_placeholders_via_lark(res.doc_url, placeholder_map)
        except LarkCliError as e:
            print(f"  占位符替换失败: {e}", file=sys.stderr)

    # ── 模式2: 新建文档（无 Wiki 模板时） ──
    if not wiki_config:
        print(f"\n飞书 CLI: 创建文档 '{title}' ...")
        try:
            r = lark_create_doc_from_markdown(
                title=title, markdown_content=markdown_content,
                admin_email=FEISHU_ADMIN_EMAIL,
            )
            res.doc_url = r.get("url", "")
            if res.doc_url:
                print(f"  文档: {res.doc_url}")
            # 新建模式也需要做占位符替换
            if placeholder_map and res.doc_url and not wiki_config:
                try:
                    _replace_placeholders_via_lark(res.doc_url, placeholder_map)
                except LarkCliError as e:
                    print(f"  占位符替换失败: {e}", file=sys.stderr)
        except LarkCliError as e:
            print(f"  创建失败: {e}", file=sys.stderr)

    # ── 多维表格 ──
    if base_config:
        rec = dict(base_config.get("record", {}))
        if res.doc_url:
            rec.setdefault("飞书文档", res.doc_url)
        elif res.wiki_url:
            rec.setdefault("飞书文档", res.wiki_url)
        try:
            res.base_record_added = lark_base_upsert_record(
                base_token=base_config["base_token"],
                table_id=base_config["table_id"],
                record=rec,
            )
            if res.base_record_added:
                print("  已追加多维表格记录")
        except LarkCliError as e:
            print(f"  多维表格写入失败: {e}", file=sys.stderr)

    # ── 通知（可选，失败不抛异常） ──
    if notification_chat_id:
        target_url = res.wiki_url or res.doc_url
        if target_url:
            try:
                msg = notification_title or f"版本发布: {title}"
                res.notification_sent = lark_send_message(chat_id=notification_chat_id, markdown=f"{msg}\n{target_url}", silent=True)
            except Exception:
                pass  # 个人 IM 通知失败不影响主流程，webhook 已处理主通知

    return res


def _replace_placeholders_via_lark(doc_token: str, mapping: Dict[str, str]) -> None:
    """str_replace 纯文本替换 + block API 加超链接。
    模板格式100%保留，文件名变为可点击 NAS 链接。"""
    import secrets
    replaced = 0
    skipped = 0
    link_items = []  # (filename, url)

    # Step 1: str_replace 替换为文件名（纯文本）
    for k, v in sorted(mapping.items(), key=lambda x: len(x[0]), reverse=True):
        val = str(v).strip()
        if not val:
            skipped += 1
            continue
        if "|" in val:
            fname, url = val.split("|", 1)
            fname, url = fname.strip(), url.strip()
            link_items.append((fname, url))
            val = fname
        fname = "_fs_" + secrets.token_hex(6) + ".txt"
        fp = os.path.join(os.getcwd(), fname)
        try:
            with open(fp, "w", encoding="utf-8") as f:
                f.write(val)
            _run([
                "docs", "+update", "--api-version", "v2",
                "--doc", doc_token,
                "--command", "str_replace",
                "--pattern", k,
                "--content", "@" + fname,
            ], timeout_sec=20)
            replaced += 1
        except LarkCliError:
            skipped += 1
        finally:
            os.unlink(fp)

    # Step 2: block API 给文件名加超链接
    if link_items:
        _add_links(doc_token, link_items)

    print(f"  已替换 {replaced} 个占位符（{skipped} 个跳过）")
    _verify_replacement(doc_token, mapping)


def _add_links(doc_token: str, items: list) -> None:
    """通过 lark-cli api 给文件名加超链接（PATCH 每个 block）"""
    import json as _json

    # 1. 获取所有 blocks
    blocks: list = []
    page_token = ""
    while True:
        args = ["api", "GET", "/open-apis/docx/v1/documents/%s/blocks" % doc_token,
                "--params", _json.dumps({"page_size": 500, "page_token": page_token}),
                "--as", "user"]
        r = _run(args, timeout_sec=30)
        d = _json.loads(r.stdout)
        items_page = (d.get("data") or {}).get("items") or []
        blocks.extend(items_page)
        page_token = (d.get("data") or {}).get("page_token") or ""
        if not page_token or not items_page:
            break

    link_map = {fname: url for fname, url in items}
    linked = 0

    for b in blocks:
        bid = b.get("block_id", "")
        text = b.get("text")
        if not isinstance(text, dict):
            continue
        els = text.get("elements") or []
        new_els = []
        changed = False
        for el in els:
            if not isinstance(el, dict) or "text_run" not in el:
                new_els.append(el)
                continue
            tr = el["text_run"]
            content = str(tr.get("content", "")).strip()
            style = dict(tr.get("text_element_style") or {})
            if content in link_map and "link" not in style:
                style["link"] = {"url": link_map[content]}
                changed = True
            nr = {"content": tr.get("content", "")}
            if style:
                nr["text_element_style"] = style
            new_els.append({"text_run": nr})
        if not changed:
            continue
        body = _json.dumps({
            "update_text_elements": {
                "elements": new_els,
                "replace_type": "replace_all",
            }
        }, ensure_ascii=False)
        r = _run(["api", "PATCH",
                  "/open-apis/docx/v1/documents/%s/blocks/%s" % (doc_token, bid),
                  "--data", body, "--as", "user"], timeout_sec=15)
        d = _json.loads(r.stdout)
        if d.get("code") == 0:
            linked += 1

    if linked > 0:
        print("  link: %d filenames -> clickable NAS links" % linked)
    else:
        print("  WARN: link: no blocks updated", file=sys.stderr)


def _verify_replacement(doc_token: str, mapping: Dict[str, str]) -> None:
    """生成后自动验证：残留占位符 + 内容完整。只检查 mapping 中有值的 k。"""
    import re as _re, time as _time
    _time.sleep(2)
    try:
        content = lark_fetch_doc(doc_url=doc_token)
    except Exception:
        print("\n[VERIFY] 无法获取文档", file=sys.stderr)
        return

    placeholder_re = _re.compile(r'\{\{[^}]+\}\}')
    remaining = placeholder_re.findall(content)
    # 只看 mapping 中有值但未替换的
    non_empty_keys = {k for k, v in mapping.items() if str(v).strip()}
    real_remaining = [p for p in remaining if p in non_empty_keys]
    only_empty = [p for p in remaining if p not in non_empty_keys]

    content_len = len(content)
    print()
    print('=' * 50)
    print('[VERIFY] 飞书文档自动验证')
    print('=' * 50)
    print(f'  内容长度: {content_len} 字符')
    print(f'  待替换占位符: {len(real_remaining)} 个', end='')

    if real_remaining:
        print(' [FAIL]')
        for p in real_remaining[:5]:
            print(f'    - {p}')
        print(f'  结论: 部分占位符未替换 [FAIL]')
    elif content_len < 300:
        print(' [PASS]')
        print(f'  结论: 内容异常短 [FAIL]')
    else:
        print(' [PASS]')
        if only_empty:
            print(f'  无值跳过: {len(only_empty)} 个（如 debug 专属占位符）')
            for p in only_empty[:3]:
                print(f'    - {p}')
        print(f'  结论: 全部通过 [OK]')
    print('=' * 50)
