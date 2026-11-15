//! Audio: PipeWire through its command-line tools, with no binding to its
//! C library.
//!
//! `pw-dump --monitor` prints every object of the sound server as JSON and
//! then each change; a reader thread turns that into the few facts the
//! shell shows. Volume, mute and the default device are set with `wpctl`.
//! Both programs are run by path with argument lists; nothing goes through
//! a shell.

use std::{
    cell::Cell,
    collections::BTreeMap,
    io::{self, BufReader, Read},
    path::Path,
    process::{Child, Command as Process, Stdio},
    rc::Rc,
    time::{Duration, Instant},
};

use async_channel::{Receiver, Sender};
use async_executor::Task;
use async_io::Timer;
use serde_json::Value;

use crate::{
    runtime::{earliest, wake, Backoff, Ctx, Debounce, Exec, Latest, Service, Wake},
    var::clean,
    AudioDevice, AudioState, Command, Event, Service as Which,
};

/// One JSON document of `pw-dump` may be this large; a sound server with
/// hundreds of nodes prints a few megabytes.
const MAX_DOCUMENT: usize = 64 * 1024 * 1024;
/// Objects kept; beyond this the monitor is restarted.
const MAX_OBJECTS: usize = 8192;
/// `wpctl` is given this long.
const SET_TIMEOUT: Duration = Duration::from_secs(3);
const MAX_VOLUME: f32 = 1.5;

pub(crate) struct Audio {
    ctx: Ctx,
    ex: Exec,
    state: Latest<AudioState>,
    monitor: Option<Monitor>,
    /// Counts the monitors started, to tell an old one's last words from
    /// the current one's.
    generation: u64,
    /// The first document of the current monitor has arrived.
    synced: bool,
    objects: BTreeMap<u32, Object>,
    /// Settings waiting for `wpctl` to be free, the latest of each kind.
    waiting: Vec<Setting>,
    setting: Option<Task<()>>,
}

/// The running `pw-dump --monitor`. Dropping it kills the program.
struct Monitor {
    child: Child,
}

impl Drop for Monitor {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// What the shell needs of an object of the sound server.
#[derive(Clone, Debug, PartialEq)]
enum Object {
    Node(Node),
    /// The "default" metadata: the names of the default sink and source.
    Defaults { sink: Option<String>, source: Option<String> },
}

#[derive(Clone, Debug, PartialEq)]
struct Node {
    output: bool,
    input: bool,
    name: String,
    description: String,
    volume: f32,
    muted: bool,
}

/// A change the monitor reported.
#[derive(Debug, PartialEq)]
enum Change {
    Set(u32, Object),
    /// The object is gone, or is of no interest.
    Drop(u32),
}

enum Msg {
    Changes { generation: u64, changes: Vec<Change> },
    Ended { generation: u64 },
    SetDone,
}

#[derive(Clone, Debug, PartialEq)]
enum Setting {
    Volume { input: bool, level: f32 },
    Mute { input: bool, muted: bool },
    Default { id: u32 },
}

impl Setting {
    /// Two settings of the same kind: the later one replaces the earlier.
    fn same_kind(&self, other: &Setting) -> bool {
        match (self, other) {
            (Setting::Volume { input: a, .. }, Setting::Volume { input: b, .. }) => a == b,
            (Setting::Mute { input: a, .. }, Setting::Mute { input: b, .. }) => a == b,
            (Setting::Default { .. }, Setting::Default { .. }) => false,
            _ => false,
        }
    }
}

impl Service for Audio {
    fn new(ctx: Ctx, ex: Exec) -> Self {
        Self {
            ctx,
            ex,
            state: Latest::default(),
            monitor: None,
            generation: 0,
            synced: false,
            objects: BTreeMap::new(),
            waiting: Vec::new(),
            setting: None,
        }
    }

