# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

import socket
import time

from jobserv.settings import CARBON_HOST, CARBON_PREFIX


class CarbonClient(object):
    def __init__(self):
        self._sock = None
        if CARBON_HOST:
            self.send = self._real_send
        else:
            self.send = self._mock_send

    def __enter__(self):
        if CARBON_HOST:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.connect(CARBON_HOST)
        return self

    def __exit__(self, *args):
        if self._sock:
            self._sock.close()

    def _mock_send(self, metric, value, timestamp=None):
        pass

    def _real_send(self, metric, value, timestamp=None):
        if timestamp is None:
            timestamp = time.time()
        buff = "%s%s %f %d\n" % (CARBON_PREFIX, metric, value, timestamp)
        buff = buff.encode()
        msglen = len(buff)
        total = 0
        while total < msglen:
            sent = self._sock.send(buff[total:])
            if sent == 0:
                raise RuntimeError("socket connection broken")
            total += sent

    def queued_runs(self, depth):
        """Track the number of queued runs"""
        self.send("queued_runs", depth)

    def worker_ping(self, worker, timestamp, metrics):
        """Track a list of metrics for a worker"""
        for k, v in metrics.items():
            try:
                v = int(v[0])
            except ValueError:
                v = float(v[0])
            self.send("workers.%s.%s" % (worker.name, k), v, timestamp)

    def worker_offline(self, worker):
        """Mark a worker as offline"""
        self.send("workers.%s.online" % worker.name, 0)

    def worker_online(self, worker):
        """Mark a worker as offline"""
        self.send("workers.%s.online" % worker.name, 1)

    def surge_started(self, tag):
        """Track when a surge has started for a given host-tag"""
        self.send("workers.surge.%s" % tag, 1)

    def surge_ended(self, tag):
        """Track when a surge has ended for a given host-tag"""
        self.send("workers.surge.%s" % tag, 0)
