# boswas-security

Configuration package for the Boswas OS v1 security baseline. It changes no
file owned by another Debian package; every setting is a drop-in.

| Area | File on the device | Mechanism |
|------|--------------------|-----------|
| Firewall ruleset | `/etc/boswas/firewall/nftables.conf` | loaded by `nftables.service` through `/usr/lib/systemd/system/nftables.service.d/boswas.conf` |
| Service enable policy | `/usr/lib/systemd/system-preset/80-boswas.preset` | enables nftables; fresh openssh-server installs stay disabled; override in `/etc/systemd/system-preset/` |
| Local firewall additions | `/etc/boswas/firewall/{input,forward}.d/*.nft` | included by the ruleset |
| Kernel hardening | `/usr/lib/sysctl.d/60-boswas-hardening.conf` | systemd-sysctl; override in `/etc/sysctl.d/` |
| sudo defaults | `/etc/sudoers.d/boswas` | grants no privileges |
| Password quality | `/etc/security/pwquality.conf.d/50-boswas.conf` | pam_pwquality |
| SSH server policy | `/etc/ssh/sshd_config.d/50-boswas.conf` | only if SSH is provisioned |
| Security updates | `/etc/apt/apt.conf.d/{21boswas-periodic,52boswas-unattended-upgrades}` | unattended-upgrades, Debian security only |
| Audit rules | `/etc/audit/rules.d/50-boswas.rules` | augenrules |
| Journal retention | `/usr/lib/systemd/journald.conf.d/50-boswas.conf` | journald |
| USB policy | `/etc/boswas/usb-policy/policy.conf` | framework only in v1 alpha |

Check the state with `boswas security status`. The full baseline and its
rationale are in `docs/security/baseline.md` in the Boswas OS repository.
