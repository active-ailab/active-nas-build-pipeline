#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飞书 CLI 发布流水线 — 统一入口
================================
重构后的一站式命令，取代原有散落的多个 Python 脚本。

用法:
    # 初始化飞书 CLI（首次使用）
    python lark_release.py setup

    # 运行完整发布流水线
    python lark_release.py run --config gqf_windermere.json

    # 仅飞书操作（跳过 Jenkins/NAS 上传）
    python lark_release.py run --config gqf_windermere.json --skip-download --skip-upload

    # 仅创建飞书文档（不上传 NAS）
    python lark_release.py doc --config gqf_windermere.json

    # 查看帮助
    python lark_release.py --help
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


# ── 常量 ──────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
PIPELINE_SCRIPT = SCRIPT_DIR / "release_pipeline_run.py"
TRIGGER_SCRIPT = SCRIPT_DIR / "jenkins_trigger_build.py"
ADAPTER_SCRIPT = SCRIPT_DIR / "lark_cli_adapter.py"


# ── 工具函数 ──────────────────────────────────────────────
def _run_script(script: Path, args: List[str], *, description: str = "") -> int:
    """运行 Python 脚本，实时流式输出 stdout/stderr"""
    cmd = [sys.executable, "-u", str(script)] + args
    print(f"\n{'='*60}")
    print(f">> {description or script.name}")
    print(f"   {' '.join(cmd)}")
    print(f"{'='*60}\n", flush=True)

    # 使用 Popen 替代 run()，确保子进程输出实时流向父进程
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    proc = subprocess.Popen(
        cmd,
        cwd=str(SCRIPT_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )

    # 逐行读取子进程输出，立即 flush 到父进程 stdout
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()

    proc.wait()
    return proc.returncode


def _load_config(config_path: str) -> Dict[str, Any]:
    """加载配置 JSON"""
    path = Path(config_path)
    if not path.is_absolute():
        path = SCRIPT_DIR / path
    if not path.exists():
        raise SystemExit(f"配置文件不存在: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _print_banner():
    print("""
+==============================================+
|     Lark Release Pipeline (Feishu CLI)        |
|     Build -> NAS Upload -> Feishu Doc         |
+==============================================+
""")


# ── 命令实现 ──────────────────────────────────────────────

def cmd_setup(args: argparse.Namespace) -> int:
    """一键初始化飞书 CLI 环境"""
    _print_banner()
    
    # Step 1: 安装 lark-cli
    print("[PKG] Step 1/3: 安装飞书 CLI...")
    try:
        subprocess.run(
            ["npx", "@larksuite/cli@latest", "install"],
            check=False, timeout=120,
        )
        print("[OK] lark-cli 安装完成")
    except Exception as e:
        print(f"[WARN]  自动安装失败: {e}", file=sys.stderr)
        print("   请手动执行: npx @larksuite/cli@latest install", file=sys.stderr)
    
    # Step 2: 初始化配置
    print("\n[CFG] Step 2/3: 初始化飞书应用...")
    try:
        subprocess.run(
            ["lark-cli", "config", "init"],
            check=False, timeout=30,
        )
        print("[OK] 应用配置完成")
    except Exception as e:
        print(f"[WARN]  跳过: {e}", file=sys.stderr)
    
    # Step 3: 用户认证
    print("\n[AUTH] Step 3/3: 用户认证（请在弹出的页面确认）...")
    try:
        subprocess.run(
            ["lark-cli", "auth", "login"],
            check=False, timeout=120,
        )
        print("[OK] 认证完成")
    except Exception as e:
        print(f"[WARN]  认证未完成: {e}", file=sys.stderr)
        print("   稍后执行: lark-cli auth login", file=sys.stderr)
    
    # 提示按域授权
    print("\n[TIP] 推荐按业务域授权:")
    for domain, desc in [("docs", "云文档"), ("drive", "云空间"), ("wiki", "知识库"), ("im", "消息通知")]:
        print(f"   lark-cli auth login --domain {domain}  # {desc}")
    
    print("\n[OK] 初始化完成！现在可以运行: python lark_release.py run --config <项目>.json")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """运行完整发布流水线"""
    _print_banner()
    
    # 构建 release_pipeline_run.py 的参数
    pipeline_args = ["--config", args.config]
    
    if args.only:
        pipeline_args.extend(["--only", args.only])
    if args.skip_download:
        pipeline_args.append("--skip-download")
    if args.skip_prepare:
        pipeline_args.append("--skip-prepare")
    if args.skip_upload:
        pipeline_args.append("--skip-upload")
    if args.skip_share:
        pipeline_args.append("--skip-share")
    if args.skip_doc:
        pipeline_args.append("--skip-doc")
    if args.skip_feishu:
        pipeline_args.append("--skip-feishu")
    if args.dry_run:
        pipeline_args.append("--dry-run")
    if args.no_progress:
        pipeline_args.append("--no-progress")
    if args.doc_output:
        pipeline_args.extend(["--doc-output", args.doc_output])
    if args.feishu_preauth:
        pipeline_args.append("--feishu-preauth")
    if args.feishu_refresh:
        pipeline_args.extend(["--feishu-refresh", "--feishu-target", args.feishu_refresh])
    
    # 额外参数透传
    if args.extra_args:
        pipeline_args.extend(args.extra_args)
    
    return _run_script(
        PIPELINE_SCRIPT, pipeline_args,
        description=f"发布流水线: {args.config}",
    )


def cmd_doc(args: argparse.Namespace) -> int:
    """生成飞书版本文档（基于已有的本地 Markdown）"""
    _print_banner()
    
    cfg = _load_config(args.config)
    feishu_cfg = (cfg.get("feishu") or {}) if isinstance(cfg.get("feishu"), dict) else {}
    
    if not feishu_cfg.get("enabled"):
        print("[WARN]  feishu.enabled=false，无法生成飞书文档", file=sys.stderr)
        return 1
    
    release_cfg = cfg.get("release") or {}
    project = release_cfg.get("project", "unknown")
    version = release_cfg.get("version", "unknown")
    
    # 查找本地 Markdown 文档
    if args.md_file:
        md_path = Path(args.md_file)
    else:
        # 自动查找最近的版本文档
        doc_dir = Path(args.doc_output) if args.doc_output else (Path.home() / "release_docs")
        if not doc_dir.exists():
            print(f"[ERR] 文档目录不存在: {doc_dir}", file=sys.stderr)
            return 1
        # 找最新的 md 文件
        md_files = sorted(doc_dir.glob(f"*{project}*{version}*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not md_files:
            md_files = sorted(doc_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not md_files:
            print(f"[ERR] 在 {doc_dir} 中找不到版本文档", file=sys.stderr)
            return 1
        md_path = md_files[0]
    
    print(f"[DOC] 使用本地文档: {md_path}")
    md_content = md_path.read_text(encoding="utf-8")
    
    # 文档标题
    name_tmpl = str(feishu_cfg.get("name_template") or "{project}_{version}_{timestamp}_版本文档").strip()
    now = datetime.now()
    doc_name = name_tmpl.format(
        project=project,
        version=version,
        timestamp=now.strftime("%Y%m%d_%H%M%S"),
    )
    
    # 调用适配器
    from lark_cli_adapter import lark_publish_release_document, lark_auth_ensure
    
    if not args.dry_run:
        lark_auth_ensure()
    
    result = lark_publish_release_document(
        title=doc_name,
        markdown_content=md_content,
        dry_run=args.dry_run,
    )
    
    if result.doc_url:
        print(f"\n[OK] 飞书文档已创建: {result.doc_url}")
    else:
        print(f"\n[WARN]  文档生成结果: {result.to_status_text()}")
    
    return 0


def cmd_trigger(args: argparse.Namespace) -> int:
    """触发 Jenkins 构建"""
    _print_banner()
    return _run_script(
        TRIGGER_SCRIPT, ["--config", args.config],
        description=f"触发 Jenkins 构建: {args.config}",
    )


def cmd_status(args: argparse.Namespace) -> int:
    """查看飞书 CLI 状态"""
    _print_banner()
    print("[?] 飞书 CLI 状态检查\n")
    
    # 检查 CLI 安装
    try:
        result = subprocess.run(["lark-cli", "--version"], capture_output=True, text=True, timeout=5)
        print(f"[OK] lark-cli: {result.stdout.strip()}")
    except Exception:
        print("[ERR] lark-cli 未安装")
        print("   安装: npx @larksuite/cli@latest install")
        return 1
    
    # 检查认证
    try:
        result = subprocess.run(["lark-cli", "auth", "status"], capture_output=True, text=True, timeout=10)
        print(f"[OK] 认证状态: OK")
    except Exception:
        print("[WARN]  未认证: lark-cli auth login")
    
    # 检查授权域
    try:
        result = subprocess.run(["lark-cli", "auth", "check"], capture_output=True, text=True, timeout=10)
        perms = (result.stdout or "").strip()
        if perms:
            print(f"[CFG] 已授权域: {perms}")
        else:
            print("[CFG] 已授权域: (无)")
    except Exception:
        pass
    
    # 检查适配器
    try:
        from lark_cli_adapter import _LARK_CLI_AVAILABLE
        print("[OK] lark_cli_adapter.py: 可用")
    except ImportError:
        print("[ERR] lark_cli_adapter.py: 未找到")
    
    # 检查配置文件
    configs = sorted(SCRIPT_DIR.glob("gqf_*.json"))
    if configs:
        print(f"\n📁 可用配置文件 ({len(configs)} 个):")
        for c in configs:
            try:
                cfg_data = json.loads(c.read_text(encoding="utf-8"))
                proj = (cfg_data.get("release") or {}).get("project", "?")
                ver = (cfg_data.get("release") or {}).get("version", "?")
                print(f"   {c.name:30s} → {proj} v{ver}")
            except Exception:
                print(f"   {c.name:30s} → (无效配置)")
    
    return 0


# ── CLI 定义 ──────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="飞书 CLI 发布流水线 — 固件构建 → NAS上传 → 飞书文档",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python lark_release.py setup                                  # 初始化飞书 CLI
  python lark_release.py run --config gqf_windermere.json       # 完整发布流水线
  python lark_release.py run --config gqf_windermere.json --only release  # 仅 Release
  python lark_release.py run --config gqf_windermere.json --dry-run        # 演练模式
  python lark_release.py doc --config gqf_windermere.json       # 仅生成飞书文档
  python lark_release.py trigger --config gqf_windermere.json   # 仅触发 Jenkins
  python lark_release.py status                                 # 查看状态
        """,
    )
    
    sub = ap.add_subparsers(dest="command", help="可用命令")
    
    # setup
    p_setup = sub.add_parser("setup", help="一键初始化飞书 CLI 环境")
    
    # run
    p_run = sub.add_parser("run", help="运行完整发布流水线")
    p_run.add_argument("--config", required=True, help="项目配置文件 (如 gqf_windermere.json)")
    p_run.add_argument("--only", choices=["all", "debug", "release"], default="all")
    p_run.add_argument("--skip-download", action="store_true")
    p_run.add_argument("--skip-prepare", action="store_true")
    p_run.add_argument("--skip-upload", action="store_true")
    p_run.add_argument("--skip-share", action="store_true")
    p_run.add_argument("--skip-doc", action="store_true")
    p_run.add_argument("--skip-feishu", action="store_true")
    p_run.add_argument("--dry-run", action="store_true")
    p_run.add_argument("--no-progress", action="store_true")
    p_run.add_argument("--doc-output", default="")
    p_run.add_argument("--feishu-preauth", action="store_true")
    p_run.add_argument("--feishu-refresh", default="")
    p_run.add_argument("extra_args", nargs="*", help="额外透传参数")
    
    # doc
    p_doc = sub.add_parser("doc", help="仅生成/更新飞书版本文档")
    p_doc.add_argument("--config", required=True, help="项目配置文件")
    p_doc.add_argument("--md-file", default="", help="指定 Markdown 文件路径")
    p_doc.add_argument("--doc-output", default="", help="版本文档目录")
    p_doc.add_argument("--dry-run", action="store_true")
    
    # trigger
    p_trigger = sub.add_parser("trigger", help="仅触发 Jenkins 构建")
    p_trigger.add_argument("--config", required=True)
    
    # status
    p_status = sub.add_parser("status", help="查看飞书 CLI 状态")
    
    args = ap.parse_args()
    
    if not args.command:
        ap.print_help()
        print("\n💡 快速开始:")
        print("  1. python lark_release.py setup                 # 初始化")
        print("  2. python lark_release.py run --config gqf_windermere.json  # 发布")
        return 0
    
    # 分发命令
    commands = {
        "setup": cmd_setup,
        "run": cmd_run,
        "doc": cmd_doc,
        "trigger": cmd_trigger,
        "status": cmd_status,
    }
    
    handler = commands.get(args.command)
    if handler:
        return handler(args)
    else:
        print(f"未知命令: {args.command}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
