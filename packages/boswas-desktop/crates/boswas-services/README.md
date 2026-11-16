# boswas-services

The system services behind the Boswas OS shell. The shell sends
[`Command`](src/api.rs)s and receives [`Event`](src/api.rs)s; this crate talks
to the system. `src/api.rs` is the contract and is documented there.

```rust
let services = Services::start(Config::desktop(), |event| forward_to_the_event_loop(event));
services.send(Command::WifiScan);
// dropping `services` stops everything
```

Three configurations:

| Constructor | Runs | Obeys |
|---|---|---|
| `Config::desktop()` | everything | every command |
| `Config::settings()` | network, Bluetooth, audio, power, brightness | every command of those |
| `Config::status_only()` | the same five | `Refresh`, volume, mute, brightness; nothing else (`restricted`) |

`status_only()` is the lock screen and the greeter. A restricted instance
drops every other command in `Services::send`, the services refuse them
again themselves, it registers no Bluetooth pairing agent and (having no
notification server, tray, media or agent service) owns no bus name.

## What each service talks to

| Service | Module | Talks to |
|---|---|---|
| Notifications | `notifications.rs` | owns `org.freedesktop.Notifications` on the session bus (specification 1.2) |
| Tray | `tray.rs` | owns `org.kde.StatusNotifierWatcher`; reads `org.kde.StatusNotifierItem` objects and their `com.canonical.dbusmenu` menus |
| Media | `media.rs` | `org.mpris.MediaPlayer2.*` on the session bus |
| Network | `network.rs` | `org.freedesktop.NetworkManager` on the system bus; BlueZ adapters for airplane mode |
| Bluetooth | `bluetooth.rs` | `org.bluez` through its object manager; registers an `org.bluez.Agent1` |
| Audio | `audio.rs` | runs `pw-dump --monitor` (reads its JSON) and `wpctl set-volume / set-mute / set-default` |
| Power | `power.rs` | `org.freedesktop.UPower` (display device), `org.freedesktop.UPower.PowerProfiles` (or the older `net.hadess.PowerProfiles`), `org.freedesktop.login1` |
| Brightness | `brightness.rs` | reads `/sys/class/backlight`, sets through logind `Session.SetBrightness` |
| Session | `session.rs` | logind: `Lock`/`Unlock` of our session, `PrepareForSleep` |
| Authentication | `auth.rs` | registers with `org.freedesktop.PolicyKit1` as the session's agent; runs `polkit-agent-helper-1` |

A daemon that is not running is "not available" (`available: false`, `None`,
an empty list), never an error: each service watches `NameOwnerChanged` and
reads again when the daemon appears. UPower and power-profiles-daemon are
asked without checking first, so D-Bus starts them on demand; NetworkManager
and BlueZ are never started by asking.

Behaviour worth knowing when writing the shell:

- Every state event carries the whole state and is sent only when it
  differs from the last one sent (`Refresh` sends it regardless). Bursts of
  change signals are coalesced: a service reads at most about five times a
  second.
- `NetworkState::error` is in exactly one event; the state without it
  follows about 200 ms later. React to the event, do not render the field.
- A new Wi-Fi profile is saved for every user when NetworkManager says the
  person may change system-wide settings, and as the person's own profile
  (`connection.permissions`) otherwise, which needs no administrator. A
  profile made for an attempt that fails is deleted again. Requests that
  NetworkManager may hold until polkit is satisfied (connecting, a new
  passphrase for a saved network, forgetting, disconnecting) are waited
  for beside the service's loop for up to two minutes.
- `Notification::timeout_ms` is `Some` only for a positive expiry time of a
  notification that is not critical; the service then closes it at that time
  (`NotificationClosed { withdrawn: false }`). `-1` (server default) and `0`
  (never) are both `None`: the service runs no timer and the notification
  stays until dismissed; how long its toast shows is the shell's choice.
- Do not disturb does not filter events: everything still reaches the
  shell, which decides what to show. The service reports the flag to
  applications as the `Inhibited` property.
- A click on a tray item that has no `Activate` method, or that says it is
  a menu (`ItemIsMenu`), is answered with `Event::TrayMenu`.
- `TrayScroll` sends 120 per wheel step, which is what items expect.
- The backlight is never set to 0 (the lowest level is 1 % of the range):
  on many panels 0 is off.
- The polkit agent authenticates as the first of `AuthRequest::users`
  until `AuthChooseUser` names another; five wrong answers fail a request.

## Threading

`Services::start` starts one thread per service (ten for the desktop), named
`boswas-<service>`. `send` puts the command in that service's unbounded
channel and returns; it never blocks and never waits for the system.

Each thread runs a small single-threaded executor (`async-executor` on
`async-io`, the crates zbus itself is built on; no tokio). A service owns its
state and its own bus connection, which is driven on the same thread (zbus
gets no thread of its own). Bus signals are forwarded into the service's
inbox by tasks, so a subscription cannot fill up while the service waits for
a reply. Calls to programs that are not system daemons (tray items, media
players) and calls that wait for a person (pairing, power actions that need
authentication) run as tasks beside the loop, never in it.

The audio and authentication services each have one more thread while
their child process runs, which reads its standard output.

