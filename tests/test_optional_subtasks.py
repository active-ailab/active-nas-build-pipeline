"""Optional release tasks fail visibly without cancelling firmware publishing."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from config_loader import read_json_with_base
from lark_cli_adapter import ReleaseDocResult
import jenkins_trigger_build as jenkins
import release_pipeline_run as pipeline
import web_release_server as web


ROOT = Path(__file__).resolve().parents[1]


class OptionalSubtaskTests(unittest.TestCase):
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

    def patch(self, *args, **kwargs):
        return self.stack.enter_context(mock.patch.object(*args, **kwargs))

    def run_jenkins(self, fault, standalone=False):
        cfg = read_json_with_base(ROOT / "gqf_fuji.json")
        cfg["vars"].update(build_tscan="yes", build_release_mode="split")
        cfg["jenkins"]["triggers"].update(auto_run_pipeline=True, modes="async")
        cfg["jenkins"]["auth"] = {"username": "offline", "password": "offline"}
        cfg["changelog"]["enabled"] = False
        path = self.temp / "gqf_offline.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        self.patch(jenkins, "_try_get_crumb", return_value=None)
        for name in ("_maybe_notify_webhook_text", "_send_bot_notification", "_notify_base_stage"):
            self.patch(jenkins, name)
        changelog = self.patch(jenkins, "_maybe_trigger_changelog_async")
        if fault == "changelog":
            changelog.side_effect = OSError("diff unavailable")

        def trigger(**kw):
            if kw["parameters"].get("TSCANCODE_CHECK") == "YES" and fault == "trigger":
                raise OSError("TSCAN trigger failed")
            return "https://offline.invalid/queue/1/"

        def queued(**kw):
            optional = all(name.endswith("tscan") for name in kw["queues"])
            if optional and fault == "queue":
                raise TimeoutError("TSCAN queue timeout")
            return {name: (1, "https://offline.invalid/job/ci/1/") for name in kw["queues"]}

        def polled(**kw):
            optional = all(name.endswith("tscan") for name in kw["builds"])
            if optional and fault == "timeout":
                raise TimeoutError("quality-check timed out")
            if optional and fault == "missing-child":
                raise RuntimeError("quality-check build number missing")
            result = "FAILURE" if optional and fault == "failure" else "SUCCESS"
            results = {name: result for name in kw["builds"]}
            if not optional and fault == "firmware":
                results[next(iter(results))] = "FAILURE"
            callback = kw.get("on_build_finished")
            if callback:
                for name, (number, url) in kw["builds"].items():
                    callback(name, number, url, results[name])
            return results

        self.patch(jenkins, "_trigger_build", side_effect=trigger)
        self.patch(jenkins, "_poll_queues_for_builds", side_effect=queued)
        self.patch(jenkins, "_poll_build_results", side_effect=polled)
        download = self.patch(jenkins, "_download_tscan_artifacts")
        if fault == "download":
            download.side_effect = OSError("TSCAN download failed")
        runner = self.patch(jenkins.subprocess, "run", return_value=SimpleNamespace(returncode=0))
        argv = ["jenkins", "--config", str(path), "--no-backup"]
        if standalone:
            argv.append("--tscan-standalone")
        self.patch(sys, "argv", argv)
        code = jenkins.main()
        return code, runner, json.loads(path.read_text(encoding="utf-8"))

    def test_attached_tscan_failures_and_timeouts_still_start_release_pipeline(self):
        for fault in ("trigger", "queue", "timeout", "missing-child", "failure", "download"):
            with self.subTest(fault=fault), contextlib.ExitStack() as current:
                # Each case gets independent mocks and cleanup.
                original = self.stack
                self.stack = current
                try:
                    code, runner, cfg = self.run_jenkins(fault)
                    self.assertEqual(code, 0)
                    runner.assert_called_once()
                    self.assertIn("tscan", cfg["subtask_errors"])
                finally:
                    self.stack = original

    def test_standalone_tscan_failure_stops_its_own_task(self):
        for fault in ("trigger", "queue", "timeout", "failure"):
            with self.subTest(fault=fault), contextlib.ExitStack() as current:
                original = self.stack
                self.stack = current
                try:
                    code, runner, _ = self.run_jenkins(fault, standalone=True)
                    self.assertNotEqual(code, 0)
                    runner.assert_not_called()
                finally:
                    self.stack = original

    def test_firmware_failure_remains_blocking(self):
        code, runner, _ = self.run_jenkins("firmware")
        self.assertNotEqual(code, 0)
        runner.assert_not_called()

    def test_changelog_trigger_error_does_not_block_release(self):
        code, runner, cfg = self.run_jenkins("changelog")
        self.assertEqual(code, 0)
        runner.assert_called_once()
        self.assertIn("changelog", cfg["subtask_errors"])

    def test_changelog_mapping_error_or_no_report_is_optional_and_not_retried(self):
        for failure in (TimeoutError("diff timed out"), ""):
            cfg = {"changelog": {"enabled": True}}
            with mock.patch.object(pipeline, "_build_changelog_link_impl") as build:
                if isinstance(failure, Exception):
                    build.side_effect = failure
                else:
                    build.return_value = failure
                self.assertEqual(pipeline._build_changelog_link(cfg=cfg), "")
                self.assertEqual(pipeline._build_changelog_link(cfg=cfg), "")
                build.assert_called_once()
                self.assertIn("changelog", cfg["subtask_errors"])

    def test_changelog_timeout_does_not_stop_release_document_generation(self):
        cfg = read_json_with_base(ROOT / "gqf_fuji.json")
        cfg["prepare"] = {"mode": "none", "workspace_dir": str(self.temp / "prepare")}
        cfg["doc"]["enabled"] = False
        cfg["feishu"].update(enabled=True, print_placeholder_mapping=False)
        cfg["notifications"] = {}
        cfg["changelog"]["enabled"] = True
        cfg["vars"]["build_release_mode"] = "split"
        path = self.temp / "document.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        self.patch(sys, "argv", ["pipeline", "--config", str(path), "--skip-download", "--skip-prepare",
                                 "--skip-upload", "--skip-doc", "--skip-cleanup"])
        self.stack.enter_context(mock.patch.dict(os.environ, {key: "offline" for key in (
            "JENKINS_USERNAME", "JENKINS_PASSWORD", "NAS_WEBDAV_USERNAME", "NAS_WEBDAV_PASSWORD",
            "NAS_DSM_USERNAME", "NAS_DSM_PASSWORD", "DSM_PASSWORD")}))
        for name in ("_notify_base_stage", "_maybe_notify_webhook_text", "_feishu_notify_user"):
            self.patch(pipeline, name)
        self.patch(pipeline, "_generate_share_links_from_remote", return_value={})
        self.patch(pipeline, "_build_changelog_link_impl", side_effect=TimeoutError("diff timed out"))
        self.patch(pipeline, "_LARK_CLI_AVAILABLE", True)
        self.patch(pipeline, "lark_auth_ensure", return_value=True)
        publish = self.patch(pipeline, "lark_publish_release_document",
                             return_value=ReleaseDocResult(wiki_url="https://zepp.feishu.cn/wiki/offline"))
        self.assertEqual(pipeline.main(), 0)
        publish.assert_called_once()
        self.assertFalse(publish.call_args[1]["placeholder_map"].get("{{REL_CHANGELOG_LINK}}"))

    def test_failed_tscan_cannot_reuse_old_nas_links_or_required_placeholders(self):
        cfg = {"release": {"project": "fuji"}, "subtask_errors": {"tscan": "timeout"}}
        dsl = {"placeholders": {"strategy": {"missing_placeholder": "error"}, "mappings": {
            "{{REL_TSCAN_OTA}}": {"mode": "nas_share", "run": "tscan", "required": True,
                                  "match": {"exact": "OTA_tscanResult.zip"}}}}}
        runs = [{"name": "tscan", "share_links_source": "nas", "share_links_main": {
            "OTA_tscanResult.zip": "https://nas.invalid/old"}}]
        result = pipeline._build_placeholder_replacements_from_dsl(
            cfg=cfg, run_summaries=runs, dsl_cfg=dsl, dsl_runtime={})
        self.assertEqual(result["{{REL_TSCAN_OTA}}"], "")

    def test_optional_tscan_error_updates_child_status_without_failing_main_task(self):
        task = {"project": "fuji", "status": "running",
                "steps": [{"status": "waiting", "percent": 0} for _ in range(5)], "log": []}
        self.patch(web, "_running_tasks", {"test": task})
        self.patch(web, "_persist_running_tasks")
        web._process_pipeline_line("test", "[TSCAN:ERROR] TimeoutError: timed out; 主发布流程继续")
        self.assertEqual(task["status"], "running")
        self.assertEqual(task["_tscan_state"]["status"], "failed")

    def _run_standalone_changelog(self, late_report=False):
        self.patch(web, "SCRIPT_DIR", self.temp)
        self.patch(web, "_running_tasks", {})
        self.patch(web, "_task_streams", {})
        self.patch(web, "_persist_running_tasks")
        self.patch(web, "_track_event")
        self.patch(web, "_notify_changelog_done")
        self.patch(web.time, "sleep")
        self.patch(web, "_resolve_jenkins_build_url", return_value="https://offline.invalid/job/diff/42/")
        thread = self.patch(web.threading, "Thread")
        session = mock.Mock()
        session.post.return_value = SimpleNamespace(status_code=201, headers={"Location": "offline-queue"})
        console_reads = []
        def get(url, **kw):
            if url.endswith("consoleText"):
                console_reads.append(url)
                if late_report and len(console_reads) >= 2:
                    return SimpleNamespace(status_code=200, text="差分报告飞书链接: https://zepp.feishu.cn/docx/offline")
                return SimpleNamespace(status_code=200, text="未生成差分报告，请检查差分版本间是否无提交记录")
            return SimpleNamespace(status_code=200, json=lambda: {
                "crumb": "offline", "building": False, "result": "SUCCESS"})
        session.get.side_effect = get
        self.patch(web.requests, "Session", return_value=session)
        response = web.app.test_client().post("/api/projects/fuji/start-changelog", json={
            "prev_version_name": "1.0.0", "curr_version_name": "1.0.1",
            "jenkins_auth": {"username": "offline", "password": "offline"}})
        task_id = response.get_json()["task_id"]
        thread.call_args[1]["target"]()
        return web._running_tasks[task_id]

    def test_standalone_changelog_without_report_finishes_failed(self):
        task = self._run_standalone_changelog()
        self.assertEqual(task["status"], "failed")
        self.assertNotEqual(task["exit_code"], 0)

    def test_standalone_changelog_checks_final_console_before_declaring_no_result(self):
        task = self._run_standalone_changelog(late_report=True)
        self.assertEqual(task["status"], "success")
        self.assertEqual(task["exit_code"], 0)
        self.assertEqual(task["feishu_link"], "https://zepp.feishu.cn/docx/offline")
