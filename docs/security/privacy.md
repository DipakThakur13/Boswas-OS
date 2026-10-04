# Privacy: what administrators can and cannot see

Boswas OS is a company-managed system. Employees are entitled to know exactly
what is collected, who can see it, and what is never collected.

## 1.0~alpha3: nothing leaves an unenrolled device

The device agent runs on every device. A device sends nothing to Boswas
Group unless an administrator configures a Control Plane in
`/etc/boswas/device.conf` and enrolls it.

**Never on any device:**

- telemetry about how people use the device;
- crash reporting;
- reporting of local application launches.

Debian's popularity-contest is disabled at install. Without enrollment, the
only outbound connections the OS itself makes are:

- APT and unattended-upgrades to the Debian archive
- NTP time synchronisation
- NetworkManager connectivity checks, if enabled by Debian defaults

An **enrolled** device additionally talks to its Control Plane over mutual
TLS, exactly as listed in "Device agent messages" below.

## Data categories

| Category | Examples | Collected locally | Sent centrally | Who may see it |
|----------|----------|-------------------|----------------|----------------|
| Device management | OS build, device ID, enrollment state, hardware model, agent version, device state | `boswas-device status`, `boswas device status` | Enrolled devices: heartbeat, inventory (see below) | Control Plane operators (viewer and above), audited |
| Security posture | Secure Boot, TPM, encryption, firewall, AppArmor and audit state | `boswas status` (on demand; the agent assesses it with each inventory) | Enrolled devices with `TELEMETRY_POLICY=security`: check results only | Control Plane operators |
| Security events | Changes to identity files, sudoers, security config, kernel modules, clock | auditd log (root-only), max retention set by the journal policy | **Not forwarded** in 1.0~alpha3 (only the agent's own management and security events) | Local administrators |
| Application inventory | Boswas packages and Windows apps | dpkg database; per-user WinCompat records (`~/.local/share/boswas/wine/<id>/app.json`) | Enrolled devices: application IDs, versions, statuses, states and installation **counts**, never user names | Control Plane operators |
| User content | Documents, browser history, e-mail, files in /home | Never by Boswas OS | **Never** | Only the user (and company-approved backup, once defined) |
| Private user activity | Keystrokes, screen contents, command lines, file reads | **Never** | **Never** | Nobody |

## Windows applications (WinCompat)

- **Local data only.** `boswas-winapp` keeps, per user and application, an
  installation record and logs of what the Windows program printed. Both
  stay in the user's home and are never sent anywhere.
- **What root sees.** Root's inventory listing (`--all-users`) reads only
  IDs, versions, statuses and states.
- **What a Windows application sees.** Only its own prefix and the folders
  its manifest grants; never the rest of the user's home.

## Device agent messages (enrolled devices only)

Every message has a closed field list
(`packages/boswas-device-agent/boswas_agent/privacy.py`, schemas in
`packages/boswas-device-agent/schemas/`). The agent checks each message
before queuing it, and the Control Plane checks it again and refuses
anything outside the list.

| Message | Content | Controlled by |
|---------|---------|---------------|
| Enrollment (once) | Device ID, certificate request, OS versions, hardware model facts (vendor, model, firmware version, CPU model, memory, TPM version, boot mode; **no serial numbers**), profile, optional device name, the one-time token | Administrator action |
| Heartbeat | Device ID, agent version, device state, applied policy version, inventory revision, number of pending results, time | `HEARTBEAT_INTERVAL` |
| Inventory | OS versions, Boswas package versions, Windows applications aggregated over users (ID, version, status, architecture, state, number of installations, number running; **no user names**), Windows runtime health; with `standard` also CPU, memory, model, firmware version and disk size | `INVENTORY_POLICY` (off, minimal, standard) |
| Status and compliance | Posture check IDs and results (`firewall: PASS`, …), compliance counts, OS versions, uptime, update channel | `TELEMETRY_POLICY` (none, security) |
| Events | Management actions and security events: remote installs, removals, launches and repairs, policy changes, policy refusals of remote commands | Always (only for management actions) |
| Command results | Command ID, status, short typed facts (application ID, version, state, booleans), error code and message; **never program output or logs** | Always |

A test fails if any other field is added. Changing these lists requires
updating this page first.

**What administrators see:** the dashboard shows exactly these documents,
the command history and the audit trail. It does not show who uses which
application: inventory is aggregated per device, and local launches are
never reported.

**Who sees what an administrator did:** every operator action is recorded
with its actor in the Control Plane's hash-chained audit trail. On the
device, `boswas-device commands` and the Compatibility Manager's activity
list show the remote commands that were run.

## Explicit non-goals

Boswas OS does **not** and will not implement:

- keystroke logging
- screen capture or screen recording
- silent inspection of personal files or browsing
- logging of the commands users type (the audit rules exclude `execve` on purpose)

## Rules for any future monitoring

Any future collection must be:

1. documented in this file **before** it ships;
2. controlled by Boswas policy (visible in `boswas policy status`);
3. limited to the stated purpose and minimal fields, with defined retention;
4. readable only by roles with a need, with every administrator access
   itself recorded in the audit trail.

## Local logs

- **Journal:** persistent, capped at 1 GB and 90 days.
- **Audit log:** `/var/log/audit`, root-only, rotated by auditd.
- **Firewall:** dropped inbound connection attempts are logged rate-limited,
  as packet headers only (no payload).
