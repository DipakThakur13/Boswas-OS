# Windows application compatibility (WinCompat)

**Goal:** approved Windows applications run on Boswas OS without a Windows
installation, each in its own isolated, confined environment.
**Non-goal:** universal compatibility with every Windows executable.

Implemented by the `boswas-compat` package (Milestone 1). Sources:

- **Code:** `packages/boswas-compat/`.
- **Shipped data:** `compatibility/`.
- **Tests:** `tests/compatibility/`.

## At a glance

```sh
boswas-winapp inspect ~/Downloads/setup.exe     # check it: type, architecture, catalog, policy (runs nothing)
boswas-winapp install ~/Downloads/setup.exe     # verify, match the catalog, install in a new prefix
boswas-winapp list                              # installed applications and their state
boswas-winapp launch com.example.app            # start it in its sandbox
boswas-winapp stop com.example.app              # end it (all its Windows processes)
boswas-winapp status com.example.app            # state, sandbox, policy decision, confinement
boswas-winapp repair com.example.app            # update the prefix, recreate missing pieces
boswas-winapp upgrade com.example.app new.exe   # newer installer into the existing prefix
boswas-winapp logs com.example.app              # latest log (--install, --repair, --launch, --clear)
boswas-winapp remove com.example.app            # delete it and its prefix
boswas-winapp catalog                           # compatibility catalog and effective policy
boswas-winapp runtime                           # Wine, bubblewrap, AppArmor, policy: healthy?
boswas-winapp manifest validate FILE...         # check manifests (administrators, CI)
```

Every command accepts `--json`. `boswas winapp ...` runs the same tool.
Command reference and exit codes: [docs/administration/cli.md](../administration/cli.md).

