# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

import datetime
import os
import select
import subprocess
import time


def _cmd_output(cmd, cwd=None, env=None):
    """Simple non-blocking way to stream the output of a command"""
    poller = select.poll()
    p = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        cwd=cwd,
        env=env,
    )

    fds = [p.stdout.fileno()]
    for fd in fds:
        poller.register(fd, select.POLLIN)

    timeouts = 0
    while len(fds) > 0:
        ready = poller.poll(1000)
        if len(ready) > 0:
            timeouts = 0
        else:
            timeouts += 1
            if timeouts == 300:
                # we've gone 5 minutes without output
                msg = "== %s: cmd seems hung\n" % datetime.datetime.utcnow()
                yield msg.encode()
                timeouts = 0  # give a chance to warn again in another 5
        for fd, event in ready:
            if event & select.POLLIN:
                yield os.read(fd, 1024)
            elif event & select.POLLHUP:
                poller.unregister(fd)
                fds.remove(fd)
            else:
                msg = "== %s: unexpected poll event: %r\n" % (
                    datetime.datetime.utcnow(),
                    event,
                )
                yield msg.encode()
    p.wait()
    p.stdout.close()
    if p.returncode != 0:
        raise subprocess.CalledProcessError(p.returncode, cmd)


def stream_cmd(stream_cb, cmd, cwd=None, env=None):
    last_update = 0
    last_buff = b""
    try:
        for buff in _cmd_output(cmd, cwd, env):
            now = time.time()
            # stream data every 10s or if we have a 1k of data
            if now - last_update > 10 or len(buff) >= 1024:
                if not stream_cb(last_buff + buff):
                    last_buff += buff
                else:
                    last_buff = b""
                    last_update = now
            else:
                last_buff += buff
    finally:
        if last_buff:
            if not stream_cb(last_buff):
                # Unable to stream part of command output
                raise subprocess.CalledProcessError(0, cmd)
