# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

import datetime
import fnmatch
import json
import os
import random
import string
import subprocess
import sys

import click
import requests

from sqlalchemy.orm.exc import NoResultFound

from jobserv.flask import create_app
from jobserv.git_poller import run
from jobserv.models import (
    Build,
    BuildEvents,
    BuildStatus,
    Project,
    ProjectTrigger,
    Run,
    TriggerTypes,
    Worker,
    db,
)
from jobserv.notify import email_on_exception
from jobserv.storage import Storage
from jobserv.worker import run_monitor_workers

app = create_app()


@app.cli.command()
def run_git_poller():
    run()


@app.cli.command()
def monitor_workers():
    run_monitor_workers()


@app.cli.group()
def project():
    pass


@project.command("list")
@click.argument("pattern", required=False)
def project_list(pattern=None):
    for p in Project.query.all():
        if pattern and not fnmatch.fnmatch(p.name, pattern):
            continue
        click.echo("Project: " + p.name)
        triggers = ProjectTrigger.query.filter(ProjectTrigger.project == p)
        if triggers.count():
            click.echo(" Triggers:")
            for t in triggers:
                t = json.dumps(t.as_json(), indent=2)
                click.echo("  " + "\n  ".join(t.split("\n")))


@project.command("create")
@click.argument("name")
def project_create(name):
    db.session.add(Project(name))
    db.session.commit()


@project.command("delete")
@click.argument("name")
def project_delete(name):
    val = input("Are you sure you want to delete: %s? Y/N " % name)
    if val != "Y":
        click.echo("exiting")
        return
    p = Project.query.filter(Project.name == name).one()
    for t in p.triggers:
        db.session.delete(t)
    db.session.commit()

    for b in p.builds:
        db.session.delete(b)
    db.session.commit()

    db.session.delete(p)
    db.session.commit()

    click.echo("The project has been deleted from the database.")
    click.echo(
        "Build artifacts still remain in the datastore and must be removed by hand"
    )  # NOQA


def _register_gitlab_hook(project, url, api_token, hook_token, server_name):
    data = {
        "url": "https://%s/gitlab/%s/" % (server_name, project),
        "merge_requests_events": True,
        "note_events": True,
        "enable_ssl_verification": True,
        "token": hook_token,
    }
    headers = {"PRIVATE-TOKEN": api_token}

    resp = requests.post(url, json=data, headers=headers)
    if resp.status_code != 201:
        sys.exit("ERROR adding webhook: %d\n%s" % (resp.status_code, resp.text))


