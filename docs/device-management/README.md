# Device management: device agent

> **State in 1.0~alpha3:** implemented.
>
> - `boswas-device-agent` runs on every device: persistent identity, state,
>   inventory and posture, the local management API, and an optional
>   Control Plane connection.
> - The Control Plane is in [control-plane.md](control-plane.md).
> - A device without a Control Plane is fully functional and contacts no
>   backend.
> - **Boswas ID is not part of this release** (ADR-0015).

## Architecture

```
                 Boswas Control Plane  (control-plane.md)
                        ▲  HTTPS, mutual TLS (device certificate)
                        │  /api/v1/enroll, /api/v1/devices/{id}/...
┌───────────────────────┴──────────────────────────── Boswas OS device ─┐
│ boswas-device-agent.service  (root, sandboxed)                         │
│   identity · configuration · state · inventory · posture               │
│   Control Plane client · outbox · signed policy · typed commands       │
│   local API /run/boswas-agent/agent.sock ◄── boswas-device (CLI)       │
│        ▲  session agents register (kernel peer credentials)            │
│ boswas-session-agent.service  (systemd user unit, per user)            │
│   local API $XDG_RUNTIME_DIR/boswas/session.sock ◄── Compatibility     │
│        │                                              Manager (GUI)    │
│        ▼                                                               │
│ boswas-winapp  ──►  per-app Wine prefix  ──►  bubblewrap  ──►  AppArmor │
└────────────────────────────────────────────────────────────────────────┘
```

- **Windows applications belong to a user and never run as root**
  (ADR-0011, ADR-0018).
- **Session agents:** the root agent therefore never runs them. Each
  logged-in user's session agent runs `boswas-winapp` as that user, for the
  Compatibility Manager and for remote commands alike. Policy, manifests,
  prefixes, bubblewrap and AppArmor are enforced in exactly one place.
- **State ownership:**

  | Owner | State |
  |-------|-------|
  | boswas-compat | Application runtime state |
  | Device agent | Device state, identity and local inventory |
  | Control Plane | Fleet state |
  | GUI | Presentation state only |

Module map (`packages/boswas-device-agent/boswas_agent/`):

| Module | Content |
|--------|---------|
| `daemon.py` | The agent service: main loop, sync, command worker, local API operations, enrollment |
| `session_agent.py` | The per-user service: local API for the GUI, jobs, remote dispatch |
| `cli.py` | `boswas-device` |
| `identity.py` | Persistent device identity |
| `config.py` | `/etc/boswas/device.conf`: settings and validation |
| `state.py` | Device state evaluation and transitions |
| `inventory.py` | Allowlisted inventory collectors |
| `commands.py` | Typed commands, payload validation, expiry, results (shared with the Control Plane) |
| `executor.py` | One handler per command type |
| `policydoc.py`, `policy_store.py` | Signed policies (shared) and their application on the device |
| `client.py`, `credentials.py` | HTTPS mutual-TLS client, device key and certificate |
| `outbox.py`, `ledger.py` | Offline outbox and back-off; command ledger and event log |
| `localapi.py`, `sessions.py`, `winapp.py` | Local API, session channels, the typed boswas-winapp wrapper |
| `privacy.py` | Closed allowlists for every outgoing message (shared) |
| `models.py`, `interfaces.py`, `user_identity.py` | Messages and states, interfaces, the Boswas ID boundary |
| `../schemas/*.schema.json` | JSON schemas of every message, kept in sync by tests |

## Device identity

- **What it is:** a random UUID version 4, created by the agent at its first
  start and stored in `/var/lib/boswas/agent/identity.json` (0644, public).
- **It survives reboots:**
  - it is never regenerated;
  - a damaged file stops the agent (state ERROR) instead of being replaced
    silently.
- **Never derived from** the hostname, a MAC address, a disk or board serial
  number, or a user name. A test asserts the code never opens such files.
- **Not a credential:** after enrollment the device authenticates with its
  certificate (mutual TLS), never with the ID alone.
