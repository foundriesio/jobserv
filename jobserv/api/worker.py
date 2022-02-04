# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

import functools
import json
import os
import urllib.parse

from flask import Blueprint, request, send_file
from jwt.exceptions import PyJWTError

from jobserv.jsend import ApiError, get_or_404, jsendify, paginate
from jobserv.models import Project, Run, Worker, db
from jobserv.project import ProjectDefinition
from jobserv.settings import (
    RUNNER,
    SIMULATOR_SCRIPT,
    SIMULATOR_SCRIPT_VERSION,
    WORKER_SCRIPT,
    WORKER_SCRIPT_VERSION,
)
from jobserv.storage import Storage
from jobserv.worker_jwt import worker_from_jwt

blueprint = Blueprint("api_worker", __name__, url_prefix="/")


def _is_worker_authenticated(worker: Worker):
    key = request.headers.get("Authorization", None)
    if key:
        parts = key.split(" ")
        if len(parts) == 2 and parts[0] == "Token":
            return worker.validate_api_key(parts[1])
        if len(parts) == 2 and parts[0] == "Bearer":
            try:
                w = worker_from_jwt(parts[1])
                return w.name == worker.name
            except PyJWTError:
                pass
    return False


def worker_authenticated(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        key = request.headers.get("Authorization", None)
        if not key:
            return jsendify("No Authorization header provided", 401)
        parts = key.split(" ")
        if len(parts) != 2 or parts[0] not in ("Token", "Bearer"):
            return jsendify("Invalid Authorization header", 401)

        if parts[0] == "Bearer":
            try:
                w = worker_from_jwt(parts[1])
            except PyJWTError as e:
                return jsendify(str(e), 401)

            if w.name != kwargs["name"]:
                # worker can only access its self
                return jsendify("Not found", 404)

            worker = Worker.query.filter(Worker.name == w.name).first()
            if worker is None:
                # This looks a little nutty - constructing this object with
                # basically "I have no idea" data. But the worker will call
                # us with `worker_update` on its first connection which will
                # fill these handy but not mission-cricital fields out.
                worker = Worker(w.name, "?", 1, 1, "?", "", 1, w.allowed_tags)
                worker.enlisted = True
                db.session.add(worker)
                db.session.commit()
            worker.allowed_tags = w.allowed_tags
        else:
            worker = get_or_404(
                Worker.query.filter_by(name=kwargs["name"], deleted=False)
            )
            if not worker.validate_api_key(parts[1]):
                return jsendify("Incorrect API key for host", 401)
            worker.allowed_tags = []
        request.worker = worker
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
    if not w.deleted and _is_worker_authenticated(w):
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
                except Exception:
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
            if attr == "host_tags" and request.worker.allowed_tags:
                # make sure the worker isn't try to access things it shouldn't
                rejects = set(val.split(",")) - set(request.worker.allowed_tags)
                if rejects:
                    raise ApiError(
                        403, f"Worker not allowed access to host_tags: {rejects}"
                    )
            setattr(request.worker, attr, val)
    db.session.commit()
    return jsendify({}, 200)


@blueprint.route("workers/<name>/events/", methods=["POST"])
@worker_authenticated
def worker_event(name):
    if not request.worker.enlisted:
        return jsendify({}, 403)
    payload = request.get_json()
    if payload:
        request.worker.log_event(payload)
    return jsendify({}, 201)


@blueprint.route("workers/<name>/volumes-deleted/", methods=["GET"])
@worker_authenticated
def worker_deleted_volumes(name):
    """Inform the worker of volumes that should be deleted by taking in a list
    of project prefixes the worker has. This may seem a little convoluted,
    but it's slightly more secure than just giving a worker a list of
    all projects on the server. This way multi-tenant workers can't ask
    for a list of all the other tenants.

    NOTE: We are really giving the client *prefixes* to delete. They'll send
    us something like "customer-1" and we'll have "customer-1/lmp" as a
    project. If "customer-1/lmp" gets removed, the response would be
    "customer-1" so that *all* volumes are removed under that namespace
    """
    if not request.worker.enlisted:
        return jsendify({}, 403)
    payload = request.get_json() or {}
    directories = payload.get("directories")
    if not directories:
        return jsendify("Missing required argument 'directories'", 400)

    deletes = []
    projects = [x.name for x in Project.query.order_by(Project.name)]
    for d in directories:
        for p in projects:
            if p.startswith(d):
                break
        else:
            deletes.append(d)
    return jsendify({"volumes": deletes}, 200)


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
