# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

import hmac
import logging
import secrets
import time
import traceback
import yaml

from urllib.parse import urlparse

import requests

from flask import Blueprint, request, url_for

from jobserv.flask import permissions
from jobserv.jsend import ApiError, get_or_404, jsendify
from jobserv.models import Project, ProjectTrigger, TriggerTypes, db
from jobserv.settings import GITLAB_SERVERS, RUN_URL_FMT
from jobserv.trigger import trigger_build

blueprint = Blueprint("api_github", __name__, url_prefix="/github")


def _get_params(owner, repo, pr_num, token):
    headers = {
        "Content-Type": "application/json",
        "Authorization": "token " + token,
    }
    url = "https://api.github.com/repos/%s/%s/pulls/%d" % (owner, repo, pr_num)
    for x in range(5):
        r = requests.get(url, headers=headers)
        if r.status_code == 200:
            try:
                data = r.json()
                return {
                    "GH_PRNUM": int(pr_num),
                    "GH_OWNER": owner,
                    "GH_REPO": repo,
                    "GH_STATUS_URL": data["statuses_url"],
                    "GH_TARGET_REPO": data["base"]["repo"]["clone_url"],
                    "GIT_URL": data["head"]["repo"]["clone_url"],
                    "GIT_SHA_BASE": data["base"]["sha"],
                    "GIT_OLD_SHA": data["base"]["sha"],
                    "GIT_SHA": data["head"]["sha"],
                }
            except Exception:
                logging.error("Error finding SHA: %d - %s", r.status_code, r.text)
        time.sleep(0.2)
    raise ApiError(500, "Error finding SHA: %d - %s" % (r.status_code, r.text))


def _get_proj_def(trigger, owner, repo, sha, token):
    headers = {
        "Content-Type": "application/json",
        "Authorization": "token " + token,
    }

    if trigger.definition_repo:
        # look up defintion out-of-tree
        name = trigger.definition_file
        if not name:
            name = trigger.project.name + ".yml"

        p = urlparse(trigger.definition_repo)
        url = p.scheme + "://" + p.netloc
        if url == "https://github.com":
            # Get the github owner/proj by removing the leading / and ".git"
            # if its in the url.
            ghproj = p.path[1:].replace(".git", "")
            url = "https://raw.githubusercontent.com/%s/master/%s" % (ghproj, name)
        elif url in GITLAB_SERVERS:
            headers["PRIVATE-TOKEN"] = trigger.secret_data["gitlabtok"]
            url = trigger.definition_repo.replace(".git", "") + "/raw/master/" + name
        else:
            raise ValueError(
                "Unknown/unsupported definition repo: %s" % (trigger.definition_repo)
            )
    else:
        # look up defintion in tree
        url = "https://raw.githubusercontent.com/%s/%s/%s/%s" % (
            owner,
            repo,
            sha,
            ".jobserv.yml",
        )

    resp = requests.get(url, headers=headers)
    if resp.status_code == 200:
        data = yaml.safe_load(resp.text)
        for trigger in data.get("triggers", []):
            if trigger["type"] == "github_pr":
                return trigger["name"], data
        raise ValueError("No github_pr trigger types defined")
    raise ValueError("Project definition does not exist: " + url)


def _fail_pr(repo, pr_num, sha, failure_url, token):
    url = "https://api.github.com/repos/%s/statuses/%s" % (repo, sha)

    headers = {
        "Content-Type": "application/json",
        "Authorization": "token " + token,
    }
    data = {
        "context": "JobServ",
        "description": "unexpected failure",
        "state": "failure",
        "target_url": failure_url,
    }
    return requests.post(url, json=data, headers=headers)


