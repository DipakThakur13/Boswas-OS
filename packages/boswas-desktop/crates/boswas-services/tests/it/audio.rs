//! Audio against stand-ins for `pw-dump` and `wpctl`: shell scripts the
//! tests write. The stand-in for `pw-dump --monitor` prints a file and
//! then follows it, so a test changes the sound server's state by
//! appending a JSON document to that file.

use std::{io::Write, path::PathBuf};

use boswas_services::{AudioDevice, AudioState, Command, Config, Event, Service};

use crate::support::*;

fn audio(event: &Event) -> Option<&AudioState> {
    match event {
        Event::Audio(state) => Some(state),
        _ => None,
    }
}

const DUMP: &str = r#"[
  { "id": 34, "type": "PipeWire:Interface:Metadata", "props": { "metadata.name": "default" },
    "metadata": [
      { "subject": 0, "key": "default.audio.sink", "type": "Spa:String:JSON", "value": { "name": "speakers" } },
      { "subject": 0, "key": "default.audio.source", "type": "Spa:String:JSON", "value": { "name": "mic" } }
    ] },
  { "id": 50, "type": "PipeWire:Interface:Node",
    "info": { "props": { "media.class": "Audio/Sink", "node.name": "speakers", "node.description": "Speakers" },
              "params": { "Props": [ { "mute": false, "channelVolumes": [ 0.125, 0.125 ] } ] } } },
  { "id": 51, "type": "PipeWire:Interface:Node",
    "info": { "props": { "media.class": "Audio/Sink", "node.name": "hdmi", "node.description": "HDMI" },
              "params": { "Props": [ { "mute": true, "channelVolumes": [ 1.0 ] } ] } } },
  { "id": 52, "type": "PipeWire:Interface:Node",
    "info": { "props": { "media.class": "Audio/Source", "node.name": "mic", "node.description": "Microphone" },
              "params": { "Props": [ { "mute": false, "channelVolumes": [ 0.343 ] } ] } } }
]
"#;

struct Sound {
    harness: Harness,
    json: PathBuf,
}

impl Sound {
    /// A sound server with two sinks and a microphone.
    fn new() -> Sound {
        let harness = Harness::new();
        let json = harness.path("pw.json");
        std::fs::write(&json, DUMP).unwrap();
        let dir = harness.dir.path().display().to_string();
        // `exec`: the process the service started (and kills) is `tail`.
        harness.script("pw-dump", &format!("[ \"$1\" = --monitor ] || exit 2\necho $$ > {dir}/pw-dump.pid\nexec tail -n +1 -f {dir}/pw.json"));
        harness.script("wpctl", &format!("echo \"$*\" >> {dir}/wpctl.log"));
        Sound { harness, json }
    }

    /// The sound server reports a change.
    fn change(&self, document: &str) {
        let mut file = std::fs::OpenOptions::new().append(true).open(&self.json).unwrap();
        file.write_all(format!("{document}\n").as_bytes()).unwrap();
    }

    fn sink(&self, id: u32, name: &str, description: &str, linear: f64, muted: bool) {
        self.change(&format!(
            r#"[ {{ "id": {id}, "type": "PipeWire:Interface:Node",
                  "info": {{ "props": {{ "media.class": "Audio/Sink", "node.name": "{name}", "node.description": "{description}" }},
                            "params": {{ "Props": [ {{ "mute": {muted}, "channelVolumes": [ {linear}, {linear} ] }} ] }} }} }} ]"#
        ));
    }

    fn wpctl_log(&self) -> Vec<String> {
        std::fs::read_to_string(self.harness.path("wpctl.log")).unwrap_or_default().lines().map(str::to_owned).collect()
    }

    /// Waits until `wpctl` has been run with `arguments`.
    fn wait_wpctl(&self, arguments: &str) {
        eventually(&format!("wpctl {arguments}"), || self.wpctl_log().iter().any(|line| line == arguments));
    }

    fn monitor_pid(&self) -> Option<u32> {
        std::fs::read_to_string(self.harness.path("pw-dump.pid")).ok()?.trim().parse().ok()
    }
}

