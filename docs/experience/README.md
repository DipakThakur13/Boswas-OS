# The Boswas OS experience

Boswas OS presents one product identity, from the boot menu to the
desktop. From the boot screen on, what the user sees is drawn by Boswas
components: the boot screen, the login screen, the desktop shell, the
window frames and the lock screen. They share one design system, **Boswas
UI** ([desktop/ui](../../desktop/ui/README.md)): the Fluent design
language (the layout, geometry, materials and motion that Windows 11
users know) with Boswas's own colours (the Prism palette), icons (Boswas
Prism) and typeface (Inter). The Boswas OS mark, in the palette's blues
with a warm dot (ADR-0047), is the Start button; brand lettering is in
Inter.

Design rule: **Boswas owns the desktop layer, Debian stays the system
layer**.

- The desktop is the package `boswas-desktop`: the compositor
  `boswas-compositor` (in place of KWin) and the shell `boswas-shell` (in
  place of Plasma's shell, screen locker and the SDDM login screen). The
  boot screen is `boswas-bootsplash` (in place of Plymouth). KDE Plasma,
  KWin, SDDM and Plymouth are not installed.
- The system applications are Boswas's own too (`boswas-apps`,
  ADR-0040): Files, Terminal, Text Editor, Calculator, Photos, Document
  Viewer, Archive Manager, Media Player, Camera, Clock, Calendar, Snipping
  Tool and Weather (ADR-0046). They,
  Control Center and the Compatibility Manager take their look from the
  Boswas Qt toolkit (ADR-0039). KDE's applications are no longer in the
  image.
- Other applications are unchanged. Firefox ESR and any Wayland
  application run as ordinary clients; a KDE application someone installs
  takes the Boswas Prism colours and icons from system-wide defaults.
  Applications that only speak X11 run through Xwayland (ADR-0034).
- Package management, the kernel, the boot chain (shim, GRUB, Secure Boot)
  and Debian's archive stay unmodified.
- What could not be changed safely is listed in
  [debian-references.md](debian-references.md).

