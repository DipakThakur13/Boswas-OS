//! Runs the bridge without any bus: every service reports itself missing,
//! bad lines are answered with an error, and the program ends with its
//! input.

use std::{
    io::{BufRead, BufReader, Write},
    process::{Command, Stdio},
    sync::mpsc,
    time::Duration,
};

use serde_json::Value;

#[test]
fn the_bridge_reports_states_answers_bad_lines_and_ends_with_its_input() {
    let empty = tempfile::tempdir().unwrap();
    let nowhere = format!("unix:path={}", empty.path().join("no-bus").display());
    let mut child = Command::new(env!("CARGO_BIN_EXE_boswas-services-bridge"))
        .env("DBUS_SESSION_BUS_ADDRESS", &nowhere)
        .env("DBUS_SYSTEM_BUS_ADDRESS", &nowhere)
        .env("XDG_RUNTIME_DIR", empty.path())
        .env("PATH", empty.path())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let mut stdin = child.stdin.take().unwrap();
    let stdout = child.stdout.take().unwrap();
    let (sender, lines) = mpsc::channel::<Value>();
    std::thread::spawn(move || {
        for line in BufReader::new(stdout).lines().map_while(Result::ok) {
            let value: Value = serde_json::from_str(&line).expect("every line is one JSON object");
            if sender.send(value).is_err() {
                break;
            }
        }
    });
    let next = |what: &str| lines.recv_timeout(Duration::from_secs(20)).unwrap_or_else(|_| panic!("no line: {what}"));

    let ready = next("ready");
    assert_eq!(ready["event"], "ready");
    assert_eq!(ready["version"], env!("CARGO_PKG_VERSION"));

    // Without NetworkManager, BlueZ and a sound server each service says so.
    let mut seen = std::collections::BTreeMap::new();
    while seen.len() < 3 {
        let line = next("the first states");
        let name = line["event"].as_str().unwrap().to_owned();
        if matches!(name.as_str(), "network" | "bluetooth" | "audio") {
            seen.insert(name, line);
        }
    }
    for (name, state) in &seen {
        assert_eq!(state["available"], false, "{name}");
    }

    stdin.write_all(b"this is not json\n").unwrap();
    stdin.write_all(b"{\"cmd\":\"power\",\"action\":\"power-off\"}\n").unwrap();
    stdin.flush().unwrap();
    let mut errors = Vec::new();
    while errors.len() < 2 {
        let line = next("an error");
        if line["event"] == "error" {
            errors.push(line["message"].as_str().unwrap().to_owned());
        }
    }
    assert_eq!(errors, ["not a JSON object", "unknown \"cmd\""]);

    // A valid command for a missing service is accepted quietly.
    stdin.write_all(b"{\"cmd\":\"wifi-scan\"}\n\n").unwrap();
    let long = format!("{{\"cmd\":\"wifi-scan\",\"pad\":\"{}\"}}\n", "x".repeat(64 * 1024));
    stdin.write_all(long.as_bytes()).unwrap();
    stdin.flush().unwrap();
    loop {
        let line = next("the over-long line's error");
        if line["event"] == "error" {
            assert_eq!(line["message"], "the line is too long");
            break;
        }
    }

    drop(stdin);
    let (done, finished) = mpsc::channel();
    std::thread::spawn(move || {
        let _ = done.send(child.wait());
    });
    let status = finished.recv_timeout(Duration::from_secs(20)).expect("the bridge ends when its input closes").unwrap();
    assert!(status.success());
}
