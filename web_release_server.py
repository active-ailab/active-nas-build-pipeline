#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
固件发布 Web 管理后台
====================
Flask 后端 + HTML 前端，提供：
- 项目下拉选择（自动从 gqf_*.json 发现）
- 历史版本自动填充（基于 .bak 文件）
- 表单字段可编辑
- 触发发布流水线
- 自动清理：bak 保留 10 个 / work/download 保留 5 个版本
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from collections import OrderedDict

import requests
from flask import Flask, Response, jsonify, render_template, request, send_from_directory, stream_with_context

# ── 配置 ──────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
BAK_KEEP_COUNT = 10        # 每个项目保留最近 10 个 bak
DOWNLOAD_KEEP_COUNT = 5    # 每个项目保留最近 5 个下载版本

app = Flask(__name__)
app.jinja_env.auto_reload = True
app.config['TEMPLATES_AUTO_RELOAD'] = True

# ── 运行中的任务追踪 ──────────────────────────────────
_running_tasks: Dict[str, Dict[str, Any]] = {}
_task_streams: Dict[str, queue.Queue] = {}  # SSE 实时日志流队列

# ── 任务持久化文件（防服务器重启丢失） ─────────────────
_RUNNING_TASKS_FILE = SCRIPT_DIR / "output" / "running_tasks.json"
_RUNNING_TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
_last_persist_ts = 0  # 节流时间戳

