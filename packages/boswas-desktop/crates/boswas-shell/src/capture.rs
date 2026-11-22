//! Screen capture: a frozen picture of the screen on which the user picks
//! an area (or confirms the whole screen). The result is saved to the
//! Pictures folder and copied to the clipboard.

use std::path::{Path, PathBuf};

use boswas_ui::Rgba;
use tiny_skia::{Pixmap, Transform};

use crate::{
    action::Action,
    ui::{ramp, Frame, Rect},
};

#[derive(Clone, Debug, PartialEq)]
pub enum Act {
    /// The picture itself: dragging on it selects an area.
    Surface,
}

/// Smallest selection (logical pixels) that counts as one.
const MIN_SELECTION: f32 = 4.0;

pub struct Capture {
    /// The screen as it was when the capture started (device pixels).
    pub shot: Pixmap,
    /// The same picture, darkened: what is outside the selection.
    dimmed: Pixmap,
    /// Capture the whole screen instead of an area.
    pub whole: bool,
    /// Where the drag started and where the pointer is, in logical pixels.
    pub anchor: Option<(f32, f32)>,
    pub pointer: Option<(f32, f32)>,
    pub dragging: bool,
}

impl Capture {
    pub fn new(shot: Pixmap, whole: bool) -> Self {
        let mut dimmed = shot.clone();
        for pixel in dimmed.data_mut().chunks_exact_mut(4) {
            for c in &mut pixel[..3] {
                *c = (u16::from(*c) * 115 / 255) as u8;
            }
        }
        Self {
            shot,
            dimmed,
            whole,
            anchor: None,
            pointer: None,
            dragging: false,
        }
    }

    /// The selected area in logical pixels of a surface of `size`.
    pub fn selection(&self, size: (f32, f32)) -> Option<Rect> {
        if self.whole {
            return Some(Rect::new(0.0, 0.0, size.0, size.1));
        }
        let (a, b) = (self.anchor?, self.pointer?);
        let clamp = |p: (f32, f32)| (p.0.clamp(0.0, size.0), p.1.clamp(0.0, size.1));
        let (a, b) = (clamp(a), clamp(b));
        let rect = Rect::new(a.0.min(b.0), a.1.min(b.1), (a.0 - b.0).abs(), (a.1 - b.1).abs());
        (rect.w >= MIN_SELECTION && rect.h >= MIN_SELECTION).then_some(rect)
    }

    /// The selected part of the picture, at the picture's own resolution.
    pub fn crop(&self, size: (f32, f32)) -> Option<Pixmap> {
        let selection = self.selection(size)?;
        let (sx, sy) = (self.shot.width() as f32 / size.0, self.shot.height() as f32 / size.1);
        let x0 = ((selection.x * sx).round() as u32).min(self.shot.width() - 1);
        let y0 = ((selection.y * sy).round() as u32).min(self.shot.height() - 1);
        let w = ((selection.w * sx).round() as u32).clamp(1, self.shot.width() - x0);
        let h = ((selection.h * sy).round() as u32).clamp(1, self.shot.height() - y0);
        let mut out = Pixmap::new(w, h)?;
        let stride = self.shot.width() as usize * 4;
        for (row, target) in out.data_mut().chunks_exact_mut(w as usize * 4).enumerate() {
            let start = (y0 as usize + row) * stride + x0 as usize * 4;
            target.copy_from_slice(&self.shot.data()[start..start + w as usize * 4]);
        }
        Some(out)
    }
}

/// An XDG user folder (`PICTURES`, `DOCUMENTS`, ...) from `user-dirs.dirs`,
/// else `default` in the home folder.
fn xdg_user_dir(home: &Path, user_dirs: Option<&str>, key: &str, default: &str) -> PathBuf {
    let prefix = format!("XDG_{key}_DIR=");
    user_dirs
        .and_then(|text| {
            text.lines().find_map(|line| {
                let value = line.trim().strip_prefix(prefix.as_str())?.trim().trim_matches('"');
                let path = match value.strip_prefix("$HOME/") {
                    Some(rest) => home.join(rest),
                    None => PathBuf::from(value),
                };
                (path.is_absolute() && path != home && !value.contains("..")).then_some(path)
            })
        })
        .unwrap_or_else(|| home.join(default))
}

