//! Render the boot screen's states to PNG files, for review.
//!
//!     cargo run --release --example frames -- OUTDIR [WIDTHxHEIGHT ...]
//!
//! For each size (default 1920x1080 and 1366x768) it writes
//!
//!     boot, boot-message          the mark and the ring, without and with a message
//!     prompt-empty, prompt        the unlock screen, empty and with nine characters typed
//!     prompt-wrong, prompt-shake  after a rejected passphrase, with Caps Lock on: at rest,
//!                                 and a tenth of a second into the shake of the box
//!     shutdown, reboot            the ring and its line
//!     spinner-1, -2, -3           three consecutive animation frames (30 a second)
//!
//! Every frame is drawn on the same scene, one after the other, so the files
//! also show that a frame leaves nothing of the one before it. Nothing here
//! runs at boot.

use std::path::Path;
use std::time::Duration;

use boswas_bootsplash::png;
use boswas_bootsplash::scene::{self, Mode, PromptView, Scene, Tone, View};

const PROMPT: &str = "Please unlock disk nvme0n1p3_crypt";

fn save(scene: &Scene, dir: &Path, name: &str) {
    let path = dir.join(name);
    std::fs::write(&path, png::encode(scene.canvas())).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
    println!("{}", path.display());
}

fn main() {
    let mut args = std::env::args().skip(1);
    let dir = args.next().expect("usage: frames OUTDIR [WIDTHxHEIGHT ...]");
    let dir = Path::new(&dir);
    std::fs::create_dir_all(dir).expect("create the output directory");
    let mut sizes: Vec<(u32, u32)> = args
        .map(|s| {
            let (w, h) = s.split_once('x').expect("size is WIDTHxHEIGHT");
            (w.parse().expect("width"), h.parse().expect("height"))
        })
        .collect();
    if sizes.is_empty() {
        sizes = vec![(1920, 1080), (1366, 768)];
    }
    // The ring 1.4 seconds into its cycle: the first dots leave the bunch
    // over the top and sweep round.
    let sweep = 1.4 / scene::SPINNER_SECS;
    let frame = 1.0 / (30.0 * scene::SPINNER_SECS);
    for (w, h) in sizes {
        let tag = format!("{w}x{h}");
        let mut scene = Scene::new(w, h);

        let mut view = View::new(Mode::Boot);
        scene.render(&view);
        save(&scene, dir, &format!("{tag}-boot.png"));

        view.prompt = Some(PromptView::new(PROMPT));
        scene.render(&view);
        save(&scene, dir, &format!("{tag}-prompt-empty.png"));

        view.prompt = Some(PromptView { dots: 9, ..PromptView::new(PROMPT) });
        scene.render(&view);
        save(&scene, dir, &format!("{tag}-prompt.png"));

        let mut wrong = PromptView { dots: 3, caps_lock: true, ..PromptView::new(PROMPT) };
        view.prompt = Some(wrong);
        view.message =
            Some(("cryptsetup: ERROR: nvme0n1p3_crypt: cryptsetup failed, bad password or options?", Tone::Error));
        scene.render(&view);
        save(&scene, dir, &format!("{tag}-prompt-wrong.png"));

        wrong.shake = scene::shake_offset(Duration::from_millis(100));
        view.prompt = Some(wrong);
        scene.render(&view);
        save(&scene, dir, &format!("{tag}-prompt-shake.png"));

        let mut view = View::new(Mode::Boot);
        view.phase = sweep;
        view.message = Some(("cryptsetup: nvme0n1p3_crypt: set up successfully", Tone::Info));
        scene.render(&view);
        save(&scene, dir, &format!("{tag}-boot-message.png"));

        let mut view = View::new(Mode::Shutdown);
        view.phase = 0.52;
        scene.render(&view);
        save(&scene, dir, &format!("{tag}-shutdown.png"));

        let mut view = View::new(Mode::Reboot);
        view.phase = 0.13;
        scene.render(&view);
        save(&scene, dir, &format!("{tag}-reboot.png"));

        let mut view = View::new(Mode::Boot);
        for n in 0..3 {
            view.phase = sweep + frame * n as f32;
            scene.render(&view);
            save(&scene, dir, &format!("{tag}-spinner-{}.png", n + 1));
        }
    }
}