    async fn run(&mut self, commands: Receiver<Command>) {
        let (inbox, messages) = async_channel::unbounded::<Msg>();
        let mut backoff = Backoff::new(Duration::from_secs(30));
        let mut debounce = Debounce::default();
        let mut retry: Option<Instant> = Some(Instant::now());
        self.publish(false);
        loop {
            match wake(&commands, &messages, earliest(debounce.deadline(), retry)).await {
                Wake::Closed => return,
                Wake::Message(Msg::Changes { generation, changes }) => {
                    if generation != self.generation || self.monitor.is_none() {
                        continue;
                    }
                    if !self.synced {
                        // The server answers: the next failure starts the
                        // waiting from the beginning.
                        self.synced = true;
                        backoff.reset();
                    }
                    for change in changes {
                        match change {
                            Change::Set(id, object) => {
                                self.objects.insert(id, object);
                            }
                            Change::Drop(id) => {
                                self.objects.remove(&id);
                            }
                        }
                    }
                    if self.objects.len() > MAX_OBJECTS {
                        tracing::warn!("audio: the sound server lists too many objects; starting over");
                        self.stop_monitor();
                        retry = Some(Instant::now() + backoff.next());
                    }
                    debounce.trigger();
                }
                Wake::Message(Msg::Ended { generation }) => {
                    if generation == self.generation && self.monitor.is_some() {
                        tracing::debug!("audio: pw-dump ended; the sound server is gone or restarting");
                        self.stop_monitor();
                        retry = Some(Instant::now() + backoff.next());
                        debounce.trigger();
                    }
                }
                Wake::Message(Msg::SetDone) => {
                    self.setting = None;
                    self.next_setting(&inbox);
                }
                Wake::Command(Command::Refresh(Which::Audio)) => {
                    if self.monitor.is_none() {
                        retry = Some(Instant::now());
                    }
                    self.publish(true);
                }
                Wake::Command(command) => {
                    if let Some(setting) = self.setting_for(command) {
                        self.waiting.retain(|waiting| !waiting.same_kind(&setting));
                        self.waiting.push(setting);
                        self.next_setting(&inbox);
                    }
                }
                Wake::Timer => {
                    if retry.is_some_and(|at| at <= Instant::now()) {
                        retry = None;
                        if self.monitor.is_none() && !self.start_monitor(&inbox) {
                            retry = Some(Instant::now() + backoff.next());
                        }
                    }
                    if debounce.take_due() {
                        self.publish(false);
                    }
                }
            }
        }
    }
}

impl Audio {
    fn publish(&mut self, force: bool) {
        let state = if self.monitor.is_some() && self.synced { describe(&self.objects) } else { AudioState::default() };
        if let Some(state) = self.state.update(state, force) {
            self.ctx.emit(Event::Audio(state));
        }
    }

    fn stop_monitor(&mut self) {
        self.monitor = None;
        self.synced = false;
        self.objects.clear();
    }

    /// Starts `pw-dump --monitor` and the thread that reads it. False when
    /// the program could not be started (it is not installed).
    fn start_monitor(&mut self, inbox: &Sender<Msg>) -> bool {
        self.generation += 1;
        self.synced = false;
        self.objects.clear();
        let generation = self.generation;
        let spawned = Process::new(&self.ctx.env.pw_dump)
            .arg("--monitor")
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn();
        let mut child = match spawned {
            Ok(child) => child,
            Err(error) => {
                tracing::debug!(%error, "audio: pw-dump could not be started");
                return false;
            }
        };
        let Some(output) = child.stdout.take() else {
            let _ = child.kill();
            let _ = child.wait();
            return false;
        };
        let inbox = inbox.clone();
        let reader = std::thread::Builder::new().name("boswas-audio-reader".into()).spawn(move || {
            read_monitor(output, |changes| inbox.try_send(Msg::Changes { generation, changes }).is_ok());
            let _ = inbox.try_send(Msg::Ended { generation });
        });
        if reader.is_err() {
            let _ = child.kill();
            let _ = child.wait();
            return false;
        }
        self.monitor = Some(Monitor { child });
        true
    }

    /// The `wpctl` setting for a command, checked against what is known.
    fn setting_for(&self, command: Command) -> Option<Setting> {
        let level = |level: f32| level.is_finite().then(|| level.clamp(0.0, MAX_VOLUME));
        match command {
            Command::SetVolume(volume) => Some(Setting::Volume { input: false, level: level(volume)? }),
            Command::SetInputVolume(volume) => Some(Setting::Volume { input: true, level: level(volume)? }),
            Command::SetMuted(muted) => Some(Setting::Mute { input: false, muted }),
            Command::SetInputMuted(muted) => Some(Setting::Mute { input: true, muted }),
            // The default can only become a device that is there, of the
            // right direction; not on the lock screen.
            Command::SetDefaultOutput { id } if !self.ctx.restricted => {
                matches!(self.objects.get(&id), Some(Object::Node(node)) if node.output).then_some(Setting::Default { id })
            }
            Command::SetDefaultInput { id } if !self.ctx.restricted => {
                matches!(self.objects.get(&id), Some(Object::Node(node)) if node.input).then_some(Setting::Default { id })
            }
            _ => None,
        }
    }

