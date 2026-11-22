use super::*;
use crate::motion::{self, Choreography, Opening};

const DISPLAYS: [(u32, u32); 11] = [
    (640, 480),
    (800, 600),
    (1024, 768),
    (1280, 720),
    (1366, 768),
    (1920, 1080),
    (2560, 1440),
    (2560, 1600),
    (3840, 2160),
    (5120, 2880),
    (1080, 1920),
];

const UNLOCK: &str = "Please unlock disk nvme0n1p3_crypt";
const REJECTED: &str = "cryptsetup: ERROR: nvme0n1p3_crypt: cryptsetup failed, bad password or options?";
const SHORT_ERROR: &str = "That passphrase did not work. Try again.";

fn area_of(rects: &[Rect]) -> i64 {
    rects.iter().map(Rect::area).sum()
}

/// Pixels of exactly `colour`: for solid shapes.
fn count(canvas: &Canvas, rect: &Rect, colour: Rgb) -> usize {
    let mut n = 0;
    for y in rect.y..rect.bottom() {
        for x in rect.x..rect.right() {
            n += usize::from(canvas.pixel(x as u32, y as u32) == colour);
        }
    }
    n
}

/// Pixels that are not the background: drawn on, however faintly.
fn ink(scene: &Scene, rect: &Rect) -> usize {
    let rect = rect.intersect(&scene.canvas().bounds());
    let mut n = 0;
    for y in rect.y..rect.bottom() {
        for x in rect.x..rect.right() {
            n += usize::from(scene.canvas().pixel(x as u32, y as u32) != scene.backdrop().pixel(x, y));
        }
    }
    n
}

fn blank(scene: &Scene, rect: &Rect) -> bool {
    ink(scene, rect) == 0
}

/// Pixels drawn in `colour` over the background at a third of full coverage
/// or more: every channel is the same mix of the two. Tells a hue (danger,
/// warning, accent) from white or grey text. Channels in which the colour
/// and the background are close tell nothing and are left out.
fn tinted(scene: &Scene, rect: &Rect, colour: Rgb) -> usize {
    let mut n = 0;
    for y in rect.y..rect.bottom() {
        for x in rect.x..rect.right() {
            let p = scene.canvas().pixel(x as u32, y as u32);
            let bg = scene.backdrop().pixel(x, y);
            let shares: Vec<f32> = [(p.r, bg.r, colour.r), (p.g, bg.g, colour.g), (p.b, bg.b, colour.b)]
                .into_iter()
                .filter(|&(_, bg, c)| (c as i32 - bg as i32).abs() >= 40)
                .map(|(p, bg, c)| (p as f32 - bg as f32) / (c as f32 - bg as f32))
                .collect();
            let (low, high) = (shares.iter().copied().fold(1.0, f32::min), shares.iter().copied().fold(0.0, f32::max));
            n += usize::from(shares.len() >= 2 && low >= 0.33 && high <= 1.02 && high - low <= 0.06);
        }
    }
    n
}

/// The pixels that differ between two frames, as a bounding rectangle.
fn changed(before: &[u32], after: &[u32], width: u32) -> Rect {
    let mut area = Rect::default();
    for (i, (a, b)) in before.iter().zip(after).enumerate() {
        if a != b {
            area = area.union(&Rect::new((i as u32 % width) as i32, (i as u32 / width) as i32, 1, 1));
        }
    }
    area
}

fn prompt(text: &str) -> View<'_> {
    View::new(Mode::Boot).with_prompt(PromptView::new(text))
}

/// The lines of text of a frame: slot, rectangle, text, style, colour.
fn labels<'a>(l: &Layout, view: &View<'a>) -> Vec<(usize, Rect, &'a str, TextStyle, Rgb)> {
    compose(l, view)
        .iter()
        .enumerate()
        .filter_map(|(slot, item)| match item.as_ref()?.kind {
            Kind::Label { text, style, colour, .. } => Some((slot, item.as_ref()?.rect, text, style, colour)),
            _ => None,
        })
        .collect()
}

fn slots(l: &Layout, view: &View) -> Vec<usize> {
    let items = compose(l, view);
    (0..SLOTS).filter(|&slot| items[slot].is_some()).collect()
}

fn with_motion(mode: Mode, motion: Motion) -> View<'static> {
    View { motion, ..View::new(mode) }
}

#[test]
fn the_unit_is_one_pixel_on_ordinary_displays_and_grows_on_dense_ones() {
    for ((w, h), unit) in [
        ((1024, 768), 1.0),
        ((1366, 768), 1.0),
        ((1920, 1080), 1.0),
        ((2560, 1600), 1600.0 / 1080.0),
        ((3840, 2160), 2.0),
        ((7680, 4320), MAX_UNIT),
        ((640, 480), 0.8),
        // Turned displays: the shorter side decides.
        ((1080, 1920), 1.0),
        ((2160, 3840), 2.0),
    ] {
        assert!((Layout::new(w, h).unit - unit).abs() < 1e-4, "{w}x{h}: {}", Layout::new(w, h).unit);
    }
}

