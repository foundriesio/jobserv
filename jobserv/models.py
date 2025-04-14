# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

from base64 import urlsafe_b64decode, urlsafe_b64encode
import contextlib
import datetime
import enum
import fcntl
import fnmatch
import json
import logging
import os
import random
import re
from shutil import rmtree
import string
import time
from typing import Dict

import bcrypt
import sqlalchemy.dialects.mysql.mysqldb as mysqldb

from cryptography.fernet import Fernet
from flask import url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.hybrid import Comparator, hybrid_property

from jobserv.settings import (
    BUILD_URL_FMT,
    JOBS_DIR,
    RUN_URL_FMT,
    SECRETS_FERNET_KEY,
    SQLALCHEMY_DATABASE_URI,
    WORKER_DIR,
)
from jobserv.stats import StatsClient

VALID_SECRET_PATTERN = r"^[a-zA-Z0-9_\-\.]+$"

db = SQLAlchemy()
ANNOTATION_COLUMN_TYPE = MEDIUMTEXT
if "mysql" not in SQLALCHEMY_DATABASE_URI:
    ANNOTATION_COLUMN_TYPE = db.Text


def hack_create_connect_args(*args, **kwargs):
    # The mysqldb driver hard-codes rowcount to always be the number found
    # and not the number updated:
    # http://docs.sqlalchemy.org/en/latest/dialects/mysql.html#rowcount-support
    # The Run.pop_queued code below needs to know if it updated a row or now.
    rv = orig_create(*args, **kwargs)
    rv[1]["client_flag"] = 0
    return rv


orig_create = mysqldb.MySQLDialect_mysqldb.create_connect_args
mysqldb.MySQLDialect_mysqldb.create_connect_args = hack_create_connect_args


def get_cumulative_status(items):
    """A helper used by Test and Build to calculate the status based on the
    status of its child TestResults and Runs."""
    status = BuildStatus.QUEUED  # Default guess to QUEUED
    states = set([x.status for x in items])
    if (
        BuildStatus.RUNNING in states
        or BuildStatus.UPLOADING in states
        or BuildStatus.CANCELLING in states
    ):
        # Something is still running
        status = BuildStatus.RUNNING
        if BuildStatus.FAILED in states or BuildStatus.CANCELLING in states:
            status = BuildStatus.RUNNING_WITH_FAILURES
    if BuildStatus.QUEUED in states and BuildStatus.FAILED in states:
        status = BuildStatus.RUNNING_WITH_FAILURES
    if BuildStatus.QUEUED in states and BuildStatus.PASSED in states:
        status = BuildStatus.RUNNING

    complete = [BuildStatus.PASSED, BuildStatus.FAILED, BuildStatus.SKIPPED]
    if not states - set(complete):
        # All runs have completed. Have we passed or failed
        if BuildStatus.FAILED in states:
            status = BuildStatus.FAILED
        else:
            status = BuildStatus.PASSED
    return status


class Project(db.Model):
    __tablename__ = "projects"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True)

    synchronous_builds = db.Column(db.Boolean, default=False)
    allowed_host_tags_str = db.Column(db.Text())

    builds = db.relationship("Build", order_by="-Build.id")
    triggers = db.relationship("ProjectTrigger")

    def __init__(self, name=None, synchronous_builds=False, allowed_host_tags=None):
        self.name = name
        self.synchronous_builds = synchronous_builds
        if allowed_host_tags:
            self.allowed_host_tags_str = json.dumps(allowed_host_tags)

    @property
    def allowed_host_tags(self):
        if self.allowed_host_tags_str:
            return json.loads(self.allowed_host_tags_str)
        return None

    def as_json(self, detailed=False):
        data = {
            "name": self.name,
            "synchronous-builds": self.synchronous_builds,
            "url": url_for("api_project.project_get", proj=self.name, _external=True),
        }
        allowed_host_tags = self.allowed_host_tags
        if allowed_host_tags:
            data["allowed-host-tags"] = allowed_host_tags
        if detailed:
            data["builds_url"] = url_for(
                "api_build.build_list", proj=self.name, _external=True
            )
        return data

    def __repr__(self):
        return "<Project %s>" % self.name


