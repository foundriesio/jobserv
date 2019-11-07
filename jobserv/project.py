# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

import copy
import os
import itertools
import json

from flask import url_for

from pykwalify.core import Core
from pykwalify.errors import SchemaError

from jobserv.models import TriggerTypes
from jobserv.jsend import ApiError
from jobserv.settings import RUN_URL_FMT


class ProjectDefinition(object):
    def __init__(self, data):
        self._data = data
        self._expand_run_loops()

    @property
    def timeout(self):
        return self._data['timeout']

    @property
    def scripts(self):
        return self._data.get('scripts', {})

    @property
    def script_repos(self):
        return self._data.get('script-repos', {})

    @property
    def triggers(self):
        return self._data['triggers']

    @property
    def params(self):
        return self._data.get('params', {})

    @property
    def project_email(self):
        return self._data.get('email', None)

    def _expand_run_loops(self):
        for trigger in self.triggers:
            for run in trigger['runs']:
                loop = run.get('loop-on')
                index = trigger['runs'].index(run)
                if loop:
                    names = [x['param'] for x in loop]
                    values = [x['values'] for x in loop]
                    for j, combo in enumerate(itertools.product(*values)):
                        name = '-'.join(combo)
                        r = copy.deepcopy(run)
                        r['name'] = run['name'].format(loop=name)
                        del r['loop-on']
                        params = r.setdefault('params', {})
                        trigger['runs'].insert(index + j, r)
                        for i, val in enumerate(combo):
                            if names[i] == 'host-tag':
                                # this is a special loop-on directive
                                r['host-tag'] = val
                            else:
                                params[names[i]] = val

                        for t in r.get('triggers', []):
                            t['name'] = t['name'].format(loop=name)
                            rname = t.get('run-names')
                            if rname:
                                # put name={name} incase they do:
                                #   {name}-{loop}
                                # rather than:
                                #   {{name}}-{loop}
                                t['run-names'] = rname.format(
                                    name='{name}', loop=name)
                    trigger['runs'].remove(run)

            path = 'triggers/' + trigger['name']
            for run in trigger['runs']:
                if len(run['name']) >= 80:
                    msg = 'Name of run must be less than 80 characters'
                    raise SchemaError(msg, path=path + '/runs/' + run['name'])

    def get_trigger(self, name):
        for trigger in self.triggers:
            if trigger['name'] == name:
                return trigger

    def get_run_definition(self, dbrun, run, trigger, params, secrets):
        url = url_for('api_run.run_update', proj=dbrun.build.project.name,
                      build_id=dbrun.build.build_id, run=dbrun.name,
                      _external=True)
        public = url
        if RUN_URL_FMT:
            public = RUN_URL_FMT.format(project=dbrun.build.project.name,
                                        build=dbrun.build.build_id,
                                        run=dbrun.name)
        rundef = {
            'project': dbrun.build.project.name,
            'timeout': self.timeout,
            'api_key': dbrun.api_key,
            'run_url': url,
            'frontend_url': public,
            'runner_url': url_for(
                'api_worker.runner_download', _external=True),
            'trigger_type': trigger['type'],
            'container': run['container'],
            'container-auth': run.get('container-auth'),
            'privileged': run.get('privileged', False),
            'container-user': run.get('container-user'),
            'container-entrypoint': run.get('container-entrypoint'),
            'env': {},
            'secrets': secrets,
            'test-grepping': run.get('test-grepping'),
            'persistent-volumes': run.get('persistent-volumes'),
            'host-tag': run.get('host-tag'),
        }

        rundef['host-tag'] = run['host-tag'].lower()
        if 'script' in run:
            rundef['script'] = self.scripts[run['script']]
        else:
            rundef['script-repo'] = self.script_repos[
                run['script-repo']['name']].copy()
            rundef['script-repo']['path'] = run['script-repo']['path']
            token = rundef['script-repo'].get('token')
            for token in (token or '').split(':'):
                val = secrets.get(token)
                if token and not val:
                    err = 'The script-repo requires a token(%s) not defined ' \
                          'in the run\'s secrets.\n' % token
                    err += 'Secret keys sent to build: %r' % secrets.keys()
                    raise ApiError(400, [err])
        if rundef['container-auth']:
            if rundef['container-auth'] not in secrets:
                err = ('"container-auth" requires a secret(%s) not '
                       'defined in the run\'s secrets.\n'
                       % rundef['container-auth'])
                err += 'Secret keys sent to build: %r' % secrets.keys()
                raise ApiError(400, [err])

        # first set project-level params
        for k, v in self.params.items():
            rundef['env'][k] = str(v)
        # second set trigger-level params
        for k, v in trigger.get('params', {}).items():
            rundef['env'][k] = str(v)
        # now set params based on the run entry
        for k, v in run.get('params', {}).items():
            rundef['env'][k] = str(v)
        # finally set params based on what was passed by the trigger
        for k, v in (params or {}).items():
            rundef['env'][k] = str(v)
        rundef['env']['H_PROJECT'] = dbrun.build.project.name
        rundef['env']['H_BUILD'] = str(dbrun.build.build_id)
        rundef['env']['H_RUN'] = dbrun.name
        dbrun.host_tag = rundef['host-tag']
        return json.dumps(rundef, indent=2)

    @classmethod
    def _check_trigger_depth(clazz, proj_data, parent, trigger, depth):
        if depth == 0:
            path = 'triggers/' + trigger['name']
            raise SchemaError('Trigger recursion depth exceeded', path=path)

        for t in proj_data['triggers']:
            if t['name'] == trigger['name']:
                break
        for run in t['runs']:
            for child in run.get('triggers', []):
                clazz._check_trigger_depth(proj_data, parent, child, depth - 1)
        for child in t.get('triggers', []):
            clazz._check_trigger_depth(proj_data, parent, child, depth - 1)

    @classmethod
    def _test_recursive_triggers(clazz, proj_data):
        for parent in proj_data['triggers']:
            for run in parent['runs']:
                for trigger in run.get('triggers', []):
                    clazz._check_trigger_depth(proj_data, parent, trigger, 2)
            for trigger in parent.get('triggers', []):
                clazz._check_trigger_depth(proj_data, parent, trigger, 2)

    @classmethod
    def validate_data(clazz, data):
        schema = os.path.join(os.path.dirname(__file__), 'project-schema.yml')
        c = Core(source_data=data, schema_files=[schema])
        c.validate()

        # now do some extra validation around triggers
        scripts = data.get('scripts', {}).keys()
        repos = data.get('script-repos', {}).keys()
        for trigger in data['triggers']:
            path = 'triggers/' + trigger['name']
            try:
                TriggerTypes[trigger['type']]
            except KeyError:
                msg = 'No such runner: ' + trigger['type']
                raise SchemaError(msg, path=path)
            for run in trigger['runs']:
                script = run.get('script')
                repo = run.get('script-repo', {}).get('name')
                if script and repo:
                    msg = '"script" and "script-repo" are mutually exclusive'
                    raise SchemaError(msg, path=path + '/runs/' + run['name'])
                elif script and script not in scripts:
                    msg = 'Script does not exist: ' + script
                    raise SchemaError(msg, path=path + '/runs/' + run['name'])
                elif repo and repo not in repos:
                    msg = 'Script repo does not exist: ' + repo
                    raise SchemaError(msg, path=path + '/runs/' + run['name'])
                elif not script and not repo:
                    msg = '"script" or "script-repo" is required'
                    raise SchemaError(msg, path=path + '/runs/' + run['name'])

                if 'host-tag' not in run:
                    msg = '"host-tag" or loop-on host-tag parameter required'
                    params = run.get('loop-on', [])
                    for item in params:
                        if item['param'] == 'host-tag':
                            break
                    else:
                        raise SchemaError(
                            msg, path=path + '/runs/' + run['name'])
        clazz._test_recursive_triggers(data)
        inst = clazz(data)
        inst._expand_run_loops()
        return inst


if __name__ == '__main__':
    import sys
    import yaml
    print('# Reading project-defintion.yml from stdin')
    data = yaml.safe_load(sys.stdin)
    print(yaml.dump(ProjectDefinition(data)._data, default_flow_style=False))
