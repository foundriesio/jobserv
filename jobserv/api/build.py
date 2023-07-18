# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

from flask import Blueprint, request, url_for

from jobserv.flask import permissions
from jobserv.settings import BUILD_URL_FMT
from jobserv.storage import Storage
from jobserv.jsend import ApiError, get_or_404, jsendify, paginate, paginate_custom
from jobserv.models import Build, BuildStatus, Project, Run, TriggerTypes, db
from jobserv.trigger import trigger_build

blueprint = Blueprint("api_build", __name__, url_prefix="/projects/<project:proj>")


@blueprint.route("/builds/", methods=("GET",))
def build_list(proj):
    p = get_or_404(Project.query.filter(Project.name == proj))
    q = Build.query.filter_by(proj_id=p.id).order_by(Build.id.desc())
    return paginate("builds", q)


@blueprint.route("/builds/", methods=("POST",))
def build_create(proj):
    u = permissions.assert_can_build(proj)
    p = Project.query.filter(Project.name == proj).first_or_404()
    d = request.get_json() or {}

    secrets = {}

    # Check if the caller wants to inherit secrets from something like the
    # "git-poller" trigger for the project.
    trigger_type = d.get("trigger-type")
    if trigger_type:
        optional = trigger_type.endswith("-optional")
        if optional:
            trigger_type = trigger_type[:-9]  # strip off the "-optional"
        for t in p.triggers:
            if TriggerTypes(t.type).name == trigger_type:
                secrets.update(t.secret_data)
                break
        else:
            if not optional:
                raise ApiError(400, "No such trigger-type: %s" % trigger_type)

    # Check if the caller wants to inherit secrets from a specific trigger
    # definied for the project.
    trigger_id = d.get("trigger-id")
    if trigger_id:
        for t in p.triggers:
            if t.id == trigger_id:
                secrets.update(t.secret_data)
                break
        else:
            raise ApiError(400, "Unknown trigger-id: %s" % trigger_id)

    secrets.update(d.get("secrets") or {})
    if "triggered-by" in secrets:
        # Let's not allow triggered-by to be set externally.
        del secrets["triggered-by"]
    if u:
        secrets["triggered-by"] = str(u)
    b = trigger_build(
        p,
        d.get("reason"),
        d.get("trigger-name"),
        d.get("params"),
        secrets,
        d.get("project-definition"),
        d.get("queue-priority", 0),
    )
    url = url_for(
        "api_build.build_get", proj=p.name, build_id=b.build_id, _external=True
    )
    weburl = None
    if BUILD_URL_FMT:
        weburl = BUILD_URL_FMT.format(project=b.project.name, build=b.build_id)
    return jsendify({"url": url, "build_id": b.build_id, "web_url": weburl}, 201)


@blueprint.route("/builds/<int:build_id>/", methods=("PATCH",))
def build_patch(proj, build_id):
    permissions.assert_can_build(proj)
    p = Project.query.filter(Project.name == proj).first_or_404()
    b = get_or_404(Build.query.filter(Build.project == p, Build.build_id == build_id))

    d = request.get_json() or {}
    annotation = d.get("annotation")
    if annotation:
        b.annotation = annotation
        db.session.commit()
        return jsendify({}), 200

    raise ApiError(400, "No changes found in payload")


@blueprint.route("/builds/<int:build_id>/", methods=("GET",))
def build_get(proj, build_id):
    p = get_or_404(Project.query.filter(Project.name == proj))
    b = get_or_404(Build.query.filter(Build.project == p, Build.build_id == build_id))
    return jsendify({"build": b.as_json(detailed=True)})


@blueprint.route("/builds/<int:build_id>/project.yml", methods=("GET",))
def build_get_project_definition(proj, build_id):
    p = get_or_404(Project.query.filter(Project.name == proj))
    b = get_or_404(Build.query.filter(Build.project == p, Build.build_id == build_id))
    pd = Storage().get_project_definition(b)
    return pd, 200, {"Content-Type": "text/yaml"}


@blueprint.route("/builds/latest/", methods=("GET",))
def build_get_latest(proj):
    """Return the most recent successful build"""
    status = BuildStatus.PASSED
    promoted = request.args.get("promoted")
    if promoted:
        status = BuildStatus.PROMOTED
    qs = Build.query.join(Build.project).filter(Project.name == proj)
    all_builds = request.args.get("all")
    if not all_builds:
        qs = qs.filter(Build.status == status)

    trigger = request.args.get("trigger_name")
    if trigger:
        qs = qs.filter(Build.trigger_name == trigger)
    b = get_or_404(qs.order_by(Build.id.desc()))
    return jsendify({"build": b.as_json(detailed=True)})


@blueprint.route("/builds/<int:build_id>/cancel", methods=("POST",))
def build_cancel(proj, build_id):
    permissions.assert_can_build(proj)
    p = get_or_404(Project.query.filter_by(name=proj))
    b = get_or_404(Build.query.filter_by(project=p, build_id=build_id))
    for r in b.runs:
        r.cancel()
    return jsendify({}), 202


@blueprint.route("/builds/<int:build_id>/promote", methods=("POST",))
def build_promote(proj, build_id):
    permissions.assert_can_promote(proj, build_id)
    p = get_or_404(Project.query.filter_by(name=proj))
    b = get_or_404(Build.query.filter_by(project=p, build_id=build_id))

    if not b.complete:
        raise ApiError(400, "Build is not yet complete")

    data = request.get_json()
    if not data:
        raise ApiError(400, "Input data must be JSON")

    b.status = BuildStatus.PROMOTED
    b.name = data.get("name")
    b.annotation = data.get("annotation")
    db.session.commit()
    return jsendify({}, 201)


def _promoted_as_json(storage, build):
    rv = build.as_json(detailed=True)
    rv["tests"] = []
    rv["artifacts"] = []
    for run in build.runs:
        for t in run.tests:
            test = t.as_json(detailed=True)
            test["name"] = "%s-%s" % (run.name, test["name"])
            rv["tests"].append(test)
        for a in storage.list_artifacts(run):
            rv["artifacts"].append("%s/%s" % (run.name, a["name"]))
    return rv


@blueprint.route("/promoted-builds/", methods=("GET",))
def promoted_build_list(proj):
    p = get_or_404(Project.query.filter_by(name=proj))
    q = (
        Build.query.filter(Build.proj_id == p.id)
        .filter(Build.status == BuildStatus.PROMOTED)
        .order_by(Build.id.desc())
    )

    s = Storage()
    return paginate_custom("builds", q, lambda x: _promoted_as_json(s, x))


@blueprint.route("/promoted-builds/<name>/", methods=("GET",))
def promoted_build_get(proj, name):
    b = get_or_404(
        Build.query.join(Project).filter(
            Project.name == proj,
            Build.status == BuildStatus.PROMOTED,
            Build.name == name,
        )
    )
    return jsendify({"build": _promoted_as_json(Storage(), b)})


@blueprint.route("/external-builds/", methods=("POST",))
def external_build_create(proj):
    permissions.assert_can_build(proj)
    p = Project.query.filter(Project.name == proj).first_or_404()
    d = request.get_json() or {}

    b = Build.create(p, init_event_status=BuildStatus.PASSED)
    b.status = BuildStatus.PASSED
    b.trigger_name = d.get("trigger-name")

    for run in d.get("runs") or []:
        r = Run(b, run["name"])
        r.status = BuildStatus.PASSED
        db.session.add(r)

    db.session.commit()
    return jsendify({"build_id": b.build_id}, 201)
