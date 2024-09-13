# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

import fnmatch
import json
import logging
import os
import time
import xml.etree.ElementTree as ET

import requests
import yaml

from urllib.parse import quote_plus, urlparse

from dataclasses import dataclass, field
from requests.auth import HTTPBasicAuth
from typing import Dict, Iterator, List, Optional, Tuple

from jobserv.flask import permissions
from jobserv.project import ProjectDefinition
from jobserv.settings import GIT_POLLER_INTERVAL, GITLAB_SERVERS, JOBSERV_URL
from jobserv.storage import Storage

logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger()
logging.getLogger("pykwalify.core").setLevel(logging.WARNING)
logging.getLogger("pykwalify.rule").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)

_cgit_repos: Dict[str, bool] = {}


@dataclass
class ProjectTrigger:
    id: int
    type: str
    project: str
    user: str
    queue_priority: int
    definition_repo: Optional[str] = None
    definition_file: Optional[str] = None
    secrets: Dict[str, str] = field(default_factory=dict)


@dataclass
class PollerEntry:
    trigger: ProjectTrigger
    definition: Optional[ProjectDefinition] = None
    projdef_headers: Dict[str, str] = field(default_factory=dict)


def _get_project_triggers() -> Optional[Dict[int, ProjectTrigger]]:
    resp = permissions.internal_get(
        JOBSERV_URL + "/project-triggers/", params={"type": "git_poller"}
    )
    if resp.status_code != 200:
        log.error(
            "Unable to get projects from front-end: %d %s", resp.status_code, resp.text
        )
        return None
    return {x["id"]: ProjectTrigger(**x) for x in resp.json()["data"]}


def _get_projdef(entry: PollerEntry) -> Optional[ProjectDefinition]:
    repo = entry.trigger.definition_repo or ""
    defile = entry.trigger.definition_file
    if not defile:
        defile = entry.trigger.project + ".yml"
    gitlab = entry.trigger.secrets.get("gitlabtok")
    gheader = entry.trigger.secrets.get("git.http.extraheader")

    headers = entry.projdef_headers
    token = entry.trigger.secrets.get("githubtok")

    if gitlab:
        headers["PRIVATE-TOKEN"] = gitlab
        p = urlparse(repo)
        proj_enc = quote_plus(p.path[1:].replace(".git", ""))
        url = (
            p.scheme
            + "://"
            + p.netloc
            + "/api/v4/projects/"
            + proj_enc
            + "/repository/files/"
            + quote_plus(defile)
            + "/raw?ref=master"
        )
    elif "github" in repo:
        if token:
            headers["Authorization"] = "token " + token
        url = repo.replace("github.com", "raw.githubusercontent.com")
        if url[-1] != "/":
            url += "/"
        url += "master/" + defile
    else:
        url = repo
        if not url.endswith(".git"):
            url += ".git"
        url = repo + "/plain/" + defile
        log.info("Assuming CGit style URL to file: %s", url)

    r = requests.get(url, headers=headers)
    if r.status_code == 401 and gheader:
        log.info("Authorization required using git header in secrets")
        key, val = gheader.split(":", 1)
        headers[key.strip()] = val.strip()
        r = requests.get(url, headers=headers)

    if r.status_code == 200:
        try:
            log.info("New version of project definition found for %s", url)
            data = yaml.safe_load(r.text)
            ProjectDefinition.validate_data(data)
            entry.definition = ProjectDefinition(data)
            # allows us to cache the resp
            headers["If-None-Match"] = r.headers["ETAG"]
        except Exception:
            log.exception("Validation failed for %s ...skipping", url)
            return None
    elif r.status_code == 304:
        # it hasn't changed
        log.debug("Cache hit for %s", url)
    else:
        log.error("Unable to read definition(%s): %d: %s", url, r.status_code, r.text)
        return None
    return entry.definition


def _get_refs(repo_url: str, trigger: ProjectTrigger) -> Iterator[Tuple[str, str]]:
    secrets = trigger.secrets
    auth = None
    ghtok = secrets.get("githubtok")
    if ghtok:
        auth = HTTPBasicAuth(trigger.user, ghtok)

    gltok = secrets.get("gitlabtok")
    git_header = secrets.get("git.http.extraheader")

    if not repo_url.endswith(".git"):
        # access these URL on github requires .git and it seems to be needed
        # for things like cgit and gitweb as well
        repo_url += ".git"
    if repo_url[-1] != "/":
        repo_url += "/"
    repo_url += "info/refs?service=git-upload-pack"
    resp = requests.get(repo_url, auth=auth)
    if gltok and resp.status_code == 401:
        # this might be a gitlab repo, try with the token
        # we have to try unauthenticated first, because it could be a non-git
        # repo, that needs gitlab credentials for the script-repo stuff
        log.debug("Trying repo(%s) with gitlab credentials", repo_url)
        user = secrets.get("gitlabuser")
        repo_url = repo_url.replace("://", "://%s:%s@" % (user, gltok))
        resp = requests.get(repo_url)
        # TODO flag this as a gitlab repo and then add in logic like
        # _github_log below
    elif git_header and resp.status_code == 401:
        key, val = git_header.split(":", 1)
        headers = {
            key.strip(): val.strip(),
            "User-Agent": "git",
        }
        resp = requests.get(repo_url, headers=headers)

    if resp.status_code != 200:
        log.error(
            "Unable to check %s for changes: %d %s",
            repo_url,
            resp.status_code,
            resp.reason,
        )
    else:
        for line in resp.text.splitlines()[2:]:
            if line == "0000":
                break
            line = line[4:]  # strip off git protocol stuff
            log.debug("Looking at ref: %s", line)
            sha, ref = line.split(" ", 1)
            yield sha, ref