**Status.** The native desktop is new. It is tested in the development
sandbox: unit tests, end-to-end tests of the compositor with real clients
on its headless back end, and tests of the service backends against
stand-ins of the system daemons. It has not yet run on real hardware or
against the real system services, and the image build and the boot test
with it are still to be done
([what is verified](../architecture/verification.md)).
[Known gaps](#known-gaps) lists what it does not do.

The system applications and the settings are described under
[Boswas applications](#boswas-applications) and [Settings](#settings).
They have their tests and have not run on the desktop yet.

## Identity

| Surface | What users see |
|---------|----------------|
| Boot menu (USB/DVD) | Boswas OS mark, Inter wordmark, "Boswas OS v3 Alpha - Live session" (default), "Install Boswas OS" |
| Boot, shutdown, restart, disk unlock | The Boswas boot screen, drawn from vector outlines at the display's size (ADR-0050): the mark assembles itself on a dark background lit softly in the brand's blues, with no name written, and a progress ring turns close under it. For encrypted disks the mark glides up and the prompt slides in: "Unlock your disk", the disk's name and a text box with one dot per character, a Caps Lock hint and a line for a rejected passphrase. Shutdown and restart show the mark, the ring and one line of text |
| Login screen | The Boswas login screen: first the clock over the background, then, after a key or a click, the people who can sign in and a password field. Another user name can be typed. A power button offers sleep, restart and shut down (the lock screen has none). If graphics cannot start, a text login takes its place |
| Desktop | The background (or the preset's wallpaper) with the desktop icons (the Trash and the files of the desktop folder), the taskbar with the Boswas mark as the Start button and the widgets button at its left end, Boswas Prism icons |
| Lock screen | First the clock, with the weather of the home place chosen in Weather in the lower left corner (ADR-0051), then, after a key or a click, the user's name and a password field; applications are neither shown nor given input, and no notifications appear |
| Control Center > System > About, `hostnamectl`, console | "Boswas OS v3 Alpha" (`/etc/os-release`: `NAME="Boswas OS"`, `ID=boswas`, `ID_LIKE=debian`); the desktop is shown as "Boswas Desktop" with its version |
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
    Center > System > Install Boswas OS) explains the steps and offers
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
exits; open windows are not affected. `boswas-shell(1)` is the reference.

- **Background:** drawn in code from the palette, exact at every
  resolution. A preset replaces it with the preset's wallpaper.
- **Desktop icons** (ADR-0060): on the primary monitor's background, the
  files and folders of your desktop folder (`~/Desktop`, or the one
  `~/.config/user-dirs.dirs` names) and the Trash; Home and This device
  can be added. They sit on a grid from the top left, column by column.
  - A click selects, Ctrl and Shift add, a drag over the background
    selects with a band; a double-click or Enter opens (folders, Home,
    This device and the Trash in Files, files with their default
    application); a drag moves icons, onto a folder into it and onto the
    Trash to the Trash; files dragged in from other programs are copied
    into the folder.
  - Keys: F2 renames in a field over the label, Delete moves to the Trash,
    Ctrl+C, Ctrl+X and Ctrl+V copy, cut and paste (Files understands the
    same clipboard), Ctrl+A selects all, Ctrl+Shift+N makes a folder, F5
    reads the folder again, the arrow keys move and typing goes to an icon
    by its name; the Menu key or Shift+F10 opens the menu.
  - A right-click on the background: **View** (large, medium or small
    icons, auto arrange, align to grid, show desktop icons), **Sort by**
    (name, size, item type, date modified), Refresh, Paste, **New** (a
    folder or a text document), Open in Terminal, Display settings,
    Personalise and All settings. On icons: Open, Open with, Cut, Copy,
    Rename, Delete and Properties (in Files).
  - A launcher (`.desktop` file) on the desktop starts only when it is
    your own file and allowed to run (Files: Properties > "Allow running
    this file as a program"); otherwise it opens as text and a notice
    says why. Pictures show a thumbnail the shell makes and keeps in
    memory.
  - Where you put icons is kept (`~/.local/state/boswas/desktop-layout.json`)
    while auto arrange is off. Settings > Personalization > Themes >
    Desktop icons holds the same choices and the system icons.
- **Taskbar:** 48 px along the bottom edge of each screen.
  - In the centre, as one group: Start (the Boswas mark), Task View, and
    the pinned and running applications. A new user finds Files, Firefox
    ESR and the Software Center pinned, as Windows pins its file manager,
    browser and store (ADR-0053). A click on an application shows
    its window, starts it, or goes through its windows; the secondary
    button opens its menu (the application's own actions, then "New window
    of" or, when it is not running, Open; pin or unpin; close); the middle
    button opens another window of a running application and starts one
    that is not running. Resting the pointer on a running application
    shows its windows. A mark under the icon shows running and active
    applications.
  - **New window** (ADR-0061, ADR-0062): "New window of", the middle
    button and Super+Shift+1 to 9 run the application's own "new window"
    action: Files, Terminal, Text Editor, Photos, Document Viewer,
    Calculator and Archive Manager (`--new-window`), Firefox
    (`new-window`) and any program whose desktop entry has one. Without
    such an action the application is started again, and one that keeps a
    single window (Clock, Calendar, Media Player, Weather, Task Manager,
    Camera, Snipping Tool) brings it forward.
  - At the left end the **widgets button** (ADR-0059): the weather icon
    and the temperature while Weather has a recent forecast of your home
    place (with the condition on a wide taskbar), else a plain widgets
    mark. It opens the widgets board. When the group sits at the left,
    the button moves beside the tray.
  - At the right: tray icons, the keyboard layout when there is more than
    one, the network, volume and battery icons (one button, which opens
    quick settings), and the clock (which opens notifications and the
    calendar).
  - **Right-click menus** (ADR-0062), as on Windows; an entry appears only
    when what it opens is installed, and Settings entries open that page
    of Control Center:
    - **Start button** (also Super+X, the quick-link menu): Installed
      apps, Power options, System, Network connections, Disks (where GNOME
      Disks is installed), Terminal, Task Manager, Settings, Files (a new
      window), Search, Run, "Shut down or sign out" (Sign out, Sleep,
      Hibernate where allowed, Restart, Shut down) and Desktop.
    - **Clock:** Adjust date and time, Notification settings.
    - **Network, volume and battery button:** Network and internet
      settings, Sound settings, Open volume mixer, and Power settings
      where there is a battery.
    - **Task View button and the taskbar's background:** Task Manager,
      Taskbar settings.
    - **Keyboard layout:** Language and region, Keyboard settings.
  - **From the keyboard** (ADR-0062): Super+T puts the keyboard on the
    applications' buttons (again: the next one), Super+B on the
    notification area (the chevron of the hidden icons, else the first
    tray icon). The arrow keys and Tab move, Enter or Space press, Escape,
    any other key or a click elsewhere leaves.
  - While a program may capture the screen, a red mark sits in front of
    the tray; a click on it stops the capture.
  - Settings > Personalization > Taskbar (ADR-0049): the group can sit at
    the left edge instead (Start then opens at the left); the widgets
    button and the Task View button can go; the clock can show seconds;
    a button can show how many notifications its application has in the
    notification centre. An
    automatically hidden taskbar reserves no room: it leaves a 2 px sliver
    at the bottom edge, comes back when the pointer reaches it or while a
    flyout, menu or preview is open, and goes 400 ms after.
- **Start:** opens with the Super key (tap it) or its button. It is laid
  out like the Start of Windows 11 since 2025 (ADR-0053): one page that
  scrolls under the search box.
  - Search (Super+S opens Start with the search box) finds applications,
    pages of Boswas Control Center (Clipboard among them, ADR-0062), and
    runs a command. It ranks applications by how often they were started
    and is local: nothing is sent anywhere. While you type, the results
    take the page's place.
  - **Pinned:** the first rows of pins; "Show all" shows the rest, "Show
    less" folds them again. A new user's Start begins with Firefox ESR,
    Files, the Software Center, Settings, Photos, Calendar, Weather and
    Clock, then the other Boswas applications; an application that is not
    installed is left out and Start fills up with others.
  - **Recommended:** applications installed in the last two weeks and the
    applications used most, two to a row; the section is gone when both
    are turned off in Settings.
  - **All:** every application (Windows applications included, as the
    menu entries `boswas-winapp` creates), in one of three views chosen
    at the section's right: **Category** (Productivity, Creativity,
    Utilities & tools, Entertainment, Internet, System, Other, from what
    each application says it is), **Grid** (alphabetical) or **List**
    (under letter headings; a click on a letter opens an index of all
    letters to jump to). Start remembers the view.
  - Start grows with the screen: eight columns of pins where the screen
    is at least 1600 by 900 pixels (at its scale), six on smaller ones,
    and up to 85 % of the screen's height. At 1920x1080 it is 832 by 918
    pixels. Pins have 36 px icons.
  - The wheel scrolls it smoothly, a touchpad follows the fingers, and
    with reduced motion it jumps. From the keyboard, typing searches, the
    arrow keys go through the whole page and it scrolls along, Page Up,
    Page Down, Home and End scroll, and Escape closes the index, clears
    the search or closes Start. Every application on the page, and every
    application found by the search, has a right-click menu: open, pin to
    or unpin from Start and the taskbar, Open file location (Files, at its
    desktop entry), Uninstall (Settings > Apps > Installed apps; the
    application itself still has to be found there), and the
    application's own actions (ADR-0062); a settings page found by the
    search has Open, a command Run.
  - The user menu, whose button shows your account picture (the one
    chosen in Settings > Accounts; ADR-0062), and the power menu, with the
    folders chosen in Settings next to the power button (Settings and
    Files, which opens a new window, by default; also Documents,
    Downloads, Music, Pictures, Videos, which open in Files).
    Signing out, restarting and shutting down ask first when windows are
    open.
  - Settings > Personalization > Start: three layouts (the default, two
    rows of pins and two of recommendations; more pins, three and one;
    more recommendations, one and three), whether recently added and most
    used applications are recommended, and the folders.
- **Quick settings** (Super+A): Wi-Fi with the list of networks,
  Bluetooth with the list of devices, airplane mode, night light, the
  power mode, do not disturb, media controls, brightness, and volume with
  the choice of the sound output. A part whose system service is not
  running shows as not available. The night light tile is lit while the
  screens are tinted; a click turns night light on or off now, and with a
  schedule that holds until the schedule's next start or end (a change in
  Settings ends it too). Where no monitor can change its colours the tile
  is greyed and says so. The battery's percentage opens Settings > Power
  & battery (ADR-0062).
- **Notifications** (Super+N): up to three at a time above the taskbar,
  and the notification centre with a calendar (Delete clears the
  notification that has the keyboard focus). While do not disturb is on
  only critical notifications pop up; nothing pops up while the session
  is locked. Notifications are kept in memory until they are dismissed
  or the shell restarts; they are not written to disk. Settings >
  System > Notifications (the centre's settings button opens it) holds the do
  not disturb a session starts with, a daily schedule for it, and the
  applications whose banners are off (their notifications still reach
  the centre); Accessibility > Visual effects sets how long a
  notification stays at least. The shell keeps the names of the
  applications that sent notifications in
  `~/.local/state/boswas/notification-senders.json` for that list.
- **The volume and brightness keys** show a small display of the level,
  also on the lock screen and the login screen, which show it themselves
  above the hint (ADR-0062).
- **Security options** (Ctrl+Alt+Del; ADR-0062): over the whole screen,
  Lock, Sign out, Change a password (Settings > Accounts > Your info),
  Task Manager and Cancel, and the power button at the bottom right. The
  arrow keys and Tab move, Escape cancels. It works even while a remote
  desktop or a virtual machine window has taken the other shortcuts.
  There is no Switch user.
- **Projection** (Super+P; ADR-0062): PC screen only, Duplicate, Extend,
  Second screen only. Super+P again or the arrow keys move, Enter or a
  click takes the choice, which holds for the session on top of the
  monitors' settings. With one monitor only PC screen only and Extend can
  be chosen.
- **Dark mode for GTK programs and Firefox** (ADR-0062): the shell writes
  the colour mode and the nearest of GNOME's accent colours to GSettings
  (`org.gnome.desktop.interface` `color-scheme` and `accent-color`), where
  GTK programs and Firefox read them through the desktop portal.
- **Widgets** (Super+W, or the widgets button; ADR-0059): a board from the
  left edge with cards for the weather (from Weather), the next events of
  the coming week (from Calendar), the world clocks and the next alarm
  (from Clock), the system (processor, memory, disk space, battery), what
  is playing (play, pause, next) and the newest picture in Pictures. A
  card opens its application; its menu makes it small or medium, moves
  it or removes it, and "Add widgets" brings a card back. The board
  shows only what your own applications keep: nothing is fetched from
  the internet. Its arrangement is kept in `~/.config/boswas/shell.toml`.
- **Clipboard history** (Super+V; ADR-0058): the last 25 things you copied
  (text, pictures, files), newest first. Enter or a click pastes the item
  into the window you were in (with Ctrl+Shift+V in a terminal); the
  arrow keys choose, Delete removes, Ctrl+P pins, typing searches, Escape
  closes; each card has Pin and Delete, and Clear all keeps the pinned
  ones. The history is in memory and gone when you sign out; pinned items
  stay (`~/.local/state/boswas/clipboard-pinned.json`). What a password
  manager marks as secret is never kept, nor anything copied while the
  session is locked. Settings > System > Clipboard turns the history off
  and clears it; with the history off, Super+V offers to turn it on.
- **Emoji and symbols** (Super+. or Super+;; ADR-0058): emoji by category
  with a choice of skin tone, kaomoji and symbols, a search by name and
  keyword, and the ones you used recently. Enter or a click types the
  item into the window you were in; the clipboard keeps what it held.
  Colour emoji come from Noto Color Emoji.
- **Run** (Super+R) starts a program with its arguments. There is no
  shell behind it and nothing is expanded.
- **Screen capture** (Print for the screen, Shift+Print or Super+Shift+S
  for a region): the capture screen shows the picture, the user chooses
  what to keep and confirms. The result is saved to the Screenshots
  folder under Pictures and copied to the clipboard. A notice says so: a
  click on it, or on its entry in the notification centre, opens the
  picture (in Photos), and its button "Open folder" opens the Screenshots
  folder in Files.
- **Opening files:** what the shell opens (a screenshot, the folders next
  to the power button) opens with its default application, the one
  chosen in Settings > Apps > Default apps or else the system's default
  (ADR-0054). On a new system pictures open in Photos, PDF files in
  Document Viewer, text in Text Editor, music and videos in Media Player,
  archives in Archive Manager, folders in Files, calendars in Calendar,
  and web pages in Firefox ESR. Programs that open files through the
  desktop's defaults follow the same choices.
- **Passwords for administrator actions:** when polkit needs a password
  (Control Center installing an update, for example) the shell shows the
  dialog, with a choice of administrator when several may authorise the
  action.
- **"Allow screen capture?":** when another program (a screenshot tool, a
  recorder, screen sharing in a browser) asks for pictures of the screen,
  the shell asks first. Neither answer is the default, and Enter alone
  allows nothing ([Screen capture by programs](#screen-capture-by-programs)).
- **Keyboard:** every flyout and dialog can be used without a pointer:
  Tab and Shift+Tab go through the controls, the arrow keys move to the
  nearest control in that direction, Enter or Space activates it, Escape
  closes. Super+T and Super+B reach the taskbar.
- **The look: Boswas glass** (ADR-0063), the same glass as the Boswas
  applications. Start, quick settings, the notification centre, the
  widgets board, the clipboard, emoji and projection panels and the
  security options are the window backdrop; menus, tooltips,
  notifications, the volume display, dialogs and window previews are the
  more opaque flyout glass; the taskbar has its own darker tint. Each is a
  tint over a blurred, saturated backdrop with a fine grain, a white
  hairline along the top and a soft glow under it; cards are glass, and a
  control under the pointer gets a brighter hairline. Where the
  compositor draws with OpenGL ES (a computer with a GPU) the backdrop is
  what really lies behind, windows included, blurred live; with pixman
  (virtual machines, no GPU) it is the blurred wallpaper. A tint is made
  more opaque wherever text would fall under 4.5:1. The lock screen and
  the login screen blur the wallpaper into glass behind the sign-in view.
  Transparency effects off (Settings > Personalization > Colours, or
  Accessibility > Visual effects) or the Boswas Carbon preset makes every
  surface opaque.
- **Icons that grow under the pointer** (ADR-0063): in Start, the widgets
  board and the clipboard and emoji panels the icon under the pointer, or
  with the keyboard focus, grows as in a dock and its neighbours a
  little, within its cell, so no label moves. With reduced motion they
  stay as they are.
- **Presets:** ten Boswas OS presets
  ([presets](../../desktop/presets/README.md)), chosen in Control Center >
  Personalization > Themes or with `boswas-preset`. A preset sets light or dark
  for the shell, the window frames and the Boswas applications, and the
  wallpaper; for KDE applications, where they are installed, also their
  colours. Nothing else: the Plasma themes, panel layouts and Konsole
  profiles the presets once carried are gone (ADR-0042). A new user
  starts with the Boswas Prism look, which is not one of the ten;
  `boswas-preset reset`, or "Reset to Boswas Default" in Control Center,
  returns to it.
- **Icons:** the Boswas Prism icon pack
  ([icons](../../desktop/icons/README.md)): applications, places, devices,
  file types, and the symbolic status and action icons. Coloured icons
  fill their square as the icons of Windows 11 do, so at the taskbar's
  24 px an application's icon is about 22 px (ADR-0052). It draws every
  icon the Boswas applications and Settings ask for, and nothing under
  another program's name: other programs (Firefox ESR, and whatever you
  install) show the icons they bring. What neither draws comes from
  Breeze's icon theme.
- **Pointer:** Boswas-Cursors
  ([cursors](../../desktop/cursors/README.md)), Boswas's own pointer
  theme: a white arrow with a dark outline, the same family for every
  shape, and a turning ring while a program is busy. It is the default of
  the compositor, of the login screen and of every program (ADR-0043).

The pins, Start's view and the widgets board are kept in
`~/.config/boswas/shell.toml`; pins you arranged yourself stay as they
are when the defaults change. How often each application was started,
when it was first seen (for "Recently added"), the applications that sent
notifications, the last commands typed into Run, the pinned clipboard
items, the recently used emoji and where you put desktop icons are in
`~/.local/state/boswas/`. None of it leaves the device.

## Windows and shortcuts

The compositor draws the window frames: a 32 px title bar with the
application's icon and title and the minimise, maximise and close
buttons, a thin border, rounded corners and a shadow. The active
window's title bar shows the wallpaper under it, blurred and tinted (the
`mica` material, ADR-0063); the window picker, the window menu, the snap layouts and
Task View are glass like the shell. Applications that
draw their own frame (GTK applications, Firefox) keep it. Windows open,
close, minimise and come back with short animations, and desktops slide
when switched; `[appearance] reduced_motion` turns that off. X11
applications get the same frame and behave like every other window.
`boswas-compositor(1)` is the reference.

| Keys | Action |
|------|--------|
| Super (tap) | Open or close Start |
| Super+S | Start, typing into its search box |
| Super+X | The quick-link menu at the Start button |
| Super+A | Quick settings |
| Super+N, Super+Alt+D | Notifications and calendar |
| Super+W | Widgets |
| Super+V | Clipboard history |
| Super+. or Super+; | Emoji and symbols |
| Super+R | Run |
| Super+T, Super+B | The keyboard on the taskbar's applications (again: the next one); on the notification area |
| Super+P | Projection: PC screen only, Duplicate, Extend, Second screen only |
| Super+E, Super+I, Ctrl+Alt+T, Ctrl+Shift+Esc | Files, Settings (Control Center), Terminal and Task Manager (the programs named in `[apps]`) |
| Super+U | Settings > Accessibility |
| Super+L | Lock the session |
| Ctrl+Alt+Del | Security options: Lock, Sign out, Change a password, Task Manager |
| Super+1 ... Super+9 | Start or switch to the taskbar application at that place; with Shift, open another window of it (its "new window" action), or start it when it is not running |
| Alt+Tab, Alt+Shift+Tab | Pick a window, most recently used first; live previews, Escape cancels |
| Super+Tab, Super+Shift+Tab | Task View: every window and the desktops |
| Alt+F4 | Close the window |
| Alt+Space | The window menu: Restore, Move, Size, Minimize, Maximize, Close |
| F11, Super+F11 | Full screen |
| Super+Up | Maximise; a snapped half goes to its upper quarter |
| Super+Down | Restore; a snapped half goes to its lower quarter; minimise a restored window |
| Super+Left, Super+Right | Snap to a half; at the edge, on to the next monitor |
| Super+Shift+Left, Super+Shift+Right | Move the window to the next monitor |
| Super+D | Show the desktop, and bring the windows back |
| Super+M, Super+Shift+M | Minimise every window; restore them |
| Super+Home | Minimise every window but the active one; again, restore them |
| Super+Shift+Up | Stretch the window to the full height of the screen, keeping its width |
| Super+Shift+Down | Put a stretched window back; otherwise as Super+Down |
| Super+Ctrl+D | New desktop |
| Super+Ctrl+Left, Super+Ctrl+Right | Previous or next desktop |
| Super+Ctrl+F4 | Close the desktop (its windows move to the neighbour) |
| Super+Space | Next keyboard layout |
| Super+Plus, Super+Minus | Start the magnifier or enlarge by its step; reduce by the step |
| Super+Esc | End the magnifier |
| Super+Ctrl+C | Colour filter on or off (when Settings > Accessibility > Colour filters allows the shortcut) |
| Print; Shift+Print, Super+Shift+S | Capture the screen; capture a region |
| Volume, brightness and media keys | Handled by the shell; on the lock and login screens the volume and brightness keys work and show the level |
| Ctrl+Alt+F1 ... F12 | Switch virtual terminal |

With the pointer:

- drag a title bar to move a window; drag it to the top edge to maximise,
  to a side for that half, to the outer quarter of a side for that corner.
  A preview shows where the window will go. Dragged to the middle of the
  top edge, it opens the snap layouts there; drop it on a zone;
- rest the pointer on the maximise button for the snap layouts (two
  halves, a half beside two quarters, four quarters) and click a zone;
- double-click a title bar to maximise; right-click it for the window
  menu, which also sends the window to another monitor or desktop; drag a
  border or a corner to resize;
- Super+left drag moves and Super+right drag resizes from anywhere on a
  window.

After Move or Size from the window menu the arrow keys move or resize the
window (with Ctrl by single pixels) until Enter or Escape.

Settings > System > Multitasking turns snapping off (dragging to an edge
and Super+Left/Right then do nothing; Super+Up and Super+Down still
maximise, restore and minimise), turns either way to the snap layouts
off, and chooses whether the taskbar and Alt+Tab show the windows of the
current desktop or of all desktops.

On a touch screen a finger does what the pointer does on title bars,
buttons, the window picker and Task View. On a touchpad, three or four
fingers sideways go to the next or previous desktop, up opens Task View
and down shows the desktop. Drawing tablets and pens are supported with
pressure, tilt and their buttons. None of this has been tried on real
devices yet.

Shortcuts use the key's position on the Latin layout, so they work the
same while another keyboard layout is active. Super+L, Ctrl+Alt+Del and
the virtual terminal keys work even when an application (a remote
desktop, a virtual machine) has taken the other shortcuts. The middle
button on a running application's taskbar button opens another window
of it. The shortcuts are fixed in this
version; there is no shortcut editor.

Desktops are created and closed as needed: a session starts with one.
Task View shows them in a strip under the windows.

## Screen capture by programs

An application cannot read the screen on its own. When a program asks the
compositor for pictures of it, the shell asks "Allow screen capture? NAME
wants to see your screen." (ADR-0037):

- "Allow" covers that program while it keeps capturing (a recording, a
  shared screen) and ends a few seconds after its last picture. The next
  request asks again.
- "Don't allow" holds for twenty seconds, so a program that retries does
  not ask again and again.
- While any program may capture, a red mark on the taskbar says which. A
  click on it stops them all.
- Nothing is captured while the session is locked.
- Sandboxed applications (Flatpak) are not offered this at all; they go
  through the desktop portal, which is the program the question then
  names.

The name in the question is the name the program runs under, and a
program can choose it. An allowed program sees everything on every
screen until it stops. Screen sharing from browsers and meetings goes
through `xdg-desktop-portal-wlr`, which shares the first monitor; this
path has not been tested yet.

## Lock, login and boot

- **Lock screen.** The compositor owns the lock. Super+L, Lock in Start's
  user menu, ten minutes without input, suspend and hibernate lock the
  session: every screen is blanked and input is taken away from
  applications first, then the lock screen is started. It shows the
  clock and, after a key or a click, asks for the password, which it
  checks through PAM. If the lock screen crashes, the screens stay blank
  and it is started again; nothing but a correct password unlocks. A
  video player or presentation that asks the compositor to stay awake
  holds the idle lock off while it runs. The lock screen shows no
  notifications and offers no way to restart or shut down; the volume and
  brightness keys change the level and show it (ADR-0062). It shows your
  account picture, and behind the sign-in view the wallpaper blurs into
  glass, darkened as far as white text needs (ADR-0063).
  - **Weather** (ADR-0051): under the clock, in the lower left corner, a
    card with the weather of the home place chosen in Weather (or the
    first place): the condition and the temperature, today's high and
    low, how warm it feels and the next four hours. It is on the monitor
    that shows the clock and the hint, not in the sign-in view, not on the
    other monitors and not on the login screen. The lock screen itself
    never uses the network: it reads a summary that Weather keeps in
    `~/.local/state/boswas/weather.json`, written by Weather while it is
    open and every half hour by a user timer, also while it is closed. A
    summary more than three hours old is not shown; after an hour the
    card says when it was fetched. Without a place in Weather there is no
    card and nothing is fetched. Settings > Privacy & security > Lock
    screen > "Show weather on the lock screen" turns it off
    (`[lock] show_weather`).
  - An administrator sets the timeout in `/etc/boswas/compositor.toml`. A
    user may shorten it, not lengthen or disable it.
  - `loginctl lock-session` locks the session while the shell runs;
    scripts can also use `boswas-compositor --send lock`.
  - The screens turn off after five minutes without input (`[power]
    screen_off_seconds`) and back on with any input. That is separate
    from the lock.
- **Login screen.** greetd runs the Boswas login screen on the first
  virtual terminal: the clock, then the people who can sign in (regular
  accounts with a login shell), with the pictures AccountsService keeps
  for them, and the password. Another user name can be typed. The volume
  and brightness keys work there. After a successful login it starts
  `boswas-session`. The live session signs in automatically.
- **When the desktop cannot start.** A boot always ends at a way to sign
  in (ADR-0044). If the login screen cannot be shown (no usable graphics
  device, a login screen that keeps failing), greetd's text login appears
  with a line that says so; signing in there opens a shell with a note on
  how to start the desktop (`boswas-session`) and where its messages are
  (`journalctl -b -t boswas-session`; the login screen's are under
  `boswas-greeter`). If greetd itself fails, the ordinary text
  login of the first console is started.
- **Boot screen.** `boswas-bootsplash` shows the mark and a progress ring
  from the initramfs to the login screen, the passphrase prompt of an
  encrypted disk, and the shutdown and restart screens. Everything is
  drawn from vector outlines at the display's exact size and moves at its
  refresh rate (ADR-0050): at the start the mark assembles itself (its
  stem rises, its bowl sweeps round, its dot lights up), then the ring
  fades in under it; the ring fills with the boot's progress when the
  boot reports one; for a passphrase the mark glides up and the prompt
  slides in; at the end the screen fades. Escape switches between the
  boot screen and the text console. `nosplash` on the kernel command line
  turns it off, `boswas.splash=static` keeps it without motion
  ([packages/boswas-bootsplash](../../packages/boswas-bootsplash/README.md)).

## Settings

Settings live in **Boswas Control Center** (Super+I), laid out like the
Settings of Windows 11 (ADR-0049): eleven categories in the pane, their
pages as cards, and on each page sections with switches, lists and
sliders that apply at once (ADR-0041). The search box above the
categories finds categories, pages, sections and single settings and
jumps to them. [control-center.md](control-center.md) describes every
page.

| Category | What it holds |
|----------|---------------|
| Home | The device (name, with Rename; maker and model), the account, the network, Boswas Update, dark mode and night light, Bluetooth devices, the background and theme, storage |
| System | Display (monitors arranged by dragging; resolution, refresh rate, the fastest by default; variable refresh rate; scale, with the one recommended for the monitor marked; orientation; on or off; brightness; night light), Sound, Notifications (do not disturb and its schedule, banners per application), Power & battery (brightness, the time until the screens turn off), Storage (cleanup recommendations: temporary files, the Trash, old downloads), Multitasking, Troubleshoot (internet, sound, Bluetooth, display), Recovery (restart into the firmware settings or the boot menu), Clipboard (the clipboard history on or off, Clear clipboard data), About (rename the device); in a live session Install Boswas OS. A display change that could leave you without a picture asks "Keep these display settings?" and goes back after fifteen seconds |
| Bluetooth & devices | Bluetooth (add, pair, connect, remove, battery), Printers (CUPS; a test page; installing printing support), Cameras, Mouse, Touchpad, Keyboard (layouts and repeat; Num Lock at sign-in, which the compositor applies at the start of the session and when it changes, ADR-0062), USB (the devices connected; a drive opens in Files or is ejected), Hardware & Drivers |
| Network & internet | Wi-Fi (networks, passphrase, forget), Ethernet (automatic or manual IP address, DNS servers, metered connection, the adapter's properties), VPN (WireGuard and OpenVPN: import a file, connect, disconnect, remove), Airplane mode, Proxy (read-only), Advanced network settings (adapters on and off, data used, connection details) |
| Personalization | Background, Colours (dark or light, the accent colour, transparency), Themes (the ten presets: light or dark, the wallpaper and the colours of KDE applications, `boswas-preset apply <id>` does the same; desktop icons), Start, Taskbar (widgets, Task View, alignment, hiding, badges, seconds), Fonts (install and remove fonts for your account) |
| Apps | Installed apps (uninstall each the way it was installed), the Software Center, Default apps, Startup, Optional features (codecs, printing, fonts, Firefox in your languages, ...), Windows Compatibility, Android |
| Accounts | Your account (picture, name, password), other users, adding and removing users (changes to other accounts ask for an administrator's password); Access work or school |
| Time & language | Date and time, time zone; system language and regional format |
| Accessibility | Text size, Visual effects (scroll bars, how long notifications stay, transparency, animations), Mouse pointer (size), Magnifier, Colour filters, Keyboard (sticky keys, filter keys) |
| Privacy & security | Boswas Security Center, Lock screen (the timeout ends at the limit the organisation set, which the page shows; the weather on the lock screen), App permissions of sandboxed applications, Activity history (clears the shared list of recent files and the recent lists of Text Editor, Document Viewer, Media Player and Archive Manager), Diagnostics & feedback |
| Boswas Update | Automatic updates, pending updates, checking and installing; Update history; Advanced options (read-only) |

Wi-Fi, Bluetooth, airplane mode, night light, the power mode, do not
disturb, brightness and volume are also in quick settings (Super+A).
Per-application volumes: `pavucontrol-qt`.

Display, input, the look, the taskbar, Start, multitasking,
notifications, accessibility, the desktop icons, the clipboard history
and the timeouts are the compositor's settings: Control Center writes
the user's file, `~/.config/boswas/compositor.toml` (the colour mode and
the background go to `appearance.toml`, which presets replace), and asks
the compositor to load it again. The file can also be edited by hand;
`boswas-compositor(1)` lists every key, `boswas-compositor --send
reload-config` applies it and `--send outputs` lists the monitors with
their names and modes.
System-wide values and the policy are in `/etc/boswas/compositor.toml`,
which Settings never writes. Do not disturb, the magnifier and the colour
filter work like night light: the setting is what a session starts with,
and quick settings, the notification centre or their keys switch them in
between.

Example, a user's `~/.config/boswas/compositor.toml`:

```toml
[keyboard]
layout = "us,in"

[touchpad]
natural_scroll = true

[outputs.HDMI-A-1]
scale = 1.5
position = [1920, 0]

[lock]
idle_seconds = 300

[taskbar]
alignment = "left"

[accessibility]
text_scale = 150
```

Super+Space goes to the next keyboard layout. A file that does not
parse, or that contains an unknown key, is reported in the log and
ignored as a whole; the other files stay in use. When Control Center
changes a setting it rewrites the file and keeps every documented key.
What it cannot keep (comments, a key the compositor does not know) is not
dropped silently: the previous file is saved as `compositor.toml.bak`
first, and Control Center says so.

Not in Settings: editing a proxy, connecting a VPN that asks for a
password each time (the desktop has no prompt for NetworkManager's
secrets yet), drawing tablets, an automatic suspend time, pausing
updates, notifications on the lock screen, contrast themes. The full list
is in [control-center.md](control-center.md#left-out).

## Known gaps

The native desktop does not have, or has not shown yet:

- **A run on real hardware or against the real system services.** See
  [what is verified](../architecture/verification.md). In particular the
  password dialog for administrator actions has only run against a
  stand-in for polkit.
- **Live blur everywhere.** The shell's glass shows the windows behind it
  only where the compositor draws with OpenGL ES; with pixman (virtual
  machines, computers without a GPU) it is the blurred wallpaper. The
  applications' glass is always a blurred copy of the desktop background:
  the toolkit does not ask for live blur yet (ADR-0063).
- **Sharp X11 windows on scaled monitors.** X11 applications are drawn at
  scale 1 and enlarged, so they look soft on the many laptops that now
  start above 100 %. They can also observe each other, as on any X
  server; they cannot observe Wayland applications.
- **Tested screen sharing.** The portal for it is installed and
  configured, shares the first monitor, and has not been run.
- **Bluetooth devices that need a PIN or passkey,** **enterprise Wi-Fi
  (802.1X)** and **VPNs that ask for a password when they connect:** none
  can be set up from quick settings or from Control Center.
- **An office suite, a mail client and a print service in the image**
  (Print saves a PDF file; Control Center can install CUPS, which has not
  been tried).
- **What the system applications leave out** next to the KDE applications
  they replace, for example syntax highlighting, PDF annotations and
  split terminal panes (ADR-0040 has the list).
- **An input method in the image** (for example for Indic, Chinese,
  Japanese or Korean text entry) and an on-screen keyboard. The
  compositor supports an input method that the administrator configures;
  none has been run.
- **More than one GPU** and HDR.
- **A screen reader for the shell** and **contrast themes.** See
  [Accessibility](#accessibility).
- **Per-preset accents in the shell.** The taskbar, Start and the lock
  screen follow a preset's light or dark variant, not its accent colour;
  the accent is chosen in Settings, from eight hues of the palette.
- **A shortcut editor,** a taskbar at another edge, desktop icons on
  other monitors and dragged into other programs, and online feeds on the
  widgets board.
- **Super+K** (Cast), **Switch user** on the Ctrl+Alt+Del screen, and an
  Uninstall in Start that goes straight to the application (ADR-0062).

## Boswas applications

The system applications are one package, `boswas-apps` (ADR-0040). Each
follows its Windows 11 counterpart in layout and shortcuts, in dark and
light, and each has a manual page (`man boswas-files`, and so on).

| Application | What it is |
|-------------|------------|
| **Files** | The file manager: tabs, an address bar that turns into a path box (Ctrl+L), a navigation pane with Home, your folders, pinned folders, the volumes of the computer and the Trash; Details, List, Tiles and Icons layouts with picture thumbnails. Copying, moving and deleting run in the background with progress and a cancel button, ask when a name is taken, and can be undone. Delete moves to the Trash. A selection can be compressed to a ZIP archive, and ZIP and TAR archives extracted. A program or script is never started by opening it without a question |
| **Terminal** | The command line with tabs and profiles (Ctrl+Shift+T, Ctrl+Tab), scrollback with search (Ctrl+Shift+F), copy and paste (Ctrl+Shift+C and V), links that open with Ctrl+click, and colour schemes that follow the desktop. Editors, pagers and terminal multiplexers work as in an xterm |
| **Text Editor** | Plain text with tabs: the encoding and the line endings of a file are kept, find and replace with regular expressions, go to line, word wrap, zoom, printing. It restores the last session, unsaved text included |
| **Calculator** | Standard, scientific and programmer modes, date calculation and twelve unit converters, with memory and history. Arithmetic is exact: 0.1 + 0.2 is 0.3 |
| **Photos** | A viewer (zoom, filmstrip, rotate, delete with undo, print, set as background), a gallery by month, a slideshow, and an editor: crop, straighten, ten adjustments, looks, pen and highlighter. "Save as copy" is the default |
| **Document Viewer** | A PDF reader: tabs, thumbnails and outline, search, text selection and copy, continuous or page-wise view, presentation mode, printing |
| **Archive Manager** | Opens an archive like a folder; extracts, adds, deletes, tests, creates. ZIP and TAR (also compressed); 7z and RAR when the `7zip` package is installed |
| **Media Player** | Music and video: a library from the tags of your files, a play queue, playlists, a mini player, subtitle and audio tracks, and an MPRIS interface for the media keys and the shell's media controls |
| **Camera** | Photos and videos from a camera of the computer, with a timer, to Pictures/Camera Roll and Videos/Camera Roll |
| **Clock** | Alarms, timers, a stopwatch, a world clock and focus sessions |
| **Calendar** | A calendar kept on this computer: month, week and day views, repeating events, reminders, several calendars, import and export of `.ics` files. It has no accounts and does not synchronise |
| **Snipping Tool** | Screenshots of an area or the whole screen, now or after a delay, through the desktop's capture screen (the same one Print opens). The picture opens in an editor with a pen, a highlighter, arrows, rectangles, text, an eraser and crop, with undo; copy it, save it as PNG or JPEG or open it in Photos. It also marks up pictures you open or paste |
| **Weather** | The places you add (it never uses your location): current conditions, the next 24 hours, details (wind, humidity, pressure, UV index, visibility, precipitation, sunrise and sunset) and 10 days, in the units you choose. It refreshes while open and shows the last forecast when there is no network. The home place's weather is also on the lock screen, fetched every half hour, also while Weather is closed (Settings > Privacy & security > Lock screen turns it off). Data from Open-Meteo |
| **Task Manager** | Ctrl+Shift+Esc. What is running, by application, with CPU, memory and disk use, and "End task"; performance graphs for the processor, memory, disks, network, graphics and battery; start-up applications (switch them off or on); the signed-in users; every process in detail; services |
| **Boswas Control Center** | Settings, laid out like the Settings of Windows 11: eleven categories from Home to Boswas Update, each page with its settings, the device's state and its actions (see [Settings](#settings)), and the three centres below. It is the application Super+I opens ([control-center.md](control-center.md)) |
| **Boswas Security Center** | Control Center's Privacy & security > Boswas Security Center: AppArmor, Windows-app sandbox, firewall, Secure Boot, disk encryption, screen lock, automatic security updates, device agent, device policy. Each item is Secure, Warning, Attention, Error or Not available, shown with an icon and text, never colour alone |
| **Boswas Software Center** | Control Center's Apps > Software Center: Boswas, Windows and native applications, searchable; Debian packages from the signed repositories, Flatpak applications and AppImages. Apps > Installed apps removes an application the way it was installed |
| **Boswas Update Center** | Control Center's Boswas Update: automatic security updates (read-only), pending updates, checking for and installing updates, the update history |
| **Boswas Compatibility Manager** | Windows applications (64-bit and 32-bit) in isolated sandboxes. In Files, a Windows program or installer package has "Install with Boswas Compatibility Manager" under "Open with"; opening the file still means "Run with Boswas" |
| **About Boswas OS** | Control Center's System > About: version, architecture, kernel, CPU, memory, storage, desktop (Boswas Desktop and its version), security, Windows compatibility, device agent, live or installed; the device name, with Rename. Technical details include the package base |

**The look: Boswas glass** (ADR-0057). Behind every window of these
applications, Control Center and the Compatibility Manager the desktop
background shows through, blurred and tinted, as Mica does on Windows
(the shell's own glass is described under [Desktop
shell](#desktop-shell));
the navigation pane and the cards are glass on it, and menus, lists,
tool tips and dialogs are frosted glass. The tint grows where a busy or
bright picture would make text hard to read, so text keeps the contrast
of the opaque look. The picture is your wallpaper (or the default
background) as a window of that size centred on the screen would cover
it: it does not move with the window, and windows behind it do not show
through. Under the pointer, rows turn a lighter grey. In Control Center's
lists of applications (Installed apps, the Software Center, Optional
features, Startup, App permissions, Default apps) the icon under the
pointer, or of the row with the keyboard focus, grows as in a dock, and
its neighbours a little; with reduced motion it grows at once. Glass is
off, and every surface opaque, with Transparency effects off (Settings >
Personalization > Colours, or Accessibility > Visual effects) and with
the high-contrast preset Boswas Carbon.

**New window:** Files, Terminal, Text Editor, Photos, Document Viewer,
Calculator and Archive Manager open another window from the "New Window"
entry of their menu in Start, and from "New window of" in the taskbar's
menu, the middle button and Super+Shift+1 to 9 (`--new-window`).

Alarms, timers and calendar reminders are shown as notifications even
while Clock and Calendar are closed: a small service, `boswas-reminders`,
starts with the session for that.

All of them run as you and refuse to run as administrator. What they
leave out next to the KDE applications they replace, and what has not
been tried yet (none has run on the desktop; the Camera has never seen a
camera), is in ADR-0040 and in
[what is verified](../architecture/verification.md).

## Terminal

Boswas Terminal starts bash with `/usr/share/boswas/terminal/bashrc`.

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
  - They support full keyboard use (sidebar arrows, Tab order) and the
    shortcuts of their Windows counterparts; each manual page lists them.
    Ctrl+F searches in Files, Text Editor, Document Viewer, Photos, Media
    Player, Archive Manager, Calendar, Task Manager, Weather and Control
    Center (Terminal: Ctrl+Shift+F). F5 refreshes in Files, Photos'
    gallery, Media Player, Archive Manager, Task Manager, Weather and
    Control Center; in Text Editor it inserts the time and date, in
    Document Viewer it starts a presentation, in Photos' viewer a
    slideshow, in Camera it tries the camera again, Calculator uses it as
    the Windows calculator does (gradians in the scientific mode,
    hexadecimal in the programmer mode), and Terminal passes it to the
    program inside. Clock and Snipping Tool have neither key, and
    Calculator has no search.
  - They show status as icon plus text.
- **The shell:** every flyout and dialog, the lock screen and the login
  screen can be used from the keyboard, and so can the window picker,
  Task View and the window menu of the compositor. None of them exposes
  an accessibility tree yet, so a screen reader cannot read them; no
  screen reader is in the image.
- **High contrast:** Boswas Carbon is the high-contrast preset for
  applications (WCAG AAA body text); it also turns glass off, in the
  applications, the shell and the compositor's own surfaces (ADR-0063). The shell uses the Prism dark or light
  theme, whose text meets WCAG AA. There are no contrast themes for the
  shell and the window frames: they would need a set of tokens of their
  own (ADR-0049).
- **Contrast:** the Prism colour schemes and every preset keep body text at
  4.5:1 or more; the tests check it. Each of the eight accent colours is
  moved, where needed, so that text on it keeps 4.5:1, in the shell and in
  the Qt applications alike.

Settings > Accessibility (ADR-0049) holds the rest; each setting is a key
of the compositor's configuration:

- **Text size:** 100 to 225 % (`[accessibility] text_scale`). The shell's
  text, the title bars (as far as their 32 px height allows, about
  180 %) and the text of every Qt application grow; nothing else does.
- **Scaling:** each monitor starts at the scale recommended for its size
  and resolution, as on Windows (ADR-0055): a 13.3- or 14-inch 1920x1080
  laptop panel at 150 %, a 15.6-inch one at 125 %, a 24-inch 1920x1080
  monitor at 100 %, a 27-inch 3840x2160 one at 150 %; never so large that
  less than 1024 by 720 pixels of room are left, and 100 % in a virtual
  machine and where the monitor does not report a believable size. 1.0 to
  2.0 in steps of 0.25 can be chosen for all screens (`[output] scale`) or
  per monitor (`[outputs.NAME] scale`) in Settings > System > Display,
  which marks the recommended scale; choosing it again leaves the monitor
  automatic.
- **Always show scroll bars** in the Qt applications
  (`[accessibility] always_show_scrollbars`), and how long a notification
  stays at least, 5 seconds to 5 minutes (`[notifications]
  toast_seconds`).
- **Magnifier:** Super+Plus starts it and enlarges by its step,
  Super+Minus reduces, Super+Esc ends it; 100 to 1600 %. The view follows
  the pointer. It works with either renderer (`[magnifier]`).
- **Colour filters:** greyscale, inverted, greyscale inverted, and
  corrections for deuteranopia, protanopia and tritanopia; Super+Ctrl+C
  switches the filter where Settings allows it (`[color_filter]`). They
  need the OpenGL ES renderer: where the compositor draws with pixman,
  Settings shows them as not available. Screenshots show the picture
  without the filter or the magnifier.
- **Sticky keys:** Shift, Ctrl, Alt and AltGr pressed and released on
  their own stay down for the next key; pressed twice, they stay down
  until pressed again. Super is not sticky, so that tapping it still
  opens Start; Windows treats it differently. **Filter keys:** slow keys
  (a key counts only after it was held for a set time) and bounce keys (a
  key pressed again soon after its release is ignored) (`[keyboard]`).
- **Pointer size:** 16 to 96 (`[pointer] cursor_size`), also under
  Accessibility > Mouse pointer. The compositor's pointer changes at once.
  X11 programs and programs that draw their own pointer read the size
  when they start: the session sets it from the setting at sign-in, and
  a change reaches what the user manager and D-Bus start afterwards, and
  at once the programs started from Start or the taskbar (ADR-0062);
  programs already open keep their size.
- **Motion:** `[appearance] reduced_motion` (Accessibility > Visual
  effects, "Animation effects") turns the animations of windows, desktops
  and the shell off; the Boswas Qt applications follow the same setting
  (an icon that grows under the pointer then grows at once; in the shell
  icons do not grow at all). Transparency effects can be turned off as
  well; that turns glass off everywhere, and with it the live blur and
  the title bars' blurred wallpaper. The boot screen's
  animation can be turned off with `boswas.splash=static` on the kernel
  command line.
- **Colour temperature:** night light, in quick settings or
  `[night_light]`.

Not there yet: a screen reader for the shell and the compositor's
surfaces, contrast themes, underlined access keys, a pointer colour.
Sticky and filter keys have run in unit tests only, and the magnifier
and the colour filters have not run on real graphics hardware
([what is verified](../architecture/verification.md)).

## Performance

No performance figures have been measured on real hardware yet. The design
choices that matter:

- **The compositor draws only when something changed** and only the
  damaged regions; an idle desktop draws nothing. A tooltip, a menu or a
  flyout appearing in front of the windows draws only its own area, and
  an invisible surface costs nothing (ADR-0064).
- **The shell is one process** for the background, the taskbar and every
  flyout. It draws again only the controls that changed (a hover redraws
  the two controls the pointer left and entered, not Start), keeps its
  buffers and hands the compositor only the changed rectangles
  (ADR-0056). The default background is rendered once per screen size,
  and the blurred copy of the wallpaper behind the shell's glass (with
  pixman) once per wallpaper and blur radius. The compositor's live blur
  (with OpenGL ES) is computed again only when something under the glass
  changed, at half resolution; a still screen costs nothing (ADR-0063).
- **The compositor fades and slides Start and the other flyouts** with
  the drawing the shell sent once; the shell draws no frame of the
  transition. Icons are prepared on a worker thread and Start, quick
  settings and the notification centre are drawn once ahead of time, so
  even the first opening of Start is quick (17 ms at 2560x1440 and 200 %
  on the build machine, where it was 375 ms). That drawing ahead runs in
  slices of a few milliseconds while the shell is idle, so a click waits
  for one slice at most (ADR-0064). The price is memory: just after the
  start the shell takes about 101 MB at that size (72 MB before
  ADR-0056), with glibc told to give large blocks back when they are
  freed.
- **The lock screen starts from the background the desktop already
  drew** (shared in the user's runtime directory) and appears 72 to
  117 ms after the lock request on the build machine; Task View keeps
  its cards and fades in one picture made ahead (ADR-0064).
- **The applications' glass** is made once per picture and size, never
  per frame; a window is opaque until its picture is ready.
- **The session's services are threads of the shell,** one per service,
  not processes of their own (ADR-0035). Each reads the system at most
  about five times a second, however many change signals arrive.
- **Control Center, the presets tool and the welcome banner run only when
  used;** the welcome reads files only, so terminals open instantly.
- **Boot screen:** nothing is parsed at boot (the outlines of the mark
  and of the glyphs are embedded at build time and drawn at the display's
  size), and a frame repaints only the items that changed: a frame of the
  ring is its 36 by 36 pixels at 1080p, about 10 µs on the build machine
  (ADR-0050).
- The device agent and session agent are the existing device-management
  services, unchanged.
