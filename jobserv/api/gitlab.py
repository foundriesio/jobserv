# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

import hmac
import traceback
import yaml

from urllib.parse import quote_plus, urlparse

import requests

from flask import Blueprint, request, url_for

from jobserv.jsend import ApiError, jsendify
from jobserv.models import Project, ProjectTrigger, TriggerTypes
from jobserv.settings import RUN_URL_FMT
from jobserv.trigger import trigger_build

blueprint = Blueprint("api_gitlab", __name__, url_prefix="/gitlab")


def _get_params(data):
    if data["object_kind"] == "note":
        mr = data["merge_request"]
        mr_url = data["object_attributes"]["url"]
    else:
        mr = data["object_attributes"]
        mr_url = mr["url"]

    status_url = mr["source"]["web_url"]
    user_repo = mr["source"]["path_with_namespace"]
    replace = "api/v4/projects/" + quote_plus(user_repo)
    status_url = status_url.replace(user_repo, replace)
    status_url += "/statuses/" + mr["last_commit"]["id"]

    target_repo = mr["target"]["path_with_namespace"]
    replace = "api/v4/projects/" + quote_plus(target_repo)
    api_url = mr["target"]["web_url"].replace(target_repo, replace)

    return {
        "GIT_SHA": mr["last_commit"]["id"],
        "GIT_URL": mr["source"]["git_http_url"],
        "GL_STATUS_URL": status_url,
        "GL_TARGET_REPO": mr["target"]["git_http_url"],
        "GL_MR": mr_url,
        "GL_MR_API": api_url + "/merge_requests/%s" % mr["iid"],
    }


def _set_base_sha(params, token):
    url = params["GL_MR_API"] + "/versions"
    headers = {
        "Content-Type": "application/json",
        "PRIVATE-TOKEN": token,
    }
    r = requests.get(url, headers=headers)
    if r.status_code == 200:
        params["GIT_SHA_BASE"] = r.json()[0]["base_commit_sha"]
    else:
        raise ApiError(
            r.status_code, "Unable to find base commit from %s:\n%s" % (url, r.text)
        )


def _get_proj_def(trigger, token, params):
    if trigger.definition_repo:
        # look up defintion out-of-tree
        name = trigger.definition_file
        if not name:
            name = trigger.project.name + ".yml"
        p = urlparse(trigger.definition_repo)
        proj_enc = quote_plus(p.path[1:].replace(".git", ""))
        url = (
            p.scheme
            + "://"
            + p.netloc
            + "/api/v4/projects/"
            + proj_enc
            + "/repository/files/"
            + quote_plus(name)
            + "/raw?ref=master"
        )
    else:
        # look up defintion in tree
        url = params["GIT_URL"]
        assert url[-4:] == ".git"
        p = urlparse(url)
        proj_enc = quote_plus(p.path[1:].replace(".git", ""))
        url = (
            p.scheme
            + "://"
            + p.netloc
            + "/api/v4/projects/"
            + proj_enc
            + "/repository/files/.jobserv.yml/raw?ref="
            + params["GIT_SHA"]
        )

    headers = {
        "PRIVATE-TOKEN": token,
    }
    resp = requests.get(url, headers=headers)
    if resp.status_code == 200:
        data = yaml.safe_load(resp.text)
        for trigger in data.get("triggers", []):
            if trigger["type"] == "gitlab_mr":
                return trigger["name"], data
        raise ValueError("No gitlab_mr trigger types defined in " + url)
    raise ValueError("Project definition does not exist: " + url)


def _fail_pr(params, token, failure_url):
    headers = {
        "Content-Type": "application/json",
        "PRIVATE-TOKEN": token,
    }
    data = {
        "context": "JobServ",
        "description": "unexpected failure",
        "state": "failure",
        "target_url": failure_url,
    }
    return requests.post(params["GL_STATUS_URL"], json=data, headers=headers)


def _update_pr(build, status_url, token):
    headers = {
        "Content-Type": "application/json",
        "PRIVATE-TOKEN": token,
    }

    for run in build.runs:
        if RUN_URL_FMT:
            url = RUN_URL_FMT.format(
                project=build.project.name, build=build.build_id, run=run.name
            )
        else:
            url = url_for(
                "api_run.run_get",
                proj=build.project.name,
                build_id=build.build_id,
                run=run.name,
                _external=True,
            )
        data = {
            "context": run.name,
            "description": "Build %d" % build.build_id,
            "target_url": url,
            "state": "pending",
        }
        r = requests.post(status_url, json=data, headers=headers)
        if r.status_code != 201:
            print("ERROR updating MR(%s): %d\n%s" % (status_url, r.status_code, r.text))


def _validate_payload(trigger):
    key = trigger.secret_data.get("webhook-key")
    if not key:
        raise ApiError(403, "Trigger has no webhook-key secret defined")

    if not hmac.compare_digest(key, request.headers["X-Gitlab-Token"]):
        raise ApiError(403, "Invalid X-Gitlab-Token")


def _find_trigger(proj):
    triggers = (
        ProjectTrigger.query.filter(ProjectTrigger.type == TriggerTypes.gitlab_mr.value)
        .join(Project)
        .filter(Project.name == proj)
    )
    last_exc = None
    for t in triggers:
        try:
            _validate_payload(t)
            return t
        except Exception as e:
            last_exc = e
    if last_exc is None:
        raise ApiError(404, "Trigger for project does not exist")
    raise last_exc


def _filter_events(event):
    events = ("Merge Request Hook", "Note Hook")
    if event not in events:
        raise ApiError(400, "Invalid action: " + event)


@blueprint.route("/<project:proj>/", methods=("POST",))
def on_webhook(proj):
    trigger = _find_trigger(proj)
    event = request.headers["X-Gitlab-Event"]
    _filter_events(event)

    data = request.get_json()
    mr_actions = ("open", "reopen", "update")
    if event == "Note Hook":
        if "ci-retest" not in data["object_attributes"]["note"]:
            return "Ingoring comment"
    elif data["object_attributes"]["action"] not in mr_actions:
        return "Ingoring Merge Request action"

    params = _get_params(data)

    reason = "GitLab MR: " + params["GL_MR"]
    secrets = trigger.secret_data
    if "gitlabtok" not in secrets or "gitlabuser" not in secrets:
        raise ApiError(400, 'Trigger secrets is missing "gitlabtok" or "gitlabuser"')
    token = secrets["gitlabtok"]

    try:
        _set_base_sha(params, token)
        trig, proj = _get_proj_def(trigger, token, params)
        b = trigger_build(
            trigger.project, reason, trig, params, secrets, proj, trigger.queue_priority
        )
        _update_pr(b, params["GL_STATUS_URL"], token)
        url = url_for(
            "api_build.build_get",
            proj=trigger.project.name,
            build_id=b.build_id,
            _external=True,
        )
        return jsendify({"url": url}, 201)
    except ApiError as e:
        url = e.resp.headers.get("Location")
        _fail_pr(params, token, url)
        raise
    except Exception:
        _fail_pr(params, token, None)
        tb = traceback.format_exc()
        return "FAILED:\n" + tb, 500
