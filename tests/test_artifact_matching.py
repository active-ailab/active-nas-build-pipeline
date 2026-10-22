"""Check artifact identity, rather than only whether placeholders were filled."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from artifact_matching import ArtifactSelectionError, select_artifact
import release_pipeline_run as pipeline


ROOT = Path(__file__).resolve().parents[1]
FULL = "archive_cologne_6.4.14.3_202609182037.tgz"
RELEASE = {
    "{{REL_RELEASE_FULL_ARCHIVE}}": FULL,
    "{{REL_RELEASE_OTA_CLOUD_ARCHIVE}}": "archive_OTA_CLOUD_cologne_6.4.14.3_202609182037.tgz",
    "{{REL_RELEASE_OTA_SLEEP_ARCHIVE}}": "archive_OTA_SLEEP_cologne_6.4.14.3_202609182037.tgz",
    "{{REL_RELEASE_OTA_SIGN_ZIP}}": "watch@mhs003_ota_sign.zip",
    "{{REL_RELEASE_FACTORY_TOOL_ZIP}}": "cologne_factory_6.4.14.3_6.4.14_2026-09-18.zip",
    "{{REL_RELEASE_SENSORHUB_OTA_ZIP}}": "sensorhub_ota_cologne_6.4.14.3.zip",
    "{{REL_RELEASE_SENSORHUB_FCT_ZIP}}": "sensorhub_fct_cologne_6.4.14.3.zip",
    "{{REL_APP_MANIFEST_INFO}}": "manifest_cologne_6.4.14.3_202609182037.xml",
    "{{REL_BOOTLOADER_MANIFEST_INFO}}": "manifest_BOOT_cologne_1.4.2_202609182037.xml",
    "{{REL_RECOVERY_MANIFEST_INFO}}": "manifest_RECOVERY_cologne_1.4.1_202609182037.xml",
    "{{REL_MD5SUM_FILE}}": "cologne_6.4.14.3_md5.txt",
    "{{REL_RELEASE_MD5}}": "cologne_6.4.14.3_md5.txt",
    "{{REL_BUILD_LOG_FILE}}": "jenkins_release_console.log",
}
DEBUG = {
    "{{REL_MONKEY_FULL_ARCHIVE}}": "archive_cologne_6.4.14.2_202609182037.tgz",
    "{{REL_MONKEY_SENSORHUB_OTA_ZIP}}": "sensorhub_ota_cologne_6.4.14.2.zip",
    "{{REL_MONKEY_SENSORHUB_FCT_ZIP}}": "sensorhub_fct_cologne_6.4.14.2.zip",
}
TSCAN = {"{{REL_TSCAN_BOOT}}": "BOOT_tscanResult.zip", "{{REL_TSCAN_OTA}}": "OTA_tscanResult.zip",
         "{{REL_TSCAN_RECOVERY}}": "RECOVERY_tscanResult.zip"}


class ArtifactMappingTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        for name in ("urllib.request.urlopen", "urllib.request.OpenerDirector.open", "subprocess.Popen"):
            self.stack.enter_context(mock.patch(name, side_effect=AssertionError("External action: " + name)))
        self.cfg = {"release": {"project": "cologne", "device_name": "cologne", "version": "6.4.14.3"},
                    "vars": {"build_release_mode": "single", "debug_version_name": "6.4.14"},
                    "changelog": {"enabled": False}, "feishu": {}, "nas": {}}
        self.dsl = json.loads((ROOT / "placeholders.default.json").read_text(encoding="utf-8"))
        self.release_files = list(dict.fromkeys(RELEASE.values()))
        self.debug_files = list(DEBUG.values())

    @staticmethod
    def url(filename):
        return "https://nas.invalid/sharing/" + filename

    def summaries(self, files=None):
        records = []
        for run, names in (("release", files if files is not None else self.release_files),
                           ("debug", self.debug_files), ("tscan", list(TSCAN.values()))):
            records.append({"name": run, "share_links_source": "nas", "remote_dir": "/offline/" + run,
                            "share_links_main": {f: self.url(f) for f in names}})
        return records

    def mapping(self, files=None, legacy=False, dsl=None):
        if legacy:
            return pipeline._build_placeholder_replacements(cfg=self.cfg, run_summaries=self.summaries(files))
        return pipeline._build_placeholder_replacements_from_dsl(cfg=self.cfg, run_summaries=self.summaries(files),
            dsl_cfg=pipeline._expand_placeholders(dsl or self.dsl, self.cfg), dsl_runtime={"vars": {}})

    def assert_link(self, mapping, placeholder, filename):
        self.assertEqual(mapping[placeholder], filename + "|" + self.url(filename), placeholder)

    def test_reported_full_archive_is_never_the_shorter_sensorhub_or_factory_zip(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                self.assert_link(self.mapping(legacy=legacy), "{{REL_RELEASE_FULL_ARCHIVE}}", FULL)

    def test_all_nas_file_fields_bind_the_expected_artifact_and_filename(self):
        mapping = self.mapping()
        expected = {**RELEASE, **DEBUG, **TSCAN}
        for placeholder, filename in expected.items():
            with self.subTest(placeholder=placeholder): self.assert_link(mapping, placeholder, filename)
        for placeholder, rule in self.dsl["placeholders"]["mappings"].items():
            if rule.get("mode") == "filename_for_share":
                self.assertEqual(mapping[placeholder], expected[rule["share_placeholder"]], placeholder)

    def test_other_projects_and_other_firmware_versions_cannot_steal_a_slot(self):
        files = list(self.release_files)
        for name in list(files):
            if "cologne" in name: files.append(name.replace("cologne", "cologne_64m"))
            if "6.4.14.3" in name:
                files.extend([name.replace("6.4.14.3", "6.4.14.1"), name.replace("6.4.14.3", "6.4.14.30")])
        files.extend(["md5.txt", "cologne_md5.txt", "sensorhub_ota_sleep_cologne_6.4.14.3.zip"])
        mapping = self.mapping(files)
        for placeholder, filename in RELEASE.items(): self.assert_link(mapping, placeholder, filename)

    def test_legacy_fields_obey_the_same_project_and_version_guards(self):
        files = self.release_files + [f.replace("6.4.14.3", "6.4.14.1") for f in self.release_files if "6.4.14.3" in f]
        mapping = self.mapping(files, legacy=True)
        for placeholder, filename in RELEASE.items():
            if placeholder in mapping: self.assert_link(mapping, placeholder, filename)

    def test_missing_full_archive_does_not_fall_back_to_a_different_package_type(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                mapping = self.mapping([f for f in self.release_files if f != FULL], legacy=legacy)
                self.assertEqual(mapping["{{REL_RELEASE_FULL_ARCHIVE}}"], "")

    def test_cloud_sleep_and_sign_packages_have_no_sensorhub_zip_fallback(self):
        missing = ["{{REL_RELEASE_OTA_CLOUD_ARCHIVE}}", "{{REL_RELEASE_OTA_SLEEP_ARCHIVE}}", "{{REL_RELEASE_OTA_SIGN_ZIP}}"]
        files = [f for f in self.release_files if f not in {RELEASE[p] for p in missing}]
        files.extend(["sensorhub_ota_sleep_cologne_6.4.14.3.zip", "sensorhub_ota_sign.zip"])
        for legacy in (False, True):
            mapping = self.mapping(files, legacy=legacy)
            for placeholder in missing: self.assertEqual(mapping[placeholder], "", placeholder)

    def test_app_manifest_cannot_be_replaced_with_boot_or_recovery_manifest(self):
        files = [f for f in self.release_files if f != RELEASE["{{REL_APP_MANIFEST_INFO}}"]]
        for legacy in (False, True):
            mapping = self.mapping(files, legacy=legacy)
            self.assertEqual(mapping["{{REL_APP_MANIFEST_INFO}}"], "")
            self.assert_link(mapping, "{{REL_BOOTLOADER_MANIFEST_INFO}}", RELEASE["{{REL_BOOTLOADER_MANIFEST_INFO}}"])

    def test_two_matching_full_archive_builds_raise_instead_of_picking_shortest_name(self):
        files = self.release_files + [FULL.replace("202609182037", "202609182038")]
        for legacy in (False, True):
            with self.subTest(legacy=legacy), self.assertRaisesRegex(ArtifactSelectionError, "REL_RELEASE_FULL_ARCHIVE"):
                self.mapping(files, legacy=legacy)

    def test_two_independent_boot_manifest_versions_require_explicit_resolution(self):
        files = self.release_files + [RELEASE["{{REL_BOOTLOADER_MANIFEST_INFO}}"].replace("1.4.2", "1.4.3")]
        with self.assertRaisesRegex(ArtifactSelectionError, "REL_BOOTLOADER_MANIFEST_INFO"):
            self.mapping(files)

    def test_old_custom_broad_rule_cannot_bypass_artifact_type_guard(self):
        dsl = copy.deepcopy(self.dsl)
        dsl["placeholders"]["mappings"]["{{REL_RELEASE_FULL_ARCHIVE}}"]["match"] = {"regex": r".*\.(zip|tgz)$"}
        self.assert_link(self.mapping(dsl=dsl), "{{REL_RELEASE_FULL_ARCHIVE}}", FULL)

    def test_single_build_debug_package_uses_the_paired_version(self):
        self.debug_files += [f.replace("6.4.14.2", "6.4.14.0") for f in DEBUG.values()]
        for placeholder, filename in DEBUG.items(): self.assert_link(self.mapping(), placeholder, filename)

    def test_split_debug_package_uses_the_configured_debug_version_prefix(self):
        self.cfg["vars"].update(build_release_mode="split", debug_version_name="6.4.15")
        self.debug_files = [f.replace("6.4.14.2", "6.4.15.0") for f in DEBUG.values()] + list(DEBUG.values())
        mapping = self.mapping()
        for placeholder, filename in DEBUG.items():
            self.assert_link(mapping, placeholder, filename.replace("6.4.14.2", "6.4.15.0"))

    def test_repeated_references_to_same_remote_file_are_not_ambiguous(self):
        chosen = select_artifact([(FULL, "first"), (FULL, "second")], cfg=self.cfg,
                                  placeholder="{{REL_RELEASE_FULL_ARCHIVE}}", run="release")
        self.assertEqual(chosen, (FULL, "first"))

    def test_same_basename_at_different_remote_paths_is_ambiguous(self):
        with self.assertRaises(ArtifactSelectionError):
            select_artifact([(FULL, "root"), ("nested/" + FULL, "nested")], cfg=self.cfg,
                            placeholder="{{REL_RELEASE_FULL_ARCHIVE}}", run="release")

    def test_local_mapping_also_chooses_current_tgz_instead_of_old_or_shorter_files(self):
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=str(ROOT / "output"))))
        for filename in self.release_files + [FULL.replace("6.4.14.3", "6.4.14.1")]:
            (root / filename).write_text("offline")
        runs = self.summaries()
        runs[0].update(share_links_source="local", local_dir=str(root), share_links_main={})
        with mock.patch.object(pipeline, "_dsl_try_dsm_share_link_for_local_path",
            side_effect=lambda **kw: (kw["local_path"].name, self.url(kw["local_path"].name))):
            mapping = pipeline._build_placeholder_replacements_from_dsl(cfg=self.cfg, run_summaries=runs,
                dsl_cfg=pipeline._expand_placeholders(self.dsl, self.cfg), dsl_runtime={"vars": {}})
        self.assert_link(mapping, "{{REL_RELEASE_FULL_ARCHIVE}}", FULL)

    def test_prepare_selects_current_archive_before_extraction(self):
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=str(ROOT / "output"))))
        for filename in [FULL, FULL.replace("6.4.14.3", "6.4.14.1")]: (root / filename).write_text("offline")
        dsl = {"prepare": {"flow": {"work_dir": str(root / "work"), "upload_payload_dir": str(root / "payload"),
            "steps": [{"op": "find", "root": str(root), "match": {"regex": r"^archive_cologne_.*\.tgz$"},
                       "save_as": "release_top_tgz"}]}}}
        result = pipeline._dsl_execute_prepare_flow(cfg=self.cfg, dsl_cfg=dsl, dry_run=False, current_run="release")
        self.assertEqual(result["vars"]["release_top_tgz"].name, FULL)

    def test_optional_prepare_step_cannot_hide_ambiguous_package_selection(self):
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=str(ROOT / "output"))))
        for filename in [FULL, FULL.replace("202609182037", "202609182038")]: (root / filename).write_text("offline")
        dsl = {"prepare": {"flow": {"work_dir": str(root / "work"), "upload_payload_dir": str(root / "payload"),
            "steps": [{"op": "find", "root": str(root), "match": {"regex": r"^archive_cologne_.*\.tgz$"},
                       "save_as": "release_top_tgz", "optional": True}]}}}
        with self.assertRaises(ArtifactSelectionError):
            pipeline._dsl_execute_prepare_flow(cfg=self.cfg, dsl_cfg=dsl, dry_run=False, current_run="release")

    def test_legacy_local_fallback_does_not_choose_an_older_archive(self):
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=str(ROOT / "output"))))
        for filename in [FULL, FULL.replace("6.4.14.3", "6.4.14.1")]: (root / filename).write_text("offline")
        self.cfg["nas"] = {"dsm": {"base_url": "https://nas.invalid", "auth": {"username": "offline", "password": "offline"}}}
        runs = self.summaries([])
        runs[0].update(share_links_source="local", local_dir=str(root))
        client = mock.Mock()
        client.create_share_link.side_effect = lambda **kw: self.url(kw["path"].rsplit("/", 1)[-1])
        with mock.patch.object(pipeline, "SynologyDsmClient", return_value=client):
            mapping = pipeline._build_placeholder_replacements(cfg=self.cfg, run_summaries=runs)
        self.assert_link(mapping, "{{REL_RELEASE_FULL_ARCHIVE}}", FULL)


if __name__ == "__main__":
    unittest.main()
