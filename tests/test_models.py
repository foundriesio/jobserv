# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

import unittest.mock
from sqlalchemy.exc import IntegrityError

from jobserv.models import (
    db,
    get_cumulative_status,
    Build,
    BuildStatus,
    Project,
    Run,
    Test,
    TestResult,
)

from tests import JobServTest


class ProjectTest(JobServTest):
    def test_simple(self):
        db.session.add(Project("job1"))
        db.session.commit()
        projs = Project.query.all()
        self.assertEqual(1, len(projs))
        self.assertEqual("job1", projs[0].name)

    def test_project_unique(self):
        db.session.add(Project("job1"))
        db.session.commit()

        with self.assertRaises(IntegrityError):
            db.session.add(Project("job1"))
            db.session.commit()


class BuildTest(JobServTest):
    def setUp(self):
        super(BuildTest, self).setUp()
        self.create_projects("job-1")
        self.proj = Project.query.filter_by(name="job-1").first_or_404()

    def test_simple(self):
        db.session.add(Build(self.proj, 1))
        db.session.commit()
        builds = Build.query.all()
        self.assertEqual(1, len(builds))
        self.assertEqual(1, builds[0].build_id)
        self.assertEqual("QUEUED", builds[0].status.name)

    def test_unique_build_id(self):
        self.create_projects("job-2")
        proj2 = Project.query.filter_by(name="job-2").first_or_404()

        # both jobs should be able to have build_id=1
        db.session.add(Build(self.proj, 1))
        db.session.add(Build(proj2, 1))
        db.session.commit()

        # now make sure build_id=1 can't be repeated
        with self.assertRaises(IntegrityError):
            db.session.add(Build(self.proj, 1))
            db.session.commit()

    def test_as_json(self):
        import jobserv.models

        orig = jobserv.models.BUILD_URL_FMT
        try:
            jobserv.models.BUILD_URL_FMT = "foo {build} | {project}"
            b = Build.create(self.proj)
            data = b.as_json()
            self.assertEqual("foo 1 | job-1", data["web_url"])
        finally:
            jobserv.models.BUILD_URL_FMT = orig

    def test_create_build(self):
        b = Build.create(self.proj)
        self.assertEqual(1, b.build_id)
        b = Build.create(self.proj)
        self.assertEqual(2, b.build_id)
        db.session.add(Build(self.proj, 99))
        db.session.commit()
        b = Build.create(self.proj)
        self.assertEqual(100, b.build_id)

    @unittest.mock.patch("jobserv.models.Build._try_build_ids")
    def test_create_build_concurrency(self, try_build_ids):
        try_build_ids.return_value = (1, 2, 3)
        # create a "collision" ie build 1
        db.session.add(Build(self.proj, 1))
        db.session.commit()

        b = Build.create(self.proj)
        self.assertEqual(2, b.build_id)

    def test_build_events(self):
        b = Build.create(self.proj)
        self.assertEqual(["QUEUED"], [x.status.name for x in b.status_events])

    def test_cumulative_status(self):
        items = (
            unittest.mock.Mock(status=BuildStatus.QUEUED),
            unittest.mock.Mock(status=BuildStatus.QUEUED),
        )
        self.assertEqual(BuildStatus.QUEUED, get_cumulative_status(items))

        items = (
            unittest.mock.Mock(status=BuildStatus.RUNNING),
            unittest.mock.Mock(status=BuildStatus.QUEUED),
        )
        self.assertEqual(BuildStatus.RUNNING, get_cumulative_status(items))

        items = (
            unittest.mock.Mock(status=BuildStatus.RUNNING),
            unittest.mock.Mock(status=BuildStatus.FAILED),
        )
        self.assertEqual(
            BuildStatus.RUNNING_WITH_FAILURES, get_cumulative_status(items)
        )

        items = (
            unittest.mock.Mock(status=BuildStatus.RUNNING),
            unittest.mock.Mock(status=BuildStatus.PASSED),
        )
        self.assertEqual(BuildStatus.RUNNING, get_cumulative_status(items))

        items = (
            unittest.mock.Mock(status=BuildStatus.FAILED),
            unittest.mock.Mock(status=BuildStatus.PASSED),
        )
        self.assertEqual(BuildStatus.FAILED, get_cumulative_status(items))

        items = (
            unittest.mock.Mock(status=BuildStatus.PASSED),
            unittest.mock.Mock(status=BuildStatus.PASSED),
        )
        self.assertEqual(BuildStatus.PASSED, get_cumulative_status(items))

        items = (
            unittest.mock.Mock(status=BuildStatus.SKIPPED),
            unittest.mock.Mock(status=BuildStatus.PASSED),
        )
        self.assertEqual(BuildStatus.PASSED, get_cumulative_status(items))

        items = (
            unittest.mock.Mock(status=BuildStatus.QUEUED),
            unittest.mock.Mock(status=BuildStatus.CANCELLING),
        )
        self.assertEqual(
            BuildStatus.RUNNING_WITH_FAILURES, get_cumulative_status(items)
        )

        items = (
            unittest.mock.Mock(status=BuildStatus.PASSED),
            unittest.mock.Mock(status=BuildStatus.CANCELLING),
        )
        self.assertEqual(
            BuildStatus.RUNNING_WITH_FAILURES, get_cumulative_status(items)
        )


