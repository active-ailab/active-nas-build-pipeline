"""A listed Release manifest must survive download and parity splitting."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

import release_pipeline_run as pipeline


ROOT = Path(__file__).resolve().parents[1]
RELEASE = "manifest_geneva_6.5.13.5_202611111438.xml"
DEBUG = "manifest_geneva_6.5.13.4_202611111438.xml"


class ManifestDownloadTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.output = io.StringIO()
        self.stack.enter_context(contextlib.redirect_stdout(self.output))
        self.stack.enter_context(contextlib.redirect_stderr(self.output))
        for target in ("urllib.request.urlopen", "urllib.request.OpenerDirector.open",
                       "requests.sessions.Session.request", "subprocess.Popen"):
            self.stack.enter_context(mock.patch(target, side_effect=AssertionError("External action: " + target)))
        self.temp = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=str(ROOT / "output"))))
        self.release_dir = self.temp / "release"
        metadata = {"result": "SUCCESS", "artifacts": [{"relativePath": filename} for filename in (DEBUG, RELEASE)]}
        self.stack.enter_context(mock.patch.object(pipeline, "_curl_bytes", return_value=json.dumps(metadata).encode()))

    def download(self):
        return pipeline.jenkins_download_artifacts(
            build_url="https://offline.invalid/job/HuamiOS_multi_platform/1846/", out_dir=self.release_dir,
            auth=pipeline.JenkinsAuth("offline", "offline"), include_globs=["**/*"], exclude_globs=["**/*.log"],
            overwrite=True, verify_tls=True, timeout_sec=5, dry_run=False, show_progress=False)

    @staticmethod
    def write_normal(**kw):
        kw["dest"].parent.mkdir(parents=True, exist_ok=True)
        kw["dest"].write_bytes(b"<manifest/>")

    def test_release_manifest_downloads_splits_and_fills_its_own_placeholder(self):
        self.stack.enter_context(mock.patch.object(pipeline, "_curl_download_file", side_effect=self.write_normal))
        self.assertEqual(self.download(), 2)
        debug_dir = self.temp / "debug"
        pipeline._split_artifacts_by_parity(src_dir=self.release_dir, dst_dir=debug_dir, dry_run=False)
        self.assertTrue((self.release_dir / RELEASE).is_file())
        self.assertFalse((self.release_dir / DEBUG).exists())
        self.assertTrue((debug_dir / DEBUG).is_file())
        cfg = {"release": {"project": "geneva", "version": "6.5.13.5"},
               "vars": {"build_release_mode": "single"}, "changelog": {"enabled": False}}
        runs = [{"name": "release", "share_links_source": "nas", "share_links_main": {RELEASE: "https://nas.invalid/release"}},
                {"name": "debug", "share_links_source": "nas", "share_links_main": {DEBUG: "https://nas.invalid/debug"}}]
        dsl = json.loads((ROOT / "placeholders.default.json").read_text(encoding="utf-8"))
        mapping = pipeline._build_placeholder_replacements_from_dsl(cfg=cfg, run_summaries=runs,
            dsl_cfg=pipeline._expand_placeholders(dsl, cfg), dsl_runtime={})
        self.assertEqual(mapping["{{REL_APP_MANIFEST_INFO}}"], RELEASE + "|https://nas.invalid/release")

    def test_failed_release_manifest_is_warned_and_skipped_without_stopping_download(self):
        def fetch(**kw):
            if RELEASE in kw["url"]:
                kw["dest"].write_bytes(b"partial data")
                raise RuntimeError("network timeout")
            self.write_normal(**kw)
        self.stack.enter_context(mock.patch.object(pipeline, "_curl_download_file", side_effect=fetch))
        self.assertEqual(self.download(), 1)
        self.assertFalse((self.release_dir / RELEASE).exists())
        self.assertTrue((self.release_dir / DEBUG).is_file())
        self.assertIn("optional manifest", self.output.getvalue())
        self.assertIn("1/2 files", self.output.getvalue())

    def test_transient_direct_download_error_is_retried_before_zip_fallback(self):
        attempts = []
        def fetch(**kw):
            attempts.append(kw["url"])
            if RELEASE in kw["url"] and sum(RELEASE in url for url in attempts) == 1:
                raise RuntimeError("temporary timeout")
            self.write_normal(**kw)
        self.stack.enter_context(mock.patch.object(pipeline, "_curl_download_file", side_effect=fetch))
        self.assertEqual(self.download(), 2)
        self.assertFalse(any("/*zip*/" in url for url in attempts))

    def test_firmware_artifact_download_failure_is_still_blocking(self):
        self.stack.enter_context(mock.patch.object(pipeline, "_curl_bytes", return_value=json.dumps({
            "artifacts": [{"relativePath": "archive_geneva_6.5.13.5_202611111438.tgz"}]}).encode()))
        self.stack.enter_context(mock.patch.object(pipeline, "_curl_download_file", side_effect=RuntimeError("timeout")))
        with self.assertRaisesRegex(RuntimeError, "required files failed"):
            self.download()

    def test_corrupt_firmware_archive_after_retry_still_blocks_release(self):
        filename = "archive_geneva_6.5.13.5_202611111438.tgz"
        self.stack.enter_context(mock.patch.object(pipeline, "_curl_bytes", return_value=json.dumps({
            "artifacts": [{"relativePath": filename}]}).encode()))
        fetch = self.stack.enter_context(mock.patch.object(pipeline, "_curl_download_file", side_effect=self.write_normal))
        with self.assertRaisesRegex(RuntimeError, "archive integrity failed after retry"):
            self.download()
        self.assertEqual(fetch.call_count, 2)

    def test_nested_manifest_failure_removes_partial_file(self):
        filename = "apollo5/" + RELEASE
        self.stack.enter_context(mock.patch.object(pipeline, "_curl_bytes", return_value=json.dumps({
            "artifacts": [{"relativePath": filename}]}).encode()))
        def fetch(**kw):
            kw["dest"].parent.mkdir(parents=True, exist_ok=True)
            kw["dest"].write_bytes(b"partial bytes")
            raise RuntimeError("timeout")
        self.stack.enter_context(mock.patch.object(pipeline, "_curl_download_file", side_effect=fetch))
        self.assertEqual(self.download(), 0)
        self.assertFalse((self.release_dir / filename).exists())
        self.assertEqual(list(self.release_dir.rglob("*.zip_tmp")), [])

    def test_zip_fallback_writes_the_manifest_as_a_file_not_a_directory(self):
        def fetch(**kw):
            if RELEASE not in kw["url"]:
                self.write_normal(**kw)
            elif "/*zip*/" not in kw["url"]:
                raise RuntimeError("direct download unavailable")
            else:
                with zipfile.ZipFile(str(kw["dest"]), "w") as archive:
                    archive.writestr("artifacts/" + RELEASE, b"<release-manifest/>")
        self.stack.enter_context(mock.patch.object(pipeline, "_curl_download_file", side_effect=fetch))
        self.assertEqual(self.download(), 2)
        self.assertTrue((self.release_dir / RELEASE).is_file())
        self.assertEqual((self.release_dir / RELEASE).read_bytes(), b"<release-manifest/>")
        self.assertEqual(list(self.release_dir.glob("*.zip_tmp")), [])

    def test_fallback_archive_with_multiple_same_named_files_is_rejected(self):
        def fetch(**kw):
            if RELEASE not in kw["url"]:
                self.write_normal(**kw)
            elif "/*zip*/" not in kw["url"]:
                raise RuntimeError("direct download unavailable")
            else:
                with zipfile.ZipFile(str(kw["dest"]), "w") as archive:
                    archive.writestr("one/" + RELEASE, b"one")
                    archive.writestr("two/" + RELEASE, b"two")
        self.stack.enter_context(mock.patch.object(pipeline, "_curl_download_file", side_effect=fetch))
        self.assertEqual(self.download(), 1)
        self.assertIn("exactly one", self.output.getvalue())
        self.assertFalse((self.release_dir / RELEASE).exists())
