//! Power, brightness and the session.

use boswas_services::{Battery, Command, Config, Event, PowerAction, PowerProfile, PowerState, Service, SessionEvent};

use crate::{mocks::*, support::*};

fn power(event: &Event) -> Option<&PowerState> {
    match event {
        Event::Power(state) => Some(state),
        _ => None,
    }
}

#[test]
fn power_without_any_daemon_reports_nothing_and_no_battery() {
    let harness = Harness::new();
    let _services = harness.start(status_config());
    let state = harness.events.latest("the first power state", power, |_| true);
    assert_eq!(
        state,
        PowerState {
            battery: None,
            on_ac: true,
            profile: None,
            profiles: vec![],
            can_suspend: false,
            can_hibernate: false,
            can_reboot: false,
            can_power_off: false,
        }
    );
}

#[test]
fn power_reads_battery_profiles_and_abilities_and_follows_changes() {
    let harness = Harness::new();
    let upower = upower(&harness.system);
    let profiles = power_profiles(&harness.system, PROFILES, PROFILES_PATH);
    let _logind = logind(&harness.system);
    let services = harness.start(status_config());

    let state = harness.events.latest("battery, profile and abilities", power, |state| {
        state.battery.is_some() && state.profile.is_some() && state.can_suspend
    });
    assert_eq!(
        state,
        PowerState {
            battery: Some(Battery { percent: 80, charging: false, full: false, seconds_left: Some(7200) }),
            on_ac: false,
            profile: Some(PowerProfile::Balanced),
            profiles: vec![PowerProfile::PowerSaver, PowerProfile::Balanced, PowerProfile::Performance],
            can_suspend: true,
            can_hibernate: false,
            // "challenge": allowed after authentication.
            can_reboot: true,
            can_power_off: true,
        }
    );

    // The charger is plugged in.
    upower.set(UPOWER_PATH, UPOWER, "OnBattery", owned(false));
    upower.set(DISPLAY_DEVICE, UPOWER_DEVICE, "TimeToFull", owned(1800i64));
    upower.set(DISPLAY_DEVICE, UPOWER_DEVICE, "State", owned(1u32));
    let state = harness.events.latest("charging", power, |state| state.on_ac && state.battery.as_ref().is_some_and(|b| b.charging));
    assert_eq!(state.battery.unwrap().seconds_left, Some(1800));

    // Full.
    upower.set(DISPLAY_DEVICE, UPOWER_DEVICE, "Percentage", owned(100.0f64));
    upower.set(DISPLAY_DEVICE, UPOWER_DEVICE, "State", owned(4u32));
    harness.events.latest("a full battery", power, |state| {
        state.battery == Some(Battery { percent: 100, charging: false, full: true, seconds_left: None })
    });

    // The shell chooses a profile; the daemon is told and the state follows.
    services.send(Command::SetPowerProfile(PowerProfile::Performance));
    let set = profiles.wait_call("Set");
    let (interface, name, value): (String, String, zbus::zvariant::OwnedValue) = set.args();
    assert_eq!((interface.as_str(), name.as_str()), (PROFILES, "ActiveProfile"));
    assert_eq!(<&str>::try_from(&*value).unwrap(), "performance");
    harness.events.latest("the performance profile", power, |state| state.profile == Some(PowerProfile::Performance));

    // Refresh sends the state again even though nothing changed.
    let before = harness.events.len();
    services.send(Command::Refresh(Service::Power));
    let again = harness.events.after(before, "the state again", |event| power(event).cloned());
    assert_eq!(again.profile, Some(PowerProfile::Performance));
}

#[test]
fn power_daemons_appear_and_disappear() {
    let harness = Harness::new();
    let _services = harness.start(status_config());
    harness.events.latest("no battery", power, |state| state.battery.is_none());

    let upower = upower(&harness.system);
    harness.events.latest("a battery once UPower runs", power, |state| state.battery.is_some() && !state.on_ac);

    // Only the old name of power-profiles-daemon.
    let legacy = power_profiles(&harness.system, LEGACY_PROFILES, LEGACY_PROFILES_PATH);
    harness.events.latest("profiles from the legacy name", power, |state| state.profiles.len() == 3);

    let logind = logind(&harness.system);
    harness.events.latest("abilities once logind runs", power, |state| state.can_power_off);

    upower.vanish();
    harness.events.latest("no battery after UPower left", power, |state| state.battery.is_none() && state.on_ac);
    legacy.vanish();
    harness.events.latest("no profiles after the daemon left", power, |state| state.profile.is_none() && state.profiles.is_empty());
    logind.vanish();
    harness.events.latest("no abilities after logind left", power, |state| !state.can_power_off && !state.can_suspend);
}

