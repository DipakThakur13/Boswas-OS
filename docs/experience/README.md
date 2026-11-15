# The Boswas OS experience

Boswas OS presents one product identity, from the boot menu to the
desktop. From the boot screen on, what the user sees is drawn by Boswas
components: the boot screen, the login screen, the desktop shell, the
window frames and the lock screen. They share one design system, **Boswas
UI** ([desktop/ui](../../desktop/ui/README.md)): the Prism palette, an 8 px
window radius, a 4 px grid, and the Inter typeface. The official mark keeps
its gold and silver, and brand lettering stays in Orbitron.

Design rule: **Boswas owns the desktop layer, Debian stays the system
layer**.

- The desktop is the package `boswas-desktop`: the compositor
  `boswas-compositor` (in place of KWin) and the shell `boswas-shell` (in
  place of Plasma's shell, screen locker and the SDDM login screen). The
  boot screen is `boswas-bootsplash` (in place of Plymouth). KDE Plasma,
  KWin, SDDM and Plymouth are not installed.
- Applications are unchanged. Dolphin, Konsole, KWrite, Okular, Gwenview,
  Ark, KCalc, Firefox ESR and the Boswas applications run as ordinary
  Wayland clients; KDE applications take the Boswas Prism colours and icons
  from system-wide defaults.
- Package management, the kernel, the boot chain (shim, GRUB, Secure Boot)
  and Debian's archive stay unmodified.
- What could not be changed safely is listed in
  [debian-references.md](debian-references.md).

**Status.** The native desktop is new. It is tested in the development
sandbox (unit tests, end-to-end tests of the compositor with real Wayland
clients on its headless back end, a nested run with the GLES renderer). It
has not yet run on real hardware, and the image build and the boot test
with it are still to be done. [Known gaps](#known-gaps) lists what this
first version does not do.

## Identity

| Surface | What users see |
|---------|----------------|
| Boot menu (USB/DVD) | Boswas OS mark, Orbitron wordmark, "Boswas OS v1 Alpha - Live session" (default), "Install Boswas OS" |
| Boot, shutdown, restart, disk unlock | The Boswas boot screen: the mark, the BOSWAS OS wordmark and a progress arc on a near-black background; for encrypted disks a passphrase field with one dot per character, a Caps Lock hint and the last error |
| Login screen | The Boswas login screen: clock, user name and password fields over the Boswas background. If graphics cannot start, a text login takes its place |
| Desktop | The Prism background (or the preset's wallpaper), the panel with the Boswas mark, Boswas Prism icons |
| Lock screen | Clock, the user's name and a password field; applications are neither shown nor given input |
| Control Center > About Boswas OS, `hostnamectl`, console | "Boswas OS v1 Alpha" (`/etc/os-release`: `NAME="Boswas OS"`, `ID=boswas`, `ID_LIKE=debian`); the desktop is shown as "Boswas Desktop" with its version |
| Terminal | "Welcome to Boswas OS" summary and the Boswas prompt |
| Live session | User "Boswas OS Live"; "Live session" in Control Center and the terminal |

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

`boswas-shell` draws everything that is not an application window. The
compositor starts it over a trusted connection and restarts it if it
exits; open windows are not affected.

- **Background:** drawn in code from the Prism palette, exact at every
  resolution. A preset replaces it with the preset's wallpaper.
- **Panel:** 48 px along the bottom edge of each screen.
  - Left: the launcher button (the Boswas mark).
  - Middle: pinned applications (Control Center, Files, Terminal, Browser,
    Compatibility Manager) and the windows of the current workspace. A
    click activates a window, or minimises it when it is already active; a
    bar under the icon marks running and active windows.
  - Right: network status, battery status (on machines with a battery),
    the time and the date. These are indicators read from the kernel's
    files; they are not menus.
- **Launcher:** opens with the Super key (tap it) or the panel button.
  - Search matches installed applications, ranked by how often they were
    started. It is local: nothing is sent anywhere.
  - Below it, a grid of all applications (Windows applications included, as
    the menu entries `boswas-winapp` creates).
  - Lock, Log out, Restart and Shut down. Restart, Shut down and Log out
    ask for a second click.
  - Keys: type to search, arrow keys to move, Enter to start, Escape to
    close.
- **Presets:** ten Boswas OS presets
  ([presets](../../desktop/presets/README.md)), chosen in Control Center >
  Personalization or with `boswas-preset`. On the native desktop a preset
  sets light or dark for the shell and the window frames, the wallpaper,
  the colours of KDE applications and the Konsole profile. A new user
  starts with the Boswas Prism look, which is not one of the ten;
  `boswas-preset reset`, or "Reset to Boswas Default" in Control Center,
  returns to it.
- **Icons:** the Boswas Prism icon pack
  ([icons](../../desktop/icons/README.md)): applications, places, devices,
  file types, and the symbolic status and action icons. Everything it does
  not draw comes from Breeze.

## Windows and shortcuts

The compositor draws the window frames: a 32 px title bar with the title
and minimise, maximise and close buttons, rounded corners and a shadow.
Applications that draw their own frame (GTK applications, Firefox) keep it.

| Keys | Action |
|------|--------|
| Super (tap) | Open or close the launcher |
| Super+L | Lock the session |
| Alt+Tab, Alt+Shift+Tab | Switch windows, most recently used first |
| Alt+F4 | Close the window |
| Super+Up | Maximise |
| Super+Down | Restore; minimise a floating window |
| Super+Left, Super+Right | Snap to the left or right half |
| Super+F11 | Fullscreen |
| Super+1 ... Super+4 | Switch to workspace 1 to 4 |
| Super+Shift+1 ... 4 | Move the window to that workspace |
| Super+E | File manager (Dolphin) |
| Ctrl+Alt+T | Terminal (Konsole) |
| Ctrl+Alt+F1 ... F12 | Switch virtual terminal |

With the pointer:

- drag a title bar to move a window; drag it to the top edge to maximise,
  to a side for that half, to the outer quarter of a side for that corner.
  A preview shows where the window will go;
- double-click a title bar to maximise; drag a border to resize;
- Super+left drag moves and Super+right drag resizes from anywhere on a
  window.

Shortcuts use the key's position on the Latin layout, so they work the
same while another keyboard layout is active. Super+L and the virtual
terminal keys work even when an application (a remote desktop, a virtual
machine) has taken the other shortcuts. The shortcuts are fixed in this
version; there is no shortcut editor.

There are four workspaces. The panel shows the windows of the current one;
there is no workspace switcher on the panel yet.

## Lock, login and boot

- **Lock screen.** The compositor owns the lock. Super+L, the launcher's
  Lock button, ten minutes without input, suspend and hibernate lock the
  session: every screen is blanked and input is taken away from
  applications first, then the lock screen is started. It checks the
  password through PAM. If the lock screen crashes, the screens stay blank
  and it is started again; nothing but a correct password unlocks. A video
  player or presentation that asks the compositor to stay awake holds the
  idle lock off while it runs.
  - An administrator sets the timeout in `/etc/boswas/compositor.toml`. A
    user may shorten it, not lengthen or disable it.
  - `loginctl lock-session` is not handled yet; scripts use
    `boswas-compositor --send lock`.
- **Login screen.** greetd runs the Boswas login screen on the first
  virtual terminal: user name, password, Enter. After a successful login it
  starts `boswas-session`. If the graphical login cannot start, greetd's
  text login is used, so there is always a way to sign in. The live session
  signs in automatically.
- **Boot screen.** `boswas-bootsplash` shows the mark and a progress arc
  from the initramfs to the login screen, the passphrase prompt of an
  encrypted disk, and the shutdown and restart screens. Escape switches
  between the boot screen and the text console. `nosplash` on the kernel
  command line turns it off
  ([packages/boswas-bootsplash](../../packages/boswas-bootsplash/README.md)).

## Settings

| What | Where |
|------|-------|
| Preset (light or dark, wallpaper, application colours, terminal profile) | Control Center > Personalization, or `boswas-preset apply <id>`; recorded in `~/.config/boswas/appearance.toml` |
| Keyboard layout and repeat, pointer (tap to click, natural scrolling, speed, cursor), display scale (1.0 to 2.0), lock timeout | `~/.config/boswas/compositor.toml`; system-wide in `/etc/boswas/compositor.toml`. `boswas-compositor(1)` lists every key. `boswas-compositor --send reload-config` applies keyboard, appearance, cursor and lock settings at once; the rest applies at the next login |
| Wi-Fi and other connections | Control Center > Network shows the state and switches Wi-Fi on and off; a new network and its password: `nmtui` in the terminal |
| Volume, output and input devices | `pavucontrol-qt` (PulseAudio Volume Control) |
| Software, updates, security, Windows and Android applications | Control Center |

Example, a user's `~/.config/boswas/compositor.toml`:

```toml
[keyboard]
layout = "us,in"
options = "grp:alt_shift_toggle"

[pointer]
natural_scroll = true

[lock]
idle_seconds = 300
```

A file that does not parse, or that contains an unknown key, is reported
in the log and ignored as a whole; the desktop then starts with the
defaults.

## Known gaps

This first version of the native desktop does not have:

- **XWayland.** Applications that only speak X11 do not run.
- **Notifications, a system tray, quick settings.** No notification
  service runs, so applications' notifications are not shown. The panel
  has no Wi-Fi, Bluetooth, volume or brightness controls.
- **Settings pages for the desktop.** Control Center pages and buttons
  that open Plasma settings modules show them as unavailable
  ([control-center.md](control-center.md)). Keyboard, pointer, display
  scale and lock timeout are set in the configuration file above. There is
  no display arrangement or resolution setting: each screen runs at its
  preferred mode, side by side.
- **Screen capture and screen sharing.** No screenshot tool and no
  screen-sharing portal; applications cannot read the screen at all.
- **Input methods** (for example for Indic, Chinese, Japanese or Korean
  text entry) and an on-screen keyboard.
- **More than one GPU**, HDR and variable refresh rate.
- **Animations.** Windows and shell surfaces appear without transitions.
- **A screen reader for the shell.** See [Accessibility](#accessibility).
- **Per-preset accents in the shell.** The panel, launcher and lock screen
  follow a preset's light or dark variant, not its accent colour.

## Boswas applications

| Application | What it is |
|-------------|------------|
| **Boswas Control Center** | The settings hub: Personalization, Network, Bluetooth, Display, Sound, Power, Storage, Applications, Windows Compatibility, Security, Users, Updates, Devices, Privacy, System, About Boswas OS (and Install Boswas OS in live sessions). It shows each area's state and offers its actions. The KDE settings modules it opened on the Plasma image are not installed on the native desktop and are shown as such ([control-center.md](control-center.md)) |
| **Boswas Security Center** | Control Center's Security page: AppArmor, Windows-app sandbox, firewall, Secure Boot, disk encryption, screen lock, automatic security updates, device agent, device policy. Each item is Secure, Warning, Attention, Error or Not available, shown with an icon and text, never colour alone |
| **Boswas Software Center** | Control Center's Applications page: Boswas, Windows and native applications, searchable. Installing new native software comes with the Boswas Store (M6); there is no unmanaged package installer |
| **Boswas Update Center** | Control Center's Updates page: automatic security updates (read-only) |
| **Boswas Compatibility Manager** | Windows applications (64-bit and 32-bit) in isolated sandboxes |
| **About Boswas OS** | Version, architecture, kernel, CPU, memory, storage, desktop (Boswas Desktop and its version), security, Windows compatibility, device agent, live or installed. Technical details include the package base |

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

- **Applications:** keyboard navigation and screen-reader support inside
  applications come from Qt and GTK, as before.
  - Boswas applications set accessible names.
  - They support full keyboard use (sidebar arrows, Tab order, Ctrl+F, F5).
  - They show status as icon plus text.
- **The shell:** the launcher, the lock screen and the login screen are
  fully usable from the keyboard. The panel, launcher, lock and login
  screens do not expose an accessibility tree yet, so a screen reader
  cannot read them; no screen reader is in the image.
- **High contrast:** Boswas Carbon is the high-contrast preset for
  applications (WCAG AAA body text). The shell uses the Prism dark or light
  theme, whose text meets WCAG AA.
- **Contrast:** the Prism colour schemes and every preset keep body text at
  4.5:1 or more; the tests check it.
- **Scaling:** one display scale from 1.0 to 2.0 for all screens
  (`[output] scale`).
- **Motion:** the desktop has no animations. The boot screen's animation
  can be turned off with `boswas.splash=static` on the kernel command line.

## Performance

No performance figures have been measured on real hardware yet. The design
choices that matter:

- **The compositor draws only when something changed** and only the
  damaged regions; an idle desktop draws nothing.
- **The shell is one process** for the background, panel and launcher. It
  redraws a surface only when its content changes (the clock once a
  minute). The default background is rendered once per screen size.
- **No new background services in the session:** Control Center, the
  presets tool and the welcome banner run only when used; the welcome
  reads files only, so terminals open instantly.
- **Boot screen:** nothing is parsed at boot (artwork and glyphs are
  embedded at build time), and a frame repaints only the items that
  changed.
- The device agent and session agent are the existing device-management
  services, unchanged.
