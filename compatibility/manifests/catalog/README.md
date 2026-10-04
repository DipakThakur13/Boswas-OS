# System compatibility catalog

Manifests of Windows applications validated by Boswas. Every `<id>.json`
file here ships in `boswas-compat` as
`/usr/share/boswas/compat/manifests/<id>.json` (the "system" catalog layer).

The catalog is empty until the first business applications have been
validated. To add one:

1. Write the manifest (format: `../schema/manifest-v1.schema.json`, guide:
   `docs/compatibility/README.md`). `tested` and `approved` manifests must pin
   the installer's SHA-256.
2. Validate it: `boswas-winapp manifest validate <id>.json` (the static tests
   validate every file here).
3. Record the test evidence (date, OS version, tester) in `tested`.

Device administrators add or override manifests in
`/etc/boswas/compat/manifests/` ("local" layer) without rebuilding anything.
