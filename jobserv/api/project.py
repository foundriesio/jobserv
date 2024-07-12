# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

from flask import Blueprint, request, url_for

from jobserv.flask import permissions
from jobserv.jsend import ApiError, get_or_404, jsendify, paginate_custom
from jobserv.models import (
    Build,
    BuildStatus,
    Project,
    ProjectTrigger,
    Run,
    TriggerTypes,
    db,
)

blueprint = Blueprint("api_project", __name__, url_prefix="/projects")


@blueprint.route("/", methods=("GET",))
def project_list():
    projects = permissions.projects_list().order_by(Project.name)
    return jsendify({"projects": [x.as_json() for x in projects]})


@blueprint.route("/", methods=("POST",))
def project_create():
    d = request.get_json() or {}
    proj = d.get("name")
    if not proj:
        raise ApiError(401, 'Missing required parameter: "name"')
    sync = d.get("synchronous-builds", False)

    permissions.assert_create_project(proj)
    db.session.add(Project(proj, sync))
    db.session.commit()

    url = url_for("api_project.project_get", proj=proj, _external=True)
    return jsendify({"url": url}, 201)


@blueprint.route("/<project:proj>/", methods=("DELETE",))
def project_delete(proj):
    permissions.assert_can_delete(proj)

    p = get_or_404(Project.query.filter_by(name=proj))

    if request.get_json().get("I_REALLY_MEAN_TO_DO_THIS") != "YES":
        raise ApiError(401, 'Missing required parameter: "name"')

    for t in p.triggers:
        db.session.delete(t)
    db.session.commit()

    for b in p.builds:
        db.session.delete(b)
    db.session.commit()

    db.session.delete(p)
    db.session.commit()
    return jsendify({"TODO": "Delete storage artifacts"})


@blueprint.route("/<project:proj>/", methods=("GET",))
def project_get(proj):
    p = get_or_404(Project.query.filter_by(name=proj))
    return jsendify({"project": p.as_json(detailed=True)})


@blueprint.route("/<project:proj>/history/<run>/", methods=("GET",))
def project_run_history(proj, run):
    q = (
        Run.query.join(Build, Project)
        .filter(Project.name == proj, Run.name == run)
        .order_by(-Build.id)
    )

    def render(run):
        r = run.as_json(detailed=True)
        r["build"] = run.build.build_id
        for x in reversed(run.status_events):
            if x.status == BuildStatus.RUNNING:
                d = run.status_events[-1].time - x.time
                r["duration_seconds"] = d.total_seconds()
                break
        return r

    return paginate_custom("runs", q, render)


@blueprint.route("/<project:proj>/triggers/", methods=("GET",))
def project_trigger_list(proj):
    permissions.assert_can_build(proj)
    p = get_or_404(Project.query.filter_by(name=proj))
    triggers = p.triggers

    t = request.args.get("type")
    if t:
        triggers = [x for x in triggers if x.type == TriggerTypes[t].value]

    # Remove the secret values, no need to ever expose them
    redacted = []
    for t in triggers:
        data = t.as_json()
        data["secrets"] = [{"name": x} for x in (data.get("secrets") or {}).keys()]
        redacted.append(data)
    return jsendify(redacted)


@blueprint.route("/<project:proj>/triggers/", methods=("POST",))
def project_create_trigger(proj):
    u = permissions.assert_create_trigger(proj)
    p = get_or_404(Project.query.filter_by(name=proj))

    d = request.get_json() or {}
    ttype = d.pop("type")
    if not ttype:
        raise ApiError(401, "Missing parameter: type")
    ttype = TriggerTypes[ttype].value

    try:
        owner = d.pop("owner")
    except KeyError:
        owner = "unknown-internal"
    if u:
        owner = str(u)
    dr = df = None
    try:
        dr = d.pop("definition_repo")
        df = d.pop("definition_file")
    except KeyError:
        pass

    try:
        secrets = d.pop("secrets")
        # convert array of [{'name': <name>, 'value': <value>}, ...] dicts
        # into dictionary of {<name>: <value>, ...}
        secrets = {x["name"]: x["value"] for x in secrets}
    except KeyError:
        secrets = d

    try:
        db.session.add(ProjectTrigger(owner, ttype, p, dr, df, secrets))
    except ValueError as e:
        raise ApiError(400, str(e))

    db.session.commit()
    return jsendify({}, 201)


@blueprint.route("/<project:proj>/triggers/<int:tid>/", methods=("PATCH",))
def project_patch_trigger(proj, tid):
    permissions.assert_can_build(proj)
    trigger = get_or_404(ProjectTrigger.query.filter(ProjectTrigger.id == tid))
    if trigger.project.name != proj:
        # someone is trying to do something fishy
        return jsendify({}, 404)

    data = request.get_json() or {}
    val = data.get("definition_file")
    if val:
        trigger.definition_file = val

    val = data.get("definition_repo")
    if val:
        trigger.definition_repo = val

    val = data.get("secrets")
    if val:
        # convert array of [{'name': <name>, 'value': <value>}, ...] dicts
        # into dictionary of {<name>: <value>, ...}
        current_secrets = trigger.secret_data
        for secret in val:
            name, value = secret["name"], secret["value"]
            if value is None:
                if name in current_secrets:
                    del current_secrets[name]
            else:
                current_secrets[name] = value
        try:
            trigger.update_secrets()
        except ValueError as e:
            raise ApiError(400, str(e))
    db.session.commit()
    return jsendify({})
