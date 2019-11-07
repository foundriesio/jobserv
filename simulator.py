#!/usr/bin/env python3
# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

import argparse
import hashlib
import json
import os
import re
import sys

from urllib.parse import urlparse, quote_plus

import requests
import yaml


def _validate(args):
    url = args.jobserv
    if url[-1] != '/':
        url += '/'
    url += 'simulator-validate'
    data = yaml.safe_load(args.proj_def)
    r = requests.post(url, json=data)
    if r.status_code != 200:
        try:
            sys.exit(r.json()['message'])
        except Exception:
            sys.exit(r.text)
    return data


def _get_trigger(projdef, trigger_name):
    for trigger in projdef.get('triggers', []):
        if trigger.get('name') == trigger_name:
            return trigger
    sys.exit('No trigger named %s was found' % trigger_name)


def _get_run(trigger, run_name):
    for r in trigger.get('runs', []):
        if r.get('name') == run_name:
            return r
        if 'loop-on' in r:
            pat = r['name'].replace('{loop}', '')
            if pat in run_name:
                print('found looping match')
                return r
    sys.exit('No run named %s was found in the trigger' % run_name)


def _get_script(projdef, script_name):
    s = projdef.get('scripts', {}).get(script_name)
    if not s:
        sys.exit('No script named %s was found' % script_name)
    return s


def _get_script_repo(projdef, script):
    s = projdef.get('script-repos', {}).get(script['name'])
    if not s:
        sys.exit('No script-repo named %s was found' % script['name'])
    s['path'] = script['path']
    return s


def _add_pr_params(params, secrets):
    headers = {
        'Content-Type': 'application/json',
        'Authorization': 'token ' + secrets['githubtok'],
    }
    url = 'https://api.github.com/repos/%s/%s/pulls/%s' % (
        params['GH_OWNER'], params['GH_REPO'], params['GH_PRNUM'])
    r = requests.get(url, headers=headers)
    if r.status_code != 200:
        sys.exit('Unable to get PR info: %s: %d\n%s' % (
            url, r.status_code, r.text))
    data = r.json()
    params['GH_STATUS_URL'] = data['statuses_url']
    params['GH_TARGET_REPO'] = data['base']['repo']['clone_url']
    params['GIT_URL'] = data['head']['repo']['clone_url']
    params['GIT_SHA_BASE'] = data['base']['sha']
    params['GIT_SHA'] = data['head']['sha']


def _add_mr_params(params, secrets):
    headers = {
        'Content-Type': 'application/json',
        'PRIVATE-TOKEN': secrets['gitlabtok'],
    }
    p = urlparse(params['GL_MR'])
    proj = quote_plus(p.path[1:p.path.find('/merge_requests/')])
    url = p.scheme + '://' + p.netloc + '/api/v4/projects/' + \
        proj + p.path[p.path.find('/merge_requests/'):]
    r = requests.get(url, headers=headers)
    if r.status_code != 200:
        sys.exit('Unable to get MR info: %s: %d\n%s' % (
            url, r.status_code, r.text))
    data = r.json()
    params['GIT_SHA'] = data['sha']
    params['GIT_SHA_BASE'] = data['diff_refs']['start_sha']

    url = p.scheme + '://' + p.netloc + '/api/v4/projects/' + \
        str(data['source_project_id'])

    r = requests.get(url, headers=headers)
    if r.status_code != 200:
        sys.exit('Unable to get MR info: %s: %d\n%s' % (
            url, r.status_code, r.text))
    params['GIT_URL'] = r.json()['http_url_to_repo']


def _fill_params(projdef, trigger, run, params, secrets):
    params['H_RUN'] = 'simulator'
    params['H_BUILD'] = '42'

    params.update(projdef.get('params', {}))
    params.update(trigger.get('params', {}))
    params.update(run.get('params', {}))

    if trigger['type'] == 'github_pr':
        _add_pr_params(params, secrets)
    elif trigger['type'] == 'gitlab_mr':
        _add_mr_params(params, secrets)


def _get_params(keyvals):
    params = {}
    for kv in keyvals:
        k, v = kv.split('=', 1)
        params[k] = v
    return params


def _get_secrets(keyvals):
    if keyvals is None:
        keyvals = []
    secrets = {}
    for kv in keyvals:
        k, v = kv.split('=', 1)
        secrets[k] = v
    return secrets


