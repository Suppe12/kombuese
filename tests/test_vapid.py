"""ECDSA P-256 / ES256 / VAPID of app/vapid.py against known test vectors.

The one exception to "HTTP only" in tests/: the signature code cannot be checked through the HTTP interface (signatures
are random-looking and go to the push service), so this file imports app/vapid.py directly. vapid.py is a library of
pure functions without imports from the app, which keeps this test independent of the rest of the backend.

If the package `cryptography` happens to be installed (never a dependency of the app, e.g. in a throwaway test
container), the signatures are cross-checked with it as well; otherwise those tests are skipped.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
import vapid  # noqa: E402

# RFC 6979 appendix A.2.5: ECDSA, 256 bits (prime field), SHA-256
D = 0xC9AFA9D845BA75166B5C215767B1D6934E50C3DB36E89B127B8A622B120F6721
UX = 0x60FED4BA255A9D31C961EB74C6356D68C049B8923B61FA6CE669622E60F29FB6
UY = 0x7903FE1008B8BC99A41AE9E95628BC64F2F1B20C2D7E9F5177A3C294D4462299
VECTORS = {  # message: (k, r, s)
    b"sample": (0xA6E3C57DD01ABE90086538398355DD4C3B17AA873382B0F24D6129493D8AAD60,
                0xEFD48B2AACB6A8FD1140DD9CD45E81D69D2C877B56AAF991C34D0EA84EAF3716,
                0xF7CB1C942D657C41D436C7A1B6E29F65F3E900DBB9AFF4064DC4AB2F843ACDA8),
    b"test": (0xD16B6AE827F17175E040871A1C7EC3500192C4C92677336EC2537ACAEE0008E0,
              0xF1ABB023518351CD71D881567B1EA663ED3EFCF6C5132B354F28D3B0B7D38367,
              0x019F4113742A2B14BD25926B49C649155F267E60D3814B4C0CC84250E46F0083),
}

try:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature
except ImportError:  # the normal case: the app has no dependencies
    ec = None


class CurveTest(unittest.TestCase):
    def test_generator_on_curve(self):
        self.assertTrue(vapid.on_curve(vapid.G))

    def test_order_times_generator_is_infinity(self):
        self.assertIsNone(vapid.mul(vapid.N))
        self.assertEqual(vapid.mul(vapid.N - 1), (vapid.G[0], vapid.P - vapid.G[1]))  # (n-1)·G = -G

    def test_small_multiples_agree(self):
        two = vapid.mul(2)
        self.assertEqual(two, vapid._affine(vapid._double((*vapid.G, 1))))
        self.assertEqual(vapid.mul(3), vapid._affine(vapid._add((*two, 1), (*vapid.G, 1))))
        # 2G from SEC test data (also in many libraries' test suites)
        self.assertEqual(two, (0x7CF27B188D034F7E8A52380304B51AC3C08969E277F21B35A60B48FC47669978,
                               0x07775510DB8ED040293D9AC69F7430DBBA7DADE63CE982299E04B79D227873D1))

    def test_public_key_of_rfc6979_key(self):
        self.assertEqual(vapid.public_key(D), (UX, UY))

    def test_point_encoding(self):
        raw = vapid.encode_point((UX, UY))
        self.assertEqual(len(raw), 65)
        self.assertEqual(vapid.decode_point(raw), (UX, UY))
        with self.assertRaises(vapid.Error):
            vapid.decode_point(raw[:-1] + bytes([raw[-1] ^ 1]))  # not on the curve
        with self.assertRaises(vapid.Error):
            vapid.decode_point(b"\x02" + raw[1:33])

    def test_private_key_range(self):
        for _ in range(5):
            d = vapid.new_private_key()
            self.assertTrue(0 < d < vapid.N)
            self.assertEqual(vapid.private_from_bytes(vapid.private_to_bytes(d)), d)
        with self.assertRaises(vapid.Error):
            vapid.private_from_bytes(b"\x00" * 32)
        with self.assertRaises(vapid.Error):
            vapid.private_from_bytes(vapid.N.to_bytes(32, "big"))


class EcdsaTest(unittest.TestCase):
    def test_rfc6979_nonces(self):
        import hashlib
        for msg, (k, _, _) in VECTORS.items():
            self.assertEqual(next(vapid.rfc6979_k(D, hashlib.sha256(msg).digest())), k, msg)

    def test_rfc6979_signatures(self):
        for msg, (_, r, s) in VECTORS.items():
            self.assertEqual(vapid.sign(D, msg), (r, s), msg)

    def test_verify(self):
        pub = (UX, UY)
        for msg, (_, r, s) in VECTORS.items():
            self.assertTrue(vapid.verify(pub, msg, r, s))
            self.assertFalse(vapid.verify(pub, msg + b"!", r, s))
            self.assertFalse(vapid.verify(pub, msg, r, (s + 1) % vapid.N))
            self.assertFalse(vapid.verify(pub, msg, 0, s))
        other = vapid.public_key(vapid.new_private_key())
        self.assertFalse(vapid.verify(other, b"sample", *VECTORS[b"sample"][1:]))

    def test_random_keys_round_trip(self):
        for i in range(10):
            d = vapid.new_private_key()
            msg = f"message {i}".encode()
            self.assertTrue(vapid.verify(vapid.public_key(d), msg, *vapid.sign(d, msg)))


class JwtTest(unittest.TestCase):
    def test_audience(self):
        self.assertEqual(vapid.audience("https://web.push.apple.com/QGuQyavXutnMH"), "https://web.push.apple.com")
        self.assertEqual(vapid.audience("https://fcm.googleapis.com/fcm/send/abc:def"), "https://fcm.googleapis.com")
        self.assertEqual(vapid.audience("http://127.0.0.1:8123/push/1"), "http://127.0.0.1:8123")
        with self.assertRaises(vapid.Error):
            vapid.audience("ftp://example.org/x")

    def test_jwt_shape_and_signature(self):
        tok = vapid.jwt(D, "https://push.example.org", "mailto:kitchen@example.org", now=1_700_000_000)
        head, body, sig = tok.split(".")
        self.assertEqual(json.loads(vapid.b64u_decode(head)), {"typ": "JWT", "alg": "ES256"})
        self.assertEqual(json.loads(vapid.b64u_decode(body)),
                         {"aud": "https://push.example.org", "exp": 1_700_000_000 + 12 * 3600, "sub": "mailto:kitchen@example.org"})
        self.assertEqual(len(vapid.b64u_decode(sig)), 64)
        self.assertNotIn("=", tok)
        self.assertTrue(vapid.jwt_verify(tok, (UX, UY)))
        self.assertFalse(vapid.jwt_verify(tok[:-2] + ("A" if tok[-2] != "A" else "B") + tok[-1], (UX, UY)))
        self.assertEqual(tok, vapid.jwt(D, "https://push.example.org", "mailto:kitchen@example.org", now=1_700_000_000))  # deterministic

    def test_authorization_header(self):
        h = vapid.authorization(D, "https://push.example.org/sub/1", "https://kitchen.example.org", now=1_700_000_000)
        self.assertTrue(h.startswith("vapid t="))
        t, k = h[len("vapid t="):].split(", k=")
        self.assertEqual(vapid.decode_point(vapid.b64u_decode(k)), (UX, UY))
        self.assertTrue(vapid.jwt_verify(t, (UX, UY)))


@unittest.skipIf(ec is None, "cryptography not installed (only used to cross-check, never by the app)")
class CrossCheckTest(unittest.TestCase):
    def test_public_keys_match(self):
        for _ in range(20):
            d = vapid.new_private_key()
            nums = ec.derive_private_key(d, ec.SECP256R1()).public_key().public_numbers()
            self.assertEqual(vapid.public_key(d), (nums.x, nums.y))

    def test_our_signatures_verify_there(self):
        for i in range(20):
            d = vapid.new_private_key()
            pub = ec.derive_private_key(d, ec.SECP256R1()).public_key()
            msg = f"kombuese {i}".encode()
            pub.verify(encode_dss_signature(*vapid.sign(d, msg)), msg, ec.ECDSA(hashes.SHA256()))  # raises if wrong

    def test_their_signatures_verify_here(self):
        for i in range(20):
            key = ec.generate_private_key(ec.SECP256R1())
            msg = f"kombuese {i}".encode()
            r, s = decode_dss_signature(key.sign(msg, ec.ECDSA(hashes.SHA256())))
            nums = key.public_key().public_numbers()
            self.assertTrue(vapid.verify((nums.x, nums.y), msg, r, s))
            self.assertFalse(vapid.verify((nums.x, nums.y), msg + b"x", r, s))

    def test_jwt_verifies_there(self):
        d = vapid.new_private_key()
        tok = vapid.jwt(d, "https://push.example.org", "mailto:a@example.org")
        head, body, sig = tok.split(".")
        raw = vapid.b64u_decode(sig)
        pub = ec.derive_private_key(d, ec.SECP256R1()).public_key()
        pub.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
                   f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))


if __name__ == "__main__":
    unittest.main()