def _persist_running_tasks():
    """将 _running_tasks 中可序列化的字段写入 JSON 文件"""
    import time as _time
    serializable = {}
    for tid, t in _running_tasks.items():
        entry = {}
        for k in ("project", "version", "user_id", "task_type", "status", "started_at",
                  "feishu_link", "feishu_no_link_msg", "_total_percent", "_finished_at", "exit_code"
                  # [2026-06-02] source_step 前端不消费，注释；测试无影响后移除
                  # "source_step"
                  ):
            if k in t:
                entry[k] = t[k]
        if "steps" in t:
            entry["steps"] = t["steps"]
        # 日志只保留最近 100 行
        log = t.get("log", [])
        if log:
            entry["log"] = log[-100:]
        serializable[tid] = entry
    try:
        _RUNNING_TASKS_FILE.write_text(json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

def _restore_running_tasks():
    """从 JSON 文件恢复 _running_tasks，跳过已过期任务"""
    import time as _time
    if not _RUNNING_TASKS_FILE.exists():
        return
    try:
        data = json.loads(_RUNNING_TASKS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    import datetime as _dt
    cutoff = _time.time() - 1800  # 30 分钟
    for tid, entry in data.items():
        if not isinstance(entry, dict):
            continue
        status = entry.get("status", "")
        finished_at = entry.get("_finished_at", 0)
        # 已完成且超时的终端任务 → 跳过
        if status in ("success", "failed", "timeout", "error", "cancelled") and finished_at < cutoff:
            continue  # 跳过已过期
        # triggering 且无进度更新超过 10 分钟 → 跳过（卡死的任务）
        started_at_str = entry.get("started_at", "")
        if status in ("triggering",) and started_at_str:
            try:
                ts = _dt.datetime.fromisoformat(started_at_str).timestamp()
                if _time.time() - ts > 600:
                    continue  # 跳过超过 10 分钟的卡死任务
            except Exception:
                pass
        _running_tasks[tid] = {
            "project": entry.get("project", ""),
            "version": entry.get("version", ""),
            "user_id": entry.get("user_id", ""),
            "task_type": entry.get("task_type", "build"),
            "status": status or "finished",
            "started_at": entry.get("started_at", ""),
            "feishu_link": entry.get("feishu_link", ""),
            "feishu_no_link_msg": entry.get("feishu_no_link_msg", ""),
            "_total_percent": entry.get("_total_percent", 100 if status == "success" else 0),
            "_finished_at": finished_at,
            "exit_code": entry.get("exit_code"),
            # [2026-06-02] source_step 前端不消费，注释；测试无影响后移除
            # "source_step": entry.get("source_step", 7),
            "steps": entry.get("steps", []),
            "log": entry.get("log", []),
        }
    if _running_tasks:
        print(f"[restore] 恢复了 {len(_running_tasks)} 个任务")

# 启动时恢复
_restore_running_tasks()


def _cleanup_stale_tasks():
    """清理超过 30 分钟的已完成任务，防内存泄漏"""
    import time as _time
    cutoff = _time.time() - 1800  # 30 分钟
    stale = [tid for tid, t in list(_running_tasks.items())
             if t.get("status") in ("success", "failed", "timeout", "error", "cancelled")
             and t.get("_finished_at", 0) < cutoff]
    for tid in stale:
        _task_streams.pop(tid, None)
        _running_tasks.pop(tid, None)
    if stale:
        print(f"[cleanup] Removed {len(stale)} stale tasks")
        _persist_running_tasks()

# ── 数据埋点 ──────────────────────────────────────────
STATS_FILE = SCRIPT_DIR / "output" / "stats.json"
STATS_FILE.parent.mkdir(parents=True, exist_ok=True)

def _load_stats() -> dict:
    if STATS_FILE.exists():
        try:
            return json.loads(STATS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"events": []}

def _save_stats(stats: dict):
    STATS_FILE.write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def _track_event(event_type: str, operator: str = "", device: str = "", detail: str = ""):
    """记录一次操作事件"""
    import threading as _th
    def _do():
        stats = _load_stats()
        stats["events"].append({
            "type": event_type,
            "operator": operator or "未知",
            "device": device,
            "detail": detail,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        _save_stats(stats)
    _th.Thread(target=_do, daemon=True).start()

EVENT_TYPE_MAP = {
    "build": "编译版本",
    "tscan": "TSCAN 扫描",
    "skip_jenkins": "直接生成文档（跳过编译）",
    "changelog": "版本 Changelog 差分",
}

USER_TOKENS_DIR = SCRIPT_DIR / "user_tokens"
USER_TOKENS_DIR.mkdir(parents=True, exist_ok=True)


def _load_user_feishu_token(user_id: str) -> Optional[dict]:
    """加载用户的飞书 token，不存在返回 None"""
    if not user_id:
        return None
    tf = USER_TOKENS_DIR / f"{user_id}.json"
    if not tf.exists():
        return None
    try:
        return json.loads(tf.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_user_feishu_token(user_id: str, cfg: dict):
    """从 cfg 中提取飞书 token 保存到用户文件"""
    if not user_id:
        return
    feishu = cfg.get("feishu") or {}
    if not isinstance(feishu, dict):
        return
    token_data = {}
    for k in ("user_access_token", "user_refresh_token",
              "user_access_token_expires_at", "user_refresh_token_expires_at",
              "user_token_saved_at"):
        if feishu.get(k):
            token_data[k] = feishu[k]
    if not token_data.get("user_access_token"):
        return
    tf = USER_TOKENS_DIR / f"{user_id}.json"
    tf.write_text(json.dumps(token_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _inject_user_feishu_token(cfg: dict, user_id: str):
    """将用户的飞书 token 注入到 cfg 中"""
    token_data = _load_user_feishu_token(user_id)
    if not token_data:
        return
    cfg.setdefault("feishu", {})
    for k, v in token_data.items():
        if v:
            cfg["feishu"][k] = v


# ══════════════════════════════════════════════════════
# 任务权限：按 user_id 做数据隔离
# ══════════════════════════════════════════════════════

def _get_request_user() -> str:
    """从请求中提取当前用户 ID（query string 或 JSON body）"""
    uid = (request.args.get("user_id") or "").strip()
    if not uid and request.is_json:
        try:
            uid = (request.get_json(silent=True) or {}).get("user_id", "")
        except Exception:
            pass
    return uid.strip() if uid else ""


def _check_task_owner(task_id: str) -> Optional[Dict[str, Any]]:
    """检查当前用户是否有权访问该任务，返回错误响应或 None（表示放行）"""
    task = _running_tasks.get(task_id)
    if not task:
        return jsonify({"ok": False, "error": "Task not found"}), 404
    task_user = (task.get("user_id") or "").strip()
    # 无 user_id 的旧任务向后兼容，所有人可访问
    if not task_user:
        return None
    current_user = _get_request_user()
    if not current_user or current_user != task_user:
        return jsonify({"ok": False, "error": "Access denied"}), 403
    return None


# ══════════════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════════════

def _parse_project_name(filename: str) -> Optional[str]:
    """从 gqf_xxx.json 提取项目名，如 gqf_milan_32.json → milan_32"""
    m = re.match(r"^gqf_(.+)\.json$", filename)
    return m.group(1) if m else None


def _find_project_config(project: str) -> Optional[Path]:
    """查找项目的主配置文件"""
    cfg = SCRIPT_DIR / f"gqf_{project}.json"
    return cfg if cfg.exists() else None


def _exact_project_baks(project: str) -> List[Path]:
    """精确匹配项目名的 bak 文件列表。
    文件名格式：gqf_{project}.json.bak.{suffix}
    使用精确正则匹配，确保 toulouse 不会匹配到 toulouseh，milan 不会匹配到 milan_64m 等。
    """
    escaped = re.escape(project)
    pattern = re.compile(rf"^gqf_{escaped}\.json\.bak\..+$")
    return sorted(
        [f for f in SCRIPT_DIR.glob("gqf_*.json.bak.*") if pattern.match(f.name)],
        key=lambda f: f.stat().st_mtime, reverse=True,
    )


def _find_latest_bak(project: str) -> Optional[Path]:
    """查找项目最新的 bak 文件（按文件修改时间，包含所有后缀）"""
    candidates = _exact_project_baks(project)
    return candidates[0] if candidates else None


def _list_baks(project: str) -> List[Dict[str, Any]]:
    """列出项目的所有 bak 文件（从新到旧），返回摘要列表。
    按文件修改时间排序，包含所有 .json.bak.* 文件（包括 before_env_migration 等）。
    使用精确项目名匹配，toulouse 不会匹配到 toulouseh，milan 不会匹配到 milan_64m。
    """
    candidates = _exact_project_baks(project)
    result = []
    for bak in candidates:
        suffix = bak.name.split(".bak.", 1)[-1]
        try:
            cfg = json.loads(bak.read_text(encoding="utf-8"))
            rel = cfg.get("release") or {}
            v = cfg.get("vars") or {}
            result.append({
                "timestamp": suffix,
                "project": rel.get("project", ""),
                "version": rel.get("version", ""),
                "stage": rel.get("stage", ""),
                "variant": rel.get("variant", ""),
                "notes": rel.get("notes", ""),
                "tag": v.get("tag", ""),
                "boot_tag": v.get("boot_tag", ""),
                "recovery_tag": v.get("recovery_tag", ""),
                "fct_tag": v.get("fct_tag", ""),
                "file": bak.name,
            })
        except Exception:
            result.append({
                "timestamp": suffix,
                "project": project,
                "version": "?",
                "stage": "?",
                "variant": "?",
                "notes": "?",
                "tag": "?",
                "boot_tag": "?",
                "recovery_tag": "?",
                "fct_tag": "?",
                "file": bak.name,
            })
    return result


def _cleanup_baks(project: str) -> int:
    """清理 bak 文件：每个项目保留最近 BAK_KEEP_COUNT 个（仅清理 YYYYMMDD_HHMMSS 时间戳格式的 bak，保留 before_env_migration 等特殊备份）"""
    import re as _re
    ts_pattern = _re.compile(r"^.+\.json\.bak\.(\d{8}_\d{6})$")
    candidates = _exact_project_baks(project)
    # Only clean up timestamp-format bak files; keep migration/other backups
    baks = [f for f in candidates if ts_pattern.match(f.name)]
    deleted = 0
    for bak in baks[BAK_KEEP_COUNT:]:
        try:
            bak.unlink()
            deleted += 1
            print(f"[cleanup] Deleted old bak: {bak.name}")
        except Exception as e:
            print(f"[cleanup] Failed to delete {bak.name}: {e}")
    return deleted


def _cleanup_downloads(project: str) -> int:
    """清理下载目录：每个项目保留最近 DOWNLOAD_KEEP_COUNT 个版本"""
    download_base = SCRIPT_DIR / "work" / "download"
    if not download_base.exists():
        return 0

    # 匹配 work/download/<project>_debug, work/download/<project>_release
    deleted = 0
    for suffix in ("_debug", "_release"):
        pattern_dir = download_base / f"{project}{suffix}"
        if not pattern_dir.exists():
            continue
        # 按修改时间排序子目录（版本号命名的子目录）
        subdirs = sorted(
            [d for d in pattern_dir.iterdir() if d.is_dir()],
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )
        for sub in subdirs[DOWNLOAD_KEEP_COUNT:]:
            try:
                shutil.rmtree(sub)
                deleted += 1
                print(f"[cleanup] Deleted old download: {sub}")
            except Exception as e:
                print(f"[cleanup] Failed to delete {sub}: {e}")
    return deleted


# common.json 中定义的公共配置段（项目配置文件写回时剥离，避免膨胀）
_COMMON_SECTIONS = {"prepare", "doc", "notifications"}
_COMMON_SUB = {
    "jenkins": {"base_url", "timeout_sec", "auth",
                "triggers.release.parameters.PRODUCT", "triggers.release.parameters.TAG_NAME",
                "triggers.release.parameters.BOOT_TAG_NAME", "triggers.release.parameters.RECOVERY_TAG_NAME",
                "triggers.release.parameters.FCT_TAG_NAME", "triggers.release.parameters.BUILD_MODE",
                "triggers.release.parameters.BUILD_TEST_TOOL", "triggers.release.parameters.VERSION_NAME",
                "triggers.release.parameters.FCT_VERSION_NAME", "triggers.release.parameters.BUILD_RELEASE",
                "triggers.debug.parameters.PRODUCT", "triggers.debug.parameters.TAG_NAME",
                "triggers.debug.parameters.BOOT_TAG_NAME", "triggers.debug.parameters.RECOVERY_TAG_NAME",
                "triggers.debug.parameters.FCT_TAG_NAME", "triggers.debug.parameters.BUILD_MODE",
                "triggers.debug.parameters.VERSION_NAME", "triggers.debug.parameters.FCT_VERSION_NAME",
                "triggers.debug.parameters.BUILD_RELEASE",
                "triggers.Tscan.parameters.PRODUCT", "triggers.Tscan.parameters.TAG_NAME",
                "triggers.Tscan.parameters.BOOT_TAG_NAME", "triggers.Tscan.parameters.RECOVERY_TAG_NAME",
                "triggers.Tscan.parameters.FCT_TAG_NAME", "triggers.Tscan.parameters.BUILD_MODE",
                "triggers.Tscan.parameters.VERSION_NAME", "triggers.Tscan.parameters.FCT_VERSION_NAME",
                "triggers.Tscan.parameters.TSCANCODE_CHECK", "triggers.Tscan.parameters.BUILD_RELEASE",
                "triggers.auto_run_pipeline", "triggers.pipeline_args",
                "builds.debug.download", "builds.release.download", "builds.tscan.download"},
    "nas": {"webdav", "dsm", "remote.folder_name", "remote.path_strategy", "uploads"},
    "feishu": {"enabled", "use_wiki", "wiki_copy_only", "docx_replace_placeholders",
               "docx_replace_only", "print_placeholder_mapping",
               "user_access_token", "user_access_token_env", "user_refresh_token", "user_refresh_token_env",
               "oauth", "template_file_token", "target_space_id",
               "docx_target_folder_token", "docx_allow_create_fallback",
               "share_admin_perm", "share_file_type", "name_template", "placeholder_overrides",
               "timeout_sec", "debug_dump_response"},
}

def _strip_common_fields(cfg: dict):
    """从配置中移除 common.json 已有的公共字段，只保留项目差异。"""
    if not isinstance(cfg, dict):
        return
    # 移除完全相同的段
    for section in _COMMON_SECTIONS:
        cfg.pop(section, None)
    # 移除段内子字段
    for section, keys in _COMMON_SUB.items():
        if section not in cfg or not isinstance(cfg[section], dict):
            continue
        for dotted in keys:
            parts = dotted.split(".")
            d = cfg[section]
            for i, p in enumerate(parts[:-1]):
                if p not in d or not isinstance(d[p], dict):
                    break
                d = d[p]
            else:
                d.pop(parts[-1], None)


def _create_config_bak(project: str) -> Optional[str]:
    """流水线成功后创建项目配置的 bak 文件：gqf_{project}.json.bak.{YYYYMMDD_HHMMSS}"""
    cfg_path = _find_project_config(project)
    if not cfg_path or not cfg_path.exists():
        return None
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak_path = SCRIPT_DIR / f"gqf_{project}.json.bak.{timestamp}"
    try:
        shutil.copy2(cfg_path, bak_path)
        return bak_path.name
    except Exception as e:
        print(f"[bak] Failed to create {bak_path.name}: {e}")
        return None


def _resolve_credential(value: str, env_key: str) -> str:
    """优先从环境变量读取凭证，fallback 到配置文件值"""
    env_val = os.environ.get(env_key, "").strip()
    if env_val:
        return env_val
    # 检查是否是从配置直接读取的明文（非 ${ENV:...} 占位符）
    if value.startswith("${ENV:") and value.endswith("}"):
        return ""  # 占位符未解析，返回空
    return value


from credential_resolver import sanitize_config_for_display


def _push_log(task_id: str, line: str):
    """同时写入内存日志列表和 SSE 流队列"""
    task = _running_tasks.get(task_id)
    if task:
        task["log"].append(line)
    sse_q = _task_streams.get(task_id)
    if sse_q:
        try:
            sse_q.put_nowait({"line": line, "status": task["status"] if task else "unknown"})
        except queue.Full:
            pass


def _push_progress(task_id: str, steps: list, total_percent: int = None):
    """推送步骤进度更新到 SSE 流，自动计算总百分比"""
    sse_q = _task_streams.get(task_id)
    if sse_q:
        # 自动计算总进度：每个 step 25%，5个步骤 = 100%
        msg = {"steps": [{"name": s["name"], "status": s["status"], "percent": s["percent"]} for s in steps]}
        if total_percent is not None:
            msg["total_percent"] = total_percent
        else:
            # 按步骤数均分计算
            n = max(1, len(steps))
            pct = sum(s["percent"] for s in steps) // n
            msg["total_percent"] = min(99, pct)
        try:
            sse_q.put_nowait(msg)
        except queue.Full:
            pass
    # 将 total_percent 存入任务 dict，供 api_tasks_list 读取
    task = _running_tasks.get(task_id)
    if task is not None:
        if total_percent is not None:
            task["_total_percent"] = total_percent
        else:
            n = max(1, len(steps))
            task["_total_percent"] = min(99, sum(s["percent"] for s in steps) // n)
        _persist_running_tasks()


def _push_tscan_progress(task_id: str, tscan_state: dict):
    """推送 TSCAN 子任务状态到 SSE 流"""
    sse_q = _task_streams.get(task_id)
    if sse_q:
        try:
            sse_q.put_nowait({
                "tscan_status": {
                    "enabled": tscan_state.get("enabled", False),
                    "status": tscan_state.get("status", "pending"),
                    "message": tscan_state.get("message", ""),
                }
            })
        except queue.Full:
            pass


def _push_jenkins_url_to_sse(task_id: str, url: str):
    """捕获到 Jenkins build URL 后立即推送给前端，让 Jenkins 跳转按钮实时显示"""
    sse_q = _task_streams.get(task_id)
    if sse_q:
        # 去掉末尾 build number，得到 job URL（点击后可看到所有 build 列表）
        job_url = re.sub(r'/\d+/?$', '', url.rstrip('/'))
        if job_url and '/job/' in job_url:
            try:
                sse_q.put_nowait({"jenkins_job_url": job_url})
            except queue.Full:
                pass


def _process_pipeline_line(task_id: str, line: str):
    """处理单行 pipeline 输出：过滤进度条噪音，原样保留有意义日志，更新步骤进度。
    步骤：0=版本编译, 1=本地下载, 2=NAS上传, 3=分享链接, 4=飞书文档。
    进度条以 Release 版本关键节点为基准驱动（不以 Debug 为准）：
    - 本地下载 → Release 下载+解压完成后才标记完成
    - NAS上传 → Release 上传完成后才标记完成
    - 分享链接 → Release 分享链接完成后才标记完成
    """
    stripped = line.strip()
    if not stripped:
        return

    # ---- 第一层：死过滤 ----
    # 纯 # 进度条：##...## 75.3% 或 #...1.8%##...75.3%
    if re.search(r'\d+\.?\d*\s*%', stripped) and re.match(r'^[\s#\d.%]+$', stripped):
        return
    if re.match(r'^[#\s]+\s*\d+\.?\d*\s*%\s*$', stripped):
        return
    if re.match(r'^\d+\.?\d*\s*%\s*$', stripped):
        return
    if '#' in stripped and '=' in stripped and re.match(r'^[#=]+\s*$', stripped):
        return
    if re.match(r'^[#Oo=\-\.\s%]{5,}$', stripped) and 'O' in stripped.upper():
        return
    if re.match(r'^[\s=+#\-|]{15,}$', stripped) and ('+' in stripped or '|' in stripped):
        return
    if not stripped:
        return

    # ---- ANSI 清理 ----
    clean = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', line)
    clean = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', clean)
    clean = clean.strip()
    if not clean:
        return

    # ---- 步骤进度驱动 ----
    task = _running_tasks.get(task_id)
    steps = task.get("steps", []) if task else []
    n_steps = len(steps)
    IDX_DL, IDX_UP, IDX_SH, IDX_FS = 1, 2, 3, 4

    # 阶段状态追踪（以 Release 为基准）
    ps = task.setdefault("_phase_state", {
        "in_release": False,       # 是否已进入 Release 阶段
        "download_done": False,    # Release 下载+解压完成
        "upload_done": False,      # Release 上传完成
    })

    # TSCAN 子任务状态追踪
    ts = task.setdefault("_tscan_state", {
        "enabled": False,          # 是否启用 TSCAN
        "status": "pending",       # pending / building / downloading / uploading / completed / failed
        "message": "",
    })

    def _go(i, pct=5):
        if 0 <= i < n_steps and steps[i]["status"] == "waiting":
            steps[i]["status"] = "running"; steps[i]["percent"] = pct
            for j in range(i):
                if steps[j]["status"] == "waiting":
                    steps[j]["status"] = "success"; steps[j]["percent"] = 100
                    _push_progress(task_id, steps)  # 先推送完成状态
    def _done(i):
        if 0 <= i < n_steps:
            was_running = steps[i]["status"] == "running"
            steps[i]["status"] = "success"; steps[i]["percent"] = 100
            # 如果之前是 running，先推送完成状态让前端看到 100%
            if was_running:
                _push_progress(task_id, steps)

    # ═══════════════════════════════════════════
    # 进度条驱动：以 Release 关键节点为基准
    # ═══════════════════════════════════════════

    # -- Step 1 本地下载：等实际下载开始再触发 --
    # 不再在 === DEBUG === 时触发，而是等 Jenkins 下载输出出现
    dl_started = ps.get("dl_started", False)
    if not dl_started and (
        'Jenkins: fetching artifact list' in clean or
        'Jenkins:' in clean and 'artifacts selected' in clean or
        (re.match(r'^\s*\[\d+/\d+\]', clean) and '->' not in clean)
    ):
        ps["dl_started"] = True
        # 编译完成 → 开始下载
        _done(0)
        _push_log(task_id, '')
        _push_log(task_id, '>>> Jenkins 构建完成，开始下载产物 ...')
        _go(IDX_DL)

    # -- 进入 Release 阶段 --
    if '=== RELEASE ===' in clean:
        ps["in_release"] = True
        _push_log(task_id, '')
        _push_log(task_id, '>>> 开始处理 Release 版本...')

    # -- Release 下载+解压完成 → 本地下载阶段完成，开始 NAS 上传 --
    if ps.get("in_release") and not ps.get("download_done"):
        if 'Prepare:' in clean and 'staged files' in clean:
            # release_extract 完成
            _done(IDX_DL); ps["download_done"] = True
            _push_log(task_id, '')
            _push_log(task_id, '>>> 本地下载全部完成（Debug + Release + 解压），开始 NAS 文件上传阶段 ...')
            _go(IDX_UP)
        elif clean.startswith('NAS: '):
            # 无 release_extract，首次上传行 = 下载完成
            _done(IDX_DL); ps["download_done"] = True
            _push_log(task_id, '')
            _push_log(task_id, '>>> 本地下载全部完成，开始 NAS 文件上传阶段 ...')
            _go(IDX_UP)

    # -- Release 上传完成 → 开始分享链接 --
    if 'Share links:' in clean and ps.get("in_release") and ps.get("download_done"):
        if not ps.get("upload_done"):
            _done(IDX_UP); ps["upload_done"] = True
            _push_log(task_id, '')
            _push_log(task_id, '>>> NAS 文件上传全部完成，开始生成分享链接 ...')
        _go(IDX_SH)

    # -- 分享链接完成 → 生成飞书文档 --
    if 'Version doc generated' in clean:
        _done(IDX_SH)
        _push_log(task_id, '')
        _push_log(task_id, '>>> 分享链接生成完成，开始生成飞书版本文档 ...')
        _go(IDX_FS, 80)

    # -- Pipeline 结束 --
    if 'Pipeline complete' in clean:
        for s in steps: s["status"] = "success"; s["percent"] = 100
        _push_log(task_id, clean)
        _push_progress(task_id, steps)
        return

    # -- 捕获 Jenkins Queue URL 和 Build URL，供手动终止用 --
    # Queue URL 格式: "Queue: https://jenkins.xxx.com/queue/item/12345/"
    if clean.startswith("Queue:") and "/queue/item/" in clean:
        qm = re.search(r'Queue:\s*(https?://\S+/queue/item/\d+/?)\s*$', clean)
        if qm:
            q_url = qm.group(1).rstrip("/") + "/"
            q_list = task.setdefault("_jenkins_queue_urls", [])
            if q_url not in q_list:
                q_list.append(q_url)
    # Build URL 格式: "Build: https://jenkins.xxx.com/job/.../123/" 或 "  default.release build: #123 https://..."
    if "/job/" in clean:
        # [2026-06-30] 优先匹配 "build: #数字 URL" 格式的行（wget 进度条可能带 URL 噪音）
        bm = re.search(r'build:\s*#\d+\s+(https?://\S+/\d+/?)\s*$', clean)
        if not bm:
            # 兼容 "Build URL: https://..." 格式
            bm = re.search(r'Build\s+URL:\s*(https?://\S+/\d+/?)\s*$', clean, re.IGNORECASE)
        if not bm:
            # async 模式轮询输出: "Build running: MHS003.release #123 https://.../123/ (building)"
            bm = re.search(r'Build\s+running:.*?#\d+\s+(https?://\S+/\d+/?)\s*[\($]', clean, re.IGNORECASE)
        if not bm:
            # async "Builds started" 列表: "- default.release: #5845 https://.../5845/"
            bm = re.search(r'^-\s*\S+:\s+#\d+\s+(https?://\S+/\d+/?)(?:\s|$)', clean)
        if not bm:
            # async 模式结果输出: "- MHS003.release: https://.../123/ result=SUCCESS" 或 "- MHS003.release: https://.../123/ (# 123)"
            bm = re.search(r'^-\s*\S+\.\S+:\s+(https?://\S+/\d+/?)(?:\s|$)', clean)
        if bm:
            b_url = bm.group(1).rstrip("/") + "/"
            if "jenkins" in b_url.lower() and re.search(r'/\d+/?$', b_url):
                b_list = task.setdefault("_jenkins_build_urls", [])
                if b_url not in b_list:
                    b_list.append(b_url)
                    # 立即推送给前端，让 Jenkins 跳转按钮实时显示
                    _push_jenkins_url_to_sse(task_id, b_url)

    # -- 版本编译完成后立即创建 bak（无论从哪个路径触发） --
    if not ps.get("bak_created") and steps[0].get("status") == "success":
        ps["bak_created"] = True
        project = task.get("project", "")
        if project:
            bak_file = _create_config_bak(project)
            if bak_file:
                _push_log(task_id, f"Backup: {bak_file}")

    # -- Jenkins 构建轮询进度估算 --
    # 匹配各种形式的 polling 行：Polling builds (3791s): ... 或 Polling (3791s) build status
    m_poll = re.search(r'Polling\s+(?:builds?\s+)?\((\d+)s\)', clean, re.IGNORECASE)
    if m_poll and n_steps > 0 and steps[0].get("status") == "running":
        elapsed = int(m_poll.group(1))
        # 改进的进度估算：用 S 曲线，前期快后期慢
        # 0~1200s(20min) → 5%~60%，1200~3600s(20~60min) → 60%~95%
        if elapsed <= 1200:
            pct = 5 + int(elapsed / 1200 * 55)
        else:
            pct = min(95, 60 + int((elapsed - 1200) / 2400 * 35))
        steps[0]["percent"] = pct

    # -- 步骤进度百分比：基于日志中 [X/Y] 文件计数实时更新 --
    m_prog = re.match(r'^\s*\[(\d+)/(\d+)\]', clean)
    if m_prog:
        cur = int(m_prog.group(1))
        total = int(m_prog.group(2))
        if total > 0:
            # 直接按比例映射 5%~95%
            pct = 5 + int(cur / total * 90)
            # 下载阶段：不含 "->" 
            if IDX_DL < n_steps and steps[IDX_DL]["status"] == "running" and '->' not in clean:
                steps[IDX_DL]["percent"] = pct
            # 上传阶段：含 "->"
            elif IDX_UP < n_steps and steps[IDX_UP]["status"] == "running" and '->' in clean:
                steps[IDX_UP]["percent"] = pct
            # 分享链接阶段
            elif IDX_SH < n_steps and steps[IDX_SH]["status"] == "running":
                steps[IDX_SH]["percent"] = pct

    # ═══════════════════════════════════════════
    # TSCAN 子任务状态检测
    # ═══════════════════════════════════════════
    if '[TSCAN' in clean:
        # 检测到 TSCAN 触发，启用 TSCAN 状态追踪
        if 'Trigger TSCAN' in clean or 'build_tscan=yes' in clean:
            ts["enabled"] = True
            ts["status"] = "building"
            ts["message"] = "TSCAN Jenkins 构建中..."
            _push_tscan_progress(task_id, ts)
        elif ts.get("enabled"):
            if 'DOWNLOAD' in clean and '开始下载' in clean:
                ts["status"] = "downloading"
                ts["message"] = "正在下载 TSCAN 产物..."
                _push_tscan_progress(task_id, ts)
            elif 'DOWNLOAD' in clean and '下载完成' in clean:
                ts["status"] = "uploading"
                ts["message"] = "正在上传 TSCAN 产物到 NAS 并更新飞书文档..."
                _push_tscan_progress(task_id, ts)
            elif 'UPLOAD' in clean:
                ts["status"] = "uploading"
                ts["message"] = "正在上传 TSCAN 产物到 NAS 并更新飞书文档..."
                _push_tscan_progress(task_id, ts)
            elif 'COMPLETE' in clean:
                ts["status"] = "completed"
                ts["message"] = "TSCAN 产物已上传 NAS，飞书文档已更新"
                _push_tscan_progress(task_id, ts)

    _push_log(task_id, clean)
    _push_progress(task_id, steps)


def _update_step(steps: list, index: int, status: str):
    """更新步骤状态"""
    if not steps or index >= len(steps):
        return
    if steps[index]["status"] == "waiting":
        steps[index]["status"] = status
        steps[index]["percent"] = 5  # 开始执行



def _sanitize_config_for_display(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """移除敏感凭证字段，返回安全的展示副本"""
    return sanitize_config_for_display(cfg)


# ══════════════════════════════════════════════════════
# API 路由
# ══════════════════════════════════════════════════════

@app.route("/api/bind/start")
def api_bind_start():
    """生成绑定码：Web 界面用此码让用户私聊 Bot 完成身份绑定"""
    _cleanup_bind_codes()
    code = _gen_bind_code()
    _bind_codes[code] = {"ts": time.time()}
    return jsonify({"ok": True, "bind_code": code})

@app.route("/api/bind/check")
def api_bind_check():
    """检查绑定码是否已被 Bot 关联"""
    code = (request.args.get("code") or "").strip()
    if not code or code not in _bind_codes:
        return jsonify({"ok": False, "error": "绑定码不存在或已过期"})
    entry = _bind_codes[code]
    open_id = entry.get("open_id", "")
    chat_id = entry.get("chat_id", "")
    if open_id:
        return jsonify({"ok": True, "open_id": open_id, "chat_id": chat_id})
    return jsonify({"ok": False, "waiting": True})


# ── 飞书免登：JSSDK requestAccess 自动获取用户 open_id ──
@app.route("/api/feishu/app-id")
def api_feishu_app_id():
    """返回飞书应用的 app_id（前端免登需要）"""
    oauth = {}
    try:
        common = json.loads((SCRIPT_DIR / "common.json").read_text(encoding="utf-8"))
        oauth = common.get("feishu", {}).get("oauth", {})
    except Exception:
        pass
    app_id = oauth.get("app_id", "")
    return jsonify({"ok": True, "app_id": app_id})


@app.route("/api/feishu/auto-bind", methods=["POST"])
def api_feishu_auto_bind():
    """飞书免登：用 JSSDK requestAccess 返回的临时授权码换取 open_id"""
    try:
        code = (request.get_json(silent=True) or {}).get("code", "").strip()
    except Exception:
        code = ""
    if not code:
        return jsonify({"ok": False, "error": "缺少 code 参数"}), 400

    try:
        token = _get_feishu_tenant_token()
        # 1. 用临时授权码换取 user_access_token
        resp = requests.post(
            "https://open.feishu.cn/open-apis/authen/v1/oidc/access_token",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"grant_type": "authorization_code", "code": code},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        user_token = (data.get("data") or {}).get("access_token", "")
        if not user_token:
            err_msg = data.get("msg", "") or str(data.get("code", ""))
            print(f"[FeishuAutoBind] token exchange failed: {data}", file=sys.stderr)
            return jsonify({"ok": False, "error": f"授权码换取失败: {err_msg}"}), 400

        # 2. 用 user_access_token 获取用户 open_id
        resp2 = requests.get(
            "https://open.feishu.cn/open-apis/authen/v1/user_info",
            headers={"Authorization": f"Bearer {user_token}"},
            timeout=10,
        )
        resp2.raise_for_status()
        user_data = resp2.json()
        open_id = (user_data.get("data") or {}).get("open_id", "")
        if not open_id:
            print(f"[FeishuAutoBind] get user_info empty: {user_data}", file=sys.stderr)
            return jsonify({"ok": False, "error": "获取用户信息失败"}), 400

        print(f"[FeishuAutoBind] ✅ auto-bound open_id={open_id}", file=sys.stderr)
        return jsonify({"ok": True, "open_id": open_id})
    except requests.RequestException as e:
        print(f"[FeishuAutoBind] network error: {e}", file=sys.stderr)
        return jsonify({"ok": False, "error": f"网络异常: {str(e)[:100]}"}), 500
    except Exception as e:
        print(f"[FeishuAutoBind] error: {e}", file=sys.stderr)
        return jsonify({"ok": False, "error": str(e)[:100]}), 500

@app.route("/")
def index():
    import time
    return render_template("index.html", version=int(time.time()))


# ══════════════════════════════════════════════════════
# 飞书 Bot 事件回调 — 接收消息自动触发构建
# ══════════════════════════════════════════════════════

# 待确认的构建任务 {chat_id: {device, version, params}}
_pending_builds: Dict[str, dict] = {}

# 支持的设备名称列表
_FEISHU_KNOWN_DEVICES = {
    # NXP595/Apollo4
    "stuttgart", "galaxy", "toulouse", "toulouseh", "andes", "andesw",
    "berlin", "cheetah", "swordfish", "swift", "monaco", "vienna", "vienna2025",
    # MHS003
    "pike", "warsaw", "windermere", "cologne", "geneva", "lyon",
    "makalu", "matterhorn", "rimo", "rocky", "milan", "milan_64m",
    "pamir", "pamir_64m", "rome_64m", "seattle",
    # MHS003S
    "oslo", "munich", "dublin", "atlas",
}


def _parse_any_command(text: str) -> dict:
    """智能解析用户消息，识别意图并提取参数。
    
    返回格式：
      {"intent": "build"|"changelog"|"tscan"|"skip_jenkins"|"unknown",
       "device": "geneva"|None,
       "params": {...},
       "missing": ["需要但未填的参数"]}
    
    支持意图：
      - build: 打版本/构建/发版/build/release
      - changelog: changelog/差分/对比/diff/compare
      - tscan: tscan/扫描
      - skip_jenkins: 生成文档/跳过编译/直接生成/已有编译/已有build
    """
    import re
    if not text:
        return {"intent": "unknown", "device": None, "params": {}, "missing": []}
    text = text.strip()
    # 去除飞书 @机器人 前缀（@_user_1 等，可能在开头或中间）
    text = re.sub(r'@_user_\d+\s*', '', text).strip()
    # 飞书自动把 x.x.x.x 转成 markdown 链接：[1.1.1.1](http://1.1.1.1/) → 只保留版本文字
    text = re.sub(r'\[(\d+\.\d+\.\d+(?:\.\d+)?)\]\(http[^)]+\)', r'\1', text)
    # 规范化设备名变体：milan64m / milan64 / milan 64m → milan_64m，rome → rome_64m
    for base in ("milan", "pamir", "rome"):
        text = re.sub(rf'\b{base}\s*64\s*([mM])?\b', f'{base}_64m', text)
    # rome 只有 64m 版本，裸 rome 也映射到 rome_64m
    text = re.sub(r'\brome\b', 'rome_64m', text)

    # ── 设备名提取 ──
    device = None
    # 先精确匹配 "_" 分隔的复合名（如 milan_32）
    for known in sorted(_FEISHU_KNOWN_DEVICES, key=lambda x: len(x), reverse=True):
        if re.search(rf'\b{re.escape(known)}\b', text, re.IGNORECASE):
            device = known
            break
    # 再尝试模糊匹配：milan32M → milan（提取基础设备名）
    if not device:
        for known in sorted(_FEISHU_KNOWN_DEVICES, key=lambda x: len(x), reverse=True):
            p = re.search(rf'\b({re.escape(known)})[\d_]*[A-Za-z]*\b', text, re.IGNORECASE)
            if p:
                device = p.group(1)
                break

    # ── 版本号提取 ──
    ver_pattern = re.compile(r'\b(\d+\.\d+\.\d+(?:\.\d+)?)\b')
    versions = ver_pattern.findall(text)

    # ── URL 提取（skip_jenkins 需要）──
    url_pattern = re.compile(r'(https?://[^\s]+)')
    urls = url_pattern.findall(text)

    # ── 意图识别 (LLM 优先，正则兜底) ──
    intent = "unknown"

    # LLM 优先：启用时先让 AI 理解意图
    try:
        llm_cfg = _load_llm_config()
        if llm_cfg.get("enabled") and llm_cfg.get("api_key") and _looks_like_intent(text):
            llm_result = _parse_with_llm(text, _FEISHU_KNOWN_DEVICES, list(_FEISHU_KNOWN_DEVICES))
            if llm_result.get("intent") != "unknown":
                intent = llm_result.get("intent", "unknown")
                device = llm_result.get("device") or device
                params = dict(llm_result.get("params", {}))
                print(f"[LLM] intent={intent} device={device} params={params}")
    except Exception as e:
        print(f"[LLM] 优先识别异常: {e}")

    # 正则兜底（LLM 未启用或识别失败时）
    if intent == "unknown":
        intent_patterns = [
            ("changelog", r'(?:chang?elog|差分|(?:版本)?对比|diff(?:\s|$)|compare)'),
            ("tscan", r'(?:tscan|TSCAN|扫描(?!版本|发版))'),
            ("skip_jenkins", r'(?:生成文档|直接生成|跳过编译|跳过Jenkins|已有编译|已有build|发文档|只生成|已有\s*(?:编译|build|构建))'),
            ("build", r'(?:打版本|构建|发版|build\b|release\b)'),
        ]
        for intent_name, pattern in intent_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                intent = intent_name
                break
        if intent == "unknown":
            # 排除确认/取消类短回复
            if re.search(r'^(确认|取消|算了|不要|yes|no|ok|cancel|好|可以|行|对)\b', text, re.IGNORECASE):
                pass
            elif device and versions:
                intent = "build"

    # LLM 可能已填充 params，仅在正则未匹配时不覆盖
    if 'params' not in locals():
        params = {}
    missing = []

    # ── 算法 tag（排除 URL）──
    if 'tag' not in params:
        tag_candidates = re.findall(r'\b(\S*[/_]\S+)\b', text)
        for candidate in tag_candidates:
            low = candidate.lower()
            if any(kw in low for kw in ('bootloader', 'recovery', 'fct-', 'http://', 'https://', 'releases/')):
                continue
            params['tag'] = candidate
            break
    if 'tag' not in params:
        tm = re.search(r'(?:算法\s*tag|alg?o\s*tag)\s*[:：]?\s*(\S+)', text, re.IGNORECASE)
        if tm: params['tag'] = tm.group(1)
    if 'tag' not in params:
        # key=value
        for token in text.split():
            if token.startswith('tag='):
                params['tag'] = token.split('=', 1)[1]

    # ── 分支提取 ──（补充消息常见：分支 ： releases/xxx）
    if 'branch' not in params:
        bm = re.search(r'(?:分支|branch)\s*[:：]?\s*(\S+)', text, re.IGNORECASE)
        if bm: params['branch'] = bm.group(1)

    # ── 目标版本/diff 提取 ──（补充消息：changelog : 7.2.0.1 或 目标版本 7.1.0.2）
    if 'diff' not in params and versions:
        dm = re.search(r'(?:changelog对比版本|changelog|目标版本|对比版本|diff\s*版本|差分版本)\s*[-:：]?\s*(\d+\.\d+\.\d+(?:\.\d+)?)', text, re.IGNORECASE)
        if dm: params['diff'] = dm.group(1)

    # ── 构建 TSCAN / 自动绑定 ──（支持：构建TSCAN yes / 自动绑定=YES）
    for key, pattern in [
        ('build_tscan', r'(?:构建\s*TSCAN|build\s*tscan|tscan\s*build)\s*[:：=]?\s*(YES|NO|yes|no|是|否)'),
        ('auto_bind', r'(?:自动绑定|auto\s*bind)\s*[:：=]?\s*(YES|NO|yes|no|是|否)'),
    ]:
        if key not in params:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                raw = m.group(1).upper()
                params[key] = 'YES' if raw in ('YES', '是') else 'NO'

    # ── 按意图提取特定参数 ──
    if device and intent == "build":
        # boot/recovery/fct
        if device:
            for suffix, key in [('bootloader', 'boot'), ('recovery', 'recovery'), ('fct', 'fct')]:
                if key not in params:
                    m = re.search(rf'\b({re.escape(device)}-{suffix}-[\d.]+|\S+-{suffix}-\S+)\b', text, re.IGNORECASE)
                    if m: params[key] = m.group(1)
        # key=value pairs
        for token in text.split():
            if '=' in token:
                k, v = token.split('=', 1)
                k = k.strip().lower()
                if k in ('boot', 'recovery', 'fct', 'ver', 'branch', 'diff', 'stage', 'build_tscan', 'auto_bind'):
                    params[k] = v.strip()
        # stage 也可以从自然语言提取："ota2 第三轮" "阶段 ota3" 等
        if 'stage' not in params:
            sm = re.search(r'(?:阶段|stage)\s*[:：]?\s*(\S+)', text, re.IGNORECASE)
            if sm: params['stage'] = sm.group(1)
        if versions:
            params.setdefault('ver', versions[0])
        if len(versions) >= 2 and 'diff' not in params:
            params['diff'] = versions[1]

        if not device: missing.append('设备名')
        if not params.get('ver') and not versions: missing.append('版本号')

    elif device and intent == "changelog":
        if len(versions) >= 2:
            params['prev'] = versions[0]
            params['curr'] = versions[1]
        elif len(versions) == 1:
            params['prev'] = versions[0]
        if not params.get('prev'): missing.append('旧版本号')
        if not params.get('curr'): missing.append('新版本号')
        if not device: missing.append('设备名')

    elif device and intent == "tscan":
        if not params.get('tag'): missing.append('算法tag')
        if not device: missing.append('设备名')

    elif device and intent == "skip_jenkins":
        if urls and len(urls) >= 2:
            params['debug_url'] = urls[0]
            params['release_url'] = urls[1]
        elif urls:
            params['release_url'] = urls[0]
        if versions:
            params['ver'] = versions[0]
        if not device: missing.append('设备名')
        if not params.get('release_url'): missing.append('Jenkins Build URL')

    return {"intent": intent, "device": device, "params": params, "missing": missing}


# ── LLM 意图识别 (DeepSeek) ──────────────────────────

# LLM 配置缓存
_llm_config_cache: Optional[dict] = None


def _load_llm_config() -> dict:
    """加载 LLM 配置（带缓存），优先读环境变量"""
    global _llm_config_cache
    if _llm_config_cache is not None:
        return _llm_config_cache

    cfg = {"enabled": False, "provider": "deepseek", "api_key": "", "api_url": "", "model": "deepseek-chat", "temperature": 0, "max_tokens": 512, "timeout_sec": 10}
    try:
        common = json.loads((SCRIPT_DIR / "common.json").read_text(encoding="utf-8"))
        llm_cfg = common.get("llm", {})
        if llm_cfg:
            cfg.update(llm_cfg)
    except Exception:
        pass

    # 环境变量优先
    env_key = cfg.get("api_key_env", "DEEPSEEK_API_KEY")
    env_val = os.environ.get(env_key, "") if env_key else ""
    if env_val:
        cfg["api_key"] = env_val

    _llm_config_cache = cfg
    return cfg


def _looks_like_intent(text: str) -> bool:
    """快速判断文本是否"看起来像"一个指令（避免无意义的 LLM 调用）

    规则：
    - 太短（<4字符）且不包含设备名 → False
    - 包含设备名或常见动词 → True
    - 纯问候/闲聊 → False
    """
    if not text or len(text) < 3:
        return False

    low = text.lower()

    # 纯问候/闲聊 → 不走 LLM
    greeting_only = re.fullmatch(r'^(hi|hello|你好|在吗|在不在|谢谢|3Q|ok|嗯|哦|好|知道了)[!！。.]*$', low)
    if greeting_only:
        return False

    # 包含设备名 → 大概率是指令
    for known in _FEISHU_KNOWN_DEVICES:
        if known in low:
            return True

    # 包含动词/操作词 → 可能是指令
    action_words = (
        "打", "构建", "编译", "发版", "发", "发布", "跑",
        "对比", "差分", "改了什么", "改了什么",
        "扫描", "tscan", "生成", "跳过", "文档",
        "build", "release", "diff", "compare", "changelog",
        "版本", "打包", "帮我", "能不能", "可以", "麻烦",
        "查", "看", "做", "要", "想", "触发",
    )
    for word in action_words:
        if word in low:
            return True

    return False


def _parse_with_llm(text: str, device_list: set, intent_list: list) -> dict:
    """用 DeepSeek LLM 做意图识别和实体提取。

    只在正则匹配失败时作为 fallback 调用。
    返回格式与 _parse_any_command() 完全一致。
    """
    cfg = _load_llm_config()
    if not cfg.get("enabled") or not cfg.get("api_key"):
        return {"intent": "unknown", "device": None, "params": {}, "missing": [], "_llm_error": "disabled_or_no_key"}

    api_url = cfg.get("api_url", "https://api.deepseek.com/v1/chat/completions")
    model = cfg.get("model", "deepseek-chat")
    timeout = cfg.get("timeout_sec", 10)

    sys_prompt = (
        "你是一个固件发版助手的意图识别器。分析用户消息，判断意图并提取参数。\n\n"
        "## 支持的意图（4种）：\n"
        "1. build — 打版本/编译/构建/发版/发布。需要：设备名 + 版本号(必填) + 算法tag\n"
        "2. changelog — 对比两个版本的改动/changelog/差分。需要：设备名 + 旧版本号 + 新版本号\n"
        "3. tscan — 代码扫描/TSCAN。需要：设备名 + 算法tag(必填)\n"
        "4. skip_jenkins — 跳过编译直接用已有构建产物生成文档。需要：设备名 + Jenkins Build URL\n"
        "5. unknown — 以上都不是\n\n"
        f"## 支持的设备（完整列表）：\n{', '.join(sorted(device_list))}\n\n"
        "## 参数说明：\n"
        "- ver: 主版本号（x.x.x.x 或 x.x.x 格式）\n"
        "- diff/prev/curr: changelog 对比的旧版和新版版本号\n"
        "- tag: 算法 tag（如 \"v2.3\"、\"bootloader_v1\" 等）\n"
        "- boot: bootloader tag\n"
        "- recovery: recovery tag\n"
        "- fct: FCT tag\n"
        "- release_url: Jenkins Build URL\n"
        "- stage: 阶段名称\n\n"
        "## 输出要求：\n"
        "严格只输出一个 JSON 对象，不要包含任何其他文字、markdown 标记或注释：\n"
        '{"intent": "build|changelog|tscan|skip_jenkins|unknown", "device": "设备名或null", "params": {}, "missing": ["缺失的参数名称"], "confidence": "high|medium|low"}'
    )

    raw_content = ""
    try:
        resp = requests.post(
            api_url,
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": text},
                ],
                "temperature": cfg.get("temperature", 0),
                "max_tokens": cfg.get("max_tokens", 512),
            },
            headers={
                "Authorization": f"Bearer {cfg['api_key']}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        raw_content = data.get("choices", [{}])[0].get("message", {}).get("content", "")

        # 解析 LLM 返回的 JSON（可能被 ```json 包裹）
        json_str = raw_content.strip()
        if json_str.startswith("```"):
            json_str = re.sub(r'^```(?:json)?\s*\n', '', json_str)
            json_str = re.sub(r'\n```\s*$', '', json_str)
        result = json.loads(json_str)

        # 标准化字段，补默认值
        result.setdefault("device", None)
        result.setdefault("params", {})
        result.setdefault("missing", [])
        result.setdefault("confidence", "medium")

        # 设备名校验：LLM 可能输出不在列表里的设备名
        raw_device = result.get("device")
        if raw_device and str(raw_device).lower() not in {d.lower() for d in device_list}:
            # 尝试模糊匹配
            for known in sorted(device_list, key=lambda x: len(x), reverse=True):
                if known.lower() in str(raw_device).lower() or str(raw_device).lower() in known.lower():
                    result["device"] = known
                    break
            else:
                result["device"] = None

        print(f"[LLM] 意图: {result.get('intent')} 设备: {result.get('device')} 置信度: {result.get('confidence')}")
        return result

    except requests.exceptions.Timeout:
        print(f"[LLM] 超时 ({timeout}s)")
        return {"intent": "unknown", "device": None, "params": {}, "missing": [], "_llm_error": "timeout"}
    except requests.exceptions.RequestException as e:
        print(f"[LLM] 网络错误: {e}")
        return {"intent": "unknown", "device": None, "params": {}, "missing": [], "_llm_error": f"network:{e}"}
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        print(f"[LLM] JSON 解析失败: {e}  原始内容: {raw_content[:200]}")
        return {"intent": "unknown", "device": None, "params": {}, "missing": [], "_llm_error": "parse_error"}
    except Exception as e:
        print(f"[LLM] 未知错误: {e}")
        import traceback
        traceback.print_exc()
        return {"intent": "unknown", "device": None, "params": {}, "missing": [], "_llm_error": f"unknown:{e}"}


def _feishu_reply_message(chat_id: str, content):
    """通过飞书 API 回复消息。
    
    自动识别内容类型：
    - dict → 飞书交互式卡片（通过 REST API）
    - str  → Markdown 文本（优先 lark-cli，fallback API）
    """
    if isinstance(content, dict):
        try:
            _send_feishu_card(chat_id, content)
        except Exception as e:
            print(f"[FeishuBot] card reply error: {e}", file=sys.stderr)
            _send_feishu_msg_api(chat_id, f"抱歉，消息发送异常：{str(e)[:80]}")
        return

    markdown = content
    # 先尝试 lark-cli（需要 Node.js）
    try:
        from lark_cli_adapter import lark_send_message
        if lark_send_message(chat_id=chat_id, markdown=markdown):
            return  # lark-cli 发送成功
    except Exception:
        pass

    # Fallback：直接用飞书 API 回复（不需要 Node.js）
    try:
        _send_feishu_msg_api(chat_id, markdown)
    except Exception as e:
        print(f"[FeishuBot] 回复消息失败: {e}", file=sys.stderr)


def _get_feishu_tenant_token():
    """获取飞书 tenant_access_token"""
    import time as _time
    now = _time.time()
    cached = _get_feishu_tenant_token._cache
    if cached and now - cached["ts"] < cached.get("expire", 7200) - 300:
        return cached["token"]

    oauth = {}
    try:
        common = json.loads((SCRIPT_DIR / "common.json").read_text(encoding="utf-8"))
        oauth = common.get("feishu", {}).get("oauth", {})
    except Exception:
        pass
    app_id = oauth.get("app_id", "")
    app_secret = oauth.get("app_secret", "")
    if not app_id or not app_secret:
        raise RuntimeError("Feishu app_id/app_secret not found")

    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=10,
    )
    data = resp.json()
    token = data.get("tenant_access_token", "")
    expire = data.get("expire", 7200)
    if not token:
        raise RuntimeError(f"Failed to get tenant_access_token: {data}")

    _get_feishu_tenant_token._cache = {"token": token, "ts": now, "expire": expire}
    return token

_get_feishu_tenant_token._cache = None


def _send_feishu_msg_api(chat_id: str, markdown: str):
    """通过飞书 REST API 直接发送消息，不抛异常"""
    try:
        token = _get_feishu_tenant_token()
        content = json.dumps({"text": markdown}, ensure_ascii=False)
        resp = requests.post(
            "https://open.feishu.cn/open-apis/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            json={
                "receive_id": chat_id,
                "msg_type": "text",
                "content": content,
            },
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
            timeout=15,
        )
        data = resp.json()
        if data.get("code") != 0:
            print(f"[FeishuBot] API 回复失败: code={data.get('code')}", file=sys.stderr)
    except Exception as e:
        print(f"[FeishuBot] text send error: {e}", file=sys.stderr)


def _send_feishu_card(chat_id: str, card: dict):
    """通过飞书 REST API 发送交互式卡片消息"""
    try:
        token = _get_feishu_tenant_token()
        content = json.dumps(card, ensure_ascii=False)
        resp = requests.post(
            "https://open.feishu.cn/open-apis/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            json={
                "receive_id": chat_id,
                "msg_type": "interactive",
                "content": content,
            },
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
            timeout=15,
        )
        data = resp.json()
        if data.get("code") != 0:
            print(f"[FeishuBot] Card failed: code={data.get('code')} msg={data.get('msg')}", file=sys.stderr)
            _send_feishu_msg_api(chat_id, f"卡片发送失败 ({data.get('code')})，请稍后重试")
        else:
            print(f"[FeishuBot] Card sent", file=sys.stderr)
    except Exception as e:
        print(f"[FeishuBot] Card exception: {e}", file=sys.stderr)
        import traceback; traceback.print_exc()
        try:
            _send_feishu_msg_api(chat_id, f"发送失败：{str(e)[:100]}")
        except Exception:
            pass


def _do_trigger_build(chat_id: str, device: str, version: str, params: dict, open_id: str = ""):
    """实际触发构建和 changelog。
    
    参数：
        chat_id: 飞书会话 ID（oc_xxx），用于实时回复
        open_id: 飞书用户 open_id（ou_xxx），用于最终个人通知
                群聊触发时 chat_id≠open_id，需要区分
    """
    try:
        cfg_path = SCRIPT_DIR / f"gqf_{device}.json"
        if not cfg_path.exists():
            _feishu_reply_message(chat_id, f"❌ 设备 `{device}` 的配置文件不存在")
            return

        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        override_msg_parts = []
        if params.get("branch"):
            cfg.setdefault("release", {})["notes"] = params["branch"]
            override_msg_parts.append(f"分支={params['branch']}")
        if params.get("stage"):
            cfg.setdefault("release", {})["stage"] = params["stage"]
            override_msg_parts.append(f"阶段={params['stage']}")
        if params.get("tag"):
            cfg.setdefault("build", {}).setdefault("tag_algo", params["tag"])
            override_msg_parts.append(f"算法tag={params['tag']}")
        if params.get("boot"):
            cfg.setdefault("build", {}).setdefault("boot_tag", params["boot"])
            override_msg_parts.append(f"boot={params['boot']}")
        if params.get("recovery"):
            cfg.setdefault("build", {}).setdefault("recovery_tag", params["recovery"])
            override_msg_parts.append(f"recovery={params['recovery']}")
        if params.get("fct"):
            cfg.setdefault("build", {}).setdefault("fct_tag", params["fct"])
            override_msg_parts.append(f"fct={params['fct']}")
        if params.get("build_tscan"):
            tscan_val = str(params["build_tscan"]).upper()
            cfg.setdefault("jenkins", {}).setdefault("triggers", {}).setdefault("release", {}).setdefault("parameters", {})["TSCANCODE_CHECK"] = tscan_val
            override_msg_parts.append(f"构建TSCAN={tscan_val}")
        if params.get("auto_bind"):
            bind_val = str(params["auto_bind"]).upper()
            cfg.setdefault("jenkins", {}).setdefault("triggers", {}).setdefault("release", {}).setdefault("parameters", {})["AUTH_CFG_AUTO_BINDING"] = bind_val
            override_msg_parts.append(f"自动绑定={bind_val}")
        ver = version or params.get("ver")
        if ver:
            # 只取前三位作为 release 版本号
            ver_parts = ver.split('.')
            if len(ver_parts) >= 3:
                ver = '.'.join(ver_parts[:3])
            cfg.setdefault("release", {})["version_name"] = ver
            override_msg_parts.append(f"版本={ver}")
            # debug 版本号：优先用用户指定的，否则默认与 release 一致
            debug_ver = params.get("debug", "")
            if not debug_ver and ver:
                debug_ver = ver
            if debug_ver:
                cfg.setdefault("vars", {})["debug_version_name"] = debug_ver
                override_msg_parts.append(f"debug={debug_ver}(异步并行)")
        diff_ver = params.get("diff", "")
        print(f"[FeishuBot] trigger build: device={device} ver={ver} diff={diff_ver} params_keys={list(params.keys())}")
        if diff_ver:
            override_msg_parts.append(f"changelog对比={diff_ver}")

        release_info = {"device": device, "version": ver or cfg.get("release", {}).get("version_name", "")}
        bot_vars = {}
        # ── 将 changelog diff 参数写入 vars，由 pipeline 自动触发 changelog ──
        # （避免在 _do_trigger_build 中重复调用 start-changelog，与 pipeline 内部触发冲突）
        if diff_ver:
            try:
                prev_code, prev_err = _lookup_version_code(device, diff_ver)
                if prev_err:
                    print(f"[FeishuBot] changelog diff version lookup failed: {prev_err}", file=sys.stderr)
                else:
                    tag = params.get("tag", "") or cfg.get("build", {}).get("tag_algo", "")
                    bot_vars["prev_version_name"] = diff_ver
                    bot_vars["prev_version_code"] = prev_code
                    if tag:
                        bot_vars["tag"] = tag
                    print(f"[FeishuBot] changelog vars to be merged: prev={diff_ver} code={prev_code} tag={tag}")
            except Exception as e:
                print(f"[FeishuBot] write changelog vars failed: {e}", file=sys.stderr)

        payload = {"release": release_info, "vars": bot_vars, "user_id": f"feishu_bot_{chat_id}", "open_id": open_id, "jenkins_auth": {"username": "", "password": ""}}
        jcfg = cfg.get("jenkins", {}); auth = jcfg.get("auth", {})
        if auth.get("username") and auth.get("token"):
            payload["jenkins_auth"] = {"username": auth["username"], "password": auth["token"]}

        lbl_ver = release_info.get('version', '') or '配置默认'
        override_msg = ("（覆盖：" + ", ".join(override_msg_parts) + "）") if override_msg_parts else ""
        _feishu_reply_message(chat_id, f"🔄 正在为 **{device}** 触发构建...\n版本：{lbl_ver} {override_msg}")

        with app.test_client() as client:
            resp = client.post(f"/api/projects/{device}/release", json=payload, content_type="application/json")
            data = resp.get_json()
            if data.get("ok"):
                task_id = data.get("task_id", "")
                reply_parts = [f"✅ **构建已触发！**", f"", f"设备：`{device}`", f"版本：`{release_info['version']}`", f"任务 ID：`{task_id}`"]

                if diff_ver:
                    reply_parts.append(f"📝 Changelog：`{diff_ver}` → `{release_info['version']}`（pipeline 自动触发）")

                _feishu_reply_message(chat_id, "\n".join(reply_parts))
            else:
                _feishu_reply_message(chat_id, f"❌ 构建触发失败：{data.get('error', '未知错误')}")
    except Exception as e:
        import traceback; traceback.print_exc()
        _feishu_reply_message(chat_id, f"❌ 异常：{str(e)[:200]}")


def _lookup_version_code(device: str, version_name: str) -> tuple:
    """通过归档 API 精确查询 versionName 对应的 versionCode。
    返回 (version_code, error_msg)：
      - 成功：(code, None)
      - 版本不存在：(None, "版本 x.x.x.x 不存在，请确认")
      - 查询失败：(None, "查询 versionCode 失败: ...")
    """
    if not device or not version_name:
        return (None, "缺少设备名或版本号")
    try:
        params = dict(num="1", trigger="", type="firmware", device=device,
                       client="", versionCode="", versionName=version_name, status="",
                       build_cause="", env="", production=device)
        resp = requests.get("https://open.zepp.top/archive/api/log/notes",
                           params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            detail = data.get("detail", [])
            if detail and len(detail) > 0:
                item = detail[0]
                code = str(item.get("versionCode", ""))
                if code:
                    return (code, None)
                else:
                    return (None, f"版本 `{version_name}` 存在但缺少 versionCode")
            else:
                return (None, f"版本 `{version_name}` 在归档中不存在，请确认版本号是否正确")
        else:
            return (None, f"查询归档 API 返回 {resp.status_code}")
    except Exception as e:
        return (None, f"查询归档 API 失败: {str(e)[:100]}")


def _do_trigger_changelog(chat_id, device, params):
    """触发 changelog 差分对比"""
    try:
        cfg_path = SCRIPT_DIR / f"gqf_{device}.json"
        if not cfg_path.exists():
            _feishu_reply_message(chat_id, f"❌ 设备 `{device}` 的配置文件不存在")
            return
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        tag = params.get("tag", "") or cfg.get("build", {}).get("tag_algo", "")
        prev_name = params.get("prev", "")
        curr_name = params.get("curr", "") or cfg.get("release", {}).get("version_name", "")
        if not prev_name or not curr_name:
            _feishu_reply_message(chat_id, "❌ 缺少旧版本号或新版本号")
            return

        # 自动查询 version_code
        prev_code, prev_err = _lookup_version_code(device, prev_name)
        curr_code, curr_err = _lookup_version_code(device, curr_name)

        if prev_err or curr_err:
            errors = []
            if prev_err: errors.append(f"旧版本：{prev_err}")
            if curr_err: errors.append(f"新版本：{curr_err}")
            _feishu_reply_message(chat_id, "❌ " + "\n".join(errors))
            return

        jcfg = cfg.get("jenkins", {}); auth = jcfg.get("auth", {})
        jenkins_auth = {"username": "", "password": ""}
        if auth.get("username") and auth.get("token"):
            jenkins_auth = {"username": auth["username"], "password": auth["token"]}

        _feishu_reply_message(chat_id, f"🔄 正在触发 **{device}** Changelog：`{prev_name}`(`{prev_code}`) → `{curr_name}`(`{curr_code}`)...")
        with app.test_client() as client:
            resp = client.post(f"/api/projects/{device}/start-changelog", json={
                "device": device, "manifest": f"{device}.xml",
                "prev_version_name": prev_name, "prev_version_code": prev_code,
                "curr_version_name": curr_name, "curr_version_code": curr_code,
                "tag": tag, "user_id": f"feishu_bot_{chat_id}", "jenkins_auth": jenkins_auth,
            }, content_type="application/json")
            data = resp.get_json()
            if data.get("ok"):
                _feishu_reply_message(chat_id, f"✅ Changelog 已触发！\n`{prev_name}` → `{curr_name}`\n任务 ID：`{data.get('task_id','')}`")
            else:
                _feishu_reply_message(chat_id, f"❌ 失败：{data.get('error','')}")
    except Exception as e:
        import traceback; traceback.print_exc()
        _feishu_reply_message(chat_id, f"❌ 异常：{str(e)[:200]}")


def _try_read_doc_from_message(message_event: dict) -> str:
    """从飞书消息事件中提取文档链接并读取内容，返回文本或空字符串"""
    content_str = message_event.get("content", "{}")
    try:
        content = json.loads(content_str)
    except (json.JSONDecodeError, TypeError):
        return ""

    # 飞书文档 URL 格式：支持 docx / docs / wiki
    doc_urls = []
    import re as _re

    # 从文本内容中提取文档链接
    text = content.get("text", "")
    if text:
        doc_urls.extend(_re.findall(r'(https?://[^\s]+?(?:docx|docs|wiki)/[^\s]+)', text))

    # 从富文本块中提取（含 wiki 类型）
    for block in content.get("blocks", []):
        for elem in block.get("elements", []):
            etype = elem.get("type", "")
            if etype in ("doc", "wiki"):
                doc_urls.append(elem.get("url", ""))
            for sub in elem.get("elements", []):
                subtype = sub.get("type", "")
                if subtype in ("doc", "wiki"):
                    doc_urls.append(sub.get("url", ""))

    # 去重并读取
    doc_texts = []
    seen = set()
    for url in doc_urls:
        if url in seen: continue
        seen.add(url)
        try:
            from lark_cli_adapter import lark_fetch_doc
            doc_content = lark_fetch_doc(doc_url=url)
            if doc_content:
                doc_texts.append(doc_content.strip())
            print(f"[FeishuBot] 读取文档: {url[:60]} → {len(doc_content)} 字符")
        except Exception as e:
            print(f"[FeishuBot] 读取文档失败: {url[:60]}: {e}")

    return "\n".join(doc_texts)


def _extract_params_from_doc(doc_text: str) -> dict:
    """从文档内容中提取发版参数"""
    import re as _re
    params = {}
    # 设备名
    for known in _FEISHU_KNOWN_DEVICES:
        if _re.search(rf'\b{_re.escape(known)}\b', doc_text, _re.IGNORECASE):
            params.setdefault("_doc_device", known)
            break
    # 版本号
    vers = _re.findall(r'\b(\d+\.\d+\.\d+(?:\.\d+)?)\b', doc_text)
    if vers:
        params["_doc_versions"] = vers
        params.setdefault("_doc_ver", vers[-1])  # 最后一个通常是当前版本
    # 算法 tag
    tag_m = _re.search(r'(?:算法\s*tag|alg?o\s*tag|TAG)\s*[:：]?\s*(\S+)', doc_text, _re.IGNORECASE)
    if tag_m: params["_doc_tag"] = tag_m.group(1)
    # 分支/发版分支
    branch_m = _re.search(r'(?:发版分支|分支|branch)\s*[-:：]+\s*(\S+)', doc_text, _re.IGNORECASE)
    if branch_m: params["_doc_branch"] = branch_m.group(1)
    # boot/recovery/fct
    for suffix, key in [('bootloader', 'boot'), ('recovery', 'recovery'), ('fct', 'fct')]:
        m = _re.search(rf'\b(\S+-{suffix}-[\d.]+)\b', doc_text, _re.IGNORECASE)
        if m: params[f"_doc_{key}"] = m.group(1)
    # URL
    urls = _re.findall(r'(https?://jenkins[^\s]+)', doc_text)
    if urls:
        params["_doc_urls"] = urls
    return params
    """触发 TSCAN 扫描"""
    try:
        from lark_cli_adapter import lark_send_message
        cfg_path = SCRIPT_DIR / f"gqf_{device}.json"
        if not cfg_path.exists():
            lark_send_message(chat_id=chat_id, markdown=f"❌ 设备 `{device}` 的配置文件不存在")
            return
        tag = params.get("tag", "")
        if not tag:
            lark_send_message(chat_id=chat_id, markdown="❌ 缺少算法 tag")
            return
        lark_send_message(chat_id=chat_id, markdown=f"🔄 正在触发 **{device}** TSCAN...")
        with app.test_client() as client:
            resp = client.post(f"/api/projects/{device}/tscan-only", json={
                "device": device, "tag_algo": tag,
                "user_id": f"feishu_bot_{chat_id}",
            }, content_type="application/json")
            data = resp.get_json()
            if data.get("ok"):
                lark_send_message(chat_id=chat_id, markdown=f"✅ TSCAN 已触发！\n任务 ID：`{data.get('task_id','')}`")
            else:
                lark_send_message(chat_id=chat_id, markdown=f"❌ 失败：{data.get('error','')}")
    except Exception as e:
        import traceback; traceback.print_exc()
        _feishu_reply_message(chat_id, f"❌ 异常：{str(e)[:200]}")


def _do_trigger_skip_jenkins(chat_id, device, params, open_id=""):
    """跳过 Jenkins，直接用已有 URL 生成文档"""
    try:
        from lark_cli_adapter import lark_send_message
        release_url = params.get("release_url", "")
        debug_url = params.get("debug_url", "") or release_url
        ver = params.get("ver", "")
        if not release_url:
            lark_send_message(chat_id=chat_id, markdown="❌ 缺少 Jenkins Build URL")
            return
        lark_send_message(chat_id=chat_id, markdown=f"🔄 正在为 **{device}** 生成文档...")
        with app.test_client() as client:
            body = {
                "release_jenkins_url": release_url, "debug_jenkins_url": debug_url,
                "release": {"project": device, "version": ver, "device_name": device, "stage": "release"},
                "vars": {"tag_algo": params.get("tag", "")},
                "user_id": f"feishu_bot_{chat_id}",
            }
            if open_id:
                body["open_id"] = open_id
            resp = client.post(f"/api/projects/{device}/run-pipeline-direct", json=body, content_type="application/json")
            data = resp.get_json()
            if data.get("ok"):
                lark_send_message(chat_id=chat_id, markdown=f"✅ 文档生成已触发！\n任务 ID：`{data.get('task_id','')}`")
            else:
                lark_send_message(chat_id=chat_id, markdown=f"❌ 失败：{data.get('error','')}")
    except Exception as e:
        import traceback; traceback.print_exc()
        _feishu_reply_message(chat_id, f"❌ 异常：{str(e)[:200]}")


@app.route("/api/feishu/event", methods=["POST"])
def api_feishu_event():
    """飞书事件订阅回调端点"""
    print(f"[FeishuBot] Webhook received event")  # 无条件日志
    try:
        body = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "Invalid JSON"}), 400

    # URL 验证（飞书首次配置回调地址时发送 challenge）
    if body.get("type") == "url_verification" or "challenge" in body:
        challenge = body.get("challenge", "")
        print(f"[FeishuBot] URL 验证: challenge={challenge[:20]}...")
        return jsonify({"challenge": challenge})

    # 事件处理
    header = body.get("header", {})
    event_type = header.get("event_type", "")
    event = body.get("event", {})
    message = event.get("message", {})

    if event_type == "im.message.receive_v1":
        chat_id = message.get("chat_id", "")
        chat_type = message.get("chat_type", "")
        content_str = message.get("content", "{}")
        try:
            content = json.loads(content_str)
            text = content.get("text", "").strip()
        except (json.JSONDecodeError, TypeError):
            text = ""

        # 提取发送者的 open_id，记录 open_id → chat_id 映射（用于个人通知）
        sender = event.get("sender", {})
        sender_id = sender.get("sender_id", {})
        open_id = str(sender_id.get("open_id") or "").strip()
        if open_id and chat_id:
            _record_user_identity(open_id=open_id, chat_id=chat_id)

        # 群聊中只响应 @机器人的消息；私聊中响应所有消息
        is_group = (chat_type == "group")
        mentions = message.get("mentions") or []
        bot_mentioned = any(
            str(m.get("mentioned_type", "")).strip() == "bot"
            for m in mentions
            if isinstance(m, dict)
        )
        if is_group and not bot_mentioned:
            print(f"[FeishuBot] 群聊未 @机器人，忽略: chat={chat_id}")
            return jsonify({"ok": True})

        print(f"[FeishuBot] 收到消息: chat={chat_id} text={text}")
        _process_feishu_message(chat_id, text, message, open_id=open_id)
        return jsonify({"ok": True})

    # 其他事件类型暂不处理
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════
# 飞书消息处理（共享逻辑：Webhook + WebSocket 共用）
# ══════════════════════════════════════════════════════

USER_IDENTITY_FILE = SCRIPT_DIR / "output" / "user_identities.json"

# ── 绑定码：Web 界面自动获取飞书用户 open_id ──
import random as _random
import string as _string
_bind_codes: Dict[str, Dict[str, Any]] = {}  # {code: {open_id, chat_id, ts}}

def _gen_bind_code() -> str:
    """生成 4 位字母+数字绑定码"""
    for _ in range(10):
        code = ''.join(_random.choices(_string.ascii_uppercase + _string.digits, k=4))
        if code not in _bind_codes:
            return code
    return 'X' + ''.join(_random.choices(_string.ascii_uppercase + _string.digits, k=3))

# 清理过期绑定码（5分钟）
def _cleanup_bind_codes():
    import time as _time
    now = _time.time()
    expired = [c for c, v in _bind_codes.items() if now - v.get("ts", 0) > 300]
    for c in expired:
        del _bind_codes[c]


def _record_user_identity(*, open_id: str, chat_id: str):
    """记录飞书用户 open_id → chat_id 映射（用于个人通知）"""
    identities: Dict[str, Any] = {}
    try:
        if USER_IDENTITY_FILE.exists():
            identities = json.loads(USER_IDENTITY_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    if not isinstance(identities, dict):
        identities = {}
    changed = False
    if identities.get(open_id, {}).get("chat_id") != chat_id:
        identities.setdefault(open_id, {})["chat_id"] = chat_id
        changed = True
    if changed:
        USER_IDENTITY_FILE.parent.mkdir(parents=True, exist_ok=True)
        USER_IDENTITY_FILE.write_text(json.dumps(identities, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def _lookup_chat_id_from_open_id(open_id: str) -> str:
    """从 open_id 查找对应的 chat_id"""
    if not open_id:
        return ""
    try:
        if USER_IDENTITY_FILE.exists():
            identities = json.loads(USER_IDENTITY_FILE.read_text(encoding="utf-8"))
            return str((identities.get(open_id) or {}).get("chat_id", "") or "")
    except Exception:
        pass
    return ""


def _process_feishu_message(chat_id: str, text: str, message=None, open_id: str = ""):
    """处理飞书消息的核心逻辑（Webhook 和 WebSocket 共用）"""

    # 只在被 @、私聊、或已有待确认任务时响应
    is_mentioned = re.search(r'@_user_\d+', text)
    is_pending = chat_id in _pending_builds
    is_p2p = False
    if message:
        try:
            is_p2p = (message.get("chat_type", "") == "p2p")
        except Exception:
            pass

    # ── 绑定码识别：用户私聊 Bot 发送 4 位码完成身份绑定 ──
    bind_code = text.strip().upper()
    if is_p2p and len(bind_code) == 4 and bind_code in _bind_codes and open_id:
        _bind_codes[bind_code]["open_id"] = open_id
        _bind_codes[bind_code]["chat_id"] = chat_id
        _feishu_reply_message(chat_id, "✅ 身份绑定成功！\n\n现在回到 Web 界面，你的通知 ID 将自动填入，无需手动操作。")
        return

    # ── 个人身份查询（任何用户私聊 Bot 均可使用） ──
    if is_p2p and text and re.search(r'(我的.?[iI][dD]|open.?id|通知.?[iI][dD]|绑定|identify)', text):
        my_open_id = open_id or "未知"
        my_chat_id = chat_id or "未知"
        _feishu_reply_message(chat_id,
            f"👤 你的飞书身份信息：\n\n"
            f"open_id: `{my_open_id}`\n"
            f"chat_id: `{my_chat_id}`\n\n"
            f"💡 在 Web 界面右上角设置「飞书通知 open_id」为上面的 open_id，即可收到个人通知。")
        return

    if not (is_mentioned or is_pending or is_p2p):
        return
    print(f"[FeishuBot] 消息放行: mentioned={is_mentioned} pending={is_pending} p2p={is_p2p} text={text[:50]}...")

    # ── 尝试读取消息中引用的文档 ──
    doc_text = _try_read_doc_from_message(message) if message else None
    if doc_text:
        print(f"[FeishuBot] 文档内容: {doc_text[:100]}...")
        doc_params = _extract_params_from_doc(doc_text)
        # 将文档提取的参数合并到文本末尾，供解析器使用
        doc_extra = []
        if doc_params.get("_doc_device"):
            doc_extra.append(f"设备{doc_params['_doc_device']}")
        if doc_params.get("_doc_ver"):
            doc_extra.append(f"版本{doc_params['_doc_ver']}")
        if doc_params.get("_doc_tag"):
            doc_extra.append(f"算法tag {doc_params['_doc_tag']}")
        if doc_params.get("_doc_boot"):
            doc_extra.append(f"boot {doc_params['_doc_boot']}")
        if doc_params.get("_doc_recovery"):
            doc_extra.append(f"recovery {doc_params['_doc_recovery']}")
        if doc_params.get("_doc_fct"):
            doc_extra.append(f"fct {doc_params['_doc_fct']}")
        if doc_params.get("_doc_branch"):
            doc_extra.append(f"分支 {doc_params['_doc_branch']}")
        if doc_params.get("_doc_urls"):
            for u in doc_params["_doc_urls"]:
                doc_extra.append(u)
        if doc_extra:
            text = text + " " + " ".join(doc_extra)
            print(f"[FeishuBot] 合并文档参数后: {text[:200]}...")

    # ── Debug 版本 A/B 选择 ──
    # 支持：纯 "A"/"B"、"Debug B"、"选择B" 等，同时提取补充参数
    pending_build = _pending_builds.get(chat_id)
    if pending_build and pending_build.get("intent") == "build":
        ab_match = re.search(r'(?<!\w)(A|B|a|b)(?!\w)', text.strip(), re.IGNORECASE)
        if ab_match:
            choice = ab_match.group(1).upper()
            # 计算 A/B 对应的 debug 版本（A=老项目规则第三位高一位, B=新版本号规则 pairRule 第四位成双成对，均为异步并行）
            ver = pending_build["params"].get("ver", "")
            parts = ver.split('.')
            if len(parts) >= 4:
                opt_a = '.'.join([parts[0], parts[1], str(int(parts[2]) + 1), '1'])
                opt_b = '.'.join(parts[:3] + [str(int(parts[3]) + 1)])
            else:
                opt_a = '.'.join([parts[0], parts[1], str(int(parts[2]) + 1), '1']) if len(parts) >= 3 else ''
                opt_b = ver + '.2' if len(parts) == 3 else (ver + '.2')

            pending_build["params"]["_debug_choice"] = choice
            pending_build["params"]["debug"] = opt_a if choice == 'A' else opt_b
            pending_build["missing"] = [m for m in pending_build["missing"] if 'Debug' not in m]

            # 同消息中提取补充参数（正则 + LLM 双路解析）
            parsed = _parse_any_command(text)
            # LLM 辅助：当 pending 需要补充参数时，用 AI 提升解析准确度
            try:
                llm_cfg = _load_llm_config()
                if llm_cfg.get("enabled") and llm_cfg.get("api_key"):
                    llm_result = _parse_with_llm(text, _FEISHU_KNOWN_DEVICES, list(_FEISHU_KNOWN_DEVICES))
                    if llm_result.get("params"):
                        for k, v in llm_result["params"].items():
                            if v and not parsed["params"].get(k):
                                parsed["params"][k] = v
                        print(f"[LLM] 补充参数合并: {llm_result['params']}")
            except Exception:
                pass
            for k, v in parsed.get("params", {}).items():
                if v and not pending_build["params"].get(k):
                    pending_build["params"][k] = v
            if parsed.get("device") and not pending_build.get("device"):
                pending_build["device"] = parsed["device"]

            pending_build["missing"] = _get_missing_for_intent(pending_build["intent"], pending_build["device"], pending_build["params"])
            card = _build_confirm_card(pending_build["intent"], pending_build["device"], pending_build["params"], pending_build["missing"])
            _feishu_reply_message(chat_id, card)
            return

    # ── 确认 / 取消回复 ──
    if re.match(r'^(确认|确认执行|执行|yes|ok|y|好|可以|行|对)\b', text, re.IGNORECASE):
        pending = _pending_builds.pop(chat_id, None)
        if pending:
            import threading
            intent = pending.get("intent", "build")
            def _trigger():
                if intent == "build":
                    ver = pending["params"].get("ver", "")
                    _do_trigger_build(chat_id, pending["device"], ver, pending["params"], open_id)
                elif intent == "changelog":
                    _do_trigger_changelog(chat_id, pending["device"], pending["params"])
                elif intent == "tscan":
                    _do_trigger_tscan(chat_id, pending["device"], pending["params"])
                elif intent == "skip_jenkins":
                    _do_trigger_skip_jenkins(chat_id, pending["device"], pending["params"], open_id)
            threading.Thread(target=_trigger, daemon=True).start()
        else:
            _feishu_reply_message(chat_id, "🤔 没有待确认的任务，请先描述你想做什么。")
        return

    if re.match(r'^(取消|算了|不要|no|cancel)\b', text, re.IGNORECASE):
        removed = _pending_builds.pop(chat_id, None)
        _feishu_reply_message(chat_id, "👌 已取消。" if removed else "当前没有待确认的任务。")
        return

    # ── 解析命令（新智能解析器）──
    parsed = _parse_any_command(text)
    intent = parsed["intent"]
    device = parsed["device"]
    params = parsed["params"]
    missing = parsed["missing"]

    # ── 如果有待确认任务且用户补充了参数 ──
    existing = _pending_builds.get(chat_id)
    if existing and existing.get("intent") and device:
        # LLM 辅助解析补充参数
        try:
            llm_cfg = _load_llm_config()
            if llm_cfg.get("enabled") and llm_cfg.get("api_key"):
                llm_result = _parse_with_llm(text, _FEISHU_KNOWN_DEVICES, list(_FEISHU_KNOWN_DEVICES))
                if llm_result.get("params"):
                    for k, v in llm_result["params"].items():
                        if v and not params.get(k):
                            params[k] = v
                    print(f"[LLM] 补充参数合并: {llm_result['params']}")
        except Exception:
            pass
        # 合并补充的参数
        for k, v in params.items():
            if v and not existing["params"].get(k):
                existing["params"][k] = v
        if device and not existing.get("device"):
            existing["device"] = device
        # 重新评估 missing
        existing["missing"] = _get_missing_for_intent(existing["intent"], existing["device"], existing["params"])
        card = _build_confirm_card(existing["intent"], existing["device"], existing["params"], existing["missing"])
        _feishu_reply_message(chat_id, card)
        return

    if intent == "unknown" or not device:
        devices_hint = ", ".join(sorted(_FEISHU_KNOWN_DEVICES)[:10]) + "..."
        _feishu_reply_message(chat_id, (
            f"🤖 你好！我是发版助手，可以帮你：\n\n"
            f"📦 **打版本** — `打版本 geneva 3.15.0 算法tag xxx`\n"
            f"📊 **Changelog 对比** — `对比 geneva 的 3.14.0.1 和 3.15.0.1`\n"
            f"🔍 **TSCAN** — `对 geneva 做 TSCAN 算法tag xxx`\n"
            f"📄 **直接生成文档** — `用这两个 URL 给 geneva 生成文档 https://... https://...`\n\n"
            f"支持设备：{devices_hint}"
        ))
        return

    # ── 缺参反问 ──
    if missing:
        card = _build_confirm_card(intent, device, params, missing)
        _pending_builds[chat_id] = {"intent": intent, "device": device, "params": params, "missing": missing}
        _feishu_reply_message(chat_id, card)
        return

    # ── 参数齐全，展示确认卡片 ──
    card = _build_confirm_card(intent, device, params, [])
    _pending_builds[chat_id] = {"intent": intent, "device": device, "params": params, "missing": []}
    _feishu_reply_message(chat_id, card)


# ══════════════════════════════════════════════════════
# 智能助手辅助函数
# ══════════════════════════════════════════════════════

def _add_missing_if(missing: list, label: str):
    """仅在 label 不在 missing 中时追加，防止重复"""
    if label not in missing:
        missing.append(label)


def _get_missing_for_intent(intent, device, params):
    """根据意图返回仍缺失的必要参数"""
    missing = []
    if not device:
        missing.append("设备名")
    if intent == "build":
        if not params.get("ver"): missing.append("版本号")
    elif intent == "changelog":
        if not params.get("prev"): missing.append("旧版本号")
        if not params.get("curr"): missing.append("新版本号")
    elif intent == "tscan":
        if not params.get("tag"): missing.append("算法tag")
    elif intent == "skip_jenkins":
        if not params.get("release_url"): missing.append("Jenkins Build URL")
    return missing


def _build_confirm_card(intent, device, params, missing):
    """构建飞书交互式卡片，返回卡片 JSON dict。
    
    不再返回 markdown 字符串，而是返回 Feishu IM Card 格式的 dict，
    由 _feishu_reply_message 检测类型后发送。
    """
    import json as _json

    intent_names = {
        "build": "发版构建", "changelog": "Changelog 差分对比",
        "tscan": "TSCAN 扫描", "skip_jenkins": "直接生成文档（跳过编译）"
    }
    title = intent_names.get(intent, intent)

    # ── 读取设备配置 ──
    cfg_path = SCRIPT_DIR / f"gqf_{device}.json" if device else None
    cfg_def = {}
    if not (cfg_path and cfg_path.exists()):
        import glob
        candidates = sorted([
            p for p in glob.glob(str(SCRIPT_DIR / f"gqf_{device}_*.json"))
            if not p.endswith('.bak')
        ])
        if candidates:
            cfg_path = Path(candidates[0])
    if cfg_path and cfg_path.exists():
        try:
            c = _json.loads(cfg_path.read_text(encoding="utf-8"))
            v = c.get("vars", {})
            r = c.get("release", {})
            j = c.get("jenkins", {})
            cfg_def["stage"] = r.get("stage", "")
            cfg_def["branch"] = r.get("notes", "")
            cfg_def["tag"] = v.get("tag", "")
            cfg_def["boot"] = v.get("boot_tag", "")
            cfg_def["recovery"] = v.get("recovery_tag", "")
            cfg_def["fct"] = v.get("fct_tag", "")
            cfg_def["build_mode"] = v.get("build_mode", "")
            cfg_def["auto_bind"] = (j.get("triggers", {}).get("release", {}).get("parameters", {}).get("AUTH_CFG_AUTO_BINDING", "") or "").strip().upper()
            tscan_yes = (j.get("triggers", {}).get("Tscan", {}).get("parameters", {}).get("TSCANCODE_CHECK", "") or "").strip().upper()
            release_tscan = (j.get("triggers", {}).get("release", {}).get("parameters", {}).get("TSCANCODE_CHECK", "") or "").strip().upper()
            cfg_def["build_tscan"] = "YES" if (tscan_yes == "YES" or release_tscan == "YES") else ""
        except Exception:
            pass

    # ── helper: 未配置标黄色 ──
    def _na(val: str) -> str:
        return f"<font color='orange'>{val}</font>" if val == '未配置' else val

    # ── helper: 正常值标灰色 ──
    def _val(v) -> str:
        return f"<font color='grey'>{v}</font>" if v else v

    # ── helper: 构建一个 markdown element ──
    def _md(content: str) -> dict:
        return {"tag": "markdown", "content": content}

    # ── helper: 构建两列 ──
    def _cols(left: str, right: str) -> dict:
        return {
            "tag": "column_set", "flex_mode": "bisect",
            "columns": [
                {"tag": "column", "width": "weighted", "weight": 1,
                 "elements": [_md(left)]},
                {"tag": "column", "width": "weighted", "weight": 1,
                 "elements": [_md(right)]},
            ]
        }

    def _hr() -> dict:
        return {"tag": "hr"}

    def _note(content: str) -> dict:
        return {"tag": "note", "elements": [{"tag": "plain_text", "content": content}]}

    # ── 组装 elements ──
    elements = []  # 设备名放入 header 标题

    # ═══════════ build ═══════════
    if intent == "build":
        ver = params.get("ver", "")
        parts = ver.split('.')
        if len(parts) >= 4:
            opt_a = '.'.join([parts[0], parts[1], str(int(parts[2]) + 1), '1'])
            opt_b = '.'.join(parts[:3] + [str(int(parts[3]) + 1)])
        else:
            opt_a = '.'.join([parts[0], parts[1], str(int(parts[2]) + 1), '1']) if len(parts) >= 3 else '?.?.1.1'
            opt_b = '.'.join(parts[:3] + ['2']) if len(parts) == 3 else (ver + '.2')
        debug_ver = params.get("debug", "")
        choice = params.get("_debug_choice", "")

        # 版本 + Debug
        if not (choice or debug_ver):
            elements.append(_md(
                f"**Release**：{_val(ver)}\n\n"
                f"**Debug**：{_val('-')}\n▸ 选择 debug 版本号规则（均为异步并行）：\n"
                f"　　<font color='blue'>**A**</font> 老项目规则 {_val(opt_a)}（第三位高一位）\n"
                f"　　<font color='blue'>**B**</font> 新版本号规则 pairRule {_val(opt_b)}（第四位成双成对）\n"
                f"回复 <font color='blue'>**A**</font> 或 <font color='blue'>**B**</font>"
            ))
            missing.append("Debug 版本：（回复A或B）")
        else:
            sel = choice or ('A' if debug_ver == opt_a else 'B')
            rule_label = '老项目规则（第三位高一位）' if sel == 'A' else '新版本号规则 pairRule（第四位成双成对）'
            elements.append(_md(f"**Release**：{_val(ver)}\n**Debug**：{_val(debug_ver)}　{rule_label}　（{sel}）"))
        elements.append(_hr())

        # 基本信息（单行 key:value）
        stage_val = params.get("stage") or cfg_def.get("stage", "")
        branch_val = params.get("branch") or cfg_def.get("branch", "")
        elements.append(_md(f"**阶段**：{_val(stage_val) if stage_val else _na('未配置')}"))
        elements.append(_md(f"**分支**：{_val(branch_val) if branch_val else _na('未配置')}"))

        # TAG（每项独立一行）
        tag_val = params.get("tag") or cfg_def.get("tag", "")
        boot_val = params.get("boot") or cfg_def.get("boot", "")
        recovery_val = params.get("recovery") or cfg_def.get("recovery", "")
        fct_val = params.get("fct") or cfg_def.get("fct", "")
        elements.append(_md(f"**tag**：{_val(tag_val) if tag_val else _na('未配置')}"))
        elements.append(_md(f"**boot**：{_val(boot_val) if boot_val else _na('未配置')}"))
        elements.append(_md(f"**recovery**：{_val(recovery_val) if recovery_val else _na('未配置')}"))
        elements.append(_md(f"**fct**：{_val(fct_val) if fct_val else _na('未配置')}"))

        # changelog对比版本
        diff_val = params.get("diff", "")
        elements.append(_hr())
        elements.append(_md(f"**changelog对比版本**：{_val(diff_val) if diff_val else _na('未配置')}"))

        # 构建内容（每项独立一行）
        build_mode = params.get("build_mode") or cfg_def.get("build_mode", "")
        tscan_val = (params.get("build_tscan") or cfg_def.get("build_tscan", "") or "NO").strip().upper()
        tscan_val = tscan_val if tscan_val in ("YES", "NO") else "NO"
        auto_bind_val = "YES" if (device and device.lower() in {"stuttgart", "toulouse", "toulouseh", "galaxy"}) else (
            (params.get("auto_bind") or cfg_def.get("auto_bind", "") or "NO").upper())
        auto_bind_val = auto_bind_val if auto_bind_val in ("YES", "NO") else "NO"
        bind_note = "（必选）" if auto_bind_val == "YES" else ""
        elements.append(_md(
            f"**组件**：{_val(build_mode or 'OTA,BOOT,RECOVERY')}\n"
            f"**自动绑定**：{_val(auto_bind_val)} {bind_note}\n"
            f"**构建 TSCAN**：{_val(tscan_val)}"
        ))

        # 全面缺失检查
        if not stage_val:
            _add_missing_if(missing, "阶段")
        if not branch_val:
            _add_missing_if(missing, "分支：（请输入 发版分支）")
        if not tag_val:
            _add_missing_if(missing, "tag")
        if not diff_val:
            _add_missing_if(missing, "changelog对比版本：（请输入 要对比的发版号）")

    # ═══════════ changelog ═══════════
    elif intent == "changelog":
        tag_val = params.get("tag") or cfg_def.get("tag", "")
        elements.append(_md(f"**旧版本**：{params.get('prev', '?')}\n**新版本**：{params.get('curr', '?')}\n**算法 tag**：{tag_val or _na('未配置')}"))

    # ═══════════ tscan ═══════════
    elif intent == "tscan":
        tag_val = params.get("tag") or cfg_def.get("tag", "")
        boot_val = params.get("boot") or cfg_def.get("boot", "")
        recovery_val = params.get("recovery") or cfg_def.get("recovery", "")
        fct_val = params.get("fct") or cfg_def.get("fct", "")
        elements.append(_md(f"**tag**：{tag_val or _na('未配置')}　**boot**：{boot_val or _na('未配置')}"))
        elements.append(_md(f"**recovery**：{recovery_val or _na('未配置')}　**fct**：{fct_val or _na('未配置')}"))
        if not tag_val:
            _add_missing_if(missing, "tag")

    # ═══════════ skip_jenkins ═══════════
    elif intent == "skip_jenkins":
        tag_val = params.get("tag") or cfg_def.get("tag", "")
        boot_val = params.get("boot") or cfg_def.get("boot", "")
        parts = [f"**版本**：{params.get('ver', '?')}"]
        if params.get("release_url"):
            parts.append(f"**Release**：{params['release_url'][:50]}...")
        if params.get("debug_url"):
            parts.append(f"**Debug**：{params['debug_url'][:50]}...")
        elements.append(_md("\n".join(parts)))
        elements.append(_md(f"**tag**：{tag_val or _na('未配置')}　**boot**：{boot_val or _na('未配置')}"))

    # ── 底部 ──
    if missing:
        items = "\n".join(f"• {m}" for m in missing)
        elements.append(_hr())
        elements.append(_md(f"⚠ **以下信息待确认：**\n{items}"))
    else:
        elements.append(_hr())
        elements.append(_md("✅ 回复 <font color='green'>**确认**</font> 执行　·　❌ 回复 <font color='red'>**取消**</font> 放弃"))

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"{device} · {title} 参数确认"},
            "template": "blue"
        },
        "elements": elements
    }


# ══════════════════════════════════════════════════════
# 数据统计 API
# ══════════════════════════════════════════════════════

@app.route("/api/stats")
def api_stats():
    """返回统计数据（汇总）"""
    stats = _load_stats()
    events = stats.get("events", [])
    summary = {"total": len(events), "by_type": {}}
    for e in events:
        t = e.get("type", "unknown")
        summary["by_type"][t] = summary["by_type"].get(t, 0) + 1
    return jsonify({"ok": True, "summary": summary})

@app.route("/api/stats/events")
def api_stats_events():
    """返回事件列表（最新 200 条）"""
    stats = _load_stats()
    events = stats.get("events", [])
    events_sorted = sorted(events, key=lambda e: e.get("time", ""), reverse=True)[:200]
    for e in events_sorted:
        e["type_cn"] = EVENT_TYPE_MAP.get(e.get("type", ""), e.get("type", ""))
    return jsonify({"ok": True, "events": events_sorted})


@app.route("/<path:filename>")
def serve_static(filename):
    """提供 templates 目录下的静态文件（style.css, script.js 等）"""
    return send_from_directory(SCRIPT_DIR / "templates", filename)


@app.route("/api/auth/verify", methods=["POST"])
def api_auth_verify():
    """验证 Jenkins 凭据是否有效"""
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "error": "Invalid JSON"}), 400

    username = (data.get("username") or "").strip()
    token = (data.get("token") or "").strip()
    if not username or not token:
        return jsonify({"ok": False, "error": "账号和密码/Token 不能为空"}), 400

    # 尝试用凭据访问 Jenkins API 验证
    try:
        session = requests.Session()
        session.auth = (username, token)
        # 用 Jenkins 根路径的 whoAmI API 验证
        r = session.get("https://jenkins.huami.com/me/api/json", timeout=10, verify=False)
        if r.status_code == 200:
            return jsonify({"ok": True, "username": username})
        else:
            return jsonify({"ok": False, "error": f"Jenkins 验证失败 (HTTP {r.status_code})"})
    except Exception as e:
        return jsonify({"ok": False, "error": f"无法连接 Jenkins: {e}"})


@app.route("/api/projects")
def api_projects():
    """列出所有可用项目（从 gqf_*.json 发现）"""
    projects = []
    for f in sorted(SCRIPT_DIR.glob("gqf_*.json")):
        name = _parse_project_name(f.name)
        if not name:
            continue
        # 跳过 bak 和 tmp 文件
        if ".bak." in f.name or ".tmp." in f.name:
            continue
        try:
            cfg = json.loads(f.read_text(encoding="utf-8"))
            rel = cfg.get("release") or {}
            projects.append({
                "name": name,
                "project": rel.get("project", name),
                "device_name": rel.get("device_name", ""),
                "version": rel.get("version", ""),
                "stage": rel.get("stage", ""),
                "file": f.name,
            })
        except Exception:
            projects.append({
                "name": name,
                "project": name,
                "device_name": "",
                "version": "?",
                "stage": "?",
                "file": f.name,
            })
    return jsonify({"ok": True, "projects": projects})


# ── Jenkins 产品列表（从 HuamiOS_HS3 构建页抓取，定期手动更新）──
JENKINS_PRODUCTS = [
    "matterhorn", "gt6", "gt6_evb", "milan", "milan_evb",
    "seattle", "pamir", "lyon", "rome", "milan_os5",
    "milan_64m", "pamir_64m", "rome_64m", "munich", "warsaw",
    "rocky", "milan_golf", "rimo", "geneva", "makalu",
    "cologne", "windermere", "oslo", "pike", "dublin",
]


# ── 新建项目默认模板（对标 gqf_geneva_ss.json 结构）──
def _build_new_project_template(project_name: str) -> OrderedDict:
    """生成新建项目的最小配置模板，结构完全对标 gqf_geneva_ss.json。
    公共配置（jenkins.auth、prepare、doc、notifications、nas.webdav/dsm/uploads 等）
    由 common.json 通过 base_config 继承，不写入项目配置文件。
    """
    import re as _re
    base_name = _re.sub(r"_\d+m?$", "", project_name)
    display_name = base_name.capitalize()

    cfg = OrderedDict()
    cfg["base_config"] = "common.json"

    cfg["release"] = OrderedDict([
        ("project", project_name),
        ("device_name", project_name),
        ("stage", ""),
        ("version", ""),
        ("variant", ""),
        ("notes", ""),
    ])

    cfg["vars"] = OrderedDict([
        ("tag", ""),
        ("boot_tag", ""),
        ("recovery_tag", ""),
        ("fct_tag", ""),
        ("release_version_name", ""),
        ("debug_version_name", ""),
        ("fct_version_name", ""),
        ("prev_version_name", ""),
        ("prev_version_code", ""),
        ("build_mode", "OTA,OTA_SLEEP,FCT,BOOT,RECOVERY"),
    ])

    cfg["changelog"] = OrderedDict([
        ("jenkins_job_url", "https://jenkins.huami.com/job/DownStream/job/CMP-JIRA-GIT"),
        ("build_url", ""),
        ("build_number", 0),
        ("status", ""),
        ("result", ""),
        ("triggered_at", ""),
        ("updated_at", ""),
    ])

    cfg["jenkins"] = OrderedDict()
    cfg["jenkins"]["triggers"] = OrderedDict([
        ("job_url", "https://jenkins.huami.com/job/firmware_auto_trigger/job/HuamiOS_multi_platform"),
        ("modes", "async"),  # "async"=Release/Debug 并行触发, "sync"=先 Release 后 Debug 串行
        ("release", OrderedDict([
            ("parameters", OrderedDict([
                ("AUTH_CFG_AUTO_BINDING", "NO"),
                ("FW_VER_STRATEGY_ENV", "none"),
            ])),
        ])),
    ])
    cfg["jenkins"]["builds"] = OrderedDict([
        ("debug", OrderedDict([("build_url", "")])),
        ("release", OrderedDict([("build_url", "")])),
        ("tscan", OrderedDict([("build_url", "")])),
    ])

    cfg["nas"] = OrderedDict()
    cfg["nas"]["remote"] = OrderedDict([
        ("base_dir", f"/GT智能手表事业部/软件部/软件版本文档/{display_name}/用户固件"),
    ])

    cfg["feishu"] = OrderedDict([
        ("template_node_token", "RzHFw5ipYiU5ibkMxSfcgUfDnve"),
    ])

    return cfg


@app.route("/api/jenkins/products")
def api_jenkins_products():
    """返回 Jenkins HuamiOS_HS3 构建中可选的 PRODUCT 列表"""
    return jsonify({"ok": True, "products": JENKINS_PRODUCTS, "count": len(JENKINS_PRODUCTS)})


@app.route("/api/config/update-wiki-token", methods=["POST"])
def api_update_wiki_token():
    """将设备→Wiki Token 映射写入 platform_config.js 的 template_node_token"""
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "error": "Invalid JSON"}), 400

    device = (data.get("device") or "").strip()
    token = (data.get("token") or "").strip()
    if not device or not token:
        return jsonify({"ok": False, "error": "device 和 token 都是必填"}), 400

    config_path = SCRIPT_DIR / "static" / "js" / "platform_config.js"
    if not config_path.exists():
        return jsonify({"ok": False, "error": "platform_config.js not found"}), 500

    content = config_path.read_text(encoding="utf-8")
    import re as _re
    pattern = _re.compile(rf'^\s*{_re.escape(device)}\s*:\s*"([^"]*)"\s*,?\s*$', _re.MULTILINE)
    match = pattern.search(content)

    if match:
        existing_token = match.group(1)
        if existing_token == token:
            return jsonify({"ok": True, "message": "Already exists, no change needed", "updated": False})
        content = content[:match.start()] + f'  {device}: "{token}",' + content[match.end():]
    else:
        last_brace = content.rfind("};")
        if last_brace < 0:
            return jsonify({"ok": False, "error": "Cannot find template_node_token closing brace"}), 500
        insert_pos = content.rfind("\n", 0, last_brace)
        if insert_pos < 0:
            insert_pos = last_brace
        content = content[:insert_pos] + f'\n  {device}: "{token}",' + content[insert_pos:]

    config_path.write_text(content, encoding="utf-8")
    return jsonify({"ok": True, "message": f"Saved {device} → {token}", "updated": True})


@app.route("/api/config/add-platform-device", methods=["POST"])
def api_add_platform_device():
    """将设备型号写入 platform_config.js 的 platformDevices 对应平台列表中"""
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "error": "Invalid JSON"}), 400

    platform = (data.get("platform") or "").strip()
    device = (data.get("device") or "").strip()
    if not platform or not device:
        return jsonify({"ok": False, "error": "platform 和 device 都是必填"}), 400

    config_path = SCRIPT_DIR / "static" / "js" / "platform_config.js"
    if not config_path.exists():
        return jsonify({"ok": False, "error": "platform_config.js not found"}), 500

    content = config_path.read_text(encoding="utf-8")
    import re as _re

    # 检查设备是否已存在该平台列表中
    # platformDevices 的结构：  "MHS003": [ "pike", "warsaw", ... ],
    plat_key = _re.escape(platform)
    pattern = _re.compile(
        rf'(^\s*{plat_key}\s*:\s*\[)([^\]]*)(\])',
        _re.MULTILINE
    )
    m = pattern.search(content)
    if not m:
        return jsonify({"ok": False, "error": f"Platform '{platform}' not found in platform_config.js"}), 500

    # 检查是否已存在
    existing = _re.findall(r'"([^"]*)"', m.group(2))
    if device in existing:
        return jsonify({"ok": True, "message": f"Device '{device}' already exists in {platform}", "updated": False})

    # 在列表末尾追加新设备
    prefix = m.group(1) + m.group(2)
    suffix = m.group(3)
    # 在最后一个设备前面加逗号 + 换行
    if existing:
        new_entry = f',\n    "{device}"'
    else:
        new_entry = f'\n    "{device}"'
    new_content = content[:m.start()] + prefix + new_entry + suffix + content[m.end():]

    config_path.write_text(new_content, encoding="utf-8")
    return jsonify({"ok": True, "message": f"Added {device} to {platform}", "updated": True})