#[test]
fn layout_fits_common_displays() {
    for (w, h) in DISPLAYS {
        let l = Layout::new(w, h);
        let screen = Rect::new(0, 0, w as i32, h as i32);
        let boot = [("mark", l.mark), ("ring", l.ring), ("boot message", l.boot_message)];
        let busy = [("mark", l.mark), ("ring", l.ring), ("status", l.status), ("message", l.busy_message)];
        let unlock = [
            ("mark", l.prompt_mark),
            ("title", l.title),
            ("detail", l.detail),
            ("field", l.field),
            ("line", l.prompt_line),
            ("second line", l.prompt_second_line),
        ];
        // Everything is on the display, in order from the top, and items
        // that are visible together do not overlap.
        for screenful in [&boot[..], &busy[..], &unlock[..]] {
            for (name, r) in screenful {
                assert!(screen.contains(r), "{name} {r:?} leaves a {w}x{h} display");
            }
            for pair in screenful.windows(2) {
                assert!(pair[1].1.y >= pair[0].1.bottom(), "{} overlaps {} at {w}x{h}", pair[1].0, pair[0].0);
            }
        }
        assert!(l.field.contains(&l.text_box));
        // Centred on the same axis, to the pixel.
        let axis = w as i32 / 2;
        for r in [l.mark, l.prompt_mark, l.ring, l.text_box, l.title, l.status] {
            assert!((r.x * 2 + r.w - axis * 2).abs() <= 2, "{r:?} is off the axis of a {w}x{h} display");
        }
        // The mark: 16 % of the shorter side (200 logical pixels at most),
        // its middle a little above the middle of the screen.
        let side = w.min(h) as f32;
        let mark_h = l.mark_pose.height();
        assert!((mark_h - (MARK_SHARE * side).min(MARK_MAX * l.unit)).abs() < 0.01, "{mark_h} at {w}x{h}");
        let (_, mark_cy) = l.mark_pose.centre();
        assert!((mark_cy - MARK_AT * h as f32).abs() <= 1.0);
        // The ring: 34 logical pixels across, close under the mark.
        assert!((l.ring.w as f32 - RING_SIZE * l.unit).abs() <= 4.0, "{:?} at {w}x{h}", l.ring);
        let gap = l.ring.y as f32 + 1.0 - (mark_cy + mark_h / 2.0);
        assert!((gap - RING_GAP * mark_h).abs() <= 2.0, "{gap} at {w}x{h}");
        // The unlock screen: the mark smaller and higher, at the head of a
        // column centred a little above the middle.
        assert!((l.prompt_pose.height() - PROMPT_MARK * mark_h).abs() < 0.01);
        assert!(l.prompt_mark.y < l.mark.y);
        let column = (l.prompt_mark.y + l.prompt_second_line.bottom()) as f32 / 2.0;
        assert!((column - 0.47 * h as f32).abs() <= 0.05 * h as f32, "{column} at {w}x{h}");
    }
}

#[test]
fn the_text_box_has_the_metrics_of_a_fluent_control() {
    let l = Layout::new(1920, 1080);
    assert_eq!((l.text_box.w, l.text_box.h), (320, 36));
    assert_eq!(l.caret, (1, 18));
    let l = Layout::new(3840, 2160);
    assert_eq!((l.text_box.w, l.text_box.h), (640, 72));
    assert_eq!(l.caret, (2, 36));
    assert_eq!(Layout::new(1366, 768).text_px(assets::BODY), 14.0);
    assert_eq!(Layout::new(3840, 2160).text_px(assets::SUBTITLE), 40.0);
    // On a 2560x1440 display text is set at its exact size, not rounded.
    assert!((Layout::new(2560, 1440).text_px(assets::BODY) - 14.0 * 1440.0 / 1080.0).abs() < 1e-3);
}

#[test]
fn the_lines_follow_the_type_ramp_and_the_dark_theme() {
    let l = Layout::new(1920, 1080);
    assert!(labels(&l, &View::new(Mode::Boot)).is_empty(), "the boot screen has no text");
    let mut view = View::new(Mode::Boot);
    view.message = Some(("Checking disks", Tone::Info));
    assert_eq!(
        labels(&l, &view),
        [(SLOT_MESSAGE, l.boot_message, "Checking disks", assets::BODY, assets::TEXT_SECONDARY)]
    );
    for (mode, status) in [(Mode::Shutdown, "Shutting down"), (Mode::Reboot, "Restarting")] {
        assert_eq!(labels(&l, &View::new(mode)), [(SLOT_STATUS, l.status, status, assets::BODY_LARGE, assets::TEXT)]);
    }
    let mut view = prompt(UNLOCK);
    assert_eq!(
        labels(&l, &view),
        [
            (SLOT_TITLE, l.title, "Unlock your disk", assets::SUBTITLE, assets::TEXT),
            (SLOT_DETAIL, l.detail, "nvme0n1p3_crypt", assets::BODY, assets::TEXT_SECONDARY),
            (SLOT_HINT, l.prompt_line, "Press Enter to unlock", assets::CAPTION, assets::TEXT_MUTED),
        ]
    );
    view.prompt = Some(PromptView { caps_lock: true, ..PromptView::new(UNLOCK) });
    assert_eq!(labels(&l, &view)[2], (SLOT_HINT, l.prompt_line, "Caps Lock is on", assets::CAPTION, assets::WARNING));
    // An error takes the line under the box; the hint moves down one.
    view.message = Some((REJECTED, Tone::Error));
    assert_eq!(labels(&l, &view)[..1], [(SLOT_MESSAGE, l.prompt_line, SHORT_ERROR, assets::CAPTION, assets::DANGER)]);
    assert_eq!(
        labels(&l, &view)[3],
        (SLOT_HINT, l.prompt_second_line, "Caps Lock is on", assets::CAPTION, assets::WARNING)
    );
    view.message = Some(("Second volume", Tone::Info));
    assert_eq!(
        labels(&l, &view)[0],
        (SLOT_MESSAGE, l.prompt_line, "Second volume", assets::CAPTION, assets::TEXT_SECONDARY)
    );
}

#[test]
fn tiny_and_odd_sizes_do_not_panic() {
    for (w, h) in [(1, 1), (7, 3), (320, 200), (200, 3000), (5000, 100)] {
        let mut scene = Scene::new(w, h);
        scene.render(&View::new(Mode::Boot));
        scene.render(&View::new(Mode::Reboot));
        scene.render(&with_motion(Mode::Boot, motion::intro(0.9)));
        let mut view = prompt("x");
        view.prompt = Some(PromptView { dots: 3, caps_lock: true, shake: -8.0, ..PromptView::new("x") });
        view.message = Some(("m", Tone::Error));
        view.motion.reveal = 0.6;
        scene.render(&view);
        view.motion.reveal = f32::NAN;
        view.motion.mark_scale = f32::NAN;
        scene.render(&view);
    }
}

#[test]
fn first_frame_paints_everything_then_nothing() {
    let mut scene = Scene::new(1280, 720);
    let view = View::new(Mode::Boot);
    assert_eq!(scene.render(&view), [Rect::new(0, 0, 1280, 720)]);
    assert_eq!(scene.damage(), [Rect::new(0, 0, 1280, 720)]);
    assert_eq!(scene.canvas().pixel(2, 2), assets::BACKGROUND);
    assert!(scene.render(&view).is_empty(), "an unchanged view repaints nothing");
    assert!(scene.damage().is_empty());
    scene.invalidate();
    assert_eq!(scene.render(&view).len(), 1);
}

