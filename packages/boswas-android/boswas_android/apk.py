"""APK inspection: identity, version, native ABIs, split status, signing certificates, compatibility.

Reads the ZIP directory, AndroidManifest.xml (binary XML) and the signature
data; never runs anything from the APK. The result describes the file. It
does not say the file is safe: Android verifies the signatures when it
installs the APK, and a valid signature only identifies who signed it.

Document shape: docs/architecture/platform-contracts.md, section 2.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import struct
import zipfile

from . import axml, der
from .errors import ApkError

MAX_APK_BYTES = 4 * 1024 * 1024 * 1024 - 1        # Android's own limit (ZIP without ZIP64)
MAX_ENTRIES = 100_000
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_SIGNATURE_FILE_BYTES = 1024 * 1024
MAX_RATIO = 200                                    # uncompressed/compressed, beyond 1 MiB
MAX_SIGNING_BLOCK = 64 * 1024 * 1024
MAX_CERTIFICATES = 16

APK_SIG_MAGIC = b"APK Sig Block 42"
SIG_V2 = 0x7109871A
SIG_V3 = 0xF05368C0
SIG_V31 = 0x1B93AD61

# android:attr resource IDs (framework R.attr)
ATTR_LABEL = 0x01010001
ATTR_MIN_SDK = 0x0101020C
ATTR_VERSION_CODE = 0x0101021B
ATTR_VERSION_NAME = 0x0101021C
ATTR_TARGET_SDK = 0x01010270
ATTR_VERSION_CODE_MAJOR = 0x01010576
ATTR_IS_SPLIT_REQUIRED = 0x01010591

ARM_ABIS = frozenset({"armeabi", "armeabi-v7a", "arm64-v8a"})
X86_ABIS = ("x86_64", "x86")
_ABI_RE = re.compile(r"^lib/([A-Za-z0-9_-]{1,32})/[^/]+\.so$")
_V1_BLOCK_RE = re.compile(r"^META-INF/[^/]+\.(RSA|DSA|EC)$", re.IGNORECASE)
_V1_SF_RE = re.compile(r"^META-INF/[^/]+\.SF$", re.IGNORECASE)
PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$")

ARM_ONLY_PROBLEM = ("This app contains native code only for ARM processors ({abis}). It needs an ARM "
                    "translation layer, which Boswas OS does not include, so it cannot run on this computer.")
SPLIT_PROBLEM = ("This file is one part (split \"{name}\") of an app that is delivered as several APK files. "
                 "All parts must be installed together, which Boswas OS does not support; install the complete "
                 "(universal) APK of the app instead.")
SPLIT_REQUIRED_PROBLEM = ("This is the base APK of an app that needs additional split APK files to work. All parts "
                          "must be installed together, which Boswas OS does not support; install the complete "
                          "(universal) APK of the app instead.")


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int, what: str) -> bytes:
    if info.file_size > limit:
        raise ApkError(f"{what} is too large ({info.file_size} bytes).")
    if info.compress_size and info.file_size > 1024 * 1024 and info.file_size / info.compress_size > MAX_RATIO:
        raise ApkError(f"{what} has an implausible compression ratio (possible ZIP bomb).")
    with archive.open(info) as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ApkError(f"{what} is larger than its directory entry says.")
    return data


# --- APK Signing Block (schemes v2 and v3) ---------------------------------------------------------

def _length_prefixed(data: bytes, pos: int, end: int) -> tuple[bytes, int]:
    if pos + 4 > end:
        raise ApkError("signing block truncated")
    (length,) = struct.unpack_from("<I", data, pos)
    pos += 4
    if pos + length > end:
        raise ApkError("signing block entry extends past its container")
    return data[pos:pos + length], pos + length


def _sequence(blob: bytes, limit: int = 64) -> list[bytes]:
    items, pos = [], 0
    while pos < len(blob):
        if len(items) >= limit:
            raise ApkError("signing block has too many entries")
        item, pos = _length_prefixed(blob, pos, len(blob))
        items.append(item)
    return items


def _signer_certificates(signers_blob: bytes) -> list[bytes]:
    """Certificates (DER) of every signer in a v2 or v3 signature scheme block.

    v2 signer: signed data, signatures, public key; v3 signer: signed data, minSdk, maxSdk,
    signatures, public key. In both, signed data starts with digests, then certificates.
    """
    certificates = []
    signers, _ = _length_prefixed(signers_blob, 0, len(signers_blob))
    for signer in _sequence(signers):
        signed_data, _ = _length_prefixed(signer, 0, len(signer))
        _digests, pos = _length_prefixed(signed_data, 0, len(signed_data))
        certs_blob, _ = _length_prefixed(signed_data, pos, len(signed_data))
        for cert in _sequence(certs_blob, MAX_CERTIFICATES):
            certificates.append(cert)
    return certificates


def _central_directory_offset(handle, size: int) -> int | None:
    tail_len = min(size, 65535 + 22)
    handle.seek(size - tail_len)
    tail = handle.read(tail_len)
    index = tail.rfind(b"PK\x05\x06")
    while index >= 0 and len(tail) - index < 22:
        index = tail.rfind(b"PK\x05\x06", 0, index)
    if index < 0:
        return None
    (offset,) = struct.unpack_from("<I", tail, index + 16)
    return None if offset == 0xFFFFFFFF else offset


def signing_block(path: str) -> dict[int, bytes]:
    """ID -> value of the APK Signing Block's pairs ({} when the APK has no block)."""
    size = os.path.getsize(path)
    with open(path, "rb") as handle:
        cd_offset = _central_directory_offset(handle, size)
        if cd_offset is None or cd_offset < 32 or cd_offset > size:
            return {}
        handle.seek(cd_offset - 24)
        footer = handle.read(24)
        if len(footer) != 24 or footer[8:] != APK_SIG_MAGIC:
            return {}
        (block_size,) = struct.unpack_from("<Q", footer, 0)
        if block_size < 24 or block_size > MAX_SIGNING_BLOCK or block_size + 8 > cd_offset:
            raise ApkError("APK Signing Block has an invalid size")
        start = cd_offset - block_size - 8
        handle.seek(start)
        block = handle.read(block_size + 8)
    if len(block) != block_size + 8 or struct.unpack_from("<Q", block, 0)[0] != block_size:
        raise ApkError("APK Signing Block sizes do not match")
    pairs: dict[int, bytes] = {}
    pos, end = 8, len(block) - 24
    while pos < end:
        if pos + 12 > end:
            raise ApkError("APK Signing Block pair truncated")
        (pair_len,) = struct.unpack_from("<Q", block, pos)
        if pair_len < 4 or pos + 8 + pair_len > end:
            raise ApkError("APK Signing Block pair has an invalid length")
        (pair_id,) = struct.unpack_from("<I", block, pos + 8)
        pairs[pair_id] = block[pos + 12:pos + 8 + pair_len]
        pos += 8 + pair_len
    return pairs


