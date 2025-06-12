# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

from concurrent.futures import ProcessPoolExecutor
import os
import datetime
import logging
from time import sleep

from flask import redirect
from google.api_core.exceptions import ServiceUnavailable
from google.cloud import storage
from google.cloud.storage.blob import Blob
from google.cloud.exceptions import NotFound

from jobserv.settings import GCE_BUCKET
from jobserv.storage.base import BaseStorage

log = logging.getLogger("jobserv.flask")


def retry():
    def decorator(func):
        def newfn(*args, **kwargs):
            for i in (0.1, 0.5, 1, 0):
                try:
                    return func(*args, **kwargs)
                except ServiceUnavailable:
                    if i:
                        log.info("GCS unavailable, trying again in %ds", i)
                        sleep(i)
                    else:
                        raise

        return newfn

    return decorator


class Storage(BaseStorage):
    def __init__(self):
        super().__init__()
        creds_file = os.environ.get("GCE_CREDS")
        if creds_file:
            self.client = storage.Client.from_service_account_json(creds_file)
        else:
            self.client = storage.Client()
        self._bucket = None

    @property
    def bucket(self):
        if self._bucket:
            return self._bucket
        self._bucket = self.client.get_bucket(GCE_BUCKET)
        return self._bucket

    @retry()
    def _create_from_string(self, storage_path, contents):
        b = self.bucket.blob(storage_path)
        b.upload_from_string(contents)

    @retry()
    def _create_from_file(self, storage_path, filename, content_type):
        b = self.bucket.blob(storage_path)
        with open(filename, "rb") as f:
            b.upload_from_file(f, content_type=content_type)

    def _get_raw(self, storage_path):
        try:
            return self.bucket.blob(storage_path).download_as_string()
        except NotFound:
            raise FileNotFoundError(storage_path)

    def _get_as_string(self, storage_path):
        return self._get_raw(storage_path).decode()

    def list_artifacts(self, run):
        name = "%s/%s/%s/" % (run.build.project.name, run.build.build_id, run.name)
        return [
            {
                "name": x.name[len(name) :],
                "size_bytes": x.size,
            }
            for x in self.bucket.list_blobs(prefix=name)
            if not x.name.endswith(".rundef.json")
        ]

    def delete_build(self, build):
        name = "%s/%s/" % (build.project.name, build.build_id)
        blobs = self.bucket.list_blobs(prefix=name)
        with ProcessPoolExecutor(max_workers=20) as conn:
            conn.map(Blob.delete, blobs)

    def _generate_put_url(self, run, path, expiration, content_type):
        b = self.bucket.blob(self._get_run_path(run, path))
        return b.generate_signed_url(
            expiration=expiration, method="PUT", content_type=content_type
        )

    def get_download_response(self, request, run, path):
        expiration = int(request.headers.get("X-EXPIRATION", "90"))
        b = self.bucket.blob(self._get_run_path(run, path))
        expiration = datetime.timedelta(seconds=expiration)
        rd = "inline; filename=%s" % os.path.basename(path)
        resp = redirect(
            b.generate_signed_url(expiration=expiration, response_disposition=rd)
        )
        resp.headers["Cache-Control"] = "private"
        return resp
