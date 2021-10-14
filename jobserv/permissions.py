# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>
import hmac
import os
import time

import requests

from flask import request

from jobserv.models import Project
from jobserv.jsend import ApiError

INTERNAL_API_KEY = os.environ.get("INTERNAL_API_KEY", "").encode()


def projects_list():
    """Allow anyone to see if a project exists."""
    return Project.query


def project_can_access(project_path):
    """Allow anyone to access a project."""
    return True


def run_can_access_secrets(run):
    """Can a user access the secrets in rundef.json"""
    return False


def health_can_access(health_path):
    """Allow anyone to access to the health endpoints."""
    return True


def assert_internal_user():
    """A function that checks request headers to ensure the caller is a valid
    internal user."""
    if not INTERNAL_API_KEY:
        raise RuntimeError("JobServ missing INTERNAL_API_KEY")

    sig = request.headers.get("X-JobServ-Sig")
    ts = request.headers.get("X-Time")
    if not sig:
        raise ApiError(401, "X-JobServ-Sig not provided")
    if not ts:
        raise ApiError(401, "X-Time not provided")
    msg = "%s,%s,%s" % (request.method, ts, request.base_url)
    computed = hmac.new(INTERNAL_API_KEY, msg.encode(), "sha1").hexdigest()
    if not hmac.compare_digest(sig, computed):
        raise ApiError(401, "Invalid signature")


def assert_can_promote(project, build_id):
    """Is the requestor allowed to promote this build."""
    assert_internal_user()


def assert_can_build(project):
    """Is the requestor allowed to trigger a build."""
    assert_internal_user()


def assert_can_rerun(run):
    """Is the requestor allowed to re-run this run"""
    assert_internal_user()


def assert_create_trigger(proj):
    """Is the requestor allowed to create triggers on a project."""
    return assert_internal_user()


def assert_can_delete(project):
    """Is the requestor allowed to delete a project."""
    assert_internal_user()


def _sign(url, headers, method):
    headers["X-Time"] = str(round(time.time()))
    msg = "%s,%s,%s" % (method, headers["X-Time"], url)
    sig = hmac.new(INTERNAL_API_KEY, msg.encode(), "sha1").hexdigest()
    headers["X-JobServ-Sig"] = sig


def internal_get(url, *args, **kwargs):
    _sign(url, kwargs.setdefault("headers", {}), "GET")
    return requests.get(url, *args, **kwargs)


def internal_post(url, *args, **kwargs):
    _sign(url, kwargs.setdefault("headers", {}), "POST")
    return requests.post(url, *args, **kwargs)