# --- manifest -------------------------------------------------------------------------------------

def _int_or_none(attribute) -> int | None:
    if attribute is None:
        return None
    value = attribute.value
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def manifest_facts(data: bytes) -> dict:
    elements = axml.parse(data)
    root = elements[0]
    if root.name != "manifest":
        raise ApkError("AndroidManifest.xml does not start with a <manifest> element.")
    facts = {"package": None, "version_name": None, "version_code": None, "label": None,
             "min_sdk": None, "target_sdk": None, "split_name": None, "requires_splits": False}
    package = root.attr("package", namespace=None)
    if package is not None and isinstance(package.value, str) and PACKAGE_RE.match(package.value):
        facts["package"] = package.value
    code = _int_or_none(root.attr("versionCode", ATTR_VERSION_CODE))
    major = _int_or_none(root.attr("versionCodeMajor", ATTR_VERSION_CODE_MAJOR))
    if code is not None:
        facts["version_code"] = ((major or 0) << 32) | (code & 0xFFFFFFFF)
    name = root.attr("versionName", ATTR_VERSION_NAME)
    if name is not None and isinstance(name.value, str):
        facts["version_name"] = name.value[:200]
    split = root.attr("split", namespace=None)
    if split is not None and isinstance(split.value, str) and split.value:
        facts["split_name"] = split.value[:200]
    required_types = root.attr("requiredSplitTypes")
    if required_types is not None and isinstance(required_types.value, str) and required_types.value:
        facts["requires_splits"] = True
    for element in elements[1:]:
        if element.path == ("manifest", "uses-sdk"):
            facts["min_sdk"] = _int_or_none(element.attr("minSdkVersion", ATTR_MIN_SDK))
            facts["target_sdk"] = _int_or_none(element.attr("targetSdkVersion", ATTR_TARGET_SDK))
        elif element.path == ("manifest", "application"):
            label = element.attr("label", ATTR_LABEL)
            if label is not None and label.type == axml.TYPE_STRING and isinstance(label.value, str):
                facts["label"] = label.value[:200]
            required = element.attr("isSplitRequired", ATTR_IS_SPLIT_REQUIRED)
            if required is not None and required.value is True:
                facts["requires_splits"] = True
    return facts


# --- compatibility --------------------------------------------------------------------------------

