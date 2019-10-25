# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

import os

import requests

from base64 import b64encode

from jobserv_runner.handlers.simple import HandlerError, SimpleHandler


def b64(val):
    return b64encode(val.encode()).decode()


class GitPoller(SimpleHandler):
    def _needs_auth(self, repo_url):
        if not repo_url.endswith('.git'):
            repo_url += '.git'
        if repo_url[-1] != '/':
            repo_url += '/'
        repo_url += 'info/refs?service=git-upload-pack'
        resp = requests.get(repo_url)
        return resp.status_code != 200

    def _get_http_header(self, log, clone_url):
        # Its hard to know if the clone_url needs authentication or not. The
        # github, gitlab, or git.http.extraheader secrets *could* be for
        # secondary repositories used in the actual CI script. This is a simple
        # way to see if we need the creds *before* we try and pass them to
        # the server
        log.info('Checking to see if %s requires authentication.', clone_url)
        if not self._needs_auth(clone_url):
            log.info('Server does not appear to need credentials for cloning')
            return
        secrets = self.rundef.get('secrets', {})
        if clone_url.startswith('https://github.com'):
            tok = secrets.get('githubtok')
            if tok:
                log.info('Using github secret to clone repo')
                return 'Authorization: Basic ' + b64(tok)

        # we can't determine by URL if its a gitlab repo, so just assume
        # the rundef/secrets are done sanely by the user
        env = self.rundef['env']
        user = env.get('gitlabuser') or secrets.get('gitlabuser')
        if user:
            log.info('Using gitlab secret to clone repo')
            token = self.rundef['secrets']['gitlabtok']
            return 'Authorization: Basic ' + b64(user + ':' + token)

        secrets = self.rundef.get('secrets', {})
        header = secrets.get('git.http.extraheader')
        if header:
            log.info('Using git.http.extraheader to clone repo')
        return header

    def _clone(self, log, dst):
        clone_url = self.rundef['env']['GIT_URL']
        log.info('Clone_url: %s', clone_url)

        args = ['git']
        header = self._get_http_header(log, clone_url)
        if header:
            args.extend(['-c', 'http.extraheader=' + header])
        args.extend(['clone', '--recursive', clone_url, dst])
        if not log.exec(args):
            raise HandlerError('Unable to clone: ' + clone_url)

        sha = self.rundef['env'].get('GIT_SHA')
        if sha:
            log.info('Checking out: %s', sha)
            if not log.exec(['git', 'branch', 'jobserv-run', sha], cwd=dst):
                raise HandlerError('Unable to branch: ' + sha)
            if not log.exec(['git', 'checkout', 'jobserv-run'], cwd=dst):
                raise HandlerError('Unable to checkout: ' + sha)
            # `git submodule update` is a no-op for repos without submodules
            if not log.exec(['git', 'submodule', 'update'], cwd=dst):
                raise HandlerError('Unable to update submodule(s)')

    def prepare_mounts(self):
        mounts = super().prepare_mounts()

        repo_dir = os.path.join(self.run_dir, 'repo')
        with self.log_context('Cloning git repository') as log:
            if os.path.exists(repo_dir):
                log.warn('Reusing repository from previous run')
            else:
                self._clone(log, repo_dir)
        mounts.append((repo_dir, '/repo'))
        self.container_cwd = '/repo'
        return mounts


handler = GitPoller