#[test]
fn the_boot_screen_is_the_mark_and_the_ring_on_a_bloom() {
    let mut scene = Scene::new(1920, 1080);
    scene.render(&View::new(Mode::Boot));
    let l = scene.layout().clone();
    let canvas = scene.canvas();
    assert!(ink(&scene, &l.mark) > (l.mark.area() / 4) as usize, "the mark covers a good part of its box");
    assert!(l.mark.h >= 170 && l.mark.h <= 180, "{:?}", l.mark);
    assert!(count(canvas, &l.ring, assets::TEXT) > 40, "the arc of the ring is drawn in white");
    // The mark keeps its own colours: the blue of its stem and bowl, and the
    // warm dot in the counter.
    let (mut blue, mut warm) = (0, 0);
    for y in l.mark.y..l.mark.bottom() {
        for x in l.mark.x..l.mark.right() {
            let p = canvas.pixel(x as u32, y as u32);
            blue += usize::from(p.b > 180 && p.b > p.r + 60);
            warm += usize::from(p.r > 220 && p.g > 150 && p.b < 120);
        }
    }
    assert!(blue > 8000, "blue pixels in the mark: {blue}");
    assert!(warm > 250, "warm pixels in the dot of the mark: {warm}");
    // Nothing else is drawn: no wordmark, no message, no status.
    assert_eq!(ink(&scene, &canvas.bounds()), ink(&scene, &l.mark) + ink(&scene, &l.ring));
    // The background: flat at the edges, lit behind the mark, blue there.
    for (x, y) in [(0, 0), (1919, 0), (0, 1079), (1919, 1079)] {
        assert_eq!(canvas.pixel(x, y), assets::BACKGROUND);
    }
    let behind = canvas.pixel(l.mark.x as u32 - 30, l.mark.y as u32);
    let bg = assets::BACKGROUND;
    assert!(behind.b as i32 > bg.b as i32 + 30 && behind.b > behind.r + 30, "{behind:?}");
}

#[test]
fn the_ring_lengthens_and_shortens_while_it_turns() {
    // A cycle starts and ends with the shortest arc at 12 o'clock and is
    // longest half-way.
    assert_eq!(ring_arc(0.0), (0.0, ARC_MIN));
    let (_, longest) = ring_arc(0.5);
    assert!((longest - (ARC_MIN + ARC_GROWTH)).abs() < 1e-3);
    let (end_start, end_sweep) = ring_arc(1.0 - 1e-6);
    assert!((end_start - 360.0).abs() < 0.01 || end_start < 0.01);
    assert!((end_sweep - ARC_MIN).abs() < 0.01);
    // Both ends only ever go clockwise, never jump, and the tail goes round
    // once in a cycle.
    let steps = 2000;
    let (mut tail, mut head) = (0.0f32, ARC_MIN);
    let mut fastest = 0f32;
    for i in 1..=steps {
        let (start, sweep) = ring_arc(i as f32 / steps as f32);
        let mut next_tail = start;
        while next_tail < tail - 1.0 {
            next_tail += 360.0;
        }
        let next_head = next_tail + sweep;
        assert!(next_tail >= tail - 1e-3 && next_head >= head - 1e-3, "step {i}");
        fastest = fastest.max(next_tail - tail).max(next_head - head);
        (tail, head) = (next_tail, next_head);
    }
    assert!((tail - 360.0).abs() < 0.05, "{tail}");
    // At 30 frames a second an end moves less than a tenth of a turn a frame.
    let per_frame = fastest * steps as f32 / (motion::RING_SECS * 30.0);
    assert!(per_frame < 36.0, "{per_frame} degrees");
    // At rest it shows a long arc.
    assert!(ring_arc(REST_PHASE).1 > 200.0);

    // Drawn: an arc from 12 to 3 o'clock lights the upper right only.
    let phase = (0..1000).map(|i| i as f32 / 2000.0).find(|&p| ring_arc(p).1 >= 90.0).unwrap();
    let (start, sweep) = ring_arc(phase);
    assert!(start < 20.0, "{start}");
    let mut scene = Scene::new(1920, 1080);
    scene.render(&View { phase, ..View::new(Mode::Boot) });
    let r = scene.layout().ring;
    let (cx, cy) = (r.x + r.w / 2, r.y + r.h / 2);
    let quarter = |x: i32, y: i32| Rect::new(x, y, r.w / 2, r.h / 2);
    let upper_right = quarter(cx, r.y);
    assert!(count(scene.canvas(), &upper_right, assets::TEXT) > 15, "{start} + {sweep}");
    assert!(blank(&scene, &quarter(r.x, cy)), "nothing at the lower left");
}

#[test]
fn a_reported_progress_fills_the_ring_from_the_top() {
    let mut scene = Scene::new(1920, 1080);
    let r = scene.layout().ring;
    let (cx, cy) = (r.x + r.w / 2, r.y + r.h / 2);
    let quarter = |x: i32, y: i32| Rect::new(x, y, r.w / 2, r.h / 2);
    scene.render(&View { progress: Some(25), ..View::new(Mode::Boot) });
    // The first quarter in white, the rest a faint track.
    assert!(count(scene.canvas(), &quarter(cx, r.y), assets::TEXT) > 15);
    let lower_left = quarter(r.x, cy);
    assert_eq!(count(scene.canvas(), &lower_left, assets::TEXT), 0);
    assert!(ink(&scene, &lower_left) > 20, "the track");
    let faint = (lower_left.y..lower_left.bottom())
        .flat_map(|y| (lower_left.x..lower_left.right()).map(move |x| (x, y)))
        .map(|(x, y)| scene.canvas().pixel(x as u32, y as u32).r as i32 - scene.backdrop().pixel(x, y).r as i32)
        .max()
        .unwrap();
    assert!(faint > 10 && faint < 70, "{faint}");
    // The ring's own share overrides the reported one while it glides.
    let mut view = View { progress: Some(25), ..View::new(Mode::Boot) };
    view.motion.ring_progress = Some(1.0);
    scene.render(&view);
    assert!(count(scene.canvas(), &lower_left, assets::TEXT) > 15, "full");
    // A still screen with a progress repaints nothing.
    assert!(scene.render(&view).is_empty());
}

