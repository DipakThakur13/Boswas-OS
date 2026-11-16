//! Power: the battery (UPower), the power profile (power-profiles-daemon)
//! and what logind lets this session do. `Command::Power` goes to logind.

use std::time::Duration;

use async_channel::{Receiver, Sender};
use async_executor::Task;
use futures_lite::future;
use zbus::{zvariant::Value, Connection};

use crate::{
    logind::{own_session, LOGIN1, MANAGER, MANAGER_PATH},
    runtime::{
        call, call_raw, get_all, idle, name_owner, owner_rule, set_property, signal_rule, subscribe, wake, Backoff, Bus,
        BusKind, CallResult, Ctx, Debounce, Exec, Flag, IdleEnd, Latest, Service, Wake, PROPERTIES,
    },
    var::{as_dict, as_list, as_str, prop, prop_bool, prop_f64, prop_i64, prop_str, prop_u32},
    Battery, Command, Environment, Event, PowerAction, PowerProfile, PowerState, Service as Which,
};

const UPOWER: &str = "org.freedesktop.UPower";
const UPOWER_PATH: &str = "/org/freedesktop/UPower";
const DISPLAY_DEVICE: &str = "/org/freedesktop/UPower/devices/DisplayDevice";
const UPOWER_DEVICE: &str = "org.freedesktop.UPower.Device";

/// power-profiles-daemon: its current name, then the one it had before 0.20.
const PROFILE_SERVICES: [(&str, &str); 2] = [
    ("org.freedesktop.UPower.PowerProfiles", "/org/freedesktop/UPower/PowerProfiles"),
    ("net.hadess.PowerProfiles", "/net/hadess/PowerProfiles"),
];

/// A power action waits for the person when logind asks polkit, so it may
/// take as long as an authentication dialog stays open.
const ACTION_TIMEOUT: Duration = Duration::from_secs(120);

pub(crate) struct Power {
    ctx: Ctx,
    ex: Exec,
    state: Latest<PowerState>,
    /// The profile service that answered last, to send `SetPowerProfile` to.
    profile_service: Option<(&'static str, &'static str)>,
    /// Actions on their way to logind.
    actions: Vec<Task<()>>,
}

enum Msg {
    Changed,
    Lost,
}

fn unavailable() -> PowerState {
    PowerState {
        // Without UPower nothing says the machine runs on a battery.
        on_ac: true,
        ..PowerState::default()
    }
}

impl Service for Power {
    fn new(ctx: Ctx, ex: Exec) -> Self {
        Self {
            ctx,
            ex,
            state: Latest::default(),
            profile_service: None,
            actions: Vec::new(),
        }
    }

    async fn run(&mut self, commands: Receiver<Command>) {
        let mut backoff = Backoff::new(Duration::from_secs(30));
        loop {
            match Bus::connect(BusKind::System, &self.ctx.env, &self.ex).await {
                Ok(bus) => {
                    backoff.reset();
                    match self.serve(&bus, &commands).await {
                        Ok(true) => return,
                        Ok(false) => {}
                        Err(error) => tracing::debug!(%error, "power: the system bus stopped answering"),
                    }
                }
                Err(error) => tracing::debug!(%error, "power: no system bus"),
            }
            self.actions.clear();
            self.profile_service = None;
            self.publish(unavailable(), false);
            match idle(&commands, backoff.next(), |command| matches!(command, Command::Refresh(Which::Power))).await {
                IdleEnd::Closed => return,
                IdleEnd::Retry => self.publish(unavailable(), true),
                IdleEnd::Elapsed => {}
            }
        }
    }
}

impl Power {
    fn publish(&mut self, state: PowerState, force: bool) {
        if let Some(state) = self.state.update(state, force) {
            self.ctx.emit(Event::Power(state));
        }
    }

    /// True when the service is to end, false when the bus was lost.
    async fn serve(&mut self, bus: &Bus, commands: &Receiver<Command>) -> CallResult<bool> {
        let (inbox, messages) = async_channel::unbounded::<Msg>();
        let conn = &bus.conn;
        let flag = Flag::default();
        let _subscriptions = self.subscribe(conn, &inbox, &flag).await?;
        let mut debounce = Debounce::default();
        debounce.trigger_now();
        let mut force = false;
        loop {
            match wake(commands, &messages, debounce.deadline()).await {
                Wake::Closed => return Ok(true),
                Wake::Message(Msg::Lost) => return Ok(false),
                Wake::Message(Msg::Changed) => debounce.trigger(),
                Wake::Command(Command::Refresh(Which::Power)) => {
                    force = true;
                    debounce.trigger_now();
                }
                // The lock screen and the greeter show the state and
                // change nothing.
                Wake::Command(Command::SetPowerProfile(profile)) if !self.ctx.restricted => {
                    self.set_profile(conn, profile).await;
                    debounce.trigger();
                }
                Wake::Command(Command::Power(action))
                    if !self.ctx.restricted || (self.ctx.power_actions && action != PowerAction::LogOut) =>
                {
                    self.act(conn, action)
                }
                Wake::Command(_) => {}
                Wake::Timer => {
                    if debounce.take_due() {
                        flag.clear();
                        let state = self.read(conn).await;
                        self.publish(state, std::mem::take(&mut force));
                    }
                }
            }
        }
    }

