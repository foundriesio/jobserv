# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

import datetime
import os
import shutil
import tempfile
import time

import jobserv.models
import jobserv.worker

from unittest.mock import patch

from jobserv.models import db, Build, BuildStatus, Project, Run, RunEvents, Worker
from jobserv.settings import SURGE_SUPPORT_RATIO
from jobserv import worker as worker_module
from jobserv.worker import (
    _check_acked,
    _check_queue,
    _check_stuck,
    _check_workers,
    _check_cancelled,
)

from tests import JobServTest


class TestWorkerMonitor(JobServTest):
    def setUp(self):
        super().setUp()
        jobserv.models.WORKER_DIR = tempfile.mkdtemp()
        jobserv.worker.SURGE_FILE = os.path.join(
            jobserv.models.WORKER_DIR, "enable_surges"
        )
        self.addCleanup(shutil.rmtree, jobserv.models.WORKER_DIR)
        self.worker = Worker("w1", "d", 1, 1, "amd64", "k", 1, "amd64")
        self.worker.enlisted = True
        self.worker.online = True
        db.session.add(self.worker)
        db.session.commit()

    def test_offline_no_pings(self):
        _check_workers()
        db.session.refresh(self.worker)
        self.assertFalse(self.worker.online)

    def test_offline(self):
        self.worker.ping()
        offline = time.time() - 81  # 81 seconds old
        os.utime(self.worker.pings_log, (offline, offline))
        _check_workers()
        db.session.refresh(self.worker)
        self.assertFalse(self.worker.online)

    @patch("jobserv.worker.WORKER_ROTATE_PINGS_LOG")
    def test_rotate(self, rotate):
        # enable rotation
        rotate.return_value = True
        # create a big file
        self.worker.ping()
        with open(self.worker.pings_log, "a") as f:
            f.write("1" * 1024 * 1024)
        _check_workers()
        self.assertEqual(0, os.stat(self.worker.pings_log).st_size)
        # there should be two files now
        self.assertEqual(2, len(os.listdir(os.path.dirname(self.worker.pings_log))))

        # we should still be online
        db.session.refresh(self.worker)
        self.assertTrue(self.worker.online)

    def test_truncate(self):
        # rotation is disabled by default:

        # create a big file
        self.worker.ping()
        with open(self.worker.pings_log, "a") as f:
            f.write("1" * 1024 * 1024)
        _check_workers()
        self.assertEqual(0, os.stat(self.worker.pings_log).st_size)
        # there should be two files now
        self.assertEqual(1, len(os.listdir(os.path.dirname(self.worker.pings_log))))

        # we should still be online
        db.session.refresh(self.worker)
        self.assertTrue(self.worker.online)

    def test_surge_simple(self):
        self.create_projects("proj1")
        b = Build.create(Project.query.all()[0])
        for x in range(SURGE_SUPPORT_RATIO + 1):
            r = Run(b, "run%d" % x)
            r.host_tag = "amd64"
            db.session.add(r)
        db.session.commit()
        _check_queue()
        self.assertTrue(os.path.exists(jobserv.worker.SURGE_FILE + "-amd64"))

        db.session.delete(Run.query.all()[0])
        db.session.commit()
        worker_module.DETECT_FLAPPING = False
        _check_queue()
        self.assertFalse(os.path.exists(jobserv.worker.SURGE_FILE + "-amd64"))

    def test_surge_complex(self):
        # we'll have two amd64 workers and one armhf
        worker = Worker("w2", "d", 1, 1, "amd64", "k", 1, "amd64")
        worker.enlisted = True
        worker.online = True
        db.session.add(worker)
        worker = Worker("w3", "d", 1, 1, "armhf", "k", 1, "armhf")
        worker.enlisted = True
        worker.online = True
        db.session.add(worker)
        db.session.commit()

        self.create_projects("proj1")
        b = Build.create(Project.query.all()[0])
        for x in range(SURGE_SUPPORT_RATIO + 1):
            r = Run(b, "amd%d" % x)
            r.host_tag = "amd64"
            db.session.add(r)
            r = Run(b, "armhf%d" % x)
            r.host_tag = "armhf"
            db.session.add(r)

        db.session.commit()
        _check_queue()
        self.assertFalse(os.path.exists(jobserv.worker.SURGE_FILE + "-amd64"))
        self.assertTrue(os.path.exists(jobserv.worker.SURGE_FILE + "-armhf"))

        # get us under surge for armhf
        db.session.delete(Run.query.filter(Run.host_tag == "armhf").first())
        # and over surge for amd64
        for x in range(SURGE_SUPPORT_RATIO + 1):
            r = Run(b, "run%d" % x)
            r.host_tag = "amd64"
            db.session.add(r)

        db.session.commit()
        worker_module.DETECT_FLAPPING = False
        _check_queue()
        self.assertTrue(os.path.exists(jobserv.worker.SURGE_FILE + "-amd64"))
        self.assertFalse(os.path.exists(jobserv.worker.SURGE_FILE + "-armhf"))

        # make sure we know about deleted workers
        worker.deleted = True
        db.session.commit()
        _check_queue()
        self.assertTrue(os.path.exists(jobserv.worker.SURGE_FILE + "-armhf"))

    @patch("jobserv.worker.notify_run_terminated")
    @patch("jobserv.worker._update_run")
    def test_stuck(self, update_run, notify):
        """Ensure stuck runs are failed."""
        self.create_projects("proj1")
        b = Build.create(Project.query.all()[0])
        r = Run(b, "bla")
        r.status = BuildStatus.RUNNING
        db.session.add(r)
        db.session.commit()
        e = RunEvents(r, BuildStatus.RUNNING)
        e.time = datetime.datetime.utcnow() - datetime.timedelta(hours=13)
        db.session.add(e)
        db.session.commit()

        _check_stuck()
        self.assertEqual("bla", notify.call_args[0][0].name)
        notify.rest_mock()

        r.status = BuildStatus.CANCELLING
        e = RunEvents(r, BuildStatus.RUNNING)
        e.time = datetime.datetime.utcnow() - datetime.timedelta(hours=13)
        db.session.add(e)
        db.session.commit()

        _check_stuck()
        self.assertEqual("bla", notify.call_args[0][0].name)
        self.assertEqual("bla", update_run.call_args[0][0].name)

    @patch("jobserv.worker._update_run")
    def test_cancelled(self, update):
        """Ensure runs that were cancelled before they were assigned to a
        worker are failed."""
        self.create_projects("proj1")
        b = Build.create(Project.query.all()[0])
        r = Run(b, "bla")
        r.status = BuildStatus.CANCELLING
        db.session.add(r)
        db.session.commit()

        _check_cancelled()

        self.assertEqual("FAILED", update.call_args[1]["status"])

    def test_running_acked(self):
        """Ensure we can detect runs that have not been acked by worker."""
        self.create_projects("proj1")
        b = Build.create(Project.query.all()[0])
        r = Run(b, "bla")
        r.status = BuildStatus.RUNNING
        db.session.add(r)
        db.session.flush()
        e = RunEvents(r, BuildStatus.RUNNING)
        db.session.add(e)
        db.session.commit()

        # It's only been 10 seconds - we shouldn't do anything
        e.time = datetime.datetime.utcnow() - datetime.timedelta(seconds=10)
        db.session.commit()
        _check_acked()
        self.assertEqual(BuildStatus.RUNNING, Run.query.get(r.id).status)

        # We've gone passed 15 seconds, we should QUEUE this run again
        e.time = datetime.datetime.utcnow() - datetime.timedelta(seconds=16)
        db.session.commit()
        _check_acked()
        self.assertEqual(BuildStatus.QUEUED, Run.query.get(r.id).status)

        # This is a real run in progress
        r.status = BuildStatus.RUNNING
        r.running_acked = 1
        db.session.commit()
        _check_acked()
        self.assertEqual(BuildStatus.RUNNING, Run.query.get(r.id).status)