@app.route("/api/config/object-branches")
def api_object_branches():
    """返回 objectBranch 数据，可选 ?project=xxx 过滤"""
    config_path = SCRIPT_DIR / "static" / "js" / "platform_config.js"
    if not config_path.exists():
        return jsonify({"ok": False, "error": "platform_config.js not found"}), 500
    import re as _re
    content = config_path.read_text(encoding="utf-8")
    # 提取 objectBranch 对象（允许跨行）
    m = _re.search(r'const\s+objectBranch\s*=\s*(\{[^}]+\})', content, _re.DOTALL)
    if not m:
        return jsonify({"ok": True, "data": {}, "project": request.args.get("project", "")})
    try:
        # JS 对象语法 → JSON：key 加引号 + 去掉尾随逗号
        obj_str = m.group(1)
        obj_str = _re.sub(r'(\s*)([a-zA-Z_]\w*)(\s*:)', r'\1"\2"\3', obj_str)
        obj_str = _re.sub(r',(\s*[}\]])', r'\1', obj_str)
        obj = json.loads(obj_str)
    except Exception:
        return jsonify({"ok": False, "error": "Failed to parse objectBranch"}), 500
    proj = (request.args.get("project") or "").strip()
    if proj:
        branches = obj.get(proj, [])
        return jsonify({"ok": True, "project": proj, "branches": branches if isinstance(branches, list) else []})
    return jsonify({"ok": True, "data": obj})


