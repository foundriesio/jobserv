# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

from flask import Blueprint, request

from jobserv.flask import permissions
from jobserv.jsend import ApiError, jsendify
from jobserv.models import BuildStatus, Test

blueprint = Blueprint("api_test_query", __name__, url_prefix="/")


@blueprint.route("find_test/", methods=("GET",))
def test_find():
    permissions.assert_internal_user()
    context = request.args.get("context")
    if not context:
        raise ApiError(401, {"message": 'Missing "context" query argument'})

    tests = []
    for t in Test.query.filter_by(context=context):
        tests.append(t.as_json(detailed=True))
        tests[-1]["metadata"] = t.run.meta
        tests[-1]["api_key"] = t.run.api_key
    return jsendify({"tests": tests})


@blueprint.route("incomplete_tests/", methods=("GET",))
def test_incomplete_list():
    permissions.assert_internal_user()
    tests = []
    complete = (BuildStatus.PASSED, BuildStatus.FAILED)
    for t in Test.query.filter(~Test.status.in_(complete)):
        tests.append(t.as_json(detailed=True))
        tests[-1]["metadata"] = t.run.meta
        tests[-1]["api_key"] = t.run.api_key
    return jsendify({"tests": tests})
