"""Offline release regressions. Never trigger Jenkins, NAS uploads or Feishu writes.

Run from the repository root: python -m unittest discover -s tests -v
"""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

import jenkins_trigger_build as jenkins
import lark_cli_adapter as lark
import lark_release as cli
import release_pipeline_run as pipeline
import web_release_server as web
from config_loader import read_json_with_base


ROOT = Path(__file__).resolve().parents[1]


class OfflineTest(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        for name in ("urllib.request.urlopen", "urllib.request.OpenerDirector.open",
                     "requests.sessions.Session.request", "subprocess.Popen"):
            self.stack.enter_context(mock.patch(name, side_effect=AssertionError("External action: " + name)))

    def patch(self, *args, **kwargs):
        return self.stack.enter_context(mock.patch.object(*args, **kwargs))


class DocumentTests(OfflineTest):
    def test_wiki_failure_propagates_without_notification(self):
        self.patch(lark, "lark_wiki_copy_node", side_effect=lark.LarkCliError("offline failure"))
        notify = self.patch(lark, "lark_send_message")
        with self.assertRaises(lark.LarkCliError):
            lark.lark_publish_release_document(
                title="test", markdown_content="body", notification_chat_id="offline",
                wiki_config={"template_node_token": "template", "target_space_id": "space"})
        notify.assert_not_called()

    def test_incomplete_wiki_copy_is_failure(self):
        self.patch(lark, "lark_wiki_copy_node", return_value={"url": "wiki"})
        with self.assertRaises(lark.LarkCliError):
            lark.lark_publish_release_document(title="test", markdown_content="body",
                wiki_config={"template_node_token": "template", "target_space_id": "space"})

    def test_empty_doc_create_is_failure(self):
        self.patch(lark, "lark_create_doc_from_markdown", return_value={})
        with self.assertRaises(lark.LarkCliError):
            lark.lark_publish_release_document(title="test", markdown_content="body")

    def test_replacement_failure_propagates(self):
        self.patch(lark, "lark_create_doc_from_markdown", return_value={"url": "doc"})
        self.patch(lark, "_replace_placeholders_via_lark", side_effect=lark.LarkCliError("replace failed"))
        with self.assertRaises(lark.LarkCliError):
            lark.lark_publish_release_document(title="test", markdown_content="body", placeholder_map={"{{A}}": "a"})

    def test_successful_wiki_keeps_existing_notification(self):
        self.patch(lark, "lark_wiki_copy_node", return_value={"url": "wiki", "docx_token": "docx"})
        replace = self.patch(lark, "_replace_placeholders_via_lark")
        notify = self.patch(lark, "lark_send_message", return_value=True)
        result = lark.lark_publish_release_document(title="test", markdown_content="body",
            placeholder_map={"{{A}}": "a"}, required_placeholders=["{{A}}"],
            wiki_config={"template_node_token": "template", "target_space_id": "space"},
            notification_chat_id="offline")
        self.assertEqual(result.wiki_url, "wiki")
        self.assertTrue(result.notification_sent)
        replace.assert_called_once_with("docx", {"{{A}}": "a"}, required_placeholders=["{{A}}"])
        notify.assert_called_once()

    def verify(self, text, mapping, required=None):
        self.patch(lark, "lark_fetch_doc", return_value=text)
        self.stack.enter_context(mock.patch("time.sleep"))
        lark._verify_replacement("offline", mapping, required_placeholders=required)

    def test_empty_required_link_fails(self):
        with self.assertRaises(lark.LarkCliError):
            self.verify("x" * 400 + " {{OTA}}", {"{{OTA}}": ""}, ["{{OTA}}"])

    def test_nonempty_unreplaced_value_fails(self):
        with self.assertRaises(lark.LarkCliError):
            self.verify("x" * 400 + " {{A}}", {"{{A}}": "a"})

    def test_optional_empty_field_remains_allowed(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.verify("x" * 400 + " {{OPTIONAL}}", {"{{OPTIONAL}}": ""})
        self.assertNotIn("[OK]", output.getvalue())
        self.assertIn("[WARN]", output.getvalue())

    def test_required_field_unused_by_template_is_allowed(self):
        self.verify("x" * 400, {"{{UNUSED}}": ""}, ["{{UNUSED}}"])

    def test_fetch_failure_is_not_success(self):
        self.patch(lark, "lark_fetch_doc", side_effect=lark.LarkCliError("fetch failed"))
        self.stack.enter_context(mock.patch("time.sleep"))
        with self.assertRaises(lark.LarkCliError):
            lark._verify_replacement("offline", {})

    def test_dry_run_performs_no_document_operations(self):
        create = self.patch(lark, "lark_create_doc_from_markdown")
        result = lark.lark_publish_release_document(title="test", markdown_content="body", dry_run=True)
        self.assertFalse(result.doc_url)
        create.assert_not_called()


class ArtifactTests(OfflineTest):
    def setUp(self):
        super().setUp()
        self.temp = self.stack.enter_context(tempfile.TemporaryDirectory(dir=str(ROOT / "output")))
        self.src, self.dst = Path(self.temp) / "release", Path(self.temp) / "debug"
        self.src.mkdir()
        self.dst.mkdir()
        (self.src / "archive_1.2.1.11.tgz").write_text("release")
        (self.src / "archive_1.2.1.10.tgz").write_text("debug")
        (self.dst / "archive_0.9.1.8.tgz").write_text("old debug")

    def test_fresh_download_removes_old_debug(self):
        pipeline._split_artifacts_by_parity(src_dir=self.src, dst_dir=self.dst, dry_run=False, reset_destination=True)
        self.assertEqual([p.name for p in self.dst.iterdir()], ["archive_1.2.1.10.tgz"])
        self.assertTrue((self.src / "archive_1.2.1.11.tgz").exists())

    def test_resume_preserves_existing_debug(self):
        pipeline._split_artifacts_by_parity(src_dir=self.src, dst_dir=self.dst, dry_run=False)
        self.assertTrue((self.dst / "archive_0.9.1.8.tgz").exists())

    def test_dry_run_preserves_both_directories(self):
        pipeline._split_artifacts_by_parity(src_dir=self.src, dst_dir=self.dst, dry_run=True, reset_destination=True)
        self.assertTrue((self.src / "archive_1.2.1.10.tgz").exists())
        self.assertTrue((self.dst / "archive_0.9.1.8.tgz").exists())

    def test_overlapping_directories_are_rejected_before_cleanup(self):
        with self.assertRaises(RuntimeError):
            pipeline._split_artifacts_by_parity(src_dir=self.src, dst_dir=self.src, dry_run=False, reset_destination=True)
        self.assertTrue((self.src / "archive_1.2.1.11.tgz").exists())


class CliTests(OfflineTest):
    def test_all_device_configs_use_same_inheritance_as_engine(self):
        for f in ROOT.glob("gqf_*.json"):
            self.assertEqual(cli._load_config(str(f)), read_json_with_base(f))

    def test_doc_inherits_enabled_and_renders_title(self):
        temp = self.stack.enter_context(tempfile.TemporaryDirectory(dir=str(ROOT / "output")))
        md = Path(temp) / "doc.md"
        md.write_text("offline markdown", encoding="utf-8")
        publish = self.patch(lark, "lark_publish_release_document", return_value=lark.ReleaseDocResult())
        args = SimpleNamespace(config=str(ROOT / "gqf_oslo.json"), md_file=str(md), doc_output="", dry_run=True)
        self.assertEqual(cli.cmd_doc(args), 0)
        self.assertIn("oslo", publish.call_args[1]["title"])
        self.assertNotIn("{REL_", publish.call_args[1]["title"])

    def test_doc_failure_returns_nonzero(self):
        temp = self.stack.enter_context(tempfile.TemporaryDirectory(dir=str(ROOT / "output")))
        md = Path(temp) / "doc.md"
        md.write_text("offline markdown", encoding="utf-8")
        self.patch(lark, "lark_auth_ensure", return_value=True)
        self.patch(lark, "lark_publish_release_document", side_effect=lark.LarkCliError("offline"))
        args = SimpleNamespace(config=str(ROOT / "gqf_oslo.json"), md_file=str(md), doc_output="", dry_run=False)
        self.assertNotEqual(cli.cmd_doc(args), 0)


class PlatformTests(OfflineTest):
    MULTI = "https://offline.invalid/job/HuamiOS_multi_platform/"

    def test_mhs_platforms_and_legacy_platform_list(self):
        for value, expected in [("mhs003", "mhs003"), ("MHS003S", "mhs003s"),
                                (["MHS003", "MHS003S"], "mhs003,mhs003s")]:
            self.assertEqual(jenkins._with_platform_select({"A": "a"}, {"platform_select": value}, self.MULTI),
                             {"A": "a", "PLATFORM_SELECT": expected})
        self.assertEqual(jenkins._with_platform_select({}, {"Platform": {"MHS003S": "YES"}}, self.MULTI),
                         {"PLATFORM_SELECT": "mhs003s"})

    def test_nxp_parameters_unchanged(self):
        params = {"BUILD_RELEASE": "release"}
        self.assertEqual(jenkins._with_platform_select(params, {"platform_select": "mhs003"}, "https://offline.invalid/job/HuamiOS/"), params)

    def test_standalone_ui_platform_overrides_saved_platform(self):
        cfg = read_json_with_base(ROOT / "gqf_windermere.json")
        cfg["platform_select"] = "mhs003"
        cfg["_tscan"] = {"platform": "MHS003S", "job_url": self.MULTI}
        trigger = self.patch(jenkins, "_trigger_build", return_value="")
        args = SimpleNamespace(dry_run=True, request_timeout_sec=5)
        self.assertEqual(jenkins._run_tscan_standalone(args, cfg, Path("offline.json"),
                         jenkins.JenkinsAuth("offline", "offline"), None, None), 0)
        self.assertEqual(trigger.call_args[1]["parameters"]["PLATFORM_SELECT"], "mhs003s")

    def test_full_build_adds_platform_to_release_debug_and_tscan(self):
        temp = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=str(ROOT / "output"))))
        cfg = read_json_with_base(ROOT / "gqf_windermere.json")
        cfg["platform_select"] = "mhs003s"
        cfg["vars"].update(build_tscan="yes", build_release_mode="split")
        cfg["jenkins"]["triggers"]["modes"] = "async"
        cfg["jenkins"]["triggers"]["auto_run_pipeline"] = False
        cfg["jenkins"]["auth"] = {"username": "offline", "password": "offline"}
        cp = temp / "gqf_offline.json"
        cp.write_text(json.dumps(cfg), encoding="utf-8")
        self.patch(sys, "argv", ["jenkins", "--config", str(cp)])
        trigger = self.patch(jenkins, "_trigger_build", return_value="offline-queue")
        for name in ("_maybe_notify_webhook_text", "_send_bot_notification", "_notify_base_stage"):
            self.patch(jenkins, name)
        self.patch(jenkins, "_try_get_crumb", return_value=None)
        self.patch(jenkins, "_poll_queues_for_builds", return_value={})
        self.patch(jenkins, "_poll_build_results", return_value={})
        self.assertEqual(jenkins.main(), 0)
        self.assertEqual(trigger.call_count, 3)
        for call in trigger.call_args_list:
            self.assertEqual(call[1]["parameters"]["PLATFORM_SELECT"], "mhs003s")


