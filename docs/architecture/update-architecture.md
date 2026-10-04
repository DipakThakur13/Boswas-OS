# Update architecture

Employee devices must never follow arbitrary upstream changes directly. The
target flow:

```
Debian upstream (deb.debian.org, security.debian.org)
        │
        ▼
Boswas Intake       mirror/snapshot of Debian + Boswas packages
        │
        ▼
Automated tests     ./build.sh && ./test.sh (boot, security, packages, WinCompat)
        │
        ▼
Boswas QA repo      channel "qa": platform/security team devices (ring R1)
        │
        ▼
Human approval      change review, CVE triage, sign-off
        │
        ▼
Boswas Stable repo  channel "stable": employee devices (rings R2/R3)
        │
        ▼
Employee devices    APT + Boswas agent reconcile to the approved state
```

## Channels

| Channel | Ring | Audience | Rule |
|---------|------|----------|------|
| `dev` | R0 Lab | OS engineering, disposable devices | Every build; may break |
| `qa` | R1 Pilot | Platform and security team | Builds that passed automated tests |
| `stable` | R2 Standard / R3 Critical | Employees; sensitive systems (manual approval) | Promoted only after human approval |

A device's channel is `CHANNEL` in `/etc/boswas/update.conf`. The image
default comes from `BOSWAS_CHANNEL` in `config/boswas/release.conf`.

## v1 alpha (interim)

No Boswas repository exists yet. Devices therefore:

1. use Debian's archive for APT sources, as set by the installer;
2. apply **Debian security updates only**, automatically through
   unattended-upgrades (`boswas-security`: `21boswas-periodic`,
   `52boswas-unattended-upgrades`), without automatic reboots;
3. receive everything else (point releases, Boswas packages) only by
   installing a newer image or through administrator action.

## Repository and signing-key placeholders

- `config/apt/boswas.sources.example`: a **disabled** template showing how
  devices will consume the Boswas repository. It is not installed anywhere.
- Repository signing:
  - Boswas will sign repository metadata (`InRelease`) with a dedicated
    OpenPGP key.
  - Only the **public** key is distributed, as a keyring file shipped by a
    future `boswas-archive-keyring` package and referenced with `Signed-By:`.
  - The **private** key is never stored in Git (`.gitignore` blocks common
    key file names, and `test.sh` scans for private key material). Its
    custody (HSM or offline signing host, who can sign, rotation) is a
    decision to freeze before Phase 7.
- Promotion: the same package files move from `qa` to `stable`. Nothing is
  rebuilt during promotion, so `stable` contains exactly what was tested.

## Rollback

- **Packages:** every promoted repository state is kept. Rolling back means
  republishing the previous known-good state, which devices then reconcile
  to. Nobody improvises on employee machines.
- **Images:** every ISO has a manifest (`build/manifest/<build-id>.json`)
  listing the exact package set. A device can be reinstalled from the last
  known-good image, then re-enrolled.