def _get_repo_changes(
    refs_cache, url: str, refs: List[str], trigger: ProjectTrigger
) -> Iterator[dict]:
    log.info("Looking for changes to: %s", url)
    cur_refs = refs_cache.setdefault(url, {})
    for sha, ref in _get_refs(url, trigger):
        for pattern in refs:
            if fnmatch.fnmatch(ref, pattern):
                cur = cur_refs.get(ref)
                if cur != sha:
                    cur_refs[ref] = sha
                    if cur is None:
                        log.info("First run detected for %s - %s", url, ref)
                    else:
                        log.info("%s %s change %s->%s", url, ref, cur, sha)
                        yield {
                            "GIT_REF": ref,
                            "GIT_URL": url,
                            "GIT_OLD_SHA": cur,
                            "GIT_SHA": sha,
                        }


def _is_skipped(*messages: str) -> bool:
    """
    determines whether commit qualifies as skipped commit
    :param messages: commit description or commit title
    :return: if skip flags found in either description or title
    """
    msg = "".join(messages)
    flags = ("[skip ci]", "[ci skip]")
    return any(flag in msg for flag in flags)


def _github_log(
    trigger: ProjectTrigger, change_params: Dict[str, str]
) -> Tuple[str, bool]:
    skip = False
    base = change_params["GIT_OLD_SHA"]
    head = change_params["GIT_SHA"]
    url = (
        change_params["GIT_URL"]
        .replace(".git", "")
        .replace("github.com", "api.github.com/repos")
        + "/commits?sha="
        + head
    )

    auth = None
    ghtok = trigger.secrets.get("githubtok")
    if ghtok:
        auth = HTTPBasicAuth(trigger.user, ghtok)

    gitlog = ""
    try:
        r = requests.get(url, auth=auth)
    except Exception as e:
        log.exception("Unable to get %s", url)
        return "Unable to get %s\n%s" % (url, str(e)), skip
    if r.status_code == 200:
        for commit in r.json():
            sha = commit["sha"]
            if sha == base:
                break
            msg = commit["commit"]["message"]
            if sha == head:
                skip = _is_skipped(msg)
            gitlog += "%s %s\n" % (sha[:7], msg.splitlines()[0])
    else:
        gitlog += "Unable to get github log(%s): %d %s" % (url, r.status_code, r.text)
    return gitlog, skip


def _gitlab_log(
    trigger: ProjectTrigger, change_params: Dict[str, str]
) -> Tuple[str, bool]:
    skip = False
    base = change_params["GIT_OLD_SHA"]
    head = change_params["GIT_SHA"]
    p = urlparse(change_params["GIT_URL"])
    proj_enc = quote_plus(p.path[1:].replace(".git", ""))

    url = (
        p.scheme
        + "://"
        + p.netloc
        + "/api/v4/projects/"
        + proj_enc
        + "/repository/commits"
    )
    headers = None
    tok = trigger.secrets.get("gitlabtok")
    if tok:
        headers = {"PRIVATE-TOKEN": tok}
    try:
        r = requests.get(url, headers=headers, params={"ref_name": head})
    except Exception as e:
        log.exception("Unable to get %s", url)
        return "Unable to get %s\n%s" % (url, str(e)), skip

    gitlog = ""
    if r.status_code == 200:
        for commit in r.json():
            sha = commit["id"]
            if sha == base:
                break
            title = commit["title"]
            gitlog += "%s %s\n" % (commit["short_id"], title)
            if sha == head:
                msg = commit["message"]
                skip = _is_skipped(msg, title)
    else:
        gitlog += "Unable to get gitlab log(%s): %d %s" % (url, r.status_code, r.text)
    return gitlog, skip


