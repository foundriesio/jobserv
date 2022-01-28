# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

import os
import hashlib

DEBUG = 1

# This key is used to encrypt ProjectTrigger.secrets values
# see https://cryptography.io/en/latest/fernet/ for generation steps
SECRETS_FERNET_KEY = os.environ.get("SECRETS_FERNET_KEY")

SQLALCHEMY_TRACK_MODIFICATIONS = False
_fmt = os.environ.get("SQLALCHEMY_DATABASE_URI_FMT")
if _fmt:
    SQLALCHEMY_DATABASE_URI = _fmt.format(
        db_user=os.environ["DB_USER"], db_pass=os.environ["DB_PASS"]
    )
else:
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "SQLALCHEMY_DATABASE_URI", "sqlite:////tmp/test.db"
    )

PERMISSIONS_MODULE = os.environ.get("PERMISSIONS_MODULE", "jobserv.permissions")

JOBS_DIR = os.environ.get("JOBS_DIR", "/data/ci_jobs")
WORKER_DIR = os.environ.get("WORKER_DIR", "/data/workers")

LOCAL_ARTIFACTS_DIR = os.environ.get("LOCAL_ARTIFACTS_DIR", "/data/artifacts")
GCE_BUCKET = os.environ.get("GCE_BUCKET")
STORAGE_BACKEND = os.environ.get("STORAGE_BACKEND", "jobserv.storage.gce_storage")
WORKER_JWTS_DIR = os.environ.get("WORKER_JWTS_DIR", "/data/worker-jwts")

# The SURGE_SUPPORT_RATIO is defined as the number of Runs in QUEUED for a
# given host_tag divided by the number of online and enlisted non-surge
# workers that can service that host_tag. If this ratio is exceeded, the
# JobServ will enter surge support mode and use surge workers for QUEUED run.
SURGE_SUPPORT_RATIO = int(os.environ.get("SURGE_SUPPORT_RATIO", "3"))

# Allow this to be deployed in a way that builds and runs can provide links
# to a custom web frontend
BUILD_URL_FMT = os.environ.get("BUILD_URL_FMT")
RUN_URL_FMT = os.environ.get("RUN_URL_FMT")
# BUILD_URL_FMT = 'https://example.com/{project}/{build}
# RUN_URL_FMT = 'https://example.com/{project}/{build}/{run}

# Allows a custom rule for project names.
# Eg - projects could be defined as user/projname with:
#   PROJECT_NAME_REGEX = '(?:\S+\/\S+^/)'
PROJECT_NAME_REGEX = os.environ.get("PROJECT_NAME_REGEX")

SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")

# Who to email when things go wrong
NOTIFICATION_EMAILS = os.environ.get("NOTIFICATION_EMAILS")

# every 90 seconds
GIT_POLLER_INTERVAL = int(os.environ.get("GIT_POLLER_INTERVAL", "90"))
GITLAB_SERVERS = [
    x.strip() for x in os.environ.get("GITLAB_SERVERS", "https://gitlab.com").split(",")
]

STATS_CLIENT_MODULE = os.environ.get(
    "STATS_CLIENT_MODULE", "jobserv.stats.carbon:CarbonClient"
)
CARBON_HOST = os.environ.get("CARBON_HOST")
if CARBON_HOST:
    parts = CARBON_HOST.split(":")
    if len(parts) == 1:
        CARBON_HOST = (CARBON_HOST, 2003)  # provide default port
    elif len(parts) == 2:
        CARBON_HOST = (parts[0], int(parts[1]))
    else:
        raise ValueError("Invalid CARBON_HOST setting: " + CARBON_HOST)

CARBON_PREFIX = os.environ.get("CARBON_PREFIX", "jobserv")
if CARBON_PREFIX and CARBON_PREFIX[-1] != ".":
    CARBON_PREFIX += "."

# Enable to let the worker monitor rotate the pings log and keep a long term
# record of all worker pings.
WORKER_ROTATE_PINGS_LOG = os.environ.get("ROTATE_PINGS_LOG", "0") != "0"

RUNNER = os.path.join(
    os.path.dirname(__file__), "../runner/dist/jobserv_runner-0.1-py3-none-any.whl"
)

SIMULATOR_SCRIPT = os.path.join(os.path.dirname(__file__), "../simulator.py")
with open(SIMULATOR_SCRIPT, "rb") as f:
    h = hashlib.md5()
    h.update(f.read())
    SIMULATOR_SCRIPT_VERSION = h.hexdigest()

WORKER_SCRIPT = os.path.join(os.path.dirname(__file__), "../jobserv_worker.py")
with open(WORKER_SCRIPT, "rb") as f:
    h = hashlib.md5()
    h.update(f.read())
    WORKER_SCRIPT_VERSION = h.hexdigest()
