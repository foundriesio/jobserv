# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

import functools
import json
import os
import urllib.parse

from flask import Blueprint, request, send_file

from jobserv.jsend import ApiError, get_or_404, jsendify, paginate
from jobserv.models import Run, Worker, db
from jobserv.project import ProjectDefinition
from jobserv.settings import (
    RUNNER,
    SIMULATOR_SCRIPT,
    SIMULATOR_SCRIPT_VERSION,
    WORKER_SCRIPT,
    WORKER_SCRIPT_VERSION,
)
from jobserv.storage import Storage

blueprint = Blueprint("api_worker", __name__, url_prefix="/")


def _is_worker_authenticated(host):
    key = request.headers.get("Authorization", None)
    if key:
        parts = key.split(" ")
        if len(parts) == 2 and parts[0] == "Token":
            return host.validate_api_key(parts[1])
    return False


def worker_authenticated(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        key = request.headers.get("Authorization", None)
        if not key:
            return jsendify("No Authorization header provided", 401)
        parts = key.split(" ")
        if len(parts) != 2 or parts[0] != "Token":
            return jsendify("Invalid Authorization header", 401)
        worker = get_or_404(Worker.query.filter_by(name=kwargs["name"]))
        if not worker.validate_api_key(parts[1]):
            return jsendify("Incorrect API key for host", 401)
        return f(*args, **kwargs)

    return wrapper


@blueprint.route("workers/", methods=("GET",))
def worker_list():
    return paginate("workers", Worker.query.filter_by(deleted=False))


def _fix_run_urls(rundef):
    rundef = json.loads(rundef)
    parts = urllib.parse.urlparse(request.url)
    public = "%s://%s" % (parts.scheme, parts.hostname)
    if parts.port:
        public += ":%s" % parts.port

    rundef["run_url"] = public + urllib.parse.urlparse(rundef["run_url"]).path
    rundef["runner_url"] = public + urllib.parse.urlparse(rundef["runner_url"]).path
    url = rundef["env"].get("H_TRIGGER_URL")
    if url:
        rundef["env"]["H_TRIGGER_URL"] = public + urllib.parse.urlparse(url).path
    return json.dumps(rundef)


@blueprint.route("workers/<name>/", methods=("GET",))
def worker_get(name):
    w = get_or_404(Worker.query.filter_by(name=name))

    data = w.as_json(detailed=True)
    if _is_worker_authenticated(w):
        data["version"] = WORKER_SCRIPT_VERSION

        if w.enlisted:
            w.ping(**request.args)

        runners = int(request.args.get("available_runners", "0"))
        if runners > 0 and w.available:
            r = Run.pop_queued(w)
            if r:
                try:
                    s = Storage()
                    with s.console_logfd(r, "a") as f:
                        f.write("# Run sent to worker: %s\n" % name)
                    data["run-defs"] = [_fix_run_urls(s.get_run_definition(r))]
                    r.build.refresh_status()
                except:
                    r.worker = None
                    r.status = "QUEUED"
                    db.session.commit()
                    raise

    return jsendify({"worker": data})


@blueprint.route("workers/<name>/", methods=["POST"])
def worker_create(name):
    worker = request.get_json() or {}
    required = (
        "api_key",
        "distro",
        "mem_total",
        "cpu_total",
        "cpu_type",
        "concurrent_runs",
        "host_tags",
    )
    missing = []
    for x in required:
        if x not in worker:
            missing.append(x)
    if missing:
        raise ApiError(400, "Missing required field(s): " + ", ".join(missing))

    w = Worker(
        name,
        worker["distro"],
        worker["mem_total"],
        worker["cpu_total"],
        worker["cpu_type"],
        worker["api_key"],
        worker["concurrent_runs"],
        worker["host_tags"],
    )
    w.surges_only = worker.get("surges_only", False)
    db.session.add(w)
    db.session.commit()
    return jsendify({}, 201)


@blueprint.route("workers/<name>/", methods=["PATCH"])
@worker_authenticated
def worker_update(name):
    w = get_or_404(Worker.query.filter_by(name=name, deleted=False))
    data = request.get_json() or {}
    attrs = (
        "distro",
        "mem_total",
        "cpu_total",
        "cpu_type",
        "concurrent_runs",
        "host_tags",
    )
    for attr in attrs:
        val = data.get(attr)
        if val is not None:
            setattr(w, attr, val)
    db.session.commit()
    return jsendify({}, 200)


@blueprint.route("workers/<name>/events/", methods=["POST"])
@worker_authenticated
def worker_event(name):
    w = get_or_404(Worker.query.filter_by(name=name, deleted=False))
    if not w.enlisted:
        return jsendify({}, 403)
    payload = request.get_json()
    if payload:
        w.log_event(payload)
    return jsendify({}, 201)


@blueprint.route("runner", methods=("GET",))
def runner_download():
    return send_file(open(RUNNER, "rb"), mimetype="application/zip")


@blueprint.route("worker", methods=("GET",))
def worker_download():
    return send_file(open(WORKER_SCRIPT, "rb"), mimetype="text/plain")


@blueprint.route("simulator", methods=("GET",))
def simulator_download():
    version = request.args.get("version")
    if version == SIMULATOR_SCRIPT_VERSION:
        return "", 304
    return send_file(open(SIMULATOR_SCRIPT, "rb"), mimetype="text/plain")


@blueprint.route("simulator-validate", methods=("POST",))
def simulator_validate():
    data = request.get_json()
    if not data:
        raise ApiError(400, "run-definition must be posted as json data")

    try:
        ProjectDefinition.validate_data(data)
    except Exception as e:
        raise ApiError(400, str(e))
    return jsendify({})


@blueprint.route("version", methods=("GET",))
def version_get():
    return jsendify({"version": os.environ.get("APP_VERSION")})
