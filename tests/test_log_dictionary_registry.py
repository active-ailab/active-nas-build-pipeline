import unittest
from unittest import mock
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import zlib

import log_dictionary_registry as registry


class DictionaryRegistryTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {"release": {"project": "fuji", "version": "1.1.20.1"},
                    "vars": {"build_release_mode": "single"}}
        self.data = b"compressed dictionary bytes"
        self.read = mock.Mock(return_value=self.data)
        self.register = mock.patch.object(registry, "register_dictionary", return_value={"id": 19}).start()
        self.addCleanup(mock.patch.stopall)

    def sync(self, runs, **kwargs):
        return registry.sync_log_dictionaries(cfg=self.cfg, runs=runs, read_file=self.read, **kwargs)

    def run_summary(self, name="release", tail=1):
        file = "apollo5/log_dict_bz2_fuji_1.1.20.%s_OTA.bin" % tail
        return {"name": name, "build_url": "https://ci.invalid/job/build/42/",
                "remote_dir": "/版本/Fuji/" + name, "share_links_source": "nas",
                "share_links_main": {file: "https://nas.invalid/sharing/token"}}

    def test_metadata_uses_actual_nas_bytes_and_preserves_nested_path(self):
        self.sync([self.run_summary(), self.run_summary("debug", 0)])
        calls = self.register.call_args_list
        self.assertEqual(len(calls), 2)
        payload = calls[0][0][1]
        self.assertEqual(payload, {"project": "fuji", "build_type": "release", "format": "bzip2",
            "length": len(self.data), "crc32": "%08x" % zlib.crc32(self.data),
            "nas_path": "/版本/Fuji/release/apollo5/log_dict_bz2_fuji_1.1.20.1_OTA.bin",
            "firmware_version": "1.1.20.1", "release_version": "1.1.20.1", "image_type": "watch",
            "build_id": "42", "download_url": "https://nas.invalid/sharing/token"})
        self.assertEqual(calls[1][0][1]["build_type"], "debug")
        self.assertEqual(calls[1][0][1]["firmware_version"], "1.1.20.0")

    def test_no_dictionary_or_wrong_project_version_parity_never_registers(self):
        run = self.run_summary()
        run["share_links_main"] = {key: "url" for key in (
            "archive_fuji_1.1.20.1.tgz", "log_dict_bz2_meru_1.1.20.1_OTA.bin",
            "log_dict_bz2_fuji_1.1.19.1_OTA.bin", "log_dict_bz2_fuji_1.1.20.0_OTA.bin")}
        self.sync([run])
        self.read.assert_not_called()
        self.register.assert_not_called()

    def test_empty_remote_manifest_does_not_register_or_read_stale_local_files(self):
        run = self.run_summary()
        run["share_links_main"] = {}
        self.sync([run])
        self.read.assert_not_called()
        self.register.assert_not_called()

    def test_dry_run_and_disabled_never_contact_servers(self):
        self.sync([self.run_summary()], dry_run=True)
        self.cfg["log_dictionary_registry"] = {"enabled": False}
        self.sync([self.run_summary()])
        self.read.assert_not_called()
        self.register.assert_not_called()

    def test_failure_propagates_to_document_stage(self):
        self.register.side_effect = RuntimeError("registry unavailable")
        with self.assertRaisesRegex(RuntimeError, "registry unavailable"):
            self.sync([self.run_summary()])

    def test_normal_upload_without_share_links_finds_local_dictionary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "apollo5").mkdir()
            (root / "apollo5" / "log_dict_bz2_fuji_1.1.20.1_OTA.bin").write_bytes(b"stale local bytes")
            run = self.run_summary()
            run.pop("share_links_source")
            run["share_links_main"] = {}
            run["local_dir"] = directory
            self.sync([run])
        payload = self.register.call_args[0][1]
        self.assertEqual(payload["length"], len(self.data))
        self.assertNotIn("download_url", payload)

    def test_registration_failure_is_not_retried(self):
        mock.patch.stopall()
        payload = {"project": "fuji", "build_type": "release", "crc32": "12345678", "length": 12,
                   "format": "bzip2", "release_version": "1.1.20.1"}
        with mock.patch.object(registry, "registry_request", side_effect=[
                {"items": [], "has_more": False}, TimeoutError("response lost")]) as request:
            with self.assertRaises(TimeoutError):
                registry.register_dictionary("https://server.invalid", payload, timeout_sec=1)
        self.assertEqual(request.call_count, 2)

    def test_existing_record_is_reused_across_share_token_changes(self):
        mock.patch.stopall()
        payload = {"project": "fuji", "build_type": "release", "crc32": "12345678", "length": 12,
                   "format": "bzip2", "release_version": "1.1.20.1", "nas_path": "/版本/dict.bin",
                   "download_url": "https://nas.invalid/new"}
        old = dict(payload, id=9, download_url="https://nas.invalid/old")
        with mock.patch.object(registry, "registry_request", return_value={"items": [old], "has_more": False}) as request:
            self.assertEqual(registry.register_dictionary("https://server.invalid", payload, timeout_sec=1), old)
        self.assertEqual(request.call_count, 1)

    def test_query_paginates_then_posts_once(self):
        mock.patch.stopall()
        payload = {"project": "fuji", "build_type": "release", "crc32": "12345678", "length": 12,
                   "format": "bzip2", "release_version": "1.1.20.1"}
        with mock.patch.object(registry, "registry_request", side_effect=[
                {"items": [], "has_more": True}, {"items": [], "has_more": False}, {"id": 20}]) as request:
            self.assertEqual(registry.register_dictionary("https://server.invalid", payload, timeout_sec=1), {"id": 20})
        self.assertEqual(request.call_count, 3)
        self.assertEqual(request.call_args[1]["method"], "POST")

    def test_nas_read_is_bounded_and_encodes_chinese_paths(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = self.data
        with mock.patch.object(registry.urllib.request, "urlopen", return_value=response) as open_url:
            data = registry.read_nas_dictionary(base_url="https://nas.invalid", path="/版本/dict.bin",
                username="user", password="password", timeout_sec=2, verify_tls=True)
        self.assertEqual(data, self.data)
        self.assertIn("%E7%89%88%E6%9C%AC", open_url.call_args[0][0].full_url)
        response.__enter__.return_value.read.assert_called_once_with(1_000_001)


class DocumentRegistryIntegrationTests(unittest.TestCase):
    """Exercise the same skip-download/upload path as the direct-document UI."""

    def run_document(self, *, dictionary=True, registry_failure=False, skip_document=False):
        from config_loader import read_json_with_base
        import release_pipeline_run as pipeline
        from lark_cli_adapter import ReleaseDocResult

        root = Path(__file__).resolve().parents[1]
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            for target in ("urllib.request.urlopen", "urllib.request.OpenerDirector.open",
                           "requests.sessions.Session.request", "subprocess.Popen"):
                stack.enter_context(mock.patch(target, side_effect=AssertionError("External action: " + target)))
            directory = Path(stack.enter_context(tempfile.TemporaryDirectory(dir=str(root / "output"))))
            cfg = read_json_with_base(root / "gqf_fuji.json")
            cfg["release"].update(project="fuji", version="1.1.20.1", variant="fuji v1.1.20.1")
            cfg["vars"]["build_release_mode"] = "split"
            cfg["prepare"] = {"mode": "none", "workspace_dir": str(directory / "prepare")}
            cfg["changelog"] = {"enabled": False}
            cfg["notifications"] = {}
            cfg["doc"]["enabled"] = False
            cfg["feishu"].update(enabled=True, print_placeholder_mapping=False)
            cfg["cleanup"] = {"enabled": False}
            cfg["jenkins"]["builds"]["release"]["build_url"] = "https://ci.invalid/job/firmware/42/"
            cfg["nas"]["remote"] = {"base_dir": "/versions/fuji", "folder_name": "1.1.20.1"}
            config_path = directory / "config.json"
            config_path.write_text(json.dumps(cfg), encoding="utf-8")
            argv = ["pipeline", "--config", str(config_path), "--only", "release", "--skip-download",
                    "--skip-prepare", "--skip-upload", "--skip-doc", "--skip-cleanup"]
            if skip_document:
                argv.append("--skip-feishu")
            stack.enter_context(mock.patch.object(sys, "argv", argv))
            stack.enter_context(mock.patch.dict(os.environ, {key: "offline" for key in (
                "JENKINS_USERNAME", "JENKINS_PASSWORD", "NAS_WEBDAV_USERNAME", "NAS_WEBDAV_PASSWORD",
                "NAS_DSM_USERNAME", "NAS_DSM_PASSWORD", "DSM_PASSWORD")}))
            for target in ("_notify_base_stage", "_maybe_notify_webhook_text", "_feishu_notify_user", "lark_auth_ensure"):
                stack.enter_context(mock.patch.object(pipeline, target))
            for target in ("jenkins_download_artifacts", "_dsl_execute_prepare_flow", "_run_uploader"):
                stack.enter_context(mock.patch.object(pipeline, target, side_effect=AssertionError(target + " must be skipped")))
            filename = "log_dict_bz2_fuji_1.1.20.1_OTA.bin"
            files = {filename: "https://nas.invalid/sharing/token"} if dictionary else {"readme.txt": "https://nas.invalid/readme"}
            stack.enter_context(mock.patch.object(pipeline, "_generate_share_links_from_remote", return_value=files))
            stack.enter_context(mock.patch.object(pipeline, "_build_placeholder_replacements", return_value={}))
            stack.enter_context(mock.patch.object(pipeline, "_LARK_CLI_AVAILABLE", True))
            read = stack.enter_context(mock.patch.object(pipeline, "read_nas_dictionary", return_value=b"NAS dictionary bytes"))
            sequence = []

            def request(base_url, *, method="GET", payload=None, **kwargs):
                sequence.append(method)
                if registry_failure:
                    raise RuntimeError("registry unavailable")
                return {"id": 41, **payload} if method == "POST" else {"items": [], "has_more": False}

            api = stack.enter_context(mock.patch.object(registry, "registry_request", side_effect=request))

            def publish(**kwargs):
                sequence.append("publish")
                return ReleaseDocResult(wiki_url="https://docs.invalid/verified")

            document = stack.enter_context(mock.patch.object(pipeline, "lark_publish_release_document", side_effect=publish))
            code = pipeline.main()
            return code, sequence, api, read, document

    def test_direct_document_registers_existing_nas_dictionary_before_publishing(self):
        code, sequence, api, read, document = self.run_document()
        self.assertEqual(code, 0)
        self.assertEqual(sequence, ["GET", "POST", "publish"])
        self.assertEqual(api.call_args.kwargs["payload"]["nas_path"],
                         "/versions/fuji/1.1.20.1/Release/log_dict_bz2_fuji_1.1.20.1_OTA.bin")
        read.assert_called_once()
        document.assert_called_once()

    def test_no_dictionary_makes_no_registry_requests_and_still_publishes(self):
        code, sequence, api, read, document = self.run_document(dictionary=False)
        self.assertEqual(code, 0)
        self.assertEqual(sequence, ["publish"])
        api.assert_not_called()
        read.assert_not_called()
        document.assert_called_once()

    def test_registry_failure_stops_document_publication(self):
        code, sequence, api, read, document = self.run_document(registry_failure=True)
        self.assertNotEqual(code, 0)
        self.assertEqual(sequence, ["GET"])
        document.assert_not_called()

    def test_skipping_both_documents_never_registers(self):
        code, sequence, api, read, document = self.run_document(skip_document=True)
        self.assertEqual(code, 0)
        api.assert_not_called()
        read.assert_not_called()
        document.assert_not_called()


if __name__ == "__main__":
    unittest.main()
