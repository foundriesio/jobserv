#!/usr/bin/python3
# Copyright (C) 2017 Linaro Limited
# Copyright (C) 2018 Foundries.io
# Author: Andy Doan <andy.doan@linaro.org>

import argparse
from base64 import b64decode
import contextlib
import datetime
import fcntl
import hashlib
import importlib
import json
import logging
import os
import platform
import random
import shutil
import string
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.parse

from configparser import ConfigParser, NoOptionError
from multiprocessing import cpu_count

import requests

script = os.path.abspath(__file__)
config_file = os.path.join(os.path.dirname(script), "settings.conf")
config = ConfigParser()
config.read([config_file])

logging.basicConfig(
    level=getattr(logging, config.get("jobserv", "log_level", fallback="INFO"))
)
log = logging.getLogger("jobserv-worker")
logging.getLogger("requests").setLevel(logging.WARNING)

if "linux" not in sys.platform:
    log.error("worker only supported on the linux platform")
    sys.exit(1)


def _host_from_jwt(jwt):
    _, payload, _ = jwt.split(".")
    content = b64decode(payload.encode() + b"==")
    data = json.loads(content)
    return data["name"]


def _create_conf(server_url, hostname, concurrent_runs, host_tags, surges, jwt):
    with open(script, "rb") as f:
        h = hashlib.md5()
        h.update(f.read())
        version = h.hexdigest()

    config.add_section("jobserv")
    config["jobserv"]["server_url"] = server_url
    config["jobserv"]["version"] = version
    config["jobserv"]["log_level"] = "INFO"
    config["jobserv"]["concurrent_runs"] = str(concurrent_runs)
    config["jobserv"]["host_tags"] = host_tags
    config["jobserv"]["surges_only"] = str(int(surges))
    if jwt:
        hostname = _host_from_jwt(jwt)
        config["jobserv"]["jwt"] = jwt
        config["jobserv"]["host_api_key"] = ""
    else:
        chars = string.ascii_letters + string.digits + "!@#$^&*~"
        config["jobserv"]["host_api_key"] = "".join(
            random.choice(chars) for _ in range(32)
        )
    if not hostname:
        with open("/etc/hostname") as f:
            hostname = f.read().strip()
    config["jobserv"]["hostname"] = hostname
    with open(config_file, "w") as f:
        config.write(f, True)


