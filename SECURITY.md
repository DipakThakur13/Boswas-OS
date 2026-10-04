# Security policy

## Reporting a vulnerability

Report suspected vulnerabilities in Boswas OS (Boswas packages,
configuration, build pipeline, images) privately to the **Boswas Security
team** through the internal security reporting channel. Do not open public
tickets.

Include:

- **Affected version.** The output of `boswas info`, or the build ID from
  `build/output/*.manifest.txt`.
- **Reproduction.** Steps and the impact.
- **Classification.** Whether it is a Boswas-specific issue or an upstream
  Debian package issue.

Upstream Debian vulnerabilities are tracked by the Debian Security Team
(https://security-tracker.debian.org/). Boswas OS receives their fixes
through automatic security updates. Boswas Security coordinates if an
upstream issue needs a Boswas-side mitigation.

## Supported versions

| Version | Supported |
|---------|-----------|
| v1 alpha | Pilot devices only. Fixes ship as new images and package versions |

## Security principles for contributors

- **No credentials, tokens or private keys in the repository or images.**
  `test.sh` scans for key material. Repository signing keys stay in the
  defined key-custody process.
- **No hidden accounts, backdoors or undocumented remote access.**
  Administrative access is explicit and auditable.
- **Untrusted installers never run as root.**
- **No security through obscurity.** Controls are documented in
  `docs/security/baseline.md`.
- **Privacy commitments in `docs/security/privacy.md` are binding.** No
  keystroke logging, no screen capture, no silent inspection of personal
  content.
- **Prefer Debian's mechanisms.** Never modify files owned by Debian
  packages; ship drop-ins.

## Baseline

See `docs/security/baseline.md` for the implemented controls, how
`boswas status` reports them, and the known gaps of v1 alpha.
