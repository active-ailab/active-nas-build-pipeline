"""API failures must reach the UI as JSON without starting a build."""
import contextlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import web_release_server as web
from config_loader import read_json_with_base


ROOT = Path(__file__).resolve().parents[1]


class ApiErrorTests(unittest.TestCase):
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

    def test_invalid_current_config_reports_filename_and_position_without_writing(self):
        target = self.temp / "gqf_fuji.json"
        content = b'{\n  "platform_select": "apollo5",\n}'
        target.write_bytes(content)
        response = self.submit()
        self.assertEqual(response.status_code, 400)
        self.assertIn("gqf_fuji.json", response.get_json()["error"])
        self.assertIn("3", response.get_json()["error"])
        self.assertEqual(target.read_bytes(), content)
        self.worker.assert_not_called()

    def test_invalid_backup_reports_the_backup_filename(self):
        backup = self.temp / "gqf_fuji.json.bak.20261106_100000"
        backup.write_text("{bad json", encoding="utf-8")
        before = (self.temp / "gqf_fuji.json").read_bytes()
        response = self.submit()
        self.assertEqual(response.status_code, 400)
        self.assertIn(backup.name, response.get_json()["error"])
        self.assertEqual((self.temp / "gqf_fuji.json").read_bytes(), before)
        self.worker.assert_not_called()

    def test_unexpected_failure_is_logged_and_returned_as_json(self):
        with mock.patch.object(web, "_find_latest_bak", side_effect=RuntimeError("private details")):
            response = self.submit()
        self.assertEqual(response.status_code, 500)
        self.assertFalse(response.get_json()["ok"])
        self.assertIn("RuntimeError", response.get_json()["error"])
        self.assertNotIn("private details", response.get_json()["error"])
        self.logger.assert_called_once()
        self.worker.assert_not_called()

    def test_invalid_sections_return_json_without_starting_task(self):
        for section in ("release", "vars", "feishu"):
            with self.subTest(section=section):
                payload = {"release": {"version": "1.2.3.1"}, section: None}
                response = self.client.post("/api/projects/fuji/release", json=payload)
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.get_json()["ok"])
                self.worker.assert_not_called()

    def test_api_http_error_is_json_while_normal_page_error_remains_html(self):
        response = self.client.get("/api/no-such-route")
        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.get_json()["ok"])
        self.assertEqual(self.client.get("/no-such-page").mimetype, "text/html")

    def test_utf8_bom_config_is_supported(self):
        target = self.temp / "bom.json"
        target.write_text(json.dumps({"vars": {}}), encoding="utf-8-sig")
        self.assertEqual(web._read_project_json(target), {"vars": {}})

    def test_fuji_with_bom_can_be_saved_and_loaded_by_pipeline(self):
        target = self.temp / "gqf_fuji.json"
        target.write_bytes(b"\xef\xbb\xbf" + target.read_bytes())
        self.assertEqual(read_json_with_base(target)["platform_select"], "apollo5")
        response = self.submit()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        self.assertFalse(target.read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertEqual(read_json_with_base(target)["platform_select"], "apollo5")
        self.worker.return_value.start.assert_called_once()

    def test_bom_in_inherited_common_config_is_supported(self):
        (self.temp / "common.json").write_bytes(b'\xef\xbb\xbf{"vars":{"tag":"offline"}}')
        target = self.temp / "child.json"
        target.write_bytes(b'\xef\xbb\xbf{"base_config":"common.json","release":{"project":"fuji"}}')
        self.assertEqual(read_json_with_base(target)["vars"]["tag"], "offline")