@app.route("/api/config/add-object-branch", methods=["POST"])
def api_add_object_branch():
    """向 objectBranch 中指定项目追加一条分支记录"""
    try:
        body = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "error": "Invalid JSON"}), 400
    project_name = (body.get("project") or "").strip()
    branch_name = (body.get("branch") or "").strip()
    if not project_name or not branch_name:
        return jsonify({"ok": False, "error": "project 和 branch 都是必填"}), 400

    config_path = SCRIPT_DIR / "static" / "js" / "platform_config.js"
    if not config_path.exists():
        return jsonify({"ok": False, "error": "platform_config.js not found"}), 500

    content = config_path.read_text(encoding="utf-8")
    import re as _re

    # 找到 objectBranch 对象的花括号范围
    start_m = _re.search(r'const\s+objectBranch\s*=\s*\{', content)
    if not start_m:
        return jsonify({"ok": False, "error": "objectBranch not found in platform_config.js"}), 500

    # 从 start 位置找到匹配的 }
    brace_start = start_m.end() - 1  # 指向 {
    depth = 0
    brace_end = -1
    for i in range(brace_start, len(content)):
        if content[i] == '{':
            depth += 1
        elif content[i] == '}':
            depth -= 1
            if depth == 0:
                brace_end = i
                break
    if brace_end < 0:
        return jsonify({"ok": False, "error": "Cannot find closing brace of objectBranch"}), 500

    obj_str = content[brace_start:brace_end + 1]
    # JS 对象语法 → JSON：key 加引号 + 去掉尾随逗号
    import re as _re2
    obj_str = _re2.sub(r'(\s*)([a-zA-Z_]\w*)(\s*:)', r'\1"\2"\3', obj_str)
    obj_str = _re2.sub(r',(\s*[}\]])', r'\1', obj_str)
    try:
        obj = json.loads(obj_str)
    except Exception:
        return jsonify({"ok": False, "error": "Failed to parse objectBranch JSON"}), 500

    if not isinstance(obj, dict):
        return jsonify({"ok": False, "error": "objectBranch is not an object"}), 500

    existing = obj.get(project_name, [])
    if not isinstance(existing, list):
        existing = []
    if branch_name in existing:
        return jsonify({"ok": True, "message": "Branch already exists", "added": False})

    existing.append(branch_name)
    obj[project_name] = existing

    # 重新序列化并写回
    new_obj_str = json.dumps(obj, ensure_ascii=False, indent=2)
    # 调整缩进以匹配原文件风格（2空格缩进行 + 外层2空格）
    new_content = (
        content[:brace_start]
        + new_obj_str
        + content[brace_end + 1:]
    )
    config_path.write_text(new_content, encoding="utf-8")
    return jsonify({"ok": True, "message": f"Added {branch_name} to {project_name}", "added": True, "project": project_name, "branches": existing})