#[test]
fn an_animation_frame_only_damages_the_ring() {
    for mode in [Mode::Boot, Mode::Shutdown] {
        let mut scene = Scene::new(1920, 1080);
        let mut view = View::new(mode);
        view.message = Some(("Starting the system", Tone::Info));
        scene.render(&view);
        let ring = scene.layout().ring;
        assert!(ring.area() <= 38 * 38, "the ring is small: {ring:?}");
        let mut moved = 0;
        for step in 1..=130 {
            let before: Vec<u32> = scene.canvas().pixels().to_vec();
            view.phase = step as f32 / (30.0 * motion::RING_SECS); // a slow pace, past the end of a cycle
            let damage = scene.render(&view).to_vec();
            assert_eq!(damage, [ring], "frame {step}");
            // No pixel outside the damage changed.
            let area = changed(&before, scene.canvas().pixels(), 1920);
            assert!(ring.contains(&area), "frame {step} changed {area:?}");
            moved += usize::from(!area.is_empty());
        }
        assert!(moved > 120, "the ring moves in nearly every frame: {moved}");
    }
}

#[test]
fn the_glow_breathes_in_its_own_rectangle() {
    let mut scene = Scene::new(1920, 1080);
    let mut view = View::new(Mode::Boot);
    scene.render(&view);
    let (_, _, glow) = scene.layout().mark_pose.glow().unwrap();
    assert!(scene.layout().mark.contains(&glow), "inside the mark's box");
    let before = scene.canvas().pixels().to_vec();
    view.motion.glow = motion::GLOW_REST + motion::GLOW_BREATH;
    assert_eq!(scene.render(&view), [glow]);
    let area = changed(&before, scene.canvas().pixels(), 1920);
    assert!(!area.is_empty() && glow.contains(&area), "{area:?}");
    // A change of the glow too small to show repaints nothing.
    view.motion.glow += 0.0005;
    assert!(scene.render(&view).is_empty());
    // Breathing out again: the picture at rest.
    view.motion.glow = motion::GLOW_REST;
    scene.render(&view);
    assert!(scene.canvas().pixels() == before);
}

#[test]
fn the_intro_draws_the_mark_in_its_box_then_the_ring() {
    let mut scene = Scene::new(1366, 768);
    scene.render(&with_motion(Mode::Boot, motion::intro(0.0)));
    let l = scene.layout().clone();
    assert!(blank(&scene, &scene.canvas().bounds()), "the first frame is the background alone");
    let allowed = l.mark.union(&l.ring);
    let mut damaged_ring = false;
    for frame in 1..=(motion::INTRO * 60.0) as u32 {
        let t = frame as f32 / 60.0;
        let before = scene.canvas().pixels().to_vec();
        let damage = scene.render(&with_motion(Mode::Boot, motion::intro(t))).to_vec();
        for d in &damage {
            assert!(allowed.contains(d), "{t}: {d:?}");
            if d.intersects(&l.ring) {
                damaged_ring = true;
                assert!(t >= motion::RING_FADE.0, "the ring appears at the end: {t}");
            }
        }
        let area = changed(&before, scene.canvas().pixels(), 1366);
        assert!(allowed.contains(&area), "{t}: {area:?}");
        assert!(area_of(&damage) <= l.mark.area() + l.ring.area());
    }
    assert!(damaged_ring);
    // Half-way: the stem has risen, the dot is not there yet.
    let mut half = Scene::new(1366, 768);
    half.render(&with_motion(Mode::Boot, motion::intro(0.5)));
    let stem_top = l.mark_pose.map(63.0, 20.0);
    let dot = l.mark_pose.map(134.0, 160.0);
    let lit =
        |s: &Scene, (x, y): (f32, f32)| s.canvas().pixel(x as u32, y as u32) != s.backdrop().pixel(x as i32, y as i32);
    assert!(lit(&half, stem_top));
    let warm = half.canvas().pixel(dot.0 as u32, dot.1 as u32);
    assert!(warm.r < 150, "no dot yet: {warm:?}");
    // At the end, the picture at rest.
    let mut rest = Scene::new(1366, 768);
    rest.render(&View::new(Mode::Boot));
    assert!(scene.canvas().pixels() == rest.canvas().pixels());
}

#[test]
fn the_glide_moves_the_mark_and_brings_in_the_prompt() {
    let l = Layout::new(1920, 1080);
    let mut scene = Scene::new(1920, 1080);
    let mut view = View::new(Mode::Boot);
    view.prompt = Some(PromptView::new(UNLOCK));
    let mut last_mark = l.mark;
    for step in 0..=40 {
        view.motion.reveal = step as f32 / 40.0;
        let items = compose(&l, &view);
        let mark = items[SLOT_MARK].unwrap().rect;
        assert!(mark.y <= last_mark.y && mark.h <= last_mark.h, "the mark glides up and shrinks");
        last_mark = mark;
        // The prompt never overlaps the mark, and comes in after the ring left.
        if let Some(title) = items[SLOT_TITLE] {
            assert!(!title.rect.intersects(&mark), "step {step}: {:?} over {mark:?}", title.rect);
            assert!(items[SLOT_RING].is_none(), "step {step}");
        }
        scene.render(&view);
    }
    assert_eq!(last_mark, l.prompt_mark);
    // At the end of the glide the unlock screen is exactly as at rest.
    let mut fresh = Scene::new(1920, 1080);
    fresh.render(&prompt(UNLOCK));
    assert!(scene.canvas().pixels() == fresh.canvas().pixels());
    // Mid-way the prompt is faint and lower than at rest.
    view.motion.reveal = 0.7;
    let items = compose(&l, &view);
    let Some(Item { rect, kind: Kind::Label { alpha, .. } }) = items[SLOT_TITLE] else { panic!("no title") };
    assert!(alpha > 0 && alpha < 255 && rect.y > l.title.y, "{alpha} {rect:?}");
    // And a glide frame touches only the mark, the ring and the prompt.
    let column = l.mark.union(&l.ring).union(&l.boot_message).union(&l.prompt_mark).union(&l.prompt_second_line);
    let column = column.union(&moved(l.field, l.slide)).union(&moved(l.prompt_second_line, l.slide));
    for d in scene.render(&view) {
        assert!(column.contains(d), "{d:?}");
    }
}

