# Windows application compatibility (WinCompat)

**Goal:** approved Windows applications run on Boswas OS without a Windows
installation. **Non-goal:** universal compatibility with every Windows
executable.

## v1 alpha state

- **Wine 10.0** (Debian 13): packages `wine` and `wine64`, which handle
  **64-bit** Windows applications.
- **No 32-bit Windows support.** Debian's `wine32` is an i386 package and
  needs i386 multiarch, so it is not installed. Many installers are still
  32-bit, which makes this the first Phase 6 decision:
  - **Option A:** enable i386 multiarch and install `wine32`. That adds
    roughly 300 MB and doubles the library attack surface.
  - **Option B:** a WoW64-mode Wine build, packaged by Boswas.
- **Isolation building blocks:** Flatpak and bubblewrap are installed, with
  no Flatpak remote configured, and AppArmor is active.
- **Not yet built:** `boswas-winapp`, compatibility manifests and per-app
  prefixes.

## Phase 6 design

```
Windows application
   │
Boswas WinCompat runtime   boswas-winapp, manifests, per-app profiles
   │
Wine (+ DXVK / VKD3D-Proton only where validated)
   │
Debian user space and kernel
```

- **One prefix per application:**
  `~/.local/share/boswas/winapps/<app-id>/prefix`. No shared global prefix
  and no global DLL overrides.
- **Never root.** `boswas-winapp install <installer.exe>` validates the
  installer (type, size, optional hash or signature in the manifest), then
  runs it as the user inside the new prefix.
- **Manifests.** Each approved application has a manifest in
  `compatibility/manifests/` covering:
  - the Wine version
  - dependencies, installed from legally redistributable sources only
  - Windows version and environment settings
  - a status (`gold` / `silver` / `bronze` / `unsupported`) and notes
- **Records.** Each installation is recorded locally (later reported to the
  Control Plane) and gets a `.desktop` launcher.
- **Licensing.** No Microsoft components are downloaded or redistributed
  unless Boswas Group holds the rights.
- **Fallback.** Unsupported applications go to a managed Windows VM or
  remote application, not to an ad-hoc Wine setup.

Example manifest (planned format):

```json
{
  "id": "com.example.app",
  "name": "Example Application",
  "platform": "windows",
  "runtime": "wine",
  "wineVersion": "10.0",
  "architecture": "win64",
  "installer": {"sha256": "...", "silentArgs": "/S"},
  "dependencies": [],
  "status": "tested",
  "notes": ""
}
```
