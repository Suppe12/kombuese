"""VAPID for Web Push (RFC 8292), Python standard library only: ECDSA on NIST P-256 with SHA-256 (ES256).

Pure functions, no state and no imports from the app, so tests/test_vapid.py can check them against known test vectors.
Signatures use deterministic nonces (RFC 6979), so a weak random number generator cannot leak the private key.
Push messages of Kombüse carry no payload, so no message encryption (RFC 8291) is needed: the service worker fetches
the text from the app after the push arrives (push.py).

Python integers are not constant time; the private key only signs short JWTs on the server and is never used for
anything an outsider can choose, so the usual timing attacks on ECDSA do not apply in a useful way.
"""
import base64
import hashlib
import hmac
import json
import secrets
import time
import urllib.parse

# NIST P-256 (secp256r1), FIPS 186-4 D.1.2.3
P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
A = P - 3
B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
G = (0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
     0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5)


class Error(ValueError):
    pass


# ---------- curve arithmetic in Jacobian coordinates (X, Y, Z) ↔ affine (X/Z², Y/Z³); Z = 0 is the point at infinity ----------
_INF = (1, 1, 0)


def _double(p1):
    x1, y1, z1 = p1
    if not z1 or not y1:
        return _INF
    delta = z1 * z1 % P
    gamma = y1 * y1 % P
    beta = x1 * gamma % P
    alpha = 3 * (x1 - delta) * (x1 + delta) % P  # a = -3
    x3 = (alpha * alpha - 8 * beta) % P
    z3 = ((y1 + z1) ** 2 - gamma - delta) % P
    y3 = (alpha * (4 * beta - x3) - 8 * gamma * gamma) % P
    return x3, y3, z3


def _add(p1, p2):
    if not p1[2]:
        return p2
    if not p2[2]:
        return p1
    x1, y1, z1 = p1
    x2, y2, z2 = p2
    z1z1, z2z2 = z1 * z1 % P, z2 * z2 % P
    u1, u2 = x1 * z2z2 % P, x2 * z1z1 % P
    s1, s2 = y1 * z2 * z2z2 % P, y2 * z1 * z1z1 % P
    if u1 == u2:
        return _double(p1) if s1 == s2 else _INF
    h, r = (u2 - u1) % P, (s2 - s1) % P
    hh = h * h % P
    hhh = h * hh % P
    v = u1 * hh % P
    x3 = (r * r - hhh - 2 * v) % P
    y3 = (r * (v - x3) - s1 * hhh) % P
    z3 = h * z1 * z2 % P
    return x3, y3, z3


def _affine(p1):
    x, y, z = p1
    if not z:
        return None
    zi = pow(z, -1, P)
    zi2 = zi * zi % P
    return x * zi2 % P, y * zi2 * zi % P


def on_curve(pt):
    if pt is None:
        return False
    x, y = pt
    return 0 <= x < P and 0 <= y < P and (y * y - (x * x * x + A * x + B)) % P == 0


def mul(k, pt=G):
    """k · pt (Montgomery ladder: the same steps for every bit). → affine point or None (infinity)."""
    if not 0 < k < N:
        k %= N
        if not k:
            return None
    r0, r1 = _INF, (pt[0], pt[1], 1)
    for i in range(N.bit_length() - 1, -1, -1):
        if (k >> i) & 1:
            r0, r1 = _add(r0, r1), _double(r1)
        else:
            r0, r1 = _double(r0), _add(r0, r1)
    return _affine(r0)


# ---------- keys ----------

def new_private_key():
    """Random private key 1 ≤ d < n (rejection sampling from the operating system's CSPRNG)."""
    while True:
        d = int.from_bytes(secrets.token_bytes(32), "big")
        if 0 < d < N:
            return d


def public_key(d):
    if not 0 < d < N:
        raise Error("private key out of range")
    return mul(d)


def encode_point(pt):
    """Uncompressed point: 0x04 || X || Y (65 bytes), the format of applicationServerKey and the k= parameter."""
    return b"\x04" + pt[0].to_bytes(32, "big") + pt[1].to_bytes(32, "big")


