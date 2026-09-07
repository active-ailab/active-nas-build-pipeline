# -*- coding: utf-8 -*-
"""飞书多维表格「环节流水」通知。

每个流水线环节往多维表格追加一行，形成完整时间线：

    项目 | 版本 | 阶段 | 环节 | 状态 | 发布时间 | 飞书文档

设计要点
--------
* **只追加，不更新**：每个环节写独立一行，不做 search + update，
  避免并发/中断留下孤儿行。
* **绝不中断流水线**：任何异常都只往 stderr 打一行 WARN 并返回 False。
* **不依赖 lark-cli**：用 .env 的 app_id/app_secret 换 tenant_access_token 直写，
  lark-cli 未登录也能用。
* **零外部依赖**：自带 token 获取与缓存，两个入口
  （jenkins_trigger_build.py / release_pipeline_run.py）都能直接 import。

用法::

    from feishu_base_notifier import notify_stage
    notify_stage(cfg=cfg, stage="下载", status="成功")

配置来自 ``cfg["feishu"]["base"]``（与既有 feishu.base 段完全一致）::

    "base": {
      "enabled": true,
      "base_token": "...",
      "table_id": "tbl...",
      "fields": {"项目": "项目", "环节": "环节", "状态": "状态", ...}
    }
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Dict, Optional

# 系统内部字段 -> 多维表格默认列名
DEFAULT_FIELDS: Dict[str, str] = {
    "项目": "项目",
    "版本": "版本",
    "阶段": "阶段",
    "环节": "环节",
    "状态": "状态",
    "发布时间": "发布时间",
    "飞书文档": "飞书文档",
}

_TOKEN_CACHE: Dict[str, Any] = {"app_id": "", "token": "", "expires_at": 0}

# 去重：同一个 (环节, 状态) 组合在同一个进程里只写一次。
# 原因：release / debug 是两次独立编译，_notify_started/_notify_finished 会各触发一次，
# 但表里没有「构建名」列，两行内容完全一样 → 纯噪音。
# 「失败」不去重，保证每次失败都留痕。
_WRITTEN: set = set()


# ─────────────────────────────────────────────────────────────
# 凭据与 token
# ─────────────────────────────────────────────────────────────

def _resolve_credentials(feishu_cfg: Dict[str, Any]) -> tuple:
    """优先用配置里的 oauth.app_id/app_secret，回退到环境变量。"""
    oauth_cfg = feishu_cfg.get("oauth") if isinstance(feishu_cfg.get("oauth"), dict) else {}
    app_id = str(oauth_cfg.get("app_id") or "").strip() or str(os.environ.get("FEISHU_APP_ID") or "").strip()
    app_secret = str(oauth_cfg.get("app_secret") or "").strip() or str(os.environ.get("FEISHU_APP_SECRET") or "").strip()
    return app_id, app_secret


def _get_tenant_token(app_id: str, app_secret: str, timeout_sec: int = 10) -> str:
    """获取 tenant_access_token，带进程内缓存（提前 60s 过期）。"""
    now = int(time.time())
    if (
        _TOKEN_CACHE.get("app_id") == app_id
        and _TOKEN_CACHE.get("token")
        and int(_TOKEN_CACHE.get("expires_at") or 0) > now + 60
    ):
        return str(_TOKEN_CACHE["token"])

    body = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8")
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=body,
        method="POST",
    )
    req.add_header("Content-Type", "application/json; charset=utf-8")
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    if data.get("code") != 0:
        raise RuntimeError(f"tenant_access_token failed: code={data.get('code')} msg={data.get('msg')}")

    _TOKEN_CACHE["app_id"] = app_id
    _TOKEN_CACHE["token"] = str(data.get("tenant_access_token") or "")
    _TOKEN_CACHE["expires_at"] = now + int(data.get("expire") or 7200)
    return _TOKEN_CACHE["token"]


# ─────────────────────────────────────────────────────────────
# 配置解析
# ─────────────────────────────────────────────────────────────

def base_config_from_cfg(cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从 cfg 里解析多维表格配置；未启用或配置不全时返回 None。"""
    feishu_cfg = cfg.get("feishu") if isinstance(cfg.get("feishu"), dict) else {}
    base_cfg = feishu_cfg.get("base") if isinstance(feishu_cfg.get("base"), dict) else None
    if not base_cfg or not base_cfg.get("enabled"):
        return None

    base_token = str(base_cfg.get("base_token") or "").strip()
    table_id = str(base_cfg.get("table_id") or "").strip()
    if not base_token or not table_id:
        return None

    app_id, app_secret = _resolve_credentials(feishu_cfg)
    if not app_id or not app_secret:
        return None

    fields_map = base_cfg.get("fields") if isinstance(base_cfg.get("fields"), dict) else {}
    return {
        "base_token": base_token,
        "table_id": table_id,
        "app_id": app_id,
        "app_secret": app_secret,
        "fields": {k: str(fields_map.get(k) or v) for k, v in DEFAULT_FIELDS.items()},
    }


