//! Render the boot screen to PNG files, for review: its states, its motion
//! as frames, a contact sheet of the motion, and an animated preview.
//!
//!     cargo run --release --example frames -- OUTDIR [WIDTHxHEIGHT ...]
//!
//! For each size (default 1920x1080 and 1366x768) it writes, as
//! `OUTDIR/WIDTHxHEIGHT-NAME.png`:
//!
//!     boot                    after the intro: the mark, the ring turning
//!     boot-static             `boswas.splash=static`: the assembled mark, a still ring
//!     intro-T                 the intro T seconds in (0.00 to 1.60)
//!     ring-1 ... ring-6       the ring over one cycle, a sixth apart
//!     ring-next-1 ... -3      three consecutive frames of the ring at 60 Hz
//!     boot-message            a message under the ring
//!     boot-progress           a reported progress: the ring shows 40 %
//!     glide-1 ... glide-3     the glide to the unlock screen, a quarter, half
//!                             and three quarters of the way
//!     prompt-empty, prompt    the unlock screen, empty and with nine characters
//!     prompt-wrong            after a rejected passphrase, with Caps Lock on
//!     prompt-shake            a tenth of a second into the shake of the box
//!     shutdown-fade-in        the shutdown screen a tenth of a second in
//!     shutdown, reboot        the mark, the ring and the status line
//!     fade-1, fade-2          `quit`: a third and two thirds into the fade
//!     fade-end                its last frame: the background
//!     retain                  `quit --retain`: the last frame left behind
//!     contact                 a contact sheet of the motion, frame by frame
//!
//! and `OUTDIR/WIDTHxHEIGHT-motion.gif`, an animated preview of a scripted
//! boot at 25 frames a second (intro, ring, a passphrase typed, rejected
//! and typed again, the hand-over), the middle of the screen scaled to at
//! most 720 pixels across. The frames are joined with `ffmpeg`; without it
//! they are left in `OUTDIR/WIDTHxHEIGHT-motion/NNNN.png`.
//!
//! The states are drawn on one scene, one after the other, so the files
//! also show that a frame leaves nothing of the one before it. Nothing here
//! runs at boot.

use std::path::Path;
use std::process::Command;
use std::time::Duration;

use boswas_bootsplash::assets::{self, Weight};
use boswas_bootsplash::gfx::{Canvas, Rect, Rgb};
use boswas_bootsplash::motion::{self, Choreography, Opening};
use boswas_bootsplash::png;
use boswas_bootsplash::scene::{self, Mode, PromptView, Scene, Tone, View};
use boswas_bootsplash::text::Font;

const PROMPT: &str = "Please unlock disk nvme0n1p3_crypt";
const REJECTED: &str = "cryptsetup: ERROR: nvme0n1p3_crypt: cryptsetup failed, bad password or options?";
/// Frames a second of the motion sequence.
const FPS: f64 = 25.0;

fn save(canvas: &Canvas, dir: &Path, name: &str) {
    let path = dir.join(name);
    std::fs::write(&path, png::encode(canvas)).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
    println!("{}", path.display());
}

/// The screen at `t` seconds of a boot whose choreography is `c`.
fn at<'a>(c: &Choreography, t: f64, mode: Mode) -> View<'a> {
    View { phase: c.phase(t), motion: c.motion(t), ..View::new(mode) }
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
        stills(dir, w, h);
        sheet(dir, w, h);
        sequence(dir, w, h);
    }
}

