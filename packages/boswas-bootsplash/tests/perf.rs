//! How much work a frame is.
//!
//! The boot screen runs while the machine is busy starting up, so a frame
//! must cost next to nothing: only the ring's rectangle is redrawn (five
//! dots on the background) and copied to the display; during a prompt only
//! the text box is. The time limits here are deliberately loose (they must
//! hold in an unoptimised build on a slow build machine);
//! `cargo test --release --test perf -- --nocapture` prints the real figures.

use std::time::{Duration, Instant};

use boswas_bootsplash::gfx::{Canvas, Rect};
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

#[test]
fn an_animation_frame_is_cheap() {
    const PROMPT: &str = "Please unlock disk sda3_crypt";
    for (w, h) in [(1366u32, 768u32), (1920, 1080), (3840, 2160)] {
        let screen = w as f64 * h as f64;
        let started = Instant::now();
        let mut scene = Scene::new(w, h);
        let mut view = View::new(Mode::Boot);
        scene.render(&view);
        let first_frame = started.elapsed();
        let mut target = vec![0u8; (w * h * 4) as usize];
        present(scene.canvas(), scene.damage(), &mut target);
        let layout = scene.layout().clone();

        // The ring, at 30 frames a second, for more than a whole cycle.
        let mut frames = Vec::new();
        for i in 1..=200 {
            view.phase = (i as f32 / (30.0 * scene::SPINNER_SECS)).fract();
            frames.push(frame(&mut scene, &view, &mut target));
            assert_eq!(scene.damage(), [layout.spinner], "an animation frame redraws the ring and nothing else");
        }
        let slowest = *frames.iter().max().unwrap();
        let animation = median(frames);
        let ring = layout.spinner.area();
        let share = ring as f64 * 100.0 / screen;

        // The unlock screen: a keystroke and a frame of the shake redraw the
        // text box and nothing else, a blink of the caret only the caret.
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
            "{w}x{h}: first frame {first_frame:?}; animation frame {animation:?}, slowest {slowest:?} \
             ({ring} pixels, {share:.3} % of the screen); keystroke {key:?}, caret {blink:?}, shake frame {shake:?} \
             ({} pixels, {field_share:.3} %)",
            layout.field.area()
        );
        assert!(share < 0.25, "an animation frame touches under a quarter of a percent of the screen: {share}");
        assert!(field_share < 2.0, "a keystroke touches under two percent of the screen: {field_share}");
        assert!(animation < Duration::from_millis(20), "animation frame took {animation:?} at {w}x{h}");
        assert!(key < Duration::from_millis(40), "keystroke redraw took {key:?} at {w}x{h}");
        assert!(blink < Duration::from_millis(40), "caret redraw took {blink:?} at {w}x{h}");
        assert!(shake < Duration::from_millis(40), "shake frame took {shake:?} at {w}x{h}");
    }
}
