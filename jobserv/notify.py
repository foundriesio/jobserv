# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

import contextlib
import functools
import hmac
import json
import logging
import requests
import smtplib
import time
from threading import Thread
import traceback

from email.mime.text import MIMEText
from email.utils import make_msgid

from flask import url_for

from jobserv.jsend import ApiError
from jobserv.models import Build, BuildStatus
from jobserv.settings import (
    BUILD_URL_FMT,
    NOTIFICATION_EMAILS,
    RUN_URL_FMT,
    SMTP_SERVER,
    SMTP_USER,
    SMTP_PASSWORD,
)

log = logging.getLogger('jobserv.flask')


def build_url(build):
    if BUILD_URL_FMT:
        return BUILD_URL_FMT.format(
            project=build.project.name, build=build.build_id)

    return url_for('api_build.build_get',
                   proj=build.project.name, build_id=build.build_id)


def run_url(run):
    if RUN_URL_FMT:
        return RUN_URL_FMT.format(project=run.build.project.name,
                                  build=run.build.build_id, run=run.name)

    return url_for('api_run.run_get_artifact', proj=run.build.project.name,
                   build_id=run.build.build_id, run=run.name,
                   path='console.log', external=True)


@contextlib.contextmanager
def smtp_session():
    s = smtplib.SMTP(SMTP_SERVER, 587)
    rv, msg = s.starttls()
    if rv != 220:
        log.error('Unable to connect to SMTP server %s: %d %s',
                  SMTP_SERVER, rv, msg.decode())
        raise ApiError(500, 'Unable to connect to SMTP server')
    rv, msg = s.login(SMTP_USER, SMTP_PASSWORD)
    if rv != 235:
        log.error('Unable to authenticate with SMTP server %s: %d %s',
                  SMTP_SERVER, rv, msg.decode())
        raise ApiError(500, 'Unable to authenticate with SMTP server')
    try:
        yield s
    finally:
        s.quit()


def _get_build_stats(build):
    '''Look at last 20 builds to see how things have been doing'''
    complete = (BuildStatus.PASSED, BuildStatus.FAILED)
    query = Build.query.filter(
        Build.proj_id == build.proj_id,
        Build.id <= build.id,
        Build.status.in_(complete)
    ).order_by(
        Build.id.desc()
    ).limit(20)

    b_stats = {
        'passes': 0,
        'total': 0,
        'pass_fails': '',
    }
    for b in query:
        if b.status == BuildStatus.PASSED:
            b_stats['passes'] += 1
            b_stats['pass_fails'] += '+'
        else:
            b_stats['pass_fails'] += '-'
        b_stats['total'] += 1
    b_stats['pass_rate'] = int((b_stats['passes'] / b_stats['total']) * 100)
    return b_stats


def _send(message):
    last_exc = None
    for x in range(3):
        with smtp_session() as s:
            try:
                s.send_message(message)
                return
            except Exception as e:
                last_exc = e
                time.sleep(1)
    email = '|  ' + '\n|  '.join(str(message).splitlines())
    log.error('Unable to send email:\n%s\n%r', email, last_exc)


def notify_build_complete_email(build, to_list):
    subject = 'jobserv: %s build #%d : %s' % (
        build.project.name, build.build_id, build.status.name)
    body = subject + '\n'
    body += 'Build URL: %s\n\n' % build_url(build)

    body += 'Runs:\n'
    for run in build.runs:
        url = run_url(run)
        body += '  %s: %s\n    %s\n' % (run.name, run.status.name, url)
    if build.reason:
        body += '\nReason:\n' + build.reason

    stats = _get_build_stats(build)
    body += '''Build history for last {total} builds:
  pass rate: {pass_rate}%
   (newest->oldest): {pass_fails}
    '''.format(**stats)

    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From'] = SMTP_USER
    msg['To'] = to_list
    _send(msg)


def notify_build_complete_webhook(build, webhook_url, secret):
    data = json.dumps(build.as_json())
    sig = hmac.new(secret.encode(), msg=data.encode(), digestmod="sha256")
    headers = {
        'Content-type': 'application/json',
        'X-JobServ-Sig': 'sha256:' + sig.hexdigest(),
    }

    def deliver():
        for i in (1, 2, 4, 0):
            try:
                r = requests.post(webhook_url, data=data, headers=headers)
                if r.ok:
                    return
                logging.error('Unable to deliver webhook to %s: HTTP_%d - %s',
                              webhook_url, r.status_code, r.data)
            except Exception:
                logging.exception('Unable to deliver webhook')
            if i:
                logging.info('Retrying in %d seconds', i)
                time.sleep(i)

    Thread(target=deliver).start()


def notify_run_terminated(run, cutoff):
    url = run_url(run)
    msg = 'The run has been terminated after: %s\n  %s' % (cutoff, url)
    msg = MIMEText(msg)
    msg['Message-ID'] = make_msgid('jobserv-%s' % run.id)
    msg['From'] = SMTP_USER
    msg['Subject'] = 'jobserv: Terminated %s/%s/%s' % (
        run.build.project.name, run.build.build_id, run.name)

    if NOTIFICATION_EMAILS:
        msg['To'] = NOTIFICATION_EMAILS
        _send(msg)
    return msg['Message-ID']


def notify_surge_started(tag):
    msg = MIMEText('Surge workers have been enabled for: ' + tag)
    msg['Message-ID'] = make_msgid('jobserv-' + tag)
    msg['From'] = SMTP_USER
    msg['Subject'] = 'jobserv: SURGE!!! ' + tag

    if NOTIFICATION_EMAILS:
        msg['To'] = NOTIFICATION_EMAILS
        _send(msg)
    return msg['Message-ID']


def notify_surge_ended(tag, in_reply_to):
    if not NOTIFICATION_EMAILS:
        return
    msg = MIMEText('Surge workers have been disabled for: ' + tag)
    msg['To'] = NOTIFICATION_EMAILS
    msg['In-Reply-To'] = in_reply_to
    msg['From'] = SMTP_USER
    msg['Subject'] = 'jobserv: ended surge for ' + tag
    _send(msg)


def email_on_exception(*decorator_args):
    '''Allows a function to automatically send an email if it hits an
       unexpected error'''
    subject = decorator_args[0]

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            try:
                func(*args, **kwargs)
            except Exception:
                msg = traceback.format_exc()
                msg = MIMEText(msg)
                msg['To'] = NOTIFICATION_EMAILS
                msg['From'] = SMTP_USER
                msg['Subject'] = subject
                _send(msg)
                raise
        return wrapper
    return decorator
