# Boswas Control Plane

The management server for Boswas OS devices: device registry, enrollment,
heartbeats and inventory, typed management commands, signed device policies,
the catalog of 64-bit Windows applications, the audit trail and a web
dashboard.

| | |
|-|-|
| **Source** | `control-plane/` |
| **Package** | `boswas-control-plane`, built by `build/scripts/build-packages.sh` into `server/`; never part of the device image |
| **Stack** | Python 3 standard library, SQLite, `openssl` (ADR-0017) |
| **Not included** | Boswas ID (operators use local API tokens), organisation hierarchy, multi-tenancy, a remote shell of any kind |

## Architecture

```
                         BOSWAS CONTROL PLANE  (boswas-cp serve)
   ┌──────────────────────────────────────────────────────────────────────┐
   │ operator port :9443                     device port :8443              │
   │  dashboard (static, CSP)                 enrollment, device API         │
   │  operator API, Bearer token              mutual TLS (device CA)         │
   │  no client-certificate request           HTTPS API /api/v1 (api.py)     │
   │                                                                        │
   │ service.py: device registry · heartbeat · inventory · typed commands    │
   │             · application catalog · artifacts · policies · events       │
   │ pki.py:     server CA · device CA · TLS certificate · Ed25519 policy key│
   │ store.py:   SQLite (WAL), hash-chained events                           │
   │ sweeper.py: offline detection · command expiry                          │
   └──────────────────────────────────┬─────────────────────────────────────┘
                                      │  typed commands, signed policies
                              boswas-device-agent  (device-management/README.md)
                                      │
                              boswas-session-agent ─► boswas-winapp ─► Wine
```

The Control Plane shares the device agent's protocol code: `commands.py`,
`policydoc.py`, `privacy.py` and the message schemas, plus boswas-compat's
manifest validator. A command or policy the Control Plane accepts is exactly
one the device accepts, and every document a device sends is checked
against the same privacy allowlists before anything is stored.

## Installing and running

```sh
apt install ./boswas-control-plane_1.0~alpha3_all.deb      # creates the system user boswas-cp
sudo -u boswas-cp boswas-cp init --public-url https://cp.example:8443 \
     --server-name cp.example --server-name 10.20.0.5      # prints the first administrator's token
sudoedit /etc/boswas-control-plane/control-plane.conf      # PUBLIC_URL, LISTEN_*, OPERATOR_LISTEN_*
sudo systemctl enable --now boswas-control-plane.service
```

- **What `init` creates in `/var/lib/boswas-control-plane` (0700):**
  - two CAs (server and device);
  - the TLS server certificate for the given names;
  - the Ed25519 policy key;
  - the database and the signed `default` policy.
- **Devices pin the server CA:** `boswas-cp ca-certificate` prints it;
  distribute it to devices as `CONTROL_PLANE_CA`.
- **The service** runs as `boswas-cp`, with no capabilities and systemd
  sandboxing. It does nothing until `init` has run (`ConditionPathExists`).
- **Two ports, disjoint APIs:**
  - **device port:** asks for an optional client certificate from the
    device CA; serves enrollment, the device endpoints and installer
    downloads;
  - **operator port:** never asks for a client certificate, so browsers
    show no certificate prompt; serves the dashboard and the operator
    endpoints;
  - both answer `GET /health`; any other endpoint requested on the wrong
    port is refused with `404 WRONG_PORT`;
  - restrict the operator port to the administration network with the
    firewall; with `OPERATOR_LISTEN_PORT=""` there is no remote
    administration at all (only `boswas-cp` on the server).

| Setting | Default | Meaning |
|---------|---------|---------|
| `LISTEN_ADDRESS`, `LISTEN_PORT` | `0.0.0.0`, `8443` | Device port: enrollment and the device API (mutual TLS) |
| `PUBLIC_URL` | (required) | The URL devices use (the device port) |
| `OPERATOR_LISTEN_ADDRESS`, `OPERATOR_LISTEN_PORT` | `0.0.0.0`, `9443` | Operator port: dashboard and operator API; empty port disables it |
| `DATA_DIR` | `/var/lib/boswas-control-plane` | Keys, database, installers |
| `DASHBOARD_DIR` | `/usr/share/boswas-control-plane/dashboard` | Static dashboard |
| `DEVICE_CERT_DAYS` | `365` | Device certificate validity |
| `OFFLINE_AFTER_SECONDS` | `180` | Offline after `max(this, 3 × heartbeat)` |
| `MAX_UPLOAD_MB` | `4096` | Installer upload limit |

### Administration (`boswas-cp`, on the server)

```
boswas-cp operator add NAME --role viewer|operator|admin    # prints the API token once
boswas-cp token create [--profile P] [--policy NAME] [--ttl-hours N] [--max-uses N] [--allow-ephemeral]
                       [--device DEVICE_ID]
boswas-cp artifact add setup.exe
boswas-cp catalog add app.json [--installer setup.exe]
boswas-cp policy publish policy.json
boswas-cp device list | retire DEVICE_ID
boswas-cp events list | verify
```