The graphical front end is the **Boswas Compatibility Manager**
([below](#compatibility-manager)); remote management goes through the
device agent ([device-management](../device-management/README.md)). Both use
`boswas-winapp` underneath, so every rule on this page applies to them.

## Runtime

- **Wine 10.0** from Debian 13 (`wine`, `wine64`). `boswas-winapp` starts
  `/usr/lib/wine/wine64` and `/usr/lib/wine/wineserver64` directly
  (`compatibility/wine/runtime.conf`), not Debian's wrapper scripts.
- **DLL overrides that always apply:**
  - `winemenubuilder` is disabled, so Windows programs never create host
    desktop entries or MIME associations;
  - the Mono and Gecko download prompts are disabled (Debian ships neither).
- **Prefix defaults:** every new prefix also gets
  `compatibility/prefixes/defaults.reg`, which disables the crash dialog that
  would otherwise block unattended runs.
- **No DXVK / VKD3D-Proton:** they are not part of this release. Direct3D
  goes through Wine's own WineD3D.

### 64-bit Windows applications only

**Boswas OS v1 runs x86_64 (64-bit) Windows applications only.** This is a
deliberate, final product decision (ADR-0014), not a missing feature. There
is no `wine32`, no i386 multiarch, no WoW64 and no fallback.

32-bit software is refused before anything is created or run, always with:

> This application requires 32-bit Windows compatibility, which is not supported by Boswas OS.

| Where | How 32-bit software is detected |
|-------|---------------------------------|
| `boswas-winapp install`, `inspect`, `upgrade` | PE header of .exe files (machine field), Template property of .msi packages (`Intel;…` means 32-bit). Exit 4, reason `architecture` |
| Catalog manifests | `"architecture": "x86"` is accepted to describe an application, but marks it unsupported: it is never installable or launchable |
| Compatibility Manager | Shows the sentence above and "32-bit Windows applications are not supported by this version of Boswas OS." |
| Device agent | Refuses a 32-bit catalog entry before downloading anything |
| Control Plane | Marks it `UNSUPPORTED_ARCHITECTURE`; it can never become an install command. An x86_64 manifest for a 32-bit installer is refused |

Guard rails fail the image build if `wine32` or a foreign dpkg architecture
appears. Many installers, including NSIS and Inno Setup stubs, are 32-bit
even when they install 64-bit software. Applications are validated for the
catalog with 64-bit installers only.

## Per-application prefixes

One prefix per application, never shared, always owned by the user who
installed it:

```
~/.local/share/boswas/wine/<application-id>/        0700, the user's
  app.json      installation record          (not visible to the application)
  logs/         install, launch, repair logs (not visible to the application)
  .lock         held while the app or an operation runs (one instance per prefix)
  sandbox/      everything the application can see and change
    prefix/     WINEPREFIX
    home/       HOME inside the sandbox
    installer/  verified installer copy, during installation only
```

Inside its sandbox the application sees `sandbox/` as
**`/var/lib/boswas/wine/<application-id>/`**. On the host, the state lives
in the user's home, for these reasons (ADR-0011):

- Wine refuses a prefix that is not owned by the user who runs it.
- A system-wide writable `/var/lib` location would need a privileged
  helper or a world-writable directory, both of which add attack surface.
- Per-user storage keeps one user's applications away from another's.

The device agent's inventory (`boswas-winapp --json list --all-users`, as
root) reads these records without following symbolic links and reports only
IDs, versions and statuses.

## Installing

`boswas-winapp install <installer>`:

1. **Root check.** Refuses to run as root.
2. **Verified copy.**
   - Copies the installer into a private staging directory while hashing
     it, so the file that is checked is the file that runs.
   - Refuses anything larger than the policy limit.
3. **Inspection.** Detects the type from the file signature: PE (`.exe`) or
   Windows Installer (`.msi`). It also reads the PE architecture and
   recognises NSIS and Inno Setup installers, for their silent switches
   (`compatibility/installers/types.json`).
4. **Catalog match.**
   - **Blocked:** if any catalog layer pins the installer's SHA-256 as
     `blocked`, it is refused under any ID.
   - **With `--id`:** the manifest must pin the same SHA-256, if it pins one.
   - **Without `--id`:** the manifest that pins the hash is used.
   - **No match:** the installer is "unlisted", allowed only if policy
     permits it.
5. **Prefix.** Creates the prefix (`wineboot --init`) and applies the
   Boswas defaults, inside the sandbox.
6. **Installer.**
   - Runs the installer inside the sandbox, as the user.
   - **Silent switches:** taken from the manifest; otherwise `/qn` for MSI
     or the detected framework's switches. `--interactive` shows the
     installer's own dialogs.
   - **Portable programs:** `--portable` copies a self-contained program
     instead of running it.
   - **Extra arguments:** anything after `--` goes to the installer.
7. **Program and launcher.**
   - **Program:** the manifest's `launch` value, or, for unlisted apps, the
     single new non-uninstaller `.exe`. If several are found, `launch --exe`
     chooses one.
   - **Launcher:** a desktop entry `boswas-winapp launch <id>` is created.

A failed installation keeps its record (`state: failed`) and logs for
diagnosis; `boswas-winapp remove` cleans it up.

## Compatibility manifests (format v1)

Schema: `compatibility/manifests/schema/manifest-v1.schema.json`, installed
as `/usr/share/boswas/compat/schema/`. A unit test keeps it in sync with the
validator in `boswas_compat/manifest.py`.

```json
{
  "id": "example.application",
  "name": "Example Application",
  "version": "1.0",
  "runtime": {"type": "wine", "wineVersion": "10"},
  "architecture": "x86_64",
  "dependencies": [],
  "environment": {},
  "winetricks": [],
  "launch": "example.exe",
  "status": "tested",
  "installer": {"type": "exe", "sha256": "<64 hex>", "silentArgs": ["/S"]},
  "sandbox": {"network": false, "display": true, "audio": false, "gpu": false,
              "folders": ["documents"]}
}
```

| Field | Rules |
|-------|-------|
| `id` | Lower-case reverse-DNS name with at least one dot. Must equal the file name (`<id>.json`) |
| `runtime.wineVersion` | Major version must match the runtime (10) |
| `architecture` | `x86_64`. `x86` describes a 32-bit application, which is never installed or started (64-bit only) |
| `launch` | `C:\dir\app.exe`, `dir/app.exe` (relative to `C:\`) or a unique file name. Never another drive, UNC or `..` |
| `status` | `unknown`, `untested`, `experimental`, `tested`, `approved`, `blocked` |
| `installer.sha256` | **Required** for `tested` and `approved`: validated status always refers to one exact installer |
| `environment` | Variables the sandbox controls are rejected: `PATH`, `HOME`, `LD_*`, `XDG_*`, `WINEPREFIX`, D-Bus, Pulse. `winemenubuilder` can never be re-enabled |
| `dependencies` | Boswas-provided runtime components. None exist yet, so a non-empty list is refused |
| `winetricks` | Each verb must be in the policy's `WINETRICKS_ALLOWED`. winetricks is not shipped, so any verb is refused in this release (licensing review first) |
| `sandbox` | What the application may access; see below. Defaults: display only |

Full example: `compatibility/manifests/examples/example.application.json`.

### Statuses

| Status | Meaning | Installable by default policy |
|--------|---------|-------------------------------|
| `approved` | Validated and approved for employees | yes |
| `tested` | Passed Boswas validation (pinned installer) | yes |
| `experimental` | Works in limited testing | yes |
| `untested` | Manifest exists, not tested | yes |
| `unknown` | No claim made. Also the status of unlisted installers | yes |
| `blocked` | Must not run on Boswas OS | **never** |

`ALLOWED_STATUSES` in the policy narrows this list; production devices are
expected to allow `approved tested` only.

## Compatibility database

The catalog has three layers. The highest precedence wins, but `blocked` in
**any** layer wins over everything:

| Layer | Path | Source |
|-------|------|--------|
| managed | `/var/lib/boswas/compat/manifests/` | Control Plane catalog, written by the device agent (validated first) |
| local | `/etc/boswas/compat/manifests/` | Device administrator |
| system | `/usr/share/boswas/compat/manifests/` | `boswas-compat` (from `compatibility/manifests/catalog/`) |

- **Invalid manifests** are skipped and reported by `boswas-winapp catalog`;
  they are never partially applied.
- **Shipped catalog:** empty until Boswas validates its first business
  applications (`compatibility/manifests/catalog/README.md`).
- **Control Plane:** its catalog entries reach a device with each install or
  update command. The agent writes them to the managed layer and removes them
  when the device is unenrolled.

## Application states

`list` and `status` report a normalised state (`app_state`), used by the
Compatibility Manager, the device agent's inventory and the Control Plane:

| State | Meaning |
|-------|---------|
| `INSTALLING` | An installation is running |
| `INSTALLED` | Installed and idle |
| `RUNNING` | Its program is running |
| `STOPPED` | Its last run was ended with `stop` |
| `ERROR` | The installation or an upgrade failed (see the record's error) |
| `REPAIR_REQUIRED` | The prefix is damaged or the program is missing |
| `BLOCKED` | The catalog or the device policy no longer allows it |
| `UNSUPPORTED` | This runtime cannot run it (32-bit, another Wine version, missing components) |

`stop` ends the application's sandbox and every Windows process in it, like
"End task": unsaved work is lost. Only the process that holds the
application's lock and is named in its `run.json` is signalled, and only if
it belongs to the user.

## Security model

Windows programs are arbitrary native x86-64 code running with the user's
UID, so Wine is not a security boundary. Isolation comes from two independent
layers (ADR-0012):

```
boswas-winapp (user, unconfined)
  └─ bwrap  ── namespaces: user, pid, ipc, uts, cgroup, net (unless granted); no new privileges;
     │         all capabilities dropped; --disable-userns; --new-session; --die-with-parent
     └─ /usr/lib/boswas/compat/winapp-exec   ← AppArmor profile "boswas-winapp" attaches here
          └─ wine64 / wineserver64 / Windows processes   (inherit the profile)
```

### Layer 1: bubblewrap

Decides per application what exists in the sandbox:

| Present | Not present |
|---------|-------------|
| `/usr`, `/etc` read-only; new `/proc`, minimal `/dev`, private `/tmp` | the user's home and `/home` |
| `/var/lib/boswas/wine/<id>/` (own state) | other applications' prefixes |
| granted only: network, X11 socket and Xauthority, PulseAudio socket, `/dev/dri`, folders (`documents`, `downloads`, ...) | session and system D-Bus, `/run`, `/var`, `/media`, block, input and USB devices |

The environment is cleared; only variables the sandbox sets exist.

### Layer 2: the AppArmor profile

`compatibility/policies/apparmor/boswas-winapp` is the upper bound for every
Windows application, whatever the sandbox contains:

- read-only system;
- writable: only `/var/lib/boswas/wine/*/`, the private `/tmp` and
  `/dev/shm`, and the runtime directory;
- **no** capabilities, mounts, user namespaces, D-Bus, or raw devices;
- **no** Linux programs other than the Wine runtime: nothing in a prefix may
  be executed (no `x`);
- explicit `audit deny` for all home directories;
- signals, ptrace and unix sockets only between processes in this profile.

The runner checks its own AppArmor label and **refuses to start Wine unless
it is `boswas-winapp (enforce)`** (policy `REQUIRE_APPARMOR`, default
`yes`). This is tested negatively: with the profile unloaded, nothing runs.

On installed systems `apparmor.service` loads the profile at boot. The
package's postinst also loads it on installation (dh_apparmor). Reviewed
local additions go in `/etc/apparmor.d/local/boswas-winapp`.

### Documented exceptions

| Exception | Why | Mitigation |
|-----------|-----|------------|
| PE files in the prefix are mapped executable (`m`) | Windows code must run | No `x`: nothing there can be executed as a Linux program |
| `/tmp`, `/dev/shm`, `$XDG_RUNTIME_DIR/wine` mapped executable | Wine's anonymous file mappings and wineserver | All three are private to the sandbox |
| Network rules in the profile | Applications whose manifest grants network | bubblewrap removes the network otherwise (loopback only) |
| X11 | Windows applications use Wine's X11 driver (Xwayland) | An X11 client can observe other X11 clients. Under Plasma Wayland that is only other Xwayland clients. `display` is a grant |
| `audio` grant | Sound through the user's PulseAudio/PipeWire socket | The PulseAudio protocol lets clients load server modules (for example a network tunnel), which then run in the host audio server, outside the sandbox. `audio` is therefore **not** granted to unlisted applications by default; grant it only in reviewed manifests |
| `network` grant | Applications that need the network | The host network namespace: interface addresses, including MAC addresses, and abstract unix sockets (for example X11's) are visible |
| `ptrace` and `/proc/<pid>/mem` within the profile | wineserver implements Read/WriteProcessMemory | Same-profile peers only |
| Read-only system information: `/proc/<pid>/net/*`, CPU topology, the sandbox root and `/dev` listings, public CA certificates | Wine's network-adapter APIs, CPU detection, path lookups on `Z:\`, TLS certificate validation | Reads only. Inside the sandbox they show its own network namespace, minimal `/dev` and empty root |
| Running `/usr/bin/wine` directly is not mediated | Debian's wine is a normal user program | Users cannot gain privileges through it. Restricting direct use remains open (roadmap) |

### Rules that always hold

- **Never as root:** `install`, `launch`, `repair` and `remove` refuse root
  (`EUID 0`) and mismatched real/effective UIDs. Root may only list, for
  inventory.
- **No escalation from the application:** the record and logs are outside
  the application's view. Sandbox grants are recomputed from the catalog and
  policy at every start, so editing state cannot widen the sandbox.
- **Re-check at every launch:** an application blocked or disallowed after
  installation no longer starts.
- **Safe host-side file handling:**
  - symbolic links the application creates are never followed;
  - records are read with `O_NOFOLLOW`;
  - removal uses `rmtree`, which does not follow links;
  - program output is stripped of terminal control sequences before it
    reaches a terminal or a log.
- **One instance per prefix:** a second launch, or removing a running
  application, is refused (exit 6).
- **No stable host identifiers:** Windows applications cannot read
  `/etc/machine-id`, network hardware addresses or device serial numbers in
  sysfs, monitor EDID, or DMI firmware data. The `gpu` grant mounts only the
  GPU's own PCI directory. With `network` granted, interface (MAC) addresses
  are visible through the network namespace.
- **The host side never walks through the application's links.** The
  application controls everything in `sandbox/` and can replace directories
  there with symbolic links (for example to the user's home).
  `boswas-winapp` checks every directory it touches there with `lstat`:
  - it removes the installer copy as a whole, refusing links;
  - it does not list a linked prefix;
  - `repair` replaces planted links instead of following them.

### Security review (Milestone 1)

An adversarial review of the WinCompat code, assuming a fully malicious
Windows program, found seven issues. All are fixed and covered by regression
tests:

| # | Finding | Severity | Fix |
|---|---------|----------|-----|
| 1 | Installer clean-up listed `sandbox/installer` after the installer ran. A planted symlink to the home directory made it delete the user's files | Critical | `lstat` the directory, remove it as a whole (`rmtree` refuses links), recreate it. Same checks for `prefix`/`drive_c` walks and the portable copy. `repair` replaces planted links |
| 2 | The `audio` grant exposes the PulseAudio protocol, which can load server modules (network tunnel) outside the sandbox | High | `audio` removed from the default grants of unlisted applications; documented as a trusted grant |
| 3 | Program file names chosen by the application reached the terminal unsanitised, and C1 control characters passed the sanitiser | Medium | Names with control characters are ignored. All text output is sanitised, including C1 (U+0080–U+009F) |
| 4 | `repair` started Wine for an application that policy no longer allowed | Medium | `repair` refuses disallowed applications |
| 5 | The `gpu` grant mounted all of `/sys/devices` (MAC addresses, disk serials) | Medium | Only the GPU's PCI directory is mounted; AppArmor denies identifier files |
| 6 | The runner and `boswas-winapp` could parse `REQUIRE_APPARMOR` differently | Low | Confinement is skipped only if both say `no`; the runner parser handles spaces and a final line without a newline |
| 7 | The root inventory reported values from user-writable records unchecked | Low | Values coerced to short plain strings; status recomputed from the current catalog and policy |

## Policy

`/etc/boswas/compat/policy.conf` is root-owned and audited. Source:
`compatibility/policies/policy.conf`.

| Key | Shipped value | Meaning |
|-----|---------------|---------|
| `ALLOWED_STATUSES` | `approved tested experimental untested unknown` | Catalog statuses that may be installed and launched (`blocked` never) |
| `UNLISTED_APPS` | `allow` | Installers in no manifest: `allow` or `deny`. Production devices should use `deny` |
| `UNLISTED_NETWORK` | `no` | Network for unlisted applications |
| `UNLISTED_DEVICES` | `display gpu` | Devices for unlisted applications. `audio` is deliberately not granted by default (see the audio exception) |
| `UNLISTED_FOLDERS` | (empty) | Host folders for unlisted applications |
| `REQUIRE_APPARMOR` | `yes` | Refuse to run Windows code unless confined |
| `MAX_INSTALLER_MB` | `4096` | Installer size limit |
| `WINETRICKS_ALLOWED` | (empty) | winetricks verbs manifests may use |
| `BLOCKED_APPLICATIONS` | (empty) | Application IDs that may not be installed or started |
| `ALLOWED_APPLICATIONS` | (empty) | Empty: no allow list. Otherwise only these IDs; `none` allows nothing |

A missing file or an invalid value always results in the **more
restrictive** setting:

- a missing file means unlisted applications are denied and only `approved`
  and `tested` statuses are allowed;
- an invalid value never loosens anything.

**Managed devices.** On a device enrolled with a Control Plane, the device
agent writes `/var/lib/boswas/compat/policy.conf` from the verified, signed
device policy (ADR-0020):

- boswas-compat uses it instead of the local file;
- a managed file that is not a regular, root-owned file without group or
  world write access is ignored in favour of the restrictive defaults;
- a signed policy cannot switch off `REQUIRE_APPARMOR`;
- unenrolling removes the file.

`boswas-winapp catalog` and `runtime` show which policy is in effect.

## "Run with Boswas" (graphical flow)

```
Double-click or right-click a .exe or .msi   (Dolphin: "Run with Boswas")
        │   /usr/share/applications/boswas-winapp-install.desktop, the only
        │   handler for application/x-msdownload, x-ms-dos-executable,
        │   vnd.microsoft.portable-executable and x-msi
        ▼
Terminal window: boswas-winapp install --pause <file>
        │   catalog match → policy → prefix → installer (its own dialogs if
        │   it has no silent switches)
        ▼
Desktop launcher "boswas-winapp launch <id>" in the application menu
```

- **Default handler:** "Run with Boswas" stays the default for Windows
  executables. The Compatibility Manager adds a Dolphin action, "Install
  with Boswas Compatibility Manager".
- **Debian default:** Wine's own `wine.desktop` association is not
  installed. Windows programs therefore never open in an unconfined default
  prefix by double-click; the image test checks this.

## Compatibility Manager

`boswas-compat-manager` (package `boswas-compat-manager`, application menu:
**Compatibility Manager**) is the graphical front end (ADR-0021).

**Pages:**

- **Dashboard:**
  - counts of installed, running and attention-needing applications;
  - Wine, AppArmor and bubblewrap health;
  - device agent, Control Plane and policy status;
  - recent activity, including actions requested remotely by
    administrators.
- **Applications:**
  - tabs All, Installed, Running, Updates, Blocked, Repair Required,
    Unsupported;
  - per application: name, version, publisher, architecture, state,
    catalog status and last launch;
  - actions Launch, Stop, Repair, Update, Remove, Logs, Details.
- **Install:**
  - choose an .exe or .msi;
  - the installer is checked first (`inspect`: type, architecture, catalog
    status, the sandbox it would get, the policy decision);
  - 32-bit installers get the sentence above and nothing else;
  - the installation runs with live progress.
- **Details:**
  - overview (executable, architecture, prefix shown as
    `~/.local/share/boswas/wine/<id>`, dates, state, policy, manifest
    source);
  - permissions, **display-only**: files, network, display, audio, GPU,
    removable devices (never), AppArmor;
  - logs (latest, launch, install, repair) with Copy, Save and Clear.
- **Remove:** confirms what is deleted (the application, its prefix and
  everything in its C: drive, its launcher) and what is kept (files in
  granted folders).
- **System Status:**
  - Wine, AppArmor, bubblewrap;
  - Device Agent, Control Plane;
  - disk space, system architecture;
  - Windows applications: x86_64 / 64-bit only;
  - Boswas OS version, policy.

**How it works:**

- It runs as the user and talks only to the user's session agent (all
  application operations, which run `boswas-winapp`) and to the device
  agent's read-only status operations.
- It never starts a process, never touches prefixes, manifests, AppArmor
  or bubblewrap, and cannot change permissions. A test fails if its code
  contains any of these.
- It keeps working without a Control Plane, and shows a clear notice if the
  session agent is not running.

## Licensing

- **Microsoft components:** none are downloaded or redistributed unless
  Boswas Group holds the rights; Mono and Gecko prompts are disabled.
- **winetricks verbs:** each needs a licensing review before it is allowed.
- **The Windows test application** used by the tests is Boswas-authored and
  built from source (`tests/compatibility/fixtures/testapp/testapp.c`).

## Testing

| Suite | What it proves |
|-------|----------------|
| `packages/boswas-compat/tests` (91 unit tests; inspect, stop, upgrade, MSI architecture, states, policy lists and the managed layer since 1.0~alpha3) | Manifest rules and schema sync, catalog layering, policy fail-closed behaviour, installer inspection, sandbox argument construction, refusals (root, blocked, unlisted, mismatch, 32-bit, dependencies), launch, repair, remove, CLI exit codes, output sanitising, and regression tests for the security review (symlinks planted by the application, control characters in file names, repair of disallowed apps, GPU sysfs exposure, runner policy agreement) |
| `tests/static/test_sources.sh` | Manifests validate. Runtime facts, profile and code agree. The profile keeps its denials and has no exec transitions |
| `tests/packages/test_packages.sh` | Package contents and modes, conffiles, dh_apparmor postinst, profile compiles |
| `tests/compatibility/test_wine.sh` | Image: package, profile compiles with the image's parser, policy, root refusal, desktop handler |
| `tests/compatibility/test_winapp_runtime.sh` | Image, end to end with real Wine and bubblewrap: install, prefix ownership and isolation, launch, 10 isolation probes, network grant, exit codes, locking, blocked, unlisted, policy re-check, repair, remove, inventory |
| `tests/boot/qemu_boot_test.py` | Live VM with the real kernel: profile loads in enforce mode, no Windows code runs without it, install, launch, AppArmor denies files outside the profile, confinement label recorded, a Windows GUI program (Wine Notepad) runs confined on the Plasma session's display (screenshot `winapp-gui.png`) |

The image runtime test runs in the builder container. Docker Desktop's WSL2
kernel has no AppArmor, so there the AppArmor layer is reported as SKIP; the
QEMU boot test covers it.
