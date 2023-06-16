from copy import deepcopy
import logging
import sys

from flask import Flask, request
import json_logging


log = logging.getLogger("api")


def log_init(app: Flask):
    json_logging.init_flask(custom_formatter=LogFormatter, enable_json=True)
    json_logging.init_request_instrument(app, custom_formatter=RequestFormatter)

    log.setLevel(logging.DEBUG)
    log.addHandler(logging.StreamHandler(sys.stdout))

    @app.after_request
    def after_request_func(response):
        cid = json_logging.get_correlation_id(request=request)
        response.headers["x-correlation-id"] = cid
        return response


def log_add_ctx(**kwargs: dict):
    ctx_copy = deepcopy(kwargs)
    try:
        request.logctx.update(ctx_copy)
    except AttributeError:
        request.logctx = ctx_copy



def _add_ctx(record):
    try:
        record.update(request.logctx)
    except AttributeError:
        pass
    return record


class LogFormatter(json_logging.JSONLogWebFormatter):
    def _format_log_object(self, record, request_util):
        return _add_ctx(super()._format_log_object(record, request_util))


class RequestFormatter(json_logging.JSONRequestLogFormatter):
    def _format_log_object(self, record, request_util):
        return _add_ctx(super()._format_log_object(record, request_util))