Reference: `boswas-cp(1)`.

## Authentication and roles

- **Devices:**
  - a client certificate issued by the device CA at enrollment;
  - it must be the certificate the registry holds for the device in the
    request path, and the device must be active;
  - re-enrollment revokes the old certificate; retirement revokes the device;
  - an enrolled device can only re-enroll with a token issued for its ID
    (`token create --device`); any other token is refused with
    `DEVICE_ALREADY_ENROLLED`.
- **Operators:**
  - `Authorization: Bearer bcp_<id>_<secret>`;
  - tokens are stored as scrypt hashes and shown once;
  - repeated failures from one address are throttled (HTTP 429). The
    limit is per address, so many failures behind one NAT also delay valid
    operators there for up to a minute; it protects the server from scrypt
    floods, since tokens themselves are not guessable.

  | Role | May |
  |------|-----|
  | viewer | Read everything |
  | operator | Also create and cancel commands |
  | admin | Also manage the catalog, policies, enrollment tokens, operators and devices |

- **Future identity provider:** `auth.OperatorAuthenticator` is the
  attachment point. Boswas ID is not implemented.

## API (`/api/v1`)

Requests and responses are JSON; errors are `{"error": {"code", "message"}}`.

### Device endpoints (device port, mutual TLS)

| Method and path | Purpose |
|-----------------|---------|
| `POST /enroll` | One-time token and CSR in; device certificate and pinned policy key out (no client certificate needed) |
| `POST /devices/{id}/heartbeat` | Liveness. Returns the current policy version, pending commands, next interval, inventory request |
| `PUT /devices/{id}/inventory` | Allowlisted inventory |
| `POST /devices/{id}/status`, `POST /devices/{id}/compliance` | Posture (TELEMETRY_POLICY=security) |
| `POST /devices/{id}/events` | Device events |
| `GET /devices/{id}/commands/pending` | Queued commands (become SENT; delivered again until acknowledged) |
| `POST /devices/{id}/commands/{cid}/ack` | ACKNOWLEDGED or RUNNING |
| `POST /devices/{id}/commands/{cid}/result` | SUCCEEDED, FAILED or EXPIRED |
| `GET /devices/{id}/policy` | The signed policy assigned to the device |
| `GET /artifacts/{sha256}` | An installer of a catalogued application |

### Operator endpoints (operator port, Bearer token)

| Method and path | Role |
|-----------------|------|
| `GET /health` | public (both ports) |
| `GET /me`, `GET /summary` | viewer |
| `GET /devices`, `GET /devices/{id}`, `/status`, `/inventory`, `/applications`, `/commands`, `/events` | viewer |
| `PATCH /devices/{id}` (name, profile, policy), `POST /devices/{id}/retire` | admin |
| `POST /devices/{id}/commands`, `POST /devices/{id}/commands/{cid}/cancel` | operator |
| `GET /commands`, `GET /events`, `GET /applications`, `GET /artifacts`, `GET /policies[/{name}]` | viewer |
| `POST /applications`, `DELETE /applications/{id}`, `POST /artifacts`, `POST /policies` | admin |
| `GET /events/verify`, `GET/POST /enrollment-tokens`, `POST /enrollment-tokens/{id}/revoke`, `GET/POST /operators`, `POST /operators/{name}/disable` | admin |

## Typed commands

```http
POST /api/v1/devices/{id}/commands
{"type": "INSTALL_APPLICATION", "application_id": "com.example.app", "ttl_seconds": 86400,
 "idempotency_key": "rollout-42"}
```

- **Allowed fields:** `type`, `application_id`, `version` (UPDATE_AGENT),
  `ttl_seconds` (60 s to 7 days, default 24 h) and `idempotency_key`.
  Anything else is refused.
- **Payloads come from the Control Plane:**
  - the catalog for INSTALL/UPDATE;
  - the device's assigned policy for APPLY_POLICY.
- **Validation:** the device agent's code validates every command.
- **Refused:** application commands for unknown or non-installable catalog
  entries, and types the device's policy does not allow.
- **Idempotent:** the same idempotency key, or the same pending operation,
  returns the existing command.

Lifecycle:

```
QUEUED ─► SENT ─► ACKNOWLEDGED ─► RUNNING ─► SUCCEEDED | FAILED
  │         └──────────────────────────────► EXPIRED   (expiry passed before a result)
  └► CANCELLED (only while queued)
```

The sweeper:

- expires undelivered and unacknowledged commands at their expiry;
- marks commands still RUNNING a day after expiry as `FAILED` (`NO_RESULT`).

There is no command type that carries a shell command, script, program path
or code (ADR-0019). The tests check that the API refuses `EXECUTE_SHELL_COMMAND`
and untyped fields, and that a device rejects such a command even when it is
written straight into the database.