    async fn subscribe(&self, conn: &Connection, inbox: &Sender<Msg>, flag: &Flag) -> CallResult<Vec<Task<()>>> {
        let mut tasks = Vec::new();
        let mut names = vec![UPOWER, LOGIN1];
        names.extend(PROFILE_SERVICES.iter().map(|(name, _)| *name));
        for name in names {
            let changed = flag.clone();
            tasks.push(
                subscribe(
                    conn,
                    &self.ex,
                    owner_rule(name)?,
                    inbox,
                    move |_| changed.raise().then_some(Msg::Changed),
                    || Msg::Lost,
                )
                .await?,
            );
        }
        let mut sources = vec![(UPOWER, UPOWER_PATH), (UPOWER, DISPLAY_DEVICE)];
        sources.extend(PROFILE_SERVICES);
        for (name, path) in sources {
            let changed = flag.clone();
            tasks.push(
                subscribe(
                    conn,
                    &self.ex,
                    signal_rule(Some(name), PROPERTIES, Some("PropertiesChanged"), Some(path))?.build(),
                    inbox,
                    move |_| changed.raise().then_some(Msg::Changed),
                    || Msg::Lost,
                )
                .await?,
            );
        }
        Ok(tasks)
    }

    async fn read(&mut self, conn: &Connection) -> PowerState {
        let ((battery, on_ac), ((profile, profiles, service), can)) = future::zip(
            read_battery(conn),
            future::zip(read_profiles(conn), read_abilities(conn)),
        )
        .await;
        self.profile_service = service;
        PowerState {
            battery,
            on_ac,
            profile,
            profiles,
            can_suspend: can[0],
            can_hibernate: can[1],
            can_reboot: can[2],
            can_power_off: can[3],
        }
    }

    async fn set_profile(&mut self, conn: &Connection, profile: PowerProfile) {
        let Some((name, path)) = self.profile_service else {
            return;
        };
        let offered = self.state.get().map(|state| state.profiles.contains(&profile)).unwrap_or(false);
        if !offered {
            return;
        }
        if let Err(error) = set_property(conn, name, path, name, "ActiveProfile", Value::from(profile_name(profile))).await {
            tracing::warn!(%error, "power: the profile was not changed");
        }
    }

    /// Sends a power action to logind without waiting for it here: logind
    /// answers only after polkit is satisfied.
    fn act(&mut self, conn: &Connection, action: PowerAction) {
        self.actions.retain(|task| !task.is_finished());
        let conn = conn.clone();
        let env = self.ctx.env.clone();
        self.actions.push(self.ex.spawn(async move {
            if let Err(error) = perform(&conn, &env, action).await {
                tracing::warn!(?action, %error, "power: logind refused");
            }
        }));
    }
}

async fn perform(conn: &Connection, env: &Environment, action: PowerAction) -> CallResult<()> {
    let method = match action {
        PowerAction::Suspend => "Suspend",
        PowerAction::Hibernate => "Hibernate",
        PowerAction::Reboot => "Reboot",
        PowerAction::PowerOff => "PowerOff",
        PowerAction::LogOut => {
            let Some(session) = own_session(conn, env).await else {
                tracing::warn!("power: this process is in no logind session, nothing to end");
                return Ok(());
            };
            call_raw(conn, ACTION_TIMEOUT, LOGIN1, MANAGER_PATH, MANAGER, "TerminateSession", &(session.id.as_str(),)).await?;
            return Ok(());
        }
    };
    // interactive = true: logind may ask polkit, which asks our agent.
    call_raw(conn, ACTION_TIMEOUT, LOGIN1, MANAGER_PATH, MANAGER, method, &(true,)).await?;
    Ok(())
}

/// The battery and whether the machine is on external power.
async fn read_battery(conn: &Connection) -> (Option<Battery>, bool) {
    // UPower starts on demand, so it is asked without looking first
    // whether it runs.
    let Ok(manager) = get_all(conn, UPOWER, UPOWER_PATH, UPOWER).await else {
        return (None, true);
    };
    let on_ac = !prop_bool(&manager, "OnBattery").unwrap_or(false);
    let Ok(device) = get_all(conn, UPOWER, DISPLAY_DEVICE, UPOWER_DEVICE).await else {
        return (None, on_ac);
    };
    (battery_of(&device), on_ac)
}

