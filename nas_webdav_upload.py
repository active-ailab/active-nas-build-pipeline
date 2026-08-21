#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import mimetypes
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable, Optional, Tuple
from urllib.parse import quote


class CurlHttpError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        method: str,
        url: str,
        http_code: Optional[int] = None,
        returncode: Optional[int] = None,
        stderr: str = "",
    ):
        super().__init__(message)
        self.method = method
        self.url = url
        self.http_code = http_code
        self.returncode = returncode
        self.stderr = stderr


def _curl_bin() -> str:
    return "curl.exe" if os.name == "nt" else "curl"


@dataclass(frozen=True)
class UploadConfig:
    nas_base_url: str
    username: str
    password: str
    remote_base_dir: str
    folder_name: str
    local_dir: str
    verify_tls: bool = False
    timeout_sec: int = 60
    transport_method: str = "webdav"  # "webdav" or "smb"
    smb_share: str = ""  # UNC path like \\\\10.2.100.85\\share_name (JSON double-escaped)


def _norm_base_url(url: str) -> str:
    return url.rstrip("/")


def _norm_remote_path(path: str) -> str:
    # Expect absolute WebDAV path; allow user to omit leading slash.
    p = path.strip()
    if not p.startswith("/"):
        p = "/" + p
    # Keep trailing slash for directory paths.
    return p


def _join_remote(base: str, sub: str) -> str:
    base = base.rstrip("/")
    sub = sub.strip("/")
    if not sub:
        return base + "/"
    return base + "/" + sub + "/"


def _encode_path(path: str) -> str:
    # Encode as UTF-8 percent-encoding, keep slashes.
    return quote(path, safe="/")


def _remote_url(nas_base_url: str, remote_path: str) -> str:
    # remote_path must start with '/'
    return _norm_base_url(nas_base_url) + _encode_path(remote_path)


def _iter_files(local_root: Path) -> Iterable[Tuple[Path, str]]:
    # Yields (absolute_path, relative_posix_path)
    for p in sorted(local_root.rglob("*")):
        if p.is_file():
            rel = p.relative_to(local_root).as_posix()
            yield p, rel


class WebDavClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        verify_tls: bool,
        timeout_sec: int,
        show_progress: bool = False,
    ):
        self._base_url = _norm_base_url(base_url)
        self._verify_tls = verify_tls
        self._timeout_sec = timeout_sec
        self._show_progress = show_progress
        self._username = username
        self._password = password
        self._netrc_path: Optional[str] = None

    def __enter__(self) -> "WebDavClient":
        # Use a temporary netrc file so credentials never appear in process args.
        # curl will read this for Basic auth.
        tmp = tempfile.NamedTemporaryFile("w", prefix="webdav_netrc_", delete=False)
        try:
            # netrc expects machine without scheme/port; Synology uses host-based auth.
            host = self._base_url.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0]
            tmp.write(f"machine {host}\n")
            tmp.write(f"  login {self._username}\n")
            tmp.write(f"  password {self._password}\n")
            tmp.flush()
            os.chmod(tmp.name, 0o600)
            self._netrc_path = tmp.name
        finally:
            tmp.close()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._netrc_path:
            try:
                os.unlink(self._netrc_path)
            except OSError:
                pass
            self._netrc_path = None

    def _curl(
        self,
        *,
        method: str,
        url: str,
        data: Optional[bytes] = None,
        upload_file: Optional[Path] = None,
        headers: Optional[dict] = None,
        ok_codes: Tuple[int, ...] = (200,),
    ) -> None:
        if not self._netrc_path:
            raise RuntimeError("WebDavClient not initialized (missing netrc). Use as a context manager.")

        cmd = [
            _curl_bin(),
            "--show-error",
            "--location",  # follow redirects within same host
            "--compressed",
            "--netrc-file",
            self._netrc_path,
            "--request",
            method,
            "--connect-timeout",
            "10",
            "--max-time",
            str(int(self._timeout_sec)),
        ]
        # Only show progress for actual file transfers.
        if self._show_progress and upload_file is not None:
            cmd.append("--progress-bar")
        else:
            cmd.append("--silent")
        if not self._verify_tls:
            cmd.append("--insecure")

        # Add headers
        if headers:
            for k, v in headers.items():
                cmd.extend(["--header", f"{k}: {v}"])

        # Request body
        if data is not None:
            cmd.extend(["--data-binary", "@-"])

        # Upload file
        if upload_file is not None:
            cmd.extend(["--upload-file", str(upload_file)])

        # Capture http code
        cmd.extend(["--output", os.devnull, "--write-out", "%{http_code}", url])

        proc = subprocess.run(
            cmd,
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if proc.returncode != 0:
            stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
            raise CurlHttpError(
                f"curl {method} failed: rc={proc.returncode} url={url}",
                method=method,
                url=url,
                http_code=None,
                returncode=proc.returncode,
                stderr=stderr.strip()[:800],
            )
        http_code_text = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
        try:
            http_code = int(http_code_text)
        except ValueError:
            http_code = 0

        if http_code in ok_codes:
            return

        stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
        raise CurlHttpError(
            f"curl {method} failed: status={http_code} url={url}",
            method=method,
            url=url,
            http_code=http_code,
            returncode=proc.returncode,
            stderr=stderr.strip()[:800],
        )

    def mkcol(self, remote_dir_path: str) -> None:
        # remote_dir_path must end with '/'
        if not remote_dir_path.endswith("/"):
            remote_dir_path += "/"
        url = _remote_url(self._base_url, remote_dir_path)
        # 201 Created (ok), 405 Method Not Allowed (already exists), 200/204 (some servers)
        self._curl(method="MKCOL", url=url, ok_codes=(200, 201, 204, 405))

    def ensure_dir_tree(self, remote_dir_path: str) -> None:
        # Create each level under remote_dir_path
        p = _norm_remote_path(remote_dir_path)
        if not p.endswith("/"):
            p += "/"

        # Split into segments while preserving leading '/'
        # Example: /a/b/c/ -> ['', 'a', 'b', 'c', '']
        segments = [seg for seg in p.split("/") if seg]
        current = "/"
        for seg in segments:
            current = _join_remote(current, seg)
            self.mkcol(current)

    def put_file(self, remote_file_path: str, local_file: Path) -> None:
        url = _remote_url(self._base_url, _norm_remote_path(remote_file_path))
        content_type, _ = mimetypes.guess_type(str(local_file))
        headers = {}
        if content_type:
            headers["Content-Type"] = content_type

        # curl uses PUT when --upload-file is specified.
        self._curl(method="PUT", url=url, upload_file=local_file, headers=headers, ok_codes=(200, 201, 204))


# ── SMB 直传（替代 WebDAV，Win/Linux 均支持） ──

def _smb_remote_path(smb_share: str, remote_base: str, folder_name: str, rel_posix: str = "") -> str:
    """将 WebDAV 路径转换为 SMB 路径：\\\\host\\share\\base\\folder\\file
    自动去重：如 share=\\\\10.2.100.85\\GT 且 base=/GT/软件部/... → \\\\10.2.100.85\\GT\\软件部\\...
    """
    base = remote_base.strip("/")
    folder = folder_name.strip("/")
    share = smb_share.rstrip("\\")
    # 如果 base 的第一段与 share 的最后一截重复，去掉 base 开头的重复段
    base_parts = base.split("/")
    share_last = share.rsplit("\\", 1)[-1]
    if base_parts and base_parts[0] == share_last:
        base_parts = base_parts[1:]
    parts = [share] + base_parts + folder.split("/")
    if rel_posix:
        parts.append(rel_posix.replace("/", "\\"))
    return "\\".join(p for p in parts if p)


def _smb_upload(
    *,
    smb_share: str,
    remote_base: str,
    folder_name: str,
    files: list,
    dry_run: bool,
) -> int:
    """通过 SMB（Windows 文件共享）直接复制文件到 NAS"""
    import shutil

    # 确保远程目录存在
    remote_dir = _smb_remote_path(smb_share, remote_base, folder_name)
    if not dry_run:
        os.makedirs(remote_dir, exist_ok=True)

    # 预创建子目录
    parent_dirs = set()
    for _, _, rel_posix in [(i, a, r) for i, (a, r) in enumerate(files, start=1)]:
        parent = os.path.dirname(_smb_remote_path(smb_share, remote_base, folder_name, rel_posix))
        if parent not in parent_dirs:
            parent_dirs.add(parent)
            if not dry_run:
                os.makedirs(parent, exist_ok=True)

    def _smb_copy_one(idx_abs_rel):
        idx, abs_path, rel_posix = idx_abs_rel
        remote_file = _smb_remote_path(smb_share, remote_base, folder_name, rel_posix)
        result = {"idx": idx, "rel": rel_posix, "ok": True}
        print(f"[{idx}/{len(files)}] {rel_posix} -> {remote_file}")
        if dry_run:
            return result
        try:
            # 使用 robocopy 而非 shutil.copy2——robocopy 对 SMB UNC 路径有正确的网络处理
            import subprocess as _sp
            parent_dir = os.path.dirname(remote_file)
            rc = _sp.run(
                ["robocopy", str(abs_path.parent), parent_dir, abs_path.name,
                 "/R:1", "/W:1", "/NP", "/NFL", "/NDL"],
                capture_output=True, text=True, timeout=120,
            )
            # robocopy returns 0-7 on success, >=8 on failure
            if rc.returncode >= 8:
                result["ok"] = False
                result["error"] = f"robocopy rc={rc.returncode}: {rc.stderr.strip()[:100]}"
                return result
            # robocopy 成功后，强制刷新确保 NAS 同步
            try:
                dst_stat = os.stat(remote_file)
                src_size = abs_path.stat().st_size
                if dst_stat.st_size != src_size:
                    result["ok"] = False
                    result["error"] = f"size mismatch after robocopy: local={src_size} remote={dst_stat.st_size}"
            except FileNotFoundError:
                result["ok"] = False
                result["error"] = f"file not found on NAS after robocopy"
        except Exception as e:
            result["ok"] = False
            result["error"] = str(e)
        return result

    workers = min(len(files), 8)
    failed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        uploads = [(i, a, r) for i, (a, r) in enumerate(files, start=1)]
        futures = {pool.submit(_smb_copy_one, t): t for t in uploads}
        for f in as_completed(futures):
            res = f.result()
            if not res["ok"]:
                failed += 1
                print(f"  [{res['idx']}/{len(files)}] {res['rel']} ERROR: {res.get('error','')[:100]}")

    return failed


def load_config(path: Path) -> UploadConfig:
    text: Optional[str] = None
    for enc in ("utf-8", "utf-8-sig", "mbcs", "gbk"):
        try:
            text = path.read_text(encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        # Last resort: replace undecodable bytes
        text = path.read_text(encoding="utf-8", errors="replace")

    data = json.loads(text)
    # SMB: JSON uses double-escaped backslashes \\\\ → Python gets \\
    smb_share_raw = str(data.get("smb_share", ""))
    smb_share = smb_share_raw.replace("\\\\", "\\") if smb_share_raw else ""
    return UploadConfig(
        nas_base_url=str(data["nas_base_url"]),
        username=str(data["username"]),
        password=str(data.get("password", "")),
        remote_base_dir=str(data["remote_base_dir"]),
        folder_name=str(data["folder_name"]),
        local_dir=str(data["local_dir"]),
        verify_tls=bool(data.get("verify_tls", False)),
        timeout_sec=int(data.get("timeout_sec", 60)),
        transport_method=str(data.get("transport_method", "webdav")).lower(),
        smb_share=smb_share,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload a local folder to Synology DSM WebDAV.")
    parser.add_argument("--config", required=True, help="Path to JSON config")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without uploading")
    parser.add_argument("--progress", action="store_true", help="Show curl progress (speed/ETA) for uploads")
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip file upload if server reports conflict/already exists",
    )
    args = parser.parse_args()

    cfg = load_config(Path(args.config))

    password = cfg.password.strip()
    if not password:
        password = (os.environ.get("NAS_PASSWORD") or os.environ.get("WEBDAV_PASS") or "").strip()
    if not password:
        raise SystemExit('Missing password. Provide it via config JSON "password" or env NAS_PASSWORD/WEBDAV_PASS.')

    local_root = Path(cfg.local_dir).expanduser().resolve()
    if not local_root.is_dir():
        raise SystemExit(f"local_dir not found or not a directory: {local_root}")

    remote_base = _norm_remote_path(cfg.remote_base_dir)
    remote_target_dir = _join_remote(remote_base, cfg.folder_name)

    print(f"NAS: {cfg.nas_base_url}")
    print(f"Remote base: {remote_base}")
    print(f"Create/upload to: {remote_target_dir}")
    print(f"Local dir: {local_root}")

    files = list(_iter_files(local_root))
    if not files:
        print("No files to upload.")
        return 0

    # ── SMB 直传模式 ──
    if cfg.transport_method == "smb":
        smb_share = cfg.smb_share
        if not smb_share:
            raise SystemExit("transport_method=smb requires smb_share (e.g. \\\\10.2.100.85\\share)")
        print(f"NAS: using SMB direct transfer ({smb_share})")
        failed = _smb_upload(
            smb_share=smb_share,
            remote_base=cfg.remote_base_dir,
            folder_name=cfg.folder_name,
            files=files,
            dry_run=args.dry_run,
        )
        if failed == 0:
            print("Upload complete.")
            return 0
        print(f"SMB: {failed}/{len(files)} files failed, falling back to WebDAV...")
        # SMB 失败 → 回退到 WebDAV，不退出

    # ── WebDAV 模式（默认） ──
    client = WebDavClient(
        cfg.nas_base_url,
        cfg.username,
        password,
        verify_tls=cfg.verify_tls,
        timeout_sec=cfg.timeout_sec,
        show_progress=bool(args.progress),
    )

    # Ensure the temporary netrc is created and cleaned up.
    with client:
        if args.dry_run:
            print("[dry-run] would MKCOL ensure:", remote_target_dir)
        else:
            client.ensure_dir_tree(remote_target_dir)

        ensured_dirs = {remote_target_dir}

        seen_remote_files: set[str] = set()

        # ── 并行上传 ──
        def _upload_one(idx_abs_rel):
            idx, abs_path, rel_posix = idx_abs_rel
            remote_file = remote_target_dir.rstrip("/") + "/" + rel_posix
            remote_parent = os.path.dirname(remote_file)
            if not remote_parent.endswith("/"):
                remote_parent += "/"
            result = {"idx": idx, "rel": rel_posix, "ok": True}
            print(f"[{idx}/{len(files)}] {rel_posix} -> {remote_file}")
            if args.dry_run:
                return result
            try:
                if remote_parent not in ensured_dirs:
                    client.ensure_dir_tree(remote_parent)
                client.put_file(remote_file, abs_path)
            except CurlHttpError as e:
                if args.skip_existing and e.http_code in (403, 405, 409, 412):
                    print(f"[skip-existing] {rel_posix} -> {remote_file} (status={e.http_code})")
                else:
                    result["ok"] = False
                    result["error"] = str(e)
            except Exception as e:
                result["ok"] = False
                result["error"] = str(e)
            return result

        # 预创建所有目录（避免并行时冲突）
        seen = set()
        for _, abs_path, rel_posix in [(i, a, r) for i, (a, r) in enumerate(files, start=1)]:
            remote_parent = os.path.dirname(remote_target_dir.rstrip("/") + "/" + rel_posix)
            if not remote_parent.endswith("/"): remote_parent += "/"
            if remote_parent not in seen:
                client.ensure_dir_tree(remote_parent)
                seen.add(remote_parent)

        # 并行上传
        workers = min(len(files), 8)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            uploads = [(i, a, r) for i, (a, r) in enumerate(files, start=1)]
            futures = {pool.submit(_upload_one, t): t for t in uploads}
            for f in as_completed(futures):
                res = f.result()
                if not res["ok"]:
                    print(f"  [{res['idx']}/{len(files)}] {res['rel']} ERROR: {res.get('error','')[:100]}")

    print("Upload complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