#[test]
fn power_survives_a_daemon_that_lies_about_types() {
    let harness = Harness::new();
    let upower = Peer::start(&harness.system, &[]);
    upower.add(UPOWER_PATH, UPOWER, props([("OnBattery", owned("certainly"))]));
    upower.add(
        DISPLAY_DEVICE,
        UPOWER_DEVICE,
        props([
            ("Type", owned("battery")),
            ("IsPresent", owned(1u32)),
            ("Percentage", owned("lots")),
            ("State", owned(-4i32)),
        ]),
    );
    upower.claim(UPOWER);
    let profiles = Peer::start(&harness.system, &[]);
    profiles.add(PROFILES_PATH, PROFILES, props([("ActiveProfile", owned(3u32)), ("Profiles", owned("all of them"))]));
    profiles.claim(PROFILES);
    let logind = Peer::start(&harness.system, &[]);
    for method in ["CanSuspend", "CanHibernate", "CanReboot", "CanPowerOff"] {
        logind.on(LOGIN1_MANAGER, method, |_, call| call.reply(&(42u32, vec![1u8, 2, 3])));
    }
    logind.claim(LOGIN1);

    let services = harness.start(status_config());
    logind.wait_call("CanPowerOff");
    let state = harness.events.latest("a state", power, |_| true);
    assert_eq!(state.battery, None);
    assert_eq!(state.profile, None);
    assert!(state.profiles.is_empty());
    assert!(!state.can_suspend && !state.can_power_off);

    // It still follows honest values afterwards.
    upower.set(DISPLAY_DEVICE, UPOWER_DEVICE, "Type", owned(2u32));
    upower.set(DISPLAY_DEVICE, UPOWER_DEVICE, "IsPresent", owned(true));
    upower.set(DISPLAY_DEVICE, UPOWER_DEVICE, "Percentage", owned(12.0f64));
    harness.events.latest("the battery", power, |state| state.battery.as_ref().is_some_and(|b| b.percent == 12));
    drop(services);
}

#[test]
fn power_actions_go_to_logind() {
    let harness = Harness::new();
    let logind = logind(&harness.system);
    let services = harness.start(status_config());
    for (action, method) in [
        (PowerAction::Suspend, "Suspend"),
        (PowerAction::Hibernate, "Hibernate"),
        (PowerAction::Reboot, "Reboot"),
        (PowerAction::PowerOff, "PowerOff"),
    ] {
        services.send(Command::Power(action));
        let call = logind.wait_call(method);
        assert_eq!(call.path, LOGIN1_PATH);
        // interactive: logind may ask for authentication.
        assert_eq!(call.args::<(bool,)>(), (true,));
    }
    services.send(Command::Power(PowerAction::LogOut));
    let call = logind.wait_call("TerminateSession");
    assert_eq!(call.args::<(String,)>(), ("7".to_owned(),));
}

#[test]
fn power_action_that_waits_for_authentication_does_not_stall_the_service() {
    let harness = Harness::new();
    let upower = upower(&harness.system);
    let logind = logind(&harness.system);
    // logind does not answer: it waits for polkit.
    logind.on(LOGIN1_MANAGER, "Reboot", |_, _| Reply::Silence);
    let services = harness.start(status_config());
    harness.events.latest("the battery", power, |state| state.battery.is_some());
    services.send(Command::Power(PowerAction::Reboot));
    logind.wait_call("Reboot");
    upower.set(DISPLAY_DEVICE, UPOWER_DEVICE, "Percentage", owned(33.0f64));
    harness.events.latest("a battery change while the action waits", power, |state| {
        state.battery.as_ref().is_some_and(|b| b.percent == 33)
    });
}

