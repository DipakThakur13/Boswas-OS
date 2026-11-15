//! Render the boot screen's states to PNG files, for review.
//!
//!     cargo run --release --example frames -- OUTDIR [WIDTHxHEIGHT ...]
//!
//! Writes boot, prompt, wrong-passphrase, shutdown and reboot frames for each
//! size (default 1920x1080 and 1366x768). Nothing here runs at boot.

use std::path::Path;

use boswas_bootsplash::png;
use boswas_bootsplash::scene::{Mode, PromptView, Scene, Tone, View};

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
    for (w, h) in sizes {
        let tag = format!("{w}x{h}");
        let mut scene = Scene::new(w, h);

        let mut view = View::new(Mode::Boot);
        view.phase = 0.18;
        scene.render(&view);
        save(&scene, dir, &format!("{tag}-boot.png"));

        view.prompt = Some(PromptView { text: "Please unlock disk nvme0n1p3_crypt".into(), dots: 0, caps_lock: false });
        scene.render(&view);
        save(&scene, dir, &format!("{tag}-prompt-empty.png"));

        view.prompt = Some(PromptView { text: "Please unlock disk nvme0n1p3_crypt".into(), dots: 9, caps_lock: false });
        scene.render(&view);
        save(&scene, dir, &format!("{tag}-prompt.png"));

        view.prompt = Some(PromptView { text: "Please unlock disk nvme0n1p3_crypt".into(), dots: 3, caps_lock: true });
        view.message = Some((
            "cryptsetup: ERROR: nvme0n1p3_crypt: cryptsetup failed, bad password or options?".into(),
            Tone::Error,
        ));
        scene.render(&view);
        save(&scene, dir, &format!("{tag}-prompt-wrong.png"));

        let mut view = View::new(Mode::Boot);
        view.phase = 0.62;
        view.progress = Some(40);
        view.message = Some(("cryptsetup: nvme0n1p3_crypt: set up successfully".into(), Tone::Info));
        scene.render(&view);
        save(&scene, dir, &format!("{tag}-boot-message.png"));

        let mut view = View::new(Mode::Shutdown);
        view.phase = 0.4;
        scene.render(&view);
        save(&scene, dir, &format!("{tag}-shutdown.png"));

        let mut view = View::new(Mode::Reboot);
        view.phase = 0.85;
        scene.render(&view);
        save(&scene, dir, &format!("{tag}-reboot.png"));
    }
}
