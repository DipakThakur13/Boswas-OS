# Boswas OS v1 security baseline

Implemented by the `boswas-security` package, the installer preseed and the
image manifest. `boswas security status` reports each control on a device.
Results are a **local self-assessment**, not remote attestation.

## Controls

| Control | v1 alpha implementation | Status check | Alpha level |
|---------|-------------------------|--------------|-------------|
| Boot chain | Debian-signed shim → GRUB → kernel. The ISO boots with Secure Boot enforced | `secure-boot` | WARN if disabled (required for production) |
| Hardware root of trust | TPM 2.0 detected and reported. Not yet used for keys | `tpm` | WARN if absent |
| Disk encryption | Installer policy: guided LVM inside LUKS2 (cryptsetup default), passphrase asked interactively | `disk-encryption` | **FAIL** if root is not encrypted, WARN on LUKS1 |
| Host firewall | nftables: inbound default drop, forward drop, outbound allowed; rate-limited drop log | `firewall` | **FAIL** if inactive |
| Mandatory access control | AppArmor (Debian kernel default LSM) with Debian's shipped profiles | `apparmor` | **FAIL** if disabled or profiles not loaded on an installed system. WARN in the live session: Debian skips profile loading on live media |
| Audit | auditd + rules for identity, privilege and security-config changes, kernel modules, time | `audit` | **FAIL** if inactive |
| Remote access | No SSH server installed. A fresh install of openssh-server is not enabled automatically (systemd preset). If provisioned: keys only, no root, 3 tries | `ssh-server` | WARN if running |
| Administrative access | Root locked by the installer. The installer-created user is in `sudo`; sudo with password, `use_pty`, 5-minute timestamp. No NOPASSWD, no hidden accounts | `root-account` (as root) | WARN if root has a password |
| Passwords | pam_pwquality: min 12 chars, 2 classes, dictionary and username checks, enforced for root | (none) | (none) |
| Screen lock | Automatic lock after 10 minutes and on resume, locked with KDE Kiosk | `screen-lock` | WARN if missing |
| Kernel hardening | kptr/dmesg restrict, no unprivileged BPF, ptrace scope 1, no kexec, restricted SysRq, no ICMP redirects. **User namespaces stay enabled** (Flatpak and browser sandboxes) | (none) | (none) |
| Updates | unattended-upgrades: daily, Debian-Security origin only, no automatic reboot | `updates` | WARN if disabled or lists >7 days old |
| Repository trust | Only Debian archive sources signed by the Debian archive keyring. live-build's unsigned boot-medium source is removed by the installer (`preseed/late_command`) | `apt-trust` | **FAIL** for any `trusted=yes` source on an installed system; INFO for the boot medium in the live session |
| Logging | Persistent journal, 1 GB / 90 days maximum | (none) | (none) |
| Removable media | Policy file and reporting only (`MODE=allow`) | `usb-policy` | INFO, not enforced |
| Service minimisation | Network-listening and unmanaged-install software pinned out of the image: KDE Connect, Discover/PackageKit, cups-browsed, openssh-server, fingerprint PAM | test suite | (none) |

Compliance states:

- `COMPLIANT`: every scored check passes.
- `COMPLIANT_WITH_WARNINGS`: no failures, but WARN or UNKNOWN results.
- `NON_COMPLIANT`: at least one FAIL. `boswas status` exits with code 1.

## Principles

- **No security through obscurity.** Every control is a documented file in
  this repository and on the device.
- **No backdoors.** No hidden administrator, shared password, embedded key or
  remote-access service. Administrative access is the explicit sudo group
  created by the person installing the device.
- **No secrets in images or Git.** `device.conf` holds identifiers only;
  credentials will live in the TPM / OS secret store (Phase 4+).
- **Untrusted installers never run as root.** Planned WinCompat tooling runs
  Windows installers as the user in per-application prefixes.
- **Upstream first.** Debian mechanisms only; no Debian file is modified (see
  ADR-0006).

## Live session

The live ISO session is a demonstration and diagnostics environment. It is
not a managed device. Upstream Debian behaviour differs there:

- **AppArmor:** profiles are not loaded (Debian's `apparmor.service`
  carries `ConditionPathExists=!/run/live/overlay/work`).
- **APT:** the boot medium's package pool is an unsigned APT source.

`boswas status` reports both instead of hiding them. live-config creates the `boswas` user (password
`live`) with passwordless sudo, and nothing is persisted. Whether this
default stays for production images is an open item (see the roadmap). The
installed system never inherits the live user.

## Known gaps (tracked in the roadmap)

- **USB storage:** not enforced.
- **AppArmor:** no Boswas-specific profiles yet (e.g. for Wine applications).
- **Secure Boot:** not enforced as a policy; the device only reports its state.
- **TPM:** not yet used to protect LUKS or device keys.
- **Release artifacts:** not yet signed.
- **systemd units:** Boswas units are not sandboxed yet (no Boswas services
  exist in v1 alpha).
