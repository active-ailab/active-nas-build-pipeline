#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飞书多维表格（Base）连通性自检工具
==================================
用途：在把 base_token / table_id 填进 common.json 的 feishu.base 之后，
      跑一次本脚本，确认「应用身份 + 表格权限 + 字段匹配」三者都通。

用法：
    python verify_feishu_base.py                # 只读检查（列字段，不写数据）
    python verify_feishu_base.py --write-test   # 额外写入一条测试记录（会真写进表里）

检查项：
    1. .env 的 FEISHU_APP_ID / FEISHU_APP_SECRET 能否换到 tenant_access_token
    2. common.json 的 feishu.base 是否已填 base_token / table_id / enabled
    3. 用 base_token + table_id 能否读到数据表字段列表（验证 token 正确 + 机器人有权限）
    4. 配置里的 fields 映射，右侧列名是否都能在表里找到（不匹配会提示实际可用列名）
    5. --write-test：写入一条 __连通性测试__ 记录并回读确认

退出码：0=全部通过，1=有问题（会打印具体是哪一步失败）
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from credential_resolver import load_dotenv  # noqa: E402

API = "https://open.feishu.cn/open-apis"

OK = "  [OK]   "
BAD = "  [FAIL] "
WARN = "  [WARN] "


def _http(method: str, path: str, *, token: str = "", body: Dict[str, Any] | None = None,
          timeout: int = 20) -> Dict[str, Any]:
    url = f"{API}{path}"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, method=method, data=data)
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"code": -1, "msg": f"HTTP {e.code}: {raw[:400]}"}


def main() -> int:
    ap = argparse.ArgumentParser(description="飞书多维表格连通性自检")
    ap.add_argument("--write-test", action="store_true", help="额外写入一条测试记录到表里")
    ap.add_argument("--project", default="", help="指定项目配置文件（默认 common.json）")
    args = ap.parse_args()

    print("=" * 72)
    print("飞书多维表格 (Base) 连通性自检")
    print("=" * 72)

    load_dotenv()

    # ── 1. 凭据 ──
    import os
    app_id = os.environ.get("FEISHU_APP_ID", "").strip()
    app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
    print("\n[1/5] 应用凭据")
    if not app_id or not app_secret:
        print(BAD + "FEISHU_APP_ID / FEISHU_APP_SECRET 未配置（检查 .env）")
        return 1
    print(OK + f"app_id = {app_id}")

    d = _http("POST", "/auth/v3/tenant_access_token/internal",
              body={"app_id": app_id, "app_secret": app_secret})
    if d.get("code") != 0:
        print(BAD + f"换取 tenant_access_token 失败: code={d.get('code')} msg={d.get('msg')}")
        return 1
    token = d["tenant_access_token"]
    print(OK + "tenant_access_token 获取成功")

    # ── 2. 配置 ──
    cfg_file = args.project or "common.json"
    print(f"\n[2/5] 读取配置 {cfg_file} 的 feishu.base")
    cfg_path = ROOT / cfg_file
    if not cfg_path.exists():
        print(BAD + f"配置文件不存在: {cfg_path}")
        return 1
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    base_cfg = (cfg.get("feishu") or {}).get("base") or {}

    base_token = str(base_cfg.get("base_token") or "").strip()
    table_id = str(base_cfg.get("table_id") or "").strip()
    enabled = bool(base_cfg.get("enabled", False))
    fields_map = base_cfg.get("fields") or {}

    if not base_token:
        print(BAD + "feishu.base.base_token 为空 —— 从表格 URL 的 /base/ 后面取")
        return 1
    if not table_id:
        print(BAD + "feishu.base.table_id 为空 —— 从表格 URL 的 ?table= 后面取")
        return 1
    print(OK + f"base_token = {base_token}")
    print(OK + f"table_id   = {table_id}")
    if not enabled:
        print(WARN + "feishu.base.enabled = false —— 流水线不会写入，验证通过记得改成 true")
    else:
        print(OK + "feishu.base.enabled = true")

    # ── 3. 读表结构 ──
    print("\n[3/5] 读取数据表结构（验证 token 正确 + 机器人有权限）")
    r = _http("GET", f"/bitable/v1/apps/{base_token}/tables/{table_id}/fields?page_size=200", token=token)
    if r.get("code") != 0:
        code = r.get("code")
        print(BAD + f"读取字段失败: code={code} msg={r.get('msg')}")
        if code == 1254005 or "not exist" in str(r.get("msg", "")):
            print("        → base_token 或 table_id 不对，重新从 URL 核对")
        elif code in (99991672, 99991663, 1254006) or "permission" in str(r.get("msg", "")).lower():
            print("        → 机器人没有这张表的权限：表格右上角「···」→ 添加文档应用 → 加为可编辑")
        elif "scope" in str(r.get("msg", "")).lower() or code == 99991672:
            print("        → 开发者后台未开通「多维表格 bitable:app」权限")
        return 1

    items = (r.get("data") or {}).get("items") or []
    actual_fields = [str(i.get("field_name") or "") for i in items]
    print(OK + f"读取成功，表里共 {len(actual_fields)} 个字段：")
    for f in actual_fields:
        print(f"          - {f}")

    # ── 4. 字段映射校验 ──
    print("\n[4/5] 校验 fields 映射（左=系统内部字段，右=表里实际列名）")
    missing = []
    for k, v in fields_map.items():
        v = str(v or k)
        if v in actual_fields:
            print(OK + f"{k}  ->  {v}")
        else:
            print(BAD + f"{k}  ->  {v}  （表里没有这一列）")
            missing.append((k, v))
    if missing:
        print(WARN + "以下列名在表里找不到，请改成实际列名：")
        for k, v in missing:
            print(f"          {k}: 现在是 '{v}'，可用列名见上方列表")

    # ── 5. 写入测试 ──
    if not args.write_test:
        print("\n[5/5] 跳过写入测试（加 --write-test 可执行真写入验证）")
        print("\n" + "=" * 72)
        print("结论：连通性检查通过。确认无误后把 feishu.base.enabled 改为 true 即可。")
        print("=" * 72)
        return 0 if not missing else 1

    print("\n[5/5] 写入一条测试记录")
    rec: Dict[str, str] = {}
    for k, v in fields_map.items():
        rec[str(v or k)] = f"__连通性测试__{k}"
    w = _http("POST", f"/bitable/v1/apps/{base_token}/tables/{table_id}/records",
              token=token, body={"fields": rec})
    if w.get("code") != 0:
        print(BAD + f"写入失败: code={w.get('code')} msg={w.get('msg')}")
        if "permission" in str(w.get("msg", "")).lower():
            print("        → 机器人对这张表只有只读权限，需要改成「可编辑」")
        return 1
    rid = (w.get("data") or {}).get("record", {}).get("record_id", "")
    print(OK + f"写入成功 record_id = {rid}")
    print(WARN + "请到表里删掉这条 __连通性测试__ 记录")

    print("\n" + "=" * 72)
    print("结论：全部通过，多维表格可以正常写入。记得把 enabled 改成 true。")
    print("=" * 72)
    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())