    fn next_setting(&mut self, inbox: &Sender<Msg>) {
        if self.setting.is_some() || self.waiting.is_empty() {
            return;
        }
        let setting = self.waiting.remove(0);
        let state = self.state.get();
        let target = |input: bool| {
            let device = state.and_then(|state| if input { state.input.as_ref() } else { state.output.as_ref() });
            match device {
                Some(device) => device.id.to_string(),
                None if input => "@DEFAULT_AUDIO_SOURCE@".to_owned(),
                None => "@DEFAULT_AUDIO_SINK@".to_owned(),
            }
        };
        let arguments: Vec<String> = match setting {
            Setting::Volume { input, level } => {
                vec!["set-volume".into(), "-l".into(), format!("{MAX_VOLUME}"), target(input), format!("{level:.3}")]
            }
            Setting::Mute { input, muted } => vec!["set-mute".into(), target(input), if muted { "1" } else { "0" }.into()],
            Setting::Default { id } => vec!["set-default".into(), id.to_string()],
        };
        let program = self.ctx.env.wpctl.clone();
        let inbox = inbox.clone();
        self.setting = Some(self.ex.spawn(async move {
            run_tool(&program, &arguments).await;
            let _ = inbox.send(Msg::SetDone).await;
        }));
    }
}

/// Runs a program to its end, killing it when it takes too long.
async fn run_tool(program: &Path, arguments: &[String]) {
    let spawned = Process::new(program).args(arguments).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).spawn();
    let mut child = match spawned {
        Ok(child) => child,
        Err(error) => {
            tracing::debug!(%error, "audio: wpctl could not be started");
            return;
        }
    };
    let started = Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                if !status.success() {
                    tracing::debug!(%status, "audio: wpctl failed");
                }
                return;
            }
            Ok(None) if started.elapsed() < SET_TIMEOUT => {
                Timer::after(Duration::from_millis(4)).await;
            }
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return;
            }
        }
    }
}

// --- reading pw-dump --------------------------------------------------------------

/// Lets through `MAX_DOCUMENT` bytes per document and then fails, so that
/// output without end cannot fill the memory.
struct Budget<R> {
    inner: R,
    left: Rc<Cell<usize>>,
}

impl<R: Read> Read for Budget<R> {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        let left = self.left.get();
        if left == 0 {
            return Err(io::Error::other("pw-dump printed more than a document can hold"));
        }
        let limit = buffer.len().min(left);
        let read = self.inner.read(&mut buffer[..limit])?;
        self.left.set(left - read);
        Ok(read)
    }
}

/// Reads the stream of JSON documents `pw-dump --monitor` prints: first
/// an array of all objects, then an array of the changed ones after each
/// change. Ends when the output ends, is not JSON, or `sink` returns false.
fn read_monitor(output: impl Read, mut sink: impl FnMut(Vec<Change>) -> bool) {
    let left = Rc::new(Cell::new(MAX_DOCUMENT));
    let reader = BufReader::new(Budget { inner: output, left: left.clone() });
    for document in serde_json::Deserializer::from_reader(reader).into_iter::<Value>() {
        let Ok(document) = document else { return };
        left.set(MAX_DOCUMENT);
        let Some(objects) = document.as_array() else { return };
        let changes: Vec<Change> = objects.iter().filter_map(change).collect();
        if !sink(changes) {
            return;
        }
    }
}

/// What one object of a document means. `None` for something without an id.
fn change(object: &Value) -> Option<Change> {
    let id = u32::try_from(object.get("id")?.as_u64()?).ok()?;
    let kind = object.get("type").and_then(Value::as_str).unwrap_or("");
    let parsed = match kind {
        "PipeWire:Interface:Node" => node(object).map(Object::Node),
        "PipeWire:Interface:Metadata" => defaults(object),
        _ => None,
    };
    // A removed object has `"info": null`; an object of another kind may
    // have taken the id of one that was kept.
    Some(match parsed {
        Some(parsed) => Change::Set(id, parsed),
        None => Change::Drop(id),
    })
}