class TriggerTypes(enum.Enum):
    git_poller = 1
    github_pr = 2
    simple = 3
    # lava = 4
    # lava_pr = 5
    gitlab_mr = 6
    # lava_mr = 7


class ProjectTrigger(db.Model):
    __tablname__ = "triggers"
    id = db.Column(db.Integer, primary_key=True)
    user = db.Column(db.String(128), nullable=False)
    type = db.Column(db.Integer)
    proj_id = db.Column(db.Integer, db.ForeignKey(Project.id), nullable=False)
    definition_repo = db.Column(db.String(512))
    definition_file = db.Column(db.String(512))
    secrets = db.Column(db.Text())
    queue_priority = db.Column(db.Integer)  # bigger is more important

    project = db.relationship(Project, back_populates="triggers")

    fernet = None

    def __init__(self, user, ttype, project, def_repo, def_file, secrets):
        self.user = user
        self.type = ttype
        self.proj_id = project.id
        self.definition_repo = def_repo
        self.definition_file = def_file
        self._secret_data = secrets
        self.update_secrets()

    def as_json(self):
        return {
            "id": self.id,
            "user": self.user,
            "type": TriggerTypes(self.type).name,
            "project": self.project.name,
            "definition_repo": self.definition_repo,
            "definition_file": self.definition_file or None,
            "queue_priority": self.queue_priority or 0,
            "secrets": self.secret_data,
        }

    @classmethod
    def _init_fernet(clazz):
        if not clazz.fernet:
            if not SECRETS_FERNET_KEY:
                raise ValueError("Missing environment value: SECRETS_FERNET_KEY")
            clazz.fernet = Fernet(SECRETS_FERNET_KEY.encode())

    @property
    def secret_data(self) -> Dict[str, str]:
        self._init_fernet()
        try:
            return self._secret_data
        except AttributeError:
            s = self.fernet.decrypt(self.secrets.encode()).decode()
            self._secret_data = json.loads(s or "{}")
            return self._secret_data

    def update_secrets(self):
        assert isinstance(self._secret_data, dict)
        pat = re.compile(VALID_SECRET_PATTERN)
        for k, v in self._secret_data.items():
            if not isinstance(k, str):
                raise ValueError("Invalid secret name: %r" % k)
            if not pat.match(k):
                raise ValueError(
                    f"Invalid secret name `{k}`. Must match pattern: `{VALID_SECRET_PATTERN}`"
                )
            if not isinstance(v, str):
                raise ValueError("Invalid secret value(%s): %r" % (k, v))
        self._init_fernet()
        self.secrets = self.fernet.encrypt(
            json.dumps(self._secret_data).encode()
        ).decode()

    def __repr__(self):
        return "<Trigger %s: %s>" % (self.project.name, TriggerTypes(self.type).name)


class BuildStatus(enum.Enum):
    QUEUED = 1
    RUNNING = 2
    PASSED = 3
    FAILED = 4
    RUNNING_WITH_FAILURES = 5
    UPLOADING = 6
    PROMOTED = 7  # ie - the build got "released"

    SKIPPED = 8  # only valid for Test and TestResult
    # only valid for Run. Its been *requested*. The worker will then *fail*
    CANCELLING = 9


class StatusComparator(Comparator):
    def __eq__(self, other):
        return self.__clause_element__() == BuildStatus(other).value

    def __ne__(self, other):
        return self.__clause_element__() != BuildStatus(other).value

    def in_(self, states):
        return self.__clause_element__().in_([x.value for x in states])


