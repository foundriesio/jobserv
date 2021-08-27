# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

import json

from cryptography.fernet import Fernet
from flask_testing import TestCase

from jobserv import permissions, settings
from jobserv.jsend import _status_str
from jobserv.models import db, Project, ProjectTrigger
from jobserv.flask import create_app
from jobserv.storage import local_storage


class JobServTest(TestCase):
    def create_app(self):
        settings.TESTING = True
        settings.PRESERVE_CONTEXT_ON_EXCEPTION = False
        ProjectTrigger.fernet = Fernet(Fernet.generate_key())
        permissions.INTERNAL_API_KEY = "just for testing".encode()
        local_storage.SIGNING_KEY = permissions.INTERNAL_API_KEY
        return create_app(settings)

    def setUp(self):
        super().setUp()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()

    def create_projects(self, *names):
        for n in names:
            db.session.add(Project(n))
        db.session.commit()

    def get_json(self, url, status_code=200, query_string=None, headers=None):
        resp = self.client.get(url, query_string=query_string, headers=headers)
        if status_code != resp.status_code:
            print("response text:", resp.data)
        self.assertEqual(status_code, resp.status_code, resp.data)
        data = json.loads(resp.data.decode())
        self.assertEqual(_status_str(status_code), data["status"])
        if "data" not in data:
            raise ValueError('"data" not in dictionary: %r' % data)
        return data["data"]

    def get_signed_json(self, url, status_code=200, query_string=None):
        headers = {}
        if not url.startswith("http://"):
            # signed url handling requires complete url
            url = "http://localhost" + url
        permissions._sign(url, headers, "GET")
        return self.get_json(url, status_code, query_string, headers)

    def patch_signed_json(self, url, data, status_code=200):
        headers = {}
        if not url.startswith("http://"):
            # signed url handling requires complete url
            url = "http://localhost" + url
        permissions._sign(url, headers, "PATCH")
        resp = self.client.patch(url, headers=headers, json=data)
        if status_code != resp.status_code:
            print("response text:", resp.data)
        self.assertEqual(status_code, resp.status_code, resp.data)
        data = json.loads(resp.data)
        return data["data"]