class RunLocks(object):
    def __init__(self, count):
        self._flocks = []
        locksdir = os.path.dirname(script)
        for x in range(count):
            x = open(os.path.join(locksdir, ".run-lock-%d" % x), "a")
            try:
                fcntl.flock(x, fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.set_inheritable(x.fileno(), True)
                self._flocks.append(x)
            except BlockingIOError:
                pass

    def aquire(self, reason):
        fd = self._flocks.pop()
        fd.seek(0)
        fd.truncate()
        fd.write(reason)
        return fd

    def release(self):
        for fd in self._flocks:
            fd.seek(0)
            fd.truncate()
            fd.write("free")
            fd.close()

    def __len__(self):
        return len(self._flocks)


class HostProps(object):
    CACHE = os.path.join(os.path.dirname(script), "hostprops.cache")
    MEM_TOTAL = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")

    def __init__(self):
        surges = int(config.get("jobserv", "surges_only", fallback="0"))
        self.data = {
            "cpu_total": cpu_count(),
            "cpu_type": platform.processor() or platform.machine(),
            "mem_total": self.MEM_TOTAL,
            "distro": self._get_distro(),
            "api_key": config["jobserv"]["host_api_key"],
            "name": config["jobserv"]["hostname"],
            "concurrent_runs": int(config["jobserv"]["concurrent_runs"]),
            "host_tags": config["jobserv"]["host_tags"],
            "surges_only": surges != 0,
        }

    def _get_distro(self):
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME"):
                    return line.split("=")[1].strip().replace('"', "")
        return "?"

    def cache(self):
        with open(self.CACHE, "w") as f:
            json.dump(self.data, f)

    def update_if_needed(self, server):
        try:
            with open(self.CACHE) as f:
                cached = json.load(f)
        except Exception:
            cached = {}
        if cached != self.data:
            log.info("updating host properies on server: %s", self.data)
            server.update_host(self.data)
            self.cache()

    @staticmethod
    def get_available_space(path):
        st = os.statvfs(path)
        return st.f_frsize * st.f_bavail  # usable space in bytes

    @staticmethod
    def get_available_memory():
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemFree:"):
                    return int(line.split()[1]) * 1024  # available in bytes
        raise RuntimeError('Unable to find "MemFree" in /proc/meminfo')

    @staticmethod
    @contextlib.contextmanager
    def available_runners():
        locks = None
        try:
            locks = RunLocks(int(config["jobserv"]["concurrent_runs"]))
            yield locks
        finally:
            if locks:
                locks.release()

    @classmethod
    def idle(cls):
        avail = int(config["jobserv"]["concurrent_runs"])
        with cls.available_runners() as locks:
            return avail == len(locks)


def http_do(method, resource, assert_ok=True, **kwargs):
    if resource.startswith("http"):
        url = resource
    else:
        url = urllib.parse.urljoin(config["jobserv"]["server_url"], resource)
    if "timeout" not in kwargs:
        kwargs["timeout"] = 15
    r = requests.request(method, url, **kwargs)
    if assert_ok and not r.ok:
        log.error("Failed to issue %s request to %s: %s\n", method, r.url, r.text)
        sys.exit(1)
    return r


class JobServ(object):
    def _auth_headers(self):
        headers = {}
        jwt = config["jobserv"].get("jwt")
        if jwt:
            headers["Authorization"] = "Bearer " + jwt
        else:
            headers["Authorization"] = "Token " + config["jobserv"]["host_api_key"]
        return headers

    def _get(self, resource, params=None, json=None):
        return http_do(
            "GET", resource, params=params, json=json, headers=self._auth_headers()
        )

    def _post(self, resource, data, use_auth_headers=False):
        headers = None
        if use_auth_headers:
            headers = self._auth_headers()
        http_do("POST", resource, json=data, headers=headers)

    def _patch(self, resource, data):
        http_do("PATCH", resource, json=data, headers=self._auth_headers())

    def _delete(self, resource):
        http_do("DELETE", resource, headers=self._auth_headers())

    def create_host(self, hostprops):
        if "jwt" in config["jobserv"]:
            return self.update_host(hostprops)
        self._post("/workers/%s/" % config["jobserv"]["hostname"], hostprops)

    def update_host(self, hostprops):
        self._patch("/workers/%s/" % config["jobserv"]["hostname"], hostprops)

    def delete_host(self):
        self._delete("/workers/%s/" % config["jobserv"]["hostname"])

    def get_deleted_volumes(self, local_dirs):
        r = self._get(
            "/workers/%s/volumes-deleted/" % config["jobserv"]["hostname"],
            json={"directories": local_dirs},
        )
        return r.json()["data"]["volumes"]

    @contextlib.contextmanager
    def check_in(self):
        load_avg_1, load_avg_5, load_avg_15 = os.getloadavg()
        with HostProps.available_runners() as locks:
            params = {
                "available_runners": len(locks),
                "mem_free": HostProps.get_available_memory(),
                # /var/lib is what should hold docker images and will be the
                # most important measure of free disk space for us over time
                "disk_free": HostProps.get_available_space("/var/lib"),
                "load_avg_1": load_avg_1,
                "load_avg_5": load_avg_5,
                "load_avg_15": load_avg_15,
            }
            data = self._get(
                "/workers/%s/" % config["jobserv"]["hostname"], params
            ).json()
            yield data, locks

    def get_worker_script(self):
        return self._get("/worker").text

    def update_run(self, rundef, status, msg):
        msg = ("== %s: %s\n" % (datetime.datetime.utcnow(), msg)).encode()
        headers = {
            "content-type": "text/plain",
            "Authorization": "Token " + rundef["api_key"],
        }
        if status:
            headers["X-RUN-STATUS"] = status
        for i in range(8):
            if i:
                log.info("Failed to update run, sleeping and retrying")
                time.sleep(2 * i)
            r = http_do(
                "POST", rundef["run_url"], assert_ok=False, data=msg, headers=headers
            )
            if r.status_code == 200:
                break
        else:
            log.error("Unable to update run: %d: %s", r.status_code, r.text)


def _create_systemd_service():
    svc = """
[Unit]
Description=JobServ Worker
After=network.target

[Service]
Type=simple
User={user}
WorkingDirectory={working_dir}
ExecStart={command}
Restart=always

[Install]
WantedBy=multi-user.target
"""
    svc = svc.format(
        user=os.environ["USER"],
        working_dir=os.path.dirname(os.path.abspath(script)),
        command=os.path.abspath(script) + " loop",
    )
    svc_file = os.path.join(os.path.dirname(script), "jobserv.service")
    with open(svc_file, "w") as f:
        f.write(svc)


def _run_callback(action: str, rundef: dict):
    cb = config.get("jobserv", "callback_script", fallback=None)
    if not cb:
        return
    env = os.environ.copy()
    env["PROJECT"] = rundef["env"]["H_PROJECT"]
    env["BUILD"] = rundef["env"]["H_BUILD"]
    env["RUN"] = rundef["env"]["H_RUN"]
    env["ACTION"] = action
    subprocess.call([cb], env=env)


def cmd_register(args):
    """Register this host with the configured JobServ server"""
    _create_conf(
        args.server_url,
        args.hostname,
        args.concurrent_runs,
        args.host_tags,
        args.surges_only,
        args.jwt,
    )
    _create_systemd_service()
    p = HostProps()
    args.server.create_host(p.data)
    p.cache()
    print(
        """
A SystemD service can be enabled with:
  sudo cp jobserv.service /etc/systemd/system/
  sudo systemctl enable jobserv
  sudo systemctl start jobserv

You also need to add a sudo entry to allow the worker to clean up root owned
files from CI runs:

 echo "$USER ALL=(ALL) NOPASSWD:/bin/rm" | sudo tee /etc/sudoers.d/jobserv
"""
    )


def cmd_unregister(args):
    """Remove worker from server"""
    args.server.delete_host()


def _upgrade_worker(args, version):
    buf = args.server.get_worker_script()
    with open(__file__, "wb") as f:
        f.write(buf.encode())
        f.flush()
    config["jobserv"]["version"] = version
    with open(config_file, "w") as f:
        config.write(f, True)


def _download_runner(url, rundir, retries=3):
    for i in range(1, retries + 1):
        r = http_do("GET", url, assert_ok=False, stream=True)
        if r.status_code == 200:
            runner = os.path.join(rundir, "runner.whl")
            with open(runner, "wb") as f:
                for chunk in r.iter_content(4096):
                    f.write(chunk)
            return runner
        else:
            if i == retries:
                raise RuntimeError(
                    "Unable to download runner(%s): %d %s"
                    % (url, r.status_code, r.text)
                )
            log.error("Error getting runner: %d %s", r.status_code, r.text)
            time.sleep(i * 2)


def _is_rebooting():
    return os.path.isfile("/tmp/jobserv_rebooting")


def _set_rebooting():
    with open("/tmp/jobserv_rebooting", "w") as f:
        f.write("%d\n" % time.time())


def _delete_rundir(rundir):
    try:
        shutil.rmtree(rundir)
    except PermissionError:
        log.info("Unable to cleanup Run as normal user, trying sudo rm -rf")
        subprocess.check_call(["sudo", "/bin/rm", "-rf", rundir])
    except Exception:
        log.exception("Unable to delete Run's directory: " + rundir)
        sys.exit(1)


def _handle_reboot(rundir, jobserv, rundef, cold):
    _set_rebooting()
    log.warn("RebootAndContinue(cold=%s) requested by %s", cold, rundef["run_url"])
    reboot_run = os.path.join(os.path.dirname(script), "rebooted-run")
    if os.path.exists(reboot_run):
        log.error("Reboot run directory(%s) exists, deleting", reboot_run)
        shutil.rmtree(reboot_run)

    os.rename(rundir, reboot_run)
    os.sync()
    key = "cold-reboot" if cold else "reboot"
    try:
        cmd = config["tools"][key]
    except KeyError:
        cmd = "/usr/bin/" + key

    for i in range(10):
        r = subprocess.run([cmd], stderr=subprocess.STDOUT, stdout=subprocess.PIPE)
        if r.returncode == 0:
            break
        msg = "Unable to reboot system. Error is:\n| "
        msg += "\n| ".join(r.stdout.decode().splitlines())
        msg += "\nRetrying in %d seconds" % (i + 1)
        jobserv.update_run(rundef, None, msg)
        time.sleep(i + 1)

    # we can't just exit here or the worker's "poll" loop might accidentally
    # pull in another run to handle. So lets sleep for 3 minutes. If we
    # are still running then the reboot command has failed.
    time.sleep(180)
    raise RuntimeError("Failed to reboot system")


def _update_shared_volumes_mapping(rundef):
    """Convert rundef mappings:
      name1: /path/in/container1
      name2: /path/in/container2

    And host config shared-volumes like:
      name1: /path/on/host1
      name2: /path/on/host2

    to produce something we can mount with docker-run:
      /path/on/host1: /path/in/container1
      /path/on/host2: /path/in/container2
    """
    shared_vols = rundef.get("shared-volumes")
    if shared_vols:
        if not config.has_section("shared-volumes"):
            raise ValueError("Host does not have shared volumes configured")
        mapping = {}
        for name, container_path in shared_vols.items():
            try:
                host_path = config.get("shared-volumes", name)
                mapping[host_path] = container_path
            except NoOptionError:
                raise ValueError("Host does not have shared volume " + name)
        rundef["shared-volumes"] = mapping


def _update_max_memory(rundef):
    mm = config["jobserv"].get("container-max-memory-percent")
    if mm:
        mmpct = int(mm) / 100
        total = round(mmpct * HostProps.MEM_TOTAL)
        rundef["max-mem-bytes"] = total


def _handle_run(jobserv, rundef, rundir=None):
    runsdir = os.path.join(os.path.dirname(script), "runs")
    try:
        _run_callback("RUN_START", rundef)
        _update_shared_volumes_mapping(rundef)
        _update_max_memory(rundef)
        jobserv.update_run(rundef, "RUNNING", "Setting up runner on worker")
        if not os.path.exists(runsdir):
            os.mkdir(runsdir)
        if not rundir:
            rundir = tempfile.mkdtemp(dir=runsdir)
        try:
            if os.fork() == 0:
                sys.path.insert(0, _download_runner(rundef["runner_url"], rundir))
                m = importlib.import_module(
                    "jobserv_runner.handlers." + rundef["trigger_type"]
                )
                try:
                    m.handler.execute(os.path.dirname(script), rundir, rundef)
                    _run_callback("RUN_COMPLETE", rundef)
                except m.handler.RebootAndContinue as e:
                    _handle_reboot(rundir, jobserv, rundef, e.cold)
                _delete_rundir(rundir)
        except SystemExit:
            raise
    except Exception:
        stack = traceback.format_exc().strip().replace("\n", "\n | ")
        msg = "Unexpected runner error:\n | " + stack
        log.error(msg)
        jobserv.update_run(rundef, "FAILED", msg)


def _handle_rebooted_run(jobserv):
    reboot_run = os.path.join(os.path.dirname(script), "rebooted-run")
    if os.path.exists(reboot_run):
        if _is_rebooting():
            log.info("Detected a reboot in progress")
            return True
        log.warn("Found rebooted-run, preparing to execute")

        rundir = os.path.join(os.path.dirname(script), "runs/rebooted-run")
        rundir += str(time.time())
        os.rename(reboot_run, rundir)

        with open(os.path.join(rundir, "rundef.json")) as f:
            rundef = json.load(f)

        with HostProps.available_runners() as locks:
            rundef["flock"] = locks.aquire(rundef["run_url"])

        log.info("Rebooted run is: %s", rundef["run_url"])
        jobserv.update_run(rundef, "RUNNING", "Resuming rebooted run")
        _handle_run(jobserv, rundef, rundir)
        return True


def cmd_check(args):
    """Check in with server for work"""
    if _handle_rebooted_run(args.server):
        return

    HostProps().update_if_needed(args.server)
    rundefs = []
    with args.server.check_in() as (data, locks):
        for rd in data["data"]["worker"].get("run-defs") or []:
            rundef = json.loads(rd)
            rundef["env"]["H_WORKER"] = config["jobserv"]["hostname"]
            rundef["flock"] = locks.aquire(rundef.get("run_url"))
            rundefs.append(rundef)

    for rundef in rundefs:
        log.info("Executing run: %s", rundef.get("run_url"))
        _handle_run(args.server, rundef)

    ver = data["data"]["worker"]["version"]
    if ver != config["jobserv"]["version"]:
        log.warning("Upgrading client to: %s", ver)
        _upgrade_worker(args, ver)


def _docker_clean():
    try:
        containers = subprocess.check_output(
            ["docker", "ps", "--filter", "status=exited", "-q"]
        )
        containers = containers.decode().splitlines()
        if not containers:
            return  # nothing to clean up
        cmd = ["docker", "inspect", "--format", "{{.State.FinishedAt}}"]
        times = subprocess.check_output(cmd + containers).decode().splitlines()

        now = datetime.datetime.now()
        deletes = []
        for i, ts in enumerate(times):
            # Times are like: 2017-09-19T21:17:33.465435028Z
            # Strip of the nanoseconds and parse the date
            ts = ts.split(".")[0]
            ts = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S")
            if (now - ts).total_seconds() > 7200:  # 2 hours old
                deletes.append(containers[i])
        if deletes:
            log.info("Cleaning up old containers:\n  %s", "\n  ".join(deletes))
            subprocess.check_call(["docker", "rm", "-v"] + deletes)
        subprocess.check_call(["docker", "volume", "prune", "-f"])
    except subprocess.CalledProcessError as e:
        log.exception(e)


def cmd_loop(args):
    # Ensure no other copy of this script is running
    try:
        cmd_args = [config["tools"]["worker-wrapper"], "check"]
    except KeyError:
        cmd_args = [sys.argv[0], "check"]
    lockfile = os.path.join(os.path.dirname(script), ".worker-lock")
    with open(lockfile, "w+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except IOError:
            sys.exit("Script is already running")
        if _is_rebooting():
            log.warning("Reboot lock from previous run detected, deleting")
            os.unlink("/tmp/jobserv_rebooting")
        try:
            idle_threshold = args.idle_threshold * 60
            last_busy = time.time()
            next_clean = time.time() + (args.docker_rm * 3600)
            while True:
                log.debug("Calling check")
                rc = subprocess.call(cmd_args)
                if rc:
                    log.error("Last call exited with rc: %d", rc)

                now = time.time()
                if HostProps.idle():
                    log.debug("Worker is idle")
                    if args.idle_command and now - last_busy > idle_threshold:
                        log.info("Worker is idle, calling %s", args.idle_command)
                        subprocess.check_call([args.idle_command])
                else:
                    last_busy = now

                if now > next_clean:
                    log.info("Running docker container cleanup")
                    _docker_clean()
                    next_clean = time.time() + (args.docker_rm * 3600)
                else:
                    time.sleep(args.every)
        except (ConnectionError, TimeoutError, requests.RequestException):
            log.exception("Unable to check in with server, retrying now")
        except KeyboardInterrupt:
            log.info("Keyboard interrupt received, exiting")
            return


def cmd_cronwrap(args):
    logfile = os.path.basename(args.script) + ".log"
    logfile = "/tmp/cronwrap-" + logfile
    data = {
        "title": "JobServ CronWrap - " + args.script,
        "msg": "",
        "type": "info",
    }
    try:
        with open(logfile, "wb") as f:
            start = time.time()
            subprocess.check_call([args.script], stdout=f, stderr=f)
            data["msg"] = "Completed in %d seconds" % (time.time() - start)
    except Exception:
        data["type"] = "error"
        data["msg"] = "Check %s for error message" % logfile
        with open(logfile) as f:
            data["msg"] += "\nFirst 1024 bytes of log:\n %s" % f.read(1024)
        sys.exit("Failed to run cronwrap: " + args.script)
    finally:
        resource = "/workers/%s/events/" % config["jobserv"]["hostname"]
        JobServ()._post(resource, data, True)


def cmd_gcvols(args):
    vols_dir = os.path.join(os.path.dirname(script), "volumes")
    try:
        vols = [
            x.name
            for x in os.scandir(vols_dir)
            if x.is_dir() and x.name != "lost+found"
        ]
    except FileNotFoundError:
        log.info("No shared volumes found")
        return

    if not vols:
        log.info("No shared volumes found")
        return

    events_url = "/workers/%s/events/" % config["jobserv"]["hostname"]
    with open(os.path.join(vols_dir, ".gcvols"), "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            deletes = JobServ().get_deleted_volumes(vols)
            if args.dryrun:
                log.info("dryrun deletes:\n %s", "\n ".join(deletes))
                return
            for x in deletes:
                log.warning("Deleting volume: %s", x)
                event = {
                    "title": "JobServ Delete Volume: " + x,
                    "type": "info",
                    "msg": "",
                }
                try:
                    shutil.rmtree(os.path.join(vols_dir, x))
                except Exception as e:
                    event["type"] = "error"
                    event["msg"] = str(e)
                finally:
                    JobServ()._post(events_url, event, True)
        except BlockingIOError:
            log.info("Another worker is doing GC")


def main(args):
    if getattr(args, "func", None):
        log.debug("running: %s", args.func.__name__)
        args.func(args)


def get_args(args=None):
    parser = argparse.ArgumentParser("Worker API to JobServ server")
    sub = parser.add_subparsers(help="sub-command help")

    p = sub.add_parser("register", help="Register this host with the server")
    p.set_defaults(func=cmd_register)
    p.add_argument(
        "--hostname",
        help="""Worker name to register. If none is provided, the value of
                /etc/hostname will be used.""",
    )
    p.add_argument("--jwt", help="A JWT to use for API authentication")
    p.add_argument(
        "--concurrent-runs",
        type=int,
        default=1,
        help="Maximum number of current runs. Default=%(default)d",
    )
    p.add_argument(
        "--surges-only",
        action="store_true",
        help="""Only use this worker when a surge of Runs has been has been
                queued on the server""",
    )
    p.add_argument("server_url")
    p.add_argument("host_tags", help="Comma separated list")

    p = sub.add_parser("unregister", help="Unregister worker with server")
    p.set_defaults(func=cmd_unregister)

    p = sub.add_parser("check", help="Check in with server for updates")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("loop", help='Run the "check" command in a loop')
    p.set_defaults(func=cmd_loop)
    interval = 20
    if int(config.get("jobserv", "surges_only", fallback="0")):
        interval = 90
    p.add_argument(
        "--every",
        type=int,
        default=interval,
        metavar="interval",
        help="Seconds to sleep between runs. default=%(default)d",
    )
    p.add_argument(
        "--docker-rm",
        type=int,
        default=8,
        metavar="interval",
        help="""Interval in hours to run to run "dock rm" on containers that
                have exited. default is every %(default)d hours""",
    )
    p.add_argument(
        "--idle-threshold",
        type=int,
        default=15,
        metavar="threshold",
        help="""Threshold for how long worker should be idle before calling
                the --idle-command. %(default)d minutes.""",
    )
    p.add_argument(
        "--idle-command",
        help="Command to call when worker has been idle --idle-threshold minutes",
    )

    p = sub.add_parser(
        "cronwrap",
        help="Run a command and report back to the jobserv if it passed or not",
    )
    p.add_argument("script", help="Program to run")
    p.set_defaults(func=cmd_cronwrap)

    p = sub.add_parser(
        "gc-volumes",
        help="""Check in with the server for active projects. Then free up
                persistent volume data for deleted projects.""",
    )
    p.add_argument("--dryrun", action="store_true")
    p.set_defaults(func=cmd_gcvols)

    args = parser.parse_args(args)
    args.server = JobServ()
    return args


if __name__ == "__main__":
    main(get_args())
