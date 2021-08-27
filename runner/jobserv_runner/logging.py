# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

import datetime
import sys
import traceback

from io import StringIO

from jobserv_runner.jobserv import RunCancelledError


class ContextLogger:
    """A simplistic logger that can be used to stream things to the local
    console, but also capture the logging buffer to be buffered via a
    context manager and then uploaded somewhere else. It also tries to make
    the logs easier to read by performing indentation for the context. eg:

    == 2017-07-21 20:44:02.521839: Entering Context $foo
       2017-07-21 20:44:02.521888  INFO hello info
       2017-07-21 20:44:02.521914  WARN blah
       2017-07-21 20:44:02.522111  ERROR Traceback (most recent call last):
       |  File "./logging.py", line 61, in <module>
       |    foo()
       |  File "./logging.py", line 56, in foo
       |    bar()
       |  File "./logging.py", line 53, in bar
       |    raise ValueError('blah')
       |ValueError: blah
       |
    """

    def __init__(self, context):
        self.io = StringIO()
        self.context = context

    def _now(self):
        return datetime.datetime.utcnow()

    def __enter__(self):
        self._write("== %s: %s\n" % (self._now(), self.context))
        return self

    def __exit__(self, type, value, tb):
        if type != RunCancelledError and tb:
            msg = "".join(traceback.format_exception(type, value, tb))
            self.error(msg.replace("\n", "\n   |"))

    def _write(self, msg):
        sys.stderr.write(msg)
        self.io.write(msg)

    def _log(self, level, msg, *args):
        pre = "   %s: %-5s " % (self._now(), level)
        self._write(pre + msg % args + "\n")

    def info(self, msg, *args):
        self._log("INFO", msg, *args)

    def warn(self, msg, *args):
        self._log("WARN", msg, *args)

    def error(self, msg, *args):
        self._log("ERROR", msg, *args)