fn stills(dir: &Path, w: u32, h: u32) {
    let tag = format!("{w}x{h}");
    let mut scene = Scene::new(w, h);
    let boot = Choreography::new(Opening::Intro, true, 0.0);
    let settled = 9.0 + 0.3 * motion::RING_SECS as f64; // after the intro, the arc growing

    let view = at(&boot, settled, Mode::Boot);
    scene.render(&view);
    save(scene.canvas(), dir, &format!("{tag}-boot.png"));

    scene.render(&View::new(Mode::Boot));
    save(scene.canvas(), dir, &format!("{tag}-boot-static.png"));

    for t in [0.0, 0.15, 0.3, 0.45, 0.6, 0.75, 0.9, 1.0, 1.1, 1.25, 1.4, 1.6] {
        scene.render(&at(&boot, t, Mode::Boot));
        save(scene.canvas(), dir, &format!("{tag}-intro-{t:.2}.png"));
    }
    for n in 0..6 {
        let mut view = at(&boot, settled, Mode::Boot);
        view.phase = n as f32 / 6.0;
        scene.render(&view);
        save(scene.canvas(), dir, &format!("{tag}-ring-{}.png", n + 1));
    }
    for n in 0..3 {
        scene.render(&at(&boot, settled + n as f64 / 60.0, Mode::Boot));
        save(scene.canvas(), dir, &format!("{tag}-ring-next-{}.png", n + 1));
    }

    let mut view = at(&boot, settled, Mode::Boot);
    view.message = Some(("cryptsetup: nvme0n1p3_crypt: set up successfully", Tone::Info));
    scene.render(&view);
    save(scene.canvas(), dir, &format!("{tag}-boot-message.png"));

    let mut view = View::new(Mode::Boot);
    view.progress = Some(40);
    scene.render(&view);
    save(scene.canvas(), dir, &format!("{tag}-boot-progress.png"));

    let mut c = Choreography::new(Opening::Intro, true, 0.0);
    c.prompt(true, 10.0);
    for (n, part) in [0.25, 0.5, 0.75].into_iter().enumerate() {
        let mut view = at(&c, 10.0 + part * motion::GLIDE as f64, Mode::Boot);
        view.prompt = Some(PromptView::new(PROMPT));
        scene.render(&view);
        save(scene.canvas(), dir, &format!("{tag}-glide-{}.png", n + 1));
    }

    let mut view = View::new(Mode::Boot).with_prompt(PromptView::new(PROMPT));
    scene.render(&view);
    save(scene.canvas(), dir, &format!("{tag}-prompt-empty.png"));

    view.prompt = Some(PromptView { dots: 9, ..PromptView::new(PROMPT) });
    scene.render(&view);
    save(scene.canvas(), dir, &format!("{tag}-prompt.png"));

    let mut wrong = PromptView { dots: 3, caps_lock: true, ..PromptView::new(PROMPT) };
    view.prompt = Some(wrong);
    view.message = Some((REJECTED, Tone::Error));
    scene.render(&view);
    save(scene.canvas(), dir, &format!("{tag}-prompt-wrong.png"));

    wrong.shake = scene::shake_offset(Duration::from_millis(100));
    view.prompt = Some(wrong);
    scene.render(&view);
    save(scene.canvas(), dir, &format!("{tag}-prompt-shake.png"));

    let down = Choreography::new(Opening::FadeIn, true, 0.0);
    scene.render(&at(&down, 0.1, Mode::Shutdown));
    save(scene.canvas(), dir, &format!("{tag}-shutdown-fade-in.png"));
    scene.render(&at(&down, 3.0, Mode::Shutdown));
    save(scene.canvas(), dir, &format!("{tag}-shutdown.png"));
    let mut view = at(&down, 3.6, Mode::Reboot);
    view.phase = 0.13;
    scene.render(&view);
    save(scene.canvas(), dir, &format!("{tag}-reboot.png"));

    let mut quit = Choreography::new(Opening::Intro, true, 0.0);
    quit.leave(false, 20.0);
    for (name, t) in [("fade-1", 1.0 / 3.0), ("fade-2", 2.0 / 3.0), ("fade-end", 1.0)] {
        let mut view = at(&quit, 20.0 + t * motion::OUTRO as f64, Mode::Boot);
        view.message = Some(("Starting emergency shell", Tone::Info));
        scene.render(&view);
        save(scene.canvas(), dir, &format!("{tag}-{name}.png"));
    }
    let mut retain = Choreography::new(Opening::Intro, true, 0.0);
    retain.leave(true, 20.0);
    scene.render(&at(&retain, 21.0, Mode::Boot));
    save(scene.canvas(), dir, &format!("{tag}-retain.png"));
}

/// The part of the screen the motion happens in: the mark, the ring and
/// the unlock column, with some background round them.
fn crop(w: u32, h: u32) -> Rect {
    let l = scene::Layout::new(w, h);
    let all = l.mark.union(&l.prompt_mark).union(&l.prompt_second_line).union(&l.busy_message).union(&l.field);
    let margin = (l.mark.h as f32 * 0.5) as i32;
    let (cw, ch) = (((all.w + margin * 2) / 2 * 2).min(w as i32), ((all.h + margin * 2) / 2 * 2).min(h as i32));
    let x = (all.x + all.w / 2 - cw / 2).clamp(0, w as i32 - cw);
    let y = (all.y + all.h / 2 - ch / 2).clamp(0, h as i32 - ch);
    Rect::new(x, y, cw, ch)
}