def _update_pr(build, status_url, token):
    headers = {
        "Content-Type": "application/json",
        "Authorization": "token " + token,
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
        requests.post(status_url, json=data, headers=headers)


def _validate_payload(trigger):
    key = trigger.secret_data.get("webhook-key")
    if not key:
        raise ApiError(403, "Trigger has no webhook-key secret defined")

    computed = hmac.new(key.encode(), request.data, "sha1").hexdigest()
    delivered = request.headers.get("X_HUB_SIGNATURE")
    if not delivered or not delivered.startswith("sha1="):
        raise ApiError(404, "Missing or invalid X_HUB_SIGNATURE header")
    if not (hmac.compare_digest(computed, delivered[5:])):
        raise ApiError(403, "Invalid X_HUB_SIGNATURE")


def _find_trigger(proj):
    triggers = (
        ProjectTrigger.query.filter(ProjectTrigger.type == TriggerTypes.github_pr.value)
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
    ignores = (
        "fork",
        "ping",
        "push",
        "status",
        "pull_request_review",
        "pull_request_review_comment",
    )
    events = ignores + ("issue_comment", "pull_request")
    if event not in events:
        raise ApiError(400, "Invalid action: " + event)
    if event in ignores:
        raise ApiError(200, "OK, ignoring")


def _assert_ok_to_test(repo, labels):
    # see if project has an "ok-to-test" label
    url = f"https://api.github.com/repos/{repo}/labels/ok-to-test"
    r = requests.get(url)
    if r.status_code == 404:
        logging.info("ok-to-test not defined for %s - proceeding", repo)
        return
    if not r.ok:
        err = "HTTP_{r.status_code}: {r.text}"
        raise ApiError(500, "Unable to check for project labels: " + err)

    # ok-to-test is defined - look for the label
    for lbl in labels:
        if lbl["name"] == "ok-to-test":
            logging.info("ok-to-test defined for issue")
            return

    raise ApiError(200, "Ingoring event: ok-to-test label not set")


@blueprint.route("/<project:proj>/", methods=("POST",))
def on_webhook(proj):
    trigger = _find_trigger(proj)
    event = request.headers.get("X-Github-Event")
    _filter_events(event)

    data = request.get_json()
    repo = data["repository"]["full_name"]
    if event == "issue_comment":
        if "ci-retest" not in request.json["comment"]["body"]:
            return "Ingoring comment"
        _assert_ok_to_test(repo, (data["issue"].get("labels") or []))
        pr_num = data["issue"]["number"]
    elif event == "pull_request":
        if data["action"] not in ("opened", "synchronize", "labeled"):
            return "Ignoring action: " + request.json["action"]
        _assert_ok_to_test(repo, (data["pull_request"].get("labels") or []))
        pr_num = data["pull_request"]["number"]
        repo = data["pull_request"]["base"]["repo"]["full_name"]
    else:
        return f"Ignoring {event}"

    reason = "GitHub PR(%s): %s, https://github.com/%s/pull/%d" % (
        pr_num,
        event,
        repo,
        pr_num,
    )
    secrets = trigger.secret_data
    token = secrets["githubtok"]
    owner, repo = repo.split("/")
    params = _get_params(owner, repo, pr_num, token)
    try:
        trig, proj = _get_proj_def(trigger, owner, repo, params["GIT_SHA"], token)
        b = trigger_build(
            trigger.project, reason, trig, params, secrets, proj, trigger.queue_priority
        )
        _update_pr(b, params["GH_STATUS_URL"], token)
        url = url_for(
            "api_build.build_get",
            proj=trigger.project.name,
            build_id=b.build_id,
            _external=True,
        )
        return jsendify({"url": url}, 201)
    except ApiError as e:
        url = e.resp.headers.get("Location")
        _fail_pr(repo, pr_num, params["GIT_SHA"], url, token)
        raise
    except Exception:
        _fail_pr(repo, pr_num, params["GIT_SHA"], None, token)
        tb = traceback.format_exc()
        return "FAILED: %s: %s\n%s" % (repo, pr_num, tb), 500


def _register_github_hook(project, url, api_token, hook_token):
    data = {
        "name": "web",
        "active": True,
        "events": [
            "pull_request",
            "pull_request_review_comment",
            "issue_comment",
        ],
        "config": {
            "url": url_for("api_github.on_webhook", proj=project.name, _external=True),
            "content_type": "json",
            "secret": hook_token,
        },
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": "token " + api_token,
    }

    resp = requests.post(url, json=data, headers=headers)
    if resp.status_code != 201:
        raise ApiError(resp.status_code, resp.json())


@blueprint.route("/<project:proj>/webhook/", methods=("POST",))
def create_webhook(proj):
    u = permissions.assert_internal_user()
    p = get_or_404(Project.query.filter_by(name=proj))

    d = request.get_json() or {}
    required = ("githubtok", "owner", "project")
    missing = []
    for x in required:
        if x not in d:
            missing.append(x)
    if missing:
        raise ApiError(401, "Missing parameters: %s" % ",".join(missing))

    owner = d.pop("owner")
    hook_url = "https://api.github.com/repos/%s/%s/hooks"
    hook_url = hook_url % (owner, d.pop("project"))

    d["webhook-key"] = secrets.token_urlsafe()
    dr = df = None
    try:
        dr = d.pop("definition_repo")
        df = d.pop("definition_file")
    except KeyError:
        pass

    user = owner
    if u:
        user = str(u)

    db.session.add(ProjectTrigger(user, TriggerTypes.github_pr.value, p, dr, df, d))

    _register_github_hook(p, hook_url, d["githubtok"], d["webhook-key"])
    db.session.commit()
    return jsendify({}, 201)
