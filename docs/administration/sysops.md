# boswas-sysops: the privileged helper

`boswas-sysops` is the only Boswas component that changes the system as root
on behalf of a desktop user. Boswas Control Center (software, updates,
storage, hardware, Android pages) and `boswas-android` start it as

    pkexec /usr/libexec/boswas/sysops <operation> [arguments...]

The interface (operations, JSON Lines events, result codes, exit statuses,
cancellation) is specified in
[docs/architecture/platform-contracts.md](../architecture/platform-contracts.md),
section 1. This page covers administration and the security design.

## Security design

| Property | How |
|----------|-----|
| No standing privilege | Not setuid, no daemon, no D-Bus service. It runs only for the duration of one pkexec call and refuses to run unless it is root **and** `PKEXEC_UID` is set. |
| Per-operation authorisation | One polkit action per operation (`com.boswas.sysops.<operation>`), selected by pkexec through the `exec.path` + `exec.argv1` annotations (supported by Debian 13's polkit 126). Read-only operations (`firmware-report`, `android-service`): active local session. Everything else: administrator password (`auth_admin_keep`). Remote and inactive sessions: never. |
| No arbitrary commands | The operation table in `boswas_sysops/cli.py` is the whole feature set. Child processes come only from `boswas_sysops/runner.py`, from ten fixed absolute paths, never through a shell, with stdin from `/dev/null`, a fixed environment and a timeout. Tests enforce all of this (`tests/test_policy_safety.py`). |
| Validated arguments | Package names `^[a-z0-9][a-z0-9+.-]{0,99}(:(amd64\|i386\|all))?$`, at most 50; Flatpak remote names; absolute normalised paths. |
| Signature verification kept | `APT::Get::AllowUnauthenticated=false`; no `--allow-*` option, no dpkg `--force-*` except conffile handling (`--force-confdef`, `--force-confold`). Flatpak remotes carry their signing key; the Waydroid keyring must match a pinned fingerprint. |
| Policy | Every change is simulated first (`apt-get -s`). Refused: installing packages Boswas excludes (the image build's exclusion list), removing protected packages directly or as a side effect (Boswas components, Essential/required packages, kernel, GRUB, shim, cryptsetup, desktop core, security baseline, APT, dpkg). |
| Local packages | `apt-install-local` opens the file without following symlinks, requires a regular file owned by the requesting user (or root), at most 2 GB, starting with the `.deb` magic, copies it into a fresh root-only directory under `/var/cache/boswas/sysops` (0700) and inspects and installs only that copy. The result states that its origin was not verified. Policy `LOCAL_DEB` can turn it off. |
| Locks | Before APT runs, the dpkg/APT lock files are tested with the same fcntl locks dpkg uses; a holder is reported (`LOCKED`, PID and command from `/proc/locks`). |
| Cancellation | Only while nothing has changed (index refresh, downloads, Android image download); the process group gets SIGINT, then SIGTERM, then SIGKILL. During dpkg unpack/configure or image extraction, a cancel request is refused with a warning. |
| Audit | One line per invocation in `/var/log/boswas/sysops.log` (directory 0750, file 0640, `root:adm`): UTC time, requesting UID, operation, arguments (JSON-encoded, so lines cannot be forged), result code. |

## Device policy: `/etc/boswas/apps.conf`

    LOCAL_DEB="allow"           # allow | deny: packages from local .deb files
    FLATPAK_REMOTES="flathub"   # remotes an administrator may enable
    ANDROID_RUNTIME="allow"     # allow | deny: Waydroid installation

Any value other than `allow` counts as `deny`. The file is a conffile of the
`boswas-sysops` package.

## Flatpak remotes

No Flatpak remote is enabled by default. `flatpak-remote-enable flathub` adds
Flathub system-wide from the definition shipped in
`/usr/share/boswas/sysops/flatpak-remotes/flathub.flatpakrepo`; users then
install applications per user (`flatpak --user`) without the helper.

The shipped definition is the unmodified file published at
`https://dl.flathub.org/repo/flathub.flatpakrepo` (fetched 2026-11-12, SHA-256
`3371dd250e61d9e1633630073fefda153cd4426f72f4afa0c3373ae2e8fea03a`). Its
embedded key was checked by importing it into a throw-away GnuPG home in the
Debian 13 builder container (`gpg --show-keys --with-fingerprint`):

    pub   rsa4096 2017-06-16 [SC] [expires: 2027-06-14]
          6E5C 05D9 79C7 6DAF 93C0  8135 4184 DD4D 907A 7CAE
    uid   Flathub Repo Signing Key <flathub@flathub.org>

This is the fingerprint Flathub publishes for its repository signing key. The
package tests recompute it with the helper's own OpenPGP parser. **The key
expires on 2027-06-14**: refresh the definition from Flathub before then.

## Android runtime

`android-runtime-install` reads `/usr/share/boswas/android/runtime.conf` and
`/usr/share/boswas/keyrings/waydroid.gpg` (both shipped by `boswas-android`),
checks that the keyring holds exactly one primary key whose fingerprint equals
`KEY_FINGERPRINT`, checks for binder support in the running kernel, then
writes the repository (deb822, `Signed-By` the shipped keyring), an APT pin
that lets only `ALLOWED_PACKAGES` come from that origin, and the binder module
configuration; it installs `waydroid`, loads `binder_linux` and turns off
automatic start of `waydroid-container.service`. If anything fails before
`waydroid` is installed, the files it created are removed again.
`android-init` downloads only the `VANILLA` image: Google components are never
requested.

Waydroid's own desktop entries are hidden after installation. Its launcher and
"Initialize Waydroid" dialog would offer an Android system with Google apps,
and its APK handler (`waydroid app install %f`) would skip Boswas's APK
inspection. The waydroid package's files are not modified. For each
`/usr/share/applications/*.desktop` that `dpkg -L waydroid` lists, sysops
writes an entry with the same name and `Hidden=true` to
`/usr/local/share/applications/`, which comes earlier in `XDG_DATA_DIRS`. The
written paths are recorded in `/var/lib/boswas/android/hidden-desktop-entries`.
`android-runtime-remove` deletes exactly those entries, and only while they
still carry the sysops marker. A file there that sysops did not write is never
touched. `android-init` refuses with `NOT_ALLOWED` when `waydroid.cfg` is
already configured for a GAPPS image (for example by Waydroid's own dialog
before the entries were hidden).

## Troubleshooting

| Symptom | Meaning |
|---------|---------|
| pkexec exits 126 | authentication dismissed or failed |
| pkexec exits 127 | not authorized (remote/inactive session) or helper missing |
| `LOCKED` | another package manager (often `unattended-upgrades`) is running; wait |
| `NETWORK` | a download failed; `apt-refresh` lists the sources that failed in `data.errors` |
| `UNTRUSTED` | a repository signature or a pinned key did not verify; nothing was installed |
| `NOT_ALLOWED` | Boswas policy (excluded/protected package, `apps.conf`) |

Every invocation is in `/var/log/boswas/sysops.log`.
