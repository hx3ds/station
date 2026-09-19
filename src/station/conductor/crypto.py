import base64
import json
import os
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from station.errors import ExternalError
from station.prototypes.boundary import ext_dict, ext_str

PUBLIC_KEY_TOKEN_PREFIX = "lcpk1:"
ENCRYPTED_TOKEN_PREFIX = "lcenc1:"

def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")

def b64url_decode(s: str) -> bytes:
    s = (s or "").strip()
    if not s:
        raise ExternalError("empty base64url")
    pad = "=" * ((4 - (len(s) % 4)) % 4)
    return base64.urlsafe_b64decode(s + pad)

def default_private_key_path(db_path: str) -> str:
    db_path = (db_path or "").strip()
    if not db_path:
        return "local_conductor_key.pem"
    abs_path = os.path.abspath(db_path)
    dir_path = os.path.dirname(abs_path)
    if not dir_path:
        return "local_conductor_key.pem"
    return os.path.join(dir_path, "local_conductor_key.pem")

def public_key_token(pub: rsa.RSAPublicKey) -> str:
    numbers = pub.public_numbers()
    n = numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, byteorder="big")
    e = numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, byteorder="big")
    jwk = {"kty": "RSA", "n": b64url_encode(n), "e": b64url_encode(e)}
    raw = json.dumps(jwk, separators=(",", ":")).encode("utf-8")
    return PUBLIC_KEY_TOKEN_PREFIX + b64url_encode(raw)

def load_private_key(path: str) -> rsa.RSAPrivateKey:
    with open(path, "rb") as f:
        raw = f.read()
    key = serialization.load_pem_private_key(raw, password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ExternalError("unsupported key type")
    return key

def write_private_key(path: str, key: rsa.RSAPrivateKey) -> None:
    raw = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    with open(path, "wb") as f:
        f.write(raw)
    os.chmod(path, 0o600)

def decrypt_if_encrypted(priv: rsa.RSAPrivateKey | None, token: str) -> tuple[str, bool]:
    token = (token or "").strip()
    if not token.startswith(ENCRYPTED_TOKEN_PREFIX):
        return token, False
    if priv is None:
        raise ExternalError("private key not configured")
    raw = token[len(ENCRYPTED_TOKEN_PREFIX) :]
    payload = ext_dict("encrypted token payload", json.loads(b64url_decode(raw).decode("utf-8")))
    if payload.get("v") != 1:
        raise ExternalError("invalid encrypted token payload")
    k = ext_str("encrypted token k", payload.get("k"), default="", strip=False)
    n = ext_str("encrypted token n", payload.get("n"), default="", strip=False)
    c = ext_str("encrypted token c", payload.get("c"), default="", strip=False)
    enc_key = b64url_decode(k)
    nonce = b64url_decode(n)
    ciphertext = b64url_decode(c)
    key = priv.decrypt(
        enc_key,
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    plain = AESGCM(key).decrypt(nonce, ciphertext, None)
    return plain.decode("utf-8"), True
