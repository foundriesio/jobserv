# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>
import random
from typing import List, Dict
from unittest import TestCase, mock

from jobserv import git_poller


def _fake_change_params(
    head_sha: str = "fake",
    url: str = "https://fake.com",
) -> Dict[str, str]:
    return {
        "GIT_OLD_SHA": f"old{head_sha}",
        "GIT_SHA": head_sha,
        "GIT_URL": url,
    }


def _fake_entry() -> git_poller.PollerEntry:
    definition = git_poller.ProjectDefinition({"triggers": []})
    trigger = _fake_trigger()
    return git_poller.PollerEntry(trigger=trigger, definition=definition)


def _fake_trigger() -> git_poller.ProjectTrigger:
    return git_poller.ProjectTrigger(
        12,
        "t",
        "proj",
        "user",
        1,
    )


def _fake_content(skip) -> str:
    msg = "fake %s fake\nmore fake"
    choices = ("[skip ci]", "[ci skip]")
    return msg % (random.choice(choices) if skip else "")


def _fake_github_content(
    skip_head_content: bool = False,
    skip_other_content: bool = False,
    head_sha: str = "fakeheadsha",
) -> List[Dict[str, str]]:
    head_content = _fake_content(skip_head_content)
    other_content = _fake_content(skip_other_content)
    # truncated output below
    return [
        {
            "sha": head_sha,
            "commit": {
                "message": head_content,
            },
        },
        {
            "sha": f"not{head_sha}",
            "commit": {
                "message": other_content,
            },
        },
    ]


def _fake_gitlab_content(
    skip_head_title: bool = False,
    skip_head_content: bool = False,
    skip_other_title: bool = False,
    skip_other_content: bool = False,
    head_sha: str = "fakeheadsha",
) -> List[Dict[str, str]]:
    head_title = _fake_content(skip_head_title)
    head_content = _fake_content(skip_head_content)
    other_title = _fake_content(skip_other_title)
    other_content = _fake_content(skip_other_content)
    # truncated output below
    return [
        {
            "id": head_sha,
            "short_id": "fakeshorthead",
            "title": head_title,
            "message": head_content,
        },
        {
            "id": f"not{head_sha}",
            "short_id": "fakeshortnothead",
            "title": other_title,
            "message": other_content,
        },
    ]


def _fake_xml_content(
    skip_head_title: bool = False,
    skip_head_content: bool = False,
    skip_other_title: bool = False,
    skip_other_content: bool = False,
    head_sha: str = "fakeheadsha",
) -> str:
    head_title = _fake_content(skip_head_title)
    head_content = _fake_content(skip_head_content)
    other_title = _fake_content(skip_other_title)
    other_content = _fake_content(skip_other_content)
    # truncated output below
    return f"""
        <feed xmlns='http://www.w3.org/2005/Atom'>
            <entry>
                <title>{head_title}: 2018-11-15</title>
                <id>{head_sha}</id>
                <content type='text'>
                    {head_content}
                </content>
            </entry>
            <entry>
                <title>{other_title}</title>
                <id>not{head_sha}</id>
                <content type='text'>
                    {other_content}
                </content>
            </entry>
        </feed>
    """