- **Reinstallation:** a reinstalled device gets a new ID, because `/var/lib`
  is new.
  - **Keeping the ID across a reinstallation:** pre-provision it as
    `DEVICE_ID` in `/etc/boswas/device.conf` (for example from the
    installer preseed) before the agent's first start.
  - **Re-enrollment** is needed either way, because the private key is gone.
    A device ID that is already enrolled can only enroll again with a token
    issued for it (`boswas-cp token create --device DEVICE_ID`), so nobody
    can take over a device's registration just by knowing its ID.
- **Live sessions:** the identity is kept in RAM and marked `ephemeral`. The
  Control Plane accepts ephemeral devices only with tokens that allow them.
- **Deliberate reset:** `boswas-device identity reset --yes` (root, while
  unenrolled) creates a new device.

```
$ boswas-device identity
Device ID:   6a2f41a3-c54c-4fc6-9cbb-07f8d7f6a8a1
Created:     2026-10-04T12:00:00Z
Source:      generated
Lifetime:    persistent
Enrollment:  enrolled
Certificate: 3f0c…
```

## Configuration: `/etc/boswas/device.conf`

- **Owner:** the administrator (a dpkg conffile of `boswas-os`).
- **The agent** reads it, reloads it when it changes, and never writes it.
- **Format:** `KEY="value"`, parsed without evaluation.
- **Validation:** `boswas-device config validate` checks it.
- **A file with errors is never used for anything that leaves the device:**
  the agent stays in ERROR, and local applications keep working.

| Key | Values | Meaning |
|-----|--------|---------|
| `AGENT_ENABLED` | `yes` / `no` | `no`: local functions only, no Control Plane contact |
| `REMOTE_COMMANDS` | `yes` / `no` | Accept typed commands from the Control Plane |
| `DEVICE_NAME`, `DEVICE_PROFILE` | label, identifier | Sent at enrollment |
| `CONTROL_PLANE_URL` | `https://…` | Empty: standalone device. No credentials, query or fragment |
| `CONTROL_PLANE_CA` | path | Pinned CA of the Control Plane server certificate (`/etc/boswas/`, `/usr/share/boswas/`, `/etc/ssl/certs/`, `/usr/local/share/ca-certificates/`); required with a URL |
| `HEARTBEAT_INTERVAL` | 30–86400 s | Default 300; the device policy can override it |
| `INVENTORY_POLICY` | `off` / `minimal` / `standard` | What inventory is sent (see Inventory) |
| `INVENTORY_INTERVAL` | 300–604800 s | Default 3600 |
| `TELEMETRY_POLICY` | `none` / `security` | `security`: send posture check results and compliance |
| `UPDATE_POLICY` | `manual` / `security-only` / `managed` | Only `managed` allows `UPDATE_AGENT` commands |
| `LOG_LEVEL` | `error` … `debug` | Journal identifier `boswas-device-agent` |
| `DEVICE_ID`, `TENANT_ID`, `ENROLLMENT_STATE`, `POLICY_VERSION`, `DEVICE_CERTIFICATE` | Milestone 1 contract | Still accepted. `DEVICE_ID` pre-provisions the identity. The agent keeps enrollment state in `/var/lib/boswas/agent` |

**Refused:**

- secret-looking keys (`*TOKEN*`, `*SECRET*`, `*PASSWORD*`, `*PRIVATE*`,
  `*CREDENTIAL*`);
- PEM material;
- quotes, `$`, backticks, `;`, `|`, `&`, `<`, `>`;
- a file that is not root-owned or is writable by group or others.

## Device state

| State | Meaning |
|-------|---------|
| `INITIALIZING` | Starting or re-initialising |
| `READY` | Everything healthy |
| `DEGRADED` | Works, but the Windows runtime (Wine, bubblewrap, AppArmor), the inventory or a posture check needs attention |
| `OFFLINE` | Enrolled, the Control Plane unreachable (after 3 failed attempts). **Local applications keep working** |
| `UPDATING` | An agent update is running |
| `ERROR` | Identity or configuration unusable; nothing leaves the device |
| `MAINTENANCE` | Remote commands paused by a local administrator (`boswas-device maintenance on`) |

