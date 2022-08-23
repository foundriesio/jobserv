# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

import datetime

from importlib import import_module

from flask import Flask, request
from flask.json import JSONEncoder
from flask_migrate import Migrate

from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.routing import UnicodeConverter

from jobserv.settings import PROJECT_NAME_REGEX

from jobserv.jsend import jsendify
from jobserv.settings import PERMISSIONS_MODULE

permissions = import_module(PERMISSIONS_MODULE)


class ISO8601_JSONEncoder(JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime.datetime):
            return obj.isoformat() + "+00:00"
        return super().default(obj)


class ProjectConverter(UnicodeConverter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, *kwargs)
        if PROJECT_NAME_REGEX:
            self.regex = PROJECT_NAME_REGEX


def _user_has_permission():
    # These are secured by "authenticate_runner" and "assert_internal_user"
    if request.method in ("POST", "PATCH", "PUT"):
        return

    if request.path.startswith("/projects/") and len(request.path) > 10:
        path = request.path[10:]
        if path and not permissions.project_can_access(path):
            return jsendify("Object does not exist: " + request.path, 404)

    if request.path.startswith("/health/"):
        path = request.path[8:]
        if not permissions.health_can_access(path):
            return jsendify("Object does not exist: " + request.path, 404)


def _handle_404(e):
    return jsendify("Not Found", 404)


def create_app(settings_object="jobserv.settings"):
    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app)
    app.config.from_object(settings_object)

    ProjectConverter.settings = settings_object
    app.url_map.converters["project"] = ProjectConverter

    from jobserv.models import db

    db.init_app(app)
    Migrate(app, db)

    import jobserv.api

    jobserv.api.register_blueprints(app)

    from jobserv.storage import Storage

    if Storage.blueprint:
        app.register_blueprint(Storage.blueprint)

    app.json_encoder = ISO8601_JSONEncoder
    app.before_request(_user_has_permission)
    app.register_error_handler(404, _handle_404)
    return app
