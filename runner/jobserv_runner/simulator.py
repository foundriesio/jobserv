# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

import argparse
import importlib
import json
import os
import shutil
import sys


def main(args):
    trigger = args.rundef["trigger_type"]
    m = importlib.import_module("jobserv_runner.handlers." + trigger)
    m.handler.execute(args.worker_dir, args.runner_dir, args.rundef)


def get_args(args=None):
    parser = argparse.ArgumentParser(description="Execute a JobServ run definition")
    parser.add_argument("-w", "--worker-dir", help="Location to store the run")
    parser.add_argument("rundef", type=argparse.FileType("r"))
    args = parser.parse_args()

    args.rundef = json.load(args.rundef)
    args.rundef["simulator"] = True

    if not os.path.isdir(args.worker_dir):
        sys.exit("worker-dir does not exist: " + args.worker_dir)
    args.runner_dir = os.path.join(args.worker_dir, "run")
    if not os.path.exists(args.runner_dir):
        os.mkdir(args.runner_dir)

    cleanups = ("archive", "repo", "script-repo", "secrets")
    for d in cleanups:
        p = os.path.join(args.runner_dir, d)
        if os.path.exists(p):
            print("Cleaning up %s from previous execution" % p)
            shutil.rmtree(p)

    return args


if __name__ == "__main__":
    main(get_args())