/// A contact sheet: the intro, the ring and the glide, frame by frame,
/// scaled down, each with its time.
fn sheet(dir: &Path, w: u32, h: u32) {
    let area = crop(w, h);
    let cell_w = 320u32;
    let cell_h = (cell_w as f32 * area.h as f32 / area.w as f32).round() as u32;
    let label = 22u32;
    let mut frames: Vec<(String, Canvas)> = Vec::new();
    let mut scene = Scene::new(w, h);
    let boot = Choreography::new(Opening::Intro, true, 0.0);
    let mut t = 0.0;
    while t <= 1.65 {
        scene.render(&at(&boot, t, Mode::Boot));
        frames.push((format!("intro {t:.2} s"), copy(scene.canvas(), area)));
        t += 0.1;
    }
    for n in 0..8 {
        let t = 9.0 + n as f64 * motion::RING_SECS as f64 / 8.0;
        scene.render(&at(&boot, t, Mode::Boot));
        frames.push((format!("ring {:.2} s", t - 9.0), copy(scene.canvas(), area)));
    }
    let mut c = Choreography::new(Opening::Intro, true, 0.0);
    c.prompt(true, 10.0);
    for n in 0..=4 {
        let t = 10.0 + n as f64 * motion::GLIDE as f64 / 4.0;
        let mut view = at(&c, t, Mode::Boot);
        view.prompt = Some(PromptView::new(PROMPT));
        scene.render(&view);
        frames.push((format!("glide {:.2} s", t - 10.0), copy(scene.canvas(), area)));
    }
    let columns = 6u32;
    let rows = (frames.len() as u32).div_ceil(columns);
    let gap = 8u32;
    let mut out = Canvas::new(columns * (cell_w + gap) + gap, rows * (cell_h + label + gap) + gap);
    let mut font = Font::new();
    for (i, (name, frame)) in frames.iter().enumerate() {
        let (col, row) = (i as u32 % columns, i as u32 / columns);
        let (x, y) = (gap + col * (cell_w + gap), gap + row * (cell_h + label + gap));
        shrink(frame, &mut out, Rect::new(x as i32, y as i32, cell_w as i32, cell_h as i32));
        let bounds = out.bounds();
        let px = 14.0;
        font.draw(
            &mut out,
            &bounds,
            x as f32,
            (y + cell_h + 16) as f32,
            name,
            Weight::Regular,
            px,
            assets::TEXT_MUTED,
            1.0,
        );
    }
    save(&out, dir, &format!("{w}x{h}-contact.png"));
}

fn copy(canvas: &Canvas, area: Rect) -> Canvas {
    let mut out = Canvas::new(area.w as u32, area.h as u32);
    for y in 0..area.h {
        let row = canvas.row((area.y + y) as u32, area.x as u32, area.w as u32);
        out.row_mut(y as u32, 0, area.w as u32).copy_from_slice(row);
    }
    out
}

/// Scale `from` down into `into` at `to` by averaging the pixels each
/// destination pixel covers.
fn shrink(from: &Canvas, into: &mut Canvas, to: Rect) {
    let (sw, sh) = (from.width() as f32 / to.w as f32, from.height() as f32 / to.h as f32);
    for y in 0..to.h {
        for x in 0..to.w {
            let (x0, y0) = ((x as f32 * sw) as u32, (y as f32 * sh) as u32);
            let (x1, y1) = ((((x + 1) as f32 * sw) as u32).max(x0 + 1), (((y + 1) as f32 * sh) as u32).max(y0 + 1));
            let mut sum = [0u32; 3];
            let mut n = 0;
            for sy in y0..y1.min(from.height()) {
                for &px in from.row(sy, x0, x1 - x0) {
                    let c = Rgb::from_xrgb(px);
                    sum[0] += c.r as u32;
                    sum[1] += c.g as u32;
                    sum[2] += c.b as u32;
                    n += 1;
                }
            }
            let n = n.max(1);
            let c = Rgb::new((sum[0] / n) as u8, (sum[1] / n) as u8, (sum[2] / n) as u8);
            into.fill_rect(&Rect::new(to.x + x, to.y + y, 1, 1), c);
        }
    }
}

