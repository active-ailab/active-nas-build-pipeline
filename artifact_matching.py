"""Select release artifacts by role, project and version, never by name length."""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Pattern, Tuple
from tscan_support import tscan_pattern


class ArtifactSelectionError(RuntimeError):
    """Several different artifacts match a single document field."""


PREPARE_ROLES = {
    "release_top_tgz": "{{REL_RELEASE_FULL_ARCHIVE}}",
    "debug_full_archive_tgz": "{{REL_MONKEY_FULL_ARCHIVE}}",
    "ota_cloud_tgz": "{{REL_RELEASE_OTA_CLOUD_ARCHIVE}}",
    "ota_sleep_tgz": "{{REL_RELEASE_OTA_SLEEP_ARCHIVE}}",
    "ota_sign_zip": "{{REL_RELEASE_OTA_SIGN_ZIP}}",
    "release_ota_zip": "{{REL_RELEASE_SENSORHUB_OTA_ZIP}}",
    "ota_sleep_zip": "{{PREPARE_SLEEP_ZIP}}",
    "ota_tgz": "{{PREPARE_OTA_ARCHIVE}}",
    "boot_tgz": "{{PREPARE_BOOT_ARCHIVE}}",
    "recovery_tgz": "{{PREPARE_RECOVERY_ARCHIVE}}",
    "boot_manifest_xml": "{{REL_BOOTLOADER_MANIFEST_INFO}}",
    "recovery_manifest_xml": "{{REL_RECOVERY_MANIFEST_INFO}}",
    "watch_elf": "{{PREPARE_WATCH_ELF}}",
}


def artifact_pattern(placeholder: str, cfg: Dict[str, Any]) -> Optional[Pattern[str]]:
    for kind in ("BOOT", "OTA", "RECOVERY"):
        if placeholder.strip("{}") == "REL_TSCAN_" + kind:
            return tscan_pattern(kind, cfg)
    release = cfg.get("release") or {}
    project = str(release.get("project") or release.get("device_name") or "").strip()
    p = re.escape(project) if project else r"(?!)"
    version = r"[0-9]+\.[0-9]+\.[0-9]+(?:\.[0-9]+)?"
    v = version + r"(?:_.*)?"
    md5 = rf"^(?:{p}(?:_{version})?_md5|md5(?:sum)?)\.txt$"
    patterns = {
        "REL_RELEASE_FULL_ARCHIVE": rf"^archive_{p}_{v}\.tgz$",
        "REL_MONKEY_FULL_ARCHIVE": rf"^archive_{p}_{v}\.tgz$",
        "REL_RELEASE_LOG_DIST_BZ2_BIN": rf"^log_dict_bz2_{p}_[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+(?:_[^/]+)?\.bin$",
        "REL_MONKEY_LOG_DIST_BZ2_BIN": rf"^log_dict_bz2_{p}_[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+(?:_[^/]+)?\.bin$",
        "REL_RELEASE_OTA_CLOUD_ARCHIVE": rf"^archive_OTA_CLOUD_{p}_{v}\.tgz$",
        "REL_RELEASE_OTA_SLEEP_ARCHIVE": rf"^archive_OTA_SLEEP_{p}_{v}\.tgz$",
        "REL_RELEASE_OTA_SIGN_ZIP": r"^watch@[^/]+_ota_sign\.zip$",
        "REL_RELEASE_FACTORY_TOOL_ZIP": rf"^(?:{p}_factory_{v}|factory_{p}_{v})\.zip$",
        "REL_RELEASE_SENSORHUB_OTA_ZIP": rf"^sensorhub_ota_{p}_{v}\.zip$",
        "REL_RELEASE_SENSORHUB_FCT_ZIP": rf"^sensorhub_fct_{p}_{v}\.zip$",
        "REL_MONKEY_SENSORHUB_OTA_ZIP": rf"^sensorhub_ota_{p}_{v}\.zip$",
        "REL_MONKEY_SENSORHUB_FCT_ZIP": rf"^sensorhub_fct_{p}_{v}\.zip$",
        "REL_APP_MANIFEST_INFO": rf"^manifest_{p}_{v}\.xml$",
        "REL_BOOTLOADER_MANIFEST_INFO": rf"^manifest_BOOT_{p}_{v}\.xml$",
        "REL_RECOVERY_MANIFEST_INFO": rf"^manifest_RECOVERY_{p}_{v}\.xml$",
        "REL_MD5SUM_FILE": md5,
        "REL_RELEASE_MD5": md5,
        "REL_BUILD_LOG_FILE": r"^jenkins_release_console\.log$",
        "REL_TSCAN_BOOT": r"^BOOT_tscanResult\.zip$",
        "REL_TSCAN_OTA": r"^OTA_tscanResult\.zip$",
        "REL_TSCAN_RECOVERY": r"^RECOVERY_tscanResult\.zip$",
        "PREPARE_SLEEP_ZIP": rf"^sensorhub_ota_sleep_{p}_{v}\.zip$",
        "PREPARE_OTA_ARCHIVE": rf"^archive_OTA_{p}_{v}\.tgz$",
        "PREPARE_BOOT_ARCHIVE": rf"^archive_BOOT_{p}_{v}\.tgz$",
        "PREPARE_RECOVERY_ARCHIVE": rf"^archive_RECOVERY_{p}_{v}\.tgz$",
        "PREPARE_WATCH_ELF": r"^watch@[^/]+\.elf$",
    }
    expression = patterns.get(placeholder.strip("{}"))
    return re.compile(expression, re.IGNORECASE) if expression else None