def _get_jenkins_platform_devices(platform: str) -> List[str]:
    normalized = platform.strip().lower()
    if normalized in ('nxp595', 'apollo4', 'nxp595/apollo4'):
        url = 'https://jenkins.huami.com/job/firmware_auto_trigger/job/HuamiOS/build?delay=0sec'
    elif normalized in ('mhs003', 'mhs003s'):
        url = 'https://jenkins.huami.com/job/firmware_auto_trigger/job/HuamiOS_multi_platform/build?delay=0sec'
    else:
        return []

    session = requests.Session()
    username = os.environ.get('JENKINS_USERNAME', '').strip()
    password = os.environ.get('JENKINS_PASSWORD', '').strip()
    if username and password:
        session.auth = (username, password)

    try:
        response = session.get(url, timeout=20, verify=False)
    except Exception:
        return []

    if response.status_code not in (200, 201):
        return []

    html = response.text
    select_match = re.search(r'<select[^>]+name=["\']PRODUCT["\'][^>]*>(.*?)</select>', html, re.S | re.I)
    options_html = select_match.group(1) if select_match else html
    option_matches = re.findall(r'<option[^>]+value=["\']([^"\']+)["\'][^>]*>(.*?)</option>', options_html, re.S | re.I)
    devices = []
    for value, _ in option_matches:
        value = value.strip()
        if not value:
            continue
        lower = value.lower()
        if lower in ('', '请选择', 'select', 'all', 'all products', '请选择设备型号'):
            continue
        devices.append(value)

    if not devices:
        fallback = {
            'nxp595': ['matterhorn', 'pamir', 'lyon'],
            'apollo4': ['matterhorn', 'pamir', 'lyon'],
            'nxp595/apollo4': ['matterhorn', 'pamir', 'lyon'],
            'mhs003': ['rocky', 'geneva', 'pike'],
            'mhs003s': ['atlas', 'alps'],
        }
        devices = fallback.get(normalized, [])

    return devices


@app.route('/api/platforms/<path:platform>/devices')
def api_platform_devices(platform: str):
    """根据平台返回可选的设备型号列表，不包含 bak 详情"""
    devices = _get_jenkins_platform_devices(platform)
    return jsonify({"ok": True, "platform": platform, "devices": devices, "count": len(devices)})


@app.route("/api/template/new/<project_name>")
def api_template_new(project_name: str):
    """返回新建项目的默认配置模板"""
    cfg = _build_new_project_template(project_name)
    return jsonify({"ok": True, "project": project_name, "data": _sanitize_config_for_display(cfg)})


@app.route("/api/projects/<project>/history")
def api_history(project: str):
    """获取项目的历史版本列表"""
    history = _list_baks(project)
    return jsonify({"ok": True, "project": project, "history": history, "count": len(history)})


@app.route("/api/projects/<project>/latest")
def api_latest(project: str):
    """获取项目最新 bak 的完整配置（用于表单自动填充），已脱敏"""
    bak = _find_latest_bak(project)
    if not bak:
        cfg_path = _find_project_config(project)
        if cfg_path and cfg_path.exists():
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            return jsonify({"ok": True, "source": "config", "data": _sanitize_config_for_display(cfg)})
        return jsonify({"ok": False, "error": f"No bak or config found for project '{project}'"}), 404

    cfg = json.loads(bak.read_text(encoding="utf-8"))
    return jsonify({
        "ok": True,
        "source": bak.name,
        "timestamp": bak.name.split(".bak.")[-1],
        "data": _sanitize_config_for_display(cfg),
    })


@app.route("/api/projects/<project>/version/<path:suffix>")
def api_version_detail(project: str, suffix: str):
    """获取指定 bak 版本的完整配置（用于历史版本回填）。suffix 可以是 YYYYMMDD_HHMMSS 或 before_env_migration 等。"""
    bak_file = SCRIPT_DIR / f"gqf_{project}.json.bak.{suffix}"
    if not bak_file.exists():
        # 尝试通配项目名变体（如 gqf_windermere_ts.json.bak.suffix）
        import re as _re
        pattern = f"gqf_{project}_*.json.bak.{suffix}"
        escaped = _re.escape(project)
        candidates = sorted(
            [f for f in SCRIPT_DIR.glob(pattern)
             if _re.match(rf"^gqf_{escaped}.*\.json\.bak\.{_re.escape(suffix)}$", f.name)],
            key=lambda f: f.stat().st_mtime, reverse=True,
        )
        if not candidates:
            return jsonify({"ok": False, "error": f"Version '{suffix}' not found for project '{project}'"}), 404
        bak_file = candidates[0]

    cfg = json.loads(bak_file.read_text(encoding="utf-8"))
    return jsonify({
        "ok": True,
        "source": bak_file.name,
        "timestamp": suffix,
        "data": _sanitize_config_for_display(cfg),
    })