class RunTest(JobServTest):
    def setUp(self):
        super(RunTest, self).setUp()
        self.create_projects("job-1")
        self.proj = Project.query.filter_by(name="job-1").first_or_404()
        self.build = Build.create(self.proj)

    def test_simple(self):
        db.session.add(Run(self.build, "name"))
        db.session.commit()
        runs = Run.query.all()
        self.assertEqual(1, len(runs))
        self.assertEqual("QUEUED", runs[0].status.name)

    def test_run_name(self):
        # can't have the same named run for a single build
        db.session.add(Run(self.build, "name"))
        db.session.add(Run(self.build, "name"))
        with self.assertRaises(IntegrityError):
            db.session.commit()

    def test_as_json(self):
        import jobserv.models

        orig = jobserv.models.RUN_URL_FMT
        r = Run(self.build, "name1")
        db.session.add(r)
        db.session.commit()
        try:
            jobserv.models.RUN_URL_FMT = "bar {build} | {run} | {project}"
            Build.create(self.proj)
            data = r.as_json()
            self.assertEqual("bar 1 | name1 | job-1", data["web_url"])
        finally:
            jobserv.models.BUILD_URL_FMT = orig

    def test_build_status_queued(self):
        db.session.add(Run(self.build, "name1"))
        db.session.add(Run(self.build, "name2"))
        db.session.commit()

        db.session.refresh(self.build)
        self.build.refresh_status()
        self.assertEqual(BuildStatus.QUEUED, self.build.status)

    def test_build_status_running(self):
        db.session.add(Run(self.build, "name1"))
        r = Run(self.build, "name2")
        r.status = BuildStatus.RUNNING
        db.session.add(r)
        db.session.commit()

        db.session.refresh(self.build)
        self.build.refresh_status()
        self.assertEqual(BuildStatus.RUNNING, self.build.status)

    def test_build_status_running_failed(self):
        db.session.add(Run(self.build, "name1"))
        r = Run(self.build, "name2")
        r.status = BuildStatus.FAILED
        db.session.add(r)
        db.session.commit()

        db.session.refresh(self.build)
        self.build.refresh_status()
        self.assertEqual(BuildStatus.RUNNING_WITH_FAILURES, self.build.status)

    def test_build_status_passed(self):
        r = Run(self.build, "name")
        r.status = BuildStatus.PASSED
        db.session.add(r)
        db.session.commit()

        db.session.refresh(self.build)
        self.build.refresh_status()
        self.assertEqual(BuildStatus.PASSED, self.build.status)

    def test_build_status_failed(self):
        r = Run(self.build, "name1")
        r.status = BuildStatus.PASSED
        db.session.add(r)
        r = Run(self.build, "name2")
        r.status = BuildStatus.FAILED
        db.session.add(r)
        db.session.commit()

        db.session.refresh(self.build)
        self.build.refresh_status()
        self.assertEqual(BuildStatus.FAILED, self.build.status)
        self.assertEqual(
            ["QUEUED", "FAILED"], [x.status.name for x in self.build.status_events]
        )


class TestsTest(JobServTest):
    def setUp(self):
        super().setUp()
        self.create_projects("job-1")
        self.proj = Project.query.filter_by(name="job-1").first_or_404()
        self.build = Build.create(self.proj)
        self.run = Run(self.build, "name1")
        self.run.status = BuildStatus.RUNNING
        db.session.add(self.run)
        db.session.commit()

    def test_empty(self):
        self.assertEqual([], self.run.tests)

    def test_same_name(self):
        db.session.add(Test(self.run, "test-name", "http://foo.com"))
        db.session.add(Test(self.run, "test-name", "http://foo.com"))
        db.session.commit()

    def test_not_complete(self):
        t = Test(self.run, "test-name", "http://foo.com")
        db.session.add(t)
        db.session.commit()
        db.session.add(TestResult(t, "test-result-1", "http://foo.com/tr"))
        db.session.add(t)
        db.session.add(TestResult(t, "test-result-2", "http://foo.com/tr"))
        db.session.refresh(t)
        self.assertFalse(t.complete)

    def test_complete(self):
        t = Test(self.run, "test-name", "http://foo.com")
        t.status = BuildStatus.PASSED
        db.session.add(t)
        db.session.commit()
        tr = TestResult(t, "test-result-1", "http://foo.com/tr")
        tr.status = "PASSED"
        db.session.add(tr)
        db.session.refresh(t)
        self.assertTrue(t.complete, "status = %s" % t.status)

        tr.status = BuildStatus.RUNNING
        db.session.commit()
        self.assertFalse(t.complete, "status = %s" % t.status)

    def test_not_queued(self):
        """Tests should never set a Run to the QUEUED state"""
        db.session.add(Test(self.run, "test-name", "http://foo.com"))
        t = Test(self.run, "test-name", "http://foo.com")
        db.session.add(t)
        db.session.commit()
        run_status = t.set_status(BuildStatus.PASSED)
        self.assertEqual(BuildStatus.RUNNING, run_status)