/// The folder screenshots are saved in: `Screenshots` under the user's
/// Pictures folder (from `user-dirs.dirs`, else `~/Pictures`).
pub fn screenshots_dir(home: &Path, user_dirs: Option<&str>) -> PathBuf {
    xdg_user_dir(home, user_dirs, "PICTURES", "Pictures").join("Screenshots")
}

/// The user's folder behind one of Start's folder buttons (`None` for the
/// two that are programs).
pub fn user_dir(home: &Path, user_dirs: Option<&str>, folder: crate::settings::Folder) -> Option<PathBuf> {
    let key = folder.xdg_name()?;
    Some(xdg_user_dir(home, user_dirs, key, folder.label()))
}

/// A file name for a screenshot taken at `time` that does not exist yet
/// in `dir` according to `exists`.
pub fn file_name(time: &str, exists: impl Fn(&str) -> bool) -> String {
    let base = format!("Screenshot {time}");
    let mut name = format!("{base}.png");
    let mut n = 2;
    while exists(&name) && n < 1000 {
        name = format!("{base} ({n}).png");
        n += 1;
    }
    name
}

pub fn draw(f: &mut Frame, capture: &Capture) {
    let (w, h) = (f.canvas.width(), f.canvas.height());
    let whole = Rect::new(0.0, 0.0, w, h);
    f.area(whole, Action::Capture(Act::Surface));
    f.drag();
    let placement = Transform::from_scale(w / capture.shot.width() as f32, h / capture.shot.height() as f32);
    f.canvas.draw_pixmap(&capture.dimmed, 0.0, 0.0, w, h, 1.0);
    let selection = capture.selection((w, h));
    if let Some(s) = selection {
        f.canvas.fill_rounded_rect_pixmap(s.x, s.y, s.w, s.h, [0.0; 4], &capture.shot, placement);
        f.canvas.stroke_rounded_rect(s.x - 1.0, s.y - 1.0, s.w + 2.0, s.h + 2.0, 0.0, 1.0, Rgba::WHITE);
        if !capture.whole {
            let scale = capture.shot.width() as f32 / w;
            let label = format!("{} × {}", (s.w * scale).round() as u32, (s.h * scale).round() as u32);
            let style = ramp("caption", Rgba::WHITE);
            let width = f.res.text.measure(&label, &style).0 + 16.0;
            let y = if s.y >= 30.0 { s.y - 28.0 } else { s.y + 6.0 };
            let tag = Rect::new(s.x.min(w - width - 4.0).max(4.0), y, width, 22.0);
            f.canvas.fill_rounded_rect(tag.x, tag.y, tag.w, tag.h, 4.0, Rgba::BLACK.alpha(0.72));
            f.text_centered(&label, tag, &style);
        }
    }
    let hint = match (capture.whole, selection.is_some(), capture.dragging) {
        (true, _, _) => "Click or press Enter to capture this screen · Esc to cancel",
        (false, true, false) => "Press Enter to capture the selection · drag to change it · Esc to cancel",
        _ => "Drag to select an area · Esc to cancel",
    };
    let style = ramp("body", Rgba::WHITE);
    let width = (f.res.text.measure(hint, &style).0 + 32.0).min(w - 16.0);
    let pill = Rect::new((w - width) / 2.0, 24.0, width, 36.0);
    f.canvas.fill_rounded_rect(pill.x, pill.y, pill.w, pill.h, 18.0, Rgba::BLACK.alpha(0.72));
    f.text_centered(hint, pill.inset(8.0), &style);
}

#[cfg(test)]
mod tests {
    use super::*;

    fn shot() -> Pixmap {
        // 200 x 100 device pixels; each pixel's red is its column, green its row.
        let mut p = Pixmap::new(200, 100).unwrap();
        for (i, px) in p.data_mut().chunks_exact_mut(4).enumerate() {
            px.copy_from_slice(&[(i % 200) as u8, (i / 200) as u8, 0, 255]);
        }
        p
    }

