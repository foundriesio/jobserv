# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>
import traceback

from sqlalchemy.exc import DataError

from flask import current_app

from jobserv.api.build import blueprint as build_bp
from jobserv.api.github import blueprint as github_bp
from jobserv.api.gitlab import blueprint as gitlab_bp
from jobserv.api.health import blueprint as health_bp
from jobserv.api.project import blueprint as project_bp
from jobserv.api.project_triggers import blueprint as project_triggers_bp
from jobserv.api.run import blueprint as run_bp
from jobserv.api.test import blueprint as test_bp
from jobserv.api.test_query import blueprint as test_query_bp
from jobserv.api.worker import blueprint as worker_bp
from jobserv.jsend import ApiError, jsendify

BLUEPRINTS = (
    project_bp,
    project_triggers_bp,
    build_bp,
    run_bp,
    test_bp,
    test_query_bp,
    worker_bp,
    health_bp,
    github_bp,
    gitlab_bp,
)


def register_blueprints(app):
    for bp in BLUEPRINTS:

        @bp.errorhandler(ApiError)
        def api_error(e):
            return e.resp

        @bp.errorhandler(DataError)
        def data_error(e):
            data = {
                "message": "An unexpected error occurred inserting data",
                "error_msg": str(e),
                "stack_trace": traceback.format_exc(),
            }
            current_app.logger.exception(
                "Unexpected DB error caught in BP error handler"
            )
            return jsendify(data, 400)

        @bp.errorhandler(Exception)
        def unexpected_error(e):
            data = {
                "message": "An unexpected error occurred",
                "error_msg": str(e),
                "stack_trace": traceback.format_exc(),
            }
            print(dir(bp))
            current_app.logger.exception("Unexpected error caught in BP error handler")
            return jsendify(data, 500)

        app.register_blueprint(bp)
