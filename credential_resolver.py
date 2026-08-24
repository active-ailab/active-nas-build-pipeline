#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一凭证解析模块
================
所有脚本共用的环境变量优先凭证读取逻辑。

规则：
  1. 优先读取环境变量
  2. 若 env 不存在，fallback 到 JSON 配置中的值
  3. 若 JSON 中值为 ${ENV:VARNAME} 占位符，自动解析 env

使用方式：
  from credential_resolver import resolve_password, resolve_app_secret, resolve_token, CREDENTIAL_ENV_MAP
"""

import os
from pathlib import Path
from typing import Any, Dict, Optional

# ── 环境变量映射表 ──────────────────────────────────
# 格式: { json_config_path: env_var_name }
CREDENTIAL_ENV_MAP: Dict[str, str] = {
    # Jenkins
    "jenkins.auth.password":    "JENKINS_PASSWORD",
    "jenkins.auth.username":    "JENKINS_USERNAME",
    # NAS WebDAV
    "nas.webdav.auth.password": "NAS_WEBDAV_PASSWORD",
    "nas.webdav.auth.username": "NAS_WEBDAV_USERNAME",
    # NAS DSM
    "nas.dsm.auth.password":    "NAS_DSM_PASSWORD",
    "nas.dsm.auth.username":    "NAS_DSM_USERNAME",
    # Feishu OAuth
    "feishu.oauth.app_id":      "FEISHU_APP_ID",
    "feishu.oauth.app_secret":  "FEISHU_APP_SECRET",
    # Feishu user token
    "feishu.user_access_token": "FEISHU_USER_ACCESS_TOKEN",
    "feishu.user_refresh_token":"FEISHU_USER_REFRESH_TOKEN",
    # LLM / DeepSeek
    "llm.api_key":              "DEEPSEEK_API_KEY",
}


def _expand_env_placeholder(value: str) -> str:
    """解析 ${ENV:VARNAME} 占位符"""
    if isinstance(value, str) and value.startswith("${ENV:") and value.endswith("}"):
        env_name = value[6:-1]
        return os.environ.get(env_name, "")
    return value


def resolve_credential(
    config_value: str,
    env_var: str,
    *,
    required: bool = False,
    sensitive: bool = True,
    log_hint: bool = True,
) -> str:
    """
    统一凭证解析：env > config > env_placeholder

    Args:
        config_value: JSON 配置中读取的值
        env_var: 环境变量名
        required: 若为 True，凭证为空时抛出异常
        sensitive: 若为 True，不在日志中打印值
        log_hint: 若为 True 且值为空，打印提示
        
    Returns:
        解析后的凭证字符串
    """
    # 1. 环境变量优先
    env_val = os.environ.get(env_var, "").strip()
    if env_val:
        return env_val

    # 2. 配置值（可能是 ${ENV:...} 占位符）
    resolved = _expand_env_placeholder(str(config_value or "").strip())
    if resolved:
        return resolved

    # 3. 为空
    if required:
        raise RuntimeError(
            f"Missing credential: set env {env_var} or configure it in JSON config"
        )
    if log_hint and not env_val:
        import sys
        print(
            f"WARN: credential '{env_var}' not set in env; using JSON config value (may be empty)",
            file=sys.stderr,
        )
    return ""


def resolve_password(config_value: str, env_var: str, *, required: bool = True) -> str:
    """解析密码类凭证"""
    return resolve_credential(config_value, env_var, required=required, sensitive=True)


def resolve_username(config_value: str, env_var: str, *, required: bool = True) -> str:
    """解析用户名"""
    return resolve_credential(config_value, env_var, required=required, sensitive=False)


def resolve_app_secret(config_value: str, env_var: str = "FEISHU_APP_SECRET") -> str:
    """解析飞书 app_secret"""
    return resolve_credential(config_value, env_var, required=True, sensitive=True)


def resolve_token(config_value: str, env_var: str, *, required: bool = False) -> str:
    """解析 Token 类凭证"""
    return resolve_credential(config_value, env_var, required=required, sensitive=True)


def sanitize_config_for_display(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """移除敏感字段，返回安全展示副本（用于 Web 前端）"""
    import json
    safe = json.loads(json.dumps(cfg))
    _mask = lambda s: (s[:4] + "****") if s and len(s) > 8 else "****"
    
    try:
        safe["jenkins"]["auth"]["password"] = _mask(safe["jenkins"]["auth"]["password"])
    except Exception:
        pass
    try:
        for section in ("webdav", "dsm"):
            safe["nas"][section]["auth"]["password"] = _mask(safe["nas"][section]["auth"]["password"])
    except Exception:
        pass
    try:
        fei = safe["feishu"]
        for key in ("user_access_token", "user_refresh_token"):
            if fei.get(key):
                fei[key] = _mask(fei[key])
        if fei.get("oauth", {}).get("app_secret"):
            fei["oauth"]["app_secret"] = _mask(fei["oauth"]["app_secret"])
    except Exception:
        pass
    return safe


def load_dotenv(path=None, *, override: bool = False) -> bool:
    """极简 .env 加载（零第三方依赖）。

    把 .env 文件中的 `KEY=VALUE` 注入 `os.environ`，供 `resolve_*` 系列函数读取。

    Args:
        path: .env 文件路径。为 None 时依次尝试：
              1) 当前工作目录下的 `.env`
              2) 本文件所在目录（项目根）下的 `.env`
        override: 为 True 时覆盖已存在的环境变量；默认 False（setdefault 语义）。

    支持：`#` 注释、空行、`KEY=VALUE`、`KEY="VALUE"`、`KEY='VALUE'`（值可含 =）。
    """
    if path is None:
        candidates = [
            Path.cwd() / ".env",
            Path(__file__).resolve().parent / ".env",
        ]
        path = next((p for p in candidates if p.exists()), None)
        if path is None:
            return False

    env_path = Path(path)
    if not env_path.exists() or not env_path.is_file():
        return False

    loaded = 0
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if not key:
            continue
        if override or os.environ.get(key) is None:
            os.environ[key] = value
            loaded += 1

    return loaded > 0
