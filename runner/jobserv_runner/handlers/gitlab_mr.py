# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

import json

from jobserv_runner.handlers.git_poller import GitPoller, HandlerError
from jobserv_runner.jobserv import JobServApi, _post

STATUS_MAP = {
    'RUNNING': 'running',
    'PASSED': 'success',
    'FAILED': 'failure',
}


class StatusApi(JobServApi):
    """Extend the JobServApi to also update the GitLab MergeRequest"""
    def __init__(self, rundef):
        super().__init__(rundef['run_url'], rundef['api_key'])

        self.headers = {
            'Content-Type': 'application/json',
            'PRIVATE-TOKEN': rundef['secrets']['gitlabtok'],
        }
        self.data = {
            'context': rundef['env']['H_RUN'],
            'description': 'Build ' + rundef['env']['H_BUILD'],
            'target_url': rundef['frontend_url'],
        }
        self.status_url = rundef['env']['GL_STATUS_URL']

    def update_run(self, msg, status=None, retry=2, metadata=None):
        rv = super().update_run(msg, status, retry, metadata)
        state = STATUS_MAP.get(status)
        if state and self.data.get('state') != state:
            self.data['state'] = state
            data = json.dumps(self.data).encode()
            _post(self.status_url, data, self.headers, raise_error=True)
        return rv


class GitLab(GitPoller):
    def _get_http_clone_token(self):
        user = self.rundef.get('secrets', {}).get('gitlabuser')
        if not user:
            user = self.rundef['env']['gitlabuser']
        token = self.rundef['secrets']['gitlabtok']
        return user + ':' + token

    @classmethod
    def get_jobserv(clazz, rundef):
        if rundef.get('simulator'):
            return GitPoller.get_jobserv(rundef)
        user = rundef.get('secrets', {}).get('gitlabuser')
        if not user:
            user = rundef['env']['gitlabuser']
        token = rundef.get('secrets', {}).get('gitlabtok')
        if not user or not token:
            raise HandlerError(
                '"gitlabuser" and/or "gitlabtok" not set in rundef secrets')
        jobserv = StatusApi(rundef)
        jobserv.update_run(b'', 'RUNNING')
        return jobserv


handler = GitLab