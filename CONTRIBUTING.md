# Contributing to Boswas OS

## Ground rules

1. **Debian is upstream.** Do not vendor or fork Debian packages. If a change
   to Debian behaviour is unavoidable, use a Boswas package, configuration
   drop-in or (as a last resort) a documented patch under `debian/patches/`.
2. **Keep Boswas changes modular.** Each concern lives in its package
   (`boswas-os`, `boswas-cli`, `boswas-branding`, `boswas-security`, and
   later ones). Never edit a file owned by another Debian package. Use
   drop-in directories (`*.d/`), systemd drop-ins, XDG config dirs and presets.
3. **Document every added package** in
   `docs/architecture/package-manifest.md`, with the reason.
4. **Never commit secrets** (credentials, tokens, private keys). Never add a
   hidden or default administrator account.
5. **Least privilege, minimal telemetry.** Anything that collects data must be
   documented in `docs/security/privacy.md` first.
6. **Prefer reproducible, signed artifacts.**

## Workflow

```sh
./test.sh --packages-only   # fast: static checks, package build, lintian, unit tests
./build.sh                  # full image
./test.sh                   # everything, including the QEMU boot test
```

- Lintian errors and warnings fail the test suite. Overrides need a comment
  explaining why.
- Version bumps:
  - change `config/boswas/release.conf`
  - add a `debian/changelog` entry to every package
  - update `boswas_cli/__init__.py`

  `test.sh` verifies they match.
- Significant decisions get an ADR in `docs/architecture/decisions.md`.
- Keep files LF-terminated (`.gitattributes`, `.editorconfig`). Windows
  checkouts are supported.

## Commit messages

Imperative summary line, followed by what changed and why. Reference the
roadmap phase where relevant, e.g. `security: enforce USB storage policy (Phase 3)`.
