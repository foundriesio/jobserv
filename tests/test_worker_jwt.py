# Copyright (C) 2022 foundries.io
# Author: Andy Doan <andy.doan@foundries.io>
import datetime
import os
from shutil import rmtree
from tempfile import mkdtemp
from unittest import TestCase

import jwt
from cryptography.hazmat.primitives.serialization import Encoding

import jobserv.worker_jwt
from jobserv.worker_jwt import _keyid, create_keypair, worker_from_jwt


def create_jwt(orgs):
    k, cert = create_keypair(orgs)
    fname = os.path.join(jobserv.worker_jwt.WORKER_JWTS_DIR, _keyid(cert))
    with open(fname, "wb") as f:
        f.write(cert.public_bytes(Encoding.PEM))
    return k, cert


class WorkerJwtTest(TestCase):
    def setUp(self):
        self.tmpdir = mkdtemp()
        self.addCleanup(rmtree, self.tmpdir)
        jobserv.worker_jwt.WORKER_JWTS_DIR = self.tmpdir
        jobserv.worker_jwt._keys.clear()

        self.pk1, self.cert1 = create_jwt([])
        self.pk2, self.cert2 = create_jwt(["org1", "org2"])

    def test_simple(self):
        # make a bad JWT to ensure we don't crash
        with open(os.path.join(self.tmpdir, "bad"), "w") as f:
            f.write("bad pubkey")

        headers = {}
        worker = {"name": "name"}
        encoded = jwt.encode(worker, self.pk1, algorithm="ES256", headers=headers)
        self.assertRaises(jwt.DecodeError, worker_from_jwt, encoded)

        headers["kid"] = _keyid(self.cert1)

        # valid JWT, but no expiriation field:
        encoded = jwt.encode(worker, self.pk1, algorithm="ES256", headers=headers)
        self.assertRaises(jwt.MissingRequiredClaimError, worker_from_jwt, encoded)

        # valid JWT, but expired
        worker["exp"] = datetime.datetime.utcnow() - datetime.timedelta(seconds=30)
        encoded = jwt.encode(worker, self.pk1, algorithm="ES256", headers=headers)
        self.assertRaises(jwt.ExpiredSignatureError, worker_from_jwt, encoded)

        # valid
        worker["exp"] = datetime.datetime.utcnow() + datetime.timedelta(seconds=30)
        encoded = jwt.encode(worker, self.pk1, algorithm="ES256", headers=headers)
        w = worker_from_jwt(encoded)
        self.assertEqual("name", w.name)

    def test_restricted(self):
        headers = {"kid": _keyid(self.cert2)}
        worker = {
            "name": "restricted",
            "exp": datetime.datetime.utcnow() + datetime.timedelta(seconds=30),
        }
        encoded = jwt.encode(worker, self.pk2, algorithm="ES256", headers=headers)
        w = worker_from_jwt(encoded)
        self.assertEqual("restricted", w.name)
        self.assertEqual(["org1", "org2"], w.allowed_tags)
