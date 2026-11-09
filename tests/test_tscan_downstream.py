"""Track the quality-check run started by this parent; never use lastBuild."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

import jenkins_trigger_build as jenkins
import release_pipeline_run as pipeline
from artifact_matching import select_artifact
from tscan_support import quality_build_from_console
from config_loader import read_json_with_base


ROOT = Path(__file__).resolve().parents[1]
PARENT = "https://offline.invalid/job/firmware_auto_trigger/job/HuamiOS_multi_platform/190/"
CHILD = "https://offline.invalid/job/firmware_auto_trigger/job/HuamiOS_multi_platform_quality_check/23/"
CONSOLE = "Starting building: 嵌入式编译 » HuamiOS_multi_platform_quality_check #23\n"
AUTH = jenkins.JenkinsAuth("offline", "offline")


class TscanDownstreamTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        for name in ("urllib.request.urlopen", "urllib.request.OpenerDirector.open",
                     "requests.sessions.Session.request", "subprocess.Popen"):
            self.stack.enter_context(mock.patch(name, side_effect=AssertionError("External action: " + name)))
        self.stack.enter_context(mock.patch.object(jenkins.time, "sleep"))
        self.temp = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=str(ROOT / "output"))))

    def patch(self, *args, **kwargs):
        return self.stack.enter_context(mock.patch.object(*args, **kwargs))

    def poll(self, builds, callback=None):
        return jenkins._poll_build_results(builds=builds, auth=AUTH, poll_interval_sec=1,
            build_timeout_sec=60, request_timeout_sec=5, opener=None, on_build_finished=callback)

    def test_exact_child_is_polled_until_finished_after_parent_success(self):
        builds = {"default.tscan": (190, PARENT)}
        api = self.patch(jenkins, "_request_json", side_effect=[
            {"building": False, "result": "SUCCESS"}, {"building": True},
            {"building": False, "result": "SUCCESS"}])
        self.patch(jenkins, "_request", return_value=(200, {}, CONSOLE.encode()))
        finished = mock.Mock()
        self.assertEqual(self.poll(builds, finished), {"default.tscan": "SUCCESS"})
        self.assertEqual(builds["default.tscan"], (23, CHILD))
        self.assertEqual([c[1]["url"] for c in api.call_args_list],
                         [PARENT + "api/json", CHILD + "api/json", CHILD + "api/json"])
        finished.assert_called_once_with("default.tscan", 23, CHILD, "SUCCESS")

    def test_child_failure_is_not_parent_success(self):
        self.patch(jenkins, "_request_json", side_effect=[
            {"building": False, "result": "SUCCESS"}, {"building": False, "result": "FAILURE"}])
        self.patch(jenkins, "_request", return_value=(200, {}, CONSOLE.encode()))
        self.assertEqual(self.poll({"tscan": (190, PARENT)}), {"tscan": "FAILURE"})

    def test_transient_console_error_is_retried_without_switching_to_other_builds(self):
        self.patch(jenkins, "_request_json", side_effect=[
            {"building": True}, {"building": False, "result": "SUCCESS"},
            {"building": False, "result": "SUCCESS"}])
        self.patch(jenkins, "_request", side_effect=[OSError("temporary timeout"), (200, {}, CONSOLE.encode())])
        builds = {"tscan": (190, PARENT)}
        self.assertEqual(self.poll(builds), {"tscan": "SUCCESS"})
        self.assertEqual(builds["tscan"], (23, CHILD))

    def test_finished_parent_without_child_number_is_an_error(self):
        self.patch(jenkins, "_request_json", return_value={"building": False, "result": "SUCCESS"})
        self.patch(jenkins, "_request", return_value=(200, {}, b"Finished: SUCCESS"))
        with self.assertRaisesRegex(RuntimeError, "number missing"):
            self.poll({"tscan": (190, PARENT)})

    def test_console_parser_deduplicates_start_and_finish_but_rejects_two_runs(self):
        complete = "Build 嵌入式编译 » HuamiOS_multi_platform_quality_check #23 completed: SUCCESS"
        self.assertEqual(quality_build_from_console(PARENT, CONSOLE + complete), (23, CHILD))
        with self.assertRaisesRegex(RuntimeError, "multiple"):
            quality_build_from_console(PARENT, CONSOLE + complete.replace("#23", "#24"))

    def test_legacy_tscan_and_multi_platform_firmware_are_not_redirected(self):
        self.patch(jenkins, "_request_json", return_value={"building": False, "result": "SUCCESS"})
        console = self.patch(jenkins, "_request")
        builds = {"tscan": (1, "https://offline.invalid/job/HuamiOS/1/"), "release": (190, PARENT)}
        self.assertEqual(self.poll(builds), {"tscan": "SUCCESS", "release": "SUCCESS"})
        console.assert_not_called()

    def metadata(self):
        return {"artifacts": [{"relativePath": f"quality_results/{platform}/{platform}_{kind}_tscanResult.zip"}
            for platform in ("mhs003", "mhs003s") for kind in ("BOOT", "OTA", "RECOVERY")]}

    def download(self, cfg, out):
        jenkins._download_tscan_artifacts(build_url=CHILD, out_dir=out, auth=AUTH,
            base_url="https://offline.invalid", verify_tls=True, request_timeout_sec=5,
            opener=None, project="geneva", cfg=cfg)

    def test_download_uses_real_nested_paths_and_keeps_only_selected_platform(self):
        cfg = {"platform_select": "mhs003"}
        out = self.temp / "tscan"
        out.mkdir()
        (out / "BOOT_tscanResult.zip").write_bytes(b"stale")
        self.patch(jenkins, "_poll_build_results", return_value={"tscan": "SUCCESS"})
        self.patch(jenkins, "_request_json", return_value=self.metadata())
        urls = []
        def download(**kw):
            urls.append(kw["url"])
            kw["dest"].write_bytes(b"new report")
        self.patch(jenkins, "_download_file", side_effect=download)
        self.download(cfg, out)
        expected = [f"mhs003_{kind}_tscanResult.zip" for kind in ("BOOT", "OTA", "RECOVERY")]
        self.assertEqual(sorted(p.name for p in out.iterdir()), sorted(expected))
        self.assertEqual(urls, [CHILD + "artifact/quality_results/mhs003/" + name for name in expected])
        self.assertEqual(cfg["jenkins"]["builds"]["tscan"]["build_url"], CHILD)

    def test_failed_download_does_not_replace_old_reports_or_mark_new_build_downloaded(self):
        cfg = {"platform_select": "mhs003"}
        out = self.temp / "tscan"
        out.mkdir()
        (out / "BOOT_tscanResult.zip").write_bytes(b"old")
        self.patch(jenkins, "_poll_build_results", return_value={"tscan": "SUCCESS"})
        self.patch(jenkins, "_request_json", return_value=self.metadata())
        self.patch(jenkins, "_download_file", side_effect=OSError("offline download failed"))
        with self.assertRaises(OSError):
            self.download(cfg, out)
        self.assertEqual((out / "BOOT_tscanResult.zip").read_bytes(), b"old")
        self.assertNotIn("jenkins", cfg)

    def test_no_selected_platform_reports_is_an_error(self):
        self.patch(jenkins, "_poll_build_results", return_value={"tscan": "SUCCESS"})
        self.patch(jenkins, "_request_json", return_value=self.metadata())
        with self.assertRaisesRegex(RuntimeError, "No matching"):
            self.download({"platform_select": "apollo5"}, self.temp / "tscan")

    def test_authenticated_download_uses_opener_without_unsupported_context_argument(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"report"
        opener = SimpleNamespace(open=lambda request, timeout: response)
        target = self.temp / "report.zip"
        jenkins._download_file(url=CHILD + "artifact/report.zip", dest=target,
                              auth=AUTH, verify_tls=True, timeout_sec=5, opener=opener)
        self.assertEqual(target.read_bytes(), b"report")

    def test_standalone_saves_quality_check_url_after_waiting(self):
        cfg = {"release": {"project": "geneva"}, "platform_select": "mhs003",
               "jenkins": {"triggers": {"job_url": PARENT.rsplit("190/", 1)[0]}}}
        self.patch(jenkins, "_trigger_build", return_value="offline-queue")
        self.patch(jenkins, "_poll_queues_for_builds", return_value={"tscan": (190, PARENT)})
        self.patch(jenkins, "_request_json", side_effect=[
            {"building": False, "result": "SUCCESS"}, {"building": False, "result": "SUCCESS"}])
        self.patch(jenkins, "_request", return_value=(200, {}, CONSOLE.encode()))
        args = SimpleNamespace(dry_run=False, request_timeout_sec=5, poll_interval_sec=1,
                               queue_timeout_sec=60, build_timeout_sec=60)
        target = self.temp / "standalone.json"
        self.assertEqual(jenkins._run_tscan_standalone(args, cfg, target, AUTH, None, None), 0)
        self.assertEqual(json.loads(target.read_text())["jenkins"]["builds"]["tscan"]["build_url"], CHILD)

    def test_prefixed_and_legacy_report_names_fill_both_document_mapping_paths(self):
        dsl = json.loads((ROOT / "placeholders.default.json").read_text(encoding="utf-8"))
        cfg = {"platform_select": "mhs003", "release": {"project": "geneva"}, "changelog": {"enabled": False}}
        for prefix in ("", "mhs003_"):
            links = {f"{prefix}{kind}_tscanResult.zip": "https://nas.invalid/" + kind for kind in ("BOOT", "OTA", "RECOVERY")}
            links["mhs003s_OTA_tscanResult.zip"] = "https://nas.invalid/wrong-platform"
            runs = [{"name": "tscan", "share_links_source": "nas", "share_links_main": links}]
            for legacy in (False, True):
                if legacy:
                    mapping = pipeline._build_placeholder_replacements(cfg=cfg, run_summaries=runs)
                else:
                    mapping = pipeline._build_placeholder_replacements_from_dsl(
                        cfg=cfg, run_summaries=runs, dsl_cfg=dsl, dsl_runtime={"vars": {}})
                for kind in ("BOOT", "OTA", "RECOVERY"):
                    self.assertEqual(mapping["{{REL_TSCAN_" + kind + "}}"],
                        f"{prefix}{kind}_tscanResult.zip|https://nas.invalid/{kind}")

    def test_prefixed_report_is_preferred_over_an_old_legacy_name(self):
        files = [("BOOT_tscanResult.zip", "old"), ("mhs003_BOOT_tscanResult.zip", "new")]
        self.assertEqual(select_artifact(files, cfg={"platform_select": "mhs003"},
            placeholder="{{REL_TSCAN_BOOT}}", run="tscan"), files[1])

    def _run_direct_pipeline(self, fail_download=False):
        cfg = read_json_with_base(ROOT / "gqf_geneva.json")
        cfg["platform_select"] = "mhs003"
        cfg["prepare"] = {"mode": "none", "workspace_dir": str(self.temp / "prepare")}
        cfg["changelog"] = {"enabled": False}
        cfg["notifications"] = {}
        cfg["feishu"]["enabled"] = False
        cfg["doc"]["enabled"] = False
        cfg["vars"].update(build_release_mode="split", build_tscan="no")
        cfg["nas"]["uploads"] = []
        for run, number, folder in (("release", 1, "Release"), ("debug", 2, "Monkey"), ("tscan", 190, "Monkey")):
            out = self.temp / run
            out.mkdir()
            cfg["jenkins"]["builds"][run] = {
                "build_url": PARENT if run == "tscan" else f"https://offline.invalid/job/fw/{number}/",
                "download": {"output_dir": str(out), "include_globs": ["**/*"]}}
            cfg["nas"]["uploads"].append({"name": run, "remote_subdir": folder, "local_dir": str(out)})
        (self.temp / "tscan" / "OLD_tscanResult.zip").write_bytes(b"old")
        cp = self.temp / "direct.json"
        cp.write_text(json.dumps(cfg), encoding="utf-8")
        self.stack.enter_context(mock.patch.dict(os.environ, {k: "offline" for k in (
            "JENKINS_USERNAME", "JENKINS_PASSWORD", "NAS_WEBDAV_USERNAME", "NAS_WEBDAV_PASSWORD",
            "NAS_DSM_USERNAME", "NAS_DSM_PASSWORD", "DSM_PASSWORD")}))
        self.patch(sys, "argv", ["pipeline", "--config", str(cp), "--skip-prepare", "--skip-doc", "--skip-feishu", "--skip-cleanup"])
        for name in ("_notify_base_stage", "_maybe_notify_webhook_text", "_feishu_notify_user", "jenkins_download_artifacts"):
            self.patch(pipeline, name)
        upload = self.patch(pipeline, "_run_uploader")
        self.patch(pipeline, "_generate_and_print_share_links", return_value={})
        def poll(**kw):
            kw["builds"]["tscan"] = (23, CHILD)
            return {"tscan": "SUCCESS"}
        self.patch(jenkins, "_poll_build_results", side_effect=poll)
        self.patch(jenkins, "_request_json", return_value=self.metadata())
        self.patch(jenkins, "_download_file", side_effect=OSError("quality report unavailable") if fail_download
                   else lambda **kw: kw["dest"].write_bytes(b"new"))
        self.assertEqual(pipeline.main(), 0)
        if fail_download:
            self.assertFalse(any(c[1]["local_dir"] == str(self.temp / "tscan") for c in upload.call_args_list))
            self.assertEqual(len(upload.call_args_list), 2)
            return
        reports = sorted(p.name for p in (self.temp / "tscan").iterdir())
        self.assertEqual(reports, sorted(f"mhs003_{kind}_tscanResult.zip" for kind in ("BOOT", "OTA", "RECOVERY")))
        tscan_upload = [c[1] for c in upload.call_args_list if c[1]["local_dir"] == str(self.temp / "tscan")]
        self.assertEqual(len(tscan_upload), 1)
        self.assertEqual(tscan_upload[0]["folder_name"], "Monkey")

    def test_direct_pipeline_replaces_stale_reports_and_uploads_selected_quality_artifacts(self):
        self._run_direct_pipeline()

    def test_direct_pipeline_report_download_failure_does_not_stop_firmware_upload(self):
        self._run_direct_pipeline(fail_download=True)
