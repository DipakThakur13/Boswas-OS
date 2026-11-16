//! The files that put the boot screen into a system: systemd units, the
//! initramfs-tools hook and scripts, and the rules that install them. These
//! are read as text; what they do on a booting machine is checked by booting
//! one.

use std::path::{Path, PathBuf};

use boswas_bootsplash::daemon::{parse_args, Options, Stage};

fn source(path: &str) -> String {
    let full: PathBuf = Path::new(env!("CARGO_MANIFEST_DIR")).join(path);
    std::fs::read_to_string(&full).unwrap_or_else(|e| panic!("{}: {e}", full.display()))
}

/// The lines of a unit file that set `key` (comments left out).
fn settings<'a>(unit: &'a str, key: &str) -> Vec<&'a str> {
    unit.lines()
        .filter(|line| !line.starts_with('#'))
        .filter_map(|line| line.strip_prefix(key)?.strip_prefix('='))
        .collect()
}

/// The daemon's options in a unit's `ExecStart=`.
fn daemon_options(unit: &str) -> Options {
    let exec = settings(unit, "ExecStart");
    assert_eq!(exec.len(), 1, "{exec:?}");
    let mut words = exec[0].split_whitespace();
    assert_eq!(words.next(), Some("-/usr/libexec/boswas/boswas-bootsplashd"));
    let args: Vec<String> = words.map(str::to_owned).collect();
    parse_args(&args).expect("the unit's command line is one the daemon accepts")
}

#[test]
fn the_start_unit_waits_for_a_graphics_device_but_not_for_long() {
    let unit = source("systemd/boswas-bootsplash-start.service");
    let opts = daemon_options(&unit);
    assert_eq!(opts.stage, Stage::Launcher);
    // Long enough for a driver that is still loading, short enough for a
    // machine without any display, and inside the unit's own time limit.
    assert!((5_000..=10_000).contains(&opts.wait_ms), "{}", opts.wait_ms);
    let limit: u64 = settings(&unit, "TimeoutStartSec")[0].parse().unwrap();
    assert!(limit * 1000 >= opts.wait_ms + 10_000, "the launcher's own limit fits in TimeoutStartSec");
    // Ordered after the device manager's work where that is being waited
    // for anyway, without asking for it.
    let after = settings(&unit, "After").join(" ");
    assert!(after.contains("systemd-udev-trigger.service") && after.contains("systemd-udev-settle.service"));
    for dependency in ["Wants", "Requires"] {
        assert!(!settings(&unit, dependency).join(" ").contains("settle"), "{dependency}");
    }
}

#[test]
fn the_shutdown_units_start_the_daemon_with_options_it_accepts() {
    for (unit, mode) in [("poweroff", "shutdown"), ("halt", "shutdown"), ("reboot", "reboot")] {
        let opts = daemon_options(&source(&format!("systemd/boswas-bootsplash-{unit}.service")));
        assert_eq!(Some(opts.mode), boswas_bootsplash::scene::Mode::parse(mode), "{unit}");
    }
}

#[test]
fn the_hand_over_does_not_depend_on_a_file() {
    // A condition on the pid file would skip the hand-over whenever that
    // file is missing while a boot screen still holds the display.
    let unit = source("systemd/boswas-bootsplash-quit.service");
    assert!(settings(&unit, "ConditionPathExists").is_empty());
    assert_eq!(settings(&unit, "ExecStart"), ["-/usr/bin/boswas-bootsplash quit --retain"]);
    let before = settings(&unit, "Before").join(" ");
    for next in ["greetd.service", "display-manager.service", "getty@tty1.service"] {
        assert!(before.contains(next), "{next}");
    }
}

#[test]
fn emergency_and_rescue_mode_end_the_boot_screen_first() {
    let drop_in = source("systemd/quit-before-shell.conf");
    assert_eq!(settings(&drop_in, "ExecStartPre"), ["-/usr/bin/boswas-bootsplash quit"]);
    let rules = source("debian/rules");
    for unit in ["emergency.service", "rescue.service"] {
        assert!(
            rules.contains(&format!("$(UNITS)/{unit}.d/50-boswas-bootsplash.conf")),
            "debian/rules installs the drop-in for {unit}"
        );
    }
}

#[test]
fn the_initramfs_hook_brings_the_graphics_drivers_itself() {
    let hook = source("initramfs/hook");
    // Not left to FRAMEBUFFER=y, which only Ubuntu's initramfs-tools knows.
    assert!(hook.contains("copy_modules_dir kernel/drivers/gpu/drm"));
    for firmware_frame_buffer in ["simpledrm", "efifb", "vesafb", "simplefb"] {
        assert!(hook.contains(firmware_frame_buffer), "{firmware_frame_buffer}");
    }
    // Nothing of the package is switched off by an option that a Debian
    // system never sets.
    for script in ["initramfs/hook", "initramfs/init-premount", "initramfs/init-bottom", "initramfs/panic"] {
        let text = source(script);
        assert!(!text.lines().any(|line| line.starts_with("OPTION=")), "{script}");
        assert!(text.starts_with("#!/bin/sh\n"), "{script}");
    }
    let rules = source("debian/rules");
    for installed in
        ["hooks/boswas-bootsplash", "scripts/init-premount/boswas-bootsplash", "scripts/panic/boswas-bootsplash"]
    {
        assert!(rules.contains(installed), "{installed}");
    }
}