#[test]
fn leaving_fades_to_the_background_or_leaves_the_mark() {
    let mut scene = Scene::new(1920, 1080);
    let mut view = View::new(Mode::Boot);
    view.message = Some(("Starting emergency shell", Tone::Info));
    scene.render(&view);
    let mut c = Choreography::new(Opening::Intro, true, 0.0);
    c.leave(false, 10.0);
    view.motion = c.motion(10.0 + motion::OUTRO as f64 / 2.0);
    scene.render(&view);
    let l = scene.layout().clone();
    assert!(ink(&scene, &l.mark) > 0, "half-way, the mark is faint");
    view.motion = c.motion(10.0 + motion::OUTRO as f64);
    scene.render(&view);
    assert!(blank(&scene, &scene.canvas().bounds()), "then gone: the background alone");
    // Kept for the next program: the mark, without the ring or the text.
    let mut c = Choreography::new(Opening::Intro, true, 0.0);
    c.leave(true, 10.0);
    view.motion = c.motion(11.0);
    scene.render(&view);
    let mut mark_only = Scene::new(1920, 1080);
    mark_only.render(&View { motion: Motion { ring_alpha: 0.0, ..Motion::REST }, ..View::new(Mode::Boot) });
    assert!(scene.canvas().pixels() == mark_only.canvas().pixels());
    assert!(blank(&scene, &l.ring) && blank(&scene, &l.boot_message));
    assert_eq!(ink(&scene, &scene.canvas().bounds()), ink(&scene, &l.mark));
}

#[test]
fn frames_leave_nothing_behind() {
    // A frame reached through any number of others is, pixel for pixel, the
    // frame painted from nothing: no trace of earlier arcs, carets, lines of
    // text, glides or shaken boxes.
    let mut scene = Scene::new(1366, 768);
    for step in 0..100 {
        let mut view = with_motion(Mode::Boot, motion::intro(step as f32 / 50.0));
        view.phase = step as f32 / 90.0;
        scene.render(&view);
    }
    let mut walk = prompt(UNLOCK);
    for step in 0..40u64 {
        let shake = shake_offset(Duration::from_millis(step * 10));
        let typing = PromptView { dots: step as usize % 30, caret: step % 3 != 0, shake, ..PromptView::new(UNLOCK) };
        walk.prompt = Some(typing);
        walk.message = (step % 7 < 3).then_some((REJECTED, Tone::Error));
        walk.motion.reveal = (step as f32 / 20.0).min(1.0);
        scene.render(&walk);
    }
    let stopping = View { message: Some(("Stopping services", Tone::Info)), ..View::new(Mode::Shutdown) };
    for view in [
        View { phase: 0.4, ..View::new(Mode::Boot) },
        prompt(UNLOCK),
        stopping,
        View { progress: Some(7), ..View::new(Mode::Boot) },
        View::new(Mode::Reboot),
        with_motion(Mode::Boot, Motion { glow: 0.3, ..Motion::REST }),
        View::new(Mode::Boot),
    ] {
        scene.render(&view);
        let mut fresh = Scene::new(1366, 768);
        fresh.render(&view);
        assert!(scene.canvas().pixels() == fresh.canvas().pixels(), "{view:?} differs from a fresh frame");
    }
}

#[test]
fn the_unlock_screen_has_a_title_the_disk_name_a_text_box_and_a_hint() {
    let mut scene = Scene::new(1366, 768);
    scene.render(&prompt(UNLOCK));
    let l = scene.layout().clone();
    assert!(ink(&scene, &l.title) > 300, "the title");
    assert!(ink(&scene, &l.detail) > 150, "the name of the disk");
    assert!(ink(&scene, &l.prompt_line) > 150, "the hint");
    assert!(blank(&scene, &l.prompt_second_line));
    assert!(ink(&scene, &l.prompt_mark) > (l.prompt_mark.area() / 4) as usize, "the mark, smaller, above");
    assert!(blank(
        &scene,
        &Rect::new(l.ring.x, l.prompt_second_line.bottom() + 4, l.ring.w, 768 - l.prompt_second_line.bottom() - 4)
    ));
    // The box: 320 by 36, the translucent white fill and outline of a
    // control over the background, and the accent line of a focused one
    // along the bottom two rows only.
    let b = l.text_box;
    assert_eq!((b.w, b.h), (320, 36));
    let layered = |x: i32, y: i32, opacity: f32| {
        let bg = scene.backdrop().pixel(x, y);
        bg.mix(assets::CONTROL_LAYER, opacity)
    };
    let close = |a: Rgb, b: Rgb| a.r.abs_diff(b.r) <= 1 && a.g.abs_diff(b.g) <= 1 && a.b.abs_diff(b.b) <= 1;
    let fill = assets::FILL_CONTROL;
    let outline = 1.0 - (1.0 - fill) * (1.0 - assets::STROKE_CONTROL);
    for (x, y, opacity) in [
        (b.x + 60, b.y + 10, fill),
        (b.right() - 20, b.y + 25, fill),
        (b.x, b.y + 18, outline),
        (b.x + 100, b.y, outline),
    ] {
        let p = scene.canvas().pixel(x as u32, y as u32);
        assert!(close(p, layered(x, y, opacity)), "({x}, {y}): {p:?} vs {:?}", layered(x, y, opacity));
    }
    let accent_rows = Rect::new(b.x, b.bottom() - 2, b.w, 2);
    assert!(count(scene.canvas(), &accent_rows, assets::ACCENT) > 2 * 300, "the accent line");
    assert_eq!(count(scene.canvas(), &b, assets::ACCENT), count(scene.canvas(), &accent_rows, assets::ACCENT));
    assert_eq!(ink(&scene, &l.field), ink(&scene, &b), "nothing is drawn round the box");
    // The corners are rounded by 4 pixels: a rectangle, not a pill.
    let corner = scene.canvas().pixel(b.x as u32, b.y as u32);
    assert!(close(corner, layered(b.x, b.y, 0.0)) || corner.r.abs_diff(layered(b.x, b.y, 0.0).r) <= 3, "{corner:?}");
    assert!(close(scene.canvas().pixel(b.x as u32 + 4, b.y as u32), layered(b.x + 4, b.y, outline)));
    // An empty box shows the caret at the left padding.
    let caret = Rect::new(b.x + 12, b.y + 9, 1, 18);
    assert_eq!(count(scene.canvas(), &caret, assets::TEXT), 18);
    assert_eq!(count(scene.canvas(), &b, assets::TEXT), 18, "and nothing else in the text colour");

    // Back to the ring: the boot screen as if the prompt had never been.
    scene.render(&View::new(Mode::Boot));
    let mut fresh = Scene::new(1366, 768);
    fresh.render(&View::new(Mode::Boot));
    assert!(scene.canvas().pixels() == fresh.canvas().pixels());
}

