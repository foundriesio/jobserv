# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

import argparse
from base64 import b64encode
import importlib
import json
import os
import shutil
import sys


def main(args):
    trigger = args.rundef["trigger_type"]
    m = importlib.import_module("jobserv_runner.handlers." + trigger)
    m.handler.execute(args.worker_dir, args.runner_dir, args.rundef)


def _update_shared_volumes_mapping(worker_dir, volumes, rundef):
    """Convert rundef mappings:
      name1: /path/in/container1
      name2: /path/in/container2

    And host config shared-volumes like:
      name1: /path/on/host1
      name2: /path/on/host2

    to produce something we can mount with docker-run:
      /path/on/host1: /path/in/container1
      /path/on/host2: /path/in/container2
    """
    shared_vols = rundef.get("shared-volumes")
    if shared_vols:
        mapping = {}
        for name, container_path in shared_vols.items():
            try:
                host_path = volumes[name]
                mapping[host_path] = container_path
            except KeyError:
                volpath = os.path.join(worker_dir, "shared-volumes", name)
                print(f"Shared volume not specified for: {name}. Default to: {volpath}")
                try:
                    os.makedirs(volpath)
                except FileExistsError:
                    pass
        rundef["shared-volumes"] = mapping


def _handle_inputs(rundef_path, rundef):
    inputs = rundef.get("simulator-inputs")
    if not inputs:
        return
    for item in inputs:
        value = input(item["prompt"])
        for name, handler in item["secrets"].items():
            transform = handler.get("transform")
            if transform and transform == "BasicAuth":
                encoded = b64encode(value.encode()).decode()
                value = f"Authorization: basic {encoded}"
            elif transform:
                sys.exit(f"unknown secret transform for {item}")
            rundef["secrets"][name] = value
    del rundef["simulator-inputs"]
    with open(rundef_path, "w") as f:
        json.dump(rundef, f, indent=2)


def get_args(args=None):
    parser = argparse.ArgumentParser(description="Execute a JobServ run definition")
    parser.add_argument("-w", "--worker-dir", help="Location to store the run")
    parser.add_argument(
        "-v",
        "--shared-volume",
        action="append",
        help="""Add a shared-volume mapping for run. Can be repeated.
                Example: -v foo=/tmp/foo""",
    )
    parser.add_argument("rundef", type=argparse.FileType("r"))
    args = parser.parse_args()

    vols = {}
    for v in args.shared_volume or []:
        parts = v.split("=")
        if len(parts) != 2:
            sys.exit(f"Invalid shared-volume: {v}")
        k, v = parts
        if not os.path.isdir(v):
            sys.exit(f"Invalid shared-volume: {k}. {v} does not exist")
        vols[k] = v

    rundef_path = args.rundef.name
    args.rundef = json.load(args.rundef)
    args.rundef["simulator"] = True
    _update_shared_volumes_mapping(args.worker_dir, vols, args.rundef)
    _handle_inputs(rundef_path, args.rundef)

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
