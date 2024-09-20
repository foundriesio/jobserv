# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

import json
from uuid import uuid4

from unittest.mock import patch

from jobserv.models import Build, BuildStatus, Project, Run, TriggerTypes, db
from jobserv.permissions import _sign

from tests import JobServTest


class ProjectAPITest(JobServTest):
    def test_no_projects(self):
        jobs = self.get_json("/projects/")["projects"]
        self.assertEqual([], jobs)

    def test_project_list(self):
        self.create_projects("job-1", "job-2", "job-3")
        jobs = self.get_json("/projects/")["projects"]
        self.assertEqual(3, len(jobs))
        for i, j in enumerate(jobs):
            self.assertEqual("job-%d" % (i + 1), j["name"])

    def test_project_get(self):
        self.create_projects("job-1", "job-2", "job-3")
        j = self.get_json("/projects/job-2/")["project"]
        self.assertEqual("job-2", j["name"])

    def test_project_get_404(self):
        r = self.client.get("/projects/job-2/")
        self.assertEqual(404, r.status_code)
        self.assertIn("message", json.loads(r.data.decode()))

    @patch("jobserv.permissions.project_can_access")
    def test_project_permission(self, can_access):
        can_access.return_value = False
        self.create_projects("job-1")
        r = self.client.get("/projects/job-1/")
        self.assertEqual(404, r.status_code)

    def test_project_create_denied(self):
        headers = {"Content-type": "application/json"}
        r = self.client.post(
            "/projects/", headers=headers, data=json.dumps({"name": "foo"})
        )
        self.assertEqual(401, r.status_code)

    def test_project_create_bad_allowed_hosts(self):
        url = "http://localhost/projects/"
        headers = {"Content-type": "application/json"}
        _sign(url, headers, "POST")
        r = self.client.post(
            "/projects/",
            headers=headers,
            data=json.dumps({"name": "foo", "allowed-host-tags": "12"}),
        )
        self.assertEqual(400, r.status_code, r.text)

    def test_project_create(self):
        url = "http://localhost/projects/"
        headers = {"Content-type": "application/json"}
        _sign(url, headers, "POST")
        r = self.client.post(url, headers=headers, data=json.dumps({"name": "foo"}))
        self.assertEqual(201, r.status_code, r.data)
        Project.query.filter(Project.name == "foo").one()

        r = self.client.post(
            url,
            headers=headers,
            data=json.dumps({"name": "foo2", "synchronous-builds": True}),
        )
        self.assertEqual(201, r.status_code, r.data)
        p = Project.query.filter(Project.name == "foo2").one()
        self.assertTrue(p.synchronous_builds)

        url = "http://localhost/projects/"
        headers = {"Content-type": "application/json"}
        _sign(url, headers, "POST")
        r = self.client.post(
            url,
            headers=headers,
            data=json.dumps({"name": "bar", "allowed-host-tags": ["1", "2"]}),
        )
        self.assertEqual(201, r.status_code, r.data)
        p = Project.query.filter(Project.name == "bar").one()
        self.assertEqual(["1", "2"], p.allowed_host_tags)

    def test_project_delete_denied(self):
        self.create_projects("proj-1")
        url = "http://localhost/projects/proj-1/"
        r = self.client.delete(url)
        self.assertEqual(401, r.status_code)
        self.assertEqual("X-JobServ-Sig not provided", r.json["message"])

        headers = {"Content-type": "application/json"}
        _sign(url, headers, "DELETE")
        r = self.client.delete(url, headers=headers, data=json.dumps({}))
        self.assertEqual(401, r.status_code, r.data)

        headers = {"Content-type": "application/json"}
        _sign(url, headers, "DELETE")
        data = {"I_REALLY_MEAN_TO_DO_THIS": "YES"}
        r = self.client.delete(url, headers=headers, data=json.dumps(data))
        self.assertEqual(200, r.status_code, r.data)

    def test_project_run_history(self):
        self.create_projects("proj-1")
        p = Project.query.all()[0]

        for x in range(4):
            b = Build.create(p)
            r = Run(b, "run0")
            r.status = BuildStatus.PASSED
            if x % 2 == 0:
                r.status = BuildStatus.FAILED
            db.session.add(r)
            r = Run(b, "run1")
            db.session.add(r)
        db.session.commit()
        r = self.get_json("/projects/proj-1/history/run0/")
        expected = ["PASSED", "FAILED", "PASSED", "FAILED"]
        self.assertEqual(expected, [x["status"] for x in r["runs"]])
        expected = ["run0", "run0", "run0", "run0"]
        self.assertEqual(expected, [x["name"] for x in r["runs"]])

    def test_project_trigger_create(self):
        self.create_projects("proj-1")
        url = "http://localhost/projects/proj-1/triggers/"

        headers = {"Content-type": "application/json"}
        _sign(url, headers, "POST")
        data = {
            "owner": "gavin.gavel",
            "type": "git_poller",
            "secret1": "ThisIsThePassword",
        }
        r = self.client.post(url, headers=headers, data=json.dumps(data))
        self.assertEqual(201, r.status_code, r.data)
        p = Project.query.filter(Project.name == "proj-1").one()
        self.assertEqual(1, len(p.triggers))
        t = p.triggers[0]
        self.assertEqual(TriggerTypes.git_poller.value, t.type)
        self.assertEqual(data["owner"], t.user)
        self.assertEqual(data["secret1"], t.secret_data["secret1"])

        trigger = self.get_signed_json(url)[0]
        self.assertEqual([{"name": "secret1"}], trigger["secrets"])

        # now patch the secrets and make sure it works
        data = {
            "secrets": [
                {"name": "secret1", "value": "newval"},  # update one
                {"name": "secret2", "value": "hax0r"},  # add one
            ]
        }
        self.patch_signed_json(url + str(trigger["id"]) + "/", data)
        p = Project.query.filter(Project.name == "proj-1").one()
        t = p.triggers[0]
        self.assertEqual(t.secret_data["secret1"], "newval")
        self.assertEqual(t.secret_data["secret2"], "hax0r")

    def test_project_trigger_secret_removal(self):
        project_name = "projectUpdateTest"
        trigger_url = f"http://localhost/projects/{project_name}/triggers/"
        trigger_headers = {"Content-type": "application/json"}
        _sign(trigger_url, trigger_headers, "POST")
        secret_a = (str(uuid4()), str(uuid4()))  # (key, value) tuple
        secret_b = (str(uuid4()), str(uuid4()))
        secret_c = (str(uuid4()), str(uuid4()))
        secrets = dict([secret_a, secret_b, secret_c])
        create_data = {
            "owner": "tester.testing",
            "type": TriggerTypes.git_poller.name,
            **secrets,
        }
        self.create_projects(project_name)
        response = self.client.post(
            trigger_url, headers=trigger_headers, data=json.dumps(create_data)
        )
        self.assertEqual(201, response.status_code, msg=response.data)
        actual_triggers = self.get_signed_json(url=trigger_url)
        self.assertEqual(1, len(actual_triggers))
        actual_trigger = actual_triggers[0]
        actual_secrets = [secret["name"] for secret in actual_trigger["secrets"]]
        self.assertEqual(3, len(actual_secrets))
        self.assertIn(secret_a[0], actual_secrets)
        self.assertIn(secret_b[0], actual_secrets)
        self.assertIn(secret_c[0], actual_secrets)
        remove_data = {"secrets": [{"name": secret_b[0], "value": None}]}

        self.patch_signed_json(f"{trigger_url}{actual_trigger['id']}/", remove_data)

        actual_trigger = self.get_signed_json(url=trigger_url)[0]
        actual_secrets = [secret["name"] for secret in actual_trigger["secrets"]]
        self.assertEqual(2, len(actual_secrets))
        self.assertIn(secret_a[0], actual_secrets)
        self.assertNotIn(secret_b[0], actual_secrets)
        self.assertIn(secret_c[0], actual_secrets)
