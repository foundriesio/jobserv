# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

from jobserv.models import Project, ProjectTrigger, db
from tests import JobServTest


class ProjectTriggerAPITest(JobServTest):
    def test_list_triggers_no_auth(self):
        r = self.client.get("/project-triggers/")
        self.assertEqual(401, r.status_code)

    def test_list_triggers_empty(self):
        triggers = self.get_signed_json("/project-triggers/")
        self.assertEqual([], triggers)

    def test_list_triggers(self):
        p = Project("p")
        db.session.add(p)
        db.session.flush()
        db.session.add(ProjectTrigger("user", 1, p, "repo", "file", {}))
        db.session.add(ProjectTrigger("use4", 2, p, "rep0", "fil3", {}))
        db.session.commit()
        triggers = self.get_signed_json("/project-triggers/")
        self.assertEqual(2, len(triggers))
        self.assertIn("id", triggers[0])
        self.assertEqual("p", triggers[0]["project"])
        self.assertEqual("user", triggers[0]["user"])
        self.assertEqual("repo", triggers[0]["definition_repo"])
        self.assertEqual("git_poller", triggers[0]["type"])
        self.assertEqual({}, triggers[0]["secrets"])

        self.assertEqual("p", triggers[1]["project"])
        self.assertEqual("use4", triggers[1]["user"])
        self.assertEqual("rep0", triggers[1]["definition_repo"])
        self.assertEqual("github_pr", triggers[1]["type"])

        triggers = self.get_signed_json(
            "/project-triggers/", query_string="type=git_poller"
        )
        self.assertEqual(1, len(triggers))
        self.assertEqual("git_poller", triggers[0]["type"])
        self.assertEqual("file", triggers[0]["definition_file"])

        triggers = self.get_signed_json(
            "/projects/p/triggers/", query_string="type=github_pr"
        )
        self.assertEqual(1, len(triggers))
        self.assertEqual("rep0", triggers[0]["definition_repo"])

        r = self.client.get("/projects/p/triggers/")
        self.assertEqual(401, r.status_code)