fn battery_of(device: &crate::var::Props) -> Option<Battery> {
    // Type 2 is a battery, 3 a UPS; the display device is of type 0 or 1
    // on a machine without one.
    let kind = prop_u32(device, "Type")?;
    if !(kind == 2 || kind == 3) || !prop_bool(device, "IsPresent").unwrap_or(false) {
        return None;
    }
    let percent = prop_f64(device, "Percentage")?.clamp(0.0, 100.0).round() as u8;
    // 1 charging, 2 discharging, 3 empty, 4 full, 5 and 6 pending.
    let state = prop_u32(device, "State").unwrap_or(0);
    let seconds = |name: &str| prop_i64(device, name).filter(|seconds| *seconds > 0).map(|seconds| seconds as u64);
    let seconds_left = match state {
        1 => seconds("TimeToFull"),
        2 => seconds("TimeToEmpty"),
        _ => None,
    };
    Some(Battery {
        percent,
        charging: state == 1,
        full: state == 4,
        seconds_left,
    })
}

type ProfileService = Option<(&'static str, &'static str)>;

async fn read_profiles(conn: &Connection) -> (Option<PowerProfile>, Vec<PowerProfile>, ProfileService) {
    for (name, path) in PROFILE_SERVICES {
        let Ok(props) = get_all(conn, name, path, name).await else {
            continue;
        };
        let active = prop_str(&props, "ActiveProfile").and_then(|name| profile_from(&name));
        let mut profiles: Vec<PowerProfile> = prop(&props, "Profiles")
            .and_then(as_list)
            .map(|list| {
                list.iter()
                    .filter_map(|entry| {
                        as_dict(entry).into_iter().find(|(key, _)| *key == "Profile").and_then(|(_, value)| as_str(value))
                    })
                    .filter_map(profile_from)
                    .collect()
            })
            .unwrap_or_default();
        profiles.sort_by_key(|profile| *profile as u8);
        profiles.dedup();
        return (active, profiles, Some((name, path)));
    }
    (None, Vec::new(), None)
}

fn profile_from(name: &str) -> Option<PowerProfile> {
    match name {
        "power-saver" => Some(PowerProfile::PowerSaver),
        "balanced" => Some(PowerProfile::Balanced),
        "performance" => Some(PowerProfile::Performance),
        _ => None,
    }
}

fn profile_name(profile: PowerProfile) -> &'static str {
    match profile {
        PowerProfile::PowerSaver => "power-saver",
        PowerProfile::Balanced => "balanced",
        PowerProfile::Performance => "performance",
    }
}

/// CanSuspend, CanHibernate, CanReboot, CanPowerOff. "challenge" counts:
/// the action is allowed once the person has authenticated.
async fn read_abilities(conn: &Connection) -> [bool; 4] {
    if name_owner(conn, LOGIN1).await.is_none() {
        return [false; 4];
    }
    let ask = |method: &'static str| async move {
        let answer: CallResult<String> = call(conn, LOGIN1, MANAGER_PATH, MANAGER, method, &()).await;
        matches!(answer.as_deref(), Ok("yes") | Ok("challenge"))
    };
    let ((suspend, hibernate), (reboot, power_off)) = future::zip(
        future::zip(ask("CanSuspend"), ask("CanHibernate")),
        future::zip(ask("CanReboot"), ask("CanPowerOff")),
    )
    .await;
    [suspend, hibernate, reboot, power_off]
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::var::Props;
    use zbus::zvariant::OwnedValue;

    fn device(kind: u32, present: bool, percent: f64, state: u32, to_empty: i64, to_full: i64) -> Props {
        let mut props = Props::new();
        props.insert("Type".into(), OwnedValue::from(kind));
        props.insert("IsPresent".into(), OwnedValue::from(present));
        props.insert("Percentage".into(), OwnedValue::from(percent));
        props.insert("State".into(), OwnedValue::from(state));
        props.insert("TimeToEmpty".into(), OwnedValue::from(to_empty));
        props.insert("TimeToFull".into(), OwnedValue::from(to_full));
        props
    }

    #[test]
    fn battery_states() {
        let discharging = battery_of(&device(2, true, 54.6, 2, 3600, 0)).unwrap();
        assert_eq!(
            discharging,
            Battery { percent: 55, charging: false, full: false, seconds_left: Some(3600) }
        );
        let charging = battery_of(&device(2, true, 80.0, 1, 0, 1200)).unwrap();
        assert!(charging.charging && charging.seconds_left == Some(1200));
        let full = battery_of(&device(2, true, 100.0, 4, 0, 0)).unwrap();
        assert!(full.full && !full.charging && full.seconds_left.is_none());
        // Out-of-range values from a confused daemon stay in range.
        assert_eq!(battery_of(&device(2, true, 250.0, 2, -5, 0)).unwrap().percent, 100);
        assert_eq!(battery_of(&device(2, true, 250.0, 2, -5, 0)).unwrap().seconds_left, None);
    }

    #[test]
    fn no_battery() {
        assert_eq!(battery_of(&device(1, true, 0.0, 0, 0, 0)), None);
        assert_eq!(battery_of(&device(2, false, 50.0, 2, 0, 0)), None);
        assert_eq!(battery_of(&Props::new()), None);
    }

    #[test]
    fn profile_names_round_trip() {
        for profile in [PowerProfile::PowerSaver, PowerProfile::Balanced, PowerProfile::Performance] {
            assert_eq!(profile_from(profile_name(profile)), Some(profile));
        }
        assert_eq!(profile_from("turbo"), None);
    }
}