def decode_point(raw):
    if len(raw) != 65 or raw[0] != 4:
        raise Error("not an uncompressed P-256 point")
    pt = int.from_bytes(raw[1:33], "big"), int.from_bytes(raw[33:], "big")
    if not on_curve(pt):
        raise Error("point is not on the curve")
    return pt


def private_to_bytes(d):
    return d.to_bytes(32, "big")


def private_from_bytes(raw):
    d = int.from_bytes(raw, "big")
    if len(raw) != 32 or not 0 < d < N:
        raise Error("not a P-256 private key")
    return d


# ---------- ECDSA with SHA-256 ----------

def _bits2int(b):
    return int.from_bytes(b, "big")  # SHA-256 output has exactly as many bits as n, nothing to shift


def rfc6979_k(d, digest):
    """Deterministic nonce for (d, H(m)) after RFC 6979 section 3.2, HMAC-SHA256, qlen = hlen = 256."""
    x = d.to_bytes(32, "big")
    h = (_bits2int(digest) % N).to_bytes(32, "big")
    v, k = b"\x01" * 32, b"\x00" * 32
    k = hmac.new(k, v + b"\x00" + x + h, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    k = hmac.new(k, v + b"\x01" + x + h, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    while True:
        v = hmac.new(k, v, hashlib.sha256).digest()
        cand = _bits2int(v)
        if 0 < cand < N:
            yield cand
        k = hmac.new(k, v + b"\x00", hashlib.sha256).digest()
        v = hmac.new(k, v, hashlib.sha256).digest()


def sign(d, msg):
    """ECDSA signature of msg (bytes) with SHA-256. → (r, s)"""
    digest = hashlib.sha256(msg).digest()
    e = _bits2int(digest) % N
    for k in rfc6979_k(d, digest):
        pt = mul(k)
        r = pt[0] % N
        if not r:
            continue
        s = pow(k, -1, N) * (e + r * d) % N
        if s:
            return r, s


def verify(pub, msg, r, s):
    if not (0 < r < N and 0 < s < N) or not on_curve(pub):
        return False
    e = _bits2int(hashlib.sha256(msg).digest()) % N
    w = pow(s, -1, N)
    pt = _affine(_add(_jac(mul(e * w % N)), _jac(mul(r * w % N, pub))))
    return pt is not None and pt[0] % N == r


def _jac(pt):
    return _INF if pt is None else (pt[0], pt[1], 1)


# ---------- JWT (ES256) and the Authorization header of RFC 8292 ----------

def b64u(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def b64u_decode(s):
    s = str(s).strip()
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def audience(endpoint):
    """Origin of the push service: scheme://host[:port] of the endpoint."""
    u = urllib.parse.urlsplit(endpoint)
    if u.scheme not in ("https", "http") or not u.hostname:
        raise Error("endpoint is not an http(s) address")
    return f"{u.scheme}://{u.netloc.rsplit('@', 1)[-1]}"


def jwt(d, aud, sub, exp=None, now=None):
    """Signed JWT {aud, exp, sub}. exp: at most 24 h ahead (RFC 8292), default 12 h."""
    now = int(time.time() if now is None else now)
    exp = int(exp if exp is not None else now + 12 * 3600)
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    body = b64u(json.dumps({"aud": aud, "exp": exp, "sub": sub}, separators=(",", ":")).encode())
    r, s = sign(d, f"{head}.{body}".encode())
    return f"{head}.{body}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


def jwt_verify(token, pub):
    """True if the ES256 signature of token fits pub (for tests and checks)."""
    try:
        head, body, sig = token.split(".")
        raw = b64u_decode(sig)
    except ValueError:
        return False
    if len(raw) != 64:
        return False
    return verify(pub, f"{head}.{body}".encode(), int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))


def authorization(d, endpoint, sub, now=None):
    """Value of the Authorization header for one push request: 'vapid t=<JWT>, k=<public key>'."""
    return f"vapid t={jwt(d, audience(endpoint), sub, now=now)}, k={b64u(encode_point(public_key(d)))}"
