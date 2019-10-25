# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

import os
import shutil
import subprocess
import tempfile

from unittest import TestCase, mock

from jobserv_runner.handlers.git_poller import GitPoller, HandlerError


class GitPollerHandlerTest(TestCase):
    def setUp(self):
        super().setUp()

        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir)

        rdir = os.path.join(self.tmpdir, 'run')
        wdir = os.path.join(self.tmpdir, 'worker')
        os.mkdir(rdir)
        os.mkdir(wdir)
        self.handler = GitPoller(wdir, rdir, mock.Mock(), None)

        def no(self):
            return True

        self.handler._needs_auth = no

    def _create_repo(self, repo_src='repo-src'):
        """Create a git repo with 2 commits and return sha of the 1st.

           This allows us to ensure we clone and check out the proper sha.
        """
        repo_src = os.path.join(self.tmpdir, repo_src)
        os.mkdir(repo_src)
        subprocess.check_call(['git', 'init'], cwd=repo_src)
        with open(os.path.join(repo_src, 'file1.txt'), 'w') as f:
            f.write('content\n')
        subprocess.check_call(['git', 'add', '.'], cwd=repo_src)
        subprocess.check_call(['git', 'commit', '-m', '1'], cwd=repo_src)
        repo_sha = subprocess.check_output(
            ['git', 'log', '-1', '--format=%H'], cwd=repo_src)

        with open(os.path.join(repo_src, 'file1.txt'), 'w') as f:
            f.write('content\ncontent\n')
        subprocess.check_call(['git', 'commit', '-a', '-m', '2'], cwd=repo_src)

        return repo_src, repo_sha.decode().strip()

    def test_prepare_mounts(self):
        """Ensure we can clone a repo and check it the proper sha."""
        repo_src, repo_sha = self._create_repo()
        self.handler.rundef = {
            'script': '',
            'persistent-volumes': None,
            'run_url': 'foo',
            'env': {
                'GIT_URL': repo_src,
                'GIT_SHA': repo_sha,
            }
        }
        self.handler.prepare_mounts()

        repo = os.path.join(self.tmpdir, 'run/repo')
        sha = subprocess.check_output(
            ['git', 'log', '-1', '--format=%H'], cwd=repo)
        self.assertEqual(repo_sha, sha.decode().strip())

    def test_prepare_mounts_bad_clone(self):
        """Ensure we can clone a repo and check it the proper sha."""
        self.handler.rundef = {
            'script': '',
            'persistent-volumes': None,
            'run_url': 'foo',
            'env': {
                'GIT_URL': '/tmp/foo-bar-does-not-existz',
                'GIT_SHA': 'doesnt matter',
            }
        }
        msg = 'Unable to clone: /tmp/foo-bar-does-not-existz'
        with self.assertRaisesRegex(HandlerError, msg):
            self.handler.prepare_mounts()

    def test_prepare_mounts_bad_sha(self):
        """Ensure we can clone a repo and check it the proper sha."""
        repo_src, repo_sha = self._create_repo()
        self.handler.rundef = {
            'script': '',
            'persistent-volumes': None,
            'run_url': 'foo',
            'env': {
                'GIT_URL': repo_src,
                'GIT_SHA': 'badbeef',
            }
        }
        msg = 'Unable to branch: badbeef'
        with self.assertRaisesRegex(HandlerError, msg):
            self.handler.prepare_mounts()

    def test_private_github(self):
        clone_url = 'https://github.com/nosuchorog/nosuchrepo'
        self.handler.rundef = {
            'script': '',
            'persistent-volumes': None,
            'run_url': 'foo',
            'env': {
                'GIT_URL': clone_url,
                'GIT_SHA': 'badbeef',
            },
            'secrets': {
                'githubtok': 'ThisIsTestGitHubToken',
            }
        }
        header = self.handler._get_http_header(mock.Mock(), clone_url)
        self.assertEqual(
            'Authorization: Basic VGhpc0lzVGVzdEdpdEh1YlRva2Vu', header)

    def test_private_gitlab(self):
        clone_url = 'https://git.com/nosuchorog/nosuchrepo'
        self.handler.rundef = {
            'script': '',
            'persistent-volumes': None,
            'run_url': 'foo',
            'env': {
                'GIT_URL': clone_url,
                'GIT_SHA': 'badbeef',
            },
            'secrets': {
                'gitlabuser': 'foo',
                'gitlabtok': 'ThisIsTestGitLab',
            }
        }
        header = self.handler._get_http_header(mock.Mock(), clone_url)
        self.assertEqual(
            'Authorization: Basic Zm9vOlRoaXNJc1Rlc3RHaXRMYWI=', header)

    def test_git_submodules(self):
        """Ensure that we pull in the proper git submodules"""
        repo_src, repo_sha = self._create_repo()
        sub_src, sub_sha = self._create_repo('submod')

        # now add submodule
        subprocess.check_call(
            ['git', 'submodule', 'add', sub_src], cwd=repo_src)
        subprocess.check_call(['git', 'add', '.'], cwd=repo_src)
        subprocess.check_call(['git', 'commit', '-m', 'addsub'], cwd=repo_src)
        repo_sha = subprocess.check_output(
            ['git', 'log', '-1', '--format=%H'], cwd=repo_src).decode().strip()

        # trigger the clone
        self.handler.rundef = {
            'script': '',
            'persistent-volumes': None,
            'run_url': 'foo',
            'env': {
                'GIT_URL': repo_src,
                'GIT_SHA': repo_sha,
            }
        }
        self.handler.prepare_mounts()

        repo = os.path.join(self.tmpdir, 'run/repo')
        with open(os.path.join(repo, 'submod/file1.txt')) as f:
            self.assertEqual('content\ncontent\n', f.read())
