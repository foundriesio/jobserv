import datetime
import hashlib
import logging
from pathlib import Path
from typing import Dict, NamedTuple, List, Tuple

from cryptography import x509
from cryptography.x509 import Certificate, load_pem_x509_certificate
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ec import (
    SECP256K1,
    EllipticCurvePrivateKey,
    generate_private_key,
)
from cryptography.hazmat.primitives.serialization import (
    PublicFormat,
    Encoding,
)
import jwt

from jobserv.settings import WORKER_JWTS_DIR


_keys: Dict[str, Certificate] = {}


def _keyid(cert: Certificate) -> str:
    pub = cert.public_key()
    h = hashlib.sha256()
    h.update(pub.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo))
    return h.hexdigest()


def _load(jwt_dir: str):
    _keys.clear()
    try:
        for p in Path(jwt_dir).iterdir():
            if p.is_file():
                try:
                    cert = load_pem_x509_certificate(p.read_bytes())
                    _keys[_keyid(cert)] = cert
                except ValueError as e:
                    logging.error("Unable to read: %s: %s", p.name, e)
    except FileNotFoundError:
        logging.info("No worker JWT keys defined")


def create_keypair(orgs: List[str]) -> Tuple[EllipticCurvePrivateKey, Certificate]:
    k = generate_private_key(SECP256K1())
    p = k.public_key()

    attrs = [x509.NameAttribute(NameOID.COMMON_NAME, "foundries.io")]
    for org in orgs:
        attrs.append(x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, org))
    subject = x509.Name(attrs)

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(p)
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.utcnow())
        .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=3650))
        .sign(k, hashes.SHA256())
    )
    return k, cert


class WorkerJWT(NamedTuple):
    name: str
    allowed_tags: List[str]


def worker_from_jwt(encoded: str) -> WorkerJWT:
    if not _keys:
        _load(WORKER_JWTS_DIR)

    kid = jwt.get_unverified_header(encoded).get("kid")
    if not kid:
        raise jwt.DecodeError("Token is missing required `kid` header")
    cert = _keys.get(kid)
    if not cert:
        raise jwt.InvalidKeyError("No certificate found with id " + kid)

    decoded = jwt.decode(encoded, cert.public_key(), algorithms=["ES256"])
    try:
        decoded.pop("exp")
    except KeyError:
        raise jwt.MissingRequiredClaimError("exp")

    try:
        name = decoded.pop("name")
    except KeyError:
        raise jwt.MissingRequiredClaimError("name")

    allowed = []
    ous = cert.subject.get_attributes_for_oid(NameOID.ORGANIZATIONAL_UNIT_NAME)
    if ous:
        allowed = [x.value for x in ous]
    return WorkerJWT(name, allowed)
