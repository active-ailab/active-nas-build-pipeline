"""Optional CI log dictionaries: parity, NAS upload, and document links."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from artifact_matching import ArtifactSelectionError, select_artifact
from config_loader import read_json_with_base
import release_pipeline_run as pipeline


ROOT = Path(__file__).resolve().parents[1]
RELEASE_KEY = "{{REL_RELEASE_LOG_DIST_BZ2_BIN}}"
DEBUG_KEY = "{{REL_MONKEY_LOG_DIST_BZ2_BIN}}"


class LogDictionaryTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        for target in ("urllib.request.urlopen", "urllib.request.OpenerDirector.open",
                       "requests.sessions.Session.request", "subprocess.Popen"):
            self.stack.enter_context(mock.patch(target, side_effect=AssertionError("External action: " + target)))
        self.temp = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=str(ROOT / "output"))))
        self.dsl = json.loads((ROOT / "placeholders.default.json").read_text(encoding="utf-8"))

    def patch(self, *args, **kwargs):
        return self.stack.enter_context(mock.patch.object(*args, **kwargs))

    @staticmethod
    def cfg(project="fuji", mode="single"):
        return {"release": {"project": project, "version": "1.2.0.1"},
                "vars": {"build_release_mode": mode, "debug_version_name": "1.2.0"},
                "changelog": {"enabled": False}}

    @staticmethod
    def filename(project, tail):
        return f"log_dict_bz2_{project}_1.2.0.{tail}_OTA.bin"

    def mapping(self, cfg, runs, legacy=False):
        if legacy:
            return pipeline._build_placeholder_replacements(cfg=cfg, run_summaries=runs)
        return pipeline._build_placeholder_replacements_from_dsl(
            cfg=cfg, run_summaries=runs, dsl_cfg=pipeline._expand_placeholders(self.dsl, cfg),
            dsl_runtime={"vars": {}})

    def test_document_links_for_all_projects_and_both_build_modes(self):
        projects = [p.stem[4:] for p in ROOT.glob("gqf_*.json")]
        for project in projects:
            for mode in ("single", "split"):
                for legacy in (False, True):
                    with self.subTest(project=project, mode=mode, legacy=legacy):
                        files = [self.filename(project, tail) for tail in (0, 1)]
                        files += [self.filename("other_project", 1), f"log_dict_bz2_{project}_1.1.0.1_OTA.bin"]
                        runs = [{"name": run, "share_links_source": "nas", "share_links_main": {
                            name: f"https://nas.invalid/{folder}/{name}" for name in files}}
                            for run, folder in (("release", "Release"), ("debug", "Monkey"))]
                        result = self.mapping(self.cfg(project, mode), runs, legacy)
                        for key, folder, tail in ((RELEASE_KEY, "Release", 1), (DEBUG_KEY, "Monkey", 0)):
                            name = self.filename(project, tail)
                            self.assertEqual(result[key], f"{name}|https://nas.invalid/{folder}/{name}")

    def test_absent_dictionary_is_optional(self):
        for legacy in (False, True):
            result = self.mapping(self.cfg(), [], legacy)
            self.assertEqual(result[RELEASE_KEY], "")
            self.assertEqual(result[DEBUG_KEY], "")

    def test_two_matching_dictionary_files_are_not_guessed(self):
        cfg = self.cfg()
        files = [(self.filename("fuji", 1), "a"), ("log_dict_bz2_fuji_1.2.0.1_BOOT.bin", "b")]
        with self.assertRaises(ArtifactSelectionError):
            select_artifact(files, cfg=cfg, placeholder=RELEASE_KEY, run="release")

    def test_three_segment_or_literal_x_versions_cannot_match(self):
        files = [("log_dict_bz2_fuji_1.2.0_OTA.bin", "a"), ("log_dict_bz2_fuji_1.2.0.x_OTA.bin", "b")]
        self.assertEqual(select_artifact(files, cfg=self.cfg(), placeholder=RELEASE_KEY, run="release"), ("", ""))

    def test_real_pipeline_uploads_dictionary_to_release_and_monkey(self):
        for mode in ("single", "split"):
            with self.subTest(mode=mode):
                cfg = read_json_with_base(ROOT / "gqf_fuji.json")
                cfg["release"].update(project="fuji", device_name="fuji", version="1.2.0.1")
                cfg["vars"].update(build_release_mode=mode, debug_version_name="1.2.0", build_tscan="no")
                cfg["prepare"] = {"mode": "none", "workspace_dir": str(self.temp / mode / "prepare")}
                cfg["changelog"] = {"enabled": False}
                cfg["notifications"] = {}
                cfg["feishu"]["enabled"] = False
                cfg["doc"]["enabled"] = False
                cfg["cleanup"] = {"enabled": False}
                cfg["nas"]["uploads"] = []
                for run, folder, number in (("release", "Release", 42), ("debug", "Monkey", 43)):
                    out = self.temp / mode / run
                    cfg["jenkins"]["builds"][run] = {
                        "build_url": f"https://offline.invalid/job/ci/{42 if mode == 'single' else number}/",
                        "download": {"output_dir": str(out), "include_globs": ["**/*"], "overwrite": True}}
                    cfg["nas"]["uploads"].append({"name": run, "remote_subdir": folder, "local_dir": str(out)})
                cfg["jenkins"]["builds"]["tscan"] = {"build_url": "", "download": {"output_dir": str(self.temp / "no-tscan")}}
                cfg["nas"]["uploads"].append({"name": "tscan", "remote_subdir": "Monkey",
                    "local_dir": str(self.temp / "no-tscan")})
                cp = self.temp / f"{mode}.json"
                cp.write_text(json.dumps(cfg), encoding="utf-8")
                uploaded = {}
                downloads = []

                def download(**kw):
                    downloads.append(kw["build_url"])
                    out = kw["out_dir"]
                    out.mkdir(parents=True, exist_ok=True)
                    tails = (0, 1) if mode == "single" else ((0,) if out.name == "debug" else (1,))
                    nested = out / "apollo5"
                    nested.mkdir(exist_ok=True)
                    for tail in tails:
                        (nested / self.filename("fuji", tail)).write_bytes(b"offline dictionary")

                def upload(**kw):
                    uploaded[kw["folder_name"]] = sorted(p.name for p in Path(kw["local_dir"]).rglob("*.bin"))

                with contextlib.ExitStack() as run_stack:
                    run_stack.enter_context(mock.patch.dict(os.environ, {
                        key: "offline" for key in ("JENKINS_USERNAME", "JENKINS_PASSWORD", "NAS_WEBDAV_USERNAME",
                            "NAS_WEBDAV_PASSWORD", "NAS_DSM_USERNAME", "NAS_DSM_PASSWORD", "DSM_PASSWORD")}))
                    run_stack.enter_context(mock.patch.object(sys, "argv", ["pipeline", "--config", str(cp),
                        "--skip-prepare", "--skip-doc", "--skip-feishu", "--skip-cleanup", "--no-progress"]))
                    for name in ("_notify_base_stage", "_maybe_notify_webhook_text", "_feishu_notify_user"):
                        run_stack.enter_context(mock.patch.object(pipeline, name))
                    run_stack.enter_context(mock.patch.object(pipeline, "jenkins_download_artifacts", side_effect=download))
                    run_stack.enter_context(mock.patch.object(pipeline, "_run_uploader", side_effect=upload))
                    client = mock.Mock()
                    client.create_share_link.side_effect = lambda **kw: "https://nas.invalid/sharing/" + kw["path"].rsplit("/", 1)[-1]
                    run_stack.enter_context(mock.patch.object(pipeline, "SynologyDsmClient", return_value=client))
                    links = run_stack.enter_context(mock.patch.object(pipeline, "_generate_and_print_share_links",
                        wraps=pipeline._generate_and_print_share_links))
                    self.assertEqual(pipeline.main(), 0)
                self.assertEqual(uploaded["Release"], [self.filename("fuji", 1)])
                self.assertEqual(uploaded["Monkey"], [self.filename("fuji", 0)])
                self.assertEqual(len(downloads), 1 if mode == "single" else 2)
                self.assertEqual({c[1]["remote_dir"].rsplit("/", 1)[-1] for c in links.call_args_list}, {"Release", "Monkey"})
                shared = [c[1]["path"] for c in client.create_share_link.call_args_list]
                self.assertTrue(any(p.endswith("/Release/apollo5/" + self.filename("fuji", 1)) for p in shared))
                self.assertTrue(any(p.endswith("/Monkey/apollo5/" + self.filename("fuji", 0)) for p in shared))
