# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

import os
import json
import shutil
import subprocess
import tempfile

from unittest import TestCase, mock, skipIf

from jobserv_runner import cmd
from jobserv_runner.handlers.simple import HandlerError, SimpleHandler
from jobserv_runner.jobserv import RunCancelledError


class TestHandler(SimpleHandler):
    _jobserv = mock.Mock()

    def __init__(self, worker_dir, run_dir, jobserv, rundef):
        jobserv._api_key = "mocked not secure"
        super().__init__(worker_dir, run_dir, jobserv, rundef)
        self.action()

    @classmethod
    def get_jobserv(clazz, rundef):
        return clazz._jobserv

    def docker_pull(self):
        pass

    def docker_run(self, mounts):
        pass


class SimpleHandlerTest(TestCase):
    def setUp(self):
        super().setUp()

        TestHandler.action = None

        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir)

        self.rdir = os.path.join(self.tmpdir, "run")
        self.wdir = os.path.join(self.tmpdir, "worker")
        os.mkdir(self.rdir)
        os.mkdir(self.wdir)
        self.handler = SimpleHandler(self.wdir, self.rdir, mock.Mock(), None)

    def test_execute_unexpected(self):
        """Ensure we do proper logging for unexpected errors."""
        self.assertFalse(TestHandler.execute(self.wdir, self.rdir, None))
        self.assertEqual("FAILED", TestHandler._jobserv.update_status.call_args[0][0])
        self.assertIn(
            "TypeError: 'NoneType' object is not callable",
            TestHandler._jobserv.update_status.call_args[0][1],
        )

    def test_execute_expected(self):
        """Ensure we do proper logging for for HandlerErrors."""

        def raise_handler(self):
            raise HandlerError("foo bar bam")

        TestHandler.action = raise_handler
        self.assertFalse(TestHandler.execute(self.wdir, self.rdir, None))
        self.assertEqual("FAILED", TestHandler._jobserv.update_status.call_args[0][0])
        self.assertEqual(
            "foo bar bam", TestHandler._jobserv.update_status.call_args[0][1]
        )

    def test_execute_cancelled(self):
        """Ensure we handle a cancellation properly."""

        def raise_cancel(self):
            raise RunCancelledError()

        TestHandler.action = raise_cancel
        self.assertFalse(TestHandler.execute(self.wdir, self.rdir, None))
        self.assertEqual("FAILED", TestHandler._jobserv.update_status.call_args[0][0])
        self.assertIn(
            "Run cancelled from server",
            TestHandler._jobserv.update_status.call_args[0][1],
        )

    def test_execute_success(self):
        """Ensure we do proper logging of a run that passes"""

        def good_handler(self):
            return

        TestHandler.action = good_handler
        rundef = {
            "timeout": 1,
            "script": "#!/bin/sh\n echo foo",
            "run_url": "http://for-simulator-instructions/run",
            "runner_url": "http://for-simulator-instructions/runner",
        }
        self.assertTrue(TestHandler.execute(self.wdir, self.rdir, rundef))

    def test_exec(self):
        self.output = b""

        def update_run(buf, retry=2):
            self.output += buf
            return True

        def update_status(status, message):
            self.output += ("%s: %s" % (status, message)).encode()

        self.handler.jobserv.SIMULATED = None
        self.handler.jobserv.update_run = update_run
        self.handler.jobserv.update_status = update_status

        with self.handler.log_context("test-execzZZ") as log:
            self.assertTrue(log.exec(["/bin/echo", "abcdefg"]))
            self.assertFalse(log.exec(["/bin/false"]))

        lines = self.output.decode().splitlines()
        self.assertIn("test-execzZZ", lines[0])
        self.assertEqual("abcdefg", lines[1])

    def test_exec_hung(self):
        self.hung = False
        self.output = b""
        cmd.HANG_DETECT_SECONDS = 1

        def update_run(buf, retry=2):
            self.output += buf
            return True

        def update_status(status, message):
            self.output += ("%s: %s" % (status, message)).encode()

        def hung_cb():
            self.hung = True

        self.handler.jobserv.SIMULATED = None
        self.handler.jobserv.update_run = update_run
        self.handler.jobserv.update_status = update_status

        with self.handler.log_context("test-execzZZ") as log:
            self.assertTrue(log.exec(["/bin/sleep", "1.1s"], hung_cb=hung_cb))

        self.assertTrue(self.hung)

    @mock.patch("jobserv_runner.handlers.simple.stream_cmd")
    def test_docker_pull_fails(self, stream_cmd):
        """Ensure we handle a bad container pull properly."""
        stream_cmd.side_effect = subprocess.CalledProcessError("foo", "cmd")
        self.handler.rundef = {"container": "foo"}
        with self.assertRaises(HandlerError):
            self.handler.docker_pull()

    @mock.patch("os.path.expanduser")
    def test_docker_login(self, expand_user):
        """Ensure we handle private containers."""
        self.handler.rundef = {
            "container": "server.com/foo",
            "container-auth": "token",
            "secrets": {"token": "1234"},
        }
        path = os.path.join(self.tmpdir, "foo.json")
        contents = {"auths": {}}
        expand_user.return_value = path
        with open(path, "w") as f:
            json.dump(contents, f)
        with self.handler.docker_login():
            with open(path) as f:
                data = json.load(f)
                self.assertEqual("1234", data["auths"]["server.com"]["auth"])
        with open(path) as f:
            self.assertEqual(contents, json.load(f))

    def test_prepare_mounts_null(self):
        """Ensure we can handle Null values"""
        self.handler.rundef = {
            "secrets": None,
            "persistent-volumes": None,
            "script": "foo",
            "run_url": "http://for-simulator-instructions/run",
        }
        self.handler.prepare_mounts()

    def test_prepare_mounts(self):
        """Ensure we create mount directories as needed"""
        self.handler.rundef = {
            "project": "p",
            "secrets": {"foo": "foo-secret-value"},
            "persistent-volumes": {"blah": "/foo"},
            "script": "foo",
            "run_url": "http://for-simulator-instructions/run",
        }
        self.handler.prepare_mounts()

        secret_file = os.path.join(self.handler.run_dir, "secrets", "foo")
        with open(secret_file) as f:
            self.assertEqual("foo-secret-value", f.read())

        vol = os.path.join(self.handler.worker_dir, "volumes", "p/blah")
        self.assertTrue(os.path.isdir(vol))

    def test_prepare_mounts_unexpected(self):
        """Ensure the run is failed upon unexpected prepare_mounts error"""

        def _fake(*args):
            raise RuntimeError("foo bar")

        self.handler._prepare_secrets = _fake
        self.handler.rundef = {
            "secrets": None,
            "persistent-volumes": None,
        }
        with self.assertRaisesRegex(RuntimeError, "foo bar"):
            self.handler.prepare_mounts()
        msg = self.handler.jobserv.update_run.call_args[0][0]
        self.assertIn(b"\n   |RuntimeError: foo bar\n", msg)

    def test_prepare_script_repo(self):
        """Ensure we set up a proper environment for script-repo runs."""
        # just clone ourself
        repo = os.path.realpath(os.path.join(os.path.dirname(__file__), "../../"))
        self.handler.rundef = {
            "project": "p",
            "secrets": {"foo": "foo-secret-value"},
            "persistent-volumes": {"blah": "/foo"},
            "run_url": "http://for-simulator-instructions/run",
            "script-repo": {
                # just clone ourself
                "clone-url": repo,
                "git-ref": "master",
                "path": "unit-test.sh",
            },
        }
        self.handler.prepare_mounts()

    @skipIf(not os.path.exists("/var/lib/docker"), "Docker not available")
    def test_docker_run(self):
        """Sort of a long test, but it really executes the whole thing."""
        cmd.HANG_DETECT_SECONDS = 1
        self.handler.rundef = {
            "project": "p",
            "container": "busybox",
            "persistent-volumes": {"blah": "/foo"},
            "run_url": "http://for-simulator-instructions/run",
            "script": """#!/bin/sh -e\n
                      echo "running"
                      sleep 3s
                      echo "persistent" > /foo/p.txt
                      echo "saved content" > /archive/f.txt
                       """,
        }
        self.handler.docker_pull()
        mounts = self.handler.prepare_mounts()
        self.assertTrue(self.handler.docker_run(mounts))
        with open(os.path.join(self.wdir, "volumes/p/blah", "p.txt")) as f:
            self.assertEqual("persistent\n", f.read())

        with open(os.path.join(self.rdir, "archive/f.txt")) as f:
            self.assertEqual("saved content\n", f.read())

        found = False
        for artifact in os.listdir(os.path.join(self.rdir, "archive")):
            if artifact.startswith("hung-"):
                found = True
                with open(os.path.join(self.rdir, "archive", artifact)) as f:
                    content = f.read()
                    self.assertIn("PS OUTPUT", content)
                    self.assertIn("TOP OUTPUT", content)
                    self.assertIn("1 root      0:00 {do_run}", content)
                break
        self.assertTrue(found)

    def test_junit_tests(self):
        archive = os.path.join(self.rdir, "archive")
        os.mkdir(archive)
        shutil.copy(os.path.join(os.path.dirname(__file__), "junit.xml"), archive)
        self.handler.jobserv.add_test.return_value = None
        self.assertTrue(self.handler.test_suite_errors())
        self.assertEqual(
            "Sanitycheck", self.handler.jobserv.add_test.call_args_list[0][0][0]
        )
        self.assertEqual(
            "junit.xml skipped=7", self.handler.jobserv.add_test.call_args_list[0][0][1]
        )
        self.assertEqual(
            "FAILED", self.handler.jobserv.add_test.call_args_list[0][0][2]
        )
        results = self.handler.jobserv.add_test.call_args_list[0][0][3]
        self.assertEqual(397, len(results), results)
        fails = passes = skips = 0
        for x in results:
            if x["status"] == "PASSED":
                passes += 1
            elif x["status"] == "SKIPPED":
                skips += 1
            else:
                fails += 1
        self.assertEqual(1, fails)
        self.assertEqual(7, skips)
        self.assertEqual(389, passes)
        self.assertIn("Booting Zephyr", results[87]["output"])
