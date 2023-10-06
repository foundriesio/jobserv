# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

import json
import re

import yaml

from flask import Blueprint, current_app, make_response, request, send_file, url_for

from jobserv.flask import permissions
from jobserv.storage import Storage
from jobserv.jsend import ApiError, get_or_404, jsendify
from jobserv.models import db, Build, BuildStatus, Project, Run, Test, TestResult
from jobserv.project import ProjectDefinition
from jobserv.notify import notify_build_complete_email, notify_build_complete_webhook
from jobserv.trigger import trigger_runs

prefix = "/projects/<project:proj>/builds/<int:build_id>/runs"
blueprint = Blueprint("api_run", __name__, url_prefix=prefix)


@blueprint.route("/", methods=("GET",))
def run_list(proj, build_id):
    p = get_or_404(Project.query.filter_by(name=proj))
    b = get_or_404(Build.query.filter_by(project=p, build_id=build_id))
    return jsendify({"runs": [x.as_json(detailed=False) for x in b.runs]})


def _get_run(proj, build_id, run):
    p = get_or_404(Project.query.filter_by(name=proj))
    b = get_or_404(Build.query.filter_by(project=p, build_id=build_id))
    return (
        Run.query.filter_by(name=run)
        .filter(Run.build.has(Build.id == b.id))
        .first_or_404()
    )


@blueprint.route("/<run>/", methods=("GET",))
def run_get(proj, build_id, run):
    r = _get_run(proj, build_id, run)
    data = r.as_json(detailed=True)
    artifacts = []
    v2 = request.args.get("version") == "v2"
    for a in Storage().list_artifacts(r):
        u = url_for(
            "api_run.run_get_artifact",
            proj=proj,
            build_id=build_id,
            run=run,
            path=a["name"],
            _external=True,
        )
        if v2:
            artifacts.append({"url": u, "size_bytes": a["size_bytes"]})
        else:
            artifacts.append(u)
    data["artifacts"] = artifacts
    return jsendify({"run": data})


def _create_triggers(
    projdef, storage, build, params, secrets, triggers, parent_trigger, queue_priority
):
    email = parent_trigger.get("email")
    webhooks = parent_trigger.get("webhooks")
    upgrade_projdef = False
    for trigger in triggers:
        run_names = trigger.get("run-names")
        trigger = projdef.get_trigger(trigger["name"])
        trigger["run-names"] = run_names
        if not trigger.get("email") and email:
            trigger["email"] = email
            upgrade_projdef = True
        if not trigger.get("webhooks") and webhooks:
            trigger["webhooks"] = webhooks
            upgrade_projdef = True
        trigger_runs(
            storage,
            projdef,
            build,
            trigger,
            params,
            secrets,
            parent_trigger["type"],
            queue_priority,
        )
    if upgrade_projdef:
        storage.create_project_definition(
            build, yaml.dump(projdef._data, default_flow_style=False)
        )


def _handle_build_complete(projdef, storage, build, params, secrets, trigger):
    if build.status == BuildStatus.PASSED:
        # we don't want to pass "trigger params" since this is a build-level
        # trigger, but we do want the context of the build url, so
        # convert http://foo/build/1/runs/ into http://foo/build/1/
        url = params["H_TRIGGER_URL"]
        params = {
            "H_TRIGGER_URL": url[: url.find("/runs/") + 1],
        }
        triggers = trigger.get("triggers", [])
        if triggers:
            # We need to guess what queue_priority we should use. They should
            # all be the same based on the current architecture, so:
            queue_priority = build.runs[0].queue_priority
            build_params = storage.get_build_params(build)
            params.update(build_params)
            _create_triggers(
                projdef,
                storage,
                build,
                params,
                secrets,
                triggers,
                trigger,
                queue_priority,
            )
            db.session.flush()
            build.refresh_status()
            return  # Exit before we try and send email

    email = trigger.get("email", projdef.project_email)
    if email:
        if build.status == BuildStatus.FAILED or not email.get("only_failures"):
            notify_build_complete_email(build, email["users"])
    webhooks = trigger.get("webhooks", projdef.project_webhooks)
    if webhooks:
        for webhook in webhooks:
            if build.status == BuildStatus.FAILED or not webhook.get("only_failures"):
                secret = secrets.get(webhook["secret_name"], None)
                if secret:
                    notify_build_complete_webhook(build, webhook["url"], secret)


