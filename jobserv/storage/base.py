# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

import contextlib
import datetime
import json
import os
import mimetypes

from cryptography.fernet import Fernet

from jobserv.log import log
from jobserv.settings import JOBS_DIR


class BaseStorage(object):
    blueprint = None

    def __init__(self):
        mimetypes.add_type("text/plain", ".log")

    def _create_from_string(self, storage_path, contents):
        raise NotImplementedError()

    def _create_from_file(self, storage_path, filename, mimetype):
        raise NotImplementedError()

    def _get_raw(self, storage_path):
        raise NotImplementedError()

    def _get_as_string(self, storage_path):
        raise NotImplementedError()

    def _generate_put_url(self, run, path, expiration, content_type):
        raise NotImplementedError()

    def list_artifacts(self, run):
        raise NotImplementedError()

    def delete_build(self, build):
        raise NotImplementedError()

    def get_download_response(self, request):
        raise NotImplementedError()

    def _get_run_path(self, run, path=None):
        name = "%s/%s/%s/" % (run.build.project.name, run.build.build_id, run.name)
        if path:
            if path[0] == "/":
                path = path[1:]
            return name + path
        return name

    def create_project_definition(self, build, projdef):
        name = "%s/%s/project.yml" % (build.project.name, build.build_id)
        self._create_from_string(name, projdef)

    def get_project_definition(self, build):
        name = "%s/%s/project.yml" % (build.project.name, build.build_id)
        return self._get_as_string(name)

    def create_build_params(self, build, params):
        name = "%s/%s/params.json" % (build.project.name, build.build_id)
        self._create_from_string(name, json.dumps(params))

    def get_build_params(self, build):
        name = "%s/%s/params.json" % (build.project.name, build.build_id)
        return json.loads(self._get_as_string(name))

    def get_artifact_content(self, run, path, decoded=True):
        if not decoded:
            return self._get_raw(self._get_run_path(run, path))
        return self._get_as_string(self._get_run_path(run, path))

    def set_run_definition(self, run, definition):
        path = self._get_run_path(run, ".rundef.json")
        secrets = definition.get("secrets")
        if secrets:
            f = Fernet(run.derive_fernet_token())
            secret_bytes = json.dumps(secrets).encode()
            encrypted_bytes = f.encrypt(secret_bytes)
            definition["secrets-encrypted"] = encrypted_bytes.decode()
            del definition["secrets"]
        self._create_from_string(path, json.dumps(definition))

    def get_run_definition(self, run):
        def_str = self._get_as_string(self._get_run_path(run, ".rundef.json"))
        rundef = json.loads(def_str)
        encrypted = rundef.get("secrets-encrypted")
        if encrypted:
            f = Fernet(run.derive_fernet_token())
            secret_bytes = f.decrypt(encrypted.encode())
            rundef["secrets"] = json.loads(secret_bytes.decode())
            del rundef["secrets-encrypted"]
        return rundef

    def console_logfd(self, run, mode="r"):
        path = os.path.join(JOBS_DIR, self._get_run_path(run, "console.log"))
        if mode[0] in ("a", "w") and not os.path.exists(path):
            try:
                os.makedirs(os.path.dirname(path))
            except FileExistsError:
                pass
        return open(path, mode)

    def copy_log(self, run):
        src = os.path.join(JOBS_DIR, self._get_run_path(run, "console.log"))

        if not os.path.exists(src):
            log.warn("Run had no console output")
            return

        self._create_from_file(
            self._get_run_path(run, "console.log"), src, "text/plain"
        )

        # try and clean up our runs on disk
        os.unlink(src)
        os.rmdir(os.path.dirname(src))
        try:
            os.rmdir(os.path.dirname(os.path.dirname(src)))
        except Exception:
            pass  # another run is still in progress

    def generate_signed(self, run, paths, expiration):
        urls = {}
        expiration = datetime.timedelta(seconds=expiration)
        for p in paths:
            ct = mimetypes.guess_type(p)[0]
            if not ct:
                ct = ""
            url = self._generate_put_url(run, p, expiration=expiration, content_type=ct)
            urls[p] = {
                "url": url,
                "content-type": ct,
            }
        return urls

    @contextlib.contextmanager
    def git_poller_cache(self):
        path = "git_poller_cache.json"
        data = {}
        try:
            data = json.loads(self._get_as_string(path))
        except FileNotFoundError:
            log.warn("Cache not found, assuming initial run with no data")

        try:
            yield data
        finally:
            self._create_from_string(path, json.dumps(data))