#[test]
fn prompts_are_turned_into_a_title_and_a_name() {
    let other = "Password for the backup volume";
    for (text, title, detail, hint) in [
        ("Please unlock disk sda3_crypt", "Unlock your disk", "sda3_crypt", "Press Enter to unlock"),
        ("Please unlock disk sda3_crypt: ", "Unlock your disk", "sda3_crypt", "Press Enter to unlock"),
        (
            "Please enter passphrase for disk Samsung SSD 990 (luks-1a2b):",
            "Unlock your disk",
            "Samsung SSD 990 (luks-1a2b)",
            "Press Enter to unlock",
        ),
        ("Enter passphrase for /dev/sda3: ", "Unlock your disk", "/dev/sda3", "Press Enter to unlock"),
        (other, "Enter your passphrase", other, "Press Enter to continue"),
        ("Please unlock disk ", "Enter your passphrase", "Please unlock disk", "Press Enter to continue"),
        ("", "Enter your passphrase", "", "Press Enter to continue"),
    ] {
        let lines = prompt_lines(text);
        assert_eq!((lines.title, lines.detail, lines.hint), (title, detail, hint), "{text:?}");
    }
    // A prompt without text has no second line.
    let mut scene = Scene::new(1024, 768);
    scene.render(&prompt(""));
    let l = scene.layout().clone();
    assert!(blank(&scene, &l.detail));
    assert!(ink(&scene, &l.title) > 300);
}

#[test]
fn typing_only_damages_the_field_and_shows_dots_not_characters() {
    let mut scene = Scene::new(1920, 1080);
    let mut view = prompt(UNLOCK);
    scene.render(&view);
    let (field, b) = (scene.layout().field, scene.layout().text_box);
    let empty = count(scene.canvas(), &field, assets::TEXT);
    let mut typed = PromptView { dots: 5, ..PromptView::new(UNLOCK) };
    view.prompt = Some(typed);
    assert_eq!(scene.render(&view), [field]);
    assert!(count(scene.canvas(), &field, assets::TEXT) > empty + 5 * 12, "five dots appear");
    // From the left, 12 pixels in, on the middle line of the box: five
    // identical dots 10 pixels apart, then the caret.
    let canvas = scene.canvas();
    let (left, row) = (b.x as u32 + 12, b.y as u32 + 18);
    for i in 0..5 {
        assert_eq!(canvas.pixel(left + 10 * i + 3, row), assets::TEXT, "dot {i}");
        assert_ne!(canvas.pixel(left + 10 * i + 8, row), assets::TEXT, "gap after dot {i}");
        let dot = Rect::new((left + 10 * i) as i32, b.y + 1, 10, b.h - 4);
        assert_eq!(
            count(canvas, &dot, assets::TEXT),
            count(canvas, &Rect::new(left as i32, b.y + 1, 10, b.h - 4), assets::TEXT)
        );
    }
    assert_eq!(canvas.pixel(left + 50, row), assets::TEXT, "the caret follows the dots");
    assert_ne!(canvas.pixel(left + 53, row), assets::TEXT);
    // The dot count is capped, and the dots stay inside the box.
    typed.dots = MAX_DOTS;
    view.prompt = Some(typed);
    scene.render(&view);
    let capped = scene.canvas().pixels().to_vec();
    let right = Rect::new(b.right() - 12, b.y, 12, b.h);
    assert_eq!(count(scene.canvas(), &right, assets::TEXT), 0, "the right padding stays clear");
    typed.dots = MAX_DOTS + 40;
    view.prompt = Some(typed);
    assert!(scene.render(&view).is_empty(), "more characters than the cap change nothing on screen");
    assert_eq!(capped, scene.canvas().pixels());
}

#[test]
fn the_caret_blinks_inside_the_field() {
    assert!(caret_visible(Duration::ZERO) && caret_visible(Duration::from_millis(529)));
    assert!(!caret_visible(Duration::from_millis(530)) && !caret_visible(Duration::from_millis(1059)));
    assert!(caret_visible(Duration::from_millis(1060)));

    let mut scene = Scene::new(1920, 1080);
    let mut view = prompt(UNLOCK);
    let mut typed = PromptView { dots: 3, caret: false, ..PromptView::new(UNLOCK) };
    view.prompt = Some(typed);
    scene.render(&view);
    let off = scene.canvas().pixels().to_vec();
    let l = scene.layout().clone();
    let caret = l.caret_rect(3, 0);
    assert_eq!((caret.x, caret.y, caret.w, caret.h), (l.text_box.x + 12 + 30, l.text_box.y + 9, 1, 18));
    // A blink repaints the caret's own 18 pixels and nothing else.
    typed.caret = true;
    view.prompt = Some(typed);
    assert_eq!(scene.render(&view), [caret]);
    assert_eq!(changed(&off, scene.canvas().pixels(), 1920), caret);
    assert_eq!(count(scene.canvas(), &caret, assets::TEXT), 18);
    typed.caret = false;
    view.prompt = Some(typed);
    assert_eq!(scene.render(&view), [caret]);
    assert!(scene.canvas().pixels() == off, "the box shows through where it was");
    // The caret follows the dots and stops with them at the cap.
    assert_eq!(l.caret_rect(MAX_DOTS + 5, 0), l.caret_rect(MAX_DOTS, 0));
    assert!(l.caret_rect(MAX_DOTS, 0).right() <= l.text_box.right() - 12);
    assert_eq!(l.caret_rect(2, -4).x, l.caret_rect(2, 0).x - 4);
}