def _create_workspace(args, proj_def, trigger, run, params, secrets):
    rundef = {
        'simulator': True,
        'trigger_type': trigger['type'],
        'run_url': '',
        'api_key': '',
        'container': run['container'],
        'env': params,
        'timeout': proj_def['timeout'],
        'secrets': secrets,
    }

    script = run.get('script')
    if script:
        rundef['script'] = proj_def['scripts'][script]
    else:
        name = run['script-repo']['name']
        rundef['script-repo'] = {
            'clone-url': proj_def['script-repos'][name]['clone-url'],
            'path': run['script-repo']['path'],
        }
        token = proj_def['script-repos'][name].get('token')
        if token:
            rundef['script-repo']['token'] = token
        ref = proj_def['script-repos'][name].get('git-ref')
        if ref:
            rundef['script-repo']['git-ref'] = ref

    rundef_file = os.path.join(args.workspace, 'rundef.json')
    with open(rundef_file, 'w') as f:
        json.dump(rundef, f, indent=2)

    print('Downloading runner for simulator')
    wheel = os.path.join(args.workspace, 'runner.whl')
    with open(wheel, 'wb') as f:
        url = args.jobserv + '/runner'
        r = requests.get(url)
        if r.status_code != 200:
            sys.exit('Unable to download %s: %d\n%s' % (
                url, r.status_code, r.text))
        for chunk in r:
            f.write(chunk)

    with open(os.path.join(args.workspace, 'run_simulator'), 'w') as f:
        os.fchmod(f.fileno(), 0o755)
        f.write('#!/bin/sh -e\n')
        f.write('export PYTHONPATH=%s\n' % wheel)
        f.write('python3 -m jobserv_runner.simulator -w %s %s </dev/null' % (
            args.workspace, rundef_file))

    print('Simulator can now be run with %s run -w %s' % (
        sys.argv[0], args.workspace))


def _create(args):
    if not os.path.exists(args.workspace):
        sys.exit('Simulator workspace, %s, does not exist' % args.workspace)

    proj_def = _validate(args)
    trigger = _get_trigger(proj_def, args.trigger_name)
    run = _get_run(trigger, args.run_name)
    secrets = _get_secrets(args.secret)
    params = _get_params(args.param)
    _fill_params(proj_def, trigger, run, params, secrets)

    _create_workspace(args, proj_def, trigger, run, params, secrets)


def _run(args):
    script = os.path.join(args.workspace, 'run_simulator')
    os.execv(script, [script])


def _test_grep(args):
    with open(args.proj_def) as f:
        proj_def = yaml.safe_load(f)

    trigger = _get_trigger(proj_def, args.trigger_name)
    run = _get_run(trigger, args.run_name)
    grepping = run.get('test-grepping')
    if not grepping:
        sys.exit(' \'test-grepping\' pattern defined in run')

    test_pat = grepping.get('test-pattern')
    if test_pat:
        test_pat = re.compile(test_pat)
    res_pat = re.compile(grepping['result-pattern'])
    fixups = grepping.get('fixupdict', {})
    cur_test = None
    passes = failures = 0
    for line in sys.stdin.readlines():
        if test_pat:
            m = test_pat.match(line)
            if m:
                cur_test = m.group('name')
        m = res_pat.match(line)
        if m:
            result = m.group('result')
            result = fixups.get(result, result)
            if not cur_test:
                cur_test = 'default'
            print('Test(%s) %s = %s' % (cur_test, m.group('name'), result))
            if result == 'PASSED':
                passes += 1
            else:
                failures += 1
    print('%d PASSED, %d FAILED' % (passes, failures))


def _get_input(items, fmt_item):
    for i, item in enumerate(items):
        print('%d. %s' % (i + 1, fmt_item.format(**item)))
    try:
        selection = int(input('Enter your selection: ')) - 1
        return items[selection]
    except (ValueError, IndexError):
        sys.exit('Invalid selection')


def _wizard_loop_params(run, params):
    for item in run['loop-on']:
        print('= Pick a value for %s' % item['param'])
        values = [{'name': x} for x in item['values']]
        params[item['param']] = _get_input(values, '{name}')['name']


def _wizard_registry_auth(proj_def, run, secrets):
    name = run['script-repo']['name']
    repo = proj_def['script-repos'][name]
    token = repo.get('token')
    if token:
        print('= Script repository for run requires secrets')
        for t in token.split(':'):
            secrets[t] = input('Enter secret for "%s": ' % t).strip()


def _wizard_gitlab_mr(run, params, secrets):
    for s in ('gitlabuser', 'gitlabtok'):
        if s not in secrets:
            secrets[s] = input('Enter secret for "%s": ' % s).strip()
    params['GL_MR'] = input('Enter the URL to the gitlab mr": ').strip()


def _wizard_github_pr(run, params, secrets):
    for s in ('githubtok',):
        if s not in secrets:
            secrets[s] = input('Enter secret for "%s": ' % s).strip()

    proj = input('Enter the GitHub project (eg docker/notary): ').strip()
    owner, repo = proj.split('/')
    params['GH_OWNER'] = owner
    params['GH_REPO'] = repo
    params['GH_PRNUM'] = input('Enter the pull request number: ').strip()


