# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

import json
import logging
import traceback

import yaml

from flask import url_for

from jobserv.jsend import ApiError
from jobserv.models import Build, BuildStatus, Run, db
from jobserv.project import ProjectDefinition
from jobserv.settings import BUILD_URL_FMT
from jobserv.storage import Storage


def _check_for_trigger_upgrade(rundef, trigger_type, parent_trigger_type):
    """We could have a build that's triggered by either a github_pr or a
    gitlab_mr. They might have runs that trigger something of type
    "simple". This could be the case where a git_poller and github_mr both
    trigger a similar set of tests *after* a build. In the case of the
    github_pr, we should "upgrade" the type of each run from simple to
    github_pr so that it can update the status of the PR.
    """
    if parent_trigger_type == "github_pr":
        if trigger_type == "simple":
            rundef = json.loads(rundef)
            rundef["trigger_type"] = "github_pr"
            logging.info("Updating the rundef from simple->github_pr")
            rundef = json.dumps(rundef, indent=2)
    elif parent_trigger_type == "git_poller":
        if trigger_type == "simple":
            rundef = json.loads(rundef)
            rundef["trigger_type"] = "git_poller"
            logging.info("Updating the rundef from simple->gith_poller")
            rundef = json.dumps(rundef, indent=2)
    return rundef


def trigger_runs(
    storage, projdef, build, trigger, params, secrets, parent_type, queue_priority=0
):
    name_fmt = trigger.get("run-names")
    added = []
    try:
        for run in trigger["runs"]:
            name = run["name"]
            if name_fmt:
                name = name_fmt.format(name=name)
            if name in [x.name for x in build.runs]:
                # NOTE: We can't really let the DB throw an IntegrityError,
                # because this is called from the build.locked context and
                # and a caller would need to call db.session.rollback which
                # would cause them to lose the lock.
                raise ValueError('A run named "%s" already exists' % name)
            r = Run(build, name, trigger["name"], queue_priority)
            db.session.add(r)
            db.session.flush()
            added.append(r)
            rundef = projdef.get_run_definition(r, run, trigger, params, secrets)
            rundef = _check_for_trigger_upgrade(rundef, trigger["type"], parent_type)
            storage.set_run_definition(r, rundef)
    except ApiError:
        logging.exception("ApiError while triggering runs for: %r", trigger)
        raise
    except ValueError:
        raise
    except Exception as e:
        logging.exception("Unexpected error creating runs for: %r", trigger)
        build.status = BuildStatus.FAILED
        for r in added:
            r.status = BuildStatus.FAILED
        db.session.commit()
        raise ApiError(500, str(e) + "\n" + traceback.format_exc())


def _fail_unexpected(build, exception):
    r = Run(build, "build-failure")
    db.session.add(r)
    r.set_status(BuildStatus.FAILED)
    db.session.commit()
    storage = Storage()
    with storage.console_logfd(r, "a") as f:
        f.write("Unexpected error prevented build from running:\n")
        f.write(str(exception))
    storage.copy_log(r)

    if BUILD_URL_FMT:
        url = BUILD_URL_FMT.format(project=build.project.name, build=build.build_id)
    else:
        url = url_for(
            "api_run.run_get_artifact",
            proj=build.project.name,
            build_id=build.build_id,
            run=r.name,
            path="console.log",
        )

    exception = ApiError(500, str(exception))
    exception.resp.headers.extend({"Location": url})
    return exception


def trigger_build(
    project, reason, trigger_name, params, secrets, proj_def, queue_priority=0
):
    proj_def = ProjectDefinition.validate_data(proj_def)
    b = Build.create(project)
    try:
        if reason:
            b.reason = reason
        if trigger_name:
            b.trigger_name = trigger_name
        storage = Storage()
        storage.create_project_definition(
            b, yaml.dump(proj_def._data, default_flow_style=False)
        )
        trigger = proj_def.get_trigger(trigger_name)
        if not trigger:
            raise KeyError(
                "Project(%s) does not have a trigger: %s" % (project, trigger_name)
            )
        if trigger.get("triggers"):
            # there's a trigger to run after all the runs for this trigger
            # completed. it will need to know the parameters for this job
            storage.create_build_params(b, params)
    except Exception as e:
        raise _fail_unexpected(b, e)

    trigger_runs(storage, proj_def, b, trigger, params, secrets, None, queue_priority)
    db.session.commit()
    return b
