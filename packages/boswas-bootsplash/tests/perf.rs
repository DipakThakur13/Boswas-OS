//! How much work a frame is.
//!
//! The boot screen runs while the machine is busy starting up, so a frame
//! must cost next to nothing: only the progress arc is redrawn, and only its
//! rectangle is copied to the display. The limits here are deliberately
//! loose (they must hold in an unoptimised build on a slow build machine);
//! `cargo test --release --test perf -- --nocapture` prints the real figures.

use std::time::{Duration, Instant};

use boswas_bootsplash::gfx::{Canvas, Rect};
use boswas_bootsplash::scene::{Mode, PromptView, Scene, View};

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

#[test]
fn an_animation_frame_is_cheap() {
    for (w, h) in [(1366u32, 768u32), (1920, 1080), (3840, 2160)] {
        let started = Instant::now();
        let mut scene = Scene::new(w, h);
        let mut view = View::new(Mode::Boot);
        let full = scene.render(&view);
        let first_frame = started.elapsed();
        let mut target = vec![0u8; (w * h * 4) as usize];
        present(scene.canvas(), &full, &mut target);

        let mut frames = Vec::new();
        let mut damaged = 0;
        for i in 0..200 {
            view.phase = (i as f32 * 0.0206).fract();
            let t = Instant::now();
            let damage = scene.render(&view);
            present(scene.canvas(), &damage, &mut target);
            frames.push(t.elapsed());
            damaged = damage.iter().map(Rect::area).sum::<i64>();
        }
        let frame = median(frames);

        view.prompt = Some(PromptView { text: "Please unlock disk sda3_crypt".into(), dots: 0, caps_lock: false });
        scene.render(&view);
        let mut keys = Vec::new();
        for i in 0..100 {
            view.prompt.as_mut().unwrap().dots = i % 20;
            let t = Instant::now();
            let damage = scene.render(&view);
            present(scene.canvas(), &damage, &mut target);
            keys.push(t.elapsed());
        }
        let key = median(keys);

        let share = damaged as f64 * 100.0 / (w as f64 * h as f64);
        println!(
            "{w}x{h}: first frame {first_frame:?}; animation frame {frame:?} ({damaged} pixels, {share:.3} % of the screen); keystroke {key:?}"
        );
        assert!(share < 1.0, "an animation frame touches under 1 % of the screen");
        assert!(frame < Duration::from_millis(20), "animation frame took {frame:?} at {w}x{h}");
        assert!(key < Duration::from_millis(40), "keystroke redraw took {key:?} at {w}x{h}");
    }
}
