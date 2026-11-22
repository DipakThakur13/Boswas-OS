//! How much work a frame is.
//!
//! The boot screen runs while the machine is busy starting up, so a frame
//! must cost next to nothing. While the system starts only the ring's
//! rectangle is redrawn (its arc on the background) and copied to the
//! display; during a prompt only the text box is. The ring turns at the
//! display's refresh rate, up to 240 frames a second. The intro, the glide
//! to the unlock screen and the fade at the end redraw the mark's
//! rectangle (and the prompt's) for a second and a half at most.
//!
//! The time limits here are deliberately loose (they must hold in an
//! unoptimised build on a slow build machine);
//! `cargo test --release --test perf -- --nocapture` prints the real figures.

use std::time::{Duration, Instant};

use boswas_bootsplash::daemon::MAX_FPS;
use boswas_bootsplash::gfx::{Canvas, Rect};
use boswas_bootsplash::motion::{self, Motion};
use boswas_bootsplash::scene::{self, Mode, PromptView, Scene, View};

fn median(mut samples: Vec<Duration>) -> Duration {
    samples.sort();
    samples[samples.len() / 2]
}

/// Copy the damage into an XRGB8888 buffer, as the display back ends do.
fn present(canvas: &Canvas, damage: &[Rect], target: &mut [u8]) {
    let stride = canvas.width() as usize * 4;
    for rect in damage {
        for y in rect.y..rect.bottom() {
            let row = canvas.row(y as u32, rect.x as u32, rect.w as u32);
            let start = y as usize * stride + rect.x as usize * 4;
            for (px, out) in row.iter().zip(target[start..start + row.len() * 4].chunks_exact_mut(4)) {
                out.copy_from_slice(&px.to_le_bytes());
            }
        }
    }
}

/// Render one frame and copy what changed; returns the time it took.
fn frame(scene: &mut Scene, view: &View, target: &mut [u8]) -> Duration {
    let started = Instant::now();
    scene.render(view);
    present(scene.canvas(), scene.damage(), target);
    started.elapsed()
}

fn pixels(damage: &[Rect]) -> i64 {
    damage.iter().map(Rect::area).sum()
}

