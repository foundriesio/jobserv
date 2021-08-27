# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

from flask import Blueprint, url_for

from sqlalchemy import func

from jobserv.jsend import ApiError, jsendify
from jobserv.models import BuildStatus, Run, db

blueprint = Blueprint("api_health", __name__, url_prefix="/health")


@blueprint.errorhandler(ApiError)
def api_error(e):
    return e.resp


@blueprint.route("/runs/")
def run_health():
    health = {}
    # get an overall count for each run state
    vals = db.session.query(Run.status, func.count(Run.status)).group_by(Run.status)
    health["statuses"] = {BuildStatus(status).name: count for status, count in vals}

    # now give some details about what's queued and what's running
    health["RUNNING"] = {}
    health["QUEUED"] = []

    active = (
        BuildStatus.QUEUED,
        BuildStatus.RUNNING,
        BuildStatus.UPLOADING,
        BuildStatus.CANCELLING,
    )
    runs = Run.query.filter(Run.status.in_(active)).order_by(
        Run.queue_priority.asc(), Run.build_id.asc(), Run.id.asc()
    )
    for run in runs:
        url = url_for(
            "api_run.run_get",
            proj=run.build.project.name,
            build_id=run.build.build_id,
            run=run.name,
            _external=True,
        )
        item = {
            "project": run.build.project.name,
            "build": run.build.build_id,
            "run": run.name,
            "url": url,
            "created": run.build.status_events[0].time,
        }

        if run.status == BuildStatus.QUEUED:
            health["QUEUED"].append(item)
        else:
            worker = run.worker_name or "?"
            health["RUNNING"].setdefault(worker, []).append(item)
    return jsendify({"health": health})