class StatusMixin(object):
    """Using ENUM columns in the Database can be a real pain and difficult
    to provide migration logic for. This hack makes the column feel just
    like an ENUM in sqlalchemy but stores the column as an Integer.
    """

    @hybrid_property
    def status(self):
        return BuildStatus(self._status)

    @status.comparator
    def status(cls):
        return StatusComparator(cls._status)

    @status.setter
    def status(self, status):
        if isinstance(status, str):
            self._status = BuildStatus[status].value
        else:
            self._status = status.value

    @property
    def complete(self):
        return self._status in (
            BuildStatus.PASSED.value,
            BuildStatus.FAILED.value,
            BuildStatus.PROMOTED.value,
            BuildStatus.SKIPPED.value,
        )

    @contextlib.contextmanager
    def locked(self):
        """Provide a distributed lock that can be used to provide sequential
        updates to certain operations like Run and Test status.
        """
        lockname = os.path.join(JOBS_DIR, "%s-%d" % (self.__class__.__name__, self.id))
        with open(lockname, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            # force a clean session so that updates from another thread will
            # be pulled in
            db.session.rollback()
            yield
            db.session.commit()
        if self.complete:
            os.unlink(lockname)


class Build(db.Model, StatusMixin):
    __tablename__ = "builds"
    id = db.Column(db.Integer, primary_key=True)

    build_id = db.Column(db.Integer, nullable=False)
    proj_id = db.Column(db.Integer, db.ForeignKey(Project.id), nullable=False)
    _status = db.Column(db.Integer)
    reason = db.Column(db.String(4096))
    trigger_name = db.Column(db.String(80))

    name = db.Column(db.String(256))
    annotation = db.Column(ANNOTATION_COLUMN_TYPE())

    project = db.relationship(Project, back_populates="builds")
    runs = db.relationship(
        "Run", order_by="Run.id", cascade="save-update, merge, delete"
    )
    status_events = db.relationship(
        "BuildEvents", order_by="BuildEvents.id", cascade="save-update, merge, delete"
    )

    __table_args__ = (db.UniqueConstraint("proj_id", "build_id", name="build_id_uc"),)

    def __init__(self, project, build_id, reason="", trigger_name=""):
        self.proj_id = project.id
        self.build_id = build_id
        self.status = BuildStatus.QUEUED
        if reason:
            self.reason = reason
        if trigger_name:
            self.trigger_name = trigger_name

    def as_json(self, detailed=False):
        url = url_for(
            "api_build.build_get",
            proj=self.project.name,
            build_id=self.build_id,
            _external=True,
        )
        data = {
            "build_id": self.build_id,
            "url": url,
            "status": self.status.name,
            "runs": [x.as_json() for x in self.runs],
        }
        if BUILD_URL_FMT:
            data["web_url"] = BUILD_URL_FMT.format(
                project=self.project.name, build=self.build_id
            )
        if self.name:
            data["name"] = self.name
        if self.trigger_name:
            data["trigger_name"] = self.trigger_name
        if self.status_events:
            data["created"] = self.status_events[0].time
            if self.complete:
                data["completed"] = self.status_events[-1].time
        if detailed:
            data["status_events"] = [
                {"time": x.time, "status": x.status.name} for x in self.status_events
            ]
            data["runs_url"] = url_for(
                "api_run.run_list",
                proj=self.project.name,
                build_id=self.build_id,
                _external=True,
            )
            data["reason"] = self.reason
            data["annotation"] = self.annotation
        return data

    def refresh_status(self):
        status = get_cumulative_status(self.runs)
        if self.status != status:
            self.status = status
            db.session.add(BuildEvents(self, status))

    def __repr__(self):
        return "<Build %d/%d: %s>" % (self.proj_id, self.build_id, self.status.name)

    @classmethod
    def _try_build_ids(clazz, project):
        last = (
            clazz.query.filter_by(proj_id=project.id).order_by(-clazz.build_id).first()
        )
        try_build_id = 1
        if last:
            try_build_id = last.build_id + 1

        # try 10 build ids to help avoid a race condition
        for build_id in range(try_build_id, try_build_id + 10):
            yield build_id

    @classmethod
    def create(
        clazz, project, init_event_status=BuildStatus.QUEUED, reason="", trigger_name=""
    ):
        last_exc = None
        for build_id in clazz._try_build_ids(project):
            try:
                b = Build(project, build_id, reason, trigger_name)
                db.session.add(b)
                db.session.flush()
                db.session.add(BuildEvents(b, init_event_status))
                db.session.commit()
                return b
            except IntegrityError as e:
                last_exc = e
                db.session.rollback()
        raise last_exc


class BuildEvents(db.Model, StatusMixin):
    __tablename__ = "build_events"

    id = db.Column(db.Integer, primary_key=True)
    time = db.Column(db.DateTime)
    _status = db.Column(db.Integer)
    build_id = db.Column(db.Integer, db.ForeignKey(Build.id), nullable=False)

    def __init__(self, build, status):
        self.build_id = build.id
        self.status = status
        self.time = datetime.datetime.utcnow()

    def __repr__(self):
        return "<Status %s: %s>" % (self.time, self.status.name)


class Run(db.Model, StatusMixin):
    __tablename__ = "runs"
    id = db.Column(db.Integer, primary_key=True)

    build_id = db.Column(db.Integer, db.ForeignKey(Build.id), nullable=False)
    name = db.Column(db.String(80))
    _status = db.Column(db.Integer)
    api_key = db.Column(db.String(80), nullable=False)
    trigger = db.Column(db.String(80))
    meta = db.Column(db.String(1024))

    worker_name = db.Column(db.String(512), db.ForeignKey("workers.name"))
    queue_priority = db.Column(db.Integer)  # bigger is more important

    host_tag = db.Column(db.String(1024))

    build = db.relationship(Build, back_populates="runs")
    status_events = db.relationship(
        "RunEvents", order_by="RunEvents.id", cascade="save-update, merge, delete"
    )

    tests = db.relationship(
        "Test", order_by="Test.id", cascade="save-update, merge, delete"
    )
    worker = db.relationship("Worker")

    __table_args__ = (
        # can't have the same named run for a single build
        db.UniqueConstraint("build_id", "name", name="run_name_uc"),
    )

    def __init__(self, build, name, trigger=None, queue_priority=0):
        self.build_id = build.id
        self.name = name
        self.trigger = trigger
        self.status = BuildStatus.QUEUED
        self.queue_priority = queue_priority
        self.api_key = "".join(
            random.SystemRandom().choice(
                string.ascii_lowercase + string.ascii_uppercase + string.digits
            )
            for _ in range(32)
        )

    def as_json(self, detailed=False):
        b = self.build
        p = b.project
        url = url_for(
            "api_run.run_get",
            proj=p.name,
            build_id=b.build_id,
            run=self.name,
            _external=True,
        )
        log = url_for(
            "api_run.run_get_artifact",
            proj=p.name,
            build_id=b.build_id,
            run=self.name,
            path="console.log",
            _external=True,
        )
        data = {
            "name": self.name,
            "url": url,
            "status": self.status.name,
            "log_url": log,
        }
        if RUN_URL_FMT:
            data["web_url"] = RUN_URL_FMT.format(
                project=p.name, build=b.build_id, run=self.name
            )
        if self.status_events:
            data["created"] = self.status_events[0].time
            if self.complete:
                data["completed"] = self.status_events[-1].time
        if self.host_tag:
            data["host_tag"] = self.host_tag
        if self.tests:
            data["tests"] = url_for(
                "api_test.test_list",
                proj=p.name,
                build_id=b.build_id,
                run=self.name,
                _external=True,
            )
        if detailed:
            data["worker_name"] = self.worker_name
            data["status_events"] = []
            for event in self.status_events:
                e = {"time": event.time, "status": event.status.name}
                if event.worker_name:
                    e["worker"] = event.worker_name
                data["status_events"].append(e)
        return data

    def derive_fernet_token(self):
        # Rather than using the same key for everything, we'll take our
        # fernet key and replace a few bytes with the run-id
        key = urlsafe_b64decode(SECRETS_FERNET_KEY)
        keyarray = bytearray(key)
        pos = self.id % 16
        keyarray[pos : pos + 4] = self.id.to_bytes(4, "little")
        return urlsafe_b64encode(bytes(keyarray))

    def cancel(self):
        # This is tricky. If we are RUNNING, then we need to go to CANCELLING
        # to let the worker know to stop. If QUEUED, we need to go FAILED.
        # However, there's a race where we might fail the run at the
        # same time the worker started the run making it not see a CANCELLING
        # status. This uses a trick similar to Run.pop_queued. We set the
        # run to RUNNING. If we see this change, we can mark it FAILED.
        # If we don't see the change, its RUNNING and needs CANCELLING.
        conn = db.session.connection().connection
        cursor = conn.cursor()

        rows = cursor.execute(
            """
            UPDATE runs
            SET
                _status = 2
            WHERE
                id = {run_id}
            """.format(
                run_id=self.id
            )
        )
        db.session.commit()
        if rows == 1:
            # It was us
            self.set_status(BuildStatus.FAILED)
        else:
            # Its really running somewhere
            self.set_status(BuildStatus.CANCELLING)
        db.session.commit()

    def set_status(self, status):
        if isinstance(status, str):
            status = BuildStatus[status]
        if self.status != status:
            self.status = status
            db.session.flush()
            self.build.refresh_status()
            db.session.add(RunEvents(self, status))

    def __repr__(self):
        return "<Run %s: %s>" % (self.name, self.status.name)

    @staticmethod
    def pop_queued(worker):
        # Forcing 2 queries seems bad, but we have to JOIN on another table
        # and MySQL doesn't allow UPDATEs that do that.
        # So we first find a suitable Run:
        conn = db.session.connection().connection
        cursor = conn.cursor()

        # Find queued(status=1), running(status=2), and uploading(status=6) runs
        cursor.execute(
            """
            SELECT
              runs.id, runs.build_id, runs._status,
              projects.id, projects.synchronous_builds, runs.host_tag
            FROM runs
            JOIN builds on builds.id = runs.build_id
            JOIN projects on projects.id = builds.proj_id
            WHERE
                runs._status in (1, 2, 6)
              ORDER BY
                runs._status DESC, runs.queue_priority DESC,
                runs.build_id ASC, runs.id ASC
            """
        )

        tags = [worker.name] + [x.strip() for x in worker.host_tags.split(",")]
        # By ordering the query above by Run._status, we'll get the active
        # runs first so that we can build up this list of build ids that are
        # active for synchronous projects runs with these build ids are okay
        # to schedule
        sync_projects = {}
        okay_sync_builds = {}
        rows = cursor.fetchall()
        oldest_builds = {}
        for run_id, build_id, status, proj_id, sync, tag in rows:
            oldest_builds.setdefault(proj_id, build_id)
            if status in (2, 6) and sync:
                sync_projects[proj_id] = True
                okay_sync_builds[build_id] = True
            elif status == 1:
                for t in tags:
                    if fnmatch.fnmatch(t, tag):
                        # if its a sync build, we have to make sure this worker
                        # isn't finding work on a newer build that should be
                        # completed first
                        if not sync or build_id == oldest_builds[proj_id]:
                            break
                else:
                    continue
                if (
                    not sync
                    or build_id in okay_sync_builds
                    or proj_id not in sync_projects
                ):
                    break
        else:
            # No run found to schedule
            return

        # We have a suitable run, try and schedule it. This check helps
        # fight the race condition where two threads might schedule the same
        # run to two different workers. The first worker will get the run,
        # the second worker won't see a row change, and won't schedule anything
        # This means the worker will have to check in again to find work
        # (if any)
        rows = cursor.execute(
            """
            UPDATE runs
            SET
                _status = 2
            WHERE
                id = {run_id}
            """.format(
                run_id=run_id
            )
        )
        db.session.commit()
        if rows == 1:
            # Critical Section!
            # If any of this fails - we'll have a run in RUNNING,
            # but no assigned worker. It will be blocked from working.
            for i in range(3):
                if i > 0:
                    time.sleep(0.01)
                try:
                    r = Run.query.get(run_id)
                    r.worker_name = worker.name
                    event = RunEvents(r, BuildStatus.RUNNING)
                    event.worker_name = worker.name
                    db.session.add(event)
                    r.build.refresh_status()
                    db.session.commit()
                    return r
                except Exception:
                    logging.warning("unable to set working info for run")
                    db.session.rollback()
            # Not great but let's try to let it proceed anyway. We'll just
            # have a run with no start time or worker-name, but the CI
            # job will still get to execute.
            logging.error("unable to update run's worker")
            return r


class RunEvents(db.Model, StatusMixin):
    __tablename__ = "run_events"

    id = db.Column(db.Integer, primary_key=True)
    time = db.Column(db.DateTime)
    _status = db.Column(db.Integer)
    run_id = db.Column(db.Integer, db.ForeignKey(Run.id), nullable=False)

    worker_name = db.Column(db.String(512))

    def __init__(self, run, status):
        self.run_id = run.id
        self.status = status
        self.time = datetime.datetime.utcnow()

    def __repr__(self):
        return "<Status %s: %s>" % (self.time, self.status.name)


class Test(db.Model, StatusMixin):
    __tablename__ = "tests"

    id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(db.Integer, db.ForeignKey(Run.id), nullable=False)
    name = db.Column(db.String(512), nullable=False)
    context = db.Column(db.String(1024))
    created = db.Column(db.DateTime, nullable=False)
    _status = db.Column(db.Integer)

    run = db.relationship(Run, back_populates="tests")
    results = db.relationship(
        "TestResult", order_by="TestResult.id", cascade="save-update, merge, delete"
    )

    def __init__(self, run, name, context, status=BuildStatus.QUEUED):
        self.run_id = run.id
        self.name = name
        self.context = context
        self.status = status
        self.created = datetime.datetime.utcnow()

    def as_json(self, detailed=False):
        r = self.run
        b = r.build
        p = b.project
        url = url_for(
            "api_test.test_get",
            proj=p.name,
            build_id=b.build_id,
            run=self.run.name,
            test=self.name,
            _external=True,
        )
        data = {
            "name": self.name,
            "url": url,
            "status": self.status.name,
            "context": self.context,
            "created": self.created,
        }
        if detailed:
            results = []
            data["results"] = results
            for x in self.results:
                results.append(
                    {
                        "name": x.name,
                        "context": x.context,
                        "status": x.status.name,
                        "output": x.output,
                    }
                )
        return data

    def set_status(self, status):
        if isinstance(status, str):
            status = BuildStatus[status]
        if self.status != status:
            self.status = status
            return get_cumulative_status(self.run.tests)

    @property
    def complete(self):
        for result in self.results:
            if not result.complete:
                return False
        return super().complete

    def __repr__(self):
        return "<Test %s: %s>" % (self.name, self.status.name)


class TestResult(db.Model, StatusMixin):
    __tablename__ = "test_results"

    id = db.Column(db.Integer, primary_key=True)
    test_id = db.Column(db.Integer, db.ForeignKey(Test.id), nullable=False)
    name = db.Column(db.String(1024), nullable=False)
    context = db.Column(db.String(1024))
    _status = db.Column(db.Integer)
    output = db.Column(db.Text())

    def __init__(self, test, name, context, status=BuildStatus.QUEUED, output=None):
        self.test_id = test.id
        self.name = name
        self.context = context
        self.status = status
        maxlen = 65535
        if output and len(output) > maxlen:
            # truncate for db
            prefix = "<truncated>\n"
            output = prefix + output[: maxlen - len(prefix)]
        self.output = output

    def __repr__(self):
        return "<TestResult %s: %s>" % (self.name, self.status.name)


class Worker(db.Model):
    __tablename__ = "workers"

    name = db.Column(db.String(512), primary_key=True)
    distro = db.Column(db.String(1024), nullable=False)
    mem_total = db.Column(db.BigInteger, nullable=False)
    cpu_total = db.Column(db.Integer, nullable=False)
    cpu_type = db.Column(db.String(1024), nullable=False)
    enlisted = db.Column(db.Boolean, nullable=False)
    api_key = db.Column(db.String(1024), nullable=False)
    concurrent_runs = db.Column(db.Integer, nullable=False)
    host_tags = db.Column(db.String(1024))
    online = db.Column(db.Boolean)
    surges_only = db.Column(db.Boolean, default=False)

    # we can't delete workers because the Run has foreign keys to them. This
    # flag allows us to exclude them from the api
    deleted = db.Column(db.Boolean, default=False)

    def __init__(
        self,
        name,
        distro,
        mem_total,
        cpu_total,
        cpu_type,
        api_key,
        concurrent_runs,
        host_tags,
    ):
        self.name = name
        self.distro = distro
        self.mem_total = mem_total
        self.cpu_total = cpu_total
        self.cpu_type = cpu_type
        self.api_key = bcrypt.hashpw(api_key.encode(), bcrypt.gensalt())
        self.concurrent_runs = concurrent_runs
        if isinstance(host_tags, list):
            self.host_tags = ",".join(host_tags)
        else:
            self.host_tags = host_tags
        self.online = True
        self.enlisted = False

    def validate_api_key(self, key):
        if isinstance(self.api_key, bytes):
            return bcrypt.checkpw(key.encode(), self.api_key)
        return bcrypt.checkpw(key.encode(), self.api_key.encode())

    def __repr__(self):
        return "<Worker %s - %s/%s>" % (self.name, self.online, self.enlisted)

    def as_json(self, detailed=False):
        url = url_for("api_worker.worker_get", name=self.name, _external=True)
        return {
            "name": self.name,
            "url": url,
            "distro": self.distro,
            "mem_total": self.mem_total,
            "cpu_total": self.cpu_total,
            "cpu_type": self.cpu_type,
            "enlisted": self.enlisted,
            "concurrent_runs": self.concurrent_runs,
            "host_tags": [x for x in self.host_tags.split(",")],
            "online": self.online,
            "surges_only": self.surges_only,
        }

    def in_queue_surge(self):
        """We have some workers that we only want to use when the backlog
        gets big."""
        for tag in self.host_tags.split(","):
            surge = os.path.join(WORKER_DIR, "enable_surge-" + tag.strip())
            if os.path.exists(surge):
                return True
        return False

    @property
    def available(self):
        """Returns True if the worker should be able to accept runs."""
        if self.enlisted and not self.deleted:
            if not self.surges_only or self.in_queue_surge():
                return True
        return False

    def delete(self):
        self.deleted = True
        db.session.commit()
        try:
            rmtree(os.path.join(WORKER_DIR, self.name))
        except FileNotFoundError:
            pass

    def log_event(self, payload):
        logfile = os.path.join(WORKER_DIR, self.name, "events.log")
        if not os.path.exists(logfile):
            os.makedirs(os.path.dirname(logfile))
        with open(logfile, "a") as f:
            f.write(json.dumps(payload))

    @property
    def pings_log(self):
        return os.path.join(WORKER_DIR, self.name, "pings.log")

    def ping(self, **kwargs):
        if not self.online:
            self.online = True
            db.session.commit()
            with StatsClient() as c:
                c.worker_online(self)
        path = self.pings_log
        try:
            os.makedirs(os.path.dirname(path))
        except FileExistsError:
            pass
        now = time.time()
        vals = ",".join(["%s=%s" % (k, v) for k, v in kwargs.items()])
        with open(path, "a") as f:
            f.write("%d: %s\n" % (now, vals))
        try:
            # this is a no-op if unconfigured
            with StatsClient() as c:
                c.worker_ping(self, now, kwargs)
        except Exception:
            logging.exception("Unable to update metrics for " + self.name)
