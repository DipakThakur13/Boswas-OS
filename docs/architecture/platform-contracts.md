# Platform contracts: boswas-sysops, boswas-android, Control Center

The interfaces between the privileged helper, the Android runtime manager and
Boswas Control Center. Each component is tested against this document with
fakes of the others; a change here changes all three.

## 1. boswas-sysops: the only privileged helper

`/usr/libexec/boswas/sysops` (Python 3, standard library only) performs a
fixed set of system operations as root. It is started by the desktop user's
programs as

    pkexec /usr/libexec/boswas/sysops <operation> [arguments...]

Never setuid, never a daemon, never a shell, never an arbitrary command.

* Runs only as root started through pkexec (`PKEXEC_UID` set); otherwise it
  prints a `result` with code `INVALID_ARGUMENT` and exits 2.
* Every operation has its own polkit action `com.boswas.sysops.<operation>`
  (annotations `org.freedesktop.policykit.exec.path` =
  `/usr/libexec/boswas/sysops` and `org.freedesktop.policykit.exec.argv1` =
  `<operation>`). Read-only operations: `allow_active=yes`; everything else:
  `allow_active=auth_admin_keep`; `allow_any` and `allow_inactive` are `no`.
* Environment is replaced: `PATH=/usr/sbin:/usr/bin:/sbin:/bin`,
  `LC_ALL=C.UTF-8`, `DEBIAN_FRONTEND=noninteractive`,
  `APT_LISTCHANGES_FRONTEND=none`.
* Each invocation is logged to `/var/log/boswas/sysops.log` (0640 root:adm):
  time, requesting UID, operation, validated arguments, result code. Never
  secrets, never file contents.

### Output: JSON Lines on stdout (UTF-8, one object per line, flushed)

    {"type": "start", "schema": "boswas-sysops/1", "operation": "apt-install", "cancellable": true}
    {"type": "progress", "phase": "download", "percent": 42.0, "message": "Downloading firefox-esr"}
    {"type": "log", "level": "info", "message": "..."}
    {"type": "result", "ok": true, "code": "OK", "message": "One sentence for the user.", "data": {}}

* `phase`: `refresh`, `download`, `install`, `configure`, `remove`,
  `cleanup`, `verify`, `init`, `service`, `inspect`.
* `percent`: number 0–100 or `null` (unknown).
* `level`: `info` or `warning`.
* Exactly one `result` line, always last, also on failure.
* `cancellable` in `start` says whether the operation can be cancelled at all;
  `progress` lines may carry `"cancellable": false` when a non-cancellable
  phase begins (dpkg unpack/configure).

### Result codes and exit status