def _wizard(args):
    proj_def = _validate(args)
    print('= Select the trigger')
    trigger = _get_input(proj_def['triggers'], 'Name({name}) Type({type})')

    print('= Select the run')
    run = _get_input(trigger['runs'], '{name}')

    secrets = {}
    params = {}

    if run.get('loop-on'):
        _wizard_loop_params(run, params)

    if run.get('script-repo'):
        _wizard_registry_auth(proj_def, run, secrets)

    if trigger['type'] == 'gitlab_mr':
        _wizard_gitlab_mr(run, params, secrets)
    elif trigger['type'] == 'github_pr':
        _wizard_github_pr(run, params, secrets)

    _fill_params(proj_def, trigger, run, params, secrets)

    _create_workspace(args, proj_def, trigger, run, params, secrets)


def _check_for_updates(args):
    with open(__file__, 'rb') as f:
        h = hashlib.md5()
        h.update(f.read())
        version = h.hexdigest()

    url = args.jobserv + '/simulator?version=' + version
    resp = requests.get(url)
    if resp.status_code == 200:
        print('Simulator version has changed, updating local script')
        with open(__file__, 'w') as f:
            f.write(resp.text)
    elif resp.status_code == 304:
        print('Simulator version has not changed')
    else:
        print('HTTP Error: %d\n%s' % (resp.status_code, resp.text))


def get_args(args=None):
    parser = argparse.ArgumentParser(description='''
        A tool to help build and run a "JobServ simulator" that can execute
        a Run defined in a project definition file locally without using the
        actual JobServ.''')
    cmds = parser.add_subparsers(title='Commands')

    api_url = 'https://api.foundries.io/'

    p = cmds.add_parser('validate-schema',
                        help='''Validate a project definition YAML file against
                             a running JobServ''')
    p.set_defaults(func=_validate)
    p.add_argument('--jobserv', '-j', default=api_url,
                   help='The JobServ to query. Default=%(default)s')
    p.add_argument('--proj-def', '-d', required=True,
                   type=argparse.FileType('r'),
                   help='Project defintion .yml')

    p = cmds.add_parser('wizard',
                        help='A guided tour to help create a run.')
    p.set_defaults(func=_wizard)
    p.add_argument('--jobserv', '-j', default=api_url,
                   help='The JobServ to query. Default=%(default)s')
    p.add_argument('--proj-def', '-d', required=True,
                   type=argparse.FileType('r'),
                   help='Project defintion .yml')
    p.add_argument('--workspace', '-w', required=True,
                   help='''A directory to serve as the simulator workspace. It
                        will hold the scripts needed to run the simulator and
                        and also store its artifacts''')

    p = cmds.add_parser('create',
                        help='Create a workspace for executing simulated run.')
    p.set_defaults(func=_create)
    p.add_argument('--jobserv', '-j', default=api_url,
                   help='The JobServ to query. Default=%(default)s')
    p.add_argument('--proj-def', '-d', required=True,
                   type=argparse.FileType('r'),
                   help='Project defintion .yml')
    p.add_argument('--trigger-name', '-t', required=True,
                   help='The name of the trigger the run is under')
    p.add_argument('--run-name', '-r', required=True,
                   help='The name of the run to try')
    p.add_argument('--workspace', '-w', required=True,
                   help='''A directory to serve as the simulator workspace. It
                        will hold the scripts needed to run the simulator and
                        and also store its artifacts''')
    p.add_argument('--param', '-p', metavar='KEY=VAL', action='append',
                   help='Parameter(s) needed by the run.')
    p.add_argument('--secret', '-s', metavar='KEY=VAL', action='append',
                   help='Parameter(s) needed by the run.')

    p = cmds.add_parser('run',
                        help='Run the simulator defined in the workspace.')
    p.set_defaults(func=_run)
    p.add_argument('--workspace', '-w', required=True,
                   help='The simulator workspace')

    p = cmds.add_parser('check-test-grepping',
                        help='''Parses STDIN with test-grepping rules to see
                             find out what tests it thinks pass/fail''')
    p.set_defaults(func=_test_grep)
    p.add_argument('--proj-def', '-d', required=True,
                   help='Project defintion .yml')
    p.add_argument('--trigger-name', '-t', required=True,
                   help='The name of the trigger the run is under')
    p.add_argument('--run-name', '-r', required=True,
                   help='The name of the run to try')

    p = cmds.add_parser('check-for-updates',
                        help='Check for updates to this simulator script')
    p.set_defaults(func=_check_for_updates)
    p.add_argument('--jobserv', '-j', default=api_url,
                   help='The JobServ to query. Default=%(default)s')

    args = parser.parse_args(args)
    return args


if __name__ == '__main__':
    args = get_args()
    if getattr(args, 'func', None):
        args.func(args)