#[test]
fn a_rejected_passphrase_shows_a_short_error_and_shakes_the_box() {
    assert_eq!(error_line(REJECTED), SHORT_ERROR);
    assert_eq!(error_line("Incorrect passphrase"), SHORT_ERROR);
    assert_eq!(error_line("No key available with this passphrase."), SHORT_ERROR);
    let other = "cryptsetup: ERROR: sda3_crypt: maximum number of tries exceeded";
    assert_eq!(error_line(other), other);
    assert_eq!(error_line(""), "");

    // The swing starts and ends at rest, goes both ways and dies away.
    assert_eq!(shake_offset(Duration::ZERO), 0.0);
    assert_eq!(shake_offset(SHAKE), 0.0);
    assert_eq!(shake_offset(SHAKE * 3), 0.0);
    let swing: Vec<f32> = (0..40).map(|ms| shake_offset(Duration::from_millis(ms * 10))).collect();
    let far_right = swing.iter().copied().fold(0.0, f32::max);
    let far_left = swing.iter().copied().fold(0.0, f32::min);
    assert!((5.0..=SHAKE_REACH).contains(&far_right), "{far_right}");
    assert!((-SHAKE_REACH..-4.0).contains(&far_left), "{far_left}");
    assert!(swing[30..].iter().all(|x| x.abs() < 1.0), "{swing:?}");

    let mut scene = Scene::new(1366, 768);
    let mut view = prompt(UNLOCK);
    scene.render(&view);
    let l = scene.layout().clone();

    // The error takes the line under the box; the hint moves down.
    view.message = Some((REJECTED, Tone::Error));
    scene.render(&view);
    assert!(tinted(&scene, &l.prompt_line, assets::DANGER) > 150, "the error line");
    assert!(ink(&scene, &l.prompt_second_line) > 150, "the hint, one line down");
    assert_eq!(tinted(&scene, &l.prompt_second_line, assets::DANGER), 0);
    if assets::TYPEFACE == "Inter" {
        // Short: no wider than the box above it.
        let wide = Rect::new(l.prompt_line.x, l.prompt_line.y, l.text_box.x - l.prompt_line.x, l.prompt_line.h);
        assert!(blank(&scene, &wide));
    }

    // The box moves sideways, as a whole, inside the field's rectangle.
    let with_error = scene.canvas().pixels().to_vec();
    let accent = count(scene.canvas(), &l.field, assets::ACCENT);
    let mut shaken = PromptView { shake: 6.0, ..PromptView::new(UNLOCK) };
    view.prompt = Some(shaken);
    assert_eq!(scene.render(&view), [l.field]);
    let (b, row) = (l.text_box, l.text_box.bottom() as u32 - 1);
    let canvas = scene.canvas();
    assert_ne!(canvas.pixel(b.x as u32 + 8, row), assets::ACCENT, "the left end moved right");
    assert_eq!(canvas.pixel(b.x as u32 + 12, row), assets::ACCENT);
    assert_eq!(canvas.pixel(b.right() as u32, row), assets::ACCENT);
    assert_eq!(canvas.pixel(b.x as u32 + 12 + 6, b.y as u32 + 18), assets::TEXT, "the caret moved with it");
    assert_eq!(count(canvas, &l.field, assets::ACCENT), accent);
    // To the left as far as it goes, and never outside its rectangle.
    shaken.shake = -100.0;
    view.prompt = Some(shaken);
    scene.render(&view);
    assert_eq!(scene.canvas().pixel(b.x as u32 - 8 + 5, row), assets::ACCENT);
    assert!(blank(&scene, &Rect::new(l.field.x, l.field.y, b.x - 8 - l.field.x, l.field.h)));
    // Back at rest the picture is what it was.
    shaken.shake = 0.0;
    view.prompt = Some(shaken);
    scene.render(&view);
    assert!(scene.canvas().pixels() == with_error);
}

#[test]
fn caps_lock_replaces_the_hint_with_a_warning() {
    let mut scene = Scene::new(1366, 768);
    let mut view = prompt(UNLOCK);
    scene.render(&view);
    let l = scene.layout().clone();
    assert_eq!(tinted(&scene, &l.prompt_line, assets::WARNING), 0);
    view.prompt = Some(PromptView { caps_lock: true, ..PromptView::new(UNLOCK) });
    let damage = scene.render(&view).to_vec();
    assert_eq!(damage, [l.prompt_line], "only the hint line is repainted");
    assert!(tinted(&scene, &l.prompt_line, assets::WARNING) > 100, "the Caps Lock warning");
    // With an error as well, both are shown: the error first.
    view.message = Some((REJECTED, Tone::Error));
    scene.render(&view);
    assert!(tinted(&scene, &l.prompt_line, assets::DANGER) > 150);
    assert_eq!(tinted(&scene, &l.prompt_line, assets::WARNING), 0);
    assert!(tinted(&scene, &l.prompt_second_line, assets::WARNING) > 100);
}