    #[test]
    fn a_drag_selects_a_rectangle_in_any_direction() {
        let mut capture = Capture::new(shot(), false);
        assert_eq!(capture.selection((200.0, 100.0)), None);
        capture.anchor = Some((150.0, 80.0));
        capture.pointer = Some((50.0, 20.0));
        assert_eq!(capture.selection((200.0, 100.0)), Some(Rect::new(50.0, 20.0, 100.0, 60.0)));
        capture.pointer = Some((500.0, -40.0));
        assert_eq!(capture.selection((200.0, 100.0)), Some(Rect::new(150.0, 0.0, 50.0, 80.0)), "clamped to the screen");
        capture.pointer = Some((151.0, 81.0));
        assert_eq!(capture.selection((200.0, 100.0)), None, "a click is not a selection");
        let whole = Capture::new(shot(), true);
        assert_eq!(whole.selection((200.0, 100.0)), Some(Rect::new(0.0, 0.0, 200.0, 100.0)));
    }

    #[test]
    fn the_crop_is_taken_from_the_picture_at_its_own_resolution() {
        let mut capture = Capture::new(shot(), false);
        capture.anchor = Some((10.0, 5.0));
        capture.pointer = Some((30.0, 25.0));
        // The surface is 100 x 50 logical pixels: the picture is at scale 2.
        let crop = capture.crop((100.0, 50.0)).unwrap();
        assert_eq!((crop.width(), crop.height()), (40, 40));
        let first = crop.pixel(0, 0).unwrap();
        assert_eq!((first.red(), first.green()), (20, 10));
        let last = crop.pixel(39, 39).unwrap();
        assert_eq!((last.red(), last.green()), (59, 49));
        let all = Capture::new(shot(), true).crop((100.0, 50.0)).unwrap();
        assert_eq!((all.width(), all.height()), (200, 100));
        // The darkened copy is darker and keeps the original untouched.
        assert!(capture.dimmed.pixel(100, 50).unwrap().red() < capture.shot.pixel(100, 50).unwrap().red());
    }

    #[test]
    fn screenshots_go_to_the_pictures_folder() {
        let home = Path::new("/home/asha");
        assert_eq!(screenshots_dir(home, None), Path::new("/home/asha/Pictures/Screenshots"));
        let dirs = "# comment\nXDG_DESKTOP_DIR=\"$HOME/Desktop\"\nXDG_PICTURES_DIR=\"$HOME/Bilder\"\n";
        assert_eq!(screenshots_dir(home, Some(dirs)), Path::new("/home/asha/Bilder/Screenshots"));
        assert_eq!(screenshots_dir(home, Some("XDG_PICTURES_DIR=\"/data/pics\"\n")), Path::new("/data/pics/Screenshots"));
        for bad in ["XDG_PICTURES_DIR=\"relative\"\n", "XDG_PICTURES_DIR=\"$HOME/\"\n", "XDG_PICTURES_DIR=\"$HOME/../other\"\n"] {
            assert_eq!(screenshots_dir(home, Some(bad)), Path::new("/home/asha/Pictures/Screenshots"), "{bad}");
        }
    }

    #[test]
    fn start_folders_are_the_users_folders() {
        use crate::settings::Folder;
        let home = Path::new("/home/asha");
        let dirs = "XDG_DOWNLOAD_DIR=\"$HOME/Herunterladen\"\nXDG_MUSIC_DIR=\"/data/music\"\n";
        assert_eq!(user_dir(home, Some(dirs), Folder::Downloads).unwrap(), Path::new("/home/asha/Herunterladen"));
        assert_eq!(user_dir(home, Some(dirs), Folder::Music).unwrap(), Path::new("/data/music"));
        assert_eq!(user_dir(home, Some(dirs), Folder::Documents).unwrap(), Path::new("/home/asha/Documents"));
        assert_eq!(user_dir(home, None, Folder::Videos).unwrap(), Path::new("/home/asha/Videos"));
        assert_eq!(user_dir(home, None, Folder::Settings), None);
        assert_eq!(user_dir(home, None, Folder::Files), None);
    }

    #[test]
    fn file_names_do_not_collide() {
        assert_eq!(file_name("2026-11-15 163812", |_| false), "Screenshot 2026-11-15 163812.png");
        let taken = ["Screenshot t.png", "Screenshot t (2).png"];
        assert_eq!(file_name("t", |name| taken.contains(&name)), "Screenshot t (3).png");
    }
}