def compatibility(abis: list[str], split: dict, runtime_abis: tuple[str, ...]) -> tuple[bool, list[str]]:
    problems = []
    if split.get("is_split"):
        problems.append(SPLIT_PROBLEM.format(name=split.get("name") or "?"))
    elif split.get("requires_splits"):
        problems.append(SPLIT_REQUIRED_PROBLEM)
    if abis and not any(abi in runtime_abis for abi in abis):
        if all(abi in ARM_ABIS for abi in abis):
            problems.append(ARM_ONLY_PROBLEM.format(abis=", ".join(abis)))
        elif "x86_64" in abis and "x86_64" not in runtime_abis:
            problems.append("This app contains native code only for x86_64, but this computer runs the 32-bit "
                            "x86 Android image (its processor lacks SSE4.2), so the app cannot run here.")
        else:
            problems.append(f"This app contains native code only for {', '.join(abis)}, which the Android runtime "
                            f"on this computer ({', '.join(runtime_abis)}) cannot run.")
    return not problems, problems


# --- inspection -----------------------------------------------------------------------------------

def inspect(path: str, runtime_abis: tuple[str, ...] = X86_ABIS) -> dict:
    """The APK document for path. Raises ApkError (a clean message) for anything that is not a readable APK."""
    try:
        return _inspect(path, runtime_abis)
    except ApkError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile):
        raise ApkError("Not an APK: the file is not a valid ZIP archive.") from None
    except (axml.AxmlError, der.DerError) as exc:
        raise ApkError(f"Not a readable APK: {exc}.") from None
    except (struct.error, IndexError, ValueError, OverflowError, EOFError, NotImplementedError, RuntimeError) as exc:
        raise ApkError(f"Not a readable APK ({type(exc).__name__}: {str(exc)[:200]}).") from None
    except OSError as exc:
        raise ApkError(f"The file cannot be read ({exc.strerror or exc}).") from None


def _inspect(path: str, runtime_abis: tuple[str, ...]) -> dict:
    try:
        st = os.stat(path)
    except OSError as exc:
        raise ApkError(f"The file cannot be read ({exc.strerror or exc}).") from None
    if not stat.S_ISREG(st.st_mode):
        raise ApkError("Not an APK: not a regular file.")
    if st.st_size < 22:
        raise ApkError("Not an APK: the file is too small to be a ZIP archive.")
    if st.st_size > MAX_APK_BYTES:
        raise ApkError("The file is larger than Android accepts for an APK (4 GiB).")
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_ENTRIES:
            raise ApkError(f"Not a plausible APK: {len(infos)} entries.")
        names = {info.filename: info for info in infos}
        manifest_info = names.get("AndroidManifest.xml")
        if manifest_info is None:
            raise ApkError("Not an APK: there is no AndroidManifest.xml in the archive.")
        facts = manifest_facts(_read_member(archive, manifest_info, MAX_MANIFEST_BYTES, "AndroidManifest.xml"))
        abis = sorted({m.group(1) for name in names if (m := _ABI_RE.match(name))})
        v1_certs: list[bytes] = []
        has_sf = any(_V1_SF_RE.match(name) for name in names)
        v1_blocks = [info for name, info in names.items() if _V1_BLOCK_RE.match(name)]
        for info in v1_blocks[:4]:
            try:
                v1_certs.extend(der.pkcs7_certificates(
                    _read_member(archive, info, MAX_SIGNATURE_FILE_BYTES, info.filename)))
            except der.DerError:
                continue
    schemes, certificates = [], []
    if has_sf and v1_blocks:
        schemes.append("v1")
    pairs = signing_block(path)
    if SIG_V2 in pairs:
        schemes.append("v2")
        certificates.extend(_signer_certificates(pairs[SIG_V2]))
    if SIG_V3 in pairs or SIG_V31 in pairs:
        schemes.append("v3")
        for key in (SIG_V3, SIG_V31):
            if key in pairs:
                certificates.extend(_signer_certificates(pairs[key]))
    certificates.extend(v1_certs)
    digests = []
    for cert in certificates:
        digest = hashlib.sha256(cert).hexdigest()
        if digest not in digests:
            digests.append(digest)
    split = {"is_split": facts["split_name"] is not None, "name": facts["split_name"],
             "requires_splits": bool(facts["requires_splits"])}
    compatible, problems = compatibility(abis, split, runtime_abis)
    if facts["package"] is None:
        compatible = False
        problems.append("The manifest names no valid package, so Android would refuse it.")
    if not schemes:
        compatible = False
        problems.append("The APK is not signed. Android installs only signed APKs, and Boswas OS never signs or "
                        "modifies an APK.")
    return {
        "path": os.path.abspath(path),
        "size": st.st_size,
        "sha256": _sha256_file(path),
        "package": facts["package"],
        "version_name": facts["version_name"],
        "version_code": facts["version_code"],
        "label": facts["label"],
        "min_sdk": facts["min_sdk"],
        "target_sdk": facts["target_sdk"],
        "abis": abis,
        "split": split,
        "signing": {"schemes": schemes, "certificates": [{"sha256": d} for d in digests]},
        "compatible": compatible,
        "problems": problems,
    }
