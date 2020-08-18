# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

from base64 import b64encode
import os
from shutil import which
import subprocess

import requests

from jobserv_runner.handlers.simple import HandlerError, SimpleHandler

SUPPORTS_SUBMODULE = os.path.exists('/usr/libexec/git-core/git-submodule') or \
                     os.path.exists('/usr/lib/git-core/git-submodule')
SUPPORTS_LFS = which('git-lfs') is not None


def b64(val):
    return b64encode(val.encode()).decode()


class GitPoller(SimpleHandler):
    def _lfs_initialize(self, env):
        subprocess.check_call(['git', 'lfs', 'install'], env=env)

    def _needs_auth(self, repo_url):
        if not repo_url.endswith('.git'):
            repo_url += '.git'
        if repo_url[-1] != '/':
            repo_url += '/'
        repo_url += 'info/refs?service=git-upload-pack'
        resp = requests.get(repo_url)
        return resp.status_code != 200

    def _create_github_content(self, log, fd, secrets):
        # User's often point to private github repositories. User's
        # normally use ssh+git because that works well locally. This
        # doesn't work for us, but hopefully we have a githubtok
        # present. This tells git to use https which will use the token
        fd.write('[url "https://github.com/"]\n')
        fd.write('  insteadOf = "git@github.com:"\n')

        tok = secrets.get('githubtok')
        if tok:
            log.info('Adding githubtok to .gitconfig')
            fd.write('[http "https://github.com"]\n')
            fd.write('  extraheader = Authorization: Basic ' + b64(tok) + '\n')

    def _create_gitlab_content(self, log, fd, secrets, clone_url):
        # we can't determine by URL if its a gitlab repo, so just assume
        # the rundef/secrets are done sanely by the user
        env = self.rundef['env']
        user = env.get('gitlabuser') or secrets.get('gitlabuser')
        if user:
            log.info('Adding gitlabtok to .gitconfig')
            token = self.rundef['secrets']['gitlabtok']
            fd.write('[http "%s"]\n' % clone_url)
            fd.write('  extraheader = Authorization: Basic ')
            fd.write(b64(user + ':' + token) + '\n')

    def _create_gitconfig(self, log, clone_url, gitconfig):
        # Its hard to know if the clone_url needs authentication or not. The
        # github, gitlab, or git.http.extraheader secrets *could* be for
        # secondary repositories used in the actual CI script. This is a simple
        # way to see if we need the creds *before* we try and pass them to
        # the server
        log.info('Checking to see if %s requires authentication.', clone_url)
        if not self._needs_auth(clone_url):
            log.info('Server does not appear to need credentials for cloning')

        secrets = self.rundef.get('secrets') or {}

        with open(gitconfig, 'w') as f:
            self._create_github_content(log, f, secrets)
            self._create_gitlab_content(log, f, secrets, clone_url)

            header = secrets.get('git.http.extraheader')
            if header:
                log.info('Adding git.http.extraheader to .gitconfig')
                f.write('[http "%s"]\n' % clone_url)
                f.write('  extraheader = ' + header + '\n')

    def _clone(self, log, dst):
        clone_url = self.rundef['env']['GIT_URL']
        log.info('Clone_url: %s', clone_url)

        gitconfig = os.path.join(self.run_dir, '.gitconfig')
        self._create_gitconfig(log, clone_url, gitconfig)
        # The env logic below is subtle: submodules might need
        # credentials for other repos (say gitlab or github). The
        # SimpleHandler class sets up a .netrc file in self.run_dir,
        # so this will let git find the .netrc file and use it for
        # this operation if needed. This also allows git to see the
        # .gitconfig file we create
        env = os.environ.copy()
        env['HOME'] = self.run_dir

        if SUPPORTS_SUBMODULE:
            log.info('Git install supports submodules')
        if SUPPORTS_LFS:
            log.info('Git install supports LFS')
            self._lfs_initialize(env)

        if not log.exec(['git', 'clone', clone_url, dst], env=env):
            raise HandlerError('Unable to clone: ' + clone_url)

        sha = self.rundef['env'].get('GIT_SHA')
        if sha:
            log.info('Checking out: %s', sha)
            if not log.exec(['git', 'branch', 'jobserv-run', sha], cwd=dst):
                raise HandlerError('Unable to branch: ' + sha)
            if not log.exec(['git', 'checkout', 'jobserv-run'], cwd=dst):
                raise HandlerError('Unable to checkout: ' + sha)
            if SUPPORTS_SUBMODULE:
                if not log.exec(
                        ['git', 'submodule', 'init'], cwd=dst, env=env):
                    raise HandlerError('Unable to init submodule(s)')

                if not log.exec(['git', 'submodule', 'update',
                                 '--init', '--recursive'],
                                cwd=dst, env=env):
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