#[test]
fn shutdown_and_restart_show_the_mark_the_ring_and_a_line() {
    let mut inks = Vec::new();
    for mode in [Mode::Shutdown, Mode::Reboot] {
        let mut scene = Scene::new(1920, 1080);
        scene.render(&View::new(mode));
        let l = scene.layout().clone();
        assert!(ink(&scene, &l.status) > 300, "{mode:?}");
        assert!(count(scene.canvas(), &l.ring, assets::TEXT) > 40, "{mode:?}");
        assert!(ink(&scene, &l.mark) > (l.mark.area() / 4) as usize, "{mode:?}");
        // Nothing else is drawn.
        let drawn = ink(&scene, &l.status) + ink(&scene, &l.ring) + ink(&scene, &l.mark);
        assert_eq!(ink(&scene, &scene.canvas().bounds()), drawn, "{mode:?}");
        inks.push(ink(&scene, &l.status));
    }
    assert_ne!(inks[0], inks[1], "the two screens say different things");
    // Which items a screen is made of; a prompt replaces the ring and the
    // status line in any mode.
    let l = Layout::new(1920, 1080);
    assert_eq!(slots(&l, &View::new(Mode::Boot)), [SLOT_GLOW, SLOT_MARK, SLOT_RING]);
    assert_eq!(slots(&l, &View::new(Mode::Shutdown)), [SLOT_GLOW, SLOT_MARK, SLOT_RING, SLOT_STATUS]);
    assert_eq!(slots(&l, &View::new(Mode::Reboot)), [SLOT_GLOW, SLOT_MARK, SLOT_RING, SLOT_STATUS]);
    let asking = View::new(Mode::Shutdown).with_prompt(PromptView::new(UNLOCK));
    assert_eq!(slots(&l, &asking), [SLOT_GLOW, SLOT_MARK, SLOT_TITLE, SLOT_DETAIL, SLOT_FIELD, SLOT_CARET, SLOT_HINT]);
    // The fade-in: everything appears together.
    let opening = with_motion(Mode::Shutdown, motion::fade_in(0.0));
    assert!(slots(&l, &opening).is_empty(), "nothing at first");
    let half = with_motion(Mode::Shutdown, motion::fade_in(motion::FADE_IN / 3.0));
    assert_eq!(slots(&l, &half), [SLOT_GLOW, SLOT_MARK, SLOT_RING, SLOT_STATUS]);
}

#[test]
fn messages_and_progress_are_one_line_under_the_ring() {
    let mut scene = Scene::new(1920, 1080);
    let mut view = View::new(Mode::Boot);
    scene.render(&view);
    let l = scene.layout().clone();
    assert!(blank(&scene, &l.boot_message), "no line until the system sends one");
    view.message = Some(("cryptsetup: nvme0n1p3_crypt: set up successfully", Tone::Info));
    assert_eq!(scene.render(&view), [l.boot_message]);
    let plain = ink(&scene, &l.boot_message);
    assert!(plain > 400, "the message is drawn: {plain}");
    assert_eq!(tinted(&scene, &l.boot_message, assets::DANGER), 0);
    view.message = Some(("cryptsetup: ERROR: sda3_crypt: maximum number of tries exceeded", Tone::Error));
    scene.render(&view);
    assert!(tinted(&scene, &l.boot_message, assets::DANGER) > 250, "an error is in the danger colour");
    // Progress is a percentage: on its own, or after the message.
    view.message = None;
    view.progress = Some(40);
    let damage = scene.render(&view).to_vec();
    assert_eq!(damage.len(), 2, "the line and the ring: {damage:?}");
    assert!(damage.contains(&l.boot_message) && damage.contains(&l.ring));
    let alone = ink(&scene, &l.boot_message);
    assert!(alone > 30 && alone < plain / 4, "{alone}");
    view.message = Some(("cryptsetup: nvme0n1p3_crypt: set up successfully", Tone::Info));
    scene.render(&view);
    assert!(ink(&scene, &l.boot_message) > plain + alone, "the message and the percentage");
    // A message of spaces is no message.
    view.progress = None;
    view.message = Some(("   ", Tone::Info));
    scene.render(&view);
    assert!(blank(&scene, &l.boot_message));
    // On the shutdown screen the line is under the status line.
    let mut view = View::new(Mode::Shutdown);
    view.message = Some(("Stopping services", Tone::Info));
    scene.render(&view);
    assert!(ink(&scene, &l.busy_message) > 150);
}

#[test]
fn text_is_centred_on_the_axis_of_the_screen() {
    for (w, h) in [(1366u32, 768u32), (1920, 1080), (3840, 2160), (1080, 1920)] {
        let mut scene = Scene::new(w, h);
        let mut view = View::new(Mode::Shutdown);
        view.message = Some(("Stopping services", Tone::Info));
        scene.render(&view);
        let l = scene.layout().clone();
        for line in [l.status, l.busy_message] {
            let mut drawn = Rect::default();
            for y in line.y..line.bottom() {
                for x in line.x..line.right() {
                    if scene.canvas().pixel(x as u32, y as u32) != scene.backdrop().pixel(x, y) {
                        drawn = drawn.union(&Rect::new(x, y, 1, 1));
                    }
                }
            }
            let centre = drawn.x as f32 + drawn.w as f32 / 2.0;
            assert!((centre - w as f32 / 2.0).abs() <= 2.0 * l.unit, "{w}x{h}: text centred at {centre}");
            assert!(drawn.y > line.y && drawn.bottom() < line.bottom(), "{w}x{h}: {drawn:?} inside {line:?}");
        }
    }
}

#[test]
fn hostile_text_is_contained() {
    let mut scene = Scene::new(1024, 768);
    let nasty = "\u{1b}[2J\n\r\t\u{0}".repeat(500) + &"W".repeat(5000);
    let mut view = View::new(Mode::Boot);
    view.message = Some((&nasty, Tone::Info));
    assert!(!scene.render(&view).is_empty());
    view = view.with_prompt(PromptView { dots: usize::MAX, shake: f32::NAN, ..PromptView::new(&nasty) });
    view.progress = Some(255);
    assert!(!scene.render(&view).is_empty());
    view.message = Some((&nasty, Tone::Error));
    scene.render(&view);
    // Long text is cut to its line, never drawn beyond it.
    let l = scene.layout().clone();
    assert!(ink(&scene, &l.detail) > 1000);
    for line in [l.detail, l.prompt_line] {
        let left = Rect::new(0, line.y, line.x, line.h);
        let right = Rect::new(line.right(), line.y, 1024 - line.right(), line.h);
        assert!(blank(&scene, &left) && blank(&scene, &right));
    }
    assert_eq!(clean("a\nb\tc"), "a b c");
    assert_eq!(clean("  \u{7}padded  "), "padded");
    assert!(clean(&"x".repeat(1000)).len() <= 240);
}

#[test]
fn mode_names() {
    assert_eq!(Mode::parse("boot"), Some(Mode::Boot));
    assert_eq!(Mode::parse("shutdown"), Some(Mode::Shutdown));
    assert_eq!(Mode::parse("reboot"), Some(Mode::Reboot));
    assert_eq!(Mode::parse("updates"), None);
}