## Application catalog

- **Entries:** compatibility manifests (format v1) validated by boswas-compat's
  validator. Each must pin its installer's SHA-256, and that installer must
  have been uploaded (`POST /artifacts`).
- **Uploads:** stored content-addressed. The Control Plane reads the
  installer's type and PE or MSI architecture.
- **Support** is computed:

  | `support` | Meaning |
  |-----------|---------|
  | `SUPPORTED` | x86_64, no missing components: installable |
  | `UNSUPPORTED_ARCHITECTURE` | x86 (32-bit): listed, **never installable** |
  | `MISSING_DEPENDENCIES` | Needs runtime components or winetricks verbs Boswas does not provide |

- **64-bit only:**
  - a manifest that declares x86_64 for a 32-bit installer is refused
    (`ARCHITECTURE_MISMATCH`);
  - the dashboard shows "x86 / 32-bit (not supported)" for such entries.

## Policies

- **Versions:** `POST /policies` publishes a new version of a named policy
  (sequence + 1, issued now), validated and signed with the Ed25519 policy
  key.
- **Assignment:** devices use the latest version of the policy assigned to
  them (`default` unless a token or an administrator chose another).
- **Delivery:** the heartbeat tells a device a newer version exists; the
  device fetches it, verifies it against the key pinned at enrollment and
  applies it (ADR-0020).
- **Contents:**
  - WinCompat rules: allowed catalog statuses, unlisted applications and
    their sandbox, installer size limit, blocked and allowed application
    IDs;
  - agent settings: heartbeat and inventory intervals, allowed command
    types;
  - agent updates (manual or managed).
- **Validation:** `require_apparmor` must be true; invalid policies are
  refused when published.

## Events and audit

- **Event types:**
  - device lifecycle: `DEVICE_REGISTERED`, `DEVICE_ONLINE`, `DEVICE_OFFLINE`;
  - applications: `APPLICATION_INSTALLED`, `APPLICATION_REMOVED`,
    `APPLICATION_LAUNCHED`, `APPLICATION_BLOCKED`, `APPLICATION_REPAIRED`;
  - policy and commands: `POLICY_UPDATED`, `COMMAND_CREATED`,
    `COMMAND_COMPLETED`, `COMMAND_FAILED`, `COMMAND_CANCELLED` (an
    operator cancelled a queued command);
  - security: `SECURITY_EVENT` (refused enrollments, rejected policies and
    commands, retirements, maintenance changes);
  - administration: `CATALOG_UPDATED`, `ARTIFACT_UPLOADED`,
    `ENROLLMENT_TOKEN_CREATED`, `OPERATOR_CHANGED`, `DEVICE_UPDATED`,
    `DEVICE_RETIRED`.
- **What each event records:** the actor (an operator, a device or the
  system), the device and the application.
- **Details are redacted:** no token, secret, password, private key or
  credential field is ever stored.
- **Hash chain:** every event stores the SHA-256 of its predecessor and its
  own content; `GET /events/verify` and `boswas-cp events verify` detect
  edited or removed rows.
- **Known limit:** truncating the newest events needs an external anchor to
  detect (planned with release signing, M9).

## Dashboard

`https://<control-plane>:9443/` (the operator port):

- **Pages:**
  - overview (devices online/offline, applications, running applications,
    pending and failed commands, policy status, security events);
  - devices with details (identity, OS, agent, architecture, heartbeat,
    applications, inventory, commands, events, policy);
  - applications, policies, commands and events;
  - enrollment tokens and operators.
- **Remote actions** on a device's applications create audited typed
  commands; there is no terminal or free-text command anywhere.
- **Hardening:**
  - a strict Content-Security-Policy (no inline script or style, nothing
    from other origins);
  - the operator token is kept in session storage only.

## Tests

| Suite | What it proves |
|-------|----------------|
| `control-plane/tests/test_cp_service.py` (26) | Enrollment (single-use, expiring, revocable and device-bound tokens; forced subject; client-auth only; refusals audited; secrets hashed); registry, re-enrollment and retirement; heartbeat and offline sweep; privacy of device documents; typed commands, lifecycle, expiry, cancellation, idempotency, policy limits; 32-bit catalog entries; policies; policy status and security events in the summary; retired devices read-only; hash chain; operators and roles |
| `control-plane/tests/test_cp_integration.py` (8) | The real device agent against the real Control Plane over mutual TLS: enrollment, heartbeat, inventory, policy, commands, a remote install with the installer downloaded over mutual TLS, a rogue command rejected by the device, outage and recovery, retirement, authentication and authorisation, device and operator ports serving disjoint APIs, HTTP hygiene |
| `control-plane/tests/test_dashboard.py` (15) | Dashboard: CSP-compatible files, no inline code, token in session storage, every API path exists |
| `tests/device/test_device_runtime.sh` | In the image with real Wine, and in the QEMU VM under AppArmor: the full device round trip |