class WebSubmissionTests(OfflineTest):
    def setUp(self):
        super().setUp()
        self.patch(web, "_running_tasks", {})
        self.patch(web, "_task_streams", {})
        self.client = web.app.test_client()

    def test_same_project_blocked_before_config_write(self):
        web._running_tasks["active"] = {"project": "windermere", "status": "running"}
        for endpoint in ("release", "run-pipeline-direct", "tscan-only"):
            response = self.client.post("/api/projects/windermere/" + endpoint, json={"device": "windermere"})
            self.assertEqual(response.status_code, 409)
            self.assertFalse(response.get_json()["ok"])

    def test_other_project_and_finished_task_do_not_block(self):
        web._running_tasks["active"] = {"project": "other", "status": "running"}
        web._running_tasks["done"] = {"project": "windermere", "status": "success"}
        response = self.client.post("/api/projects/windermere/release", json={})
        self.assertEqual(response.status_code, 400)  # Existing version validation, no conflict.

    def test_finishing_worker_still_protects_shared_files(self):
        worker = mock.Mock()
        worker.is_alive.return_value = True
        web._running_tasks["done"] = {"project": "windermere", "status": "success", "thread": worker}
        self.assertEqual(self.client.post("/api/projects/windermere/release", json={}).status_code, 409)

    def test_restart_history_does_not_permanently_lock_project(self):
        web._running_tasks["old"] = {"project": "windermere", "status": "running", "_restored": True}
        self.assertEqual(self.client.post("/api/projects/windermere/release", json={}).status_code, 400)

    def test_task_ids_unique_in_same_second(self):
        self.assertEqual(len({web._new_task_id("offline") for _ in range(1000)}), 1000)

    def test_read_only_ui_endpoints_still_respond(self):
        for path in ("/", "/api/projects", "/api/projects/windermere/latest", "/api/projects/windermere/history"):
            self.assertEqual(self.client.get(path).status_code, 200)

    def test_submission_check_and_registration_are_atomic(self):
        calls = []
        @web._protect_project_submission
        def register(project):
            calls.append(project)
            web._running_tasks["created"] = {"project": project, "status": "starting"}
            return web.jsonify({"ok": True})
        results = []
        def submit():
            with web.app.test_request_context():
                results.append(web.app.make_response(register("offline")).status_code)
        threads = [threading.Thread(target=submit) for _ in range(2)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(sorted(results), [200, 409])
        self.assertEqual(calls, ["offline"])

    def prepare_submission(self):
        temp = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=str(ROOT / "output"))))
        (temp / "common.json").write_bytes((ROOT / "common.json").read_bytes())
        (temp / "gqf_windermere.json").write_bytes((ROOT / "gqf_windermere.json").read_bytes())
        self.patch(web, "SCRIPT_DIR", temp)
        self.patch(web, "_inject_user_feishu_token")
        self.patch(web, "_persist_running_tasks")
        self.patch(web, "_prewarm_lark_auth", return_value=True)
        threads = self.patch(web.threading, "Thread")
        threads.return_value.is_alive.return_value = False
        return temp, threads

    def test_normal_release_payload_and_task_response_unchanged(self):
        temp, threads = self.prepare_submission()
        payload = {"release": {"version": "1.2.3.1"}, "user_id": "offline", "platform_select": "mhs003"}
        response = self.client.post("/api/projects/windermere/release", json=payload)
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["ok"])
        task = web._running_tasks[data["task_id"]]
        self.assertEqual(task["user_id"], "offline")
        self.assertEqual(task["version"], "1.2.3.1")
        cfg = json.loads((temp / "gqf_windermere.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["release"]["version"], "1.2.3.1")
        threads.return_value.start.assert_called_once()
        before = (temp / "gqf_windermere.json").read_bytes()
        response = self.client.post("/api/projects/windermere/release", json=payload)
        self.assertEqual(response.status_code, 409)
        self.assertEqual((temp / "gqf_windermere.json").read_bytes(), before)

    def test_direct_document_resume_keeps_skip_flags_and_project_config(self):
        temp, threads = self.prepare_submission()
        original = (temp / "gqf_windermere.json").read_bytes()
        payload = {"release": {"version": "1.2.3.1"}, "user_id": "offline",
                   "skip_flags": {"skip_download": True, "skip_prepare": True, "skip_upload": True}}
        response = self.client.post("/api/projects/windermere/run-pipeline-direct", json=payload)
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual((temp / "gqf_windermere.json").read_bytes(), original)
        self.assertEqual(web._running_tasks[data["task_id"]]["task_type"], "skip_jenkins")
        self.assertTrue(Path(data["config_file"]).is_file())
        threads.return_value.start.assert_called_once()

    def test_wiki_failure_keeps_existing_single_automatic_retry(self):
        temp, threads = self.prepare_submission()
        payload = {"release": {"version": "1.2.3.1"}, "user_id": "offline",
                   "skip_flags": {"skip_download": True, "skip_prepare": True, "skip_upload": True}}
        response = self.client.post("/api/projects/windermere/run-pipeline-direct", json=payload)
        task_id = response.get_json()["task_id"]
        target = threads.call_args[1]["target"]
        proc = mock.Mock(returncode=1, stdout=io.StringIO("Wiki 失败: offline auth failure\n"))
        self.patch(web.subprocess, "Popen", return_value=proc)
        self.stack.enter_context(mock.patch("time.sleep"))
        retry = self.patch(web, "_launch_skip_jenkins_task")
        retry.return_value.get_json.return_value = {"task_id": "retry_offline"}
        target()
        self.assertEqual(web._running_tasks[task_id]["status"], "failed")
        retry.assert_called_once_with(payload, "windermere", _wiki_retry_count=1)
        self.assertEqual(web._running_tasks[task_id]["wiki_retried_task_id"], "retry_offline")

    def test_task_owner_checks_still_apply(self):
        web._running_tasks["offline"] = {"project": "offline", "user_id": "owner", "status": "running",
                                          "started_at": "2026-10-20T00:00:00"}
        for uid, expected in [("", 403), ("other", 403), ("owner", 200)]:
            self.assertEqual(self.client.get("/api/tasks/offline", query_string={"user_id": uid}).status_code, expected)


class PipelineOutcomeTests(OfflineTest):
    def run_pipeline(self, result=None, failure=None, skip_feishu=False, dry_run=False,
                     dsl=False, only="debug", skip_share=True):
        temp = self.stack.enter_context(tempfile.TemporaryDirectory(dir=str(ROOT / "output")))
        cfg = read_json_with_base(ROOT / "gqf_windermere.json")
        cfg["prepare"] = {"mode": "none"}
        if dsl:
            cfg["prepare"] = {"mode": "placeholders_dsl", "workspace_dir": str(Path(temp) / "prepare"),
                              "placeholders_config": str(ROOT / "placeholders.default.json")}
        cfg["doc"]["enabled"] = False
        cfg["feishu"]["print_placeholder_mapping"] = False
        cfg["cleanup"] = {"enabled": False}
        cfg["changelog"] = {"enabled": False}
        cfg["vars"]["build_release_mode"] = "split"
        cfg["jenkins"]["auth"] = {"username": "offline", "password": "offline"}
        for name in ("release", "debug"):
            cfg["jenkins"]["builds"][name]["build_url"] = "https://offline.invalid/1/"
        cfg["nas"]["webdav"]["auth"] = {"username": "offline", "password": "offline"}
        cp = Path(temp) / "config.json"
        cp.write_text(json.dumps(cfg), encoding="utf-8")
        args = ["pipeline", "--config", str(cp), "--only", only, "--skip-download", "--skip-prepare",
                "--skip-upload", "--skip-doc", "--skip-cleanup"]
        if skip_share: args.append("--skip-share")
        if skip_feishu: args.append("--skip-feishu")
        if dry_run: args.append("--dry-run")
        self.patch(sys, "argv", args)
        for name in ("_notify_base_stage", "_maybe_notify_webhook_text", "_feishu_notify_user"):
            self.patch(pipeline, name)
        self.patch(pipeline, "lark_auth_ensure", return_value=True)
        self.patch(pipeline, "_build_placeholder_replacements", return_value={})
        self.patch(pipeline, "_build_placeholder_replacements_from_dsl", return_value={})
        publish = self.patch(pipeline, "lark_publish_release_document", return_value=result, side_effect=failure)
        return pipeline.main(), publish

    def test_failed_document_no_longer_returns_success(self):
        code, _ = self.run_pipeline(result=lark.ReleaseDocResult())
        self.assertNotEqual(code, 0)

    def test_document_exception_returns_failure(self):
        code, _ = self.run_pipeline(failure=lark.LarkCliError("offline"))
        self.assertNotEqual(code, 0)

    def test_successful_document_keeps_zero_exit(self):
        code, _ = self.run_pipeline(result=lark.ReleaseDocResult(wiki_url="offline"))
        self.assertEqual(code, 0)

    def test_explicit_skip_keeps_success(self):
        code, publish = self.run_pipeline(skip_feishu=True)
        self.assertEqual(code, 0)
        publish.assert_not_called()

    def test_dry_run_allows_no_document_url(self):
        code, _ = self.run_pipeline(result=lark.ReleaseDocResult(), dry_run=True)
        self.assertEqual(code, 0)

    def test_dsl_required_fields_reach_document_verifier(self):
        code, publish = self.run_pipeline(result=lark.ReleaseDocResult(), dry_run=True,
                                          dsl=True, only="all", skip_share=False)
        self.assertEqual(code, 0)
        required = publish.call_args[1]["required_placeholders"]
        self.assertIn("{{REL_VERSION}}", required)
        self.assertIn("{{REL_RELEASE_OTA_CLOUD_ARCHIVE}}", required)
        self.assertNotIn("{{REL_RELEASE_OTA_SLEEP_ARCHIVE}}", required)

    def test_debug_only_does_not_require_release_artifacts(self):
        code, publish = self.run_pipeline(result=lark.ReleaseDocResult(), dry_run=True,
                                          dsl=True, only="debug", skip_share=False)
        self.assertEqual(code, 0)
        self.assertNotIn("{{REL_RELEASE_OTA_CLOUD_ARCHIVE}}", publish.call_args[1]["required_placeholders"])


if __name__ == "__main__":
    unittest.main()
