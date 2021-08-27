# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

import json
import os
import shutil
import tempfile

import jobserv.storage.local_storage

from unittest import mock

from tests import JobServTest

from jobserv.models import Build, BuildStatus, Run, Project, db


class LocalStorageTest(JobServTest):
    def setUp(self):
        super().setUp()
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir)
        jobserv.storage.local_storage.LOCAL_ARTIFACTS_DIR = self.tmpdir
        self.storage = jobserv.storage.local_storage.Storage()
        self.storage.artifacts = self.tmpdir

        self.create_projects("local-1")
        self.proj = Project.query.filter_by(name="local-1").first_or_404()
        self.build = Build(self.proj, 1)
        db.session.add(self.build)
        db.session.flush()
        self.run = Run(self.build, "run1")
        self.run.status = BuildStatus.PASSED
        db.session.add(self.run)
        db.session.commit()

        self.app.register_blueprint(self.storage.blueprint)

    def test_list_empty(self):
        self.assertEqual([], list(self.storage.list_artifacts(self.run)))

    def test_list(self):
        path = self.storage._get_run_path(self.run)
        self.storage._create_from_string(os.path.join(path, "file1.txt"), "a")
        self.storage._create_from_string(os.path.join(path, "file2.txt"), "b")
        self.storage._create_from_string(os.path.join(path, "subdir/1"), "c")
        expected = ["file1.txt", "file2.txt", "subdir/1"]
        found = list(sorted(self.storage.list_artifacts(self.run)))
        self.assertEqual(expected, found)

    @mock.patch("jobserv.api.run.Storage")
    def test_download(self, storage):
        storage.return_value = self.storage
        path = self.storage._get_run_path(self.run)
        self.storage._create_from_string(os.path.join(path, "file1.txt"), "a1")
        r = self.client.get("/projects/local-1/builds/1/runs/run1/no-name")
        self.assertEqual(404, r.status_code)

        r = self.client.get("/projects/local-1/builds/1/runs/run1/file1.txt")
        self.assertEqual((200, b"a1"), (r.status_code, r.data))

    @mock.patch("jobserv.api.run.Storage")
    def test_upload(self, storage):
        self.run.status = BuildStatus.RUNNING
        db.session.commit()
        storage.return_value = self.storage

        headers = [
            ("Authorization", "Token %s" % self.run.api_key),
            ("Content-type", "application/json"),
        ]
        url = "/projects/local-1/builds/1/runs/run1/create_signed"
        uploads = json.dumps(["foo.txt", "sub/bar.bin"])
        r = self.client.post(url, data=uploads, headers=headers)
        self.assertEqual(200, r.status_code, r.data)
        urls = json.loads(r.data.decode())["data"]["urls"]

        url = urls["foo.txt"]["url"]
        headers = {"Content-type": urls["foo.txt"]["content-type"]}
        r = self.client.put(url, data=b"foo-content", headers=headers)
        self.assertEqual(200, r.status_code, r.data)

        url = urls["sub/bar.bin"]["url"]
        headers = {"Content-type": urls["sub/bar.bin"]["content-type"]}
        r = self.client.put(url, data=b"bar-content", headers=headers)
        self.assertEqual(200, r.status_code, r.data)

        p = os.path.join(self.storage._get_run_path(self.run), "foo.txt")
        self.assertEqual("foo-content", self.storage._get_as_string(p))

        p = os.path.join(self.storage._get_run_path(self.run), "sub/bar.bin")
        self.assertEqual("bar-content", self.storage._get_as_string(p))

        self.run.status = BuildStatus.PASSED
        db.session.commit()
        r = self.client.get("/projects/local-1/builds/1/runs/run1/foo.txt")
        self.assertEqual((200, b"foo-content"), (r.status_code, r.data))
