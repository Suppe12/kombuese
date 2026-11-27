"""Encryption of stored credentials, Python standard library only.

Authenticated encryption built from HMAC-SHA256: the key stream is HMAC(k_enc, nonce || counter) (HMAC as a PRF in
counter mode), integrity is HMAC(k_mac, "v1" || nonce || ciphertext) checked before decrypting (encrypt-then-MAC).

The key comes from KOMBUESE_SECRET_KEY (any long random string) or, if not set, from the file secret.key in the data
directory, created on first use. With the file, a copy of the data directory contains the key as well; set
KOMBUESE_SECRET_KEY to keep database backups unreadable on their own.
"""
import base64
import hashlib
import hmac
import os
import secrets

import config

_KEY = []


def _key():
    if not _KEY:
        env = os.environ.get("KOMBUESE_SECRET_KEY")
        if env:
            k = hashlib.sha256(env.encode()).digest()
        else:
            p = os.path.join(config.DATA, "secret.key")
            if not os.path.isfile(p):
                fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(secrets.token_bytes(32))
            with open(p, "rb") as f:
                k = f.read()
        _KEY.append(derive(k))
    return _KEY[0]


def derive(raw):
    """Encryption and MAC key from raw key bytes. backup.py uses it for a one-time transfer key."""
    return hmac.new(raw, b"kombuese enc", hashlib.sha256).digest(), hmac.new(raw, b"kombuese mac", hashlib.sha256).digest()


def _stream(k_enc, nonce, n):
    out, i = bytearray(), 0
    while len(out) < n:
        out += hmac.new(k_enc, nonce + i.to_bytes(8, "big"), hashlib.sha256).digest()
        i += 1
    return bytes(out[:n])


def encrypt(text, keys=None):
    k_enc, k_mac = keys or _key()
    nonce, data = secrets.token_bytes(16), text.encode()
    ct = bytes(a ^ b for a, b in zip(data, _stream(k_enc, nonce, len(data))))
    tag = hmac.new(k_mac, b"v1" + nonce + ct, hashlib.sha256).digest()
    return "v1:" + base64.b64encode(nonce + ct + tag).decode()


def decrypt(blob, keys=None):
    if not blob.startswith("v1:"):
        raise ValueError("unknown format")
    k_enc, k_mac = keys or _key()
    raw = base64.b64decode(blob[3:])
    nonce, ct, tag = raw[:16], raw[16:-32], raw[-32:]
    if not hmac.compare_digest(tag, hmac.new(k_mac, b"v1" + nonce + ct, hashlib.sha256).digest()):
        raise ValueError("credentials could not be decrypted (wrong key?)")
    return bytes(a ^ b for a, b in zip(ct, _stream(k_enc, nonce, len(ct)))).decode()