#[test]
fn an_animation_frame_is_cheap() {
    const PROMPT: &str = "Please unlock disk sda3_crypt";
    let fps = MAX_FPS as f32;
    for (w, h) in [(1366u32, 768u32), (1920, 1080), (3840, 2160)] {
        let screen = w as f64 * h as f64;
        let mut target = vec![0u8; (w * h * 4) as usize];

        // The intro, at the most frames a second: the first frame (the
        // background and everything), then the mark's rectangle and, at the
        // end, the ring's.
        let started = Instant::now();
        let mut scene = Scene::new(w, h);
        let mut view = View { motion: motion::intro(0.0), ..View::new(Mode::Boot) };
        scene.render(&view);
        present(scene.canvas(), scene.damage(), &mut target);
        let first_frame = started.elapsed();
        let layout = scene.layout().clone();
        let mut intro = Duration::ZERO;
        let mut intro_frames = Vec::new();
        let mut intro_pixels = 0;
        for i in 1..=(motion::INTRO * fps) as u32 {
            view.motion = motion::intro(i as f32 / fps);
            view.phase = (i as f32 / fps - motion::RING_FADE.0).max(0.0) / motion::RING_SECS;
            let took = frame(&mut scene, &view, &mut target);
            intro += took;
            intro_frames.push(took);
            intro_pixels = intro_pixels.max(pixels(scene.damage()));
            let allowed = layout.mark.union(&layout.ring);
            assert!(scene.damage().iter().all(|d| allowed.contains(d)), "the intro redraws the mark and the ring");
        }
        let intro_share = intro_pixels as f64 * 100.0 / screen;

        // The ring at the most frames a second it is drawn, for more than a
        // whole cycle.
        view.motion = Motion::REST;
        scene.render(&view);
        let mut frames = Vec::new();
        let cycle = (fps * motion::RING_SECS) as u32;
        for i in 1..=cycle + 100 {
            view.phase = (i as f32 / (fps * motion::RING_SECS)).fract();
            frames.push(frame(&mut scene, &view, &mut target));
            assert_eq!(scene.damage(), [layout.ring], "an animation frame redraws the ring and nothing else");
        }
        let slowest = *frames.iter().max().unwrap();
        let animation = median(frames);
        let second = animation * MAX_FPS;
        let ring = layout.ring.area();
        let share = ring as f64 * 100.0 / screen;

        // A step of the glow's breath: its own small rectangle.
        let (_, _, glow) = layout.mark_pose.glow().unwrap();
        let mut breaths = Vec::new();
        for i in 0..50 {
            view.motion.glow = motion::GLOW_REST + motion::GLOW_BREATH * ((i + 1) % 2) as f32;
            breaths.push(frame(&mut scene, &view, &mut target));
            assert_eq!(scene.damage(), [glow]);
        }
        let breath = median(breaths);
        view.motion.glow = motion::GLOW_REST;

        // The glide to the unlock screen, at the most frames a second.
        view.prompt = Some(PromptView::new(PROMPT));
        let mut glide = Duration::ZERO;
        let mut glide_pixels = 0;
        for i in 1..=(motion::GLIDE * fps) as u32 {
            view.motion.reveal = i as f32 / (motion::GLIDE * fps);
            glide += frame(&mut scene, &view, &mut target);
            glide_pixels = glide_pixels.max(pixels(scene.damage()));
        }
        let glide_share = glide_pixels as f64 * 100.0 / screen;

        // The unlock screen: a keystroke and a frame of the shake redraw the
        // text box and nothing else, a blink of the caret only the caret.
        view.motion.reveal = 1.0;
        let mut prompt = PromptView::new(PROMPT);
        view.prompt = Some(prompt);
        scene.render(&view);
        let (mut keys, mut blinks, mut shakes) = (Vec::new(), Vec::new(), Vec::new());
        for i in 0..100usize {
            prompt.dots = 1 + i % 20;
            view.prompt = Some(prompt);
            keys.push(frame(&mut scene, &view, &mut target));
            assert_eq!(scene.damage(), [layout.field]);
            prompt.caret = !prompt.caret;
            view.prompt = Some(prompt);
            blinks.push(frame(&mut scene, &view, &mut target));
            assert_eq!(
                scene.damage(),
                [layout.caret_rect(prompt.dots, 0)],
                "a blink redraws the caret and nothing else"
            );
        }
        for i in 1..12u64 {
            prompt.shake = scene::shake_offset(Duration::from_millis(i * 33));
            view.prompt = Some(prompt);
            shakes.push(frame(&mut scene, &view, &mut target));
            assert!(layout.field.contains(&scene.damage().iter().fold(Rect::default(), |all, r| all.union(r))));
        }
        let (key, blink, shake) = (median(keys), median(blinks), median(shakes));
        let field_share = layout.field.area() as f64 * 100.0 / screen;

        println!(
            "{w}x{h}: first frame {first_frame:?}; intro {intro:?} for {} frames at {MAX_FPS} a second (median \
             {:?}, at most {intro_pixels} pixels, {intro_share:.2} % of the screen); ring frame {animation:?}, \
             slowest {slowest:?} ({ring} pixels, {share:.3} %), {second:?} a second at {MAX_FPS} frames a second; \
             breath of the glow {breath:?} ({} pixels); glide {glide:?} ({glide_share:.2} %); keystroke {key:?}, \
             caret {blink:?}, shake frame {shake:?} ({} pixels, {field_share:.3} %)",
            (motion::INTRO * fps) as u32,
            median(intro_frames),
            glow.area(),
            layout.field.area()
        );
        assert!(share < 0.25, "an animation frame touches under a quarter of a percent of the screen: {share}");
        assert!(field_share < 2.0, "a keystroke touches under two percent of the screen: {field_share}");
        assert!(intro_share < 5.0, "an intro frame touches under five percent of the screen: {intro_share}");
        assert!(animation < Duration::from_millis(20), "animation frame took {animation:?} at {w}x{h}");
        assert!(
            second < Duration::from_millis(500),
            "a second of the ring at {MAX_FPS} frames takes {second:?} of processor time at {w}x{h}"
        );
        assert!(breath < Duration::from_millis(40), "a breath took {breath:?} at {w}x{h}");
        assert!(key < Duration::from_millis(40), "keystroke redraw took {key:?} at {w}x{h}");
        assert!(blink < Duration::from_millis(40), "caret redraw took {blink:?} at {w}x{h}");
        assert!(shake < Duration::from_millis(40), "shake frame took {shake:?} at {w}x{h}");
        // The intro and the glide happen once; even unoptimised they are a
        // few seconds of processor time at the very most.
        assert!(intro < Duration::from_secs(20), "the intro took {intro:?} at {w}x{h}");
        assert!(glide < Duration::from_secs(10), "the glide took {glide:?} at {w}x{h}");
    }
}
