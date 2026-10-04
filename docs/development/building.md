# Building Boswas OS

## TL;DR

```sh
./build.sh     # → build/output/Boswas-OS-v1-alpha-amd64.{iso,sha256,manifest.txt}
./test.sh      # static, package, image and QEMU boot tests
```

`make build`, `make test`, `make test-quick`, `make clean` and `make help`
wrap the same scripts.

## Build hosts

| Host | How it builds | Requirements |
|------|---------------|--------------|
| Debian 13 (trixie) | Natively (`./build.sh` as root, or `sudo ./build.sh`) | `apt install live-build debootstrap xorriso squashfs-tools mtools dosfstools grub-efi-amd64-bin grub-pc-bin dpkg-dev debhelper dh-apparmor build-essential librsvg2-bin fonts-lato python3 rsync git`; for `test.sh` also `lintian apparmor gcc-mingw-w64-x86-64-win32 qemu-system-x86 ovmf`; 25 GB free |
| Anything else (Windows + Docker Desktop/WSL2, macOS, other Linux) | Automatically inside `build/container/Containerfile` (`debian:trixie`) | Docker or Podman; the container runs `--privileged` because live-build mounts `/proc`, `/sys` and `/dev` in its chroot |

On Windows, run the scripts from Git Bash or WSL. `./build.sh` detects that
the host is not Debian 13 and builds the `boswas-os-builder:trixie` image. It
then re-runs itself inside that container with the repository bind-mounted at
`/src`.

Force a mode with `--container` or `--native`.

## What `build.sh` does

1. **Preflight.** Checks the host (Debian 13, root), the required tools and
   the free disk space.
2. **Packages.** Builds the five Boswas packages with `dpkg-buildpackage`;
   the CLI and WinCompat unit tests run during the build.
3. **Metadata.** Writes `/usr/lib/boswas/image-info`: build ID, date, git
   commit, dirty flag, configuration hash, live-build version.
4. **Staging.** Assembles the live-build tree in an isolated work directory
   from `config/live-build`, adding:
   - the Boswas `.deb` files
   - the boot menu splash and installer banner rendered from SVG
   - the installer preseed
   - `image-info`

   Host file modes are normalised.
5. **Image.** Runs `lb config` and `lb build`, pulling everything else from
   deb.debian.org.
6. **Collect.** Copies the ISO to `build/output/`, writes the SHA-256 file,
   the human-readable manifest and `build/manifest/<build-id>.json`.
7. **Cleanup.** Removes the chroot and binary trees. The package cache is
   kept to speed up the next build.

Logs go to `build/logs/build-<timestamp>.log`. A failed build prints the log
location and keeps the work tree for inspection.

### Guard rails

`config/live-build/config/hooks/live/0100-boswas-verify.hook.chroot` runs
inside the image and **fails the build** if:

- a required package is missing
- an excluded package slipped in
- nftables, auditd or AppArmor is not enabled
- sudoers does not validate
- `boswas info` does not run
- `boswas-winapp` does not run, or the `boswas-winapp` AppArmor profile does
  not compile against Debian's kernel feature set

### Where state lives

| | Native mode | Container mode |
|-|-------------|----------------|
| Work tree | `/var/tmp/boswas-os-build` (`BOSWAS_WORK_DIR`) | volume `boswas-os-build` → `/var/cache/boswas/work` |
| live-build cache | `build/cache` (`BOSWAS_CACHE_DIR`) | volume `boswas-os-build` → `/var/cache/boswas/lb-cache` |
| Outputs, logs, manifests | `build/output`, `build/logs`, `build/manifest` | same (bind-mounted repository) |

`./clean.sh` removes outputs and work trees. `./clean.sh --cache` also drops
the cache and the volume. `./clean.sh --all` also removes the builder image.

### Options and environment

| Option / variable | Effect |
|-------------------|--------|
| `--keep-work` | Keep the live-build tree (chroot, binary) after the build |
| `--clean-cache` | Drop cached packages and the bootstrap before building |
| `BOSWAS_MIRROR`, `BOSWAS_MIRROR_SECURITY` | Use a local Debian mirror or caching proxy (e.g. apt-cacher-ng) for the build |
| `BOSWAS_CONTAINER_RUNTIME` | `docker` or `podman` |
| `BOSWAS_BUILDER_IMAGE` | Builder image tag; pin a digest for release builds |

