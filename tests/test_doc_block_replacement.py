"""Offline coverage for rich-text placeholder fallback."""
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import lark_cli_adapter as lark


class BlockReplacementTests(unittest.TestCase):
    @staticmethod
    def updated_elements(call):
        args = call.args[0]
        return json.loads(args[args.index("--data") + 1])["update_text_elements"]["elements"]

    def test_split_repeated_placeholders_preserve_heading_styles_and_mentions(self):
        mention = {"mention_user": {"user_id": "offline"}}
        blocks = [{"block_id": "heading", "heading1": {"elements": [
            {"text_run": {"content": "设备 {{REL_", "text_element_style": {"bold": True}}},
            {"text_run": {"content": "DEVICE_NAME}} / {{REL_DEVICE_NAME}}!"}},
            mention,
            {"text_run": {"content": " {{REL_BRANCH}} {{EMPTY}}"}},
        ]}}]
        with patch.object(lark, "_document_blocks", return_value=blocks), patch.object(
            lark, "_run", return_value=SimpleNamespace(stdout='{"code":0}')
        ) as run:
            lark._replace_block_placeholders("offline", {
                "{{REL_DEVICE_NAME}}": "A&B", "{{REL_BRANCH}}": "main", "{{EMPTY}}": ""})
        args = run.call_args[0][0]
        elements = json.loads(args[args.index("--data") + 1])["update_text_elements"]["elements"]
        self.assertEqual("".join(e.get("text_run", {}).get("content", "") for e in elements),
                         "设备 A&B / A&B! main {{EMPTY}}")
        self.assertTrue(elements[0]["text_run"]["text_element_style"]["bold"])
        self.assertIn(mention, elements)

    def test_no_match_does_not_write(self):
        with patch.object(lark, "_document_blocks", return_value=[
            {"block_id": "p", "text": {"elements": [{"text_run": {"content": "done"}}]}}
        ]), patch.object(lark, "_run") as run:
            lark._replace_block_placeholders("offline", {"{{A}}": "a"})
        run.assert_not_called()

    def test_api_failure_propagates(self):
        with patch.object(lark, "_document_blocks", return_value=[
            {"block_id": "p", "text": {"elements": [{"text_run": {"content": "{{A}}"}}]}}
        ]), patch.object(lark, "_run", return_value=SimpleNamespace(stdout='{"code":999}')):
            with self.assertRaises(lark.LarkCliError):
                lark._replace_block_placeholders("offline", {"{{A}}": "a"})

    def test_links_in_heading_and_table_paragraph_preserve_surrounding_text_and_style(self):
        mention = {"mention_user": {"user_id": "offline"}}
        blocks = [{"block_id": "heading", "heading2": {"elements": [
            {"text_run": {"content": "下载 firmware.bin（Release）", "text_element_style": {"bold": True}}},
            mention,
        ]}}, {"block_id": "table-paragraph", "text": {"elements": [
            {"text_run": {"content": "manifest.xml / firmware.bin"}},
        ]}}]
        with patch.object(lark, "_document_blocks", return_value=blocks), patch.object(
            lark, "_run", return_value=SimpleNamespace(stdout='{"code":0}')
        ) as run:
            lark._add_links("offline", [("firmware.bin", "https://nas.invalid/fw"),
                                        ("manifest.xml", "https://nas.invalid/manifest")])
        self.assertEqual(run.call_count, 2)
        elements = self.updated_elements(run.call_args_list[0])
        self.assertEqual("".join(e.get("text_run", {}).get("content", "") for e in elements),
                         "下载 firmware.bin（Release）")
        linked = [e["text_run"] for e in elements if "link" in e.get("text_run", {}).get("text_element_style", {})]
        self.assertEqual([e["content"] for e in linked], ["firmware.bin"])
        self.assertEqual(linked[0]["text_element_style"], {"bold": True, "link": {"url": "https://nas.invalid/fw"}})
        self.assertIn(mention, elements)
        self.assertEqual(len(self.updated_elements(run.call_args_list[1])), 3)

    def test_existing_links_are_preserved(self):
        elements = [{"text_run": {"content": "firmware.bin", "text_element_style": {
            "link": {"url": "https://existing.invalid"}}}}]
        with patch.object(lark, "_document_blocks", return_value=[{"block_id": "p", "text": {"elements": elements}}]), patch.object(lark, "_run") as run:
            lark._add_links("offline", [("firmware.bin", "https://nas.invalid/fw")])
        run.assert_not_called()

    def test_failed_link_patch_is_reported(self):
        with patch.object(lark, "_document_blocks", return_value=[{"block_id": "p", "text": {
            "elements": [{"text_run": {"content": "firmware.bin"}}]}}]), patch.object(
                lark, "_run", return_value=SimpleNamespace(stdout='{"code":999}')
        ):
            with self.assertRaises(lark.LarkCliError):
                lark._add_links("offline", [("firmware.bin", "https://nas.invalid/fw")])

    def test_cli_failure_falls_back_and_xml_characters_are_escaped(self):
        content = []
        def update(args, **kwargs):
            content.append(Path(args[args.index("--content") + 1][1:]).read_text(encoding="utf-8"))
            raise lark.LarkCliError("split placeholder")
        with patch.object(lark, "_run", side_effect=update), patch.object(
                lark, "_replace_block_placeholders") as fallback, patch.object(
                lark, "_verify_replacement") as verify:
            mapping = {"{{DEVICE}}": "A&B <test>"}
            lark._replace_placeholders_via_lark("offline", mapping)
        self.assertEqual(content, ["A&amp;B &lt;test&gt;"])
        fallback.assert_called_once_with("offline", mapping)
        verify.assert_called_once()

    def test_placeholder_does_not_match_across_a_mention(self):
        with patch.object(lark, "_document_blocks", return_value=[{"block_id": "p", "text": {"elements": [
            {"text_run": {"content": "{{DEV"}}, {"mention_user": {"user_id": "offline"}},
            {"text_run": {"content": "ICE}}"}},
        ]}}]), patch.object(lark, "_run") as run:
            lark._replace_block_placeholders("offline", {"{{DEVICE}}": "fuji"})
        run.assert_not_called()

    def test_document_blocks_reads_all_pages(self):
        pages = [{"code": 0, "data": {"items": [{"block_id": "one"}], "page_token": "next", "has_more": True}},
                 {"code": 0, "data": {"items": [{"block_id": "two"}], "has_more": False}}]
        with patch.object(lark, "_run", side_effect=[SimpleNamespace(stdout=json.dumps(p)) for p in pages]) as run:
            self.assertEqual(lark._document_blocks("offline"), [{"block_id": "one"}, {"block_id": "two"}])
        self.assertEqual(run.call_count, 2)
        args = run.call_args.args[0]
        self.assertEqual(json.loads(args[args.index("--params") + 1])["page_token"], "next")

    def test_cli_api_error_with_zero_process_exit_is_not_success(self):
        response = SimpleNamespace(returncode=0, stdout='{"code":999,"msg":"denied"}', stderr="")
        with patch.object(lark.subprocess, "run", return_value=response):
            with self.assertRaises(lark.LarkCliError):
                lark._run(["api", "GET", "/offline"])