def is_combined_build(cfg: Dict[str, Any]) -> bool:
    """同一 CI 构建链接也是合并产物的依据，兼容直接生成文档的入口。"""
    builds = (cfg.get("jenkins") or {}).get("builds") or {}
    release_url = str((builds.get("release") or {}).get("build_url") or "").strip().rstrip("/")
    debug_url = str((builds.get("debug") or {}).get("build_url") or "").strip().rstrip("/")
    return (str((cfg.get("vars") or {}).get("build_release_mode") or "").strip().lower() == "single"
            or bool(release_url and release_url == debug_url))


def _expected_version(cfg: Dict[str, Any], run: str) -> str:
    release_version = str((cfg.get("release") or {}).get("version") or "").strip()
    variables = cfg.get("vars") or {}
    if run != "debug":
        return release_version or str(variables.get("release_version_name") or "").strip()
    if is_combined_build(cfg):
        parts = release_version.split(".")
        if len(parts) == 4 and all(p.isdigit() for p in parts) and int(parts[-1]) % 2:
            return ".".join(parts[:-1] + [str(int(parts[-1]) - 1)])
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            return release_version
    return str(variables.get("debug_version_name") or "").strip()


def eligible_artifacts(candidates: Iterable[Tuple[str, Any]], *, cfg: Dict[str, Any],
                       placeholder: str, run: str) -> List[Tuple[str, Any]]:
    pattern = artifact_pattern(placeholder, cfg)
    pairs = [(str(k).replace("\\", "/"), value) for k, value in candidates if value]
    if pattern:
        pairs = [(k, v) for k, v in pairs if pattern.fullmatch(k.rsplit("/", 1)[-1])]
    if placeholder.startswith("{{REL_TSCAN_"):
        prefixed = [(k, v) for k, v in pairs if not k.rsplit("/", 1)[-1].upper().startswith(("BOOT_", "OTA_", "RECOVERY_"))]
        pairs = prefixed or pairs
    # BOOT and RECOVERY have their own versions, independent of app firmware.
    independent = placeholder in ("{{REL_BOOTLOADER_MANIFEST_INFO}}", "{{REL_RECOVERY_MANIFEST_INFO}}",
                                  "{{PREPARE_BOOT_ARCHIVE}}", "{{PREPARE_RECOVERY_ARCHIVE}}")
    expected = _expected_version(cfg, run)
    combined = is_combined_build(cfg)
    log_dictionary = placeholder in ("{{REL_RELEASE_LOG_DIST_BZ2_BIN}}", "{{REL_MONKEY_LOG_DIST_BZ2_BIN}}")
    if pattern and (expected or combined or log_dictionary) and not independent:
        versioned, unversioned = [], []
        for key, value in pairs:
            name = key.rsplit("/", 1)[-1]
            versions = re.findall(r"(?<![\d.])\d+\.\d+\.\d+(?:\.\d+)?(?!\d|\.\d)", name)
            if not versions:
                unversioned.append((key, value))
            else:
                matching = [v for v in versions if not expected or v == expected
                            or (len(expected.split(".")) == 3 and v.startswith(expected + "."))]
                if (combined or log_dictionary) and any(len(v.split(".")) == 4 for v in versions):
                    matching = [v for v in matching if len(v.split(".")) == 4
                                and int(v.split(".")[-1]) % 2 == (0 if run == "debug" else 1)]
                if matching:
                    versioned.append((key, value))
        pairs = versioned or unversioned
    if placeholder in ("{{REL_MD5SUM_FILE}}", "{{REL_RELEASE_MD5}}") and len(pairs) > 1:
        project = str((cfg.get("release") or {}).get("project") or "").lower()
        specific = [(k, v) for k, v in pairs if k.rsplit("/", 1)[-1].lower().startswith(project + "_")]
        pairs = specific or pairs
    return pairs


def select_artifact(candidates: Iterable[Tuple[str, Any]], *, cfg: Dict[str, Any],
                    placeholder: str, run: str) -> Tuple[str, Any]:
    # Repeated references to the same remote path (main + extra upload) are one
    # artifact. Different filenames or relative paths must not be guessed at.
    unique = {}
    for key, value in eligible_artifacts(candidates, cfg=cfg, placeholder=placeholder, run=run):
        unique.setdefault(key, value)
    if len(unique) > 1:
        raise ArtifactSelectionError(f"{placeholder}: 多个产物匹配，无法唯一确定: {', '.join(sorted(unique)[:8])}")
    if not unique:
        return "", ""
    key, value = next(iter(unique.items()))
    return key.rsplit("/", 1)[-1], value