def _cgit_log(
    trigger: ProjectTrigger, change_params: Dict[str, str]
) -> Tuple[str, bool]:
    skip = False
    gheader = trigger.secrets.get("git.http.extraheader")
    base = change_params["GIT_OLD_SHA"]
    head = change_params["GIT_SHA"]
    url = change_params["GIT_URL"]
    if url[-4:] != ".git":
        url += ".git"
    url += "/atom"

    try:
        params = {"h": head}
        r = requests.get(url, params=params)
        if r.status_code == 401 and gheader:
            log.info("Authorization required using git header in secrets")
            key, val = gheader.split(":", 1)
            headers = {key.strip(): val.strip()}
            r = requests.get(url, headers=headers, params=params)
    except Exception as e:
        log.exception("Unable to get %s", url)
        return "Unable to get %s\n%s" % (url, str(e)), skip

    if r.status_code == 404:
        return "", skip

    gitlog = ""
    if r.status_code == 200:
        root = ET.fromstring(r.text)
        for entry in root.findall("{http://www.w3.org/2005/Atom}entry"):
            item = entry.find("{http://www.w3.org/2005/Atom}id")
            sha = item.text if item is not None else ""
            if sha == base:
                break
            item = entry.find("{http://www.w3.org/2005/Atom}title")
            title = str(item.text) if item is not None else ""
            if sha and title:
                gitlog += "%s %s\n" % (sha[:7], title)
            if sha == head:
                item = entry.find("{http://www.w3.org/2005/Atom}content")
                msg = str(item.text) if item is not None else ""
                skip = _is_skipped(msg, title)
    else:
        gitlog += "Unable to get cgit atom feed for(%s): %d %s" % (
            url,
            r.status_code,
            r.text,
        )
    return gitlog, skip


def _trigger(entry: PollerEntry, trigger_name: str, change_params: Dict[str, str]):
    log.info(
        "Trigger build for %s with params: %r", entry.trigger.project, change_params
    )
    assert entry.definition
    data = {
        "trigger-name": trigger_name,
        "params": change_params,
        "secrets": entry.trigger.secrets,
        "project-definition": entry.definition._data,
        "reason": json.dumps(change_params, indent=2),
        "queue-priority": entry.trigger.queue_priority,
    }
    p = urlparse(change_params["GIT_URL"])
    url = p.scheme + "://" + p.netloc
    skipped = False
    if url == "https://github.com":
        summary, skipped = _github_log(entry.trigger, change_params)
        data["reason"] += "\n" + summary
    elif url in GITLAB_SERVERS:
        summary, skipped = _gitlab_log(entry.trigger, change_params)
        data["reason"] += "\n" + summary
    else:
        capable = _cgit_repos.get(url, True)
        if capable:
            summary, skipped = _cgit_log(entry.trigger, change_params)
            if summary:
                data["reason"] += "\n" + summary
            else:
                _cgit_repos[url] = False
    if skipped:
        log.info(
            "Skipping build for %s because of skip-ci message", entry.trigger.project
        )
        return

    log.debug("Data for build is: %r", data)
    url = "%s/projects/%s/builds/" % (JOBSERV_URL, entry.trigger.project)
    resp = permissions.internal_post(url, json=data)
    if resp.status_code != 201:
        log.error("Error creating build(%s): %d - %s", url, resp.status_code, resp.text)
    else:
        log.info("Build created: %s", resp.text)


def _poll_project(refs_cache, entry: PollerEntry):
    triggers: List[Dict] = []
    if entry.definition:
        triggers = entry.definition.triggers
    for trigger in triggers:
        if trigger["type"] == "git_poller":
            params = trigger.get("params", {})
            urls = params.get("GIT_URL", "").split()
            refs = params.get("GIT_POLL_REFS", "").split()
            if not urls or not refs:
                log.error(
                    "Project(%s) missing GIT_URL or GIT_POLL_REFS",
                    entry.trigger.project,
                )
                continue
            for url in urls:
                for changes in _get_repo_changes(
                    refs_cache, url, refs, entry.trigger
                ):  # NOQA
                    _trigger(entry, trigger["name"], changes)


def _poll(entries: Dict[int, PollerEntry]):
    try:
        triggers = _get_project_triggers()
        if triggers is None:
            return
    except Exception:
        logging.exception("Unable to get project list from JobServ")
        return

    names = set(triggers.keys())
    cur_names = set(entries.keys())

    for n in cur_names - names:
        log.info("Removing %s from poller list", n)
        del entries[n]

    for n in names - cur_names:
        log.info("Adding %s to poller list", n)
        entries[n] = PollerEntry(trigger=triggers[n])

    for n in names & cur_names:
        if entries[n].trigger != triggers[n]:
            log.info("Updating %s", n)
            entries[n].trigger = triggers[n]

    with Storage().git_poller_cache() as refs_cache:
        for entry in entries.values():
            log.debug(
                "Checking project: %s %d", entry.trigger.project, entry.trigger.id
            )
            projdef = _get_projdef(entry)
            proj_refs = refs_cache.setdefault(str(entry.trigger.id), {})
            if projdef:
                _poll_project(proj_refs, entry)


def run():
    last_run = time.time() - 15  # Wait a few seconds before polling jobserv
    entries = {}
    while True:
        sleep = GIT_POLLER_INTERVAL - (time.time() - last_run)
        if sleep > 0:
            log.debug("Waiting %d before running again", sleep)
            time.sleep(sleep)
        last_run = time.time()
        try:
            _poll(entries)
            with open("/tmp/git-poller.timestamp", "w") as f:
                f.write("%d" % time.time())
        except Exception:
            log.exception("Error getting cache, retrying in a bit")
