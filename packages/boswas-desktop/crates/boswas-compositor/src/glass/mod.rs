//! Glass in the compositor: whether it is on, live blur behind translucent
//! surfaces, the background effect protocol, and Mica for the compositor's
//! own surfaces.
//!
//! * **On or off.** Glass is on unless `[appearance] transparency = false`
//!   or the user's desktop preset is a high-contrast one (the preset
//!   `boswas-preset` records in `$XDG_CONFIG_HOME/boswasrc`); both are read
//!   again on `reload-config`, and the shell is told in the `config` event.
//! * **Live blur** (`blur`, `protocol`): on OpenGL ES the compositor blurs
//!   what lies behind each surface with a blur region, and offers the blur
//!   capability while glass is on. pixman does not blur; clients then draw
//!   their own backdrop (the shell, from the wallpaper).
//! * **Mica** for the compositor's own surfaces (window menu, Alt+Tab, snap
//!   layouts, Task View, title bars): the wallpaper, read back small from
//!   the shell's background surface whenever it changed (the compositor
//!   never decodes a picture file itself), blurred and tinted with the same
//!   code as the shell's (`boswas_ui::material`, `boswas_ui::glass`).

pub mod blur;
pub mod protocol;

use std::{
    collections::HashMap,
    path::PathBuf,
    time::{Duration, Instant},
};

use boswas_ui::{glass as ui_glass, material::Backdrop};
use smithay::{
    backend::renderer::{
        element::{Element, Id},
        utils::CommitCounter,
        Texture,
    },
    desktop::layer_map_for_output,
    output::Output,
    utils::{Logical, Physical, Point, Rectangle, Scale, Size},
    wayland::shell::wlr_layer::Layer,
};
use tiny_skia::{IntSize, Pixmap};

use crate::{
    effects::BoswasRenderer,
    render::{OutputElement, OutputRenderElement},
    state::Boswas,
};

/// Desktop presets whose look is high contrast (as the Qt toolkit's
/// `appearance.HIGH_CONTRAST_PRESETS`).
pub const HIGH_CONTRAST_PRESETS: &[&str] = &["carbon"];
/// Largest preset record that is read.
const MAX_RECORD_BYTES: u64 = 256 * 1024;
/// The wallpaper is read back at most this often.
const MICA_INTERVAL: Duration = Duration::from_millis(750);
/// Width of the wallpaper as it is read back for Mica, in pixels.
const MICA_WIDTH: f64 = 480.0;

/// A blur the compositor asks for behind one of its own surfaces.
pub struct OwnBlur {
    /// The surface's element.
    pub id: Id,
    /// Where it is (output physical pixels).
    pub rect: Rectangle<i32, Physical>,
    /// Its corner radius in physical pixels.
    pub radius: f32,
}

/// What is kept of each blurred surface between frames.
struct Kept {
    /// The blur element's own identity.
    id: Id,
    commit: CommitCounter,
    rects: Vec<Rectangle<i32, Physical>>,
    seen: bool,
}

/// The wallpaper of one output for Mica.
pub struct Mica {
    pub backdrop: Backdrop,
    /// Changes when the picture changed.
    pub generation: u64,
    /// The background surfaces' commits it was read at.
    commits: Vec<(Id, CommitCounter)>,
    read: Instant,
    pixels: Vec<u8>,
}

pub struct GlassState {
    pub effects: protocol::BackgroundEffectState,
    renderer_blurs: bool,
    /// The user's desktop preset is a high-contrast one.
    pub high_contrast: bool,
    /// Blurs asked for by the compositor's own surfaces on the output being
    /// drawn.
    pub own: Vec<OwnBlur>,
    blurred: HashMap<(String, Id), Kept>,
    mica: HashMap<String, Mica>,
    generations: u64,
}

impl GlassState {
    pub fn new(display: &smithay::reexports::wayland_server::DisplayHandle) -> Self {
        Self {
            effects: protocol::BackgroundEffectState::new(display),
            renderer_blurs: false,
            high_contrast: high_contrast_preset(preset_record_path().as_deref()),
            own: Vec::new(),
            blurred: HashMap::new(),
            mica: HashMap::new(),
            generations: 0,
        }
    }
}

