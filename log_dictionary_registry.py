"""Register NAS log dictionary metadata before publishing release documents."""
import base64
import json
from pathlib import Path, PurePosixPath
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
import zlib

from artifact_matching import eligible_artifacts


DEFAULT_BASE_URL = "http://zeppos.zepp.com/dictlog"
MAX_LENGTH = 1_000_000


def read_nas_dictionary(*, base_url, path, username, password, timeout_sec, verify_tls):
    url = base_url.rstrip("/") + urllib.parse.quote(path, safe="/")
    request = urllib.request.Request(url)
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    request.add_header("Authorization", "Basic " + token)
    context = ssl.create_default_context() if verify_tls else ssl._create_unverified_context()
    with urllib.request.urlopen(request, timeout=timeout_sec, context=context) as response:
        data = response.read(MAX_LENGTH + 1)
    if not 1 <= len(data) <= MAX_LENGTH:
        raise RuntimeError(f"字典大小必须在 1～{MAX_LENGTH} 字节之间: {path}")
    return data


def registry_request(base_url, *, method="GET", query=None, payload=None, timeout_sec=30):
    url = base_url.rstrip("/") + "/api/v1/dictionaries"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, method=method,
        headers={"Content-Type": "application/json; charset=utf-8"})
    # POST is deliberately never retried: each accepted request creates a new row.
    try:
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:
            expected = 201 if method == "POST" else 200
            if response.status != expected:
                raise RuntimeError(f"字典登记接口返回 HTTP {response.status}，预期 {expected}")
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read(2000).decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"字典登记接口 {method} 返回 HTTP {exc.code}: {detail or exc.reason}") from exc


def register_dictionary(base_url, payload, *, timeout_sec):
    query = {key: payload[key] for key in ("project", "build_type", "crc32", "length", "format", "release_version")}
    query.update(limit=100, offset=0)
    while True:
        page = registry_request(base_url, query=query, timeout_sec=timeout_sec)
        # Share tokens can change when a document is regenerated; the NAS source
        # and build identity determine whether this exact artifact was registered.
        identity = {k: v for k, v in payload.items() if k != "download_url"}
        for record in page["items"]:
            if all(record.get(k) == v for k, v in identity.items()):
                return record
        if not page["has_more"]:
            break
        query["offset"] += query["limit"]
    record = registry_request(base_url, method="POST", payload=payload, timeout_sec=timeout_sec)
    if not isinstance(record, dict) or not record.get("id"):
        raise RuntimeError("字典登记接口未返回记录 ID；请查询服务器确认后重试")
    return record


def sync_log_dictionaries(*, cfg, runs, read_file, dry_run=False):
    settings = cfg.get("log_dictionary_registry") or {}
    if not settings.get("enabled", True):
        return []
    base_url = str(settings.get("base_url") or DEFAULT_BASE_URL)
    timeout_sec = int(settings.get("timeout_sec", 30))
    project = str((cfg.get("release") or {}).get("project") or
                  (cfg.get("release") or {}).get("device_name") or "").strip()
    results, seen = [], set()
    for run in runs:
        name = run.get("name")
        if name not in ("release", "debug"):
            continue
        links = dict(run.get("share_links_main") or {})
        for extra in (run.get("share_links_extra") or {}).values():
            if isinstance(extra, dict):
                links.update({k: v for k, v in extra.items() if isinstance(v, str)})
        candidates = dict(links)
        if run.get("share_links_source") == "nas" and not candidates:
            print(f"[DictLog:SKIP] {name} 无可用 NAS 文件清单，未发现日志字典；续生成时请保留分享链接步骤。")
        if run.get("share_links_source") != "nas":
            for directory in [run.get("local_dir"), *(run.get("extra_dirs") or [])]:
                if not directory:
                    continue
                root = Path(directory)
                if root.is_dir():
                    candidates.update({p.relative_to(root).as_posix(): True for p in root.rglob("*")
                                       if p.is_file() and p.suffix.lower() == ".bin"})
        placeholder = "{{REL_MONKEY_LOG_DIST_BZ2_BIN}}" if name == "debug" else "{{REL_RELEASE_LOG_DIST_BZ2_BIN}}"
        for relative, _ in eligible_artifacts(candidates.items(), cfg=cfg, placeholder=placeholder, run=name):
            relative = str(relative).replace("\\", "/")
            if PurePosixPath(relative).is_absolute() or any(p in ("", ".", "..") for p in relative.split("/")):
                raise RuntimeError(f"无效字典相对路径: {relative}")
            path = run["remote_dir"].rstrip("/") + "/" + relative
            if path in seen:
                continue
            seen.add(path)
            if dry_run:
                print(f"[DictLog:DRY-RUN] 将登记日志字典: {path}")
                continue
            match = re.fullmatch(r"log_dict_bz2_" + re.escape(project) +
                r"_(\d+\.\d+\.\d+\.\d+)(?:_(.+))?\.bin", PurePosixPath(relative).name, re.I)
            version, image = match.groups()
            data = read_file(path)
            payload = {"project": project.lower(), "build_type": name, "format": "bzip2",
                "length": len(data), "crc32": f"{zlib.crc32(data) & 0xffffffff:08x}",
                "nas_path": path, "firmware_version": version, "release_version": version}
            if image:
                payload["image_type"] = {"OTA": "watch", "BOOT": "bootloader", "RECOVERY": "recovery"}.get(image.upper(), image.lower())
            build_id = str(run.get("build_url") or "").rstrip("/").rsplit("/", 1)[-1]
            if build_id:
                payload["build_id"] = build_id
            if links.get(relative):
                payload["download_url"] = links[relative]
            record = register_dictionary(base_url, payload, timeout_sec=timeout_sec)
            results.append(record)
            print(f"[DictLog:OK] {path}，登记 ID={record['id']}")
    return results