// --- brightness ---------------------------------------------------------------------

fn brightness(event: &Event) -> Option<&Option<boswas_services::Brightness>> {
    match event {
        Event::Brightness(level) => Some(level),
        _ => None,
    }
}

fn backlight(harness: &Harness, name: &str, kind: &str, current: u32, max: u32) {
    let dir = harness.path("backlight").join(name);
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(dir.join("type"), format!("{kind}\n")).unwrap();
    std::fs::write(dir.join("max_brightness"), format!("{max}\n")).unwrap();
    std::fs::write(dir.join("brightness"), format!("{current}\n")).unwrap();
}

#[test]
fn brightness_without_a_backlight_is_none() {
    let harness = Harness::new();
    let services = harness.start(status_config());
    assert_eq!(harness.events.latest("no backlight", brightness, |_| true), None);
    // Setting it does nothing and breaks nothing.
    services.send(Command::SetBrightness(0.5));
    let before = harness.events.len();
    services.send(Command::Refresh(Service::Brightness));
    assert_eq!(harness.events.after(before, "still none", |event| brightness(event).cloned()), None);
}

#[test]
fn brightness_is_read_set_through_logind_and_followed() {
    let harness = Harness::new();
    backlight(&harness, "raw_panel", "raw", 10, 100);
    backlight(&harness, "acpi_video0", "firmware", 200, 1000);
    let logind = logind(&harness.system);
    // logind writes the value, as the real one does.
    let root = harness.path("backlight");
    logind.on(LOGIN1_SESSION, "SetBrightness", move |_, call| {
        let (subsystem, name, value): (String, String, u32) = call.args();
        assert_eq!(subsystem, "backlight");
        std::fs::write(root.join(name).join("brightness"), format!("{value}\n")).unwrap();
        Reply::Done
    });
    let services = harness.start(status_config());
    let level = harness.events.latest("the firmware device's level", brightness, |level| level.is_some()).unwrap();
    assert!((level.level - 0.2).abs() < 1e-6, "{level:?}");

    services.send(Command::SetBrightness(0.75));
    let call = logind.wait_call("SetBrightness");
    assert_eq!(call.path, SESSION_PATH);
    assert_eq!(call.args::<(String, String, u32)>(), ("backlight".to_owned(), "acpi_video0".to_owned(), 750));
    harness.events.latest("the new level", brightness, |level| level.is_some_and(|l| (l.level - 0.75).abs() < 1e-6));

    // Out-of-range levels are clamped, and 0 does not switch the panel off.
    services.send(Command::SetBrightness(9.0));
    harness.events.latest("full", brightness, |level| level.is_some_and(|l| l.level == 1.0));
    services.send(Command::SetBrightness(-3.0));
    harness.events.latest("the lowest level", brightness, |level| level.is_some_and(|l| (l.level - 0.01).abs() < 1e-6));
    services.send(Command::SetBrightness(f32::NAN));

    // A change from outside (a hardware key) is seen while the service
    // still follows the last command, and on Refresh at any time.
    std::fs::write(harness.path("backlight").join("acpi_video0/brightness"), "500\n").unwrap();
    harness.events.latest("the level set from outside", brightness, |level| level.is_some_and(|l| (l.level - 0.5).abs() < 1e-6));
    std::fs::write(harness.path("backlight").join("acpi_video0/brightness"), "300\n").unwrap();
    services.send(Command::Refresh(Service::Brightness));
    harness.events.latest("the level after Refresh", brightness, |level| level.is_some_and(|l| (l.level - 0.3).abs() < 1e-6));
}

#[test]
fn brightness_drag_sends_the_latest_level() {
    let harness = Harness::new();
    backlight(&harness, "panel", "raw", 100, 1000);
    let logind = logind(&harness.system);
    let root = harness.path("backlight");
    logind.on(LOGIN1_SESSION, "SetBrightness", move |_, call| {
        let (_, name, value): (String, String, u32) = call.args();
        std::fs::write(root.join(name).join("brightness"), format!("{value}\n")).unwrap();
        Reply::Done
    });
    let services = harness.start(status_config());
    harness.events.latest("a level", brightness, |level| level.is_some());
    for step in 1..=50 {
        services.send(Command::SetBrightness(0.4 + step as f32 / 100.0));
    }
    harness.events.latest("the last level of the drag", brightness, |level| level.is_some_and(|l| (l.level - 0.9).abs() < 1e-6));
    // Far fewer calls than commands: the levels in between were skipped.
    assert!(logind.calls("SetBrightness").len() < 50);
}