class TestGitPoller(TestCase):
    def setUp(self):
        super().setUp()

    @mock.patch("jobserv.git_poller.permissions")
    def test_get_project_triggers(self, perms):
        resp = mock.Mock()
        resp.status_code = 200
        resp.json.return_value = {
            "data": [
                {
                    "id": 12,
                    "type": "git_poller",
                    "project": "foo",
                    "user": "root",
                    "queue_priority": 1,
                },
            ],
        }
        perms.internal_get.return_value = resp
        project_triggers = git_poller._get_project_triggers().values()
        self.assertEqual([(12, "foo")], [(x.id, x.project) for x in project_triggers])

    @mock.patch("jobserv.git_poller._get_project_triggers")
    @mock.patch("jobserv.git_poller.Storage")
    def test_poll_remove(self, storage, get_project_triggers):
        get_project_triggers.return_value = {}

        project_triggers = {}
        git_poller._poll(project_triggers)
        self.assertEqual({}, project_triggers)

    @mock.patch("jobserv.git_poller._get_projdef")
    @mock.patch("jobserv.git_poller._get_project_triggers")
    @mock.patch("jobserv.git_poller.Storage")
    def test_poll_add(self, storage, get_project_triggers, get_projdef):
        get_project_triggers.return_value = {
            "foo": git_poller.ProjectTrigger(12, "t", "proj", "user", 1),
        }
        get_projdef.return_value = None  # prevents trying to really poll

        project_triggers = {}
        git_poller._poll(project_triggers)
        self.assertEqual(["foo"], list(project_triggers.keys()))

    @mock.patch("jobserv.git_poller._get_projdef")
    @mock.patch("jobserv.git_poller._get_project_triggers")
    @mock.patch("jobserv.git_poller.Storage")
    def test_poll_updated(self, storage, get_project_triggers, get_projdef):
        project_triggers = {
            "foo": git_poller.PollerEntry(
                git_poller.ProjectTrigger(12, "t", "proj", "user", 1)
            ),
        }
        get_project_triggers.return_value = {
            "foo": git_poller.ProjectTrigger(12, "t", "proj", "user", 0, "r"),
        }
        get_projdef.return_value = None  # prevents trying to really poll

        git_poller._poll(project_triggers)
        self.assertEqual(0, project_triggers["foo"].trigger.queue_priority)
        self.assertEqual("r", project_triggers["foo"].trigger.definition_repo)

    @mock.patch("jobserv.git_poller.requests")
    def test_get_refs(self, requests):
        requests.get().status_code = 200
        requests.get().text = """ignore
ignore
004015f12d4181355604efa7b429fc3bcbae08d27f40 refs/heads/master
004015f12d4181355604efa7b429fc3bcbae08d27f41 refs/pulls/123
"""
        trigger = git_poller.ProjectTrigger(
            id=1, type="t", project="p", user="u", queue_priority=1
        )
        vals = []
        for sha, ref in git_poller._get_refs("doesnot matter", trigger):
            vals.append((sha, ref))
        expected = [
            ("15f12d4181355604efa7b429fc3bcbae08d27f40", "refs/heads/master"),
            ("15f12d4181355604efa7b429fc3bcbae08d27f41", "refs/pulls/123"),
        ]
        self.assertEqual(expected, vals)

    @mock.patch("jobserv.git_poller.requests")
    def test_get_refs_fatal(self, requests):
        requests.get().status_code = 500
        requests.get().text = "foobar"
        trigger = git_poller.ProjectTrigger(
            id=1, type="t", project="p", user="u", queue_priority=1
        )
        vals = []
        for sha, ref in git_poller._get_refs("doesnot matter", trigger):
            vals.append((sha, ref))
        self.assertEqual([], vals)

    @mock.patch("jobserv.git_poller._get_refs")
    def test_repo_changes_first_run(self, get_refs):
        trigger = git_poller.ProjectTrigger(
            id=1, type="t", project="p", user="u", queue_priority=1
        )
        refs = ["ref1"]
        get_refs.return_value = [
            ("sha1", "ref1"),
            ("sha2", "ref2"),
        ]
        cache = {}
        change_params = git_poller._get_repo_changes(cache, "url1", refs, trigger)
        self.assertEqual([], list(change_params))
        self.assertEqual({"url1": {"ref1": "sha1"}}, cache)

        refs = ["refs1", "ref2"]
        change_params = git_poller._get_repo_changes(cache, "url1", refs, trigger)
        self.assertEqual([], list(change_params))
        self.assertEqual({"url1": {"ref1": "sha1", "ref2": "sha2"}}, cache)

    @mock.patch("jobserv.git_poller._get_refs")
    def test_repo_changes_changed(self, get_refs):
        trigger = git_poller.ProjectTrigger(
            id=1, type="t", project="p", user="u", queue_priority=1
        )
        refs = ["ref1", "ref2"]
        get_refs.return_value = [
            ("sha1", "ref1"),
            ("sha2", "ref2"),
        ]
        cache = {"url1": {"ref1": "oldsha", "ref2": "sha2"}}
        change_params = git_poller._get_repo_changes(cache, "url1", refs, trigger)
        expected = [
            {
                "GIT_URL": "url1",
                "GIT_OLD_SHA": "oldsha",
                "GIT_SHA": "sha1",
                "GIT_REF": "ref1",
            }
        ]
        self.assertEqual(expected, list(change_params))

    @mock.patch.object(git_poller.permissions, "internal_post")
    @mock.patch("jobserv.git_poller._github_log")
    def test_trigger_skip_github(self, github_log, poster):
        github_log.return_value = ("fake", True)

        git_poller._trigger(
            entry=_fake_entry(),
            trigger_name="fake",
            change_params={"GIT_URL": "https://github.com"},
        )

        self.assertTrue(github_log.called)
        self.assertIs(poster.called, False)

    @mock.patch.object(git_poller.permissions, "internal_post")
    @mock.patch("jobserv.git_poller._gitlab_log")
    def test_trigger_skip_gitlab(self, gitlab_log, poster):
        gitlab_log.return_value = ("fake", True)
        fake_gitlab_server = "https://fake.com"

        with mock.patch("jobserv.git_poller.GITLAB_SERVERS", [fake_gitlab_server]):
            git_poller._trigger(
                entry=_fake_entry(),
                trigger_name="fake",
                change_params={"GIT_URL": fake_gitlab_server},
            )

        self.assertTrue(gitlab_log.called)
        self.assertIs(poster.called, False)

    @mock.patch.object(git_poller.permissions, "internal_post")
    @mock.patch("jobserv.git_poller._cgit_log")
    def test_trigger_skip_cgit(self, cgit_log, poster):
        cgit_log.return_value = ("fake", True)

        git_poller._trigger(
            entry=_fake_entry(), trigger_name="fake", change_params={"GIT_URL": "fake"}
        )

        self.assertTrue(cgit_log.called)
        self.assertIs(poster.called, False)

    @mock.patch("jobserv.git_poller.requests")
    def test_cgit_log_skip_head_title(self, requests):
        head_sha = "fakeheadid"
        response_mock = mock.Mock()
        requests.get.return_value = response_mock
        response_mock.status_code = 200
        response_mock.text = _fake_xml_content(skip_head_title=True, head_sha=head_sha)

        actual_output, actual_skipped = git_poller._cgit_log(
            trigger=_fake_trigger(),
            change_params=_fake_change_params(head_sha=head_sha),
        )

        self.assertEqual(actual_skipped, True)

    @mock.patch("jobserv.git_poller.requests")
    def test_cgit_log_skip_head_content(self, requests):
        head_sha = "fakeheadid"
        response_mock = mock.Mock()
        requests.get.return_value = response_mock
        response_mock.status_code = 200
        response_mock.text = _fake_xml_content(
            skip_head_content=True, head_sha=head_sha
        )

        actual_output, actual_skipped = git_poller._cgit_log(
            trigger=_fake_trigger(),
            change_params=_fake_change_params(head_sha=head_sha),
        )

        self.assertEqual(actual_skipped, True)

    @mock.patch("jobserv.git_poller.requests")
    def test_cgit_log_not_skip_other_title(self, requests):
        head_sha = "fakeheadid"
        response_mock = mock.Mock()
        requests.get.return_value = response_mock
        response_mock.status_code = 200
        response_mock.text = _fake_xml_content(skip_other_title=True, head_sha=head_sha)

        actual_output, actual_skipped = git_poller._cgit_log(
            trigger=_fake_trigger(),
            change_params=_fake_change_params(head_sha=head_sha),
        )

        self.assertEqual(actual_skipped, False)

    @mock.patch("jobserv.git_poller.requests")
    def test_cgit_log_not_skip_other_content(self, requests):
        head_sha = "fakeheadid"
        response_mock = mock.Mock()
        requests.get.return_value = response_mock
        response_mock.status_code = 200
        response_mock.text = _fake_xml_content(
            skip_other_content=True, head_sha=head_sha
        )

        actual_output, actual_skipped = git_poller._cgit_log(
            trigger=_fake_trigger(),
            change_params=_fake_change_params(head_sha=head_sha),
        )

        self.assertEqual(actual_skipped, False)

    @mock.patch("jobserv.git_poller.requests")
    def test_github_log_skip_head_message(self, requests):
        head_sha = "fakeheadid"
        response_mock = mock.Mock()
        requests.get.return_value = response_mock
        response_mock.status_code = 200
        response_mock.json.return_value = _fake_github_content(
            head_sha=head_sha, skip_head_content=True
        )

        actual_output, actual_skipped = git_poller._github_log(
            trigger=_fake_trigger(),
            change_params=_fake_change_params(head_sha=head_sha),
        )

        self.assertEqual(actual_skipped, True)

    @mock.patch("jobserv.git_poller.requests")
    def test_github_log_not_skip_other_message(self, requests):
        head_sha = "fakeheadid"
        response_mock = mock.Mock()
        requests.get.return_value = response_mock
        response_mock.status_code = 200
        response_mock.json.return_value = _fake_github_content(
            head_sha=head_sha, skip_other_content=True
        )

        actual_output, actual_skipped = git_poller._github_log(
            trigger=_fake_trigger(),
            change_params=_fake_change_params(head_sha=head_sha),
        )

        self.assertEqual(actual_skipped, False)

    @mock.patch("jobserv.git_poller.requests")
    def test_github_log_not_skip(self, requests):
        head_sha = "fakeheadid"
        response_mock = mock.Mock()
        requests.get.return_value = response_mock
        response_mock.status_code = 200
        response_mock.json.return_value = _fake_github_content(
            head_sha=head_sha, skip_head_content=False, skip_other_content=False
        )

        actual_output, actual_skipped = git_poller._github_log(
            trigger=_fake_trigger(),
            change_params=_fake_change_params(head_sha=head_sha),
        )

        self.assertEqual(actual_skipped, False)

    @mock.patch("jobserv.git_poller.requests")
    def test_gitlab_skip_head_title(self, requests):
        head_sha = "fakeheadid"
        response_mock = mock.Mock()
        requests.get.return_value = response_mock
        response_mock.status_code = 200
        response_mock.json.return_value = _fake_gitlab_content(
            head_sha=head_sha, skip_head_title=True
        )

        actual_output, actual_skipped = git_poller._gitlab_log(
            trigger=_fake_trigger(),
            change_params=_fake_change_params(head_sha=head_sha),
        )

        self.assertEqual(actual_skipped, True)

    @mock.patch("jobserv.git_poller.requests")
    def test_gitlab_skip_head_content(self, requests):
        head_sha = "fakeheadid"
        response_mock = mock.Mock()
        requests.get.return_value = response_mock
        response_mock.status_code = 200
        response_mock.json.return_value = _fake_gitlab_content(
            head_sha=head_sha, skip_head_content=True
        )

        actual_output, actual_skipped = git_poller._gitlab_log(
            trigger=_fake_trigger(),
            change_params=_fake_change_params(head_sha=head_sha),
        )

        self.assertEqual(actual_skipped, True)

    @mock.patch("jobserv.git_poller.requests")
    def test_gitlab_not_skip_other_title(self, requests):
        head_sha = "fakeheadid"
        response_mock = mock.Mock()
        requests.get.return_value = response_mock
        response_mock.status_code = 200
        response_mock.json.return_value = _fake_gitlab_content(
            head_sha=head_sha, skip_other_title=True
        )

        actual_output, actual_skipped = git_poller._gitlab_log(
            trigger=_fake_trigger(),
            change_params=_fake_change_params(head_sha=head_sha),
        )

        self.assertEqual(actual_skipped, False)

    @mock.patch("jobserv.git_poller.requests")
    def test_gitlab_not_skip_other_content(self, requests):
        head_sha = "fakeheadid"
        response_mock = mock.Mock()
        requests.get.return_value = response_mock
        response_mock.status_code = 200
        response_mock.json.return_value = _fake_gitlab_content(
            head_sha=head_sha, skip_other_content=True
        )

        actual_output, actual_skipped = git_poller._gitlab_log(
            trigger=_fake_trigger(),
            change_params=_fake_change_params(head_sha=head_sha),
        )

        self.assertEqual(actual_skipped, False)
