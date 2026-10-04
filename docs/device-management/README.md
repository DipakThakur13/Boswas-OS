# Device management: device agent foundations

> **State after Milestone 1:** contracts only.
>
> | Exists | Does not exist yet |
> |--------|--------------------|
> | Data models, interfaces, JSON schemas, device identity handling, privacy guard, and the Windows-application inventory collector (`packages/boswas-device-agent/`), with unit tests | The `boswas-device-agent.service` daemon, its Debian package and any network client (Milestone 2); the Control Plane (Milestone 3) |
>
> **Nothing on a device contacts a backend.**

## Architecture

```
            Boswas Control Plane (Milestone 3)
                       ▲  HTTPS, mutual TLS (device certificate)
                       │  /api/v1/devices/...
┌──────────────────────┴───────────────────────────── Boswas OS device ─┐
│ boswas-device-agent.service (root, Milestone 2)                        │
│   ControlPlaneClient  CredentialStore  PolicyVerifier  CommandHandler  │
│   InventoryCollector ─┬─ boswas status checks (boswas-cli)             │
│                       └─ boswas-winapp --json list --all-users         │
│   /etc/boswas/device.conf   identifiers only                           │
│   /var/lib/boswas/agent/    credentials (root only; TPM later), state  │
└────────────────────────────────────────────────────────────────────────┘
```

Module map (`packages/boswas-device-agent/boswas_agent/`):

| Module | Content |
|--------|---------|
| `models.py` | `Heartbeat`, `EnrollmentRequest`, `ComplianceReport`, `DeviceCommand` and `CommandType`, `EnrollmentState`, `ComplianceState`, `ApplicationInventoryItem` |
| `interfaces.py` | `ControlPlaneClient` (plus `OfflineControlPlaneClient`), `CredentialStore`, `PolicyVerifier`, `InventoryCollector`, `CommandHandler` |
| `device_identity.py` | Device ID generation, `DeviceConfig` (read, validate, atomic write of `device.conf`) |
| `privacy.py` | Closed field allowlists for every outgoing message |
| `inventory.py` | `WindowsApplicationsCollector` (the WinCompat ↔ agent interface) |
| `user_identity.py` | Boswas ID boundary: `IdentityProvider`, `IdentityContext`, `AuthenticatedPrincipal` (interfaces only) |
| `../schemas/*.schema.json` | API v1 message schemas, shared with the Control Plane |

## Device identity

- **`DEVICE_ID`:** a random UUID version 4 from the kernel CSPRNG, created
  once per installation by the agent on first start (`ensure_device_id`).
  - **Never derived from** the hostname, a MAC address, a user name or a
    serial number.
  - **Hardware identifiers** are inventory attributes only (`HardwareFacts`).
- **Authentication:** after enrollment the device authenticates with its
  **certificate**, using mutual TLS. Its private key never leaves the
  `CredentialStore`: a root-only file first, TPM-backed later. An ID alone
  authenticates nothing; IP and MAC addresses and hostnames are never trusted.
- **Enrollment** (`POST /api/v1/devices/enroll`):
  - the device sends its ID, a PKCS#10 CSR, OS and hardware facts, and a
    **one-time enrollment token** issued by an administrator;
  - the token is never stored and never part of `repr()` or logs.

### `/etc/boswas/device.conf`

Identifiers and references only. The writer refuses secrets: PEM blocks,
quotes and shell metacharacters, and certificate paths outside
`/var/lib/boswas/agent/` or `/etc/boswas/`.

| Key | Content |
|-----|---------|
| `DEVICE_ID` | Random UUID v4 |
| `TENANT_ID` | Boswas Group tenant |
| `CONTROL_PLANE_URL` | `https://` only |
| `ENROLLMENT_STATE` | `unenrolled`, `pending`, `enrolled`, `retired` |
| `POLICY_VERSION` | Version of the applied, verified policy |
| `DEVICE_PROFILE` | Profile assigned by the Control Plane |
| `DEVICE_CERTIFICATE` | Path of the device's **public** certificate |

`boswas device status` shows exactly these keys and nothing else.

## Messages (API v1)

| Message | Endpoint | Schema |
|---------|----------|--------|
| Enrollment request | `POST /api/v1/devices/enroll` | `enrollment-request-v1` |
| Heartbeat | `POST /api/v1/devices/{id}/heartbeat` | `heartbeat-v1` |
| Compliance report | `POST /api/v1/devices/{id}/compliance` | `compliance-report-v1` |
| Command | `GET .../commands` (via heartbeat) | `device-command-v1` |

### Heartbeat contents

- device ID, agent version, OS (name, version, build, Debian version,
  kernel) and uptime;
- compliance summary: state and counts;
- policy version, and update channel and state;
- security posture: a map of check ID to status only (`firewall: PASS`,
  ...), restricted to the documented `boswas status` check IDs.

**Never sent:** file contents, keystrokes, screenshots, browsing data,
passwords, private keys or personal documents.
`privacy.check()` rejects any field outside the closed allowlist of each
message type. Adding data means changing the allowlist, the schema and
[privacy.md](../security/privacy.md) together; a unit test fails otherwise.

### Compliance states

`COMPLIANT`, `NON_COMPLIANT`, `PENDING` (policy received, not yet assessed),
`UNKNOWN`. The local `boswas status` state `COMPLIANT_WITH_WARNINGS` maps to
`COMPLIANT`, with warnings reported per check.

### Commands

`SYNC_POLICY`, `CHECK_UPDATE`, `INSTALL_UPDATE`, `RESTART`, `LOCK_DEVICE`,
`REFRESH_CONFIGURATION`. Unknown types are rejected
(`UnsupportedCommand`).

**Destructive commands (wipe, retire) are intentionally absent.** Adding one
needs a new command type, explicit authorization recorded in the audit
trail, and a separate security review. `issued_by` is an opaque Control
Plane actor reference for the audit trail, not a user identity.

## Windows application inventory

`WindowsApplicationsCollector` runs `boswas-winapp --json list --all-users`
as root and aggregates per application: ID, version, status and the number
of users who have it installed. **User names are not reported.**

## Boswas ID (deferred)

Boswas ID is **not** implemented: there is no identity provider, SSO,
OAuth/OIDC server, user directory or password synchronisation. Devices keep
their local administrator account.

`user_identity.py` defines the attachment point:

- `AuthenticatedPrincipal`: subject, issuer, display name, groups;
- `IdentityContext`: principal or `None`, device ID, method;
- `IdentityProvider`: with the only implementation, `NoIdentityProvider`,
  never asserting a user.

**Device identity ≠ user identity:** nothing may derive one from the other.

## Next (Milestone 2)

1. **Package:** `boswas-device-agent` (Debian package, `services/boswas-device/`
   unit with systemd sandboxing), which creates `DEVICE_ID` at first start.
2. **Local state:** inventory and compliance snapshots in `/var/lib/boswas/agent/`.
3. **Network:** an HTTPS client implementing `ControlPlaneClient` (mutual
   TLS), with heartbeat scheduling and back-off.
4. **Credentials:** a file-based `CredentialStore` (TPM-backed store later).
5. **Integration:** `boswas status` reports the agent; docs and privacy page
   updated before anything is sent.
