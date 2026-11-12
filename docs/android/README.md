# Android applications on Boswas OS

Boswas OS runs Android apps in an optional Android runtime, **Waydroid**
(LineageOS in an LXC container, shown in Plasma on Wayland). The runtime is
not part of the default image: a user installs it after installation, from
Boswas Control Center or with `boswas-android runtime install`. It never
starts on its own, and no Android app starts automatically.

Components:

| Component | Role |
|-----------|------|
| `boswas-android` (package and command) | status and compatibility checks, APK inspection, app install/launch/remove, session control, F-Droid on request; runs as the user |
| `boswas-sysops` | the privileged steps (repository, packages, binder module configuration, image download, container service), each authorised by polkit |
| Waydroid 1.6 (`repo.waydro.id`) | the runtime itself, installed only on request |

Interfaces between them: [docs/architecture/platform-contracts.md](../architecture/platform-contracts.md), section 2.

## Where the runtime comes from (verified 2026-11-12)

Waydroid, `libgbinder`, `libglibutil` and `python3-gbinder` are **not in
Debian 13**. The maintained source is the Waydroid project's APT repository,
the one Waydroid's official installation instructions use
(<https://docs.waydro.id/usage/install-on-desktops> → `https://repo.waydro.id`).
That address serves a shell script; Boswas OS does **not** run it. We read it
(2026-11-12) to learn the repository layout it configures:

    deb [signed-by=/usr/share/keyrings/waydroid.gpg] https://repo.waydro.id/ <codename> main

with the key from `https://repo.waydro.id/waydroid.gpg`; `trixie` is a
supported codename. Instead of the script, boswas-sysops writes the
equivalent deb822 source from `/usr/share/boswas/android/runtime.conf`, with
the keyring shipped in `boswas-android` (checked against the pinned
fingerprint first), and an APT pin so that only these packages can come from
that repository:

| Package (trixie suite, 2026-10-27 index) | Version |
|---|---|
| `waydroid` (all) | 1.6.3 |
| `python3-gbinder` (amd64) | 1.3.1 |
| `libgbinder` (amd64) | 1.1.45 |
| `libglibutil` (amd64) | 1.0.80 |

`waydroid` depends on `python3:any, lxc, python3-gbinder, python3-gi,
gir1.2-gtk-3.0, python3-dbus, dbus, polkitd | policykit-1,
pipewire-pulse | pulseaudio, iptables`; everything except `python3-gbinder`
comes from Debian 13. The repository publishes all architectures in a single
`main/binary-all/Packages` index (unusual, accepted by APT).

**Signing key:** `/usr/share/boswas/keyrings/waydroid.gpg` (binary OpenPGP,
SHA-256 of the file `71fe05d735c812e15fe229bf10106b02b62561be8aa5280d63a58e25a5c0c5e2`):

    pub   rsa3072 2023-08-03 [SC] [expires: 2099-08-03]
          7CE0 331F 71E0 A238 BB10  02D7 0E40 6D18 1DCE E19C
    uid   Czarek Nakamoto <ci@mrcyjanek.net>
    sub   rsa3072 2023-08-03 [E]  B7B6 2E86 3405 C6E4 E18D  A95C 2BF5 5735 4822 0A06

How it was checked:

1. Downloaded over HTTPS from `https://repo.waydro.id/waydroid.gpg`, the
   location named by Waydroid's installation script.
2. The trixie `InRelease` (dated 2026-10-27) carries a good signature by this
   key, and its `SignWith: 7CE0331F71E0A238` names the same key.
3. The same fingerprint is published under the same identity on
   `keyserver.ubuntu.com` and on `keys.openpgp.org`; the script's author line
   names the same person (Czarek Nakamoto). This is consistent, independent
   publication, not proof of ownership: the trust anchor remains the key the
   Waydroid project serves over HTTPS. The unit tests recompute the
   fingerprint from the shipped file and compare it with `runtime.conf`.

## Kernel, Secure Boot, hardware

* Debian 13's kernel ships the binder IPC driver as the module
  `binder_linux` (`CONFIG_ANDROID_BINDER_IPC=m`, no binderfs). It is part of
  Debian's signed kernel package, so it loads with Secure Boot enabled; no
  unsigned module is needed. boswas-sysops configures
  `options binder_linux devices=binder,hwbinder,vndbinder`, the legacy device
  nodes Waydroid supports.
* Waydroid needs a **Wayland** session (Plasma on Wayland is the Boswas OS
  default), an **x86_64 CPU with SSSE3**; without **SSE4.2** Waydroid uses
  32-bit x86 images and apps built only for x86_64 do not run
  (`tools/helpers/arch.py`).
* Graphics: Waydroid uses the first `/dev/dri/renderD*` whose kernel driver is
  not `nvidia` (`tools/helpers/gpu.py`). With only NVIDIA's proprietary driver,
  or no render node (e.g. most virtual machines), Android uses software
  rendering: it works, slowly.
* Disk: the system and vendor images take about 2.5 GB in `/var/lib/waydroid`;
  `boswas-android check` asks for 4 GB free (the archives are downloaded first).

`boswas-android check` reports each of these as PASS, WARN, FAIL or UNKNOWN.
Not every GPU, kernel or machine works: the checks find the known blockers,
not every problem.

## How Waydroid behaves (read from the Waydroid 1.6.3 source)

| Behaviour | Source | Consequence in boswas-android |
|---|---|---|
| `waydroid status` prints `Session:\tRUNNING/STOPPED`, `Container:\t<state>`, `IP address:`, … | `tools/actions/status.py` | parsed defensively |
| `waydroid app list` prints `Name:`, `packageName:`, `categories:` blocks; no versions | `tools/actions/app_manager.py` | app versions are reported as `null` |
| `app install` / `app remove` report failures only as log lines, exit 0; install returns before Android has finished | same | success is confirmed by polling `app list` for the package |
| `app launch` starts a session itself if none runs (in the foreground) | same | started detached; the container service is started first |
| `session start` runs in the foreground | `tools/actions/session_manager.py` | started detached, then `status` is polled |
| Commands other than init/container/log answer `Waydroid is not initialized, run "waydroid init"` (exit 0) when not set up | `tools/__init__.py` | "set up" = `/var/lib/waydroid/waydroid.cfg` and `/var/lib/waydroid/rootfs` exist (Waydroid's own test) |
| Desktop entries: `$XDG_DATA_HOME/applications/waydroid.<package>.desktop`, `Exec=waydroid app launch <package>`, icons in `$XDG_DATA_HOME/waydroid/data/icons`; removed when the app is uninstalled, re-synced when Android starts | `tools/services/user_manager.py` | apps appear in the Boswas launcher; `boswas-android cleanup` removes leftovers after the runtime itself is removed |
| Config `/var/lib/waydroid/waydroid.cfg` (`[waydroid]` `arch`, `images_path`, `system_ota`, `vendor_ota`, `system_datetime`, `vendor_datetime`, …); image version = OTA build time (epoch) | `tools/config/__init__.py`, `tools/actions/initializer.py` | `status` shows image type (from `system_ota`, e.g. `…/VANILLA.json`) and build times |
| `waydroid init -s VANILLA` downloads `system` and `vendor` zips from `ota.waydro.id`, checks each zip's SHA-256 against the OTA channel's JSON before extracting; progress `\r[Downloading] <n> MB/<total> MB <speed>` | `tools/helpers/images.py`, `tools/helpers/http.py` | run by boswas-sysops `android-init` |
| Logs: `/var/lib/waydroid/waydroid.log` (root, mode 0644) | `tools/__init__.py`, `tools/helpers/logging.py` | shown by `boswas-android logs` with Boswas's own `~/.local/state/boswas/android.log` |

## APK files

`boswas-android apk inspect FILE` reads the APK without running anything:
the binary `AndroidManifest.xml` (package, version, SDK levels, a literal
application label), native code ABIs (`lib/<abi>/`), split status (`split`,
`isSplitRequired`, `requiredSplitTypes`), signing schemes (v1 JAR signature,
v2/v3 APK Signature Scheme blocks) and the SHA-256 of each signer
certificate. It was checked against the real F-Droid client APK: package
`org.fdroid.fdroid` 2.0.2, schemes v1+v2+v3, certificate SHA-256 equal to the
value F-Droid publishes.

What inspection does **not** do: it does not say an app is safe (a valid
signature only identifies who signed it), and it does not verify signatures
cryptographically. Android verifies the signature when it installs the APK.
Boswas OS never modifies, re-signs or "fixes" an APK.

Refused, with an explanation:

* **ARM-only apps** (native code only for `armeabi`, `armeabi-v7a`,
  `arm64-v8a`). Running them on an x86_64 Android image needs an ARM
  translation layer. The ones used with Waydroid (Intel's libhoudini, Google's
  libndk_translation) are proprietary components extracted from other
  products (Android-x86 / ChromeOS images) and installed by community scripts;
  Boswas OS has no right to redistribute them and does not include or
  download them. Apps without native code, or with x86/x86_64 code, run.
* **Split APKs**: one part of an app delivered as several APK files (and base
  APKs that require such parts). Installing the parts together is not
  supported; use the app's complete (universal) APK.
* Unsigned APKs and APKs without a valid package name (Android would refuse
  them anyway).

## Google Play

**Not available, by assessment, not by omission.**

* Google states that Google Mobile Services "is only available through a
  license with Google" (<https://www.android.com/gms/>), for devices Google
  certifies (<https://www.android.com/certified/>). Boswas OS has no such
  licence, and Waydroid/LineageOS is not Play Protect certified.
* Waydroid offers "GAPPS" images (LineageOS with Google's apps). They
  redistribute Google's proprietary apps without a GMS licence from Google,
  so they are not a legitimate source for Boswas OS to offer. Boswas OS
  initialises the runtime only with `IMAGE_TYPE="VANILLA"` and never
  downloads GAPPS images.
* Boswas OS does not work around device certification, Play Integrity or
  attestation, and never asks for Google account credentials.

Alternatives, under their own names: **F-Droid** (an independent catalogue of
free and open-source apps; `boswas-android fdroid install` downloads the
client from `https://f-droid.org/F-Droid.apk` and installs it only if its
signer certificate SHA-256 is
`43238d512c1e5eb2d6569f4a3afbf5523418b82e0a3ed1552770abb9a9c9ccab`, the value
F-Droid publishes at
<https://f-droid.org/en/docs/Release_Channels_and_Signing_Keys/> and on its
home page, owner "CN=Ciaran Gultnieks, … Wetherby … UK", valid until 2037), and
**APK files** the user already has. Declining Android entirely is the default.

If Boswas Group ever obtains a GMS licence and a licensed image source, the
assessment in `boswas_android/googleplay.py` and this section are the places
to change; nothing else in boswas-android offers Google Play.

### Known gap in Waydroid's own interface

Waydroid's own launcher entry ("Waydroid", `waydroid first-launch`) and its
"Initialize Waydroid" action (`waydroid init --client`) open Waydroid's GTK
dialog, which offers "Android with Google Apps". Boswas OS does not offer
or use that path, but a user with the runtime installed can still choose it
there. Closing this needs controls outside boswas-android (for example hiding
Waydroid's own desktop entry and restricting its remote-init D-Bus method); it
is listed as remaining work. Likewise Waydroid's APK handler
("Install in Waydroid", `waydroid app install %f`) bypasses Boswas's APK
inspection (Android still verifies signatures).

## Using it

    boswas-android check                 # can this computer run Android apps?
    boswas-android runtime install       # Waydroid from its signed repository (asks for authorisation)
    boswas-android runtime init          # download and verify the Android images (~800 MB download)
    boswas-android session start
    boswas-android apk inspect app.apk
    boswas-android apk install app.apk
    boswas-android app list
    boswas-android app launch org.example.app
    boswas-android runtime remove --purge-data
    boswas-android cleanup               # your Android data and leftover launcher entries

All commands accept `--json`; exit codes are in `boswas-android(1)`.

## Limitations

* Waydroid is maintained outside Debian; its updates come from its repository
  and are not covered by Debian security support.
* No ARM translation (above); no Google Play (above); split APKs not supported.
* Android app versions are not reported by Waydroid, so updates can be
  confirmed only as "installed".
* Hardware acceleration depends on the GPU driver; virtual machines usually
  run Android with software rendering.
* Android runs in a privileged LXC container managed by Waydroid's
  `waydroid-container.service` (root); its isolation is Waydroid's, not the
  per-application sandbox Boswas WinCompat uses for Windows apps.