/// `$XDG_CONFIG_HOME/boswasrc` (or `~/.config/boswasrc`): where
/// `boswas-preset` records the current preset.
pub fn preset_record_path() -> Option<PathBuf> {
    crate::config::user_config_path().and_then(|path| Some(path.parent()?.parent()?.join("boswasrc")))
}

/// Whether the preset recorded at `path` (`Current` in its `[Preset]`
/// group) is a high-contrast one.
pub fn high_contrast_preset(path: Option<&std::path::Path>) -> bool {
    let Some(path) = path else {
        return false;
    };
    let Some(text) = std::fs::metadata(path)
        .ok()
        .filter(|m| m.is_file() && m.len() <= MAX_RECORD_BYTES)
        .and_then(|_| std::fs::read(path).ok())
    else {
        return false;
    };
    preset_of(&String::from_utf8_lossy(&text)).is_some_and(|preset| HIGH_CONTRAST_PRESETS.contains(&preset.as_str()))
}

/// `Current` of the `[Preset]` group of a preset record.
fn preset_of(text: &str) -> Option<String> {
    let mut group = "";
    let mut found = None;
    for line in text.lines() {
        let line = line.trim();
        if let Some(name) = line.strip_prefix('[').and_then(|l| l.strip_suffix(']')) {
            group = name;
        } else if group == "Preset" {
            if let Some((key, value)) = line.split_once('=') {
                if key.trim() == "Current" {
                    found = Some(value.trim().to_owned());
                }
            }
        }
    }
    found
}

impl Boswas {
    /// Whether glass is on (transparency effects on, no high-contrast
    /// preset): surfaces are translucent.
    pub fn glass_on(&self) -> bool {
        self.config.appearance.transparency && !self.glass.high_contrast
    }

    /// Whether the compositor blurs what lies behind translucent surfaces.
    pub fn live_blur(&self) -> bool {
        self.glass.effects.blur()
    }

