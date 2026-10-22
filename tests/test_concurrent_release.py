"""Offline replay of the Windermere/Cologne concurrent release failures."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import changelog_state as state
import jenkins_trigger_build as jenkins
import lark_cli_adapter as lark
import release_pipeline_run as pipeline


class OfflineTest(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        for name in ("urllib.request.urlopen", "urllib.request.OpenerDirector.open", "subprocess.Popen"):
            self.stack.enter_context(mock.patch(name, side_effect=AssertionError("External action: " + name)))
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))

    def patch(self, *args, **kwargs):
        return self.stack.enter_context(mock.patch.object(*args, **kwargs))


class CliRecoveryTests(OfflineTest):
    def setUp(self):
        super().setUp()
        self.binary = self.root / "lark-cli.cmd"
        self.binary.write_text("offline stub")
        self.patch(lark, "_LARK", str(self.binary))
        self.patch(lark, "_auth_state", {})
        self.stack.enter_context(mock.patch("time.sleep"))

    def test_binary_presence_does_not_launch_node_or_probe_version(self):
        run = self.patch(lark, "_run", side_effect=lark.LarkCliError("startup timeout"))
        self.assertTrue(lark._check_bin())
        run.assert_not_called()

    def test_missing_binary_is_rechecked_instead_of_cached_forever(self):
        self.patch(lark, "_LARK", str(self.root / "later.cmd"))
        self.patch(lark.shutil, "which", return_value=None)
        self.assertFalse(lark._check_bin())
        (self.root / "later.cmd").write_text("installed")
        self.assertTrue(lark._check_bin())

    def test_auth_startup_timeout_recovers_using_same_user_identity(self):
        run = self.patch(lark, "_run", side_effect=[lark.LarkCliError("超时 (30s)"), mock.Mock()])
        self.assertTrue(lark.lark_auth_ensure(domain="wiki", force=True))
        self.assertEqual([c.args[0] for c in run.call_args_list], [["auth", "status"]] * 2)

    def test_failed_auth_preserves_actual_error_and_can_be_forced_to_retry(self):
        run = self.patch(lark, "_run", side_effect=lark.LarkCliError("startup timeout"))
        ok, reason = lark.lark_auth_check(domain="wiki")
        self.assertFalse(ok)
        self.assertIn("startup timeout", reason)
        self.assertNotIn("未安装", reason)
        self.assertIn(mock.call(["auth", "login", "--domain", "wiki"], timeout_sec=30), run.call_args_list)
        self.assertFalse(lark.lark_auth_ensure(domain="wiki"))
        run.side_effect = None
        self.assertTrue(lark.lark_auth_ensure(domain="wiki", force=True))

    def test_process_launch_error_keeps_cause_for_auth_retry(self):
        self.patch(lark.subprocess, "run", side_effect=PermissionError("offline launch failure"))
        with self.assertRaisesRegex(lark.LarkCliError, "启动失败.*offline launch failure"):
            lark._run(["auth", "status"])

    def test_failed_precheck_still_attempts_real_wiki_operation_as_user(self):
        self.patch(lark, "lark_auth_ensure", return_value=False)
        run = self.patch(lark, "_run", return_value=mock.Mock(stdout=json.dumps({
            "data": {"node": {"node_token": "new", "obj_token": "doc"}}})))
        tenant = self.patch(lark, "_get_tenant_token")
        result = lark.lark_wiki_copy_node(source_node_token="template", target_space_id="123")
        self.assertEqual(result["docx_token"], "doc")
        self.assertEqual(run.call_args.args[0][:5], ["wiki", "nodes", "copy", "--as", "user"])
        tenant.assert_not_called()

    def test_actual_missing_binary_and_denied_bot_still_fail(self):
        self.patch(lark, "lark_auth_ensure", return_value=False)
        self.patch(lark, "_check_bin", return_value=False)
        self.patch(lark, "_get_tenant_token", return_value="offline")
        self.patch(lark, "_http_post_json", return_value={"code": 131006, "msg": "permission denied"})
        with self.assertRaisesRegex(lark.LarkCliError, "131006"):
            lark.lark_wiki_copy_node(source_node_token="template", target_space_id="123")


class ChangelogStateTests(OfflineTest):
    VERSION = "6.5.15.1"

    def record(self, project, number, **extra):
        return dict(project=project, version=self.VERSION, build_number=number,
                    job_url="https://offline.invalid/job/diff/", **extra)

    def test_interleaved_projects_keep_their_own_builds(self):
        barrier = threading.Barrier(2)
        records = [self.record("windermere", 4758), self.record("cologne", 4760)]
        results, errors = {}, []
        def work(record):
            try:
                state.save_state(record, self.root)
                barrier.wait(timeout=5)
                results[record["project"]] = state.load_state(record["project"], self.VERSION, self.root)
            except BaseException as e:
                errors.append(e)
        threads = [threading.Thread(target=work, args=(r,)) for r in records]
        for t in threads: t.start()
        for t in threads: t.join(timeout=10)
        self.assertFalse(errors)
        self.assertEqual(results, {r["project"]: r for r in records})

    def test_other_version_does_not_reuse_current_build(self):
        state.save_state(self.record("windermere", 4758), self.root)
        self.assertEqual(state.load_state("windermere", "6.5.16.1", self.root), {})

    def test_legacy_file_reused_only_for_exact_project_and_version(self):
        record = self.record("cologne", 4760)
        (self.root / "changelog_job.json").write_text(json.dumps(record))
        self.assertEqual(state.load_state("cologne", self.VERSION, self.root), record)
        self.assertEqual(state.load_state("windermere", self.VERSION, self.root), {})
        self.assertEqual(state.load_state("cologne", "6.5.16.1", self.root), {})

    def test_legacy_identity_without_version_is_rejected(self):
        record = self.record("windermere", 4758)
        record.pop("version")
        (self.root / "changelog_job.json").write_text(json.dumps(record))
        self.assertEqual(state.load_state("windermere", self.VERSION, self.root), {})

    def test_foreign_legacy_state_and_owned_config_do_not_form_hybrid(self):
        (self.root / "changelog_job.json").write_text(json.dumps(self.record("cologne", 4760)))
        own = self.record("windermere", 4758)
        self.assertEqual(state.select_state("windermere", self.VERSION, own, self.root), own)

    def test_untagged_config_cannot_override_saved_build(self):
        own = self.record("windermere", 4758)
        state.save_state(own, self.root)
        self.assertEqual(state.select_state("windermere", self.VERSION,
                         {"build_number": 4760, "link": "wrong"}, self.root), own)

    def test_saved_progress_wins_over_same_build_queued_config(self):
        own = self.record("windermere", 4758, status="finished", link="correct")
        state.save_state(own, self.root)
        queued = self.record("windermere", 4758, status="queued", link="")
        self.assertEqual(state.select_state("windermere", self.VERSION, queued, self.root), own)

    def test_new_build_does_not_inherit_old_link(self):
        state.save_state(self.record("windermere", 4758, link="old", result="FAILURE"), self.root)
        self.patch(pipeline, "load_changelog_state", side_effect=lambda p, v: state.load_state(p, v, self.root))
        self.patch(pipeline, "save_changelog_state", side_effect=lambda s: state.save_state(s, self.root))
        updated = pipeline._update_changelog_state(project="windermere", version=self.VERSION,
                                                   build_number=4761, status="running")
        self.assertNotIn("link", updated)
        self.assertNotIn("result", updated)

    def test_sanitized_names_cannot_collide_or_escape_directory(self):
        paths = [state.state_path(p, self.VERSION, self.root) for p in ("a/b", "a_b", "../../evil")]
        self.assertEqual(len(set(paths)), 3)
        for p in paths: self.assertEqual(p.parent, self.root / "changelog")

    def test_atomic_update_failure_keeps_last_good_file(self):
        old = self.record("windermere", 4758)
        state.save_state(old, self.root)
        self.patch(state.os, "replace", side_effect=OSError("offline simulated write failure"))
        with self.assertRaises(OSError):
            state.save_state(self.record("windermere", 4761), self.root)
        self.assertEqual(state.load_state("windermere", self.VERSION, self.root), old)
        self.assertEqual(list((self.root / "changelog").glob("*.tmp")), [])


class ChangelogIntegrationTests(OfflineTest):
    VERSION = "6.5.15.1"

    def record(self, project, number, **extra):
        return dict(project=project, version=self.VERSION, build_number=number,
                    job_url="https://offline.invalid/job/diff/", **extra)

    def setUp(self):
        super().setUp()
        self.patch(jenkins, "save_changelog_state", side_effect=lambda s: state.save_state(s, self.root))
        self.patch(pipeline, "load_changelog_state", side_effect=lambda p, v: state.load_state(p, v, self.root))
        self.patch(pipeline, "save_changelog_state", side_effect=lambda s: state.save_state(s, self.root))
        self.patch(pipeline, "select_changelog_state", side_effect=lambda p, v, c: state.select_state(p, v, c, self.root))
        self.patch(jenkins, "_atomic_write_json")
        self.patch(pipeline, "_resolve_jenkins_auth", return_value={"username": "offline", "password": "offline"})
        self.stack.enter_context(mock.patch("time.sleep"))

    def config(self, project):
        return {"release": {"project": project, "version": self.VERSION},
                "jenkins": {"base_url": "https://offline.invalid"},
                "changelog": {"enabled": True, "jenkins_job_url": "https://offline.invalid/job/diff/",
                              "params": {"PRODUCTION": "${release.project}"}, "link": "stale"}}

    def opener(self, console=None, result="SUCCESS"):
        op = mock.Mock()
        urls = []
        def response(req, **kwargs):
            url = req.full_url
            urls.append(url)
            if url.endswith("/consoleText"):
                number = url.rstrip("/").split("/")[-2]
                body = console if console is not None else "差分报告飞书链接: https://zepp.feishu.cn/docx/" + number
            elif url.endswith("/api/json"):
                body = json.dumps({"building": result is None, "result": result})
            else:
                raise AssertionError("Unexpected URL: " + url)
            res = mock.MagicMock()
            res.__enter__.return_value.read.return_value = body.encode()
            return res
        op.open.side_effect = response
        self.stack.enter_context(mock.patch("urllib.request.build_opener", return_value=op))
        return urls

    def test_both_projects_reuse_their_original_build_without_retrigger(self):
        self.patch(jenkins, "_trigger_build", side_effect=["https://offline.invalid/queue/1/", "https://offline.invalid/queue/2/"])
        self.patch(jenkins, "_poll_queue_for_build", side_effect=[(4758, "https://offline.invalid/job/diff/4758/"),
                                                               (4760, "https://offline.invalid/job/diff/4760/")])
        configs = [self.config("windermere"), self.config("cologne")]
        for cfg in configs:
            jenkins._maybe_trigger_changelog_async(cfg_path=self.root / (cfg["release"]["project"] + ".json"),
                cfg=cfg, auth=jenkins.JenkinsAuth("offline", "offline"), crumb=None,
                request_timeout_sec=5, opener=None)
            self.assertNotIn("link", cfg["changelog"])
            self.assertEqual(cfg["changelog"]["project"], cfg["release"]["project"])
        # Reproduce the legacy global file being overwritten by the other task.
        (self.root / "changelog_job.json").write_text(json.dumps(self.record("cologne", 4760)))
        urls = self.opener()
        retrigger = self.patch(pipeline, "_trigger_changelog_job_and_get_feishu_link")
        self.assertEqual([pipeline._build_changelog_link(cfg=c) for c in configs],
                         ["https://zepp.feishu.cn/docx/4758", "https://zepp.feishu.cn/docx/4760"])
        retrigger.assert_not_called()
        self.assertIn("https://offline.invalid/job/diff/4758/consoleText", urls)
        self.assertIn("https://offline.invalid/job/diff/4760/consoleText", urls)
        for p, n in (("windermere", 4758), ("cologne", 4760)):
            self.assertEqual(state.load_state(p, self.VERSION, self.root)["build_number"], n)

    def test_failed_existing_build_without_report_is_not_triggered_again(self):
        state.save_state(self.record("windermere", 4758, status="queued"), self.root)
        self.opener(console="Finished: FAILURE", result="FAILURE")
        retrigger = self.patch(pipeline, "_trigger_changelog_job_and_get_feishu_link")
        self.assertEqual(pipeline._build_changelog_link(cfg=self.config("windermere")), "")
        self.assertEqual(pipeline._build_changelog_link(cfg=self.config("windermere")), "")
        retrigger.assert_not_called()
        self.assertEqual(state.load_state("windermere", self.VERSION, self.root)["result"], "FAILURE")

    def test_failed_existing_build_still_returns_its_report(self):
        retrigger = self.patch(pipeline, "_trigger_changelog_job_and_get_feishu_link")
        for saved_result in ("", "FAILURE"):
            with self.subTest(saved_result=saved_result):
                state.save_state(self.record("windermere", 4758, result=saved_result), self.root)
                self.opener(result="FAILURE")
                self.assertEqual(pipeline._build_changelog_link(cfg=self.config("windermere")),
                                 "https://zepp.feishu.cn/docx/4758")
                saved = state.load_state("windermere", self.VERSION, self.root)
                self.assertEqual(saved["result"], "FAILURE")
                self.assertEqual(saved["link"], "https://zepp.feishu.cn/docx/4758")
        retrigger.assert_not_called()

    def test_newly_triggered_failed_build_still_returns_its_report(self):
        self.patch(jenkins, "_try_get_crumb", return_value=None)
        self.patch(jenkins, "_trigger_build", return_value="https://offline.invalid/queue/123/")
        self.patch(jenkins, "_poll_queue_for_build", return_value=(4758, "https://offline.invalid/job/diff/4758/"))
        self.opener(result="FAILURE")
        link = pipeline._trigger_changelog_job_and_get_feishu_link(
            job_url="https://offline.invalid/job/diff/", params={"PRODUCTION": "windermere"},
            j_auth={"username": "offline", "password": "offline"}, project="windermere", version=self.VERSION)
        self.assertEqual(link, "https://zepp.feishu.cn/docx/4758")
        saved = state.load_state("windermere", self.VERSION, self.root)
        self.assertEqual(saved["result"], "FAILURE")
        self.assertEqual(saved["link"], link)

    def test_existing_pending_build_without_report_is_not_triggered_again(self):
        state.save_state(self.record("windermere", 4758, status="queued"), self.root)
        cfg = self.config("windermere")
        cfg["changelog"]["timeout_sec"] = 1
        self.opener(console="Still running", result=None)
        self.patch(pipeline.time, "time", side_effect=range(100))
        retrigger = self.patch(pipeline, "_trigger_changelog_job_and_get_feishu_link")
        self.assertEqual(pipeline._build_changelog_link(cfg=cfg), "")
        retrigger.assert_not_called()
        self.assertEqual(state.load_state("windermere", self.VERSION, self.root)["status"], "running")

    def test_cached_report_survives_later_jenkins_outage(self):
        state.save_state(self.record("windermere", 4758, link="https://zepp.feishu.cn/docx/cached"), self.root)
        self.assertEqual(pipeline._build_changelog_link(cfg=self.config("windermere")),
                         "https://zepp.feishu.cn/docx/cached")

    def test_fallback_uses_own_queue_instead_of_shared_last_build(self):
        self.patch(jenkins, "_try_get_crumb", return_value=None)
        self.patch(jenkins, "_trigger_build", return_value="https://offline.invalid/queue/123/")
        poll = self.patch(jenkins, "_poll_queue_for_build", return_value=(4758, "https://offline.invalid/job/diff/4758/"))
        urls = self.opener()
        link = pipeline._trigger_changelog_job_and_get_feishu_link(
            job_url="https://offline.invalid/job/diff/", params={"PRODUCTION": "windermere"},
            j_auth={"username": "offline", "password": "offline"}, project="windermere", version=self.VERSION)
        self.assertEqual(link, "https://zepp.feishu.cn/docx/4758")
        self.assertEqual(poll.call_args.kwargs["queue_url"], "https://offline.invalid/queue/123/")
        self.assertNotIn("https://offline.invalid/job/diff/api/json", urls)

    def test_missing_queue_does_not_guess_other_projects_latest_build(self):
        self.patch(jenkins, "_try_get_crumb", return_value=None)
        self.patch(jenkins, "_trigger_build", side_effect=RuntimeError("No Location queue header"))
        poll = self.patch(jenkins, "_poll_queue_for_build")
        link = pipeline._trigger_changelog_job_and_get_feishu_link(
            job_url="https://offline.invalid/job/diff/", params={}, j_auth={},
            project="windermere", version=self.VERSION)
        self.assertEqual(link, "")
        poll.assert_not_called()


if __name__ == "__main__":
    unittest.main()
