//! The boot screen against Debian's real disk-unlock helper.
//!
//! `/lib/cryptsetup/askpass` (debian/askpass.c in the cryptsetup source
//! package) is what the initramfs runs to get the passphrase of an encrypted
//! root disk. It tries, in this order: systemd, a FIFO, Plymouth, the
//! console. For Plymouth it runs `/bin/plymouth --ping` and, if that
//! succeeds, `/bin/plymouth ask-for-password --prompt PROMPT`, reading the
//! passphrase from that program's standard output until end of file; the
//! console is then not used at all (the method is marked `no_more`).
//!
//! The helper is not part of this repository and is used unmodified. The
//! test builds a scratch root directory laid out like the initramfs
//!
//! ```text
//! ROOT/lib/cryptsetup/askpass          the helper (tests/build-askpass.sh)
//! ROOT/bin/plymouth                    the boswas-bootsplash client
//! ROOT/bin/sh                          the shell askpass's system() needs
//! ROOT/run/boswas-bootsplash/socket    the test daemon's control socket
//! ```
//!
//! and runs the helper inside it with chroot(8), which needs root. Like the
//! initramfs, that root has no running systemd, so the helper goes on to the
//! FIFO and Plymouth methods. Without the helper binary, or without root,
//! the test is skipped with a message.

mod common;

use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::Duration;

use boswas_bootsplash::assets;
use common::*;

fn askpass_binary() -> Option<PathBuf> {
    let candidates = [
        std::env::var_os("BOSWAS_BOOTSPLASH_ASKPASS").map(PathBuf::from),
        Some(Path::new(env!("CARGO_TARGET_TMPDIR")).join("askpass").join("askpass")),
    ];
    candidates.into_iter().flatten().find(|path| path.is_file())
}

/// Copy a program into the scratch root together with the shared libraries
/// it loads (what the initramfs tools' `copy_exec` does).
fn install(root: &Path, program: &Path, target: &str) {
    let copy = |from: &Path, to: &Path| {
        std::fs::create_dir_all(to.parent().unwrap()).unwrap();
        if !to.exists() {
            std::fs::copy(from, to).unwrap_or_else(|e| panic!("copy {}: {e}", from.display()));
        }
    };
    copy(program, &root.join(target.trim_start_matches('/')));
    let ldd = Command::new("ldd").arg(program).output().expect("ldd");
    for line in String::from_utf8_lossy(&ldd.stdout).lines() {
        let library = line.split_whitespace().find(|word| word.starts_with('/'));
        if let Some(library) = library {
            copy(Path::new(library), &root.join(library.trim_start_matches('/')));
        }
    }
}

fn prompt_shown(frame: &Frame) -> bool {
    frame.count(&frame.layout().field, assets::FIELD_FILL) > 500
}

fn arc_shown(frame: &Frame) -> bool {
    frame.count(&frame.layout().arc, assets::TRACK) > 20
}

const PROMPT: &str = "Please unlock disk sda3_crypt: ";

#[test]
fn the_real_askpass_gets_the_passphrase_from_the_boot_screen() {
    let Some(askpass) = askpass_binary() else {
        eprintln!(
            "skipped: no askpass binary. Build it with tests/build-askpass.sh from the cryptsetup \
             source package's debian/askpass.c, or set BOSWAS_BOOTSPLASH_ASKPASS."
        );
        return;
    };
    if !rustix::process::geteuid().is_root() {
        eprintln!("skipped: running askpass in a scratch root needs root (chroot)");
        return;
    }

    // The scratch root.
    let dir = temp_dir("askpass");
    let root = dir.join("root");
    install(&root, &askpass, "/lib/cryptsetup/askpass");
    install(&root, Path::new(CLIENT), "/bin/plymouth");
    install(&root, &std::fs::canonicalize("/bin/sh").unwrap(), "/bin/sh");
    let fifo = root.join("lib/cryptsetup/passfifo");
    let socket = root.join("run/boswas-bootsplash/socket");
    let run = |stdin: Stdio| {
        Command::new("chroot")
            .arg(&root)
            .args(["/lib/cryptsetup/askpass", PROMPT])
            .stdin(stdin)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap()
    };
    let stderr_of = |child: &mut std::process::Child| {
        let mut text = String::new();
        std::io::Read::read_to_string(child.stderr.as_mut().unwrap(), &mut text).unwrap();
        text
    };

    // 1. The passphrase typed on the boot screen arrives on askpass's stdout.
    let mut splash = Splash::start_at(dir.clone(), socket, "1920x1080", &[]);
    let mut child = run(Stdio::null());
    let frame = splash.wait_for("the prompt", prompt_shown);
    assert!(frame.count(&frame.layout().prompt, assets::TEXT) > 200, "the unlock prompt is on screen");
    splash.pty.type_keys(b"boswas-disk-passphrase\r");
    let console = stderr_of(&mut child);
    let (code, stdout) = wait_child(child, Duration::from_secs(5));
    assert_eq!(code, Some(0));
    assert_eq!(stdout, b"boswas-disk-passphrase");
    assert!(console.is_empty(), "askpass did not also prompt on the console: {console:?}");
    splash.wait_for("the arc again", arc_shown);

    // 2. The FIFO stays usable next to the boot screen (cryptroot-unlock,
    //    remote unlocking): its passphrase wins, askpass kills the client,
    //    and the boot screen takes its prompt down.
    let mut child = run(Stdio::null());
    splash.wait_for("the prompt", prompt_shown);
    wait_until("the FIFO", Duration::from_secs(3), || exists(&fifo));
    std::fs::OpenOptions::new().write(true).open(&fifo).unwrap().write_all(b"from the fifo").unwrap();
    let _ = stderr_of(&mut child);
    let (code, stdout) = wait_child(child, Duration::from_secs(5));
    assert_eq!((code, stdout), (Some(0), b"from the fifo".to_vec()));
    splash.wait_for("the prompt to go away", arc_shown);

    // 3. The boot screen dies while the prompt is open: askpass returns at
    //    once (with an empty passphrase, which cryptsetup rejects, so the
    //    initramfs asks again)...
    let child = run(Stdio::null());
    splash.wait_for("the prompt", prompt_shown);
    splash.kill();
    let (code, stdout) = wait_child(child, Duration::from_secs(3));
    assert_eq!((code, stdout.len()), (Some(0), 0));

    // 4. ...and the next attempt, like any attempt without a boot screen,
    //    is asked on the console: the ping fails and askpass falls through.
    let mut console = open_pty();
    let slave = std::fs::OpenOptions::new().read(true).write(true).open(&console.path).unwrap();
    let mut child = run(Stdio::from(slave));
    std::thread::sleep(Duration::from_millis(300));
    console.type_keys(b"typed on the console\n");
    let prompt = stderr_of(&mut child);
    let (code, stdout) = wait_child(child, Duration::from_secs(5));
    assert_eq!((code, stdout), (Some(0), b"typed on the console".to_vec()));
    assert!(prompt.contains("Please unlock disk sda3_crypt"), "console prompt: {prompt:?}");
}