**Transitions:**

- `ERROR` and `MAINTENANCE` recover through `INITIALIZING`.
- Precedence when evaluating: ERROR > MAINTENANCE > UPDATING > OFFLINE >
  DEGRADED > READY.

**Application states** come from boswas-compat: `INSTALLING`, `INSTALLED`,
`RUNNING`, `STOPPED`, `ERROR`, `REPAIR_REQUIRED`, `BLOCKED`, `UNSUPPORTED`.

**Connection states:** `STANDALONE`, `UNENROLLED`, `CONNECTED`, `OFFLINE`,
`REVOKED` (the device was retired or re-enrolled elsewhere).

## Inventory

Everything is allowlisted (`privacy.py`, `schemas/inventory-v1.schema.json`):

| Section | Content | Policy |
|---------|---------|--------|
| `os` | Boswas name, version, build; Debian version; kernel; architecture | minimal, standard |
| `packages` | Versions of the Boswas packages, Wine, bubblewrap, AppArmor only | minimal, standard |
| `windows_applications` | Per application and version: status, architecture, compatibility, aggregate state, number of installations, number running. **No user names** | minimal, standard |
| `compatibility` | Wine version, supported architectures (`x86_64`), bubblewrap, AppArmor mode, managed policy, health | minimal, standard |
| `hardware` | CPU model and count, memory, vendor/model/firmware version, boot mode, TPM version | standard |
| `storage` | Size and free space of the root filesystem | standard |

**Never collected:**

- serial numbers, `product_uuid`, MAC addresses or `/etc/machine-id`;
- user names or home paths;
- files, documents or browsing data;
- passwords or keys;
- command lines, keystrokes or screen contents.

Inventory is always collected locally (`boswas-device inventory`). It is
sent only when `INVENTORY_POLICY` is not `off`, and only when it changes
(its revision number increases) or the Control Plane asks.

## The agent service

`boswas-device-agent.service` runs as root with systemd sandboxing:

- `ProtectSystem=strict`, `ProtectHome=read-only`;
- the single capability `CAP_DAC_READ_SEARCH`, to read users' WinCompat
  records for the inventory;
- `NoNewPrivileges`, `MemoryDenyWriteExecute`, `SystemCallFilter=@system-service`;
- writable state only in `/var/lib/boswas/agent` and `/var/lib/boswas/compat`.

Main loop:

1. sleep on an event until the next scheduled task (never a busy loop);
2. check the Windows runtime every 10 minutes;
3. collect the inventory (and posture) every `INVENTORY_INTERVAL`;
4. when enrolled:
   1. send a heartbeat;
   2. fetch a newer signed policy if one is announced;
   3. fetch commands;
   4. flush the outbox.
5. Retries use exponential back-off with jitter: 5 s doubling to a 15-minute
   cap.

A separate worker thread executes commands, so a long installation never
delays heartbeats.

### Offline behaviour

- **Local work is unaffected.** Applications, `boswas-winapp`, the CLI, the
  Compatibility Manager and inventory collection never depend on the
  Control Plane.
- **The outbox keeps what must be sent** (`/var/lib/boswas/agent/outbox`,
  bounded to 500 items):
  - command results and events are kept;
  - inventory, status and compliance are coalesced to the latest;
  - events are dropped first when full.
- **Stale state is identifiable:**
  - `boswas-device status` shows `OFFLINE`, the last contact and the last
    error;
  - the Control Plane marks a device offline after `max(180 s, 3 ×
    heartbeat)` of silence and shows the seconds since its last heartbeat.

## Communication with the Control Plane