def build_record(
    *,
    cfg: Dict[str, Any],
    base_cfg: Dict[str, Any],
    stage: str,
    status: str,
    doc_url: str = "",
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """按字段映射构造一行记录。"""
    release_cfg = cfg.get("release") if isinstance(cfg.get("release"), dict) else {}
    f = base_cfg["fields"]
    when = now or datetime.now()
    return {
        f["项目"]: str(release_cfg.get("project") or ""),
        f["版本"]: str(release_cfg.get("version") or ""),
        f["阶段"]: str(release_cfg.get("stage") or ""),
        f["环节"]: str(stage or ""),
        f["状态"]: str(status or ""),
        f["发布时间"]: when.strftime("%Y-%m-%d %H:%M"),
        f["飞书文档"]: str(doc_url or ""),
    }


# ─────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────

def notify_stage(
    *,
    cfg: Dict[str, Any],
    stage: str,
    status: str,
    doc_url: str = "",
    now: Optional[datetime] = None,
    dedupe: bool = True,
) -> bool:
    """往多维表格追加一条「环节流水」记录。

    :param cfg: 已合并的完整配置（含 feishu.base 段）
    :param stage: 环节名，如 编译 / 打包流水线 / 下载 / 上传 / 分享 / 版本文档 / 飞书文档 / 发布完成
    :param status: 进行中 / 成功 / 失败 / 跳过
    :param doc_url: 飞书文档链接，仅最终环节需要
    :param dedupe: 同一 (环节, 状态) 是否只写一次（「失败」永远不去重）
    :return: 是否写入成功
    """
    _key = (str(stage or ""), str(status or ""))
    if dedupe and status != "失败" and _key in _WRITTEN:
        print(f"  [多维表格] {stage} → {status} （已记录，跳过重复）")
        return True
    try:
        base_cfg = base_config_from_cfg(cfg)
        if base_cfg is None:
            print(f"[Base] 跳过（未启用或配置不全）: 环节={stage} 状态={status}", file=sys.stderr)
            return False

        token = _get_tenant_token(base_cfg["app_id"], base_cfg["app_secret"])
        record = build_record(
            cfg=cfg, base_cfg=base_cfg, stage=stage, status=status, doc_url=doc_url, now=now,
        )

        url = (
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/{base_cfg['base_token']}"
            f"/tables/{base_cfg['table_id']}/records?user_id_type=open_id"
        )
        body = json.dumps({"fields": record}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", "application/json; charset=utf-8")
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))

        if data.get("code") == 0:
            rec = (data.get("data") or {}).get("record") or {}
            _WRITTEN.add(_key)
            print(f"  [多维表格] {stage} → {status} (record_id={str(rec.get('record_id', ''))[:16]})")
            return True

        print(
            f"WARN: [多维表格] 写入失败 环节={stage} code={data.get('code')} "
            f"msg={str(data.get('msg', 'unknown'))[:200]}",
            file=sys.stderr,
        )
        return False
    except Exception as e:  # 绝不中断流水线
        print(f"WARN: [多维表格] 写入异常 环节={stage}: {type(e).__name__}: {e}", file=sys.stderr)
        return False


def notify_stages_if_enabled(*, cfg: Dict[str, Any], events: list) -> int:
    """批量写入多个环节，events = [(stage, status), ...]。返回成功条数。"""
    ok = 0
    for item in events:
        stage, status = (list(item) + [""])[:2]
        if notify_stage(cfg=cfg, stage=stage, status=status):
            ok += 1
    return ok


if __name__ == "__main__":
    # 自检：python feishu_base_notifier.py [环节] [状态]
    import sys as _sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from config_loader import read_json_with_base  # noqa: E402
    from credential_resolver import load_dotenv  # noqa: E402

    load_dotenv()
    _cfg = read_json_with_base(Path("common.json"))
    _stage = _sys.argv[1] if len(_sys.argv) > 1 else "自检"
    _status = _sys.argv[2] if len(_sys.argv) > 2 else "成功"
    _ok = notify_stage(cfg=_cfg, stage=_stage, status=_status)
    print("写入成功" if _ok else "写入失败")
    _sys.exit(0 if _ok else 1)