/// A scripted boot, as the daemon would show it, at `FPS` frames a second.
fn sequence(dir: &Path, w: u32, h: u32) {
    let tag = format!("{w}x{h}");
    let frames = dir.join(format!("{tag}-motion"));
    let _ = std::fs::remove_dir_all(&frames);
    std::fs::create_dir_all(&frames).expect("create the motion directory");
    let area = crop(w, h);
    let width = area.w.min(720);
    let size = Rect::new(0, 0, width, (width as f32 * area.h as f32 / area.w as f32).round() as i32);
    let mut small = Canvas::new(size.w as u32, size.h as u32);
    let mut scene = Scene::new(w, h);
    let mut c = Choreography::new(Opening::Intro, true, 0.0);
    // (time, what happens)
    #[derive(Clone, Copy)]
    enum Event {
        Ask,
        Key,
        Enter,
        Reject,
        Leave,
    }
    let mut script: Vec<(f64, Event)> = vec![(3.2, Event::Ask)];
    script.extend((0..7).map(|i| (3.9 + i as f64 * 0.13, Event::Key)));
    script.push((5.0, Event::Enter));
    script.push((5.9, Event::Reject));
    script.push((5.9, Event::Ask));
    script.extend((0..9).map(|i| (6.9 + i as f64 * 0.12, Event::Key)));
    script.push((8.2, Event::Enter));
    script.push((9.8, Event::Leave));
    let end = 10.6;

    let (mut asking, mut dots, mut fading, mut since_key, mut shake_from, mut message) =
        (false, 0usize, None::<usize>, 0.0f64, None::<f64>, None::<&str>);
    let mut next = 0;
    let mut n = 0;
    loop {
        let t = n as f64 / FPS;
        if t > end {
            break;
        }
        while next < script.len() && script[next].0 <= t {
            let when = script[next].0;
            match script[next].1 {
                Event::Ask => {
                    asking = true;
                    dots = 0;
                    since_key = when;
                    c.prompt(true, when);
                    if message.is_some() {
                        shake_from = Some(when);
                    }
                }
                Event::Key => {
                    dots += 1;
                    since_key = when;
                }
                Event::Enter => {
                    asking = false;
                    fading = Some(dots);
                    message = None;
                    c.prompt(false, when);
                }
                Event::Reject => message = Some(REJECTED),
                Event::Leave => c.leave(true, when),
            }
            next += 1;
        }
        let shake = shake_from.map_or(0.0, |s| scene::shake_offset(Duration::from_secs_f64((t - s).max(0.0))));
        let caret = scene::caret_visible(Duration::from_secs_f64(t - since_key));
        let motion = c.motion(t);
        if !asking && motion.reveal <= 0.0 {
            fading = None;
        }
        let prompt = if asking {
            Some(PromptView { dots, caret, shake, ..PromptView::new(PROMPT) })
        } else {
            fading.map(|dots| PromptView { dots, caret: false, ..PromptView::new(PROMPT) })
        };
        let view = View {
            prompt,
            message: message.map(|m| (m, Tone::Error)),
            phase: c.phase(t),
            motion,
            ..View::new(Mode::Boot)
        };
        scene.render(&view);
        shrink(&copy(scene.canvas(), area), &mut small, size);
        std::fs::write(frames.join(format!("{n:04}.png")), png::encode(&small)).expect("write a frame");
        n += 1;
    }

    // Join the frames into an animated preview where ffmpeg is there.
    let gif = dir.join(format!("{tag}-motion.gif"));
    let filter = "split[a][b];[a]palettegen=max_colors=256:stats_mode=full[p];[b][p]paletteuse=dither=sierra2_4a";
    let made = Command::new("ffmpeg")
        .args(["-hide_banner", "-loglevel", "error", "-y", "-framerate", "25", "-i"])
        .arg(frames.join("%04d.png"))
        .args(["-vf", filter, "-loop", "0"])
        .arg(&gif)
        .status();
    match made {
        Ok(status) if status.success() => {
            let _ = std::fs::remove_dir_all(&frames);
            println!("{} ({n} frames)", gif.display());
        }
        _ => println!("{} ({n} frames; no animated preview: ffmpeg is not installed or failed)", frames.display()),
    }
}