@app.route("/api/projects/<project>/release", methods=["POST"])
def api_release(project: str):
    """接收表单数据，生成配置文件，触发流水线"""
    try:
        form_data = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "error": "Invalid JSON body"}), 400

    # 验证必填字段
    release_info = form_data.get("release", {})
    vars_info = form_data.get("vars", {})
    if not release_info.get("version"):
        return jsonify({"ok": False, "error": "version 是必填字段"}), 400

    # 加载基础配置模板（从最新 bak / 当前 config / 新建模板）
    base_cfg: Dict[str, Any] = {}
    is_new = bool(form_data.get("is_new", False))

    if is_new:
        # 新建项目：使用模板
        base_cfg = _build_new_project_template(project)
        cfg_path = _find_project_config(project)
        if cfg_path and cfg_path.exists():
            # 如果配置已存在，读取并合并（保留 feishu tokens 等）
            existing = json.loads(cfg_path.read_text(encoding="utf-8"))
            # 只保留用户填写的必要字段，其余用模板覆盖
            for section in ("release", "vars"):
                if section in form_data:
                    base_cfg[section] = {**base_cfg.get(section, {}), **form_data[section]}
            # 保留已有的 feishu template_node_token
            if "feishu" in existing and existing["feishu"].get("template_node_token"):
                base_cfg["feishu"]["template_node_token"] = existing["feishu"]["template_node_token"]
    else:
        # 已有项目：加载最新 bak 或当前 config
        bak = _find_latest_bak(project)
        if bak:
            base_cfg = json.loads(bak.read_text(encoding="utf-8"))
        else:
            cfg_path = _find_project_config(project)
            if cfg_path:
                base_cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

    if not base_cfg:
        return jsonify({"ok": False, "error": "No base config found"}), 500

    # 确保关键配置段存在（从模板补全，兼容旧配置文件）
    template = _build_new_project_template(project)
    # 补全缺失的 base_config（兼容手动创建/迁移丢失的配置）
    if "base_config" not in base_cfg or not base_cfg["base_config"]:
        base_cfg["base_config"] = template.get("base_config", "common.json")
    for section in ("changelog", "jenkins", "nas", "feishu"):
        if section not in base_cfg or not base_cfg[section]:
            base_cfg[section] = template[section]
    # 确保 jenkins.triggers 和 builds 存在
    base_cfg.setdefault("jenkins", OrderedDict())
    if "triggers" not in base_cfg["jenkins"]:
        base_cfg["jenkins"]["triggers"] = template["jenkins"]["triggers"]
    if "builds" not in base_cfg["jenkins"]:
        base_cfg["jenkins"]["builds"] = template["jenkins"]["builds"]
    # 确保 modes 字段存在（默认 async）
    base_cfg["jenkins"]["triggers"].setdefault("modes", "async")

    # 合并用户提交的字段
    if "release" in form_data:
        base_cfg["release"] = {**base_cfg.get("release", {}), **form_data["release"]}
    # 存储触发者标识，用于通知消息中的用户归属
    operator = (form_data.get("user_id") or "").strip()
    if operator:
        base_cfg.setdefault("release", {})
        base_cfg["release"]["operator"] = operator
    # 存储触发者的飞书 open_id（用于最终个人通知，群聊触发时 chat_id≠open_id）
    trigger_open_id = (form_data.get("open_id") or "").strip()
    if trigger_open_id:
        base_cfg.setdefault("release", {})
        base_cfg["release"]["operator_open_id"] = trigger_open_id
    # 存储通知目标：从 web 界面传来的飞书 open_id（用于个人通知）
    notify_oid = (form_data.get("notification_open_id") or "").strip()
    if notify_oid:
        base_cfg.setdefault("feishu", {})
        base_cfg["feishu"]["notification_open_id"] = notify_oid
        # 查找对应的 chat_id（用于 lark-cli 发送）
        n_chat_id = _lookup_chat_id_from_open_id(notify_oid)
        if n_chat_id:
            base_cfg["feishu"]["notification_chat_id"] = n_chat_id
    if "vars" in form_data:
        base_cfg["vars"] = {**base_cfg.get("vars", {}), **form_data["vars"]}

    # 将前端 auto_bind_after_upgrade 映射到 Jenkins 构建参数 AUTH_CFG_AUTO_BINDING
    auto_bind_raw = (base_cfg.get("vars", {}).get("auto_bind_after_upgrade") or "").strip()
    if auto_bind_raw:
        auto_bind_val = auto_bind_raw.upper()
        if auto_bind_val in ("YES", "NO"):
            base_cfg.setdefault("jenkins", OrderedDict())
            base_cfg["jenkins"].setdefault("triggers", OrderedDict())
            base_cfg["jenkins"]["triggers"].setdefault("release", OrderedDict())
            base_cfg["jenkins"]["triggers"]["release"].setdefault("parameters", OrderedDict())
            base_cfg["jenkins"]["triggers"]["release"]["parameters"]["AUTH_CFG_AUTO_BINDING"] = auto_bind_val

    # HMI_CORE_MM_OWNER_DEP：release=TSCAN=输入值N，debug=默认2时为2、否则N×2
    hmi_raw = (base_cfg.get("vars", {}).get("hmi_core_mm_owner_dep") or "").strip()
    try:
        hmi_n = int(hmi_raw) if hmi_raw else 2
    except ValueError:
        hmi_n = 2
    hmi_release = str(hmi_n)
    hmi_debug = "2" if hmi_n == 2 else str(hmi_n * 2)
    for _rn, _hmi_val in (("release", hmi_release), ("debug", hmi_debug), ("Tscan", hmi_release)):
        base_cfg.setdefault("jenkins", OrderedDict())
        base_cfg["jenkins"].setdefault("triggers", OrderedDict())
        base_cfg["jenkins"]["triggers"].setdefault(_rn, OrderedDict())
        base_cfg["jenkins"]["triggers"][_rn].setdefault("parameters", OrderedDict())
        base_cfg["jenkins"]["triggers"][_rn]["parameters"]["HMI_CORE_MM_OWNER_DEP"] = _hmi_val

    # 使用前端选择的 Jenkins Job URL（NXP595→HuamiOS, MHS003/MHS003S→HuamiOS_multi_platform）
    if form_data.get("jenkins_job_url"):
        base_cfg.setdefault("jenkins", OrderedDict())
        base_cfg["jenkins"].setdefault("triggers", OrderedDict())
        base_cfg["jenkins"]["triggers"]["job_url"] = form_data["jenkins_job_url"]
    # 保存平台选择（MHS003 / MHS003S 芯片选择）
    if form_data.get("platform_select"):
        base_cfg["platform_select"] = form_data["platform_select"]

    # 自动计算 variant 和同步 device_name/xml_name
    rel = base_cfg["release"]
    project_name = rel.get("project", project)
    stage = rel.get("stage", "")
    version = rel.get("version", "")
    rel["variant"] = f"{project_name} {stage} v{version}".strip().rstrip("v")
    # device_name 与 project 一致（前端已合并）
    if not rel.get("device_name"):
        rel["device_name"] = project_name
    # xml_name 自动去除 _32/_64/_64m 等后缀
    if not rel.get("xml_name"):
        import re as _re
        rel["xml_name"] = _re.sub(r"_\d+m?$", "", project_name)

    # 清除自动填充字段（运行时写入）
    if "builds" in base_cfg.get("jenkins", {}):
        for btype in ("debug", "release", "tscan"):
            if btype in base_cfg["jenkins"]["builds"]:
                base_cfg["jenkins"]["builds"][btype].pop("build_url", None)
    if "changelog" in base_cfg:
        for key in ("build_url", "build_number", "status", "result", "triggered_at", "updated_at"):
            base_cfg["changelog"].pop(key, None)

    # 剥离 common.json 公共字段，只保留项目差异配置
    _strip_common_fields(base_cfg)

    # 使用当前登录用户的 Jenkins 凭据（替换 common.json 默认账号）
    j_auth = form_data.get("jenkins_auth") or {}
    j_user = (j_auth.get("username") or "").strip()
    j_pass = (j_auth.get("password") or "").strip()
    if j_user and j_pass:
        base_cfg.setdefault("jenkins", {})["auth"] = {
            "type": "basic",
            "username": j_user,
            "password": j_pass,
        }

    # 写入新配置文件
    cfg_path = SCRIPT_DIR / f"gqf_{project}.json"
    # 注入当前用户的飞书 token（而非 common.json 的公共 token）
    user_id = (form_data.get("user_id") or "").strip()
    _inject_user_feishu_token(base_cfg, user_id)
    cfg_path.write_text(json.dumps(base_cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # 在后台线程中触发流水线
    task_id = f"{project}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    # 创建 SSE 日志流队列
    _task_streams[task_id] = queue.Queue()
    _running_tasks[task_id] = {
        "project": project,
        "version": release_info.get("version", ""),
        "user_id": (form_data.get("user_id") or "").strip(),
        "task_type": "build",
        "platform_select": base_cfg.get("platform_select", ""),
        "status": "starting",
        "started_at": datetime.now().isoformat(),
        "_jenkins_auth": j_auth if j_user and j_pass else {},  # 保存用户凭据，用于终止构建时鉴权
        "log": [],
        "steps": [
            {"name": "版本编译", "status": "waiting", "percent": 0},
            {"name": "本地下载", "status": "waiting", "percent": 0},
            {"name": "NAS文件上传", "status": "waiting", "percent": 0},
            {"name": "分享链接", "status": "waiting", "percent": 0},
            {"name": "生成飞书文档", "status": "waiting", "percent": 0},
        ],
    }

    def _run_pipeline():
        try:
            _running_tasks[task_id]["status"] = "triggering"
            # ── 标记版本编译步骤为运行中（修复 terminate 按钮不显示）──
            steps = _running_tasks[task_id].get("steps", [])
            if steps and steps[0].get("status") == "waiting":
                steps[0]["status"] = "running"
                steps[0]["percent"] = 5
                _push_progress(task_id, steps)
            cmd = [
                sys.executable, str(SCRIPT_DIR / "jenkins_trigger_build.py"),
                "--config", str(cfg_path),
                "--run-pipeline",
                "--pipeline-script", str(SCRIPT_DIR / "lark_release.py"),
                "--pipeline-subcommand", "run",
                "--no-backup",
            ]
            _push_log(task_id, f"Running: {' '.join(cmd)}")

            # 使用 Popen 实时捕获输出，而不是 run()
            pipeline_env = os.environ.copy()
            pipeline_env["PYTHONUNBUFFERED"] = "1"  # 强制行缓冲，避免子进程输出积压
            proc = subprocess.Popen(
                cmd,
                cwd=str(SCRIPT_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True,
                env=pipeline_env,
            )
            _running_tasks[task_id]["proc"] = proc  # 保存进程引用，用于手动终止

            # 实时读取 stdout，统一处理日志和步骤进度
            for line in iter(proc.stdout.readline, ''):
                _process_pipeline_line(task_id, line.rstrip('\n\r'))
            proc.stdout.close()
            proc.wait(timeout=4 * 60 * 60)

            # 根据退出码决定步骤状态
            _running_tasks[task_id]["exit_code"] = proc.returncode
            if proc.returncode == 0:
                # 成功：全部标记完成
                for s in _running_tasks[task_id]["steps"]:
                    s["percent"] = 100
                    s["status"] = "success"
                _push_progress(task_id, _running_tasks[task_id]["steps"], total_percent=100)
                _running_tasks[task_id]["status"] = "success"
                _running_tasks[task_id]["_finished_at"] = time.time()
                # 数据埋点：编译版本完成
                _track_event("build",
                    operator=_running_tasks[task_id].get("user_id", ""),
                    device=project,
                    detail=_running_tasks[task_id].get("version", ""))
                # 清理旧文件（bak 已在版本编译完成时创建）
                cleaned_baks = _cleanup_baks(project)
                cleaned_downloads = _cleanup_downloads(project)
                _push_log(task_id, 
                    f"Cleanup: removed {cleaned_baks} old bak(s), {cleaned_downloads} old download(s)"
                )
                # 保存用户飞书 token（lark_release.py 可能通过 OAuth 刷新了 token）
                user_id = (form_data.get("user_id") or "").strip()
                if user_id:
                    try:
                        updated_cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                        _save_user_feishu_token(user_id, updated_cfg)
                    except Exception:
                        pass
            else:
                # 如果已被手动终止，保持 terminated 状态
                if _running_tasks[task_id].get("status") != "terminated":
                    for s in _running_tasks[task_id]["steps"]:
                        if s["status"] == "running":
                            s["status"] = "error"
                    _push_progress(task_id, _running_tasks[task_id]["steps"])
                    _running_tasks[task_id]["status"] = "failed"
                _running_tasks[task_id]["_finished_at"] = time.time()
                _persist_running_tasks()
                _push_log(task_id, f"❌ 流水线执行失败（退出码: {proc.returncode}）")
        except subprocess.TimeoutExpired:
            _running_tasks[task_id]["status"] = "timeout"
            _running_tasks[task_id]["_finished_at"] = time.time()
            _persist_running_tasks()
            _push_log(task_id, "Pipeline timed out after 4 hours")
        except Exception as e:
            _running_tasks[task_id]["status"] = "error"
            _running_tasks[task_id]["_finished_at"] = time.time()
            _persist_running_tasks()
            _push_log(task_id, f"Exception: {e}")
        finally:
            sse_q = _task_streams.get(task_id)
            if sse_q:
                try:
                    sse_q.put_nowait({
                        "status": _running_tasks[task_id].get("status", "error"),
                        "exit_code": _running_tasks[task_id].get("exit_code", -1),
                        "complete": True,
                    })
                except queue.Full:
                    pass

    thread = threading.Thread(target=_run_pipeline, daemon=True)
    thread.start()
    _running_tasks[task_id]["thread"] = thread

    return jsonify({
        "ok": True,
        "task_id": task_id,
        "project": project,
        "config_file": str(cfg_path),
        "message": f"配置已保存到 {cfg_path.name}，流水线已在后台启动",
    })


@app.route("/api/projects/<project>/run-pipeline-direct", methods=["POST"])
def api_run_pipeline_direct(project: str):
    """跳过 Jenkins 触发，直接用已有的 Jenkins build URL 跑下载→上传→文档流水线。
    支持可选 tscan_jenkins_url 用于 TSCAN 产物下载。"""
    try:
        form_data = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "error": "Invalid JSON body"}), 400

    release_url = (form_data.get("release_jenkins_url") or "").strip()
    debug_url = (form_data.get("debug_jenkins_url") or "").strip()
    tscan_url = (form_data.get("tscan_jenkins_url") or "").strip()
    if not release_url or not debug_url:
        return jsonify({"ok": False, "error": "release_jenkins_url 和 debug_jenkins_url 都是必填"}), 400

    release_info = form_data.get("release", {})
    vars_info = form_data.get("vars", {})
    if not release_info.get("version"):
        return jsonify({"ok": False, "error": "version 是必填字段"}), 400

    # 使用 template 作为基础，合并用户数据
    base_cfg = _build_new_project_template(project)

    # 如果已有配置文件，保留其 nas.base_dir 和 feishu token
    existing_path = SCRIPT_DIR / f"gqf_{project}.json"
    if existing_path.exists():
        try:
            existing = json.loads(existing_path.read_text(encoding="utf-8"))
            if "nas" in existing and existing["nas"].get("remote", {}).get("base_dir"):
                base_cfg["nas"]["remote"]["base_dir"] = existing["nas"]["remote"]["base_dir"]
            if "feishu" in existing and existing["feishu"].get("template_node_token"):
                base_cfg["feishu"]["template_node_token"] = existing["feishu"]["template_node_token"]
        except Exception:
            pass

    if "release" in form_data:
        base_cfg["release"] = {**base_cfg.get("release", {}), **form_data["release"]}
    if "vars" in form_data:
        base_cfg["vars"] = {**base_cfg.get("vars", {}), **form_data["vars"]}
    if "feishu" in form_data and form_data["feishu"].get("template_node_token"):
        base_cfg["feishu"]["template_node_token"] = form_data["feishu"]["template_node_token"]

    # 自动计算 variant
    rel = base_cfg["release"]
    project_name = rel.get("project", project)
    stage = rel.get("stage", "")
    version = rel.get("version", "")
    rel["variant"] = f"{project_name} {stage} v{version}".strip().rstrip("v")
    if not rel.get("device_name"):
        rel["device_name"] = project_name

    # 设置 operator_open_id 用于 Bot 通知（谁触发谁收到）
    trigger_open_id = (form_data.get("open_id") or "").strip()
    if trigger_open_id:
        base_cfg.setdefault("release", {})
        base_cfg["release"]["operator_open_id"] = trigger_open_id

    # 填入用户提供的 Jenkins build URL
    base_cfg.setdefault("jenkins", OrderedDict())
    base_cfg["jenkins"].setdefault("builds", OrderedDict())
    base_cfg["jenkins"]["builds"]["debug"] = OrderedDict([("build_url", debug_url.rstrip("/") + "/")])
    base_cfg["jenkins"]["builds"]["release"] = OrderedDict([("build_url", release_url.rstrip("/") + "/")])

    # 清除运行时字段
    base_cfg["jenkins"]["builds"]["debug"].pop("download", None)
    base_cfg["jenkins"]["builds"]["release"].pop("download", None)
    # 可选的 TSCAN build URL
    if tscan_url:
        tscan_dl_cfg = {
            "mode": "artifacts",
            "include_globs": ["**/*"],
            "exclude_globs": ["**/*.log"],
            "output_dir": f"work/download/{project}_tscan",
            "overwrite": True,
        }
        base_cfg["jenkins"]["builds"]["tscan"] = OrderedDict([
            ("build_url", tscan_url.rstrip("/") + "/"),
            ("download", tscan_dl_cfg),
        ])
        # 确保 nas.uploads 中有 tscan 条目
        nas_cfg = base_cfg.setdefault("nas", {})
        uploads = nas_cfg.setdefault("uploads", [])
        if not any(u.get("name") == "tscan" for u in uploads if isinstance(u, dict)):
            uploads.append({
                "name": "tscan",
                "remote_subdir": "Monkey",
                "local_dir": f"work/download/{project}_tscan",
                "include_globs": ["**/*.zip"],
                "exclude_globs": [],
                "overwrite": True,
            })
    if "changelog" in base_cfg:
        for key in ("build_url", "build_number", "status", "result", "triggered_at", "updated_at"):
            base_cfg["changelog"].pop(key, None)

    # 写入临时配置文件（不覆盖项目 JSON）
    _strip_common_fields(base_cfg)
    # 补偿：必须引用 base_config，否则 read_json_with_base 拿不到 notifications/feishu 等公共配置
    base_cfg["base_config"] = "common.json"
    # skip_jenkins 流程：直接从 gqf_*.json 中的 vars 获取 tag 信息（此流程无表单确认）
    cfg_path_orig = _find_project_config(project)
    if cfg_path_orig and cfg_path_orig.exists():
        try:
            orig_cfg = json.loads(cfg_path_orig.read_text(encoding="utf-8"))
            orig_vars = (orig_cfg.get("vars") or {}) if isinstance(orig_cfg, dict) else {}
            if isinstance(orig_vars, dict):
                base_cfg.setdefault("vars", {})
                for tag_key in ("tag", "boot_tag", "recovery_tag", "fct_tag"):
                    val = orig_vars.get(tag_key)
                    if val and not base_cfg["vars"].get(tag_key):
                        base_cfg["vars"][tag_key] = val
        except Exception:
            pass
    # 使用当前登录用户的 Jenkins 凭据
    j_auth = form_data.get("jenkins_auth") or {}
    j_user = (j_auth.get("username") or "").strip()
    j_pass = (j_auth.get("password") or "").strip()
    if j_user and j_pass:
        base_cfg.setdefault("jenkins", {})["auth"] = {"type": "basic", "username": j_user, "password": j_pass}
    # 注入当前用户的飞书 token
    user_id = (form_data.get("user_id") or "").strip()
    _inject_user_feishu_token(base_cfg, user_id)
    # 写入临时文件，不修改项目 JSON
    import tempfile as _tempfile_mod
    tmp_fd, tmp_path = _tempfile_mod.mkstemp(suffix='.json', prefix=f'gqf_{project}_tmp_', dir=str(SCRIPT_DIR))
    os.close(tmp_fd)
    cfg_path = Path(tmp_path)
    cfg_path.write_text(json.dumps(base_cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # 创建 Task 并直接跑 release_pipeline_run.py（跳过 jenkins_trigger_build.py）
    task_id = f"{project}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    skip_flags = form_data.get("skip_flags") or {}
    skip_dl = bool(skip_flags.get("skip_download"))
    skip_up = bool(skip_flags.get("skip_upload"))
    skip_sh = bool(skip_flags.get("skip_share"))
    skip_dc = bool(skip_flags.get("skip_doc"))
    skip_fs = bool(skip_flags.get("skip_feishu"))

    # 保持 5 步结构，跳过的步骤直接标记 success
    dl_status = "success" if skip_dl else "waiting"
    up_status = "success" if skip_up else "waiting"
    sh_status = "success" if skip_sh else "waiting"
    fs_status = "success" if (skip_dc and skip_fs) else "waiting"
    dl_pct = 100 if skip_dl else 0
    up_pct = 100 if skip_up else 0
    sh_pct = 100 if skip_sh else 0
    fs_pct = 100 if (skip_dc and skip_fs) else 0

    _task_streams[task_id] = queue.Queue()
    _running_tasks[task_id] = {
        "project": project,
        "version": release_info.get("version", ""),
        "user_id": (form_data.get("user_id") or "").strip(),
        "task_type": "skip_jenkins",
        "platform_select": base_cfg.get("platform_select", ""),
        "status": "starting",
        "started_at": datetime.now().isoformat(),
        # "source_step": form_data.get("source_step", 5),  # [2026-06-02] 前端不消费
        "log": [],
        "steps": [
            {"name": "版本编译", "status": "success", "percent": 100},
            {"name": "本地下载", "status": dl_status, "percent": dl_pct},
            {"name": "NAS文件上传", "status": up_status, "percent": up_pct},
            {"name": "分享链接", "status": sh_status, "percent": sh_pct},
            {"name": "生成飞书文档", "status": fs_status, "percent": fs_pct},
        ],
    }

    def _run_pipeline_direct():
        try:
            _running_tasks[task_id]["status"] = "running"
            cmd = [
                sys.executable, str(SCRIPT_DIR / "lark_release.py"),
                "run",
                "--config", str(cfg_path),
            ]
            if skip_flags.get("skip_download"): cmd.append("--skip-download")
            if skip_flags.get("skip_prepare"): cmd.append("--skip-prepare")
            if skip_flags.get("skip_upload"): cmd.append("--skip-upload")
            if skip_flags.get("skip_share"): cmd.append("--skip-share")
            if skip_flags.get("skip_doc"): cmd.append("--skip-doc")
            if skip_flags.get("skip_feishu"): cmd.append("--skip-feishu")
            _push_log(task_id, f"Running: {' '.join(cmd)}")
            skip_list = [k for k, v in skip_flags.items() if v]
            skip_msg = f"（跳过: {', '.join(skip_list)}）" if skip_list else ""
            _push_log(task_id, f"Release Jenkins: {release_url}")
            _push_log(task_id, f"Debug Jenkins: {debug_url}")
            if tscan_url:
                _push_log(task_id, f"TSCAN Jenkins: {tscan_url}")
            _push_log(task_id, f"版本编译已完成（跳过 Jenkins 触发），开始执行... {skip_msg}")
            _push_progress(task_id, _running_tasks[task_id]["steps"])

            direct_env = os.environ.copy()
            direct_env["PYTHONUNBUFFERED"] = "1"  # 强制行缓冲，避免子进程输出积压
            proc = subprocess.Popen(
                cmd,
                cwd=str(SCRIPT_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True,
                env=direct_env,
            )
            _running_tasks[task_id]["proc"] = proc  # 保存进程引用，用于手动终止

            for line in iter(proc.stdout.readline, ''):
                _process_pipeline_line(task_id, line.rstrip('\n\r'))

            proc.stdout.close()
            proc.wait(timeout=4 * 60 * 60)

            # 根据退出码决定步骤状态
            _running_tasks[task_id]["exit_code"] = proc.returncode
            if proc.returncode == 0:
                for s in _running_tasks[task_id]["steps"]:
                    s["percent"] = 100
                    s["status"] = "success"
                _push_progress(task_id, _running_tasks[task_id]["steps"], total_percent=100)
                _running_tasks[task_id]["status"] = "success"
                _running_tasks[task_id]["_finished_at"] = time.time()
                _persist_running_tasks()
                _track_event("skip_jenkins",
                    operator=_running_tasks[task_id].get("user_id", ""),
                    device=project,
                    detail=_running_tasks[task_id].get("version", ""))
                cleaned_baks = _cleanup_baks(project)
                cleaned_downloads = _cleanup_downloads(project)
                _push_log(task_id, f"Cleanup: removed {cleaned_baks} old bak(s), {cleaned_downloads} old download(s)")
                # 保存用户飞书 token
                if user_id:
                    try:
                        updated_cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                        _save_user_feishu_token(user_id, updated_cfg)
                    except Exception:
                        pass
            else:
                if _running_tasks[task_id].get("status") != "terminated":
                    for s in _running_tasks[task_id]["steps"]:
                        if s["status"] == "running":
                            s["status"] = "error"
                    _push_progress(task_id, _running_tasks[task_id]["steps"])
                    _running_tasks[task_id]["status"] = "failed"
                _running_tasks[task_id]["_finished_at"] = time.time()
                _persist_running_tasks()
                _push_log(task_id, f"❌ 流水线执行失败（退出码: {proc.returncode}）")
        except subprocess.TimeoutExpired:
            _running_tasks[task_id]["status"] = "timeout"
            _running_tasks[task_id]["_finished_at"] = time.time()
            _persist_running_tasks()
            _push_log(task_id, "Pipeline timed out after 4 hours")
        except Exception as e:
            _running_tasks[task_id]["status"] = "error"
            _running_tasks[task_id]["_finished_at"] = time.time()
            _persist_running_tasks()
            _push_log(task_id, f"Exception: {e}")
        finally:
            # 清理临时配置文件
            try:
                if cfg_path.exists():
                    cfg_path.unlink()
            except Exception:
                pass
            sse_q = _task_streams.get(task_id)
            if sse_q:
                try:
                    sse_q.put_nowait({
                        "status": _running_tasks[task_id].get("status", "error"),
                        "exit_code": _running_tasks[task_id].get("exit_code", -1),
                        "complete": True,
                    })
                except queue.Full:
                    pass

    thread = threading.Thread(target=_run_pipeline_direct, daemon=True)
    thread.start()
    _running_tasks[task_id]["thread"] = thread

    return jsonify({
        "ok": True,
        "task_id": task_id,
        "project": project,
        "config_file": str(cfg_path),
        "message": "pipeline 已在后台启动（跳过 Jenkins 触发，不修改项目配置）",
    })


@app.route("/api/projects/<project>/tscan-only", methods=["POST"])
def api_tscan_only(project: str):
    """独立 TSCAN 构建：仅触发 TSCAN Jenkins 构建并跟踪进度"""
    try:
        form_data = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "error": "Invalid JSON body"}), 400

    device = (form_data.get("device") or "").strip()
    tag_algo = (form_data.get("tag_algo") or "").strip()
    if not device or not tag_algo:
        return jsonify({"ok": False, "error": "缺少 device 或 tag_algo"}), 400

    cfg_path = SCRIPT_DIR / f"gqf_{device}.json"
    if not cfg_path.exists():
        return jsonify({"ok": False, "error": f"设备配置不存在: gqf_{device}.json"}), 404

    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

    # 写入平台选择，临时覆盖 Platform 配置，确保使用正确的单平台 Job URL
    platform = (form_data.get("platform") or "").strip()
    jenkins_job_url = (form_data.get("jenkins_job_url") or "").strip()
    if platform:
        cfg.setdefault("_tscan", {})["platform"] = platform
    if jenkins_job_url:
        cfg.setdefault("_tscan", {})["job_url"] = jenkins_job_url
    cfg.setdefault("vars", {})
    for key in ("tag_algo", "tag_boot", "tag_recovery", "tag_fct",
                "project_id", "version", "build_mode", "build_test_tool",
                "fct_version_name", "version_name", "build_release", "tscancode_check"):
        val = (form_data.get(key) or "").strip()
        if val:
            cfg["vars"][key] = val
    # 写入临时配置文件（TSCAN 独立构建不修改项目 JSON）
    import tempfile as _tempfile_mod
    tmp_fd, tmp_path = _tempfile_mod.mkstemp(suffix='.json', prefix=f'gqf_{device}_tscan_', dir=str(SCRIPT_DIR))
    os.close(tmp_fd)
    cfg_path = Path(tmp_path)
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # 不创建 bak（TSCAN/Changelog 独立构建不产生 bak）

    # 创建任务
    task_id = f"{project}_tscan_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    _task_streams[task_id] = queue.Queue()
    _running_tasks[task_id] = {
        "project": project,
        "user_id": (form_data.get("user_id") or "").strip(),
        "task_type": "tscan",
        "platform_select": cfg.get("platform_select", ""),
        "status": "starting",
        "started_at": datetime.now().isoformat(),
        # "source_step": form_data.get("source_step", 5),  # [2026-06-02] 前端不消费
        "log": [],
        "steps": [
            {"name": "TSCAN构建", "status": "waiting", "percent": 0},
        ],
        "is_tscan_standalone": True,
        "tscan_build_url": "",
    }

    def _run_tscan_only():
        try:
            _running_tasks[task_id]["status"] = "triggering"
            _push_log(task_id, f"== Trigger TSCAN (standalone) for {device} ==")
            _push_log(task_id, f"TAG: {tag_algo}")

            steps = _running_tasks[task_id].get("steps", [])
            if steps:
                steps[0]["status"] = "running"
                steps[0]["percent"] = 5
                _push_progress(task_id, steps, total_percent=5)

            cmd = [
                sys.executable, str(SCRIPT_DIR / "jenkins_trigger_build.py"),
                "--config", str(cfg_path),
                "--tscan-standalone",
                "--no-backup",
            ]
            proc = subprocess.Popen(
                cmd, cwd=str(SCRIPT_DIR),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, universal_newlines=True,
            )
            _running_tasks[task_id]["proc"] = proc

            for line in iter(proc.stdout.readline, ''):
                _process_pipeline_line(task_id, line.rstrip('\n\r'))
                # 检测 TSCAN build URL
                stripped = line.strip()
                if stripped and '/job/' in stripped and ('TSCAN' in stripped.upper() or 'tscan' in stripped.lower()):
                    # Try to extract build URL
                    url_match = re.search(r'(https?://\S+)', stripped)
                    if url_match and not _running_tasks[task_id].get("tscan_build_url"):
                        _running_tasks[task_id]["tscan_build_url"] = url_match.group(1).rstrip('/')

            proc.stdout.close()
            proc.wait(timeout=4 * 60 * 60)
            _running_tasks[task_id]["exit_code"] = proc.returncode

            if proc.returncode == 0:
                steps[0]["status"] = "success"
                steps[0]["percent"] = 100
                _push_progress(task_id, steps, total_percent=100)
                _running_tasks[task_id]["status"] = "success"
                _running_tasks[task_id]["_finished_at"] = time.time()
                _persist_running_tasks()
                _track_event("tscan",
                    operator=_running_tasks[task_id].get("user_id", ""),
                    device=_running_tasks[task_id].get("project", ""),
                    detail=tag_algo)
                tscan_url = _running_tasks[task_id].get("tscan_build_url", "")
                _push_log(task_id, f"✅ TSCAN 构建成功")
                if tscan_url:
                    _push_log(task_id, f"可点击查看下载 TSCAN 产物：{tscan_url}")
            else:
                # 如果已被手动终止，保持 terminated 状态，不覆盖为 failed
                if _running_tasks[task_id].get("status") != "terminated":
                    steps[0]["status"] = "failed"
                    _push_progress(task_id, steps)
                    _running_tasks[task_id]["status"] = "failed"
                _running_tasks[task_id]["_finished_at"] = time.time()
                _persist_running_tasks()
                _push_log(task_id, f"❌ TSCAN 构建失败（退出码: {proc.returncode}）")
        except Exception as e:
            _running_tasks[task_id]["status"] = "error"
            _push_log(task_id, f"Exception: {e}")
        finally:
            # 清理临时配置文件
            try:
                if cfg_path.exists():
                    cfg_path.unlink()
            except Exception:
                pass
            sse_q = _task_streams.get(task_id)
            if sse_q:
                try:
                    sse_q.put_nowait({
                        "status": _running_tasks[task_id].get("status", "error"),
                        "complete": True,
                        "tscan_build_url": _running_tasks[task_id].get("tscan_build_url", ""),
                        "is_tscan_standalone": True,
                    })
                except queue.Full:
                    pass

    thread = threading.Thread(target=_run_tscan_only, daemon=True)
    thread.start()
    _running_tasks[task_id]["thread"] = thread

    return jsonify({
        "ok": True,
        "task_id": task_id,
        "project": project,
        "message": f"TSCAN 构建已启动: {task_id}",
    })


def _abort_jenkins_builds(task_id: str, task: dict) -> int:
    """解析任务中的 Jenkins queue/build URL，取消队列项并终止构建。
    返回成功终止的构建数量。
    
    [2026-06-30] 修复：增加队列项取消和构建停止验证，解决"页面已终止但 Jenkins 仍在跑"的问题。
    """
    project = task.get("project", "")
    if not project:
        _push_log(task_id, "  无法确定项目名，跳过 Jenkins 终止")
        return 0

    cfg_path = SCRIPT_DIR / f"gqf_{project}.json"
    if not cfg_path.exists():
        _push_log(task_id, f"  配置文件不存在: gqf_{project}.json，跳过 Jenkins 终止")
        return 0
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        _push_log(task_id, f"  配置文件读取失败: gqf_{project}.json，跳过 Jenkins 终止")
        return 0

    jcfg = cfg.get("jenkins", {})
    # 优先使用任务创建时保存的用户 Jenkins 凭据（有更高权限），fallback 到配置文件和 common.json
    task_auth = task.get("_jenkins_auth", {})
    if isinstance(task_auth, dict) and task_auth.get("username"):
        j_user = str(task_auth.get("username", "")).strip()
        j_pass = str(task_auth.get("password", task_auth.get("token", ""))).strip()
    else:
        auth_cfg = jcfg.get("auth", {})
        if not auth_cfg.get("username"):
            # auth 可能被剥离到 common.json，尝试回退读取
            common_path = SCRIPT_DIR / "common.json"
            if common_path.exists():
                try:
                    common_cfg = json.loads(common_path.read_text(encoding="utf-8"))
                    common_auth = common_cfg.get("jenkins", {}).get("auth", {})
                    if common_auth.get("username"):
                        auth_cfg = common_auth
                except Exception:
                    pass
        j_user = _resolve_credential(
            str(auth_cfg.get("username", "")), "JENKINS_USERNAME"
        )
        j_pass = _resolve_credential(
            str(auth_cfg.get("password", auth_cfg.get("token", ""))), "JENKINS_PASSWORD"
        )
    if not j_user or not j_pass:
        _push_log(task_id, "  无法获取 Jenkins 凭证，跳过终止远程构建")
        return 0

    base_url = str(jcfg.get("base_url", "")).strip().rstrip("/")
    if not base_url:
        common_path = SCRIPT_DIR / "common.json"
        if common_path.exists():
            try:
                common_cfg = json.loads(common_path.read_text(encoding="utf-8"))
                base_url = str(common_cfg.get("jenkins", {}).get("base_url", "")).strip().rstrip("/")
            except Exception:
                pass

    # 使用 Session 保持 cookie（CSRF crumb 绑定到 session cookie）
    session = requests.Session()
    session.auth = (j_user, j_pass)

    # 获取 CSRF crumb
    crumb_field = ""
    crumb_headers: dict = {"Content-Type": "application/x-www-form-urlencoded"}
    if base_url:
        for crumb_path in ("/crumbIssuer/api/json", "/crumbIssuer/api/xml"):
            try:
                crumb_url = f"{base_url}{crumb_path}"
                resp = session.get(crumb_url, timeout=5)
                if resp.status_code == 200:
                    if crumb_path.endswith("/json"):
                        data = resp.json()
                        crumb_field = str(data.get("crumbRequestField", "")).strip()
                        crumb_val = str(data.get("crumb", "")).strip()
                    else:
                        m_field = re.search(r'<crumbRequestField>([^<]+)</crumbRequestField>', resp.text)
                        m_val = re.search(r'<crumb>([^<]+)</crumb>', resp.text)
                        crumb_field = m_field.group(1) if m_field else ""
                        crumb_val = m_val.group(1) if m_val else ""
                    if crumb_field and crumb_val:
                        crumb_headers[crumb_field] = crumb_val
                        _push_log(task_id, f"  Jenkins CSRF crumb 获取成功 ({crumb_path})")
                        break
            except Exception:
                continue
        if not crumb_field:
            _push_log(task_id, "  无法获取 Jenkins CSRF crumb，尝试无 CSRF 终止...")

    # ── 阶段 1：取消 Jenkins 队列项 ──
    queue_cancelled = 0
    queue_urls: list = []

    # 1a. 优先使用运行时捕获的队列 URL
    captured_q = task.get("_jenkins_queue_urls", [])
    if isinstance(captured_q, list):
        queue_urls.extend(captured_q)

    # 1b. 从日志中提取队列 URL（兜底）
    raw_logs = task.get("log", [])
    for entry in raw_logs:
        text = entry.get("text", "") if isinstance(entry, dict) else str(entry)
        for m in re.finditer(r'Queue:\s*(https?://\S+/queue/item/\d+/?)\b', text):
            q_url = m.group(1).rstrip("/") + "/"
            if q_url not in queue_urls:
                queue_urls.append(q_url)

    # 去重
    seen_q = set()
    unique_queue_urls = []
    for u in queue_urls:
        if u not in seen_q:
            seen_q.add(u)
            unique_queue_urls.append(u)
    queue_urls = unique_queue_urls

    if queue_urls:
        _push_log(task_id, f"  发现 {len(queue_urls)} 个 Jenkins 队列项，正在取消...")
        for q_url in queue_urls:
            # 提取队列 ID：/queue/item/12345/ → 12345
            qid_match = re.search(r'/queue/item/(\d+)', q_url)
            if not qid_match:
                continue
            qid = qid_match.group(1)
            cancel_url = f"{base_url}/queue/cancelItem?id={qid}" if base_url else ""
            if not cancel_url:
                continue
            try:
                resp = session.post(cancel_url, headers=crumb_headers, timeout=10)
                if resp.status_code < 400:
                    queue_cancelled += 1
                    _push_log(task_id, f"  Jenkins 队列项已取消 (id={qid}): {q_url}")
                else:
                    # Jenkins cancelItem 可能返回 302（重定向）也算成功
                    if resp.status_code in (302, 303):
                        queue_cancelled += 1
                        _push_log(task_id, f"  Jenkins 队列项已取消 (id={qid}, 302): {q_url}")
                    else:
                        _push_log(task_id,
                            f"  取消队列项失败 (id={qid}, HTTP {resp.status_code}): {q_url}")
            except Exception as e:
                _push_log(task_id, f"  取消队列项异常 (id={qid}): {e}")

    if queue_cancelled > 0:
        _push_log(task_id, f"  已取消 {queue_cancelled} 个 Jenkins 队列项")

    # ── 阶段 2：终止正在运行的 Jenkins 构建 ──
    build_urls: set = set()

    # 2a. 优先使用运行时捕获的 build URL
    captured_b = task.get("_jenkins_build_urls", [])
    if isinstance(captured_b, list):
        for u in captured_b:
            u = str(u).strip().rstrip("/") + "/"
            if "jenkins" in u.lower() and "/job/" in u and re.search(r'/\d+/?$', u):
                build_urls.add(u)

    # 2b. TSCAN 独立构建的 tscan_build_url
    tscan_url = str(task.get("tscan_build_url") or "").strip().rstrip("/")
    if tscan_url and "jenkins" in tscan_url.lower() and "/job/" in tscan_url:
        build_urls.add(tscan_url + "/")

    # 2c. 从日志中提取 build URL（兜底）
    for entry in raw_logs:
        text = entry.get("text", "") if isinstance(entry, dict) else str(entry)
        urls = re.findall(r'https?://[^\s"\'<>]+', text)
        for u in urls:
            u = u.rstrip("/.") + "/"
            if "jenkins" not in u.lower() or "/job/" not in u:
                continue
            if "/buildWithParameters" in u or "/api/" in u or "/crumbIssuer" in u:
                continue
            if "/queue/" in u:
                continue  # 队列 URL 已在阶段 1 处理
            if not re.search(r'/\d+/?$', u):
                continue
            build_urls.add(u)

    if not build_urls:
        # 如果既没有队列项也没有构建，给出提示
        if queue_cancelled == 0 and queue_urls:
            _push_log(task_id, "  所有任务均为队列状态，已取消队列项")
        elif queue_cancelled == 0 and not queue_urls:
            _push_log(task_id, "  未找到 Jenkins 构建 URL（可能构建尚未从队列进入构建状态）")
        return queue_cancelled

    aborted = 0
    for build_url in sorted(build_urls):
        # 先检查构建是否还在运行
        running = False
        try:
            api_url = build_url.rstrip("/") + "/api/json"
            api_resp = session.get(api_url, timeout=10)
            if api_resp.status_code == 200:
                build_info = api_resp.json()
                running = bool(build_info.get("building", False))
                if not running:
                    _push_log(task_id, f"  构建已结束，跳过: {build_url}")
                    continue
        except Exception:
            running = True  # 无法确认，假设仍在运行，尝试终止

        # Jenkins stop API: /stop (graceful abort) → /term (force kill)
        stopped = False
        for stop_suffix in ("/stop", "/term"):
            stop_url = build_url.rstrip("/") + stop_suffix
            try:
                resp = session.post(stop_url, headers=crumb_headers, timeout=10)
                if resp.status_code < 400 or resp.status_code in (302, 303):
                    # [2026-06-30] 验证构建是否真正停止
                    try:
                        verify_url = build_url.rstrip("/") + "/api/json"
                        time.sleep(1)  # 给 Jenkins 一点时间处理
                        v_resp = session.get(verify_url, timeout=10)
                        if v_resp.status_code == 200:
                            v_info = v_resp.json()
                            if not bool(v_info.get("building", False)):
                                aborted += 1
                                stopped = True
                                _push_log(task_id, f"  Jenkins 构建已终止（已验证）: {build_url}")
                            else:
                                # 构建仍在运行，继续尝试下一个后缀
                                if stop_suffix == "/stop":
                                    _push_log(task_id,
                                        f"  /stop 已发送但构建仍在运行，尝试 /term...")
                                else:
                                    # /term 也失败了
                                    _push_log(task_id,
                                        f"  /term 已发送但构建仍在运行: {build_url}")
                                    aborted += 1  # 还是算已尝试
                                    stopped = True
                        else:
                            aborted += 1
                            stopped = True
                            _push_log(task_id, f"  Jenkins 构建已终止: {build_url}")
                    except Exception:
                        # 验证失败但 POST 返回成功，保守地认为已终止
                        aborted += 1
                        stopped = True
                        _push_log(task_id, f"  Jenkins 构建终止请求已发送: {build_url}")
                    break
                elif stop_suffix == "/stop":
                    _push_log(task_id,
                        f"  /stop 返回 {resp.status_code}，尝试 /term...")
                else:
                    _push_log(task_id,
                        f"  /term 返回 {resp.status_code}: {build_url}（可能无管理员权限强制终止）")
            except Exception as e:
                if stop_suffix == "/stop":
                    _push_log(task_id, f"  /stop 异常: {e}，尝试 /term...")
                else:
                    _push_log(task_id, f"  终止 Jenkins 构建异常: {build_url} — {e}")

        if not stopped:
            _push_log(task_id,
                f"  终止 Jenkins 构建失败: {build_url}（/stop 和 /term 均未成功，本地进程已终止但 Jenkins 远程构建可能仍在运行）")

    return aborted + queue_cancelled


@app.route("/api/tasks/<task_id>/stop", methods=["POST"])
def api_task_stop(task_id: str):
    """终止正在运行的任务：先停 Jenkins 远程构建，再杀本地进程"""
    err = _check_task_owner(task_id)
    if err: return err
    task = _running_tasks[task_id]

    _push_log(task_id, "🛑 正在终止任务...")

    # 1. 尝试终止 Jenkins 上的远程构建（含队列项取消 + 运行中构建停止）
    jenkins_aborted = _abort_jenkins_builds(task_id, task)
    if jenkins_aborted > 0:
        _push_log(task_id, f"  已终止/取消 {jenkins_aborted} 个 Jenkins 任务（含队列项和构建）")
    else:
        _push_log(task_id, "  未找到可终止的 Jenkins 远程任务")

    # 2. 终止本地子进程
    proc = task.get("proc")
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
        except Exception as e:
            _push_log(task_id, f"  终止本地进程异常: {e}")

    task["status"] = "terminated"
    task["_finished_at"] = time.time()
    _persist_running_tasks()
    _push_log(task_id, "🛑 任务已被手动终止")
    sse_q = _task_streams.get(task_id)
    if sse_q:
        try:
            sse_q.put_nowait({"status": "terminated", "complete": True, "exit_code": -1})
        except queue.Full:
            pass

    return jsonify({"ok": True, "message": "Task terminated"})


@app.route("/api/tasks/<task_id>")
def api_task_status(task_id: str):
    """查询任务状态"""
    err = _check_task_owner(task_id)
    if err: return err
    task = _running_tasks[task_id]
    result = {
        "ok": True,
        "task_id": task_id,
        "project": task["project"],
        "version": task.get("version", ""),
        "task_type": task.get("task_type", "build"),
        "status": task["status"],
        "started_at": task["started_at"],
        # "source_step": task.get("source_step", 7),  # [2026-06-02] 前端不消费
        "log": task.get("log", [])[-30:],  # 最近 30 行日志
    }
    if "exit_code" in task:
        result["exit_code"] = task["exit_code"]
    # changelog 任务附带飞书链接
    fl = task.get("feishu_link", "")
    if fl:
        result["feishu_link"] = fl
    nlm = task.get("feishu_no_link_msg", "")
    if nlm:
        result["feishu_no_link_msg"] = nlm
    return jsonify(result)

def _get_jenkins_job_url(task: dict) -> str:
    """从任务数据中提取 Jenkins job URL（去掉 build number）"""
    # Changelog: 固定 URL
    if task.get("task_type") == "changelog":
        return CHANGELOG_JENKINS_URL if CHANGELOG_JENKINS_URL else ""
    # TSCAN: 从 tscan_build_url 提取
    tscan_url = str(task.get("tscan_build_url") or "").strip().rstrip("/")
    if tscan_url:
        return re.sub(r'/\d+$', '', tscan_url)
    # 正常构建 / skip_jenkins：优先从已捕获的 _jenkins_build_urls 获取（实时准确）
    build_urls = task.get("_jenkins_build_urls", [])
    if build_urls:
        job_url = re.sub(r'/\d+/?$', '', build_urls[0].rstrip('/'))
        if job_url and "/job/" in job_url:
            return job_url
    # Fallback：从日志中提取第一个 Jenkins build URL（兼容旧任务）
    raw_logs = task.get("log", [])
    for entry in raw_logs:
        text = entry.get("text", "") if isinstance(entry, dict) else str(entry)
        urls = re.findall(r'https?://[^\s"\'<>]+', text)
        for u in urls:
            u = u.rstrip("/.")
            if "jenkins" in u.lower() and "/job/" in u:
                # 去掉 build number 得到 job URL
                job_url = re.sub(r'/(\d+|buildWithParameters)$', '', u)
                if job_url and "/job/" in job_url:
                    return job_url
    return ""


@app.route("/api/tasks/<task_id>/stream")
def api_task_stream(task_id: str):
    """SSE 实时日志流端点"""
    err = _check_task_owner(task_id)
    if err: return err
    task = _running_tasks[task_id]

    sse_q = _task_streams.get(task_id)
    if not sse_q:
        return jsonify({"ok": False, "error": "Stream not available"}), 404

    def generate():
        # 发送初始状态：历史日志 + 当前步骤进度（支持断开重连恢复）
        steps_state = task.get("steps", [])
        task_status = task.get("status", "unknown")
        if task_status in ("success",):
            init_total = 100
        else:
            init_total = min(99, sum(s["percent"] for s in steps_state) // max(1, len(steps_state)))
        all_logs = task.get("log", [])
        init_logs = all_logs[-300:] if len(all_logs) > 300 else all_logs
        init_msg = {
            "init": True,
            "logs": init_logs,
            "log_total": len(all_logs),
            "steps": [{"name": s["name"], "status": s["status"], "percent": s["percent"]} for s in steps_state],
            "total_percent": init_total,
            "status": task_status,
            "exit_code": task.get("exit_code"),
            "feishu_link": task.get("feishu_link", ""),
            "feishu_no_link_msg": task.get("feishu_no_link_msg", ""),
            "tscan_status": task.get("_tscan_state", {"enabled": False, "status": "pending", "message": ""}),
            "is_tscan_standalone": task.get("is_tscan_standalone", False),
            "jenkins_job_url": _get_jenkins_job_url(task),
        }
        print(f"[SSE init] task={task_id} status={task_status} logs={len(all_logs)} feishu_link={'yes' if task.get('feishu_link') else 'no'} no_link_msg={'yes' if task.get('feishu_no_link_msg') else 'no'}")
        yield f"data: {json.dumps(init_msg, ensure_ascii=False)}\n\n"

        # 如果任务已经完成/失败/超时/出错，直接发送 complete 并退出，无需等待新消息
        if task_status not in ("running", "starting", "triggering"):
            complete_msg = {
                "status": task_status,
                "exit_code": task.get("exit_code", -1),
                "complete": True,
            }
            fl = task.get("feishu_link", "")
            if fl:
                complete_msg["feishu_link"] = fl
            nlm = task.get("feishu_no_link_msg", "")
            if nlm:
                complete_msg["feishu_no_link_msg"] = nlm
            yield f"data: {json.dumps(complete_msg, ensure_ascii=False)}\n\n"
            return

        while True:
            try:
                msg = sse_q.get(timeout=25)
                yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                if msg.get("complete"):
                    break
            except queue.Empty:
                yield f"data: {json.dumps({'heartbeat': True})}\n\n"
            except GeneratorExit:
                break
        # SSE 断开后清理旧任务数据（30 分钟超时）
        _cleanup_stale_tasks()

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
        },
    )


    return jsonify(result)


@app.route("/api/tasks")
def api_tasks_list():
    """列出当前用户的任务（按 user_id 隔离，新任务在前）"""
    current_user = (request.args.get("user_id") or "").strip()
    tasks = []
    for tid, t in reversed(list(_running_tasks.items())[-50:]):
        # 按 user_id 做数据隔离，无 user_id 的旧任务对所有人可见（向后兼容）
        task_user = (t.get("user_id") or "").strip()
        if current_user and task_user and task_user != current_user:
            continue
        status = t.get("status", "unknown")
        # 进度：优先使用 _push_progress 存储值，已完成的强制 100
        if status in ("success",):
            total_pct = 100
        elif "_total_percent" in t:
            total_pct = int(t["_total_percent"])
        else:
            steps = t.get("steps", [])
            n = max(1, len(steps)) if steps else 1
            total_pct = min(99, sum(s["percent"] for s in steps) // n) if steps else 0
        task_info = {
            "task_id": tid,
            "project": t["project"],
            "version": t.get("version", ""),
            "user_id": task_user,
            "task_type": t.get("task_type", "build"),
            "platform_select": t.get("platform_select", ""),
            "status": status,
            "started_at": t["started_at"],
            "total_percent": total_pct,
            # "source_step": t.get("source_step", 7),  # [2026-06-02] 前端不消费
        }
        # changelog 任务：附带飞书链接和未生成提示
        if t.get("task_type") == "changelog":
            fl = t.get("feishu_link", "")
            nlm = t.get("feishu_no_link_msg", "")
            if fl:
                task_info["feishu_link"] = fl
            if nlm:
                task_info["feishu_no_link_msg"] = nlm
        # 所有任务：附带 feishu_link（如 build 任务也可能有）
        fl_all = t.get("feishu_link", "")
        if fl_all and "feishu_link" not in task_info:
            task_info["feishu_link"] = fl_all
        tasks.append(task_info)
    return jsonify({"ok": True, "tasks": tasks})


@app.route("/api/tasks/<task_id>/remove", methods=["POST"])
def api_task_remove(task_id: str):
    """移除任务。进行中的任务传 force=true 时会先终止 Jenkins 构建再移除。"""
    err = _check_task_owner(task_id)
    if err: return err
    task = _running_tasks[task_id]

    status = task.get("status", "")
    force = request.args.get("force", "").lower() == "true"
    try:
        body = request.get_json(silent=True) or {}
        if body.get("force"):
            force = True
    except Exception:
        pass

    # running/starting 任务需 force=true，移除前先停 Jenkins + 本地进程
    if status in ("running", "starting"):
        if not force:
            return jsonify({"ok": False, "error": "进行中的任务不能移除，请先终止任务或使用 force=true 强制移除"}), 400
        # 终止 Jenkins 远程构建
        try:
            aborted = _abort_jenkins_builds(task_id, task)
            if aborted > 0:
                _push_log(task_id, f"  移除前已终止 {aborted} 个 Jenkins 远程任务")
        except Exception:
            pass
        # 杀本地子进程
        proc = task.get("proc")
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                try: proc.wait(timeout=5)
                except subprocess.TimeoutExpired: proc.kill(); proc.wait(timeout=3)
            except Exception:
                pass
        task["status"] = "terminated"
        task["_finished_at"] = time.time()

    # 清理 SSE 流队列
    sse_q = _task_streams.pop(task_id, None)
    if sse_q:
        try:
            sse_q.put_nowait({"status": "terminated", "complete": True, "exit_code": -2})
        except queue.Full:
            pass

    _running_tasks.pop(task_id, None)
    _persist_running_tasks()
    return jsonify({"ok": True, "message": "Task removed"})


# ══════════════════════════════════════════════════════
# Changelog 版本差分
# ══════════════════════════════════════════════════════

CHANGELOG_JENKINS_URL = "https://jenkins.huami.com/job/DownStream/job/CMP-JIRA-GIT"


@app.route("/api/projects/<project>/start-changelog", methods=["POST"])
def api_start_changelog(project: str):
    """触发 Jenkins CMP-JIRA-GIT 生成版本差分 changelog"""
    try:
        form_data = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "error": "Invalid JSON"}), 400

    device = (form_data.get("device") or project).strip()
    manifest = (form_data.get("manifest") or f"{device}.xml").strip()
    prev_ver_name = (form_data.get("prev_version_name") or "").strip()
    prev_ver_code = (form_data.get("prev_version_code") or "").strip()
    curr_ver_name = (form_data.get("curr_version_name") or "").strip()
    curr_ver_code = (form_data.get("curr_version_code") or "").strip()
    tag = (form_data.get("tag") or "").strip()
    user_id = (form_data.get("user_id") or "").strip()
    j_auth = form_data.get("jenkins_auth") or {}
    j_user = (j_auth.get("username") or "").strip()
    j_pass = (j_auth.get("password") or "").strip()

    if not prev_ver_name or not curr_ver_name:
        return jsonify({"ok": False, "error": "pre 和 after 版本都是必填"}), 400

    # 读取平台选择以确定 Jenkins 跳转 URL
    platform_select = ""
    try:
        cl_cfg_path = SCRIPT_DIR / f"gqf_{device}.json"
        if cl_cfg_path.exists():
            cl_cfg = json.loads(cl_cfg_path.read_text(encoding="utf-8"))
            platform_select = cl_cfg.get("platform_select", "")
    except Exception:
        pass

    task_id = f"changelog_{device}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    _task_streams[task_id] = queue.Queue()
    _running_tasks[task_id] = {
        "project": device,
        "version": f"{prev_ver_name} → {curr_ver_name}",
        "user_id": user_id,
        "task_type": "changelog",
        "platform_select": platform_select,
        "status": "starting",
        "started_at": datetime.now().isoformat(),
        # "source_step": form_data.get("source_step", 7),  # [2026-06-02] 前端不消费
        "log": [],
        "steps": [
            {"name": "获取CSRF令牌", "status": "waiting", "percent": 0},
            {"name": "触发Jenkins构建", "status": "waiting", "percent": 0},
            {"name": "等待构建启动", "status": "waiting", "percent": 0},
            {"name": "构建执行中", "status": "waiting", "percent": 0},
            {"name": "生成差分报告", "status": "waiting", "percent": 0},
        ],
    }

    def _run_changelog():
        steps = _running_tasks[task_id]["steps"]
        try:
            _running_tasks[task_id]["status"] = "running"
            _push_log(task_id, f"Changelog: {device}  {prev_ver_name} → {curr_ver_name}")
            _push_log(task_id, f"Manifest: {manifest}  Tag: {tag}")

            if not j_user or not j_pass:
                _push_log(task_id, "❌ 缺少 Jenkins 认证信息（请先登录）")
                steps[0]["status"] = "failed"
                _push_progress(task_id, steps, total_percent=0)
                _running_tasks[task_id]["status"] = "failed"
                _finish_changelog(task_id, "failed", -1)
                return

            session = requests.Session()
            session.auth = (j_user, j_pass)

            # 获取 Jenkins CSRF crumb（防止 403 错误）
            steps[0]["status"] = "running"
            _push_progress(task_id, steps, total_percent=5)
            crumb = ""
            crumb_field = "Jenkins-Crumb"
            # 候选 crumb URL：1) Jenkins 根路径（最可靠） 2) job 的父级路径
            crumb_urls = [
                f"{CHANGELOG_JENKINS_URL.split('/job/')[0]}/crumbIssuer/api/json",
                f"{CHANGELOG_JENKINS_URL.rsplit('/job/', 1)[0]}/crumbIssuer/api/json",
            ]
            # 去重（保持顺序）
            seen = set()
            unique_crumb_urls = []
            for cu in crumb_urls:
                if cu not in seen:
                    seen.add(cu)
                    unique_crumb_urls.append(cu)
            for cu in unique_crumb_urls:
                try:
                    r = session.get(cu, timeout=10, verify=False)
                    if r.status_code == 200:
                        cd = r.json()
                        crumb = str(cd.get("crumb", ""))
                        crumb_field = str(cd.get("crumbRequestField", "Jenkins-Crumb"))
                        if crumb:
                            _push_log(task_id, f"Changelog: got CSRF crumb from {cu}")
                            steps[0]["status"] = "success"; steps[0]["percent"] = 100
                            _push_progress(task_id, steps, total_percent=15)
                            break
                except Exception as e:
                    _push_log(task_id, f"DEBUG: crumb fetch failed for {cu}: {e}")
            if not crumb:
                _push_log(task_id, "⚠️ 无法获取 CSRF crumb，尝试不带 crumb 发送...")
                steps[0]["status"] = "success"; steps[0]["percent"] = 100
                _push_progress(task_id, steps, total_percent=15)

            params = {
                "PRODUCTION": device,
                "BEFORE_VERSION_NAME": prev_ver_name,
                "BEFORE_VERSION_CODE": prev_ver_code,
                "AFTER_VERSION_NAME": curr_ver_name,
                "AFTER_VERSION_CODE": curr_ver_code,
                "MANIFEST_FILE": manifest,
                "AFTER_MANIFEST_TAG": tag,
                "DIFF_COMPARE_SKIP_JIRA": "YES",
            }
            _push_log(task_id, f"Triggering Jenkins: {CHANGELOG_JENKINS_URL}")
            _push_log(task_id, f"Params: {json.dumps(params, ensure_ascii=False)}")
            steps[1]["status"] = "running"; steps[1]["percent"] = 50
            _push_progress(task_id, steps, total_percent=20)

            trigger_url = f"{CHANGELOG_JENKINS_URL}/buildWithParameters"
            headers = {}
            if crumb:
                headers[crumb_field] = crumb
            try:
                resp = session.post(trigger_url, data=params, headers=headers, allow_redirects=False, timeout=30, verify=False)
            except Exception as e:
                _push_log(task_id, f"❌ Jenkins 触发失败: {e}")
                steps[1]["status"] = "failed"
                _push_progress(task_id, steps, total_percent=0)
                _running_tasks[task_id]["status"] = "failed"
                _finish_changelog(task_id, "failed", -1)
                return

            if resp.status_code not in (200, 201, 302):
                _push_log(task_id, f"❌ Jenkins 返回 {resp.status_code}: {resp.text[:500]}")
                steps[1]["status"] = "failed"
                _push_progress(task_id, steps, total_percent=0)
                _running_tasks[task_id]["status"] = "failed"
                _finish_changelog(task_id, "failed", -1)
                return

            location = resp.headers.get("Location", "")
            _push_log(task_id, f"Jenkins build queued: {location}")
            steps[1]["status"] = "success"; steps[1]["percent"] = 100
            _push_progress(task_id, steps, total_percent=30)

            # 获取实际 build URL（可能重定向到队列页）
            queue_url = location if location else f"{CHANGELOG_JENKINS_URL}/"
            build_url = _resolve_jenkins_build_url(session, queue_url)
            if not build_url:
                _push_log(task_id, "⚠️ 无法解析 build URL，轮询队列页面...")
                build_url = queue_url

            _push_log(task_id, f"Build URL: {build_url}")

            # 等待 build 开始
            _push_log(task_id, "等待 Jenkins 构建开始...")
            steps[2]["status"] = "running"; steps[2]["percent"] = 30
            _push_progress(task_id, steps, total_percent=35)
            build_api_url = ""
            for attempt in range(60):
                time.sleep(10)
                try:
                    api_resp = session.get(build_url.rstrip("/") + "/api/json", timeout=10, verify=False)
                    if api_resp.status_code == 200:
                        build_data = api_resp.json()
                        if build_data.get("building") or build_data.get("result"):
                            build_api_url = build_url.rstrip("/") + "/api/json"
                            steps[2]["status"] = "success"; steps[2]["percent"] = 100
                            steps[3]["status"] = "running"; steps[3]["percent"] = 10
                            _push_progress(task_id, steps, total_percent=45)
                            break
                except Exception:
                    pass
                if attempt % 6 == 0 and attempt > 0:
                    _push_log(task_id, f"  仍在等待... ({attempt * 10}s)")

            if not build_api_url:
                _push_log(task_id, "⚠️ 构建超时未开始，尝试读取 console 输出...")

            # 轮询 console 输出
            console_url = build_url.rstrip("/") + "/consoleText"
            last_pos = 0
            finished = False
            feishu_link = ""
            feishu_no_link_msg = ""  # 未生成差分报告时的提示信息
            poll_count = 0
            for _ in range(360):  # 最多 1 小时
                try:
                    cr = session.get(console_url, timeout=30, verify=False)
                    if cr.status_code == 200:
                        text = cr.text
                        if len(text) > last_pos:
                            new_lines = text[last_pos:].split("\n")
                            for line in new_lines:
                                stripped = line.strip()
                                if stripped:
                                    _push_log(task_id, stripped)
                                    # 从日志中提取飞书差分报告链接
                                    if not feishu_link:
                                        m_link = re.search(r'(差分报告飞书链接[：:\s]*)(https?://\S+)', stripped)
                                        if m_link:
                                            feishu_link = m_link.group(2).rstrip('>,;')
                                            _running_tasks[task_id]["feishu_link"] = feishu_link
                                            _push_log(task_id, f"📎 差分报告链接: {feishu_link}")
                                    # 检测未生成差分报告的情况
                                    if not feishu_no_link_msg:
                                        m_no = re.search(r'[-：]\s*(未生成差分报告.*)', stripped)
                                        if m_no:
                                            feishu_no_link_msg = m_no.group(1).rstrip('>,;')
                                            _running_tasks[task_id]["feishu_no_link_msg"] = feishu_no_link_msg
                                            _push_log(task_id, f"⚠️ {feishu_no_link_msg}")
                            last_pos = len(text)
                except Exception:
                    pass

                # 逐步推进步骤3进度（10% → 80%，360次轮询分摊）
                poll_count += 1
                if poll_count % 12 == 0 and steps[3]["status"] == "running":
                    new_pct = min(80, 10 + int(poll_count / 360 * 70))
                    if new_pct > steps[3]["percent"]:
                        steps[3]["percent"] = new_pct
                        steps[4]["status"] = "running"; steps[4]["percent"] = max(steps[4]["percent"], 5)
                        _push_progress(task_id, steps, total_percent=min(90, 45 + int(poll_count / 360 * 45)))

                # 检查是否完成
                try:
                    ar = session.get(build_api_url or build_url.rstrip("/") + "/api/json", timeout=10, verify=False)
                    if ar.status_code == 200:
                        bd = ar.json()
                        if bd.get("result") and not bd.get("building"):
                            _push_log(task_id, f"Build result: {bd['result']}")
                            finished = True
                            is_success = bd["result"] == "SUCCESS"
                            _running_tasks[task_id]["status"] = "success" if is_success else "failed"
                            if is_success:
                                steps[3]["status"] = "success"; steps[3]["percent"] = 100
                                steps[4]["status"] = "success"; steps[4]["percent"] = 100
                                _push_progress(task_id, steps, total_percent=100)
                                _track_event("changelog",
                                    operator=_running_tasks[task_id].get("user_id", ""),
                                    device=device,
                                    detail=f"{prev_ver_name} → {curr_ver_name}")
                                # 推送飞书链接或未生成提示到前端
                                sse_q = _task_streams.get(task_id)
                                if sse_q:
                                    try:
                                        if feishu_link:
                                            sse_q.put_nowait({"feishu_link": feishu_link})
                                        elif feishu_no_link_msg:
                                            sse_q.put_nowait({"feishu_no_link_msg": feishu_no_link_msg})
                                    except queue.Full:
                                        pass
                            else:
                                steps[3]["status"] = "failed"; steps[3]["percent"] = 0
                                steps[4]["status"] = "failed"; steps[4]["percent"] = 0
                                _push_progress(task_id, steps, total_percent=0)
                            _finish_changelog(task_id, _running_tasks[task_id]["status"], 0 if is_success else 1)
                            return
                except Exception:
                    pass
                time.sleep(10)

            if not finished:
                _push_log(task_id, "⏰ Changelog 构建轮询超时")
                for s in steps: 
                    if s["status"] not in ("success", "failed"): 
                        s["status"] = "failed"
                _push_progress(task_id, steps, total_percent=0)
                _running_tasks[task_id]["status"] = "timeout"
                _finish_changelog(task_id, "timeout", -1)

        except Exception as e:
            _push_log(task_id, f"❌ Changelog 异常: {e}")
            for s in steps: 
                if s["status"] not in ("success", "failed"): 
                    s["status"] = "failed"
            _push_progress(task_id, steps, total_percent=0)
            _running_tasks[task_id]["status"] = "error"
            _finish_changelog(task_id, "error", -1)

    thread = threading.Thread(target=_run_changelog, daemon=True)
    thread.start()
    _running_tasks[task_id]["thread"] = thread

    return jsonify({"ok": True, "task_id": task_id, "message": "Changelog started"})


