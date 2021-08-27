# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

import datetime
import json
import logging
import mimetypes
import os
import time
import urllib.error
import urllib.request
import urllib.parse

from http.client import HTTPException

from multiprocessing.pool import ThreadPool

import requests


def split(items, group_size):
    return [items[i : i + group_size] for i in range(0, len(items), group_size)]


class PostError(Exception):
    pass


class RunCancelledError(Exception):
    pass


def urllib_error_str(e):
    if hasattr(e, "code"):
        error = "HTTP_%d" % e.code
    else:
        error = "HTTP Error"
    if hasattr(e, "reason"):
        error += ": %s" % e.reason
    if hasattr(e, "read"):
        error += "\n" + e.read().decode()
    return error


def _post(url, data, headers, raise_error=False):
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        resp = urllib.request.urlopen(req)
        if resp.headers.get("X-JOBSERV-CANCEL"):
            raise RunCancelledError()
        return resp
    except urllib.error.URLError as e:
        error = urllib_error_str(e)
        logging.error("%s: %s", url, error)
        if raise_error:
            raise PostError(error)
    except HTTPException as e:
        logging.exception("Unable to post to: " + url)
        if raise_error:
            raise PostError(str(e))


class JobServApi(object):
    SIMULATED = False

    def __init__(self, run_url, api_key):
        mimetypes.add_type("text/plain", ".log")
        self._run_url = run_url
        self._api_key = api_key

    def _post(self, data, headers, retry):
        if self.SIMULATED:
            if data:
                return os.write(1, data)
            return True
        for x in range(retry):
            if _post(self._run_url, data, headers):
                return True
            time.sleep(2 * x + 1)  # try and give the server a moment
        return False

    def update_run(self, msg, status=None, retry=2, metadata=None):
        headers = {
            "content-type": "text/plain",
            "Authorization": "Token " + self._api_key,
        }
        if status:
            headers["X-RUN-STATUS"] = status
        if metadata:
            headers["X-RUN-METADATA"] = metadata
        return self._post(msg, headers, retry=retry)

    def update_status(self, status, msg, metadata=None):
        msg = "== %s: %s\n" % (datetime.datetime.utcnow(), msg)
        if self.SIMULATED:
            print(msg.replace("==", "== " + status))
            return
        if not self.update_run(msg.encode(), status, 8, metadata):
            logging.error("TODO HOW TO HANDLE?")

    def add_test(self, test_name, context, status, results=[]):
        headers = {"Authorization": "Token " + self._api_key}
        test = {
            "context": context,
            "status": status,
            "results": results,
        }
        if self.SIMULATED:
            print(
                "== %s: Test(%s)=%r\n" % (datetime.datetime.utcnow(), test_name, test)
            )
            return
        url = self._run_url + "tests/%s/" % test_name
        for x in range(3):
            r = requests.post(url, json=test, headers=headers)
            if r.status_code == 200:
                return
            time.sleep(2 * x + 1)  # try and give the server a moment
        return r

    def _get_urls(self, uploads):
        headers = {
            "content-type": "application/json",
            "Authorization": "Token " + self._api_key,
        }
        url = self._run_url
        if url[-1] != "/":
            url += "/"
        url += "create_signed"

        urls = [x["file"] for x in uploads]
        data = json.dumps(urls).encode()
        for i in range(1, 5):
            try:
                resp = _post(url, data, headers)
                return json.loads(resp.read().decode())["data"]["urls"]
            except Exception:
                if i == 4:
                    raise
                logging.exception("Unable to get urls, sleeping and retrying")
                time.sleep(2 * i)

    def _upload_item(self, artifacts_dir, artifact, urldata):
        # http://stackoverflow.com/questions/2502596/
        with open(os.path.join(artifacts_dir, artifact), "rb") as f:
            try:
                headers = {"Content-Type": urldata["content-type"]}
                r = requests.put(urldata["url"], data=f, headers=headers)
                if r.status_code not in (200, 201):
                    return "Unable to upload %s: HTTP_%d\n%s" % (
                        artifact,
                        r.status_code,
                        r.text,
                    )
            except Exception as e:
                return "Unexpected error for %s: %s" % (artifact, str(e))

    def upload(self, artifacts_dir, uploads):
        def _upload_cb(data):
            e = None
            for i in range(1, 5):
                e = self._upload_item(artifacts_dir, data[0], data[1])
                if not e:
                    break
                msg = "Error uploading %s, sleeping and retrying" % data[0]
                self.update_status("UPLOADING", msg)
                time.sleep(2 * i)  # try and give the server a moment
            return e

        # it seems that 100 is about the most URLs you can sign in one HTTP
        # request, so we'll split up our uploads array into groups of 75 to
        # be safe and upload them in bunches
        errors = []
        upload_groups = split(uploads, 75)
        for i, upload_group in enumerate(upload_groups):
            if self.SIMULATED:
                self.update_status("UPLOADING", "simulate %s" % upload_group)
                continue
            urls = self._get_urls(upload_group)
            p = ThreadPool(4)
            errors.extend([x for x in p.map(_upload_cb, urls.items()) if x])
            if len(upload_groups) > 2:  # lets give some status messages
                msg = "Uploading %d%% complete" % (100 * (i + 1) / len(upload_groups))
                self.update_status("UPLOADING", msg)
        return errors