## Reproducibility

Every build records:

- the Debian suite and every installed package with its version (manifest)
- the git commit and the dirty flag
- the build timestamp
- the live-build and debootstrap versions
- the builder image
- a hash of all build inputs (`config_hash`: relative paths and content, so
  checkouts in different directories hash identically)

`SOURCE_DATE_EPOCH` is the commit time for clean, committed trees.

**Not yet.** Bit-for-bit identical rebuilds also need the package inputs
pinned to a `snapshot.debian.org` timestamp (see the roadmap). Until then, a
rebuild picks up newer Debian (security) updates, and the manifest shows
exactly which versions were used.

## Changing the image

| To change | Edit |
|-----------|------|
| Packages | `config/live-build/config/package-lists/*.list.chroot`. Document why in `docs/architecture/package-manifest.md` |
| Exclusions | `config/live-build/config/archives/boswas-exclude.pref.chroot` |
| Version or name | `config/boswas/release.conf`, then `debian/changelog` of each package and `boswas_cli/__init__.py` (`test.sh` checks they match) |
| Security settings | `security/…`, installed by `packages/boswas-security/debian/rules` |
| Desktop defaults and artwork | `desktop/…`, installed by `packages/boswas-branding/debian/rules` |
| Installer policy | `installer/configuration/preseed.cfg` |
| Boot menu | `config/live-build/config/bootloaders/grub-pc/` and `desktop/branding/boot-splash.svg` |

## Test stages

`./test.sh` runs these stages in order and writes one report:

1. static checks
2. Boswas packages (lintian, contents, CLI unit tests)
3. unit tests (WinCompat, device agent)
4. WinCompat test fixtures: compiles the Windows test application from
   `tests/compatibility/fixtures/` with MinGW-w64, writes manifests pinned to
   its hash, and packs them into a fixtures ISO
5. package manifest resolution
6. image tests: artifacts, extraction, ISO contents, packages, security,
   compatibility
7. **WinCompat runtime** (`tests/compatibility/test_winapp_runtime.sh`)
8. QEMU boot test

A stage that exits non-zero without recording a failure, such as a crashed
test script, is itself recorded as a FAIL. Tests that never ran can therefore
not pass silently.

The WinCompat runtime stage installs and launches the test application as an
unprivileged user, with the image's own Wine, bubblewrap and boswas-compat:

- **Mounts:** the image root goes through a throw-away overlay and is entered
  with `pivot_root`, not `chroot`, because the kernel refuses the user
  namespaces bubblewrap needs inside a chroot.
- **AppArmor:** Docker Desktop's WSL2 kernel has no AppArmor, so this stage
  reports the AppArmor layer as SKIP. The QEMU boot test attaches the
  fixtures ISO as a second CD and checks enforcement with the real kernel.

## Boot test speed

`./test.sh` boots the ISO in QEMU twice:

- UEFI with Secure Boot enforced, through the GRUB menu, to the desktop
- direct kernel boot with a serial console, for functional checks

Screenshots and serial logs are saved to `build/logs/boot-test/`. With
`/dev/kvm` this takes a few minutes. Without it, QEMU emulates the CPU (TCG)
and the test can take 30–60 minutes. Docker Desktop does not expose
`/dev/kvm` to containers. For fast boot tests, use a Linux host with KVM and
Docker/Podman, or a Debian 13 machine (native mode). `./test.sh --no-boot`
skips the boot test.

## Troubleshooting

- **"The repository … is not signed" / "Not live until …".** The host clock
  is wrong. APT rejects signatures from the future. Sync the clock (on
  Windows: Settings → Time & language → Date & time → Sync now). Docker
  Desktop and WSL take their time from the host.
- **The build stops in `0100-boswas-verify`.** The message names what
  differs from the manifest. That is intentional; fix the cause and do not
  remove the check.
- **Leftover mounts after an interrupted native build.** The scripts unmount
  everything below the work tree before deleting it, and refuse to delete if
  anything is still mounted.