def _register_github_hook(project, url, api_token, hook_token, server_name):
    data = {
        "name": "web",
        "active": True,
        "events": [
            "pull_request",
            "pull_request_review_comment",
            "issue_comment",
        ],
        "config": {
            "url": "https://%s/github/%s/" % (server_name, project),
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
        sys.exit("ERROR adding webhook: %d\n%s" % (resp.status_code, resp.text))


def add_trigger(
    project,
    user,
    type,
    secrets,
    definition_repo,
    definition_file,
    hook_url,
    server_name,
):
    key = "".join(
        random.SystemRandom().choice(
            string.ascii_lowercase + string.ascii_uppercase + string.digits
        )
        for _ in range(32)
    )
    secret_map = {}
    for secret in secrets or []:
        k, v = secret.split("=", 1)
        secret_map[k.strip()] = v.strip()

    type = TriggerTypes[type].value
    p = Project.query.filter(Project.name == project).first()
    if not p:
        click.echo("No such project: %s" % project)
        return
    db.session.add(
        ProjectTrigger(user, type, p, definition_repo, definition_file, secret_map)
    )

    if type == TriggerTypes.gitlab_mr.value:
        secret_map["webhook-key"] = key
        if "gitlabtok" not in secret_map or "gitlabuser" not in secret_map:
            raise ValueError('"gitlabtok" and "gitlabuser" are required secrets')
        _register_gitlab_hook(
            project, hook_url, secret_map["gitlabtok"], key, server_name
        )
    elif type == TriggerTypes.github_pr.value:
        secret_map["webhook-key"] = key
        if "githubtok" not in secret_map:
            raise ValueError('"githubtok" is required in secrets')
        _register_github_hook(
            project, hook_url, secret_map["githubtok"], key, server_name
        )

    db.session.commit()


@project.command("add-trigger")
@click.argument("project")
@click.option("--user", "-u", required=True)
@click.option(
    "--type", "-t", required=True, type=click.Choice([x.name for x in TriggerTypes])
)
@click.option("--secret", "-s", "secrets", multiple=True)
@click.option("--definition_repo", "-r", default=None)
@click.option("--definition_file", "-f", default=None)
@click.option("--hook_url", default=None)
@click.option("--server_name", default=None)
def project_add_trigger(
    project,
    user,
    type,
    secrets=None,
    definition_repo=None,
    definition_file=None,
    hook_url=None,
    server_name=None,
):
    add_trigger(
        project,
        user,
        type,
        secrets,
        definition_repo,
        definition_file,
        hook_url,
        server_name,
    )


@project.command("update-trigger")
@click.argument("project")
@click.option("--user", "-u", required=True)
@click.option(
    "--type", "-t", required=True, type=click.Choice([x.name for x in TriggerTypes])
)
@click.option("--secret", "-s", "secrets", multiple=True)
@click.option("--definition_repo", "-r", default=None)
@click.option("--definition_file", "-f", default=None)
def project_update_trigger(
    project, user, type, secrets=None, definition_repo=None, definition_file=None
):
    t = (
        ProjectTrigger.query.join(Project)
        .filter(
            Project.name == project,
            ProjectTrigger.user == user,
            ProjectTrigger.type == TriggerTypes[type].value,
        )
        .one()
    )
    if definition_repo and t.definition_repo != definition_repo:
        click.echo("Updating defintion_repo")
        t.definition_repo = definition_repo
    if definition_file and t.definition_file != definition_file:
        click.echo("Updating defintion_file")
        t.definition_file = definition_file

    for secret in secrets or []:
        k, v = secret.split("=", 1)
        if k in t.secret_data:
            if not v:
                click.echo("Removing secret: %s" % k)
                del t.secret_data[k]
            elif v != t.secret_data[k]:
                click.echo("Updating secret: %s" % k)
                t.secret_data[k] = v
        else:
            click.echo("Adding new secret: %s" % k)
            t.secret_data[k] = v
    t.update_secrets()
    db.session.commit()


@app.cli.group()
def worker():
    pass


@worker.command("list")
def worker_list():
    print("Worker\tEnlisted\tOnline")
    for w in Worker.query.all():
        print("%s\t%s\t%s" % (w.name, w.enlisted, w.online))


@worker.command("delete")
@click.argument("name")
def worker_delete(name):
    w = Worker.query.filter(Worker.name == name).one()
    w.delete()


@worker.command("enlist")
@click.argument("name")
def worker_enlist(name):
    w = Worker.query.filter(Worker.name == name).one()
    w.enlisted = True
    db.session.commit()


@app.cli.command("backup")
@click.option("--keep-local", is_flag=True)
@email_on_exception("jobserv: DB Backup Failed")
def backup(keep_local=False):
    command = (
        "mysqldump",
        "--user=" + db.engine.url.username,
        "--password=" + db.engine.url.password,
        "--host=" + db.engine.url.host,
        "--single-transaction",
        "--no-tablespaces",
        db.engine.url.database,
    )
    backup = "/data/jobserv-db.sql-%s" % datetime.datetime.now()
    with open(backup, "w") as f:
        subprocess.check_call(command, stdout=f)

    Storage()._create_from_file(
        "backups/" + os.path.basename(backup), backup, "application/x-sql"
    )
    if not keep_local:
        os.unlink(backup)


@app.cli.command("run-status")
@click.argument("project", required=True)
@click.argument("build", type=int, required=True)
@click.argument("run", required=True)
@click.argument(
    "status",
    required=False,
    type=click.Choice(["QUEUED", "PASSED", "FAILED", "CANCELLING"]),
)
def run_status(project, build, run, status=None):
    """Get the status of a Run and optionally set it."""
    try:
        run = (
            Run.query.join(Build)
            .join(Project)
            .filter(Project.name == project, Build.build_id == build, Run.name == run)
            .one()
        )
    except NoResultFound:
        click.echo("Could not find this project/build/run")
        return

    click.echo(run)
    if status:
        status = BuildStatus[status]
        if status == BuildStatus.QUEUED:
            click.echo("Removing test results since run is getting re-queued")
            for t in run.tests:
                db.session.delete(t)
        run.set_status(status)
        db.session.commit()
        if run.status in (BuildStatus.FAILED, BuildStatus.PASSED):
            # The run is finished, move logs from disk to GCS
            Storage().copy_log(run)
        click.echo("Run is now: %r" % run)


@app.cli.command("create-worker-jwt")
@click.argument("ou", nargs=-1)
def create_jwt(ou):
    """Generate a worker JWT keypair"""
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )
    from jobserv.worker_jwt import create_keypair, _keyid

    priv, cert = create_keypair(ou)

    pkey = priv.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=NoEncryption(),
    )

    kid = _keyid(cert)
    click.echo(f"# Key Id: {kid}")
    click.echo(pkey)
    click.echo(cert.public_bytes(Encoding.PEM))


@app.cli.command("prune-builds")
@click.argument("project", required=True)
@click.argument("cutoff_days", required=True, type=int)
@click.option("--dryrun", default=False, type=bool)
def prune_builds(project, cutoff_days, dryrun):
    """Delete non-promoted builds older than the given cutoff"""
    p = Project.query.filter(Project.name == project).one().id
    cutoff_date = datetime.datetime.utcnow() - datetime.timedelta(days=cutoff_days)
    click.echo(f"Looking for builds in {project} before {cutoff_date}...")
    builds = (
        Build.query.join(BuildEvents)
        .filter(
            Build.proj_id == p,
            Build.status != BuildStatus.PROMOTED,
            BuildEvents.time <= cutoff_date,
        )
        .order_by(-Build.build_id)
    )
    storage = Storage()
    for b in builds:
        if dryrun:
            click.echo(f"DRYRUN would delete: {b.build_id}")
        else:
            click.echo(f"Deleting {b.build_id}")
            try:
                storage.delete_build(b)
            except FileNotFoundError:
                # Maybe we tried to prune this before and hit an error
                pass
            db.session.delete(b)
            db.session.commit()
