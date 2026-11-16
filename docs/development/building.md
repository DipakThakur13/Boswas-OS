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
| Debian 13 (trixie) | Natively (`./build.sh` as root, or `sudo ./build.sh`) | `apt install live-build debootstrap xorriso squashfs-tools mtools dosfstools grub-efi-amd64-bin grub-pc-bin dpkg-dev debhelper dh-apparmor build-essential librsvg2-bin fonts-lato python3 openssl rsync git cargo rustc pkgconf libgbm-dev libinput-dev libpam0g-dev libpixman-1-dev libseat-dev libudev-dev libxkbcommon-dev fonts-inter`; for `test.sh` also `lintian apparmor gcc-mingw-w64-x86-64-win32 qemu-system-x86 ovmf python3-pyside6.qtwidgets qt6-qpa-plugins`; 25 GB free |
| Anything else (Windows + Docker Desktop/WSL2, macOS, other Linux) | Automatically inside `build/container/Containerfile` (`debian:trixie`) | Docker or Podman; the container runs `--privileged` because live-build mounts `/proc`, `/sys` and `/dev` in its chroot |

On Windows, run the scripts from Git Bash or WSL. `./build.sh` detects that
the host is not Debian 13 and builds the `boswas-os-builder:trixie` image. It
then re-runs itself inside that container with the repository bind-mounted at
`/src`.

Force a mode with `--container` or `--native`.

Network access during a build: deb.debian.org (the image) and, since the
native desktop, **crates.io** (the Rust packages, see below). Both are
needed on every build host and inside the container.

## What `build.sh` does

1. **Preflight.** Checks the host (Debian 13, root), the required tools and
   the free disk space.
