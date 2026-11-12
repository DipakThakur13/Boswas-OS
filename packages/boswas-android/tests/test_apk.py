"""APK inspection: manifest facts, ABIs, splits, signing, compatibility, malformed input.

Run: python3 -B -m unittest discover -s tests
"""

from __future__ import annotations

import hashlib
import os
import random
import struct
import sys
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import android_fixtures as fx  # noqa: E402

from boswas_android import apk, axml, der  # noqa: E402
from boswas_android.errors import ApkError  # noqa: E402

CERT_A = b"\x30\x03\x02\x01\x01"          # opaque certificate bytes for signing-block tests without openssl
CERT_B = b"\x30\x03\x02\x01\x02"


class ManifestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = fx.tempdir(self)

    def apk(self, name="app.apk", manifest=None, abis=(), entries=None, pairs=None):
        path = os.path.join(self.tmp, name)
        signed = pairs if pairs is not None else {apk.SIG_V2: fx.v2_value([CERT_A])}
        return fx.build_apk(path, fx.manifest() if manifest is None else manifest, abis, entries, signed)

    def test_x86_64_app(self):
        doc = apk.inspect(self.apk(abis=("x86_64", "arm64-v8a")))
        self.assertEqual(doc["package"], "com.boswas.testapp")
        self.assertEqual((doc["version_name"], doc["version_code"]), ("1.2.3", 42))
        self.assertEqual(doc["label"], "Boswas Test")
        self.assertEqual((doc["min_sdk"], doc["target_sdk"]), (24, 33))
        self.assertEqual(doc["abis"], ["arm64-v8a", "x86_64"])
        self.assertTrue(doc["compatible"])
        self.assertEqual(doc["problems"], [])
        self.assertEqual(doc["split"], {"is_split": False, "name": None, "requires_splits": False})
        self.assertEqual(doc["sha256"], hashlib.sha256(Path(doc["path"]).read_bytes()).hexdigest())
        self.assertEqual(doc["size"], os.path.getsize(doc["path"]))

    def test_document_has_exactly_the_contract_keys(self):
        doc = apk.inspect(self.apk())
        self.assertEqual(set(doc), {"path", "size", "sha256", "package", "version_name", "version_code", "label",
                                    "min_sdk", "target_sdk", "abis", "split", "signing", "compatible", "problems"})
        self.assertEqual(set(doc["signing"]), {"schemes", "certificates"})

    def test_no_native_code_runs_anywhere(self):
        doc = apk.inspect(self.apk())
        self.assertEqual(doc["abis"], [])
        self.assertTrue(doc["compatible"])

    def test_x86_only_app(self):
        self.assertTrue(apk.inspect(self.apk(abis=("x86",)))["compatible"])

    def test_arm_only_app_is_refused_with_the_translation_explanation(self):
        doc = apk.inspect(self.apk(abis=("arm64-v8a", "armeabi-v7a")))
        self.assertFalse(doc["compatible"])
        self.assertEqual(len(doc["problems"]), 1)
        self.assertIn("ARM translation layer", doc["problems"][0])
        self.assertIn("does not include", doc["problems"][0])

    def test_x86_64_only_app_on_a_32_bit_runtime(self):
        doc = apk.inspect(self.apk(abis=("x86_64",)), runtime_abis=("x86",))
        self.assertFalse(doc["compatible"])
        self.assertIn("SSE4.2", doc["problems"][0])

    def test_other_architectures(self):
        doc = apk.inspect(self.apk(abis=("riscv64",)))
        self.assertFalse(doc["compatible"])
        self.assertIn("riscv64", doc["problems"][0])

    def test_split_apk_is_refused(self):
        doc = apk.inspect(self.apk(manifest=fx.manifest(split="config.xxhdpi")))
        self.assertTrue(doc["split"]["is_split"])
        self.assertEqual(doc["split"]["name"], "config.xxhdpi")
        self.assertFalse(doc["compatible"])
        self.assertIn("several APK files", doc["problems"][0])
        self.assertIn("config.xxhdpi", doc["problems"][0])

    def test_base_that_requires_splits_is_refused(self):
        doc = apk.inspect(self.apk(manifest=fx.manifest(split_required=True)))
        self.assertFalse(doc["split"]["is_split"])
        self.assertTrue(doc["split"]["requires_splits"])
        self.assertFalse(doc["compatible"])
        self.assertIn("additional split APK", doc["problems"][0])

    def test_required_split_types_count_as_requiring_splits(self):
        doc = apk.inspect(self.apk(manifest=fx.manifest(required_split_types="base__abi")))
        self.assertTrue(doc["split"]["requires_splits"])
        self.assertFalse(doc["compatible"])

    def test_label_reference_is_not_resolved(self):
        self.assertIsNone(apk.inspect(self.apk(manifest=fx.manifest(label_ref=True)))["label"])

    def test_utf8_string_pool(self):
        doc = apk.inspect(self.apk(manifest=fx.manifest(utf8=True)))
        self.assertEqual((doc["package"], doc["label"]), ("com.boswas.testapp", "Boswas Test"))

    def test_version_code_major(self):
        doc = apk.inspect(self.apk(manifest=fx.manifest(version_code=7, version_code_major=2)))
        self.assertEqual(doc["version_code"], (2 << 32) | 7)

    def test_attributes_are_found_by_resource_id_when_names_are_obfuscated(self):
        doc = apk.inspect(self.apk(manifest=fx.manifest(hide_names=True)))
        self.assertEqual((doc["version_code"], doc["version_name"], doc["min_sdk"]), (42, "1.2.3", 24))

    def test_invalid_package_name_is_not_compatible(self):
        doc = apk.inspect(self.apk(manifest=fx.manifest(package="../../etc")))
        self.assertIsNone(doc["package"])
        self.assertFalse(doc["compatible"])

    def test_unsigned_apk_is_not_compatible(self):
        doc = apk.inspect(self.apk(pairs={}))
        self.assertEqual(doc["signing"], {"schemes": [], "certificates": []})
        self.assertFalse(doc["compatible"])
        self.assertTrue(any("not signed" in p for p in doc["problems"]))


class SigningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = fx.tempdir(self)

    def test_v2_certificate_digest(self):
        path = fx.build_apk(os.path.join(self.tmp, "v2.apk"), fx.manifest(), pairs={apk.SIG_V2: fx.v2_value([CERT_A])})
        doc = apk.inspect(path)
        self.assertEqual(doc["signing"]["schemes"], ["v2"])
        self.assertEqual(doc["signing"]["certificates"], [{"sha256": hashlib.sha256(CERT_A).hexdigest()}])

    def test_v2_and_v3_certificates_are_deduplicated(self):
        path = fx.build_apk(os.path.join(self.tmp, "v23.apk"), fx.manifest(),
                            pairs={apk.SIG_V2: fx.v2_value([CERT_A]), apk.SIG_V3: fx.v3_value([CERT_A, CERT_B]),
                                   0x42726577: b"\0" * 64})        # padding block (ignored)
        doc = apk.inspect(path)
        self.assertEqual(doc["signing"]["schemes"], ["v2", "v3"])
        self.assertEqual([c["sha256"] for c in doc["signing"]["certificates"]],
                         [hashlib.sha256(CERT_A).hexdigest(), hashlib.sha256(CERT_B).hexdigest()])

    def test_v31_counts_as_v3(self):
        path = fx.build_apk(os.path.join(self.tmp, "v31.apk"), fx.manifest(), pairs={apk.SIG_V31: fx.v3_value([CERT_B])})
        self.assertEqual(apk.inspect(path)["signing"]["schemes"], ["v3"])

    def test_corrupt_signing_block_is_a_clean_error(self):
        path = fx.build_apk(os.path.join(self.tmp, "bad.apk"), fx.manifest(), pairs={apk.SIG_V2: fx.v2_value([CERT_A])})
        data = bytearray(Path(path).read_bytes())
        magic = data.find(b"APK Sig Block 42")
        struct.pack_into("<Q", data, magic - 8, 0xFFFFFFFF)          # block size larger than the file
        Path(path).write_bytes(bytes(data))
        with self.assertRaises(ApkError):
            apk.inspect(path)

    def test_truncated_signer_is_a_clean_error(self):
        path = fx.build_apk(os.path.join(self.tmp, "t.apk"), fx.manifest(), pairs={apk.SIG_V2: struct.pack("<I", 99)})
        with self.assertRaises(ApkError):
            apk.inspect(path)

    @unittest.skipUnless(fx.OPENSSL, "openssl not installed")
    def test_real_certificates_v1_and_v2(self):
        certs = fx.Certs(self.tmp)
        sf = b"Signature-Version: 1.0\r\nCreated-By: test\r\n\r\n"
        entries = {"META-INF/MANIFEST.MF": b"Manifest-Version: 1.0\r\n\r\n", "META-INF/CERT.SF": sf,
                   "META-INF/CERT.RSA": certs.pkcs7(sf)}
        path = fx.build_apk(os.path.join(self.tmp, "v1v2.apk"), fx.manifest(), entries=entries,
                            pairs={apk.SIG_V2: fx.v2_value([certs.der])})
        doc = apk.inspect(path)
        self.assertEqual(doc["signing"]["schemes"], ["v1", "v2"])
        self.assertEqual(doc["signing"]["certificates"], [{"sha256": hashlib.sha256(certs.der).hexdigest()}])
        self.assertTrue(doc["compatible"])

    @unittest.skipUnless(fx.OPENSSL, "openssl not installed")
    def test_v1_only(self):
        certs = fx.Certs(self.tmp, "V1 Signer")
        sf = b"Signature-Version: 1.0\r\n\r\n"
        entries = {"META-INF/MANIFEST.MF": b"Manifest-Version: 1.0\r\n\r\n", "META-INF/SIGNER.SF": sf,
                   "META-INF/SIGNER.RSA": certs.pkcs7(sf)}
        doc = apk.inspect(fx.build_apk(os.path.join(self.tmp, "v1.apk"), fx.manifest(), entries=entries, pairs={}))
        self.assertEqual(doc["signing"]["schemes"], ["v1"])
        self.assertEqual(doc["signing"]["certificates"][0]["sha256"], hashlib.sha256(certs.der).hexdigest())
        self.assertEqual(der.pkcs7_certificates(certs.pkcs7(sf)), [certs.der])

    def test_ber_indefinite_lengths(self):
        cert = bytes.fromhex("3003020107")
        signed_data = (b"\x30\x80" + bytes.fromhex("020101") + b"\x31\x00" + bytes.fromhex("3000")
                       + b"\xa0\x80" + cert + b"\x00\x00" + b"\x31\x00" + b"\x00\x00")
        content_info = b"\x30\x80" + b"\x06\x09" + der.SIGNED_DATA_OID + b"\xa0\x80" + signed_data + b"\x00\x00\x00\x00"
        self.assertEqual(der.pkcs7_certificates(content_info), [cert])

    def test_der_garbage(self):
        for blob in (b"", b"\x30", b"\x30\x84\xff\xff\xff\xff", b"\x02\x01\x01", b"\x30\x80\x30\x80"):
            with self.assertRaises(der.DerError):
                der.pkcs7_certificates(blob)


class MalformedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = fx.tempdir(self)

    def assert_clean_error(self, path):
        with self.assertRaises(ApkError) as ctx:
            apk.inspect(path)
        self.assertTrue(ctx.exception.message)
        self.assertNotIn("Traceback", ctx.exception.message)

    def test_not_a_zip(self):
        path = os.path.join(self.tmp, "x.apk")
        Path(path).write_bytes(os.urandom(4096))
        self.assert_clean_error(path)

    def test_too_small_and_missing_and_directory(self):
        path = os.path.join(self.tmp, "small.apk")
        Path(path).write_bytes(b"PK")
        self.assert_clean_error(path)
        self.assert_clean_error(os.path.join(self.tmp, "missing.apk"))
        self.assert_clean_error(self.tmp)

    def test_truncated_zip(self):
        path = fx.build_apk(os.path.join(self.tmp, "t.apk"), fx.manifest())
        data = Path(path).read_bytes()
        Path(path).write_bytes(data[: len(data) // 2])
        self.assert_clean_error(path)

    def test_no_manifest(self):
        self.assert_clean_error(fx.build_apk(os.path.join(self.tmp, "n.apk"), None))

    def test_manifest_not_binary_xml(self):
        self.assert_clean_error(fx.build_apk(os.path.join(self.tmp, "x.apk"), b"<manifest package='a.b'/>"))

    def test_manifest_truncated(self):
        self.assert_clean_error(fx.build_apk(os.path.join(self.tmp, "t.apk"), fx.manifest()[:120]))

    def test_root_element_must_be_manifest(self):
        data = fx.encode_axml(fx.Node("application", [fx.Attr("label", "x")]))
        self.assert_clean_error(fx.build_apk(os.path.join(self.tmp, "r.apk"), data))

    def test_zip_bomb_manifest(self):
        path = fx.build_apk(os.path.join(self.tmp, "bomb.apk"), b"\0" * (4 * 1024 * 1024))
        with self.assertRaises(ApkError) as ctx:
            apk.inspect(path)
        self.assertIn("compression ratio", ctx.exception.message)

    def test_oversized_manifest(self):
        path = fx.build_apk(os.path.join(self.tmp, "big.apk"), os.urandom(apk.MAX_MANIFEST_BYTES + 1),
                            compress=zipfile.ZIP_STORED)
        with self.assertRaises(ApkError) as ctx:
            apk.inspect(path)
        self.assertIn("too large", ctx.exception.message)

    def test_fuzzed_manifests_never_raise_anything_else(self):
        rng = random.Random(1234)
        base = bytearray(fx.manifest())
        outcomes = {"ok": 0, "error": 0}
        for i in range(300):
            data = bytearray(base)
            for _ in range(rng.randint(1, 8)):
                data[rng.randrange(len(data))] = rng.randrange(256)
            if rng.random() < 0.2:
                data = data[: rng.randrange(8, len(data))]
            path = fx.build_apk(os.path.join(self.tmp, f"f{i}.apk"), bytes(data), pairs={apk.SIG_V2: fx.v2_value([CERT_A])})
            try:
                apk.inspect(path)
                outcomes["ok"] += 1
            except ApkError:
                outcomes["error"] += 1
            os.unlink(path)
        self.assertEqual(sum(outcomes.values()), 300)

    def test_axml_direct_garbage(self):
        for blob in (b"", b"\x03\x00\x08\x00", struct.pack("<HHI", 3, 8, 8), struct.pack("<HHI", 3, 8, 16) + b"\xff" * 8):
            with self.assertRaises(axml.AxmlError):
                axml.parse(blob)

    def test_entry_limit(self):
        path = os.path.join(self.tmp, "many.apk")
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
            archive.writestr("AndroidManifest.xml", fx.manifest())
        original = apk.MAX_ENTRIES
        apk.MAX_ENTRIES = 0
        try:
            self.assert_clean_error(path)
        finally:
            apk.MAX_ENTRIES = original


if __name__ == "__main__":
    unittest.main()
