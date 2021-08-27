# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

import json
import os

import yaml

from unittest.mock import Mock, patch

from jobserv.jsend import ApiError
from jobserv.project import ProjectDefinition

from tests import JobServTest


class ProjectSchemaTest(JobServTest):
    def setUp(self):
        super().setUp()
        self.examples = os.path.join(os.path.dirname(__file__), "../examples/projects")

    def test_examples(self):
        for f in os.listdir(self.examples):
            if f[0] == ".":  # a vim swap file :)
                continue
            with open(os.path.join(self.examples, f)) as f:
                data = yaml.safe_load(f)
                ProjectDefinition.validate_data(data)

    def test_simple_bad(self):
        # just make a schema with no "timeout" and ensure it fails
        with open(os.path.join(self.examples, "python-github.yml")) as f:
            data = yaml.safe_load(f)
            del data["timeout"]
            exp = "Cannot find required key 'timeout'"
            with self.assertRaisesRegex(Exception, exp):
                ProjectDefinition.validate_data(data)

    def test_bad_script(self):
        with open(os.path.join(self.examples, "python-github.yml")) as f:
            data = yaml.safe_load(f)
            data["triggers"][0]["runs"][0]["script"] = "doesnotexist"
            exp = "Script does not exist"
            with self.assertRaisesRegex(Exception, exp):
                ProjectDefinition.validate_data(data)

    def test_bad_script_repo(self):
        with open(os.path.join(self.examples, "python-github.yml")) as f:
            data = yaml.safe_load(f)
            del data["triggers"][0]["runs"][0]["script"]
            data["triggers"][0]["runs"][0]["script-repo"] = {
                "name": "doesnotexsit",
                "path": "path",
            }
            exp = "Script repo does not exist"
            with self.assertRaisesRegex(Exception, exp):
                ProjectDefinition.validate_data(data)

    def test_bad_script_mutual_exclusion(self):
        with open(os.path.join(self.examples, "python-github.yml")) as f:
            data = yaml.safe_load(f)
            data["triggers"][0]["runs"][0]["script-repo"] = {
                "name": "doesnotexsit",
                "path": "path",
            }
            exp = '"script" and "script-repo" are mutually exclusive'
            with self.assertRaisesRegex(Exception, exp):
                ProjectDefinition.validate_data(data)

    def test_bad_trigger(self):
        with open(os.path.join(self.examples, "python-github.yml")) as f:
            data = yaml.safe_load(f)
            data["triggers"][0]["type"] = "doesnotexist"
            exp = "No such runner"
            with self.assertRaisesRegex(Exception, exp):
                ProjectDefinition.validate_data(data)

    def test_recursive_run_trigger(self):
        with open(os.path.join(self.examples, "python-github.yml")) as f:
            data = yaml.safe_load(f)
            # cause an infinite loop for triggers
            data["triggers"][0]["runs"][0]["triggers"] = [
                {"name": "unit-test"},
            ]
            exp = "Trigger recursion depth exceeded"
            with self.assertRaisesRegex(Exception, exp):
                ProjectDefinition.validate_data(data)

    def test_recursive_build_trigger(self):
        with open(os.path.join(self.examples, "python-github.yml")) as f:
            data = yaml.safe_load(f)
            # cause an infinite loop for triggers
            data["triggers"][0]["triggers"] = [
                {"name": "unit-test"},
            ]
            exp = "Trigger recursion depth exceeded"
            with self.assertRaisesRegex(Exception, exp):
                ProjectDefinition.validate_data(data)

    def test_run_name_too_long(self):
        with open(os.path.join(self.examples, "python-github.yml")) as f:
            data = yaml.safe_load(f)
            # cause an infinite loop for triggers
            data["triggers"][0]["runs"][0]["name"] = "1" * 80
            exp = "Name of run must be less than 80 characters"
            with self.assertRaisesRegex(Exception, exp):
                ProjectDefinition.validate_data(data)

    def test_loop_on(self):
        with open(os.path.join(self.examples, "python-github.yml")) as f:
            data = yaml.safe_load(f)
            run = {
                "name": "compile-{loop}",
                "container": "foo",
                "host-tag": "amd64",
                "script": "unit-test",
                "loop-on": [
                    {"param": "BOARD", "values": ["carbon", "nitrogen"]},
                    {"param": "ZEPHYR", "values": ["upstream", "dev", "test"]},
                    {"param": "COMPILER", "values": ["gcc", "llvm"]},
                ],
                "triggers": [
                    {"name": "trigger", "run-names": "{name}-{loop}"},
                ],
            }
            data["triggers"][0]["runs"].insert(1, run)
            data["triggers"].append(
                {
                    "type": "simple",
                    "name": "trigger",
                    "runs": [
                        {
                            "name": "trigger",
                            "container": "foo",
                            "host-tag": "amd64",
                            "script": "flake8",
                        }
                    ],
                }
            )
            ProjectDefinition.validate_data(data)
            runs = ProjectDefinition(data)._data["triggers"][0]["runs"]

            # we should have 2 + (len(BOARD) * len(ZEPHYR) * len(COMPILER))
            self.assertEqual(14, len(runs))

            # they should be inserted in between the original runs in a
            # predictable order
            self.assertEqual("unit-test", runs[0]["name"])

            self.assertEqual("compile-carbon-upstream-gcc", runs[1]["name"])
            self.assertEqual("compile-carbon-upstream-llvm", runs[2]["name"])
            self.assertEqual("compile-carbon-dev-gcc", runs[3]["name"])
            self.assertEqual("compile-carbon-dev-llvm", runs[4]["name"])
            self.assertEqual("compile-carbon-test-gcc", runs[5]["name"])
            self.assertEqual("compile-carbon-test-llvm", runs[6]["name"])
            self.assertEqual("compile-nitrogen-upstream-gcc", runs[7]["name"])
            self.assertEqual("compile-nitrogen-upstream-llvm", runs[8]["name"])
            self.assertEqual("compile-nitrogen-dev-gcc", runs[9]["name"])
            self.assertEqual("compile-nitrogen-dev-llvm", runs[10]["name"])
            self.assertEqual("compile-nitrogen-test-gcc", runs[11]["name"])
            self.assertEqual("compile-nitrogen-test-llvm", runs[12]["name"])

            self.assertEqual("flake8", runs[13]["name"])

    def test_script_repo_rundef(self):
        with open(os.path.join(self.examples, "python-github.yml")) as f:
            data = yaml.safe_load(f)
            del data["triggers"][0]["runs"][0]["script"]
            data["triggers"][0]["runs"][0]["script-repo"] = {
                "name": "foo",
                "path": "path/foo.sh",
            }
            data["script-repos"] = {"foo": {"clone-url": "url"}}
            ProjectDefinition.validate_data(data)
            proj = ProjectDefinition(data)
            dbrun = Mock()
            dbrun.build.project.name = "jobserv"
            dbrun.name = "flake8"
            dbrun.build.build_id = 1
            dbrun.api_key = "123"
            trigger = proj._data["triggers"][0]
            run = trigger["runs"][0]
            rundef = proj.get_run_definition(dbrun, run, trigger, {}, {})
            repo = json.loads(rundef).get("script-repo")
            self.assertEqual({"clone-url": "url", "path": "path/foo.sh"}, repo)

    @patch("jobserv.project.url_for")
    def test_script_repo_token(self, url_for):
        url_for.return_value = "blah"
        with open(os.path.join(self.examples, "python-github.yml")) as f:
            data = yaml.safe_load(f)
            del data["triggers"][0]["runs"][0]["script"]
            data["triggers"][0]["runs"][0]["script-repo"] = {
                "name": "foo",
                "path": "path/foo.sh",
            }
            data["script-repos"] = {"foo": {"clone-url": "url", "token": "f"}}
            ProjectDefinition.validate_data(data)
            proj = ProjectDefinition(data)
            dbrun = Mock()
            dbrun.build.project.name = "jobserv"
            dbrun.build.build_id = 1
            dbrun.name = "flake8"
            dbrun.api_key = "secret"
            trigger = proj._data["triggers"][0]
            run = trigger["runs"][0]

            with self.assertRaises(ApiError):
                proj.get_run_definition(dbrun, run, trigger, {}, {})

    def test_bad_container_auth(self):
        with open(os.path.join(self.examples, "private-container.yml")) as f:
            data = yaml.safe_load(f)
            proj = ProjectDefinition.validate_data(data)

            dbrun = Mock()
            dbrun.build.project.name = "jobserv"
            dbrun.build.build_id = 1
            dbrun.name = "flake8"
            dbrun.api_key = "secret"
            trigger = proj._data["triggers"][0]
            run = trigger["runs"][0]

            exp = "not defined in the run's secrets"
            with self.assertRaisesRegex(ApiError, exp):
                proj.get_run_definition(dbrun, run, trigger, {}, {})

    def test_host_tag_rundef(self):
        with open(os.path.join(self.examples, "host-tag.yml")) as f:
            data = yaml.safe_load(f)
            ProjectDefinition.validate_data(data)
            proj = ProjectDefinition(data)
            dbrun = Mock()
            dbrun.build.project.name = "jobserv"
            dbrun.name = "flake8"
            dbrun.build.build_id = 1
            dbrun.api_key = "123"
            trigger = proj._data["triggers"][0]
            run = trigger["runs"][0]
            rundef = proj.get_run_definition(dbrun, run, trigger, {}, {})
            data = json.loads(rundef)
            self.assertEqual("aarch6*", data["host-tag"])
            self.assertEqual("aarch6*", dbrun.host_tag)

    def test_host_tag_rundef_loopon(self):
        with open(os.path.join(self.examples, "host-tag.yml")) as f:
            data = yaml.safe_load(f)
            ProjectDefinition.validate_data(data)
            proj = ProjectDefinition(data)
            dbrun = Mock()
            dbrun.build.project.name = "jobserv"
            dbrun.name = "flake8"
            dbrun.build.build_id = 1
            dbrun.api_key = "123"
            trigger = proj._data["triggers"][0]
            run = trigger["runs"][1]
            rundef = proj.get_run_definition(dbrun, run, trigger, {}, {})
            data = json.loads(rundef)
            self.assertEqual("aarch64", data["host-tag"])
            self.assertEqual("aarch64", dbrun.host_tag)

            trigger = proj._data["triggers"][0]
            run = trigger["runs"][2]
            rundef = proj.get_run_definition(dbrun, run, trigger, {}, {})
            data = json.loads(rundef)
            self.assertEqual("armhf", data["host-tag"])
            self.assertEqual("armhf", dbrun.host_tag)

    def test_host_tag_rundef_loopon_bad(self):
        with open(os.path.join(self.examples, "host-tag.yml")) as f:
            data = yaml.safe_load(f)
            data["triggers"][0]["runs"][1]["loop-on"][0]["param"] = "host-tagz"
            exp = '"host-tag" or loop-on host-tag parameter required'
            with self.assertRaisesRegex(Exception, exp):
                ProjectDefinition.validate_data(data)

    def test_params(self):
        """Make sure we get project, trigger, and run params"""
        with open(os.path.join(self.examples, "parameters.yml")) as f:
            data = yaml.safe_load(f)
            proj = ProjectDefinition.validate_data(data)

            dbrun = Mock()
            dbrun.build.project.name = "jobserv"
            dbrun.name = "basic"
            dbrun.build.build_id = 1
            dbrun.api_key = "123"
            trigger = proj._data["triggers"][0]
            run = trigger["runs"][0]
            rundef = proj.get_run_definition(dbrun, run, trigger, {}, {})
            data = json.loads(rundef)
            self.assertEqual("GLOBAL", data["env"]["GLOBAL_PARAM"])
            self.assertEqual("RUN", data["env"]["RUN_PARAM"])
            self.assertEqual("TRIGGER", data["env"]["TRIGGER_PARAM"])