| code | exit | meaning |
|------|------|---------|
| `OK` | 0 | done |
| `FAILED` | 1 | the operation ran and failed (`data.detail`: last lines of the tool's output) |
| `INVALID_ARGUMENT` | 2 | argument rejected by validation |
| `NOT_ALLOWED` | 2 | refused by Boswas policy (excluded or protected package, policy file) |
| `NOT_FOUND` | 2 | no such package in the configured, signature-verified repositories |
| `UNTRUSTED` | 2 | source not verified (unsigned repository, keyring fingerprint mismatch) |
| `DISK_SPACE` | 1 | not enough free space |
| `LOCKED` | 3 | another package manager holds the dpkg/APT lock (`data.holder`: `{"pid": n, "command": "..."}` when known) |
| `CANCELLED` | 4 | cancelled by the user before anything was changed |
| `NETWORK` | 5 | a download failed (no network, mirror unreachable) |
| `UNSUPPORTED` | 6 | hardware or system cannot run this (e.g. no binder support) |

pkexec itself exits 126 (authentication dismissed or failed) or 127 (not
authorized / helper missing); callers report those as "not authorized" without
reading stdout.

### Cancellation

The caller writes `cancel\n` to the helper's stdin. It is honoured only while
nothing has been changed (APT index refresh, package download, Android image
download); the helper stops the child process and reports `CANCELLED`. During
dpkg unpack/configure it logs a `warning` ("cannot be cancelled while packages
are being installed") and continues. End of stdin is not a cancel request.

### Operations (v1)

| operation | arguments | polkit default | notes |
|-----------|-----------|----------------|-------|
| `apt-refresh` | – | auth_admin_keep | `apt-get update`; `data.errors` lists failed sources |
| `apt-upgrade` | – | auth_admin_keep | `apt-get --with-new-pkgs upgrade` (never removes packages); `data.upgraded` |
| `apt-install` | `PKG...` (1–50) | auth_admin_keep | names `^[a-z0-9][a-z0-9+.-]{0,99}(:(amd64\|i386\|all))?$`; signed repositories only (no `--allow-unauthenticated`); Boswas-excluded packages refused |
| `apt-remove` | `PKG...` (1–50) | auth_admin_keep | simulated first; refused if it would remove a protected package (`boswas-*`, essential/required, kernel, bootloader, desktop core, security baseline) |
| `apt-install-local` | `/abs/path/file.deb` | auth_admin_keep | policy `LOCAL_DEB` in `/etc/boswas/apps.conf` (`allow` default, `deny`); copied to a root-only directory before inspection; result says it was not verified by a signed repository |
| `apt-clean` | – | auth_admin_keep | `apt-get clean`; `data.freed_bytes` |
| `journal-vacuum` | – | auth_admin_keep | `journalctl --vacuum-size=200M`; `data.freed_bytes` |
| `firmware-report` | – | yes | read-only, see below |
| `flatpak-remote-enable` | `NAME` | auth_admin_keep | NAME must have `/usr/share/boswas/sysops/flatpak-remotes/NAME.flatpakrepo` and be allowed by `FLATPAK_REMOTES` in `/etc/boswas/apps.conf` |
| `flatpak-remote-disable` | `NAME` | auth_admin_keep | |
| `android-runtime-install` | – | auth_admin_keep | see section 2; policy `ANDROID_RUNTIME` (`allow` default, `deny`) |
| `android-runtime-remove` | `[--purge-data]` | auth_admin_keep | removes packages, repository entry, pin, module configuration; `--purge-data` also `/var/lib/waydroid` |
| `android-init` | – | auth_admin_keep | `waydroid init -s VANILLA` (downloads images, verified by Waydroid's checksums; cancellable while downloading). Never GAPPS. |
| `android-update` | – | auth_admin_keep | `waydroid upgrade` |
| `android-repair` | – | auth_admin_keep | stop container, `waydroid upgrade --offline`, reload binder module, start container |
| `android-service` | `start\|stop\|restart` | yes | `systemctl <verb> waydroid-container.service` |

`firmware-report` data:

    {"source": "journal" | "dmesg" | "none",
     "missing": [{"file": "rtl_nic/rtl8125b-2.fw", "driver": "r8169", "device": "0000:03:00.0",
                  "package": "firmware-realtek" | null, "installed": false, "candidate": "20250410-2" | null}]}

Package mapping uses `/usr/share/boswas/firmware/index.tsv` (generated at
image build time from the Debian Contents indices: `<path below
lib/firmware>\t<package>` per line), then `dpkg -S` for installed files.

## 2. boswas-android: Android runtime manager (Waydroid)

Python package `boswas_android` in `/usr/lib/boswas/python`, CLI
`/usr/bin/boswas-android [--json] <command>`. Runs as the desktop user;
privileged steps call `pkexec /usr/libexec/boswas/sysops android-*` and pass
its JSON Lines through unchanged (`--json`) or as text.

Data shipped by the package:

* `/usr/share/boswas/keyrings/waydroid.gpg`: the Waydroid repository signing
  key (binary keyring), fingerprint recorded in
* `/usr/share/boswas/android/runtime.conf` (KEY="value", parsed without
  evaluation): `REPO_URL`, `REPO_SUITE`, `REPO_COMPONENTS`, `KEY_FINGERPRINT`,
  `ALLOWED_PACKAGES` (space-separated: what the APT pin lets through from that
  repository), `IMAGE_TYPE="VANILLA"`, `FDROID_URL`, `FDROID_CERT_SHA256`.

sysops `android-runtime-install` writes, from that file only:
`/etc/apt/sources.list.d/boswas-waydroid.sources` (deb822, `Signed-By:` the
shipped keyring after checking its fingerprint),
`/etc/apt/preferences.d/boswas-waydroid.pref` (origin pinned to -1 except
`ALLOWED_PACKAGES`), `/etc/modprobe.d/boswas-android.conf`
(`options binder_linux devices=binder,hwbinder,vndbinder`),
`/etc/modules-load.d/boswas-android.conf` (`binder_linux`); then refreshes,
installs `waydroid`, loads the module and disables autostart of
`waydroid-container.service` (started on demand by `android-service start`).

### `boswas-android --json status`

    {"schema": "boswas-android/1",
     "runtime": {"installed": bool, "version": str|null, "repository": bool},
     "compatibility": {"supported": bool,
                       "checks": [{"id": "binder"|"wayland"|"cpu"|"kernel"|"gpu"|"disk"|"secure-boot",
                                   "status": "PASS"|"WARN"|"FAIL"|"UNKNOWN", "title": str, "detail": str}]},
     "initialized": bool,
     "images": {"type": "VANILLA"|null, "system": str|null, "vendor": str|null, "bytes": int|null},
     "service": {"active": bool|null},
     "session": {"running": bool, "state": str|null},
     "storage": {"images_bytes": int|null, "user_bytes": int|null},
     "network": {"interface": "waydroid0", "present": bool, "address": str|null},
     "gpu": {"acceleration": "hardware"|"software"|"unknown", "driver": str|null, "render_node": str|null},
     "google_play": {"available": false, "reason": str, "alternatives": ["fdroid", "local-apk"]}}

Other commands (all accept `--json`; JSON output always has `schema`):

| command | effect |
|---------|--------|
| `check` | `{"schema", "compatibility": {...}}` only |
| `apk inspect FILE` | `{"schema", "apk": {...}}` (below); never executes anything |
| `apk install FILE` | inspect, refuse incompatible or split APKs, then `waydroid app install`; Android verifies the signature |
| `app list` | `{"schema", "apps": [{"package", "name", "version"}]}` |
| `app launch PKG` / `app remove PKG` | |
| `session start` / `session stop` | user-level Waydroid session |
| `runtime install\|init\|update\|repair\|remove [--purge-data]` | sysops `android-*` |
| `service start\|stop\|restart` | sysops `android-service` |
| `fdroid install` | explicit only: download the F-Droid client from `FDROID_URL` over HTTPS, require signer certificate SHA-256 = `FDROID_CERT_SHA256`, install. Labelled "F-Droid", never "Google Play" |
| `google-play` | the feasibility assessment (`google_play` object) |
| `logs [--lines N]` | Boswas Android log and the Waydroid log where readable |
| `cleanup` | after `runtime remove`: the user's Android data and leftover `waydroid.*.desktop` entries (asks unless `--yes`) |

APK document:

    {"path": str, "size": int, "sha256": str,
     "package": str|null, "version_name": str|null, "version_code": int|null,
     "label": str|null, "min_sdk": int|null, "target_sdk": int|null,
     "abis": ["x86_64", "arm64-v8a", ...],          // from lib/<abi>/ entries; [] = no native code
     "split": {"is_split": bool, "name": str|null, "requires_splits": bool},
     "signing": {"schemes": ["v1", "v2", "v3"], "certificates": [{"sha256": str}]},
     "compatible": bool, "problems": [str]}          // problems explain every refusal

Compatibility: the runtime is x86_64 (Waydroid images); APKs with native code
only for ARM ABIs are refused ("needs an ARM translation layer, which Boswas
OS does not include"); split APKs (`split` attribute or `isSplitRequired`)
are refused with an explanation that all parts must be installed together.
Inspection never claims an APK is safe.

## 3. Control Center integration

Control Center reaches these only through its allowlisted runner
(`commands.EXECUTABLES`): `/usr/bin/pkexec` always with
`/usr/libexec/boswas/sysops` as first argument and an operation from a fixed
set, and `/usr/bin/boswas-android`. Long operations use a streaming call that
reads JSON Lines, shows progress, offers Cancel only while the last `start` /
`progress` line says `cancellable`, and reports success only on a `result`
line with `ok: true`.
