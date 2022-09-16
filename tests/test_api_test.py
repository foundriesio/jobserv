# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

import json
import shutil
import tempfile

from unittest.mock import patch

import jobserv.models
import jobserv.storage.base

from jobserv.models import Build, BuildStatus, Project, Run, Test, TestResult, db

from tests import JobServTest


class TestAPITest(JobServTest):
    def setUp(self):
        super().setUp()
        self.create_projects("proj-1")
        p = Project.query.all()[0]
        b = Build.create(p)
        r = Run(b, "run0")
        db.session.add(r)
        db.session.flush()
        self.test = Test(r, "test1", "test1-ctx")
        db.session.add(self.test)
        db.session.commit()
        self.urlbase = "/projects/proj-1/builds/1/runs/run0/tests/"

        jobserv.storage.base.JOBS_DIR = tempfile.mkdtemp()
        jobserv.models.JOBS_DIR = jobserv.storage.base.JOBS_DIR
        self.addCleanup(shutil.rmtree, jobserv.storage.base.JOBS_DIR)

    def _post(self, url, data, headers, status=200):
        resp = self.client.post(url, data=data, headers=headers)
        self.assertEqual(status, resp.status_code, resp.data)
        return resp

    def test_test_list(self):
        data = self.get_json(self.urlbase)
        self.assertEqual("test1", data["tests"][0]["name"])
        self.assertEqual("test1-ctx", data["tests"][0]["context"])

    def test_test_get(self):
        data = self.get_json(self.urlbase + "test1/")
        self.assertEqual(0, len(data["test"]["results"]))

        db.session.add(TestResult(self.test, "tr1", "ctx"))
        db.session.commit()
        data = self.get_json(self.urlbase + "test1/")
        self.assertEqual(1, len(data["test"]["results"]))
        self.assertEqual("tr1", data["test"]["results"][0]["name"])

    def test_test_create(self):
        headers = [
            ("Authorization", "Token %s" % self.test.run.api_key),
            ("Content-type", "application/json"),
        ]

        url = self.urlbase + "test2/"
        self._post(url, json.dumps({"context": "foo"}), headers)
        db.session.refresh(self.test.run)
        self.assertEqual(["test1", "test2"], [x.name for x in self.test.run.tests])

    @patch("jobserv.api.run.Storage")
    def test_test_create_results(self, storage):
        headers = [
            ("Authorization", "Token %s" % self.test.run.api_key),
            ("Content-type", "application/json"),
        ]
        test = {
            "context": "junit",
            "results": [
                {
                    "name": "tr1",
                    "context": "ctx1",
                    "status": "PASSED",
                },
                {
                    "name": "tr2",
                    "context": "ctx1",
                    "status": "FAILED",
                },
                {
                    "name": "tr3",
                    "context": "ctx1",
                    "status": "SKIPPED",
                },
            ],
            "status": "FAILED",
        }

        url = self.urlbase + "test2/"
        self._post(url, json.dumps(test), headers)
        db.session.refresh(self.test.run)
        self.assertEqual(["test1", "test2"], [x.name for x in self.test.run.tests])
        results = self.test.run.tests[-1].results
        self.assertEqual(["tr1", "tr2", "tr3"], [x.name for x in results])
        exp = [BuildStatus.PASSED, BuildStatus.FAILED, BuildStatus.SKIPPED]
        self.assertEqual(exp, [x.status for x in results])
        self.assertEqual(BuildStatus.FAILED, self.test.run.tests[-1].status)

        self.test.run.tests[0].status = BuildStatus.PASSED
        db.session.commit()
        storage().get_run_definition.return_value = {}
        headers.append(("X-RUN-STATUS", "FAILED"))
        self._post("/projects/proj-1/builds/1/runs/run0/", None, headers=headers)
        db.session.refresh(self.test.run)
        self.assertEqual(BuildStatus.FAILED, self.test.run.status)

    def test_test_create_results_stdout(self):
        headers = [
            ("Authorization", "Token %s" % self.test.run.api_key),
            ("Content-type", "application/json"),
        ]
        test = {
            "context": "junit",
            "results": [
                {
                    "name": "tr1",
                    "context": "ctx1",
                    "status": "PASSED",
                    "output": "This is the test output",
                },
            ],
            "status": "FAILED",
        }

        url = self.urlbase + "test2/"
        self._post(url, json.dumps(test), headers)
        db.session.refresh(self.test.run)
        self.assertEqual(
            "This is the test output", self.test.run.tests[-1].results[0].output
        )

    @patch("jobserv.api.test.Storage")
    def test_test_update(self, storage):
        headers = [
            ("Content-type", "application/json"),
            ("Authorization", "Token " + self.test.run.api_key),
        ]
        url = self.urlbase + "test1/"
        resp = self.client.put(
            url, data=json.dumps({"msg": "blah blah"}), headers=headers
        )
        self.assertEqual(200, resp.status_code)

        resp = self.client.put(
            url, data=json.dumps({"status": "FAILED"}), headers=headers
        )
        self.assertEqual(200, resp.status_code)
        self.assertTrue(resp.json["data"]["complete"])
        db.session.refresh(self.test)
        self.assertEqual("FAILED", self.test.status.name)
        self.assertEqual("FAILED", self.test.run.status.name)

    @patch("jobserv.api.test.Storage")
    def test_test_update_multiple(self, storage):
        test2 = Test(self.test.run, "test2", "test2-ctx")
        db.session.add(test2)
        db.session.commit()

        headers = [
            ("Content-type", "application/json"),
            ("Authorization", "Token " + self.test.run.api_key),
        ]
        url = self.urlbase + "test1/"
        resp = self.client.put(
            url, data=json.dumps({"msg": "blah blah"}), headers=headers
        )
        self.assertEqual(200, resp.status_code)

        resp = self.client.put(
            url, data=json.dumps({"status": "FAILED"}), headers=headers
        )
        self.assertEqual(200, resp.status_code)
        self.assertFalse(resp.json["data"]["complete"])
        db.session.refresh(self.test)
        self.assertEqual("FAILED", self.test.status.name)
        self.assertEqual("RUNNING_WITH_FAILURES", self.test.run.status.name)

        url = self.urlbase + "test2/"
        resp = self.client.put(
            url, data=json.dumps({"msg": "blah blah"}), headers=headers
        )
        self.assertEqual(200, resp.status_code)

        resp = self.client.put(
            url, data=json.dumps({"status": "FAILED"}), headers=headers
        )
        self.assertEqual(200, resp.status_code)
        self.assertTrue(resp.json["data"]["complete"])
        db.session.refresh(self.test)
        self.assertEqual("FAILED", self.test.status.name)
        self.assertEqual("FAILED", self.test.run.status.name)

    @patch("jobserv.api.test.Storage")
    def test_test_update_duplicate(self, storage):
        """Ensure tests with same name can filter by context.
        Tests don't have to have the same name, so we also allow users
        to update a test based on it context.
        """
        t = Test(self.test.run, "test1", "test1-ctx2")
        db.session.add(t)
        db.session.commit()

        headers = [
            ("Content-type", "application/json"),
            ("Authorization", "Token " + self.test.run.api_key),
        ]

        url = self.urlbase + "test1/"
        resp = self.client.put(
            url,
            data=json.dumps({"status": "PASSED"}),
            headers=headers,
            query_string={"context": "test1-ctx2"},
        )
        self.assertEqual(200, resp.status_code)
        db.session.refresh(t)
        self.assertEqual("PASSED", t.status.name)

    @patch("jobserv.api.test.Storage")
    def test_test_update_with_results(self, storage):
        headers = [
            ("Content-type", "application/json"),
            ("Authorization", "Token " + self.test.run.api_key),
        ]

        url = self.urlbase + "test1/"
        data = {
            "status": "PASSED",
            "results": [
                {"name": "tr1", "status": "PASSED"},
                {"name": "tr2", "status": "PASSED"},
            ],
        }
        resp = self.client.put(url, data=json.dumps(data), headers=headers)
        self.assertEqual(200, resp.status_code)
        self.assertEqual("PASSED", self.test.status.name)
        self.assertEqual("PASSED", self.test.run.status.name)
        self.assertEqual(2, len(self.test.results))
        self.assertEqual("tr1", self.test.results[0].name)
        self.assertEqual("PASSED", self.test.results[0].status.name)
        self.assertEqual("tr2", self.test.results[1].name)

    def test_test_find(self):
        # internal api not signed, must fail
        r = self.client.get("/find_test/")
        self.assertEqual(401, r.status_code, r.data)

        data = self.get_signed_json("/find_test/", query_string="context=d")
        self.assertEqual(0, len(data["tests"]))
        data = self.get_signed_json("/find_test/", query_string="context=test1-ctx")
        self.assertEqual(1, len(data["tests"]))
        self.assertEqual("test1", data["tests"][0]["name"])

    def test_find_incomplete(self):
        t = Test(self.test.run, "test2", "test2-ctx")
        t.status = BuildStatus.PASSED
        db.session.add(t)
        db.session.commit()

        # internal api not signed, must fail
        r = self.client.get("/incomplete_tests/")
        self.assertEqual(401, r.status_code)

        data = self.get_signed_json("/incomplete_tests/")
        self.assertEqual(["test1"], [x["name"] for x in data["tests"]])