fn node(object: &Value) -> Option<Node> {
    let info = object.get("info")?;
    let props = info.get("props")?;
    let class = props.get("media.class")?.as_str()?;
    let (output, input) = match class {
        "Audio/Sink" => (true, false),
        "Audio/Source" | "Audio/Source/Virtual" => (false, true),
        "Audio/Duplex" => (true, true),
        _ => return None,
    };
    let text = |key: &str| props.get(key).and_then(Value::as_str).map(|text| clean(text, 128, false)).filter(|text| !text.is_empty());
    let name = props.get("node.name").and_then(Value::as_str).unwrap_or("").to_owned();
    let description = text("node.description").or_else(|| text("node.nick")).unwrap_or_else(|| clean(&name, 128, false));
    // The volume is in the "Props" parameter; there may be several, the
    // one with the channel volumes is the node's own.
    let params = info.get("params").and_then(|params| params.get("Props")).and_then(Value::as_array);
    let mut volume = 1.0f64;
    let mut muted = false;
    if let Some(params) = params {
        let own = params.iter().find(|param| param.get("channelVolumes").is_some()).or_else(|| params.iter().find(|param| param.get("volume").is_some()));
        if let Some(own) = own {
            let channels = own
                .get("channelVolumes")
                .and_then(Value::as_array)
                .and_then(|channels| channels.iter().filter_map(Value::as_f64).filter(|v| v.is_finite()).reduce(f64::max));
            volume = channels.or_else(|| own.get("volume").and_then(Value::as_f64)).unwrap_or(1.0);
            muted = own.get("mute").and_then(Value::as_bool).unwrap_or(false);
        }
    }
    Some(Node {
        output,
        input,
        name,
        description,
        volume: shown_volume(volume),
        muted,
    })
}

/// PipeWire stores a linear factor; people see its cube root (the same
/// scale `wpctl` and PulseAudio use).
fn shown_volume(linear: f64) -> f32 {
    if !linear.is_finite() || linear <= 0.0 {
        return 0.0;
    }
    (linear.cbrt() as f32).clamp(0.0, MAX_VOLUME)
}

/// The "default" metadata object: which sink and source are the defaults.
fn defaults(object: &Value) -> Option<Object> {
    let name = object.get("props").and_then(|props| props.get("metadata.name")).and_then(Value::as_str)?;
    if name != "default" {
        return None;
    }
    let entries = object.get("metadata").and_then(Value::as_array)?;
    let lookup = |key: &str| {
        entries.iter().find(|entry| entry.get("key").and_then(Value::as_str) == Some(key)).and_then(|entry| {
            let value = entry.get("value")?;
            // The value is JSON: `{ "name": "..." }`, sometimes as a string.
            match value {
                Value::Object(map) => map.get("name")?.as_str().map(str::to_owned),
                Value::String(text) => serde_json::from_str::<Value>(text).ok()?.get("name")?.as_str().map(str::to_owned),
                _ => None,
            }
        })
    };
    Some(Object::Defaults {
        sink: lookup("default.audio.sink").or_else(|| lookup("default.configured.audio.sink")),
        source: lookup("default.audio.source").or_else(|| lookup("default.configured.audio.source")),
    })
}