The sink is called from the service threads. Dropping `Services` closes the
command channels, abandons whatever a service is waiting for, unregisters
the agents, kills the child processes, closes the connections (releasing the
bus names) and joins the threads; the tests require this to take less than
three seconds with unanswered calls pending. If a bus goes away, the
services report "not available" and connect again when it is back (first
after one second, then less often, up to every 30 seconds).

## Security decisions

**Other programs are not trusted.** Session-bus clients (notification
senders, tray items, media players) and even the system daemons can send
anything; nothing they send can make a service panic, block or grow without
bound:

- Values are read by type; a value of another type is "not there". Replies
  are never unwrapped.
- Text is stripped of control characters and bidirectional overrides and cut
  (labels 256 characters, notification summary 256, body 4096, device names
  64, SSIDs 32 bytes).
- `Notify` refuses, with `LimitsExceeded`, a summary over 4 KiB, a body over
  64 KiB, more than 64 action strings, more than 64 hints. Markup is removed,
  not interpreted. Only the hints `urgency`, `transient`, `resident`,
  `desktop-entry`, `value` and `image-path` are read; image data is dropped
  unread. Icons are icon names or absolute paths, nothing else. At most 500
  notifications are kept open; beyond that the oldest non-critical ones are
  closed.
- Tray: at most 64 items. A property reply is decoded only up to 256 KiB;
  pixmaps are read straight from the message bytes (at most 8 MiB, sides up
  to 1024, scaled to at most 256 x 256) instead of through the generic
  decoder, which would use many times their size. Menus are cut at 8 levels
  and 1000 entries, and a layout reply over 512 KiB is not decoded.
- Commands that name something (`key`, `ssid`, `address`, `id`) act only on
  objects the service itself listed; the string is never used to build a bus
  name or a path.
- Lists are bounded (32 media players, 256 Bluetooth devices, 512 access
  points, 1024 saved profiles, 8192 sound-server objects, 64 MiB per
  `pw-dump` document).
- Child programs (`pw-dump`, `wpctl`, `polkit-agent-helper-1`, `getent`) are
  run by absolute path with argument lists, never through a shell. Their
  paths are fixed; only tests replace them, through `Environment`.

**Bluetooth pairing.** The agent is registered with the capability
`NoInputNoOutput`: it has no way to show or ask for a code, and it does not
pretend to. It agrees to a request (`RequestConfirmation`,
`RequestAuthorization`, `AuthorizeService`) only when the request comes from
BlueZ itself and concerns a device the person asked to connect to with
`BluetoothConnect` in the last 60 seconds. Everything else is refused with
`org.bluez.Error.Rejected`: a device that asks to pair on its own never
succeeds. Requests to enter or display a PIN or passkey are always refused,
so devices that insist on a code cannot be paired from the shell.

**Authentication agent.** `BeginAuthentication` and `CancelAuthentication`
are accepted only from the connection that owns `org.freedesktop.PolicyKit1`
at that moment (the bus is asked on every call); anybody else gets
`NotAuthorized` and no dialog appears. The password is checked by polkit's
own setuid helper, not by this crate. An answer is copied once, into a
buffer that is wiped after it was written to the helper's standard input;
only its first line is sent. `Secret` wipes itself and prints as
`Secret(..)`. The copy inside the D-Bus message that carries a Wi-Fi
passphrase to NetworkManager is zbus's buffer and is not wiped. A test
captures everything this crate and zbus log down to TRACE during a Wi-Fi
connection and an authentication and checks that no secret is in it. One
request is shown at a time; others wait (at most 16). The helper is killed
when a request is cancelled, withdrawn or abandoned.

**Signals are believed only from their daemon.** Match rules for logind,
NetworkManager, BlueZ, UPower and the power profiles name the sender, so the
bus delivers only what the owner of that name sent. Tray and media signals
come from arbitrary connections and are matched to an item or player by the
unique name the bus stamped on them.

**Names are not taken from others.** If another notification server or
tray watcher owns the name, the service shows nothing and waits for the
name to be released.

## Tests

`cargo test -p boswas-services` needs `dbus-daemon` and a POSIX `sh` with
`tail`; nothing else. No root, no network, no real bus, no hardware.

- Unit tests sit next to the code (parsers, policies, conversions).
- `tests/it` runs the services against two private `dbus-daemon`s per test
  (one as the session bus, one as the system bus; sockets and configuration
  in a directory under `/tmp`, no service directories). `support.rs` has a
  scriptable bus peer that plays NetworkManager, BlueZ, UPower, the power
  profiles daemon, logind, polkit, tray items with menus and media players;
  the services this crate owns are driven by real clients. `pw-dump`,
  `wpctl` and the polkit helper are shell scripts the tests write.
- Tests wait on conditions with a 20 second limit; none sleeps for a fixed
  time.

`examples/monitor.rs` prints every event on a real machine.

## What the features need at run time

Debian packages: `dbus` and a session bus (`dbus-user-session`), `systemd`
with logind, `network-manager`, `bluez`, `pipewire-bin` (`pw-dump`),
`wireplumber` (`wpctl`), `upower`, `power-profiles-daemon`, `polkitd` (its
helper at `/usr/lib/polkit-1/polkit-agent-helper-1`) and `libc-bin`
(`getent`, for users of directory services). Each is optional: without one,
its part of the state is "not available".
