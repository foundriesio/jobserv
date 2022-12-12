# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

import json

from unittest.mock import patch

from jobserv.permissions import _sign
from jobserv.models import (
    Build,
    BuildStatus,
    Project,
    ProjectTrigger,
    Run,
    Test,
    TriggerTypes,
    db,
)

from tests import JobServTest


class BuildAPITest(JobServTest):
    def setUp(self):
        super().setUp()
        self.create_projects("proj-1")
        self.project = Project.query.all()[0]
        self.urlbase = "/projects/%s/builds/" % self.project.name

    def _post(self, url, data, headers, status=200):
        resp = self.client.post(url, data=data, headers=headers)
        self.assertEqual(status, resp.status_code, resp.data)
        return resp

    def test_no_builds(self):
        builds = self.get_json(self.urlbase)["builds"]
        self.assertEqual([], builds)

    def test_build_list(self):
        Build.create(self.project)
        Build.create(self.project)
        Build.create(self.project)

        builds = self.get_json(self.urlbase)["builds"]
        self.assertEqual(3, len(builds))
        for i, b in enumerate(builds):
            self.assertEqual(3 - i, b["build_id"])

    def test_build_list_paginate(self):
        for x in range(8):
            Build.create(self.project)
        data = self.get_json(self.urlbase + "?limit=4")
        self.assertIn("next", data)
        data = self.get_json(data["next"])
        self.assertNotIn("next", data)
        data = self.get_json(self.urlbase + "?limit=4&page=2")
        self.assertEqual([], data["builds"])

    def test_build_get(self):
        Build.create(self.project)
        b = Build.create(self.project)
        Build.create(self.project)
        data = self.get_json(self.urlbase + "2/")["build"]
        self.assertEqual(b.build_id, data["build_id"])
        self.assertEqual(["QUEUED"], [x["status"] for x in data["status_events"]])

    @patch("jobserv.api.build.Storage")
    def test_build_get_definition(self, storage):
        Build.create(self.project)
        storage().get_project_definition.return_value = "foo: bar"
        r = self.client.get(self.urlbase + "1/project.yml")
        self.assertEqual(200, r.status_code, r.data)
        self.assertEqual("foo: bar", r.data.decode())

    def test_build_get_latest(self):
        b1 = Build.create(self.project)
        b1.trigger_name = "test-trigger-qs"
        b1.status = BuildStatus.PASSED
        db.session.commit()
        b = Build.create(self.project)
        b.status = BuildStatus.PASSED
        Build.create(self.project)
        data = self.get_json(self.urlbase + "latest/")["build"]
        self.assertEqual(b.build_id, data["build_id"])

        url = self.urlbase + "latest/?trigger_name=test-trigger-qs"
        data = self.get_json(url)["build"]
        self.assertEqual(b1.build_id, data["build_id"])

        b1.status = BuildStatus.PROMOTED
        db.session.commit()
        data = self.get_json(self.urlbase + "latest/?promoted=1")["build"]
        self.assertEqual(b1.build_id, data["build_id"])

    def test_build_trigger_fails(self):
        # ensure we have a graceful failure when we are triggered
        headers = {}
        r = self.client.post(self.urlbase, data={}, headers=headers)
        self.assertEqual(401, r.status_code)  # not signed

        _sign("http://localhost/projects/proj-1/builds/", headers, "POST")
        r = self.client.post(self.urlbase, data={}, headers=headers)
        self.assertEqual(500, r.status_code)
        data = json.loads(r.data.decode())
        self.assertEqual("error", data["status"])

    @patch("jobserv.trigger.Storage")
    def test_build_unexpected(self, storage):
        """Ensure unexpected storage errors are handled gracefully."""
        storage().set_run_definition.side_effect = RuntimeError("edge case!!!")
        headers = {"Content-type": "application/json"}
        data = {
            "trigger-name": "git",
            "project-definition": {
                "timeout": 5,
                "email": {
                    "users": "f@f.com",
                },
                "triggers": [
                    {
                        "name": "git",
                        "type": "git_poller",
                        "runs": [
                            {
                                "name": "run0",
                                "host-tag": "foo*",
                                "container": "alpine",
                                "script": "test",
                            }
                        ],
                    },
                ],
                "scripts": {
                    "test": "#test#",
                },
            },
        }
        _sign("http://localhost/projects/proj-1/builds/", headers, "POST")
        self._post(self.urlbase, json.dumps(data), headers, 500)
        self.assertEqual([BuildStatus.FAILED], [x.status for x in Run.query])

    @patch("jobserv.api.build.trigger_build")
    def test_build_trigger_simple(self, trigger_build):
        """Assert we can trigger a minimal build."""
        trigger_build.return_value.build_id = 1
        headers = {"Content-type": "application/json"}
        data = {}
        _sign("http://localhost/projects/proj-1/builds/", headers, "POST")
        self._post(self.urlbase, json.dumps(data), headers, 201)
        self.assertEqual({}, trigger_build.call_args[0][4])

        trigger_build.reset_mock()
        data = {"secrets": {"foo": "bar"}}
        self._post(self.urlbase, json.dumps(data), headers, 201)
        self.assertEqual(data["secrets"], trigger_build.call_args[0][4])

    @patch("jobserv.api.build.trigger_build")
    def test_build_trigger_with_secrets(self, trigger_build):
        """Assert we honor the trigger-type and trigger-id params."""
        trigger_build.return_value.build_id = 1
        pt = ProjectTrigger(
            "user",
            TriggerTypes.simple.value,
            self.project,
            None,
            None,
            {"foo": "simple"},
        )
        db.session.add(pt)
        db.session.commit()

        # try first trigger type
        headers = {"Content-type": "application/json"}
        data = {"trigger-type": "simple"}
        _sign("http://localhost/projects/proj-1/builds/", headers, "POST")
        self._post(self.urlbase, json.dumps(data), headers, 201)
        self.assertEqual({"foo": "simple"}, trigger_build.call_args[0][4])

        # try "optional" trigger type (when there is no "optional")
        data = {"trigger-type": "git-poller-optional"}
        _sign("http://localhost/projects/proj-1/builds/", headers, "POST")
        self._post(self.urlbase, json.dumps(data), headers, 201)
        self.assertEqual({}, trigger_build.call_args[0][4])

        # try override
        data = {"trigger-type": "simple", "secrets": {"foo": "override"}}
        _sign("http://localhost/projects/proj-1/builds/", headers, "POST")
        self._post(self.urlbase, json.dumps(data), headers, 201)
        self.assertEqual({"foo": "override"}, trigger_build.call_args[0][4])

        # try by trigger-id
        data = {"trigger-id": pt.id}
        _sign("http://localhost/projects/proj-1/builds/", headers, "POST")
        self._post(self.urlbase, json.dumps(data), headers, 201)
        self.assertEqual({"foo": "simple"}, trigger_build.call_args[0][4])

    @patch("jobserv.api.build.Storage")
    def test_promote_list_empty(self, storage):
        b = Build.create(self.project)
        db.session.add(Run(b, "run0"))
        db.session.add(Run(b, "run1"))
        for r in b.runs:
            r.set_status(BuildStatus.PASSED)
        url = "/projects/%s/promoted-builds/" % self.project.name
        builds = self.get_json(url)["builds"]
        self.assertEqual(0, len(builds))

    @patch("jobserv.api.build.Storage")
    def test_promote_list(self, storage):
        b = Build.create(self.project)
        db.session.add(Run(b, "run0"))
        db.session.add(Run(b, "run1"))
        for r in b.runs:
            r.set_status(BuildStatus.PASSED)
            t = Test(r, "t1", None, BuildStatus.PASSED)
            db.session.add(t)
        b.status = BuildStatus.PROMOTED
        b.name = "release-X"
        b.annotation = "foo bar"
        url = "/projects/%s/promoted-builds/" % self.project.name
        builds = self.get_json(url)["builds"]
        self.assertEqual(1, len(builds))
        self.assertEqual("release-X", builds[0]["name"])
        self.assertEqual("foo bar", builds[0]["annotation"])
        self.assertEqual(
            ["run0-t1", "run1-t1"], [x["name"] for x in builds[0]["tests"]]
        )

    @patch("jobserv.api.build.Storage")
    def test_promote_get(self, storage):
        b = Build.create(self.project)
        db.session.add(Run(b, "run0"))
        db.session.add(Run(b, "run1"))
        for r in b.runs:
            r.set_status(BuildStatus.PASSED)
            t = Test(r, "t1", None, BuildStatus.PASSED)
            db.session.add(t)
        b.status = BuildStatus.PROMOTED
        b.name = "release-X"
        b.annotation = "foo bar"
        url = "/projects/%s/promoted-builds/release-X/" % self.project.name
        build = self.get_json(url)["build"]
        self.assertEqual("foo bar", build["annotation"])

    def test_promote_post(self):
        b = Build.create(self.project)
        db.session.add(Run(b, "run0"))
        db.session.add(Run(b, "run1"))

        url = "http://localhost/projects/proj-1/builds/%d/promote" % b.build_id

        headers = {
            "Content-type": "application/json",
        }
        data = {
            "name": "release-x",
            "annotation": "foo bar",
        }

        # you can't promote an in-progress build
        _sign(url, headers, "POST")
        self._post(url, json.dumps(data), headers, 400)

        for r in b.runs:
            r.set_status(BuildStatus.PASSED)
        self._post(url, json.dumps(data), headers, 201)
        db.session.refresh(b)
        self.assertEqual(BuildStatus.PROMOTED, b.status)
        self.assertEqual(data["name"], b.name)
        self.assertEqual(data["annotation"], b.annotation)

    def test_cancel(self):
        b = Build.create(self.project)
        db.session.add(Run(b, "run0"))
        r = Run(b, "run1")
        r.status = BuildStatus.RUNNING
        db.session.add(r)

        url = "http://localhost/projects/proj-1/builds/%d/cancel" % b.build_id

        headers = {
            "Content-type": "application/json",
        }
        _sign(url, headers, "POST")
        self._post(url, "", headers, 202)

        expected = [BuildStatus.FAILED, BuildStatus.CANCELLING]
        if db.engine.dialect.name == "sqlite":
            # sqlite doesn't handle the query properly
            expected[0] = BuildStatus.CANCELLING

        self.assertEqual(expected, [x.status for x in b.runs])

    def test_external_build_post(self):
        headers = {"Content-type": "application/json"}
        input_data = {
            "trigger-name": "myapp-github-workflow-ci",
        }
        urlbase = "/projects/%s/external-builds/" % self.project.name
        _sign("http://localhost/projects/proj-1/external-builds/", headers, "POST")
        resp = self._post(urlbase, json.dumps(input_data), headers, 201)
        resp_data = json.loads(resp.data.decode())
        self.assertEqual(resp_data["status"], "success")
        self.assertEqual(resp_data["data"]["build_id"], 1)

        # make sure the build was added to the DB and its details
        # are equal to expected one
        data = self.get_json(self.urlbase + "1/")
        # check the build data
        self.assertIsNotNone(data.get("build"))
        b = data["build"]
        self.assertEqual(b["build_id"], 1)
        self.assertEqual(b["status"], BuildStatus.PASSED.name)
        self.assertIsNotNone(b.get("created"))
        self.assertIsNotNone(b.get("completed"))
        self.assertEqual(b["created"], b["completed"])
        self.assertEqual(b["trigger_name"], input_data["trigger-name"])
