# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

import yaml

from unittest import mock

from jobserv.api.github import _get_params, _get_proj_def
from jobserv.jsend import ApiError

from tests import JobServTest


class ApiTest(JobServTest):
    @mock.patch("requests.get")
    @mock.patch("time.sleep")
    def test_find_target_sha(self, sleep, get):
        """Ensure we return an ApiError on a failed GET"""
        rv = mock.Mock()
        rv.status_code = 123
        rv.text = "abc"
        get.return_value = rv
        with self.assertRaises(ApiError):
            _get_params("owner", "repo", 3, "token")

    @mock.patch("requests.get")
    def test_get_proj_def_repo(self, get):
        """Ensure no grossly bad python for the definition_repo case"""
        rv = mock.Mock()
        rv.status_code = 200
        rv.text = yaml.dump({})
        get.return_value = rv
        trigger = mock.Mock()
        trigger.definition_repo = "https://github.com/foo"
        with self.assertRaisesRegex(ValueError, "No github_pr trigger types"):
            _get_proj_def(trigger, "owner", "repo", "sha", "token")

    @mock.patch("requests.get")
    def test_get_proj_def_heracles(self, get):
        """Ensure no grossly bad python for the .jobserv.yml case"""
        trigger = mock.Mock()
        trigger.definition_repo = ""

        exp = r"Project definition does not exist:.*\.jobserv.yml"
        with self.assertRaisesRegex(ValueError, exp):
            _get_proj_def(trigger, "owner", "repo", "sha", "token")