    /// The source of the backdrop of the compositor's own glass on an
    /// output: live blur, the output's wallpaper, or none (glass off).
    pub fn glass_source(&mut self, output: &str) -> ui_glass::Source<'_> {
        if !self.glass_on() {
            return ui_glass::Source::Off;
        }
        if self.live_blur() {
            return ui_glass::Source::Live;
        }
        match self.glass.mica.get_mut(output) {
            Some(mica) => ui_glass::Source::Wallpaper(&mut mica.backdrop),
            None => ui_glass::Source::Unknown,
        }
    }

    /// What the compositor's own glass on an output looks like now (for the
    /// keys of kept pictures): on, live, and the wallpaper's generation.
    pub fn glass_key(&self, output: &str) -> (bool, bool, u64) {
        let generation = self.glass.mica.get(output).map_or(0, |m| m.generation);
        (self.glass_on(), self.live_blur(), generation)
    }

    /// The wallpaper backdrop of an output (Mica), when it is known.
    pub fn mica(&mut self, output: &str) -> Option<&mut Backdrop> {
        self.glass.mica.get_mut(output).map(|m| &mut m.backdrop)
    }

    /// The configuration was loaded again: the preset may have changed.
    pub fn glass_config_changed(&mut self) {
        self.glass.high_contrast = high_contrast_preset(preset_record_path().as_deref());
        self.glass_update();
    }

    /// Whether the renderer blurs (learned as it draws).
    fn glass_renderer(&mut self, blurs: bool) {
        if self.glass.renderer_blurs != blurs {
            self.glass.renderer_blurs = blurs;
            self.glass_update();
        }
    }

    fn glass_update(&mut self) {
        let live = self.glass.renderer_blurs && self.glass_on();
        if live != self.glass.effects.blur() {
            self.glass.effects.set_blur(live);
            self.schedule_redraw();
        }
    }

    /// Puts the blurred scene behind every surface with a blur region (and
    /// every own surface that asked) into the elements of an output, right
    /// behind the surface; refreshes the output's Mica. Called at the end of
    /// [`Boswas::output_elements`].
    pub fn blur_behind_surfaces<R>(&mut self, renderer: &mut R, output: &Output, scale: Scale<f64>, elements: &mut Vec<OutputRenderElement<R>>)
    where
        R: BoswasRenderer,
        R::TextureId: Clone + Send + Texture + 'static,
    {
        self.glass_renderer(renderer.blurs());
        let name = output.name();
        self.refresh_mica(renderer, output, &name, elements);
        let own = std::mem::take(&mut self.glass.own);
        for kept in self.glass.blurred.values_mut() {
            kept.seen = false;
        }
        if self.live_blur() {
            self.insert_blurs(renderer, output, &name, scale, elements, own);
        }
        let output_name = name.clone();
        self.glass.blurred.retain(|(name, _), kept| kept.seen || *name != output_name);
    }

    #[allow(clippy::too_many_arguments)]
    fn insert_blurs<R>(&mut self, renderer: &mut R, output: &Output, name: &str, scale: Scale<f64>, elements: &mut Vec<OutputRenderElement<R>>, own: Vec<OwnBlur>)
    where
        R: BoswasRenderer,
        R::TextureId: Clone + Send + Texture + 'static,
    {
        // The surfaces of this output that asked for blur.
        let everything = Size::from((i32::MAX / 4, i32::MAX / 4));
        let mut wanted: HashMap<Id, Vec<Rectangle<i32, Logical>>> = HashMap::new();
        for layer in layer_map_for_output(output).layers() {
            let rects = protocol::blur_region(layer.wl_surface(), everything);
            if !rects.is_empty() {
                wanted.insert(Id::from_wayland_resource(layer.wl_surface()), rects);
            }
        }
        for window in self.space.elements_for_output(output) {
            if let Some(surface) = window.wl_surface() {
                let rects = protocol::blur_region(&surface, everything);
                if !rects.is_empty() {
                    wanted.insert(Id::from_wayland_resource(&surface), rects);
                }
            }
        }
        if wanted.is_empty() && own.is_empty() {
            return;
        }
        let Some(mode) = output.current_mode() else {
            return;
        };
        let screen = Rectangle::from_size(output.current_transform().transform_size(mode.size));
        // (index, the element's identity, region in output pixels, radius)
        let mut requests: Vec<(usize, Id, Vec<Rectangle<i32, Physical>>, f32)> = Vec::new();
        for (index, element) in elements.iter().enumerate() {
            let id = element.id();
            if let Some(own) = own.iter().find(|o| o.id == *id) {
                if let Some(rect) = own.rect.intersection(screen) {
                    requests.push((index, id.clone(), vec![rect], own.radius));
                }
                continue;
            }
            if !matches!(element, OutputElement::Surface(_) | OutputElement::Window(_)) {
                continue;
            }
            let Some(rects) = wanted.get(id) else {
                continue;
            };
            let geometry = element.geometry(scale);
            let physical: Vec<Rectangle<i32, Physical>> = rects
                .iter()
                .filter_map(|r| {
                    let local: Rectangle<i32, Physical> = r.to_f64().to_physical(scale).to_i32_round();
                    Rectangle::new(local.loc + geometry.loc, local.size).intersection(geometry)?.intersection(screen)
                })
                .collect();
            if !physical.is_empty() {
                requests.push((index, id.clone(), physical, 0.0));
            }
        }
        let theme = self.theme;
        let Some(glass) = ui_glass::Glass::of(theme) else {
            return;
        };
        let band = ui_glass::luminosity_band(theme.scheme);
        let params = blur::BlurParams::for_sigma(glass.blur / 2.0 * scale.x as f32, glass.saturation, band);
        let reach = blur::reach(&params);
        // Back to front: a blur behind lies in the scene of one in front.
        requests.sort_by(|a, b| b.0.cmp(&a.0));
        for (index, id, rects, radius) in requests {
            let Some(bbox) = blur::bounds(&rects) else {
                continue;
            };
            let grown = Rectangle::new(bbox.loc - Point::from((reach, reach)), bbox.size + Size::from((2 * reach, 2 * reach)));
            let Some(area) = grown.intersection(screen) else {
                continue;
            };
            let key = (name.to_owned(), id);
            let alpha = elements[index].alpha();
            let Some(blurred) = renderer.blur_behind(&key, area, scale, &elements[index + 1..], &params) else {
                continue;
            };
            let kept = self.glass.blurred.entry(key).or_insert_with(|| Kept {
                id: Id::new(),
                commit: CommitCounter::default(),
                rects: Vec::new(),
                seen: true,
            });
            kept.seen = true;
            let damage = if kept.rects != rects {
                kept.rects = rects.clone();
                kept.commit.increment();
                rects.iter().map(|r| Rectangle::new(r.loc - bbox.loc, r.size)).collect()
            } else if !blurred.changed.is_empty() {
                kept.commit.increment();
                let local: Vec<Rectangle<i32, Physical>> = rects.iter().map(|r| Rectangle::new(r.loc - bbox.loc, r.size)).collect();
                let changed: Vec<Rectangle<i32, Physical>> = blurred.changed.iter().map(|r| Rectangle::new(r.loc - bbox.loc, r.size)).collect();
                blur::expand_damage(&changed, reach, &local)
            } else {
                Vec::new()
            };
            if let Some(element) =
                blur::BlurElement::new(kept.id.clone(), kept.commit, &blurred, &rects, damage, alpha, params.saturation, params.band, radius)
            {
                elements.insert(index + 1, OutputElement::Blur(element));
            }
        }
    }

    /// Reads the output's wallpaper back for Mica when the shell's
    /// background surface changed (at most every [`MICA_INTERVAL`]).
    fn refresh_mica<R>(&mut self, renderer: &mut R, output: &Output, name: &str, elements: &[OutputRenderElement<R>])
    where
        R: BoswasRenderer,
        R::TextureId: Clone + Send + Texture + 'static,
    {
        if self.lock.locked {
            return;
        }
        let now = Instant::now();
        if self.glass.mica.get(name).is_some_and(|m| now.duration_since(m.read) < MICA_INTERVAL) {
            return;
        }
        let ids: Vec<Id> = layer_map_for_output(output)
            .layers_on(Layer::Background)
            .map(|layer| Id::from_wayland_resource(layer.wl_surface()))
            .collect();
        let commits: Vec<(Id, CommitCounter)> =
            elements.iter().filter(|e| ids.contains(e.id())).map(|e| (e.id().clone(), e.current_commit())).collect();
        if commits.is_empty() {
            return;
        }
        if let Some(mica) = self.glass.mica.get_mut(name) {
            mica.read = now;
            if mica.commits == commits {
                return;
            }
        }
        let Some(geo) = self.space.output_geometry(output) else {
            return;
        };
        let (w, h) = (f64::from(geo.size.w.max(1)), f64::from(geo.size.h.max(1)));
        let small = (MICA_WIDTH / w).min(1.0);
        let size = Size::<i32, Physical>::from(((w * small).ceil() as i32, (h * small).ceil() as i32));
        let mut background = Vec::new();
        crate::render::layer_elements(renderer, output, Layer::Background, Scale::from(small), &mut background);
        let Some(pixels) = renderer.read_back(size, Scale::from(small), &background) else {
            return;
        };
        // A change too small to see in a blurred picture (a desktop icon's
        // hover) keeps the generation, so nothing is drawn again for it.
        let same = self.glass.mica.get(name).is_some_and(|m| {
            m.pixels.len() == pixels.len() && {
                let total: u64 = m.pixels.iter().zip(&pixels).map(|(a, b)| u64::from(a.abs_diff(*b))).sum();
                total * 4 < pixels.len() as u64
            }
        });
        if same {
            if let Some(mica) = self.glass.mica.get_mut(name) {
                mica.commits = commits;
            }
            return;
        }
        let Some(picture) = IntSize::from_wh(size.w as u32, size.h as u32).and_then(|s| Pixmap::from_vec(pixels.clone(), s)) else {
            return;
        };
        let Some(backdrop) = Backdrop::new(&picture, w as f32, h as f32) else {
            return;
        };
        self.glass.generations += 1;
        let generation = self.glass.generations;
        self.glass.mica.insert(name.to_owned(), Mica { backdrop, generation, commits, read: now, pixels });
        // What is drawn from it is drawn again.
        self.redraw = true;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_preset_record_decides_high_contrast() {
        assert_eq!(preset_of("[General]\nCurrent=x\n[Preset]\nCurrent = carbon\n").as_deref(), Some("carbon"));
        assert_eq!(preset_of("[Preset]\nOther=1\n"), None);
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("boswasrc");
        std::fs::write(&path, "[Preset]\nCurrent=carbon\n").unwrap();
        assert!(high_contrast_preset(Some(&path)));
        std::fs::write(&path, "[Preset]\nCurrent=horizon\n").unwrap();
        assert!(!high_contrast_preset(Some(&path)));
        assert!(!high_contrast_preset(Some(&dir.path().join("missing"))));
        assert!(!high_contrast_preset(None));
    }
}
