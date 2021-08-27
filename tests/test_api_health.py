# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

import json

from jobserv.models import Build, BuildStatus, Project, Run, Worker, db

from tests import JobServTest


class HealthApiTest(JobServTest):
    def test_run_health(self):
        self.create_projects("proj-1")
        p = Project.query.first()
        b = Build.create(p)
        db.session.add(Run(b, "queued-1"))
        db.session.add(Run(b, "queued-2"))

        w1 = Worker("worker1", "distro", 12, 12, "amd", "key", 2, "tag")
        db.session.add(w1)
        w2 = Worker("worker2", "distro", 12, 12, "amd", "key", 2, "tag")
        db.session.add(w2)
        db.session.flush()

        r = Run(b, "run1-worker1")
        r.worker = w1
        r.status = BuildStatus.UPLOADING
        db.session.add(r)

        r = Run(b, "run1-worker2")
        r.worker = w2
        r.status = BuildStatus.RUNNING
        db.session.add(r)
        r = Run(b, "run2-worker2")
        r.worker = w2
        r.status = BuildStatus.RUNNING
        db.session.add(r)
        r = Run(b, "run3rworker2")
        r.worker = w2
        r.status = BuildStatus.CANCELLING
        db.session.add(r)
        db.session.commit()

        r = self.client.get("/health/runs/")
        self.assertEqual(200, r.status_code)
        d = json.loads(r.data.decode())["data"]

        self.assertEqual(2, d["health"]["statuses"]["QUEUED"])
        self.assertEqual(2, d["health"]["statuses"]["RUNNING"])
        self.assertEqual(1, d["health"]["statuses"]["UPLOADING"])

        self.assertEqual(1, len(d["health"]["RUNNING"]["worker1"]))
        self.assertEqual(3, len(d["health"]["RUNNING"]["worker2"]))

        self.assertEqual(2, len(d["health"]["QUEUED"]))
