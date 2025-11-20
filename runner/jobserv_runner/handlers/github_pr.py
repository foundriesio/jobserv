# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

import json
import logging
import time

from jobserv_runner.handlers.git_poller import GitPoller, HandlerError
from jobserv_runner.jobserv import JobServApi, PostError, _post

STATUS_MAP = {
    "PASSED": "success",
    "FAILED": "failure",
}


class GHStatusApi(JobServApi):
    """Extend the JobServApi to also update the GitHub Pull Request"""

    def __init__(self, rundef):
        super().__init__(rundef["run_url"], rundef["api_key"])

        self.headers = {
            "Content-Type": "application/json",
            "Authorization": "token " + rundef["secrets"]["githubtok"],
        }
        self.data = {
            "context": rundef["env"]["H_RUN"],
            "description": "Build " + rundef["env"]["H_BUILD"],
            "target_url": rundef["frontend_url"],
        }
        self.status_url = rundef["env"]["GH_STATUS_URL"]

    def update_run(self, msg, status=None, retry=2, metadata=None):
        rv = super().update_run(msg, status, retry, metadata)
        state = STATUS_MAP.get(status, "pending")
        if self.data.get("state") != state:
            self.data["state"] = state
            data = json.dumps(self.data).encode()
            for i in range(4):
                if i:
                    logging.info("Failed to update run, sleeping and retrying")
                    time.sleep(2 * i)
                try:
                    r = _post(self.status_url, data, self.headers, raise_error=True)
                    if r.status < 300:
                        break
                except PostError as e:
                    logging.error(e)
            else:
                logging.error("Unable to update run: %d: %s", r.status_code, r.text)
        return rv


class GitHub(GitPoller):
    @classmethod
    def get_jobserv(clazz, rundef):
        if rundef.get("simulator"):
            return GitPoller.get_jobserv(rundef)
        token = rundef.get("secrets", {}).get("githubtok")
        if not token:
            raise HandlerError('"githubtok" not set in rundef secrets')
        jobserv = GHStatusApi(rundef)
        jobserv.update_run(b"", "RUNNING")
        return jobserv


handler = GitHub