def _resolve_jenkins_build_url(session, queue_url: str) -> str:
    """从 Jenkins 队列 URL 解析出实际 build URL"""
    for _ in range(30):
        try:
            api_url = queue_url.rstrip("/") + "/api/json"
            r = session.get(api_url, timeout=10, verify=False)
            if r.status_code == 200:
                data = r.json()
                executable = data.get("executable") or {}
                build_url = executable.get("url", "")
                if build_url:
                    return str(build_url)
                # 如果还在排队，检查 why 字段
                why = data.get("why", "")
                if why and "quiet" not in why.lower():
                    pass  # 仍在等待
        except Exception:
            pass
        time.sleep(5)
    return str(queue_url)


def _notify_changelog_done(task: dict, feishu_link: str):
    """发送 Changelog 完成通知 — 同时走 webhook（开发者监控）和飞书 Bot 直发（触发者本人）"""
    import requests as _requests
    try:
        project = task.get("project", "")
        version_info = task.get("version", "")  # e.g. "3.8.0.1 → 3.9.0.1"
        operator = task.get("user_id", "")
        text = f"{project} {version_info} 的 commit changelog 提交差分：{feishu_link}"
        if operator:
            text = f"用户 {operator} 正在编译 — {text}"

        # ── 1. 通过 webhook 发送开发者监控副本（common.json 的固定 URL → guoqifa）──
        common_cfg = json.loads((SCRIPT_DIR / "common.json").read_text(encoding="utf-8"))
        wh = (common_cfg.get("notifications") or {}).get("webhook") or {}
        if wh.get("enabled"):
            wh_url = str(os.environ.get("FEISHU_WEBHOOK_URL", "") or wh.get("url") or "").strip()
            if wh_url:
                payload = {"text": text}
                print(f"[ChangelogNotify] Sending webhook: {wh_url}")
                resp = _requests.post(wh_url, json=payload, timeout=10, verify=bool(wh.get("verify_tls", True)))
                print(f"[ChangelogNotify] Webhook response: {resp.status_code}")

        # ── 2. 如果触发者是飞书 Bot 用户，额外直接通知本人 ──
        if operator and operator.startswith("feishu_bot_"):
            try:
                chat_id = operator[len("feishu_bot_"):]
                direct_text = f"✅ Changelog 已完成：{project} {version_info}\n{feishu_link}"
                _feishu_reply_message(chat_id, direct_text)
                print(f"[ChangelogNotify] Direct message sent to chat_id={chat_id}")
            except Exception as e:
                print(f"[ChangelogNotify] Direct message failed: {e}", file=sys.stderr)
    except Exception as e:
        print(f"[ChangelogNotify] Failed: {e}", file=sys.stderr)