def _handle_triggers(storage, run):
    if not run.complete or not run.trigger:
        return

    projdef = ProjectDefinition(
        yaml.safe_load(storage.get_project_definition(run.build))
    )
    rundef = storage.get_run_definition(run)
    secrets = rundef.get("secrets")
    params = rundef.get("env", {})
    params["H_TRIGGER_URL"] = request.url

    run_trigger = projdef.get_trigger(run.trigger)
    try:
        for rt in run_trigger["runs"]:
            if rt["name"] == run.name:
                if run.status == BuildStatus.PASSED:
                    _create_triggers(
                        projdef,
                        storage,
                        run.build,
                        params,
                        secrets,
                        rt.get("triggers", []),
                        run_trigger,
                        run.queue_priority,
                    )
                    db.session.refresh(run.build)
                    run.build.refresh_status()
        if run.build.complete:
            _handle_build_complete(
                projdef, storage, run.build, params, secrets, run_trigger
            )
    except ValueError as e:
        current_app.logger.exception(
            "Caught integrity error and failed run: %d", run.id
        )
        run.set_status(BuildStatus.FAILED)
        content = storage.get_artifact_content(run, "console.log")
        with storage.console_logfd(run, "w") as f:
            f.write(content)
            f.write("\n\n== ERROR TRIGGERING RUN: %s\n" % e)
        storage.copy_log(run)


def _failed_tests(storage, run):
    failures = False
    rundef = storage.get_run_definition(run)
    grepping = rundef.get("test-grepping")
    if grepping:
        test_pat = grepping.get("test-pattern")
        if test_pat:
            test_pat = re.compile(test_pat)
        res_pat = re.compile(grepping["result-pattern"])
        fixups = grepping.get("fixupdict", {})
        cur_test = None
        with storage.console_logfd(run, "r") as f:
            for line in f.readlines():
                if test_pat:
                    m = test_pat.match(line)
                    if m:
                        if cur_test:
                            statuses = [x.status for x in cur_test.results]
                            if BuildStatus.FAILED in statuses:
                                cur_test.status = BuildStatus.FAILED
                        cur_test = Test(
                            run,
                            m.group("name"),
                            grepping["test-pattern"],
                            BuildStatus.PASSED,
                        )
                        db.session.add(cur_test)
                        db.session.flush()
                m = res_pat.match(line)
                if m:
                    result = m.group("result")
                    result = fixups.get(result, result)
                    if result == "FAILED":
                        failures = True
                        if cur_test:
                            cur_test.status = result
                    if not cur_test:
                        cur_test = Test(run, "default", None, result)
                        db.session.add(cur_test)
                        db.session.flush()
                    db.session.add(TestResult(cur_test, m.group("name"), None, result))
        db.session.commit()
    return failures


def _running_tests(run):
    for t in run.tests:
        if not t.complete:
            return True
    return False


def _authenticate_runner(run):
    key = request.args.get("apikey")
    if key and key == run.api_key:
        return
    key = request.headers.get("Authorization", None)
    if not key:
        raise ApiError(401, {"message": "No Authorization header provided"})
    parts = key.split(" ")
    if len(parts) != 2 or parts[0] != "Token":
        raise ApiError(401, {"message": "Invalid Authorization header"})
    if parts[1] != run.api_key:
        raise ApiError(401, {"message": "Incorrect API key"})
    if run.complete:
        raise ApiError(401, {"message": "Run has already completed"})


@blueprint.route("/<run>/", methods=("POST",))
def run_update(proj, build_id, run):
    r = _get_run(proj, build_id, run)
    _authenticate_runner(r)

    storage = Storage()
    if request.data:
        with storage.console_logfd(r, "ab") as f:
            f.write(request.data)

    metadata = request.headers.get("X-RUN-METADATA")
    if metadata:
        r.meta = metadata
        db.session.commit()

    status = request.headers.get("X-RUN-STATUS")
    if status:
        status = BuildStatus[status]
        if r.status != status:
            if status in (BuildStatus.PASSED, BuildStatus.FAILED):
                if _running_tests(r):
                    status = BuildStatus.RUNNING
                if _failed_tests(storage, r):
                    status = BuildStatus.FAILED
                storage.copy_log(r)
            with r.build.locked():
                r.set_status(status)
                if r.complete:
                    _handle_triggers(storage, r)

    resp = jsendify({})
    if r.status == BuildStatus.CANCELLING:
        resp.headers["X-JOBSERV-CANCEL"] = "1"
    return resp


@blueprint.route("/<run>/rerun", methods=("POST",))
def run_rerun(proj, build_id, run):
    r = _get_run(proj, build_id, run)
    permissions.assert_can_rerun(r)
    for t in r.tests:
        db.session.delete(t)
    r.set_status(BuildStatus.QUEUED)
    db.session.commit()
    return jsendify({})