fn describe(objects: &BTreeMap<u32, Object>) -> AudioState {
    let (mut default_sink, mut default_source) = (None, None);
    for object in objects.values() {
        if let Object::Defaults { sink, source } = object {
            default_sink = sink.as_deref().or(default_sink);
            default_source = source.as_deref().or(default_source);
        }
    }
    let mut outputs = Vec::new();
    let mut inputs = Vec::new();
    for (id, object) in objects {
        let Object::Node(node) = object else { continue };
        let device = |default: Option<&str>| AudioDevice {
            id: *id,
            name: node.description.clone(),
            volume: node.volume,
            muted: node.muted,
            is_default: default == Some(node.name.as_str()),
        };
        if node.output {
            outputs.push(device(default_sink));
        }
        if node.input {
            inputs.push(device(default_source));
        }
    }
    AudioState {
        available: true,
        output: outputs.iter().find(|device| device.is_default).cloned(),
        input: inputs.iter().find(|device| device.is_default).cloned(),
        outputs,
        inputs,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const DUMP: &str = r#"[
      { "id": 0, "type": "PipeWire:Interface:Core", "info": { "props": { "core.name": "pipewire-0" } } },
      { "id": 34, "type": "PipeWire:Interface:Metadata", "props": { "metadata.name": "default" },
        "metadata": [
          { "subject": 0, "key": "default.configured.audio.sink", "type": "Spa:String:JSON", "value": { "name": "old" } },
          { "subject": 0, "key": "default.audio.sink", "type": "Spa:String:JSON", "value": { "name": "alsa_output.speakers" } },
          { "subject": 0, "key": "default.audio.source", "type": "Spa:String:JSON", "value": "{ \"name\": \"alsa_input.mic\" }" }
        ] },
      { "id": 35, "type": "PipeWire:Interface:Metadata", "props": { "metadata.name": "settings" }, "metadata": [] },
      { "id": 50, "type": "PipeWire:Interface:Node",
        "info": { "props": { "media.class": "Audio/Sink", "node.name": "alsa_output.speakers", "node.description": "Speakers" },
                  "params": { "Props": [ { "volume": 1.0, "mute": false, "channelVolumes": [ 0.125, 0.064 ] }, { "params": [] } ] } } },
      { "id": 51, "type": "PipeWire:Interface:Node",
        "info": { "props": { "media.class": "Audio/Sink", "node.name": "hdmi", "node.nick": "HDMI" },
                  "params": { "Props": [ { "volume": 0.027, "mute": true } ] } } },
      { "id": 52, "type": "PipeWire:Interface:Node",
        "info": { "props": { "media.class": "Audio/Source", "node.name": "alsa_input.mic", "node.description": "Microphone" },
                  "params": { "Props": [ { "mute": false, "channelVolumes": [ 1.0 ] } ] } } },
      { "id": 60, "type": "PipeWire:Interface:Node",
        "info": { "props": { "media.class": "Stream/Output/Audio", "node.name": "a player" }, "params": {} } },
      { "id": 61, "type": "PipeWire:Interface:Node", "info": { "props": { "media.class": "Video/Source", "node.name": "camera" } } }
    ]"#;

    fn objects_of(text: &str) -> BTreeMap<u32, Object> {
        let mut objects = BTreeMap::new();
        read_monitor(text.as_bytes(), |changes| {
            for change in changes {
                match change {
                    Change::Set(id, object) => {
                        objects.insert(id, object);
                    }
                    Change::Drop(id) => {
                        objects.remove(&id);
                    }
                }
            }
            true
        });
        objects
    }

    #[test]
    fn reads_devices_defaults_and_cubic_volumes() {
        let state = describe(&objects_of(DUMP));
        assert!(state.available);
        assert_eq!(state.outputs.len(), 2);
        let speakers = &state.outputs[0];
        assert_eq!((speakers.id, speakers.name.as_str(), speakers.muted, speakers.is_default), (50, "Speakers", false, true));
        // The loudest channel, on the cubic scale: cbrt(0.125) = 0.5.
        assert!((speakers.volume - 0.5).abs() < 1e-6);
        let hdmi = &state.outputs[1];
        assert_eq!((hdmi.id, hdmi.name.as_str(), hdmi.muted, hdmi.is_default), (51, "HDMI", true, false));
        assert!((hdmi.volume - 0.3).abs() < 1e-3);
        assert_eq!(state.output.as_ref(), Some(speakers));
        assert_eq!(state.inputs.len(), 1);
        assert_eq!(state.input.as_ref().map(|device| device.name.as_str()), Some("Microphone"));
        assert_eq!(state.input.as_ref().map(|device| device.volume), Some(1.0));
    }

    #[test]
    fn follows_the_documents_of_the_monitor() {
        let stream = format!(
            "{DUMP}\n{}\n{}\n",
            // A volume change of the speakers; the HDMI sink disappears.
            r#"[ { "id": 50, "type": "PipeWire:Interface:Node",
                  "info": { "props": { "media.class": "Audio/Sink", "node.name": "alsa_output.speakers", "node.description": "Speakers" },
                            "params": { "Props": [ { "mute": true, "channelVolumes": [ 1.0, 1.0 ] } ] } } },
                { "id": 51, "info": null } ]"#,
            // The default moves to a new sink; an id is reused by a stream.
            r#"[ { "id": 70, "type": "PipeWire:Interface:Node",
                  "info": { "props": { "media.class": "Audio/Sink", "node.name": "usb", "node.description": "USB headset" },
                            "params": { "Props": [ { "channelVolumes": [ 3.375 ] } ] } } },
                { "id": 34, "type": "PipeWire:Interface:Metadata", "props": { "metadata.name": "default" },
                  "metadata": [ { "subject": 0, "key": "default.audio.sink", "value": { "name": "usb" } } ] },
                { "id": 52, "type": "PipeWire:Interface:Node", "info": { "props": { "media.class": "Stream/Input/Audio" } } } ]"#
        );
        let state = describe(&objects_of(&stream));
        let names: Vec<&str> = state.outputs.iter().map(|device| device.name.as_str()).collect();
        assert_eq!(names, ["Speakers", "USB headset"]);
        assert!(state.outputs[0].muted && !state.outputs[0].is_default);
        assert_eq!(state.output.as_ref().map(|device| device.id), Some(70));
        // Amplified, and capped at 1.5.
        assert_eq!(state.outputs[1].volume, 1.5);
        assert!(state.inputs.is_empty());
        assert_eq!(state.input, None);
    }

    #[test]
    fn nonsense_is_survived() {
        // Not JSON at all, cut-off JSON, the wrong shapes.
        for text in ["", "pw-dump: no such daemon", "[ { \"id\": 5, ", "{ \"id\": 1 }", "[ 1, \"two\", null, [], {} ]", "42"] {
            assert!(objects_of(text).is_empty(), "{text}");
        }
        let odd = r#"[
          { "id": -4, "type": "PipeWire:Interface:Node" },
          { "id": 99999999999, "type": "PipeWire:Interface:Node" },
          { "id": 7, "type": "PipeWire:Interface:Node", "info": { "props": { "media.class": 5 } } },
          { "id": 8, "type": "PipeWire:Interface:Node", "info": { "props": { "media.class": "Audio/Sink", "node.name": 12, "node.description": "a\u001b[2Jb" },
                     "params": { "Props": [ { "channelVolumes": [ "loud", -1.0, 1e308 ], "mute": "yes" } ] } } },
          { "id": 9, "type": "PipeWire:Interface:Node", "info": { "props": { "media.class": "Audio/Sink" }, "params": { "Props": "none" } } },
          { "id": 10, "type": "PipeWire:Interface:Metadata", "props": { "metadata.name": "default" }, "metadata": [ { "key": "default.audio.sink", "value": 3 }, 7 ] }
        ]"#;
        let state = describe(&objects_of(odd));
        assert_eq!(state.outputs.len(), 2);
        assert_eq!(state.outputs[0].name, "a[2Jb");
        assert_eq!(state.outputs[0].volume, 1.5);
        assert!(!state.outputs[0].muted);
        assert_eq!(state.outputs[1].volume, 1.0);
        assert_eq!(state.output, None);
    }

    #[test]
    fn output_without_end_is_cut_off() {
        // An endless string inside a document: the budget ends it.
        struct Endless(bool);
        impl Read for Endless {
            fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
                if !self.0 {
                    self.0 = true;
                    buffer[..2].copy_from_slice(b"[\"");
                    return Ok(2);
                }
                buffer.fill(b'a');
                Ok(buffer.len())
            }
        }
        let mut documents = 0;
        read_monitor(Endless(false), |_| {
            documents += 1;
            true
        });
        assert_eq!(documents, 0);
    }

    #[test]
    fn volume_scale() {
        assert_eq!(shown_volume(0.0), 0.0);
        assert_eq!(shown_volume(-1.0), 0.0);
        assert_eq!(shown_volume(f64::NAN), 0.0);
        assert_eq!(shown_volume(1.0), 1.0);
        assert!((shown_volume(0.343) - 0.7).abs() < 1e-6);
        assert_eq!(shown_volume(1000.0), 1.5);
    }

    #[test]
    fn later_settings_replace_earlier_ones_of_the_same_kind() {
        let volume = |level| Setting::Volume { input: false, level };
        assert!(volume(0.1).same_kind(&volume(0.9)));
        assert!(!volume(0.1).same_kind(&Setting::Volume { input: true, level: 0.1 }));
        assert!(!volume(0.1).same_kind(&Setting::Mute { input: false, muted: true }));
        assert!(Setting::Mute { input: true, muted: true }.same_kind(&Setting::Mute { input: true, muted: false }));
        assert!(!Setting::Default { id: 1 }.same_kind(&Setting::Default { id: 2 }));
    }
}
