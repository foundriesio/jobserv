# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

import contextlib
import fcntl
import glob
import json
import io
import xml.etree.ElementTree as ET
import os
import shutil
import signal
import subprocess
import time
import traceback
import urllib.parse
import uuid

from jobserv_runner.cmd import stream_cmd
from jobserv_runner.jobserv import JobServApi, RunCancelledError
from jobserv_runner.logging import ContextLogger

passed_msg = r"""Runner has completed
            _  _
           | \/ |
        \__|____|__/
          |  o  o|           Thumbs Up
          |___\/_|_____||_
          |       _____|__|
          |      |
          |______|
          | |  | |
          | |  | |
          |_|  |_|
"""
failed_msg = r"""Runner has completed
          ________
          |  o  o|           Thumbs Down
          |___/\_|________
          |       _____|__|
          |      |     ||
          |______|
          | |  | |
          | |  | |
          |_|  |_|
"""
# Courtesy of: http://patorjk.com/software/taag/#p=display&f=Doom&t=Rebooting
reboot_msg = r"""
   ______     _                 _   _               _   _   _
   | ___ \   | |               | | (_)             | | | | | |
   | |_/ /___| |__   ___   ___ | |_ _ _ __   __ _  | | | | | |
   |    // _ \ '_ \ / _ \ / _ \| __| | '_ \ / _` | | | | | | |
   | |\ \  __/ |_) | (_) | (_) | |_| | | | | (_| | |_| |_| |_|
   \_| \_\___|_.__/ \___/ \___/ \__|_|_| |_|\__, | (_) (_) (_)
                                             __/ |
                                            |___/
"""


class HandlerError(Exception):
    """An exception that can be used to tell SimpleHandler.main that the error
    has already been properly logged, and we just need to fail the run."""


class RunTimeoutError(HandlerError):
    pass


class JobServLogger(ContextLogger):
    def __init__(self, context, jobserv):
        super().__init__(context)
        self.jobserv = jobserv

    def __exit__(self, type, value, tb):
        if type == RunTimeoutError:
            return  # we handle logging of this properly

        super().__exit__(type, value, tb)
        self.jobserv.update_run(self.io.getvalue().encode(), retry=5)
        if tb:
            # flag this so we know the stack trace was printed
            value.handler_logged = True

    def exec(self, cmd_args, cwd=None, env=None, hung_cb=None):
        buf = self.io.getvalue()
        if buf:
            # send any buffer data to server
            self.jobserv.update_run(buf.encode())
            self.io = io.StringIO()

        def cb(buff):
            # dont stream this to local logs, just to server
            if self.jobserv.SIMULATED:
                # we are in simulator mode, dump to stdout
                return os.write(1, buff)
            return self.jobserv.update_run(buff)

        try:
            stream_cmd(cb, cmd_args, cwd, env, hung_cb)
            return True
        except subprocess.CalledProcessError as e:
            if e.output:
                if not self.jobserv.update_run(e.output, retry=8):
                    self.error("unable to update run output: %s", e.output)
            return False

    def exec_retriable(self, cmd_args, cwd=None, env=None, hung_cb=None):
        for i in range(4):
            if i:
                self.error("command failed, retrying in %d seconds", i)
                time.sleep(i)
            if self.exec(cmd_args, cwd, env, hung_cb):
                return True
        return False

    def _write(self, msg):
        if not self.jobserv.SIMULATED:
            self.io.write(msg)
            buf = self.io.getvalue()
            try:
                if buf and self.jobserv.update_run(buf.encode(), retry=1):
                    self.io = io.StringIO()
            except RunCancelledError as e:
                # the call to self.jobserv.update_run, if successful, might
                # get this exception. This means we've uploaded self.io to
                # the server and should clear it here so that we don't send
                # the buffer twice.
                self.io = io.StringIO()
                raise e
        else:
            self.io.write(msg)


