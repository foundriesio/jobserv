# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

from flask import Blueprint, request

from jobserv.api.run import _authenticate_runner, _get_run, _handle_triggers
from jobserv.jsend import jsendify
from jobserv.models import BuildStatus, Run, Test, TestResult, db
from jobserv.storage import Storage

prefix = "/projects/<project:proj>/builds/<int:build_id>/runs/<run>/tests"
blueprint = Blueprint("api_test", __name__, url_prefix=prefix)


@blueprint.route("/", methods=("GET",))
def test_list(proj, build_id, run):
    r = _get_run(proj, build_id, run)
    return jsendify({"tests": [x.as_json(detailed=False) for x in r.tests]})


@blueprint.route("/<test>/", methods=("GET",))
def test_get(proj, build_id, run, test):
    r = _get_run(proj, build_id, run)
    t = Test.query.filter_by(run_id=r.id, name=test).first_or_404()
    return jsendify({"test": t.as_json(detailed=True)})


def create_test_result(test, test_result_dict):
    name = test_result_dict["name"]
    status = BuildStatus[test_result_dict["status"]]
    context = test_result_dict.get("context")
    output = test_result_dict.get("output")
    db.session.add(TestResult(test, name, context, status, output))


@blueprint.route("/<test>/", methods=("POST",))
def test_create(proj, build_id, run, test):
    r = _get_run(proj, build_id, run)
    _authenticate_runner(r)
    context = ""
    status = results = None
    json = request.get_json()
    if json:
        context = json.get("context")
        status = json.get("status")
        results = json.get("results")

    t = Test(r, test, context)
    db.session.add(t)

    if status:
        t.status = status

    if results:
        db.session.flush()
        for tr in results:
            create_test_result(t, tr)

    db.session.commit()
    return jsendify({})


@blueprint.route("/<test>/", methods=("PUT",))
def test_update(proj, build_id, run, test):
    r = _get_run(proj, build_id, run)
    _authenticate_runner(r)
    t = Test.query.filter_by(name=test).filter(Test.run.has(Run.id == r.id))
    context = request.args.get("context")
    if context:
        t = t.filter(Test.context == context)
    t = t.first_or_404()

    json = request.get_json()
    if json:
        msg = json.get("message")
        status = json.get("status")
        results = json.get("results", [])
        storage = Storage()

        if msg:
            with storage.console_logfd(r, "a") as f:
                f.write(msg)
        if results:
            for tr in results:
                create_test_result(t, tr)
            db.session.commit()
        if status:
            run_status = t.set_status(status)
            db.session.commit()
            if run_status in (BuildStatus.PASSED, BuildStatus.FAILED):
                storage.copy_log(r)
            if run_status is not None:
                with r.build.locked():
                    t.run.set_status(run_status)
                    if r.complete:
                        _handle_triggers(storage, r)

    return jsendify({"complete": t.run.complete})