@blueprint.route("/<run>/cancel", methods=("POST",))
def run_cancel(proj, build_id, run):
    r = _get_run(proj, build_id, run)
    permissions.assert_can_build(r.build.project.name)
    r.cancel()
    return jsendify({}), 202


def _get_run_def(proj, build_id, run):
    r = _get_run(proj, build_id, run)
    rundef = Storage().get_run_definition(r)
    try:
        _authenticate_runner(r)
    except ApiError:
        if not permissions.run_can_access_secrets(r):
            # The requestor is not authorized to view secrets
            secrets = rundef.get("secrets")
            if secrets:
                rundef["secrets"] = {k: "TODO" for k, v in secrets.items()}
        del rundef["api_key"]
    return rundef


@blueprint.route("/<run>/.rundef.json", methods=("GET",))
def run_get_definition(proj, build_id, run):
    rundef = json.dumps(_get_run_def(proj, build_id, run), indent=2)
    return rundef, 200, {"Content-Type": "application/json"}


@blueprint.route("/<run>/progress-regex", methods=("GET",))
def run_get_progress_regex(proj, build_id, run):
    r = _get_run(proj, build_id, run)
    rundef = Storage().get_run_definition(r)
    progress = rundef.get("console-progress")
    if progress:
        return jsendify(progress)
    raise ApiError(404, {"message": "Run has not defined console-progress"})


@blueprint.route("/<run>/.simulate.sh", methods=("GET",))
def run_get_simulate_sh(proj, build_id, run):
    runner = url_for("api_worker.runner_download", _external=True)
    rundef = _get_run_def(proj, build_id, run)
    rundef["runner_url"] = runner
    # rundef must be shell escaped friendly so that it will work in the script:
    rundef_str = json.dumps(rundef, indent=2)
    rundef_str = rundef_str.replace("$", "\\$")
    rundef_str = rundef_str.replace("\\", "\\\\")

    script = """#!/bin/sh -e

SIMDIR="${{SIMDIR-/tmp/sim-run}}"
echo "Creating JobServ simulation under $SIMDIR"
mkdir $SIMDIR
cd $SIMDIR

cat >rundef.json <<EIEIO
{rundef}
EIEIO

wget -O runner {runner}
PYTHONPATH=./runner python3 -m jobserv_runner.simulator -w `pwd` rundef.json
    """.format(
        rundef=rundef_str, runner=runner
    )
    return script, 200, {"Content-Type": "text/plain"}


@blueprint.route("/<run>/<path:path>", methods=("GET",))
def run_get_artifact(proj, build_id, run, path):
    r = _get_run(proj, build_id, run)
    if r.complete:
        storage = Storage()
        if path.endswith(".html"):
            # we are probably trying to render a static site like a build of
            # ltd-docs. Return its content rather than a redirect so it will
            # render in the browser
            content = storage.get_artifact_content(r, path)
            return content, 200, {"Content-Type": "text/html"}
        resp = storage.get_download_response(request, r, path)
        resp.headers["X-RUN-STATUS"] = r.status.name
        return resp

    if path != "console.log":
        raise ApiError(404, {"message": "Run in progress, no artifacts available"})

    if r.status == BuildStatus.QUEUED:
        msg = "# Waiting for worker with tag: " + r.host_tag
        return (msg, 200, {"Content-Type": "text/plain", "X-RUN-STATUS": r.status.name})
    try:
        fd = Storage().console_logfd(r, "rb")
        offset = request.headers.get("X-OFFSET")
        if offset:
            offset = int(offset)
            end = fd.seek(0, 2)
            if offset >= end:
                return (
                    b"",
                    200,
                    {"Content-Type": "text/html", "X-RUN-STATUS": r.status.name},
                )
            fd.seek(offset, 0)
        resp = make_response(send_file(fd, mimetype="text/plain"))
        resp.headers["X-RUN-STATUS"] = r.status.name
        return resp

    except FileNotFoundError:
        # This is a race condition. The run completed while we were checking
        return Storage().get_download_response(request, r, path)


@blueprint.route("/<run>/create_signed", methods=("POST",))
def run_upload(proj, build_id, run):
    r = _get_run(proj, build_id, run)
    _authenticate_runner(r)

    data = request.get_json()
    urls = {}
    if data:
        # determine url expiration, default 1800 = 30 minues
        expiration = request.headers.get("X-URL-EXPIRATION", 1800)
        urls = Storage().generate_signed(r, data, expiration)

    return jsendify({"urls": urls})
