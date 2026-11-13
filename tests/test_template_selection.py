"""Template selection must override stale project and backup settings."""
import contextlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import web_release_server as web
from config_loader import read_json_with_base


ROOT = Path(__file__).resolve().parents[1]


class TemplateSelectionTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.temp = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=str(ROOT / "output"))))
        for name in ("common.json", "gqf_fuji.json"):
            (self.temp / name).write_bytes((ROOT / name).read_bytes())
        for name, value in (("SCRIPT_DIR", self.temp), ("_running_tasks", {}), ("_task_streams", {})):
            self.stack.enter_context(mock.patch.object(web, name, value))
        self.logger = self.stack.enter_context(mock.patch.object(web.app.logger, "exception"))
        self.worker = self.stack.enter_context(mock.patch.object(web.threading, "Thread"))
        self.stack.enter_context(mock.patch.object(web, "_inject_user_feishu_token"))
        self.stack.enter_context(mock.patch.object(web, "_persist_running_tasks"))
        self.client = web.app.test_client()

    def submit(self):
        return self.client.post("/api/projects/fuji/release", json={"release": {"version": "1.2.3.1"}})

    def test_selected_template_overrides_old_config_and_backup(self):
        target = self.temp / "gqf_fuji.json"
        cfg = json.loads(target.read_text(encoding="utf-8"))
        old_token = "RzHFw5ipYiU5ibkMxSfcgUfDnve"
        selected_token = "NYbnwgJsFi3xqUktEcbcD4aPnDb"
        cfg["feishu"]["template_node_token"] = old_token
        target.write_text(json.dumps(cfg), encoding="utf-8")
        (self.temp / "gqf_fuji.json.bak.20261106_100000").write_text(
            json.dumps(cfg), encoding="utf-8")
        for is_new in (False, True):
            with self.subTest(is_new=is_new):
                response = self.client.post("/api/projects/fuji/release", json={
                    "release": {"version": "1.2.3.1"}, "is_new": is_new,
                    "feishu": {"template_node_token": selected_token}})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(read_json_with_base(target)["feishu"]["template_node_token"],
                                 selected_token)
                web._running_tasks.clear()

    def test_missing_template_preserves_existing_selection(self):
        target = self.temp / "gqf_fuji.json"
        expected = read_json_with_base(target)["feishu"]["template_node_token"]
        self.assertEqual(self.submit().status_code, 200)
        self.assertEqual(read_json_with_base(target)["feishu"]["template_node_token"], expected)