2. **Packages.** Builds the thirteen Boswas device packages (every
   directory of `packages/`) with `dpkg-buildpackage`, and the Control
   Plane server package into `server/` (never part of the image). The
   unit tests of the CLI, WinCompat, the device agent, the Compatibility
   Manager, Control Center, the system applications, the Control Plane,
   the native desktop and the boot screen run during the build.
   `boswas-desktop` and `boswas-bootsplash` are compiled with cargo from
   their committed `Cargo.lock` (see [Rust packages](#rust-packages)).
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
- the device agent is not enabled, `boswas-device` cannot validate the
  shipped `device.conf`, the session agent is not enabled for user sessions,
  or the Compatibility Manager does not import
- `wine32`, a foreign dpkg architecture (i386 multiarch) or the Control
  Plane appears in the image
- the native desktop is incomplete: `boswas-compositor`, `boswas-shell` or
  `boswas-session` is missing or does not run, the session entry is
  missing, a Plasma session is installed, greetd is not enabled or does
  not read `/etc/boswas/greetd.toml`, the greeter user or the text-login
  fallback is missing, the image signs a user in automatically, the lock
  screen's PAM service is missing, or the shipped compositor defaults do
  not enforce the lock and trust policy
- the boot screen is missing from an initramfs, its client is not there
  under the name the disk-unlock helper calls, Plymouth is in an
  initramfs, or `boswas-bootsplashd --self-test` fails
- the Boswas Prism icon themes are missing or are not the default

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
| Colours, radii, type, motion | `desktop/ui/tokens.json`, then `python3 desktop/ui/tools/build_kde_colors.py` and `python3 desktop/icons/tools/build_prism.py` to regenerate the colour schemes and icons (the tests fail on stale files). The Rust packages pick the file up when they are rebuilt; the Qt applications read the installed file |
| The look of the Boswas Qt applications | `desktop/ui/qt/boswas_fluent` (the toolkit, installed by `packages/boswas-branding/debian/rules`); `python3 -m boswas_fluent.gallery` shows every widget ([README](../../desktop/ui/qt/README.md)) |
| Compositor defaults and policy | `packages/boswas-desktop/config/compositor.toml` (shipped defaults); on a device `/etc/boswas/compositor.toml` |
| Login, session, lock | `packages/boswas-desktop/session/` |
| Boot screen | `packages/boswas-bootsplash/` (artwork from `desktop/branding/`) |
| Installer policy | `installer/configuration/preseed.cfg` |
| Boot menu | `config/live-build/config/bootloaders/grub-pc/` and `desktop/branding/boot-splash.svg` |

## Test stages

`./test.sh` runs these stages in order and writes one report:

1. static checks, including 64-bit-only, no shell execution, no Boswas ID
   implementation and service hardening
2. Boswas packages (lintian, contents, packaging checks for the agent, the
   Compatibility Manager and the Control Plane, CLI unit tests; for the
   desktop: the programs, the service bridge, the session and portal
   files; for `boswas-apps`: every application's launcher, desktop entry,
   manual page, module and icon, the reminders autostart entry and the
   `x-terminal-emulator` alternative)
3. unit tests: WinCompat, device agent, Compatibility Manager and Control
   Center (Qt offscreen), the system applications, and Control Plane,
   including the real agent against the real Control Plane over mutual
   TLS; the design tokens, the Qt toolkit and the icon packs; and the two
   Rust workspaces (`cargo test --locked`), which include the compositor's
   end-to-end tests on its headless back end and the service backends'
   integration tests against private `dbus-daemon` processes. Without
   cargo the Rust suites are reported as SKIP
4. WinCompat test fixtures: compiles the Windows test application from
   `tests/compatibility/fixtures/` with MinGW-w64, writes manifests pinned to
   its hash, and packs them into a fixtures ISO
5. package manifest resolution
6. image tests: artifacts, extraction, ISO contents, packages, security,
   compatibility, device management (no identity baked in, enabled units,
   64-bit and 32-bit Windows runtime)
7. **WinCompat runtime** (`tests/compatibility/test_winapp_runtime.sh`)
8. **Device management runtime** (`tests/device/test_device_runtime.sh`):
   in the image, the device agent, a user's session agent, the
   Compatibility Manager window (offscreen) and a Control Plane started from
   the sources. It covers local and remote install, launch, stop, repair and
   remove with real Wine, policies, offline operation and the audit trail
9. QEMU boot test: the boot screen, the login screen, the Boswas desktop
   (shell, a window, the lock), the Boswas terminal, file manager and
   calculator, the device agent, the session agent, the Compatibility
   Manager on the desktop and a Control Plane round trip under the real
   kernel's AppArmor. **This stage has not been run since
   the native desktop replaced Plasma**; its checks were rewritten for the
   new desktop and are untested until an image is built

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

## Rust packages

`packages/boswas-desktop` (five crates: `boswas-ui`, `boswas-compositor`,
`boswas-services`, `boswas-services-bridge` and `boswas-shell`) and
`packages/boswas-bootsplash` are Rust (ADR-0032).

| | |
|-|-|
| Toolchain | `rustc` and `cargo` 1.85, the versions in Debian 13. Both workspaces set `rust-version = "1.85"`. A newer toolchain works for development; code and dependencies must still build with 1.85 |
| Dependencies | The crates named in each `Cargo.lock`, downloaded from **crates.io** by cargo during the build and checked against the lock file. `debian/rules` and the tests use `--locked`: a build fails instead of changing the lock file |
| System libraries | `libseat-dev`, `libinput-dev`, `libudev-dev`, `libgbm-dev`, `libpixman-1-dev`, `libxkbcommon-dev`, `libpam0g-dev`, found through `pkgconf` |
| Fonts at build time | `fonts-inter` for `boswas-bootsplash`: its `build.rs` makes the glyph atlases from Inter. The Debian package build fails without it; a plain `cargo build` warns and uses Orbitron from the repository |
| Programs the tests start | `dbus-daemon` for the service backends' integration tests (they fail without it). `grim`, `Xwayland` and `xclock` for three of the compositor's end-to-end tests, which skip themselves without them |
| Where build products go | Package build: `build/` inside the staged package (cargo home and target directory; removed with the stage). Tests: the test work directory. Development: `target/` in the workspace (git-ignored) |
| Adding or updating a crate | Change `Cargo.toml`, run `cargo update -p NAME` (or `cargo build`) with network access, check that `cargo +1.85.0 build --locked` still works, and commit `Cargo.lock` with the change |
| `unsafe` | In shipped code (`src/`) only in the files listed in `tests/static/test_sources.sh`; the static stage fails on any other |

A build host without access to crates.io cannot build these two packages.
Vendoring the crates is planned (ADR-0032).

```sh
cd packages/boswas-desktop
cargo build --locked                                  # debug build of all five crates
cargo test --locked --workspace \
    --features boswas-ui/text,boswas-ui/icons,boswas-ui/images
cd ../boswas-bootsplash && cargo test --locked
```

The desktop workspace has unit tests in every crate and three sets of
tests that start real programs:

- `crates/boswas-compositor/tests/headless.rs`: eleven end-to-end tests
  that start the real `boswas-compositor` binary on its headless back end
  and drive it with real clients, injected keys and the control socket.
  They check that an ordinary client is offered no privileged protocol
  and a program the compositor starts is; that the public control socket
  answers `version`, `lock`, `reload-config`, `outputs` and `capture`
  only; a window's life from mapping to closing, with a screenshot; the
  window picker, Task View, the snap keys and the window menu; that a
  program (`grim`) gets a picture of the screen only after the answer
  "allow" and never while the session is locked; an X11 program
  (`xclock`) through Xwayland; and that a locked session stays locked
  when the lock screen dies and unlocks only through a lock client. They
  need `xkb-data` and a writable `/tmp`, no graphics device and no fonts.
- `crates/boswas-services/tests/it`: the service backends against two
  private `dbus-daemon` processes per test and scripted stand-ins for the
  system daemons (`crates/boswas-services/README.md`). Sockets go under
  `/tmp`, because socket paths are short.
- `crates/boswas-services-bridge/tests/bridge.rs`: the bridge program
  itself.

[verification.md](../architecture/verification.md) has the counts and
what the tests do not reach.

## Developing the desktop

The compositor runs without a virtual terminal in two ways:

```sh
cd packages/boswas-desktop

# In a window inside your own Wayland or X11 session (OpenGL ES):
cargo run -p boswas-compositor -- --backend winit

# Without any display, rendering in software into memory:
cargo run -p boswas-compositor -- --backend headless --headless-size 1280x800 --print-env
```

- `--print-env` prints `WAYLAND_DISPLAY` and `BOSWAS_COMPOSITOR_SOCKET`
  once the compositor is ready. Start clients with that `WAYLAND_DISPLAY`
  (`WAYLAND_DISPLAY=wayland-1 firefox-esr`).
- The compositor starts the shell from `[shell] command`, by default
  `/usr/libexec/boswas/boswas-shell`. From a checkout, pass a
  configuration that names your build:
  `--config dev.toml` with `[shell] command = ["target/debug/boswas-shell"]`
  and `[lock] command = ["target/debug/boswas-shell", "--lock"]`, or use
  `--no-shell`.
- `-- COMMAND` starts a program as an ordinary client once the compositor
  is ready.
- Under an X11 session the `winit` back end also needs `libxkbcommon-x11-0`
  (a Suggests of the package).
- `boswas-compositor --send version|lock|reload-config|outputs|capture
  [--control-socket PATH]` talks to a running compositor. Everything else
  on the control socket (window list, screenshots, input injection with
  `--test-input`) needs a trusted connection; for development,
  `[policy] privileged_protocols = "all"` in the `--config` file makes
  the socket file trusted. Never set that on a device. The protocol is in
  `docs/architecture/platform-contracts.md`.
- `BOSWAS_LOG=debug` (or `boswas_compositor=trace`) sets the log filter;
  messages go to standard error.
- A debug build of the shell runs without the real system services when
  `BOSWAS_SHELL_FAKE_SERVICES` is set: a stand-in inside the shell reports
  a plausible system, so every flyout can be tried on a development
  machine. Release builds do not contain it.
- X11 applications need `Xwayland` on the development machine; without it
  the compositor logs that and runs without X11.
- The lock screen authenticates with PAM service `boswas-lock`. Without
  `/etc/pam.d/boswas-lock` on the development machine every password is
  refused and a locked nested session can only be ended by stopping the
  compositor.

The `udev` back end (real hardware) needs a free virtual terminal and a
logind or seatd session: log in on a text console and run
`boswas-session`. It has not been run on hardware yet.

Review frames of the boot screen:
`cargo run --release --example frames -- OUTDIR 1920x1080` in
`packages/boswas-bootsplash` (see its README).

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
- **`cargo` fails with "failed to get … from crates.io" or "the lock file
  needs to be updated but --locked was passed".** The first means the build
  host cannot reach crates.io. The second means `Cargo.toml` changed
  without `Cargo.lock`: update the lock file on a machine with network
  access and commit it.
- **The build stops in `0100-boswas-verify`.** The message names what
  differs from the manifest. That is intentional; fix the cause and do not
  remove the check.
- **Leftover mounts after an interrupted native build.** The scripts unmount
  everything below the work tree before deleting it, and refuse to delete if
  anything is still mounted.
