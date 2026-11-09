"""Identify the exact quality-check run and platform-specific TSCAN reports."""
import re
from urllib.parse import urlsplit, urlunsplit


QUALITY_JOB = "HuamiOS_multi_platform_quality_check"


def is_multi_tscan_parent(url):
    return bool(re.search(r"/job/HuamiOS_multi_platform/\d+/?$", urlsplit(url).path))


def quality_build_from_console(parent_url, console):
    numbers = set(re.findall(r"(?:Starting building:|Build\s+)[^\r\n]*?\b"
                             + QUALITY_JOB + r"\s+#(\d+)\b", console))
    if len(numbers) > 1:
        raise RuntimeError("TSCAN parent references multiple quality-check builds; cannot select uniquely")
    if not numbers:
        return None
    number = int(next(iter(numbers)))
    parsed = urlsplit(parent_url)
    path = re.sub(r"/job/HuamiOS_multi_platform/\d+/?$",
                  f"/job/{QUALITY_JOB}/{number}/", parsed.path)
    return number, urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def selected_platforms(cfg):
    value = cfg.get("platform_select") or (cfg.get("_tscan") or {}).get("platform")
    if not value:
        value = [k for k, v in (cfg.get("Platform") or {}).items() if str(v).upper() == "YES"]
    if isinstance(value, str):
        value = value.split(",")
    return [str(v).strip().lower() for v in (value or []) if re.fullmatch(r"[\w-]+", str(v).strip())]


def tscan_pattern(kind, cfg):
    platforms = selected_platforms(cfg)
    prefix = "(?:" + "|".join(re.escape(v) for v in platforms) + ")" if platforms else r"[\w-]+"
    return re.compile(rf"^(?:{prefix}_)?{re.escape(kind)}_tscanResult\.zip$", re.IGNORECASE)