| Message | Endpoint | Schema | When |
|---------|----------|--------|------|
| Enrollment | `POST /api/v1/enroll` | `enrollment-request-v1` | `boswas-device enroll` |
| Heartbeat | `POST /api/v1/devices/{id}/heartbeat` | `heartbeat-v2` | Every heartbeat interval |
| Inventory | `PUT /api/v1/devices/{id}/inventory` | `inventory-v1` | On change or request |
| Status report | `POST /api/v1/devices/{id}/status` | `status-report-v1` | With the inventory (`TELEMETRY_POLICY=security`) |
| Compliance | `POST /api/v1/devices/{id}/compliance` | `compliance-report-v1` | Same |
| Device events | `POST /api/v1/devices/{id}/events` | `device-event-v1` | When they happen |
| Policy | `GET /api/v1/devices/{id}/policy` | `signed-policy-v1` | When the heartbeat announces a new version |
| Commands | `GET /api/v1/devices/{id}/commands/pending` | `device-command-v2` | After each heartbeat |
| Acknowledgement | `POST …/commands/{cid}/ack` | | `ACKNOWLEDGED`, then `RUNNING` |
| Result | `POST …/commands/{cid}/result` | `command-result-v1` | Outcome |
| Installer | `GET /api/v1/artifacts/{sha256}` | | For install and update commands |

**The heartbeat** carries only:

- the device ID, the agent version and the device state;
- the applied policy version and the inventory revision;
- the number of pending results and the time.

**Security of the channel:**

- **Server authentication:** the server certificate must chain to the
  pinned `CONTROL_PLANE_CA`, with host name checking.
- **Device authentication:** the device presents its certificate.
- **The private key** (EC P-256) is generated on the device in
  `/var/lib/boswas/agent/credentials` (0700). It never leaves.
- **Certificate checks:** the agent installs a certificate only if its CN
  is the device ID and its key is the device's own.

### Enrollment

1. The administrator sets `CONTROL_PLANE_URL` and `CONTROL_PLANE_CA` in
   `device.conf`.
2. The administrator creates a one-time token in the Control Plane.
3. On the device: `sudo boswas-device enroll --token-file FILE` (or the
   token on standard input).

- **Never on the command line:** the token is not accepted in argv (and no
  abbreviation of `--token-file` exists), so it never appears in `ps`.
- **Never stored or logged:** a test searches every file and log for it.
- **What the agent sends:** a CSR, OS facts and non-identifying hardware
  facts.
- **What it pins:** the policy-signing key from the response.

`boswas-device unenroll --yes` forgets the certificate, the managed policy
and the managed catalog; the local WinCompat policy applies again.

## Typed commands

Commands are a closed set of types (ADR-0019); payloads are built by the
Control Plane from its catalog and validated again on the device:

| Type | Payload | Device behaviour |
|------|---------|------------------|
| `INSTALL_APPLICATION` / `UPDATE_APPLICATION` | catalog application (manifest, installer SHA-256 and size) | Refuses 32-bit entries before downloading. Checks the device policy. Writes the manifest to the managed catalog layer. Downloads over mutual TLS and verifies size and SHA-256. Runs `boswas-winapp install` / `upgrade` in the active user's session |
| `REMOVE_APPLICATION` | application ID | `boswas-winapp remove`; already removed: success |
| `LAUNCH_APPLICATION` | application ID | Starts it in the user's session (policy re-checked) |
| `STOP_APPLICATION` | application ID | `boswas-winapp stop` |
| `REPAIR_APPLICATION` | application ID | `boswas-winapp repair` |
| `REFRESH_INVENTORY` | none | Collects and sends the inventory |
| `APPLY_POLICY` | policy version | Fetches, verifies and applies the signed policy |
| `UPDATE_AGENT` | Debian version | Only if `UPDATE_POLICY=managed` and the policy's `agent_updates=managed`. Installs that version of `boswas-device-agent` from the device's signed APT sources (`boswas-agent-update@.service`) |

**Rules:**

- **Unknown types are rejected.** Invalid envelopes, payloads for another
  device, expired commands and types the policy does not allow are
  rejected and reported.
