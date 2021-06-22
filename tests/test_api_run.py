# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

import contextlib
import hmac
import json
import os
import shutil
import tempfile
from time import sleep
import yaml

from unittest.mock import Mock, patch

from jobserv import permissions
import jobserv.models
import jobserv.storage.base

from jobserv.storage import Storage
from jobserv.models import (
    Build, BuildStatus, Project, Run, Test, TestResult, db)

from tests import JobServTest


class RunAPITest(JobServTest):
    def setUp(self):
        super().setUp()
        self.create_projects('proj-1')
        p = Project.query.all()[0]
        self.build = Build.create(p)
        self.urlbase = '/projects/proj-1/builds/1/runs/'

        jobserv.storage.base.JOBS_DIR = tempfile.mkdtemp()
        jobserv.models.JOBS_DIR = jobserv.storage.base.JOBS_DIR
        self.addCleanup(shutil.rmtree, jobserv.storage.base.JOBS_DIR)

    def test_no_runs(self):
        self.assertEqual([], self.get_json(self.urlbase)['runs'])

    def test_run_list(self):
        db.session.add(Run(self.build, 'run0'))
        db.session.add(Run(self.build, 'run1'))

        runs = self.get_json(self.urlbase)['runs']
        self.assertEqual(2, len(runs))
        for i, r in enumerate(runs):
            self.assertEqual('run%d' % i, r['name'])
            self.assertEqual('QUEUED', r['status'])

    @patch('jobserv.storage.gce_storage.storage')
    def test_run_get(self, storage):
        db.session.add(Run(self.build, 'run0'))
        db.session.add(Run(self.build, 'run1'))

        # be a little anal and make sure our DB queries can handle
        # multiple jobs with the same run names
        self.create_projects('proj-2')
        p = Project.query.all()[1]
        b = Build.create(p)
        r = Run(b, 'run0')
        db.session.add(r)
        r.set_status(BuildStatus.PASSED)
        r = Run(b, 'run1')
        db.session.add(r)
        r.set_status(BuildStatus.FAILED)

        data = self.get_json(self.urlbase + 'run1/')['run']
        self.assertEqual('run1', data['name'])
        self.assertEqual('QUEUED', data['status'])

        url = '/projects/proj-2/builds/%d/runs/%s/' % (b.build_id, 'run1')
        data = self.get_json(url)['run']
        self.assertEqual('FAILED', data['status'])
        self.assertEqual('FAILED', data['status_events'][0]['status'])

    @patch('jobserv.api.run.Storage')
    def test_run_get_definition(self, storage):
        """Ensure unauthenticated requests redact the secrets"""
        rundef = {
            'api_key': 'secret',
            'secrets': {'key': 'val'},
            'script-repo': {'token': 'secret'},
            'runner_url': 'foo',
        }
        storage().get_run_definition.return_value = json.dumps(rundef)
        db.session.add(Run(self.build, 'run0'))
        db.session.commit()
        r = self.client.get(self.urlbase + 'run0/.rundef.json')
        self.assertEqual(200, r.status_code, r.data)
        data = json.loads(r.data.decode())
        self.assertEqual('TODO', data['secrets']['key'])
        self.assertEqual('secret', data['script-repo']['token'])
        self.assertIsNone(data.get('api_key'))

        r = self.client.get(self.urlbase + 'run0/.simulate.sh')
        self.assertEqual(200, r.status_code, r.data)

    def _post(self, url, data, headers, status=200):
        resp = self.client.post(url, data=data, headers=headers)
        self.assertEqual(status, resp.status_code, resp.data)
        return resp

    def test_run_rerun(self):
        r = Run(self.build, 'run0')
        r.status = BuildStatus.FAILED
        db.session.add(r)
        db.session.commit()

        url = 'http://localhost' + self.urlbase + 'run0/rerun'

        headers = {}
        self._post(url, 'message', headers, 401)

        permissions._sign(url, headers, 'POST')
        self._post(url, 'message', headers, 200)

    @patch('jobserv.storage.gce_storage.storage')
    def test_run_cancel(self, storage):
        r = Run(self.build, 'run0')
        db.session.add(r)
        db.session.commit()

        headers = {}
        url = 'http://localhost' + self.urlbase + 'run0/cancel'

        self._post(url, 'message', headers, 401)

        permissions._sign(url, headers, 'POST')
        self._post(url, '', headers, 202)
        db.session.refresh(r)
        self.assertEqual(BuildStatus.CANCELLING, r.status)

    def test_run_stream_not_authenticated(self):
        r = Run(self.build, 'run0')
        db.session.add(r)
        db.session.commit()

        headers = [('Authorization', 'Token badtoken')]
        self._post(self.urlbase + 'run0/', 'message', headers, 401)

    @patch('jobserv.storage.gce_storage.storage')
    def test_run_stream(self, storage):
        r = Run(self.build, 'run0')
        db.session.add(r)
        db.session.commit()

        headers = [
            ('Authorization', 'Token %s' % r.api_key),
            ('X-RUN-STATUS', 'RUNNING'),
        ]
        self._post(self.urlbase + 'run0/', 'message', headers, 200)

        with Storage().console_logfd(r, 'r') as f:
            self.assertEqual('message', f.read())
        db.session.refresh(r)
        self.assertEqual('RUNNING', r.status.name)

    @patch('jobserv.storage.gce_storage.storage')
    def test_get_stream(self, storage):
        r = Run(self.build, 'run0')
        r.status = BuildStatus.RUNNING
        db.session.add(r)
        db.session.commit()

        with Storage().console_logfd(r, 'ab') as f:
            f.write(b'this is the message')

        resp = self.client.get(self.urlbase + 'run0/console.log')
        self.assertEqual(200, resp.status_code)
        self.assertEqual('text/plain', resp.mimetype)

    @patch('jobserv.storage.gce_storage.storage')
    def test_run_metadata(self, storage):
        r = Run(self.build, 'run0')
        db.session.add(r)
        db.session.commit()

        headers = [
            ('Authorization', 'Token %s' % r.api_key),
            ('X-RUN-METADATA', 'foobar-meta'),
        ]
        self._post(self.urlbase + 'run0/', 'message', headers, 200)
        db.session.refresh(r)
        self.assertEqual('foobar-meta', r.meta)

    @patch('jobserv.storage.gce_storage.storage')
    def test_upload(self, storage):
        r = Run(self.build, 'run0')
        r.status = BuildStatus.RUNNING
        db.session.add(r)
        db.session.commit()

        headers = [('Authorization', 'Token %s' % r.api_key)]
        storage.generate_signed.return_value = {
            'foo': 'bar',
            'blah': 'bam',
        }
        url = self.urlbase + 'run0/create_signed'
        uploads = json.dumps(['foo', 'bar'])
        self._post(url, uploads, headers, 200)

    @patch('jobserv.api.run.Storage')
    @patch('jobserv.api.run.notify_build_complete_email')
    def test_run_complete_triggers(self, build_complete, storage):
        m = Mock()
        m.get_project_definition.return_value = json.dumps({
            'timeout': 5,
            'email': {
                'users': 'f@f.com',
            },
            'triggers': [
                {
                    'name': 'git',
                    'type': 'git_poller',
                    'runs': [{
                        'name': 'run0',
                        'host-tag': 'foo*',
                        'triggers': [
                            {'name': 'triggered', 'run-names': '{name}-run0'}
                        ]
                    }],
                },
                {
                    'name': 'triggered',
                    'type': 'simple',
                    'runs': [{
                        'name': 'test',
                        'host-tag': 'bar',
                        'container': 'container-foo',
                        'script': 'test',
                    }],
                },
            ],
            'scripts': {
                'test': '#test#',
            }
        })
        m.console_logfd.return_value = open('/dev/null', 'w')
        m.get_run_definition.return_value = json.dumps({})
        storage.return_value = m
        r = Run(self.build, 'run0')
        r.trigger = 'git'
        r.status = BuildStatus.RUNNING
        db.session.add(r)
        db.session.commit()

        headers = [
            ('Authorization', 'Token %s' % r.api_key),
            ('X-RUN-STATUS', 'PASSED'),
        ]
        self._post(self.urlbase + 'run0/', None, headers, 200)
        run = Run.query.all()[1]
        self.assertEqual('test-run0', run.name)
        self.assertEqual('QUEUED', run.status.name)

        rundef = json.loads(m.set_run_definition.call_args_list[0][0][1])
        self.assertEqual('git_poller', rundef['trigger_type'])

        # Make sure we didn't send the email since the build isn't complete yet
        self.assertEqual(0, build_complete.call_count)

    @patch('jobserv.api.run.Storage')
    def test_run_complete_triggers_type_upgrade(self, storage):
        """ We have a build that's triggered by either a github_pr or a
            gitlab_mr. They might have runs that trigger something of type
            "simple". This could be the case where a git_poller and github_mr
            both trigger a similar set of tests *after* a build. In the
            case of the github_pr, we should "upgrade" the type of each run
            from simple to github_pr so that it can update the status of the
            PR.
        """
        m = Mock()
        m.get_project_definition.return_value = json.dumps({
            'timeout': 5,
            'triggers': [
                {
                    'name': 'github',
                    'type': 'github_pr',
                    'runs': [{
                        'name': 'run0',
                        'host-tag': 'foo*',
                        'triggers': [
                            {'name': 'triggered', 'run-names': '{name}-run0'}
                        ]
                    }],
                },
                {
                    'name': 'triggered',
                    'type': 'simple',
                    'runs': [{
                        'name': 'test',
                        'host-tag': 'bar',
                        'container': 'container-foo',
                        'script': 'test',
                    }],
                },
            ],
            'scripts': {
                'test': '#test#',
            }
        })
        m.console_logfd.return_value = open('/dev/null', 'w')
        m.get_run_definition.return_value = json.dumps({})
        storage.return_value = m
        r = Run(self.build, 'run0')
        r.trigger = 'github'
        r.status = BuildStatus.RUNNING
        db.session.add(r)
        db.session.commit()

        headers = [
            ('Authorization', 'Token %s' % r.api_key),
            ('X-RUN-STATUS', 'PASSED'),
        ]
        self._post(self.urlbase + 'run0/', None, headers, 200)
        run = Run.query.all()[1]
        self.assertEqual('test-run0', run.name)
        rundef = json.loads(m.set_run_definition.call_args_list[0][0][1])
        self.assertEqual('github_pr', rundef['trigger_type'])

    @patch('jobserv.api.run.Storage')
    def test_run_complete_triggers_name_error(self, storage):
        m = Mock()
        m.get_project_definition.return_value = json.dumps({
            'timeout': 5,
            'triggers': [
                {
                    'name': 'github',
                    'type': 'github_pr',
                    'runs': [{
                        'name': 'run0',
                        'host-tag': 'foo*',
                        'triggers': [
                            {'name': 'triggered'}
                        ]
                    }],
                },
                {
                    'name': 'triggered',
                    'type': 'simple',
                    'runs': [{
                        'name': 'collision-name',
                        'host-tag': 'bar',
                        'container': 'container-foo',
                        'script': 'test',
                    }],
                },
            ],
            'scripts': {
                'test': '#test#',
            }
        })
        m.console_logfd.return_value = open('/dev/null', 'w')
        m.get_run_definition.return_value = json.dumps({})
        m.get_artifact_content.return_value = '#mocked line 1\n'
        storage.return_value = m
        r = Run(self.build, 'run0')
        r.trigger = 'github'
        r.status = BuildStatus.RUNNING
        db.session.add(r)
        # cause a duplicate name collision
        db.session.add(Run(self.build, 'collision-name'))
        db.session.commit()

        headers = [
            ('Authorization', 'Token %s' % r.api_key),
            ('X-RUN-STATUS', 'PASSED'),
        ]
        self._post(self.urlbase + 'run0/', None, headers, 200)
        self.assertEqual('RUNNING_WITH_FAILURES', r.build.status.name)

    @patch('jobserv.api.run.Storage')
    def test_run_complete_tests_default(self, storage):
        m = Mock()
        m.get_project_definition.return_value = json.dumps({
            'timeout': 5,
            'triggers': [
                {
                    'name': 'github',
                    'type': 'github_pr',
                    'runs': [{
                        'name': 'run0',
                    }],
                },
            ],
        })

        @contextlib.contextmanager
        def _logfd(run, mode='r'):
            path = os.path.join(jobserv.storage.base.JOBS_DIR, run.name)
            with open(path, mode) as f:
                yield f
        m.console_logfd = _logfd
        data = '''
        t1: PASSED
        t2: FAILED
        t3: foo
        '''
        rundef = {
            'test-grepping': {
                'result-pattern': '\s*(?P<name>\S+): '
                                  '(?P<result>(PASSED|FAILED|foo))',
                'fixupdict': {'foo': 'PASSED'},
            }
        }
        m.get_run_definition.return_value = json.dumps(rundef)
        storage.return_value = m
        r = Run(self.build, 'run0')
        r.trigger = 'github'
        r.status = BuildStatus.RUNNING
        db.session.add(r)
        db.session.commit()

        headers = [
            ('Authorization', 'Token %s' % r.api_key),
            ('X-RUN-STATUS', 'PASSED'),
        ]
        self._post(self.urlbase + 'run0/', data, headers, 200)
        tests = [(x.name, x.status.name) for x in Test.query.all()]
        self.assertEqual([('default', 'FAILED')], tests)
        results = [(x.name, x.status.name) for x in TestResult.query.all()]
        expected = [('t1', 'PASSED'), ('t2', 'FAILED'), ('t3', 'PASSED')]
        self.assertEqual(expected, results)

    @patch('jobserv.api.run.Storage')
    def test_run_complete_tests(self, storage):
        # Same as test_run_complete_tests_default but with a test name pattern
        m = Mock()
        m.get_project_definition.return_value = json.dumps({
            'timeout': 5,
            'triggers': [
                {
                    'name': 'github',
                    'type': 'github_pr',
                    'runs': [{
                        'name': 'run0',
                    }],
                },
            ],
        })

        @contextlib.contextmanager
        def _logfd(run, mode='r'):
            path = os.path.join(jobserv.storage.base.JOBS_DIR, run.name)
            with open(path, mode) as f:
                yield f
        m.console_logfd = _logfd
        data = """
        t1: PASSED
        t2: FAILED
        Starting Test: TNAME...
        t3: foo
        """
        rundef = {
            'test-grepping': {
                'test-pattern': '.*Starting Test: (?P<name>\S+)...',
                'result-pattern': '\s*(?P<name>\S+): '
                                  '(?P<result>(PASSED|FAILED|foo))',
                'fixupdict': {'foo': 'PASSED'},
            }
        }
        m.get_run_definition.return_value = json.dumps(rundef)
        storage.return_value = m
        r = Run(self.build, 'run0')
        r.trigger = 'github'
        r.status = BuildStatus.RUNNING
        db.session.add(r)
        db.session.commit()

        headers = [
            ('Authorization', 'Token %s' % r.api_key),
            ('X-RUN-STATUS', 'PASSED'),
        ]
        self._post(self.urlbase + 'run0/', data, headers, 200)
        tests = [(x.name, x.status.name) for x in Test.query.all()]
        self.assertEqual([('default', 'FAILED'), ('TNAME', 'PASSED')], tests)
        results = [(x.name, x.status.name)
                   for x in Test.query.all()[0].results]
        expected = [('t1', 'PASSED'), ('t2', 'FAILED')]
        self.assertEqual(expected, results)

    @patch('jobserv.api.run.Storage')
    def test_build_complete_lava_tests(self, storage):
        m = Mock()
        m.get_project_definition.return_value = json.dumps({
            'timeout': 5,
            'triggers': [
                {
                    'name': 'github',
                    'type': 'github_pr',
                    'runs': [{
                        'name': 'run0',
                    }],
                },
            ],
        })
        m.console_logfd.return_value = open('/dev/null', 'w')
        m.get_run_definition.return_value = json.dumps({})
        storage.return_value = m
        r = Run(self.build, 'run0')
        r.trigger = 'github'
        r.status = BuildStatus.RUNNING
        db.session.add(r)
        db.session.commit()

        db.session.add(Test(r, 'test-1', 'ctx', BuildStatus.QUEUED))
        db.session.commit()

        headers = [
            ('Authorization', 'Token %s' % r.api_key),
            ('X-RUN-STATUS', 'PASSED'),
        ]
        self._post(self.urlbase + 'run0/', None, headers, 200)

        db.session.refresh(r)
        self.assertEqual(BuildStatus.RUNNING, r.status)

    @patch('jobserv.api.run.Storage')
    @patch('jobserv.api.run.notify_build_complete_email')
    @patch('jobserv.notify.requests')
    def test_build_complete(self, webhook, build_complete_email, storage):
        m = Mock()
        m.get_project_definition.return_value = json.dumps({
            'timeout': 5,
            'triggers': [
                {
                    'name': 'github',
                    'type': 'github_pr',
                    'runs': [{
                        'name': 'run0',
                    }],
                    'email': {
                        'users': 'f@f.com',
                    },
                    'webhooks': [
                        {
                            'url': 'https://example.com',
                            'secret_name': 'example_secret',
                        },
                    ],
                },
            ],
        })
        m.console_logfd.return_value = open('/dev/null', 'w')
        m.get_run_definition.return_value = json.dumps(
            {'secrets': {'example_secret': 'secret_value'}})
        storage.return_value = m
        r = Run(self.build, 'run0')
        r.trigger = 'github'
        r.status = BuildStatus.RUNNING
        db.session.add(r)
        db.session.commit()

        headers = [
            ('Authorization', 'Token %s' % r.api_key),
            ('X-RUN-STATUS', 'PASSED'),
        ]
        self._post(self.urlbase + 'run0/', None, headers, 200)

        db.session.refresh(r)
        build_complete_email.assert_called_with(self.build, 'f@f.com')

        # dumb but its notified from a thread
        sleep(0.1)
        webhook.post.assert_called_once()
        call = webhook.post.call_args_list[0]
        self.assertEqual('https://example.com', call[0][0])
        sig = call[1]['headers']['X-JobServ-Sig']
        data = call[1]['data']

        self.assertEqual('sha256:', sig[:7])
        computed = hmac.new(
            b'secret_value', data.encode(), 'sha256').hexdigest()
        self.assertTrue(hmac.compare_digest(computed, sig[7:]))

    @patch('jobserv.api.run.Storage')
    @patch('jobserv.api.run.notify_build_complete_email')
    def test_build_complete_email_skip(self, build_complete, storage):
        m = Mock()
        m.get_project_definition.return_value = json.dumps({
            'timeout': 5,
            'triggers': [
                {
                    'name': 'github',
                    'type': 'github_pr',
                    'runs': [{
                        'name': 'run0',
                    }],
                    'email': {
                        'users': 'f@f.com',
                        'only_failures': True,
                    }
                },
            ],
        })
        m.console_logfd.return_value = open('/dev/null', 'w')
        m.get_run_definition.return_value = json.dumps({})
        storage.return_value = m
        r = Run(self.build, 'run0')
        r.trigger = 'github'
        r.status = BuildStatus.RUNNING
        db.session.add(r)
        db.session.commit()

        headers = [
            ('Authorization', 'Token %s' % r.api_key),
            ('X-RUN-STATUS', 'PASSED'),
        ]
        self._post(self.urlbase + 'run0/', None, headers, 200)

        db.session.refresh(r)
        self.assertEqual([], build_complete.call_args_list)

        # now fail the run and make sure we get notified
        r.status = BuildStatus.RUNNING
        db.session.commit()
        headers = [
            ('Authorization', 'Token %s' % r.api_key),
            ('X-RUN-STATUS', 'FAILED'),
        ]
        self._post(self.urlbase + 'run0/', None, headers, 200)

        db.session.refresh(r)
        self.assertEqual(self.build, build_complete.call_args_list[0][0][0])

    @patch('jobserv.api.run.notify_build_complete_email')
    @patch('jobserv.api.run.Storage')
    def test_build_complete_triggers(self, storage, notify):
        class MockStorage:
            projdef = {
                'timeout': 5,
                'triggers': [
                    {
                        'name': 'github',
                        'type': 'github_pr',
                        'email': {
                            'users': 'help@aol.com',
                        },
                        'runs': [{
                            'name': 'run0',
                            'host-tag': 'foo*',
                        }],
                        'triggers': [
                            {'name': 'build-trigger'},
                        ]
                    },
                    {
                        'name': 'build-trigger',
                        'type': 'simple',
                        'runs': [{
                            'name': 'test',
                            'host-tag': 'foo*',
                            'container': 'container-foo',
                            'script': 'test',
                        }],
                    },
                ],
                'scripts': {
                    'test': '#test#',
                }
            }

            @classmethod
            def get_projdef(cls, build):
                return yaml.dump(cls. projdef)

            @classmethod
            def create_projdef(cls, build, projdef):
                cls.projdef = yaml.safe_load(projdef)

        m = Mock()
        m.get_project_definition = MockStorage.get_projdef
        m.create_project_definition = MockStorage.create_projdef

        m.console_logfd.return_value = open('/dev/null', 'w')
        m.get_run_definition.return_value = json.dumps({})
        m.get_build_params.return_value = {'buildparam': '42'}
        storage.return_value = m
        r = Run(self.build, 'run0')
        r.trigger = 'github'
        r.status = BuildStatus.RUNNING
        r.queue_priority = 8675309
        db.session.add(r)
        db.session.commit()

        headers = [
            ('Authorization', 'Token %s' % r.api_key),
            ('X-RUN-STATUS', 'PASSED'),
        ]
        self._post(self.urlbase + 'run0/', None, headers, 200)

        db.session.refresh(r)
        run = Run.query.all()[1]
        self.assertEqual('test', run.name)
        self.assertEqual('QUEUED', run.status.name)
        rundef = json.loads(m.set_run_definition.call_args[0][1])
        self.assertEqual('42', rundef['env']['buildparam'])
        self.assertEqual(8675309, run.queue_priority)

        headers[0] = ('Authorization', 'Token %s' % run.api_key)
        self._post(self.urlbase + 'test/', None, headers, 200)
        run = Run.query.all()[1]
        self.assertEqual('test', run.name)
        self.assertEqual('PASSED', run.status.name)
        self.assertEqual('PASSED', self.build.status.name)

        notify.assert_called_once_with(self.build, 'help@aol.com')