def _finish_changelog(task_id: str, status: str, exit_code: int):
    """发送 changelog 完成信号到 SSE 队列（含飞书链接/未生成提示，确保刷新可恢复）"""
    import time as _time
    task = _running_tasks.get(task_id, {})
    feishu_link = task.get("feishu_link", "")
    feishu_no_link_msg = task.get("feishu_no_link_msg", "")
    # 标记完成时间
    task["_finished_at"] = _time.time()
    task["exit_code"] = exit_code
    _persist_running_tasks()
    sse_q = _task_streams.get(task_id)
    if sse_q:
        try:
            msg = {
                "status": status,
                "exit_code": exit_code,
                "complete": True,
            }
            if feishu_link:
                msg["feishu_link"] = feishu_link
            if feishu_no_link_msg:
                msg["feishu_no_link_msg"] = feishu_no_link_msg
            sse_q.put_nowait(msg)
        except queue.Full:
            pass

    # ── 飞书通知：差分完成 → 发送 webhook ──
    if status == "success" and feishu_link:
        _notify_changelog_done(task, feishu_link)


# ══════════════════════════════════════════════════════
# 飞书长连接 WebSocket 客户端
# ══════════════════════════════════════════════════════

def start_feishu_ws():
    """启动飞书长连接 WebSocket 客户端（后台线程）"""
    import json as _json
    import threading as _threading

    # 读取应用凭证
    common_path = SCRIPT_DIR / "common.json"
    try:
        common = _json.loads(common_path.read_text(encoding="utf-8"))
        feishu_cfg = common.get("feishu", {}).get("oauth", {})
        FEISHU_APP_ID = feishu_cfg.get("app_id", "")
        FEISHU_APP_SECRET = feishu_cfg.get("app_secret", "")
        _VERIFY_TOKEN = feishu_cfg.get("verification_token", "v")
    except Exception:
        FEISHU_APP_ID = FEISHU_APP_SECRET = ""
        _VERIFY_TOKEN = "v"

    if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
        print("[FeishuWS] ERROR: APP_ID/APP_SECRET not configured")
        return

    print(f"[FeishuWS] Connecting... APP_ID={FEISHU_APP_ID[:8]}... token={_VERIFY_TOKEN[:4]}")

    def _ws_run():
        import lark_oapi as lark

        def handle_message(data: lark.im.v1.P2ImMessageReceiveV1):
            print(f"[FeishuWS] EVENT")
            event = getattr(data, 'event', None)
            if not event: return
            message = getattr(event, 'message', None)
            if not message: return
            msg_type = getattr(message, 'msg_type', '') or getattr(message, 'message_type', '') or ''
            if msg_type != 'text': return
            chat_id = getattr(message, 'chat_id', '') or ''
            content_str = getattr(message, 'content', '{}') or '{}'
            try:
                content_obj = _json.loads(content_str)
                text = (content_obj.get("text", "") or "").strip()
            except: 
                text = ""
            if not text or not chat_id: return
            print(f"[FeishuWS] msg: {text[:60]}")
            try:
                _process_feishu_message(chat_id, text, message={"content": content_str})
            except Exception as e:
                import traceback
                print(f"[FeishuWS] err: {e}")
                traceback.print_exc()

        event_handler = lark.EventDispatcherHandler.builder(_VERIFY_TOKEN, "") \
            .register_p2_im_message_receive_v1(handle_message) \
            .build()

        cli = lark.ws.Client(
            FEISHU_APP_ID, FEISHU_APP_SECRET,
            event_handler=event_handler,
            log_level=lark.LogLevel.DEBUG
        )
        cli.start()

    t = _threading.Thread(target=_ws_run, daemon=True, name="feishu-ws")
    t.start()
    print("[FeishuWS] thread started")

def main():
    # 追踪意外退出
    import atexit
    import signal as _signal
    def _on_exit():
        import traceback
        print("[MAIN] Process exiting", flush=True)
    atexit.register(_on_exit)
    def _on_signal(sig, frame):
        print(f"[MAIN] Signal {sig} received", flush=True)
        import os; os._exit(0)
    try:
        _signal.signal(_signal.SIGTERM, _on_signal)
        _signal.signal(_signal.SIGINT, _on_signal)
    except: pass

    # Windows 控制台 UTF-8 编码（防 emoji/符号 崩溃）
    import sys as _sys
    try:
        _sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    import argparse
    ap = argparse.ArgumentParser(description="固件发布 Web 管理后台")
    ap.add_argument("--host", default="0.0.0.0", help="监听地址 (默认 0.0.0.0)")
    ap.add_argument("--port", type=int, default=5000, help="监听端口 (默认 5000)")
    ap.add_argument("--debug", action="store_true", help="调试模式")
    ap.add_argument("--no-feishu-ws", action="store_true", help="禁用飞书长连接 WebSocket")
    args = ap.parse_args()

    print(f"""
╔══════════════════════════════════════════════════╗
║     固件发布 Web 管理后台                          ║
║     http://{args.host}:{args.port}                         ║
║     项目目录: {SCRIPT_DIR}
╚══════════════════════════════════════════════════╝
""")

    # 启动飞书长连接 WebSocket（后台线程）
    if not getattr(args, 'no_feishu_ws', False):
        start_feishu_ws()

    print("[MAIN] Starting Flask...", flush=True)
    try:
        app.run(host=args.host, port=args.port, debug=args.debug)
    except Exception as e:
        import traceback
        print(f"[MAIN] Flask crashed: {e}", flush=True)
        traceback.print_exc()
    print("[MAIN] Flask exited", flush=True)


if __name__ == "__main__":
    from credential_resolver import load_dotenv
    load_dotenv()
    try:
        main()
    except Exception as e:
        import traceback
        print(f"[MAIN] Fatal: {e}", flush=True)
        traceback.print_exc()