- **Idempotent:** executed command IDs are recorded and never run twice.
  Repeating an operation is harmless.
- **Waiting for a user:** application commands wait until a user with a
  running session agent is logged in, or the command expires.
- **After a restart:** a command interrupted by an agent restart is reported
  as `INTERRUPTED`.

## Local management API

`/run/boswas-agent/agent.sock` (and the session agent's socket):

- **Framing:** newline-delimited JSON.
- **Authorisation:** the caller's user ID comes from the kernel
  (`SO_PEERCRED`), never from the request.

| Role | Operations |
|------|------------|
| any local user (read-only) | `agent.status`, `device.identity`, `device.inventory`, `device.config`, `policy.status`, `events.list`, `commands.list`, `runtime.status` |
| root | `enroll`, `unenroll`, `sync.now`, `maintenance.set`, `inventory.refresh`, `identity.reset` |
| a user's session agent | `session.register` (uid ≥ 1000) |

**The session agent's socket** accepts only its own user. Its operations:

- `apps.list`, `apps.status`, `apps.inspect`;
- `apps.install`, `apps.upgrade`, `apps.repair`, `apps.remove` (as jobs);
- `apps.launch`, `apps.stop`;
- `apps.logs`, `apps.clear_logs`;
- `catalog.list`, `runtime.status`, `system.status`;
- `jobs.get`, `jobs.list`, `events.list`.

It accepts from the device agent only the application operations of remote
commands, and installers only from the agent's verified artifacts.

**Validation:** every operation declares its parameters; unknown or
ill-typed parameters are refused before any handler runs. No operation
accepts a command line or a program to run.

## `boswas-device`

```
boswas-device [--json] status | identity | config validate [--file F] | config show
              | inventory [--refresh] | policy | commands | events
              | enroll [--token-file F] | unenroll --yes | sync | maintenance on|off
              | identity reset --yes
```

Read-only commands work without the service. Exit codes: 0 success; 1
failed or invalid configuration; 2 usage; 4 refused; 5 agent not running.
Reference: `boswas-device(1)`.

## Boswas ID (deferred)

Boswas ID is **not** implemented:

- no identity provider, SSO, OAuth/OIDC, user directory or account linking;
- `user_identity.py` keeps the attachment point (`IdentityProvider`,
  `IdentityContext`, `AuthenticatedPrincipal`, with `NoIdentityProvider`);
- on the Control Plane, `auth.OperatorAuthenticator` is where a future
  identity provider would plug in.

A static check fails if any other identity provider or an OAuth/OIDC/SAML/JWT
library appears. **Device identity ≠ user identity.**

## Tests

| Suite | What it proves |
|-------|----------------|
| `packages/boswas-device-agent/tests` (97 tests) | Identity persistence, races and reset; configuration validation; state machine; command validation, expiry, serialisation and idempotency; outbox, back-off; signed policies (a newly applied version is reported at once); privacy allowlists vs schemas; inventory (no serials, MAC addresses or user names; an unhealthy runtime is reported as such); daemon with an in-memory Control Plane (offline, revoked, maintenance, interrupted commands, token never stored); local API over real sockets including refusals for other users; session agent; boswas-winapp wrapper; CLI |
| `control-plane/tests` | Control Plane domain rules, and the real agent against the real Control Plane over mutual TLS (see control-plane.md) |
| `tests/device/test_device_image.sh` | Image: packages, enabled units, no identity baked in, configuration validates, 64-bit only |
| `tests/device/test_device_runtime.sh` | Image, end to end with real Wine: local install/launch/stop/logs/repair/remove through the session agent, 32-bit and blocked refusals, enrollment, policy, remote install/launch/remove, 32-bit catalog refusal, policy blocks, offline operation, audit, clean shutdown |
| `tests/boot/qemu_boot_test.py` | Live VM: agent service, session agent, Compatibility Manager on Plasma, enrollment and remote install/launch under the real kernel's AppArmor, offline launch |
