"""Replay document-only releases without Jenkins, NAS or Feishu writes."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from config_loader import read_json_with_base
import lark_cli_adapter as lark
import release_pipeline_run as pipeline
import web_release_server as web


ROOT = Path(__file__).resolve().parents[1]
RELEASE_URL = "https://offline.invalid/job/firmware/1665/"
EXTRA_FILES = {
    "{{REL_RELEASE_OTA_SIGN_ZIP}}": "watch@mhs003_ota_sign.zip",
    "{{REL_RELEASE_OTA_CLOUD_ARCHIVE}}": "archive_OTA_CLOUD_cologne_6.5.15.1_202610211920.tgz",
    "{{REL_RECOVERY_MANIFEST_INFO}}": "manifest_RECOVERY_cologne_1.4.1_202610211920.xml",
    "{{REL_BOOTLOADER_MANIFEST_INFO}}": "manifest_BOOT_cologne_1.4.2_202610211920.xml",
    "{{REL_BUILD_LOG_FILE}}": "jenkins_release_console.log",
}
MAIN_FILES = [
    "archive_cologne_6.5.15.1_202610211920.tgz",
    "archive_OTA_SLEEP_cologne_6.5.15.1_202610211920.tgz",
    "cologne_6.5.15.1_md5.txt",
    "cologne_factory_6.5.15.1_6.5.15_2026-10-21.zip",
    "config_cologne_6.5.15.1",
    "manifest_cologne_6.5.15.1_202610211920.xml",
    "sensorhub_fct_cologne_6.5.15.1.zip",
    "sensorhub_ota_cologne_6.5.15.1.zip",
    "sensorhub_ota_sleep_cologne_6.5.15.1.zip",
]


class OfflineTest(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.output = io.StringIO()
        self.stack.enter_context(contextlib.redirect_stdout(self.output))
        self.stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        for name in ("urllib.request.urlopen", "urllib.request.OpenerDirector.open",
                     "requests.sessions.Session.request", "subprocess.Popen"):
            self.stack.enter_context(mock.patch(name, side_effect=AssertionError("External action: " + name)))
        self.temp = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=str(ROOT / "output"))))

    def patch(self, *args, **kwargs):
        return self.stack.enter_context(mock.patch.object(*args, **kwargs))


class DocumentResumeTests(OfflineTest):
    def run_resume(self, local_main=True, local_payload=False, relative_workspace=False):
        cfg = read_json_with_base(ROOT / "gqf_cologne.json")
        cfg["release"].update(project="cologne", device_name="cologne", version="6.5.15.1", stage="offline")
        cfg["vars"]["build_release_mode"] = "single"
        cfg["prepare"] = {"mode": "placeholders_dsl", "workspace_dir": "prepare" if relative_workspace else str(self.temp / "prepare"),
                          "placeholders_config": str(ROOT / "placeholders.default.json")}
        cfg["jenkins"]["auth"] = {"username": "offline", "password": "offline"}
        cfg["nas"]["webdav"]["auth"] = {"username": "offline", "password": "offline"}
        cfg["nas"]["dsm"] = {"base_url": "https://nas.invalid", "auth": {"username": "offline", "password": "offline"}}
        cfg["nas"]["remote"] = {"base_dir": "/offline/cologne", "folder_name": "6.5.15.1"}
        cfg["nas"]["uploads"] = []
        for name, subdir in (("release", "Release"), ("debug", "Monkey")):
            folder = self.temp / "download" / name
            cfg["jenkins"]["builds"][name].update(build_url=RELEASE_URL,
                download={"output_dir": str(folder), "include_globs": ["**/*"], "overwrite": True})
            # No extra_local_dirs: the DSL's payload directory must be restored.
            cfg["nas"]["uploads"].append({"name": name, "remote_subdir": subdir, "local_dir": str(folder)})
            if local_main:
                folder.mkdir(parents=True)
                names = MAIN_FILES if name == "release" else ["archive_cologne_6.5.15.0_202610211920.tgz"]
                for f in names: (folder / f).write_text("offline")
        tscan_dir = self.temp / "download" / "tscan"
        cfg["jenkins"]["builds"]["tscan"] = {"build_url": "", "download": {"output_dir": str(tscan_dir)}}
        cfg["nas"]["uploads"].append({"name": "tscan", "remote_subdir": "Monkey", "local_dir": str(tscan_dir)})
        payload = self.temp / "prepare" / "cologne" / "upload_payload"
        if local_payload:
            payload.mkdir(parents=True)
            for f in EXTRA_FILES.values(): (payload / f).write_text("offline")
        cfg["doc"]["enabled"] = False
        cfg["feishu"].update(enabled=True, print_placeholder_mapping=False)
        cfg["changelog"] = {"enabled": False}
        cfg["cleanup"] = {"enabled": False}
        cp = self.temp / "resume.json"
        cp.write_text(json.dumps(cfg), encoding="utf-8")

        self.patch(sys, "argv", ["pipeline", "--config", str(cp), "--skip-download", "--skip-prepare",
                                  "--skip-upload", "--skip-doc", "--skip-cleanup"])
        # Prevent machine credentials from affecting this offline fixture.
        self.stack.enter_context(mock.patch.dict(os.environ, {
            k: "offline" for k in ("JENKINS_USERNAME", "JENKINS_PASSWORD", "NAS_WEBDAV_USERNAME",
                "NAS_WEBDAV_PASSWORD", "NAS_DSM_USERNAME", "NAS_DSM_PASSWORD", "DSM_PASSWORD")}))
        for name in ("_notify_base_stage", "_maybe_notify_webhook_text", "_feishu_notify_user"):
            self.patch(pipeline, name)
        download = self.patch(pipeline, "jenkins_download_artifacts")
        prepare = self.patch(pipeline, "_dsl_execute_prepare_flow")
        upload = self.patch(pipeline, "_run_uploader")
        self.patch(pipeline, "lark_auth_ensure", return_value=True)
        self.patch(pipeline, "_dsm_client_cache", {})
        dsm = mock.Mock()
        dsm.list_files.side_effect = lambda **kw: (MAIN_FILES + list(EXTRA_FILES.values())
            if kw["folder_path"].endswith("/Release") else ["archive_cologne_6.5.15.0_202610211920.tgz"])
        dsm.create_share_link.side_effect = lambda **kw: "https://nas.invalid/sharing/" + kw["path"].rsplit("/", 1)[-1]
        self.patch(pipeline, "SynologyDsmClient", return_value=dsm)
        publish = self.patch(pipeline, "lark_publish_release_document", return_value=lark.ReleaseDocResult(wiki_url="offline"))

        self.assertEqual(pipeline.main(), 0)
        download.assert_not_called()
        prepare.assert_not_called()
        upload.assert_not_called()
        mapping = publish.call_args.kwargs["placeholder_map"]
        for ph, filename in EXTRA_FILES.items():
            self.assertEqual(mapping[ph], filename + "|https://nas.invalid/sharing/" + filename)
        self.assertEqual(mapping["{{REL_CI_BUILD_URL}}"], RELEASE_URL)
        for filename_ph, share_ph in (
            ("{{REL_RELEASE_OTA_SIGN_ZIP_FILENAME}}", "{{REL_RELEASE_OTA_SIGN_ZIP}}"),
            ("{{REL_RELEASE_OTA_CLOUD_ARCHIVE_FILENAME}}", "{{REL_RELEASE_OTA_CLOUD_ARCHIVE}}"),
            ("{{REL_RECOVERY_MANIFEST_FILENAME}}", "{{REL_RECOVERY_MANIFEST_INFO}}"),
            ("{{REL_BOOTLOADER_MANIFEST_FILENAME}}", "{{REL_BOOTLOADER_MANIFEST_INFO}}"),
            ("{{REL_BUILD_LOG_FILE_FILENAME}}", "{{REL_BUILD_LOG_FILE}}"),
        ):
            self.assertEqual(mapping[filename_ph], EXTRA_FILES[share_ph])
        dsm.list_files.assert_any_call(folder_path="/offline/cologne/6.5.15.1/Release")
        self.assertIn(str(payload), self.output.getvalue())
        return dsm

    def test_partial_local_downloads_still_resolve_all_nas_artifacts(self):
        self.run_resume(local_main=True, local_payload=False)

    def test_existing_payload_is_reused_without_running_prepare(self):
        self.run_resume(local_main=True, local_payload=True)

    def test_remote_files_resolve_after_all_local_artifacts_are_cleaned(self):
        self.run_resume(local_main=False, local_payload=False)

    def test_relative_prepare_paths_resolve_next_to_config(self):
        self.run_resume(relative_workspace=True)

    def test_missing_dsl_directory_never_resolves_to_repository_root(self):
        with self.assertRaisesRegex(RuntimeError, "upload_payload_dir"):
            pipeline._dsl_prepare_paths({"prepare": {"flow": {"work_dir": str(self.temp)}}})

    def test_remote_completion_preserves_existing_links_without_recreating_them(self):
        dsm = mock.Mock()
        dsm.list_files.return_value = ["known.zip", "new.zip"]
        dsm.create_share_link.return_value = "https://nas.invalid/new"
        self.patch(pipeline, "SynologyDsmClient", return_value=dsm)
        links = pipeline._generate_share_links_from_remote(dsm_base_url="https://nas.invalid", dsm_verify_tls=False,
            timeout_sec=1, username="offline", password="offline", remote_dir="/Release",
            existing_links={"known.zip": "https://nas.invalid/known"})
        self.assertEqual(links, {"known.zip": "https://nas.invalid/known", "new.zip": "https://nas.invalid/new"})
        dsm.create_share_link.assert_called_once_with(path="/Release/new.zip")


class WebResumeTests(OfflineTest):
    def setUp(self):
        super().setUp()
        self.patch(web, "_running_tasks", {})
        self.patch(web, "_task_streams", {})
        self.patch(web, "SCRIPT_DIR", self.temp)
        self.patch(web, "_inject_user_feishu_token")
        self.patch(web, "_persist_running_tasks")
        self.patch(web.threading, "Thread")
        self.client = web.app.test_client()
        (self.temp / "common.json").write_text("{}")
        cfg = {"base_config": "common.json", "release": {"project": "cologne", "version": "6.5.15.1"},
               "vars": {"build_release_mode": "single", "tag": "saved-tag"},
               "jenkins": {"builds": {"release": {"build_url": RELEASE_URL},
                                      "debug": {"build_url": "https://offline.invalid/job/firmware/1664/"}}}}
        self.original = self.temp / "gqf_cologne.json"
        self.original.write_text(json.dumps(cfg))

    def submit(self, **extra):
        payload = {"release": {"version": "6.5.15.1"}, "user_id": "offline",
                   "skip_flags": {"skip_download": True, "skip_prepare": True, "skip_upload": True}, **extra}
        before = self.original.read_bytes()
        res = self.client.post("/api/projects/cologne/run-pipeline-direct", json=payload)
        self.assertEqual(res.status_code, 200)
        cfg = read_json_with_base(Path(res.get_json()["config_file"]))
        self.assertEqual(self.original.read_bytes(), before)
        return cfg

    def test_empty_form_keeps_saved_ci_url_build_mode_and_tags(self):
        cfg = self.submit()
        self.assertEqual(cfg["jenkins"]["builds"]["release"]["build_url"], RELEASE_URL)
        self.assertEqual(cfg["vars"]["build_release_mode"], "single")
        self.assertEqual(cfg["vars"]["tag"], "saved-tag")

    def test_empty_ui_variables_do_not_erase_saved_build_mode_and_tags(self):
        cfg = self.submit(vars={"tag": "", "build_release_mode": "", "boot_tag": " "})
        self.assertEqual(cfg["vars"]["build_release_mode"], "single")
        self.assertEqual(cfg["vars"]["tag"], "saved-tag")

    def test_submitted_build_urls_override_saved_builds(self):
        cfg = self.submit(release_jenkins_url="https://offline.invalid/job/new/123",
                          debug_jenkins_url="https://offline.invalid/job/new/122")
        self.assertEqual(cfg["jenkins"]["builds"]["release"]["build_url"], "https://offline.invalid/job/new/123/")
        self.assertEqual(cfg["jenkins"]["builds"]["debug"]["build_url"], "https://offline.invalid/job/new/122/")


if __name__ == "__main__":
    unittest.main()