class SimpleHandler(object):
    """Executes the steps needed to do a "simple" trigger-type rundef"""

    class RebootAndContinue(Exception):
        """Tells the jobserv_worker script to save this run, reboot the system,
        and continue it after reboot."""

        def __init__(self, cold):
            super().__init__()
            self.cold = cold

    def __init__(self, worker_dir, run_dir, jobserv, rundef):
        self.worker_dir = worker_dir
        self.run_dir = run_dir
        self.jobserv = jobserv
        self.rundef = rundef
        self.container_cwd = "/"

    def log_context(self, context):
        return JobServLogger(context, self.jobserv)

    @contextlib.contextmanager
    def docker_login(self):
        auth = self.rundef.get("container-auth")
        if not auth:
            # No authentication needed
            yield
            return

        auth = self.rundef["secrets"][auth]
        server = self.rundef["container"].split("/", 1)[0]

        path = os.path.expanduser("~/.docker/config.json")
        try:
            os.mkdir(os.path.dirname(path))
        except FileExistsError:
            pass
        with open(path, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            try:
                data = json.load(f)
            except Exception:
                data = {"auths": {}}
            data["auths"][server] = {"auth": auth}
            f.seek(0)
            try:
                f.truncate()
                json.dump(data, f, indent=2)
                f.flush()
                yield
            finally:
                del data["auths"][server]
                f.seek(0)
                f.truncate()
                json.dump(data, f, indent=2)

    def docker_pull(self):
        container = self.rundef["container"]

        logctx = self.log_context("Pulling container: " + container)
        login = self.docker_login()
        with logctx as log, login:
            for x in (0, 2, 4):  # try three times with these back-off vals
                if x:
                    log.warn("Unable to pull container, retrying in %ds", x)
                    time.sleep(x)
                if log.exec(["docker", "pull", container]):
                    return
            raise HandlerError("Unable to pull container: " + container)

    def docker_run(self, mounts):
        env_file = os.path.join(self.run_dir, "docker-env")
        envvars = self.rundef.get("env") or {}

        with self.log_context("Setting up container environment") as log:
            env = ["%s=%s" % (k, v) for k, v in envvars.items()]
            log.info("Container environment variables:\n  %s", "\n  ".join(env))
            with open(env_file, "w") as f:
                # environment variable might have \n's so escape them to
                # make the docker-env file correct
                for val in env:
                    f.write(val.replace("\n", "\\n") + "\n")
        name = str(uuid.uuid4())

        with self.log_context("Running script inside container") as log:

            def hung_cb():
                outfile = "archive/hung-%s.txt" % time.time()
                log.info("cmd seems hung, dumping diagnostic info to %s", outfile)
                outfile = os.path.join(self.run_dir, outfile)
                with open(outfile, "w") as f:
                    diag_cmd = "echo '# PS OUTPUT'; /bin/ps -e; echo; echo '# TOP OUTPUT'; top -b -n1"  # NOQA
                    subprocess.call(
                        ["docker", "exec", name, "/bin/sh", "-c", diag_cmd],
                        stdout=f,
                        stderr=f,
                    )

            cmd = ["docker", "run"]
            cmd.extend(["--name", name])
            cmd.extend(["-w", self.container_cwd])
            cmd.extend(["--env-file", env_file])
            if self.rundef.get("privileged"):
                log.info('Running with "--privileged"')
                cmd.append("--privileged")
            if self.rundef.get("cap-add"):
                adds = self.rundef.get("cap-add")
                log.info("Running with cap-adds: %s", adds)
                for add in adds:
                    cmd.extend(["--cap-add", add])
            if self.rundef.get("cap-drop"):
                drops = self.rundef.get("cap-drop")
                log.info("Running with cap-drops: %s", drops)
                for drop in drops:
                    cmd.extend(["--cap-drop", drop])
            if self.rundef.get("max-mem-bytes"):
                maxbytes = self.rundef.get("max-mem-bytes")
                log.info("Running with --memory=%d", maxbytes)
                cmd.extend(["--memory", str(maxbytes)])
            if self.rundef.get("container-user"):
                user = self.rundef.get("container-user")
                log.info("Overriding container user to be: %s", user)
                cmd.extend(["--user", user])
            if self.rundef.get("container-entrypoint") is not None:
                ep = self.rundef.get("container-entrypoint")
                log.info("Overriding container entrypointpoint to be: %s", ep)
                cmd.extend(["--entrypoint", ep])
            cmd.extend(["-v" + ":".join(x) for x in mounts])
            cmd.extend([self.rundef["container"], self._container_command])
            try:
                return log.exec(cmd, hung_cb=hung_cb)
            except RunTimeoutError:
                log.error("Run has timed out, killing containter")
                log.exec(["docker", "kill", name])
                raise

    def _prepare_secrets(self, log):
        """Create the /secrets folder that will be bind-mounted by docker."""
        secrets = os.path.join(self.run_dir, "secrets")
        if not os.path.exists(secrets):
            os.mkdir(secrets)  # probably a rebooted run

        # Deal with docker rate-limiting:
        try:
            with open(os.path.expanduser("~/.docker/config.json")) as fin:
                # copy this into container. It may have hub.docker.io read-only
                # credentials required to avoid rate-limited pulls
                secret = os.path.join(secrets, "docker_host_config.json")
                with open(secret, "w") as fout:
                    fout.write(fin.read())
        except FileNotFoundError:
            log.info("Default docker configuration detected")

        for secret, value in (self.rundef.get("secrets") or {}).items():
            log.info("Creating secret: %s", secret)
            with open(os.path.join(secrets, secret), "w") as f:
                f.write(value)
        return [(secrets, "/secrets")]

    def _prepare_volumes(self, log):
        """Create persistent volumes that will be bind-mounted by docker."""
        volumes = []
        for v, path in (self.rundef.get("persistent-volumes") or {}).items():
            p = os.path.join(self.worker_dir, "volumes", self.rundef["project"], v)
            if not os.path.exists(p):
                os.makedirs(p)
            log.info("Creating volume: %s", p)
            volumes.append((p, path))

        for host, cont in (self.rundef.get("shared-volumes") or {}).items():
            log.info("Adding shared volume: %s", cont)
            volumes.append((host, cont, "ro"))

        return volumes

    def _prepare_netrc(self):
        """Create a netrc for certain secrets.
        This is handy for Android repo style builds that exist in a private
        repository. By setting this in .netrc everything just works.
        """
        netrc = os.path.join(self.run_dir, ".netrc")
        logctx = self.log_context("Creating container .netrc file")
        with logctx as log, open(netrc, "w") as f:
            log.info("Creating token for jobserv run access")
            machine = urllib.parse.urlparse(self.rundef["run_url"])
            f.write("machine %s\n" % machine.netloc)
            f.write("login jobserv\n")
            f.write("password %s\n" % self.jobserv._api_key)

            secrets = self.rundef.get("secrets") or {}

            token = secrets.get("githubtok")
            if token:
                log.info("Creating a github token entry")
                f.write("machine github.com\n")
                f.write("login %s\n" % token)

            # we have to guess at a gitlab "machine" name
            url = (self.rundef.get("script-repo") or {}).get("clone-url")
            token = secrets.get("gitlabtok")
            if token and url:
                user = secrets["gitlabuser"]
                machine = urllib.parse.urlparse(url).netloc
                if machine != "github.com":
                    log.info("Creating a gitlab token entry for %s", machine)
                    f.write("machine %s\n" % machine)
                    f.write("login %s\npassword %s\n" % (user, token))
            if token:
                user = secrets["gitlabuser"]
                log.info("Creating a gitlab.com token entry")
                f.write("machine gitlab.com\n")
                f.write("login %s\npassword %s\n" % (user, token))

            token = secrets.get("bitbuckettok")
            if token:
                log.info("Creating a bitbucket token entry")
                user = secrets["bitbucketuser"]
                f.write("machine bitbucket.org\n")
                f.write("login %s\npassword %s\n" % (user, token))

            token = secrets.get("qpmtok")
            if token:
                user = secrets["qpmuser"]
                log.info("Creating a qpm token entry")
                f.write("machine qpm-git.qualcomm.com\n")
                f.write("login %s\npassword %s\n" % (user, token))
                log.info("Creating a chipcode token entry")
                f.write("machine chipmaster2.qti.qualcomm.com\n")
                f.write("login %s\npassword %s\n" % (user, token))


        # NOTE: Curl (used by git) doesn't look at the $NETRC environment
        # for overriding the .netrc location. We have to assume the
        # container's $HOME is /root
        curlrc = os.path.join(self.run_dir, ".curlrc")
        with open(curlrc, "w") as f:
            f.write("--netrcfile /root/.netrc")
        return (netrc, "/root/.netrc"), (curlrc, "/root/.curlrc")

    def _clone_script_repo(self, log, repo, dst):
        url = repo["clone-url"]
        log.info("Repo is: %s", url)
        token = repo.get("token")
        if token:
            parts = token.split(":")
            if len(parts) == 1:
                token = self.rundef["secrets"][token]
                p = urllib.parse.urlsplit(url)
                url = p.scheme + "://" + token + "@" + p.netloc + p.path
            elif len(parts) == 2:
                token = self.rundef["secrets"][parts[0]]
                token += ":" + self.rundef["secrets"][parts[1]]
                p = urllib.parse.urlsplit(url)
                url = p.scheme + "://" + token + "@" + p.netloc + p.path

        if os.path.exists(dst):
            shutil.rmtree(dst)  # probably a rebooted run
        if not log.exec(["git", "clone", url, dst]):
            raise HandlerError("Unable to clone repo: " + repo["clone-url"])

        ref = repo.get("git-ref")
        if ref:
            log.info("Git reference is: %s", ref)
            if not log.exec(["git", "checkout", ref], cwd=dst):
                raise HandlerError("Unable to checkout: " + ref)
        sha = (
            subprocess.check_output(["git", "log", "--format=%H", "-1"], cwd=dst)
            .strip()
            .decode()
        )
        log.info("Git HEAD reference is: %s", sha)
        with open(os.path.join(self.run_dir, "archive/script_repo.log"), "w") as f:
            f.write("REPO: %s\n" % repo["clone-url"])
            if ref:
                f.write("REF: %s\n" % ref)
            f.write("SHA: %s\n" % sha)

        if not os.path.exists(os.path.join(dst, repo["path"])):
            raise HandlerError("Script not found in repo: " + repo["path"])
        self._container_command = "/script-repo/" + repo["path"]

    def create_script(self, log):
        """Create the script that will run in the container."""
        reboot_script = self.rundef.get("reboot-script")
        repo = self.rundef.get("script-repo")
        script_dir = os.path.join(self.run_dir, "script-repo")
        if repo and not reboot_script:
            self._clone_script_repo(log, repo, script_dir)
        else:
            if not os.path.exists(script_dir):
                os.mkdir(script_dir)  # probably a rebooted run
            script = os.path.join(script_dir, "do_run")
            if not reboot_script:
                contents = self.rundef["script"]
            else:
                log.info("Using reboot-script for run")
                contents = reboot_script
            with open(script, "w") as f:
                f.write(contents)
                os.fchmod(f.fileno(), 0o555)
            self._container_command = "/script-repo/do_run"
        return script_dir, "/script-repo"

    def log_simulator_instructions(self):
        with self.log_context("Steps to recreate inside simulator") as log:
            msg = """
    wget -O simulate.sh {run}/.simulate.sh
    # wget'ing the file may require the --header flag if the
    # jobserv API requires authentication.
    sh ./simulate.sh
""".format(
                run=self.rundef["run_url"]
            )
            log._write(msg)

    def prepare_mounts(self):
        """Prepare the directories we will bind mount by docker."""
        with self.log_context("Preparing bind mounts") as log:
            mounts = self._prepare_secrets(log) + self._prepare_volumes(log)
        for mount in self._prepare_netrc():
            mounts.append(mount)
        archive = os.path.join(self.run_dir, "archive")
        if not os.path.exists(archive):
            os.mkdir(archive)  # probably a rebooted run
        mounts.append((archive, "/archive"))
        with self.log_context("Preparing script") as log:
            mounts.append(self.create_script(log))

        for pair in mounts:
            src = pair[0]
            if not os.path.exists(src):
                raise HandlerError("Invalid mount path for container: " + src)
        return mounts

    def _on_alarm(self, signum, frame):
        raise RunTimeoutError("Run timed out")

    def start_timer(self):
        """Set an alarm to enforce the job timeout."""
        signal.signal(signal.SIGALRM, self._on_alarm)
        signal.alarm(self.rundef["timeout"] * 60)

    def _junit_errors(self, log, junit_xml):
        try:
            root = ET.fromstring(junit_xml)
        except ET.ParseError as pe:
            log.warn("Unable to parse junit.xml: %s\n" % pe)
            return True

        failed = False
        skipped = 0
        for ts in root.iter("testsuite"):
            results = []
            result = "PASSED"

            for tc in ts:
                status = "PASSED"
                output = None
                child = list(tc)
                if len(child):
                    if child[0].tag in ("error", "failure"):
                        status = "FAILED"
                        result = status
                        failed = True
                        output = ET.tostring(tc, encoding="unicode")
                    elif child[0].tag == "skipped":
                        skipped += 1
                        status = "SKIPPED"
                results.append(
                    {
                        "name": tc.attrib["name"],
                        "context": tc.attrib.get("classname"),
                        "status": status,
                        "output": output,
                    }
                )

            # some runners like junit don't set the "skipped" attribute,
            # so look at both values we've found and pick the biggest one
            attr_skipped = int(ts.attrib.get("skipped", "0"))
            context = "junit.xml skipped=%d" % max(attr_skipped, skipped)
            name = ts.attrib.get("name")
            if not name:
                name = "junit"
            r = self.jobserv.add_test(name, context, result, results)
            if r:
                log.error(
                    "Unable to create test results on server: %d\n%s",
                    r.status_code,
                    r.text,
                )
        if failed:
            log.error("Found failure(s)")
        return failed

    def test_suite_errors(self):
        """Look for artifacts like junit.xml and automatically create Test
        and TestResult objects for the Run."""
        pattern = os.path.join(self.run_dir, "archive/junit.xml*")
        errors = False
        for path in glob.glob(pattern):
            with open(path) as f:
                msg = "Analyzing junit results(%s)" % path
                with self.log_context(msg) as log:
                    errors |= self._junit_errors(log, f.read())
        return errors

    def upload_artifacts(self):
        self.jobserv.update_status("UPLOADING", "Finding artifacts to upload")
        archive = os.path.join(self.run_dir, "archive")
        total_size = 0
        uploads = []
        for root, dirs, files in os.walk(archive):
            rel = root[len(archive) + 1 :]
            for f in files:
                if f:
                    f = os.path.join(rel, f)
                    size = os.stat(os.path.join(archive, f)).st_size
                    uploads.append({"file": f, "size": size})
                    total_size += size

        msg = "Uploading %d items %d bytes\n" % (len(uploads), total_size)
        self.jobserv.update_run(msg.encode())
        if uploads:
            errors = self.jobserv.upload(archive, uploads)
            if errors:
                self.jobserv.update_run(("\n".join(errors)).encode())
                return False
        return True

    def check_for_reboot(self):
        warm = os.path.join(self.run_dir, "archive/execute-on-reboot")
        cold = os.path.join(self.run_dir, "archive/execute-on-cold-reboot")
        # TODO - handle timeout adjustment?
        if os.path.isfile(warm):
            reboot = warm
        elif os.path.isfile(cold):
            reboot = cold
        else:
            return

        with self.log_context("Found %s script." % reboot) as log:
            log.info("Preparing run for reboot")
            with open(reboot) as f:
                self.rundef["reboot-script"] = f.read()
            os.unlink(reboot)

            # The flock is a file descriptor that can't be serialized.
            # We do need to keep a reference to it so the worker won't try
            # and start another run while we are trying to reboot.
            self.flock_hack = self.rundef["flock"]
            del self.rundef["flock"]
            with open(os.path.join(self.run_dir, "rundef.json"), "w") as f:
                json.dump(self.rundef, f)
            log.warn(reboot_msg)

        raise self.RebootAndContinue(cold=(reboot == cold))

    @classmethod
    def get_jobserv(clazz, rundef):
        if rundef.get("simulator"):
            JobServApi.SIMULATED = True
            rundef["run_url"] = "http://simulated/"
            rundef["api_key"] = "simulated"
        return JobServApi(rundef["run_url"], rundef["api_key"])

    @classmethod
    def execute(clazz, worker_dir, run_dir, rundef):
        jobserv = clazz.get_jobserv(rundef)
        try:
            h = clazz(worker_dir, run_dir, jobserv, rundef)
            if not JobServApi.SIMULATED:
                h.log_simulator_instructions()
            h.docker_pull()
            h.start_timer()
            mounts = h.prepare_mounts()

            last_status = "FAILED"
            msg = "Script completed with error(s)\n"
            if h.docker_run(mounts):
                h.check_for_reboot()
                last_status = "PASSED"
                msg = "Script completed\n"
            h.jobserv.update_run(msg.encode())

            if not h.upload_artifacts():
                last_status = "FAILED"

            if h.test_suite_errors():
                last_status = "FAILED"

            if last_status == "PASSED":
                jobserv.update_status(last_status, passed_msg)
            else:
                jobserv.update_status(last_status, failed_msg)
            return True
        except clazz.RebootAndContinue:
            raise
        except HandlerError as e:
            jobserv.update_status("FAILED", str(e))
        except RunCancelledError:
            jobserv.update_status("FAILED", "Run cancelled from server\n" + failed_msg)
        except Exception as e:
            if getattr(e, "handler_logged", False):
                # we've already logged the stack trace, just fail the run
                jobserv.update_status("FAILED", str(e))
            else:
                stack = traceback.format_exc()
                print("Unexpected Runner Error:\n" + stack)
                try:
                    jobserv.update_status("FAILED", "Unexpected error: " + stack)
                except Exception:
                    stack = traceback.format_exc()
                    print("Unable to fail job:\n" + stack)
        return False


handler = SimpleHandler
