# The Boswas OS experience

Boswas OS presents one product identity, from the boot menu to the
desktop. It is built from the Boswas OS logo, the Orbitron brand typeface
and the logo's gold, on KDE Plasma's maintained components.

Design rule: **branding layer, not system layer**.

- Look and naming change only through supported mechanisms: themes,
  colour schemes, Global Themes, icon themes, splash themes, configuration
  defaults and Boswas applications.
- Package management, the kernel, the boot chain (shim, GRUB, Secure Boot),
  Debian's archive and KDE's applications stay unmodified.
- What could not be changed safely is listed in
  [debian-references.md](debian-references.md).

## Identity

| Surface | What users see |
|---------|----------------|
| Boot menu (USB/DVD) | Boswas OS mark, Orbitron wordmark, "Boswas OS v1 Alpha - Live session" (default), "Install Boswas OS" |
| Boot, shutdown, disk unlock | Boswas OS Plymouth theme: the mark and a gold spinner; a Boswas passphrase field for encrypted disks |
| Login screen | Boswas background, Boswas lockup (Breeze login form) |
| Session start | Boswas OS splash |
| Desktop | Preset wallpaper (Horizon by default), Boswas Launcher with the mark, Boswas icons |
| Lock screen | Preset lock-screen art (Breeze lock form: avatar, password, clock) |
| About this System, `hostnamectl`, console | "Boswas OS v1 Alpha" (`/etc/os-release`: `NAME="Boswas OS"`, `ID=boswas`, `ID_LIKE=debian`) |
| Terminal | "Welcome to Boswas OS" summary and the Boswas prompt |
| Live session | User "Boswas OS Live"; "Live session" in Control Center, About this System and the terminal |

## Live USB: use first, install only by choice

Write the ISO to a USB stick and boot from it. The default boot entry
starts the **live session**:

```
Power on -> Boswas boot menu -> Boswas boot splash -> Boswas OS desktop (signed in as "Boswas OS Live")
```

- **Nothing installs on its own.** No installer, installation wizard or
  web page opens. The desktop is usable at once: applications, settings,
  the terminal, the Compatibility Manager and Control Center.
- **Internal disks are not touched.** The live session does not mount,
  partition, format or write internal disks, and changes no EFI boot
  entries. The boot test verifies this bit for bit against an internal
  disk.
- **Live changes are temporary.** Files and settings live in RAM and are
  lost at shutdown. Boswas OS v1 does not offer persistent live storage:
  it would add a writable partition to the stick and change what the
  live system is.
- **Installing is a deliberate choice:**
  - from the desktop: Applications > **Install Boswas OS** (or Control
    Center > Install Boswas OS) explains the steps and offers
    **Restart to Install…**;
  - in the boot menu: choose **Install Boswas OS**.

  The installer runs as its own boot mode, because it applies the Boswas
  installation policy (full-disk encryption, administrator account, root
  locked). It writes to a disk only after its own confirmation steps.
- **Validation:**
  - automated: `tests/boot/qemu_boot_test.py`, scenario `usb` (USB stick,
    UEFI with Secure Boot, internal disk checksum) and scenario `install`
    (the installed system boots through Secure Boot);
  - manual, on real hardware: [live-usb-checklist.md](live-usb-checklist.md).

## Desktop shell

Boswas OS keeps KDE Plasma and shapes it, instead of replacing it:

- **Boswas Launcher** (`com.boswas.launcher`): KDE's maintained application
  launcher under the Boswas name and mark.
  - It has search, favourites (Control Center, Files, Terminal, Browser,
    Compatibility Manager, Calculator), recently used items, all
    applications by category (Windows applications included, as menu
    entries created by `boswas-winapp`) and power actions.
  - It opens with the Meta key.
- **Panel:** floating for the modern presets, a classic full-width panel
  for Boswas Classic. Pinned: Control Center, Files, Terminal, Browser,
  Compatibility Manager.
- **Quick settings:** Plasma's system tray (network and Wi-Fi, Bluetooth,
  volume, brightness and Night Light, battery, notifications), styled by
  the preset. Security status is in Control Center > Security.
- **Notifications, dialogs, menus:** Plasma's, in the preset's colours
  (Breeze style with the Boswas colour scheme).
- **Presets:** ten Boswas OS presets ([presets](../../desktop/presets/README.md)),
  chosen in Control Center > Personalization, with `boswas-preset`, or in
  System Settings > Global Theme.
- **Icons:** the Boswas and Boswas Light icon themes
  ([icons](../../desktop/icons/README.md)) replace the application and
  settings icons. Small symbolic icons (tray, toolbars) stay Breeze for
  legibility.

## Boswas applications

| Application | What it is |
|-------------|------------|
| **Boswas Control Center** | The settings hub: Personalization, Network, Bluetooth, Display, Sound, Power, Storage, Applications, Windows Compatibility, Security, Users, Updates, Devices, Privacy, System, About Boswas OS (and Install Boswas OS in live sessions). It explains each area and opens KDE's maintained settings modules |
| **Boswas Security Center** | Control Center's Security page: AppArmor, Windows-app sandbox, firewall, Secure Boot, disk encryption, screen lock, automatic security updates, device agent, device policy. Each item is Secure, Warning, Attention, Error or Not available, shown with an icon and text, never colour alone |
| **Boswas Software Center** | Control Center's Applications page: Boswas, Windows and native applications, searchable. Installing new native software comes with the Boswas Store (M6); there is no unmanaged package installer |
| **Boswas Update Center** | Control Center's Updates page: automatic security updates (read-only) |
| **Boswas Compatibility Manager** | Windows applications (x86_64 / 64-bit only) in isolated sandboxes |
| **About Boswas OS** | Version, architecture, kernel, CPU, memory, storage, desktop, security, Windows compatibility, device agent, live or installed. Technical details include the package base |

## Terminal

The Boswas Konsole profiles start bash with `/usr/share/boswas/terminal/bashrc`.

- **Your settings stay:** it runs your `~/.bashrc` first.
- **Welcome:** `boswas-welcome` shows the version, kernel, architecture,
  session (live or installed), AppArmor state and device agent state.
- **Prompt:** user and host in gold, the directory in silver, and a failed
  command's exit code in brackets.
- **Quiet option:** `touch ~/.config/boswas/no-welcome` turns the welcome
  off.

## Accessibility

- **Keyboard and screen readers:** keyboard navigation and screen-reader
  support come from KDE and Qt.
  - Boswas applications set accessible names.
  - They support full keyboard use (sidebar arrows, Tab order, Ctrl+F, F5).
  - They show status as icon plus text.
- **High contrast:** Boswas Carbon is the high-contrast preset (WCAG AAA
  body text).
- **Contrast in every preset:** body text meets at least 4.5:1, and gold
  selections use dark text.
- **Respected settings:** font size, scaling and "animations off" (splash
  screens included).

## Performance

- **No new background services in the session:**
  - Control Center, the presets tool and the welcome banner run only when
    used;
  - the welcome reads files only, so terminals open instantly.
- **Boot splash:** Plymouth adds no measurable boot time and is the
  standard Debian component.
- **Presets:** they use Breeze (no blur-heavy effects). Wallpapers are
  single pre-rendered images.
- The device agent and session agent are the existing device-management
  services, unchanged.