fn running(pid: u32) -> bool {
    // A killed child that was not reaped yet still has an entry; it is a
    // zombie then (state Z).
    match std::fs::read_to_string(format!("/proc/{pid}/stat")) {
        Ok(stat) => !stat.rsplit(')').next().unwrap_or("").trim_start().starts_with('Z'),
        Err(_) => false,
    }
}

#[test]
fn without_the_tools_audio_is_unavailable() {
    let harness = Harness::new();
    let services = harness.start(status_config());
    assert_eq!(harness.events.latest("the first state", audio, |_| true), AudioState::default());
    services.send(Command::SetVolume(0.5));
    services.send(Command::SetMuted(true));
    services.send(Command::SetDefaultOutput { id: 1 });
    let before = harness.events.len();
    services.send(Command::Refresh(Service::Audio));
    assert!(!harness.events.after(before, "the state again", |event| audio(event).cloned()).available);
}

#[test]
fn devices_volumes_and_defaults_are_read_and_followed() {
    let sound = Sound::new();
    let _services = sound.harness.start(status_config());
    let state = sound.harness.events.latest("the devices", audio, |state| state.available);
    let speakers = AudioDevice { id: 50, name: "Speakers".into(), volume: 0.5, muted: false, is_default: true };
    let hdmi = AudioDevice { id: 51, name: "HDMI".into(), volume: 1.0, muted: true, is_default: false };
    assert_eq!(state.outputs, vec![speakers.clone(), hdmi]);
    assert_eq!(state.output, Some(speakers));
    assert_eq!(state.inputs.len(), 1);
    let microphone = state.input.unwrap();
    assert_eq!((microphone.id, microphone.name.as_str(), microphone.is_default), (52, "Microphone", true));
    assert!((microphone.volume - 0.7).abs() < 1e-5);

    // Somebody turns the volume up and mutes.
    sound.sink(50, "speakers", "Speakers", 0.729, true);
    let state = sound.harness.events.latest("the new volume", audio, |state| state.output.as_ref().is_some_and(|output| output.muted));
    assert!((state.output.unwrap().volume - 0.9).abs() < 1e-5);

    // A headset is plugged in and becomes the default.
    sound.sink(60, "usb", "USB headset", 1.0, false);
    sound.change(
        r#"[ { "id": 34, "type": "PipeWire:Interface:Metadata", "props": { "metadata.name": "default" },
              "metadata": [ { "subject": 0, "key": "default.audio.sink", "value": { "name": "usb" } },
                            { "subject": 0, "key": "default.audio.source", "value": { "name": "mic" } } ] } ]"#,
    );
    let state = sound.harness.events.latest("the headset as default", audio, |state| state.output.as_ref().is_some_and(|output| output.id == 60));
    assert_eq!(state.outputs.len(), 3);
    assert!(!state.outputs[0].is_default);

    // It is unplugged; the default has not moved yet: no default output.
    sound.change(r#"[ { "id": 60, "info": null } ]"#);
    let state = sound.harness.events.latest("the headset gone", audio, |state| state.outputs.len() == 2);
    assert_eq!(state.output, None);
    // The microphone goes too.
    sound.change(r#"[ { "id": 52, "info": null } ]"#);
    sound.harness.events.latest("no input", audio, |state| state.inputs.is_empty() && state.input.is_none());
}

#[test]
fn volume_mute_and_default_are_set_with_wpctl() {
    let sound = Sound::new();
    let services = sound.harness.start(status_config());
    sound.harness.events.latest("the devices", audio, |state| state.available);

    services.send(Command::SetVolume(0.3));
    sound.wait_wpctl("set-volume -l 1.5 50 0.300");
    services.send(Command::SetMuted(true));
    sound.wait_wpctl("set-mute 50 1");
    services.send(Command::SetMuted(false));
    sound.wait_wpctl("set-mute 50 0");
    services.send(Command::SetInputVolume(0.55));
    sound.wait_wpctl("set-volume -l 1.5 52 0.550");
    services.send(Command::SetInputMuted(true));
    sound.wait_wpctl("set-mute 52 1");
    services.send(Command::SetDefaultOutput { id: 51 });
    sound.wait_wpctl("set-default 51");
    services.send(Command::SetDefaultInput { id: 52 });
    sound.wait_wpctl("set-default 52");

    // Out of range is clamped; not a number, unknown ids and ids of the
    // wrong direction are not passed on.
    services.send(Command::SetVolume(f32::NAN));
    services.send(Command::SetVolume(f32::INFINITY));
    services.send(Command::SetDefaultOutput { id: 999 });
    services.send(Command::SetDefaultOutput { id: 52 });
    services.send(Command::SetDefaultInput { id: 50 });
    services.send(Command::SetDefaultInput { id: 34 });
    services.send(Command::SetVolume(-4.0));
    sound.wait_wpctl("set-volume -l 1.5 50 0.000");
    services.send(Command::SetVolume(9.0));
    sound.wait_wpctl("set-volume -l 1.5 50 1.500");
    let log = sound.wpctl_log();
    assert_eq!(log.iter().filter(|line| line.starts_with("set-default")).count(), 2, "{log:?}");
    assert!(log.iter().all(|line| !line.contains("NaN") && !line.contains("inf")), "{log:?}");

    // A drag of the slider: the levels in between are skipped, the last
    // one is set.
    let before = sound.wpctl_log().len();
    for step in 0..200 {
        services.send(Command::SetVolume(step as f32 / 400.0));
    }
    services.send(Command::SetVolume(0.777));
    sound.wait_wpctl("set-volume -l 1.5 50 0.777");
    assert!(sound.wpctl_log().len() - before < 150, "every level of the drag was set");
    assert_eq!(sound.wpctl_log().last().map(String::as_str), Some("set-volume -l 1.5 50 0.777"));
}

#[test]
fn the_monitor_is_restarted_when_the_sound_server_goes_away() {
    let sound = Sound::new();
    let services = sound.harness.start(status_config());
    sound.harness.events.latest("the devices", audio, |state| state.available);
    let first = sound.monitor_pid().unwrap();
    assert!(running(first));

    // The sound server dies: pw-dump ends.
    std::process::Command::new("kill").arg(first.to_string()).status().unwrap();
    sound.harness.events.latest("unavailable", audio, |state| !state.available && state.outputs.is_empty());
    // It is started again and the devices are back.
    sound.harness.events.latest("available again", audio, |state| state.available && state.outputs.len() == 2);
    let second = sound.monitor_pid().unwrap();
    assert_ne!(first, second);

    // Dropping the services kills the monitor.
    assert!(running(second));
    drop(services);
    eventually("the monitor to be killed", || !running(second));
}

#[test]
fn output_that_is_not_json_means_no_audio() {
    let harness = Harness::new();
    let dir = harness.dir.path().display().to_string();
    harness.script("pw-dump", &format!("echo run >> {dir}/runs; echo 'pw-dump: cannot connect to the daemon'; echo '<html>'; exec sleep 600"));
    let services = harness.start(status_config());
    // The stand-in ran and its output was thrown away.
    eventually("pw-dump to be run", || harness.path("runs").exists());
    let before = harness.events.len();
    services.send(Command::Refresh(Service::Audio));
    assert_eq!(harness.events.after(before, "the state", |event| audio(event).cloned()), AudioState::default());
    assert_eq!(harness.events.count(|event| audio(event).is_some_and(|state| state.available)), 0);

    // The tools are installed properly later: the service finds them.
    std::fs::write(harness.path("pw.json"), DUMP).unwrap();
    harness.script("pw-dump", &format!("exec tail -n +1 -f {dir}/pw.json"));
    eventually("audio to become available", || {
        services.send(Command::Refresh(Service::Audio));
        harness.events.all().iter().rev().find_map(audio).is_some_and(|state| state.available)
    });
}

#[test]
fn the_lock_screen_may_change_the_volume_but_not_the_default_device() {
    let sound = Sound::new();
    let services = sound.harness.start(Config::status_only());
    sound.harness.events.latest("the devices", audio, |state| state.available);
    services.send(Command::SetDefaultOutput { id: 51 });
    services.send(Command::SetDefaultInput { id: 52 });
    services.send(Command::SetMuted(true));
    services.send(Command::SetVolume(0.2));
    sound.wait_wpctl("set-volume -l 1.5 50 0.200");
    assert_eq!(sound.wpctl_log(), ["set-mute 50 1", "set-volume -l 1.5 50 0.200"]);
}
