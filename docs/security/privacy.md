# Privacy: what administrators can and cannot see

Boswas OS is a company-managed system. Employees are entitled to know exactly
what is collected, who can see it, and what is never collected.

## v1 alpha: nothing leaves the device

There is no device agent, telemetry, crash reporting or remote management in
v1 alpha. Nothing is sent to Boswas Group. Debian's popularity-contest is
disabled at install. The only outbound connections the OS itself makes are:

- APT and unattended-upgrades to the Debian archive
- NTP time synchronisation
- NetworkManager connectivity checks, if enabled by Debian defaults

## Data categories

| Category | Examples | Collected locally | Sent centrally | Who may see it |
|----------|----------|-------------------|----------------|----------------|
| Device management | OS build, device ID, enrollment state, hardware model, agent version | `boswas device status` (on demand) | Phase 4+, documented before release | IT/Platform admins via the Admin Console |
| Security posture | Secure Boot, TPM, encryption, firewall, AppArmor and audit state | `boswas status` (on demand) | Phase 4+ | IT/Security admins |
| Security events | Changes to identity files, sudoers, security config, kernel modules, clock | auditd log (root-only), max retention set by the journal policy | Phase 4+, policy-controlled forwarding | Security admins, access-controlled and audited |
| Application inventory | Installed packages and Windows apps | dpkg database | Phase 5+ | IT admins |
| User content | Documents, browser history, e-mail, files in /home | Never by Boswas OS | **Never** | Only the user (and company-approved backup, once defined) |
| Private user activity | Keystrokes, screen contents, command lines, file reads | **Never** | **Never** | Nobody |

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
