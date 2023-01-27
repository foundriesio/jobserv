# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

import hmac
import os
import mimetypes
import shutil

from flask import Blueprint, make_response, request, send_file, url_for

from jobserv.jsend import get_or_404
from jobserv.models import Build, Project, Run
from jobserv.settings import LOCAL_ARTIFACTS_DIR
from jobserv.storage.base import BaseStorage

SIGNING_KEY = os.environ.get("LOCAL_STORAGE_KEY", "").encode()


blueprint = Blueprint("local_storage", __name__, url_prefix="/local-storage")


class Storage(BaseStorage):
    blueprint = blueprint

    def __init__(self):
        super().__init__()
        self.artifacts = LOCAL_ARTIFACTS_DIR

    def _get_local(self, storage_path):
        assert storage_path[0] != "/"
        path = os.path.join(self.artifacts, storage_path)
        dirname = os.path.dirname(path)
        if not os.path.exists(dirname):
            os.makedirs(dirname)
        return path

    def _create_from_string(self, storage_path, contents):
        path = self._get_local(storage_path)
        with open(path, "w") as f:
            f.write(contents)

    def _create_from_file(self, storage_path, filename, content_type):
        path = self._get_local(storage_path)
        with open(filename, "rb") as fin, open(path, "wb") as fout:
            shutil.copyfileobj(fin, fout)

    def _get_raw(self, storage_path):
        assert storage_path[0] != "/"
        path = os.path.join(self.artifacts, storage_path)
        with open(path, "rb") as f:
            return f.read()

    def _get_as_string(self, storage_path):
        assert storage_path[0] != "/"
        path = os.path.join(self.artifacts, storage_path)
        with open(path, "r") as f:
            return f.read()

    def list_artifacts(self, run):
        path = "%s/%s/%s/" % (run.build.project.name, run.build.build_id, run.name)
        path = os.path.join(self.artifacts, path)
        for base, _, names in os.walk(path):
            for name in names:
                if name != ".rundef.json":
                    yield os.path.join(base, name)[len(path) :]

    def delete_build(self, build):
        path = os.path.join(self.artifacts, build.project.name, str(build.build_id))
        shutil.rmtree(path)

    def get_download_response(self, request, run, path):
        try:
            p = os.path.join(self.artifacts, self._get_run_path(run), path)
            mt = mimetypes.guess_type(p)[0]
            return send_file(open(p, "rb"), mimetype=mt)
        except FileNotFoundError:
            return make_response("File not found", 404)

    def _generate_put_url(self, run, path, expiration, content_type):
        if not SIGNING_KEY:
            raise RuntimeError("JobServ missing LOCAL_STORAGE_KEY")
        p = os.path.join(self.artifacts, self._get_run_path(run), path)
        msg = "%s,%s,%s" % ("PUT", p, content_type)
        sig = hmac.new(SIGNING_KEY, msg.encode(), "sha1").hexdigest()
        return url_for(
            "local_storage.run_upload_artifact",
            sig=sig,
            proj=run.build.project.name,
            build_id=run.build.build_id,
            run=run.name,
            path=path,
            _external=True,
        )


def _get_run(proj, build_id, run):
    p = get_or_404(Project.query.filter_by(name=proj))
    b = get_or_404(Build.query.filter_by(project=p, build_id=build_id))
    return (
        Run.query.filter_by(name=run)
        .filter(Run.build.has(Build.id == b.id))
        .first_or_404()
    )


@blueprint.route(
    "/<sig>/<project:proj>/builds/<int:build_id>/runs/<run>/<path:path>",
    methods=("PUT",),
)
def run_upload_artifact(sig, proj, build_id, run, path):
    if not SIGNING_KEY:
        raise RuntimeError("JobServ missing LOCAL_STORAGE_KEY")
    run = _get_run(proj, build_id, run)

    # validate the signature
    ls = Storage()
    p = os.path.join(ls.artifacts, ls._get_run_path(run), path)
    msg = "%s,%s,%s" % (request.method, p, request.headers.get("Content-Type"))
    computed = hmac.new(SIGNING_KEY, msg.encode(), "sha1").hexdigest()
    if not hmac.compare_digest(sig, computed):
        return "Invalid signature", 401

    dirname = os.path.dirname(p)
    try:
        # we could have 2 uploads trying this, so just do it this way to avoid
        # race conditions
        os.makedirs(dirname)
    except FileExistsError:
        pass

    # stream the contents to disk
    with open(p, "wb") as f:
        chunk_size = 4096
        while True:
            chunk = request.stream.read(chunk_size)
            if len(chunk) == 0:
                break
            f.write(chunk)
    return "ok"