// --- session ------------------------------------------------------------------------

fn session(event: &Event) -> Option<SessionEvent> {
    match event {
        Event::Session(event) => Some(*event),
        _ => None,
    }
}

#[test]
fn session_reports_lock_unlock_and_sleep() {
    let harness = Harness::new();
    let logind = logind(&harness.system);
    let _services = harness.start(only(|config| config.session = true));
    // The service has found its session when it has subscribed to it;
    // signals sent before that are not for it, so send until one arrives.
    let lock = || logind.signal(SESSION_PATH, LOGIN1_SESSION, "Lock", &());
    eventually("the lock signal to arrive", || {
        lock();
        harness.events.count(|event| session(event) == Some(SessionEvent::Lock)) > 0
    });
    let before = harness.events.len();
    logind.signal(SESSION_PATH, LOGIN1_SESSION, "Unlock", &());
    logind.signal(LOGIN1_PATH, LOGIN1_MANAGER, "PrepareForSleep", &(true,));
    logind.signal(LOGIN1_PATH, LOGIN1_MANAGER, "PrepareForSleep", &(false,));
    // Another session's lock is not ours.
    logind.signal("/org/freedesktop/login1/session/_38", LOGIN1_SESSION, "Lock", &());
    logind.signal(LOGIN1_PATH, LOGIN1_MANAGER, "PrepareForSleep", &(true,));
    let seen = harness.events.wait("unlock and three sleep events", |events| {
        let seen: Vec<SessionEvent> = events.iter().skip(before).filter_map(session).filter(|event| *event != SessionEvent::Lock).collect();
        (seen.len() >= 4).then_some(seen)
    });
    assert_eq!(
        seen,
        [
            SessionEvent::Unlock,
            SessionEvent::PrepareForSleep(true),
            SessionEvent::PrepareForSleep(false),
            SessionEvent::PrepareForSleep(true),
        ]
    );
}

#[test]
fn session_finds_logind_when_it_starts_later_and_ignores_impostors() {
    let harness = Harness::new();
    let _services = harness.start(only(|config| config.session = true));
    // Somebody who is not logind sends the same signals.
    let impostor = Peer::start(&harness.system, &[]);
    impostor.signal(LOGIN1_PATH, LOGIN1_MANAGER, "PrepareForSleep", &(true,));
    impostor.signal(SESSION_PATH, LOGIN1_SESSION, "Lock", &());

    let logind = logind(&harness.system);
    eventually("the unlock signal to arrive", || {
        logind.signal(SESSION_PATH, LOGIN1_SESSION, "Unlock", &());
        harness.events.count(|event| session(event) == Some(SessionEvent::Unlock)) > 0
    });
    assert_eq!(harness.events.count(|event| session(event) == Some(SessionEvent::Lock)), 0);
    assert_eq!(harness.events.count(|event| session(event) == Some(SessionEvent::PrepareForSleep(true))), 0);
}

#[test]
fn status_only_runs_no_session_listener() {
    let harness = Harness::new();
    let logind = logind(&harness.system);
    let _services = harness.start(Config::status_only());
    harness.events.latest("a power state", power, |state| state.can_power_off);
    logind.signal(LOGIN1_PATH, LOGIN1_MANAGER, "PrepareForSleep", &(true,));
    // The power service reads logind on the same bus: once it has seen a
    // change made after the signal, the signal has been delivered too.
    logind.set("/test", "test", "CanPowerOff", owned("no"));
    logind.release(LOGIN1);
    logind.claim(LOGIN1);
    harness.events.latest("the change", power, |state| !state.can_power_off);
    assert_eq!(harness.events.count(|event| session(event).is_some()), 0);
}
