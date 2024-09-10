# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

import json_logging
from sqlalchemy.exc import DataError
from werkzeug.exceptions import HTTPException

from flask import current_app

from jobserv.api.build import blueprint as build_bp
from jobserv.api.github import blueprint as github_bp
from jobserv.api.gitlab import blueprint as gitlab_bp
from jobserv.api.health import blueprint as health_bp
from jobserv.api.project import blueprint as project_bp
from jobserv.api.project_triggers import blueprint as project_triggers_bp
from jobserv.api.run import blueprint as run_bp
from jobserv.api.test import blueprint as test_bp
from jobserv.api.worker import blueprint as worker_bp
from jobserv.jsend import ApiError, jsendify

BLUEPRINTS = (
    project_bp,
    project_triggers_bp,
    build_bp,
    run_bp,
    test_bp,
    worker_bp,
    health_bp,
    github_bp,
    gitlab_bp,
)


def register_blueprints(app):
    @app.errorhandler(ApiError)
    def api_error(e):
        return e.resp

    @app.errorhandler(HTTPException)
    def werkzeug_err(e):
        if e.response:
            return e.response
        return e.description, e.code

    @app.errorhandler(DataError)
    def data_error(e):
        cor_id = json_logging.get_correlation_id()
        data = {
            "message": "An unexpected error occurred inserting data. Correlation ID: {cor_id}",
            "error_msg": str(e),
        }
        current_app.logger.exception("Unexpected DB error caught in BP error handler")
        return jsendify(data, 400)

    @app.errorhandler(FileNotFoundError)
    def notfound_error(e):
        data = {
            "message": "Not found",
        }
        return jsendify(data, 404)

    @app.errorhandler(Exception)
    def unexpected_error(e):
        cor_id = json_logging.get_correlation_id()
        data = {
            "message": "An unexpected error occurred. Correlation ID: {cor_id}",
            "error_msg": str(e),
        }
        current_app.logger.exception("Unexpected error caught in BP error handler")
        return jsendify(data, 500)

    for bp in BLUEPRINTS:
        app.register_blueprint(bp)

    @app.route("/healthz")
    def _healthz():
        return ""
