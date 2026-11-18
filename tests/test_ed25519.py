"""Testy podpisů Ed25519 na testovacích vektorech z RFC 8032, oddíl 7.1."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ed25519  # noqa: E402

VECTORS = [  # (soukromý klíč, veřejný klíč, zpráva, podpis)
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
     "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    ("c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
     "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
     "af82",
     "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
]


class Ed25519Test(unittest.TestCase):
    def test_rfc8032_vectors(self):
        for sk, pk, msg, sig in VECTORS:
            sk, pk, msg, sig = (bytes.fromhex(x) for x in (sk, pk, msg, sig))
            with self.subTest(pk=pk.hex()):
                self.assertEqual(ed25519.public_key(sk), pk)
                self.assertEqual(ed25519.sign(sk, msg), sig)
                self.assertTrue(ed25519.verify(pk, msg, sig))

    def test_rejects_tampering(self):
        sk, pk, _, _ = (bytes.fromhex(x) for x in VECTORS[1])
        sig = ed25519.sign(sk, b"version 1.2.3")
        self.assertFalse(ed25519.verify(pk, b"version 9.9.9", sig))
        bad = bytearray(sig)
        bad[5] ^= 1
        self.assertFalse(ed25519.verify(pk, b"version 1.2.3", bytes(bad)))
        other = ed25519.public_key(bytes(32))
        self.assertFalse(ed25519.verify(other, b"version 1.2.3", sig))
        self.assertFalse(ed25519.verify(pk, b"version 1.2.3", sig[:63]))
        self.assertFalse(ed25519.verify(b"\xff" * 32, b"version 1.2.3", sig))
        # s >= q (podpis s „přetečeným“ s se musí odmítnout, jinak by šel podpis pozměnit)
        s = int.from_bytes(sig[32:], "little") + ed25519.q
        self.assertFalse(ed25519.verify(pk, b"version 1.2.3", sig[:32] + s.to_bytes(32, "little")))


if __name__ == "__main__":
    unittest.main()
