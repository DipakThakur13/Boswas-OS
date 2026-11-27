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
    rc::Rc,
    time::{Duration, Instant},
};

use boswas_ui::{glass as ui_glass, material::Backdrop};
use smithay::{
    backend::{
        allocator::Fourcc,
        renderer::{
            element::{
                memory::{MemoryRenderBuffer, MemoryRenderBufferRenderElement},
                solid::{SolidColorBuffer, SolidColorRenderElement},
                Element, Id, Kind,
            },
            utils::CommitCounter,
            Texture,
        },
    },
    desktop::layer_map_for_output,
    output::Output,
    utils::{Logical, Physical, Point, Rectangle, Scale, Size, Transform},
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
/// How long the desktop behind Task View is kept after Task View last
/// faded (as long as the shell keeps its spare canvases): about 30 MB at
/// 2560 x 1440 with pixman's copy.
const DARKENED_KEPT: Duration = Duration::from_secs(180);

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
    /// The desktop behind Task View, per output.
    desktops: HashMap<String, Desktop>,
    /// The Mica of each window's title bar: what it was made for
    /// (position on the output, wallpaper generation) and the picture.
    titlebars: HashMap<u64, MicaBar>,
}

/// What the desktop behind Task View was made for: the wallpaper's
/// generation, the output's size in pixels, the darkening and the theme.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct DesktopKey {
    generation: u64,
    pixels: (i32, i32),
    dim: u32,
    theme: usize,
}

/// The desktop behind Task View on one output.
struct Desktop {
    key: DesktopKey,
    /// Blurred, at half the output's size.
    blurred: MemoryRenderBuffer,
    /// The same picture's pixels (ARGB), until a worker thread takes them
    /// to make the darkened picture from.
    pixels: Option<(Vec<u8>, (i32, i32))>,
    /// Blurred, enlarged and darkened, at the output's size (`None`: not
    /// made yet; `Some(None)`: it could not be made).
    darkened: Option<Option<MemoryRenderBuffer>>,
    /// When Task View last drew the darkened picture (or it came).
    used: Instant,
}

/// The desktop behind Task View, as [`Boswas::glass_desktop`] gives it.
pub enum GlassDesktop<R: BoswasRenderer>
where
    R::TextureId: Texture + 'static,
{
    /// Blurred and darkened in one picture, at the opacity Task View has
    /// reached on its way in or out.
    Darkened(MemoryRenderBufferRenderElement<R>),
    /// Only blurred, for Task View at rest (or while the darkened picture
    /// is not there): the darkening is drawn over it.
    Blurred(MemoryRenderBufferRenderElement<R>),
}

/// A title bar's Mica.
pub type MicaBar = ((i32, i32, i32, u64), Rc<Pixmap>);

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
            desktops: HashMap::new(),
            titlebars: HashMap::new(),
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
            tracing::info!("live blur behind translucent surfaces {}", if live { "on" } else { "off" });
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

    /// The desktop behind Task View, filling the output, blurred into
    /// glass, with `progress` how far Task View is open (its opacity).
    /// `None` with glass off or before the wallpaper is known.
    ///
    /// On its way in or out it comes darkened too (`dim`: the opacity of
    /// the black over it), in one picture: each translucent layer over the
    /// whole output costs a blend of all its pixels on every frame, which
    /// without a GPU is most of a frame at 144 Hz, and the blurred desktop
    /// with the darkening over it were two. pixman draws that picture (the
    /// blurred desktop enlarged, the black over it) on a worker thread,
    /// once per wallpaper and output size, and it is kept for the next
    /// opening; until it is there, the two layers. At rest Task View draws
    /// the two layers as before, on either renderer: pixman's enlargement
    /// leaves the outermost pixels translucent, and there one layer rounds
    /// differently from two by a level.
    pub fn glass_desktop<R>(&mut self, renderer: &mut R, output: &Output, scale: Scale<f64>, dim: f32, progress: f32) -> Option<GlassDesktop<R>>
    where
        R: BoswasRenderer,
        R::TextureId: Clone + Send + Texture + 'static,
    {
        if !self.glass_on() {
            return None;
        }
        let name = output.name();
        let glass = ui_glass::Glass::of(self.theme)?;
        let geo = self.space.output_geometry(output)?;
        let generation = self.glass.mica.get(&name)?.generation;
        let pixels = geo.size.to_physical_precise_round(scale);
        let key = DesktopKey {
            generation,
            pixels: (pixels.w, pixels.h),
            dim: dim.to_bits(),
            theme: std::ptr::from_ref(self.theme) as usize,
        };
        if self.glass.desktops.get(&name).map(|d| d.key) != Some(key) {
            // At half the output's size with the grain on it, so that the
            // wide soft gradients do not band when enlarged.
            let (w, h) = ((geo.size.w as f32 / 2.0).ceil().max(1.0), (geo.size.h as f32 / 2.0).ceil().max(1.0));
            let picture = self.glass.mica.get_mut(&name)?.backdrop.picture(glass.blur, glass.saturation).clone();
            let mut canvas = boswas_ui::Canvas::new(w, h, 1.0)?;
            canvas.draw_pixmap(&picture, 0.0, 0.0, w, h, 1.0);
            if let Some(tile) = ui_glass::grain(glass.noise) {
                ui_glass::draw_grain(&mut canvas, 0.0, 0.0, w, h, [0.0; 4], &tile);
            }
            let (bytes, size) = argb_pixels(&canvas);
            let blurred = MemoryRenderBuffer::from_slice(&bytes, Fourcc::Argb8888, size, 1, Transform::Normal, None);
            self.glass.desktops.insert(name.clone(), Desktop { key, blurred, pixels: Some((bytes, size)), darkened: None, used: Instant::now() });
        }
        let desktop = self.glass.desktops.get_mut(&name)?;
        if progress < 1.0 {
            // Made on a worker thread (30 to 40 ms at 2560 x 1440 without a
            // GPU); until it is there, the two layers.
            if let Some(half) = desktop.pixels.take() {
                let job = DarkenJob { half, size: geo.size, pixels, scale, dim };
                self.make_darkened(name.clone(), key, job);
            }
            let desktop = self.glass.desktops.get_mut(&name)?;
            if let Some(Some(buffer)) = &desktop.darkened {
                let element = MemoryRenderBufferRenderElement::from_buffer(renderer, (0.0, 0.0), buffer, Some(progress), Some(whole(pixels)), Some(geo.size), Kind::Unspecified);
                desktop.used = Instant::now();
                return element.ok().map(GlassDesktop::Darkened);
            }
        }
        // The picture, enlarged to the output (filtered linearly).
        let desktop = self.glass.desktops.get(&name)?;
        let src = half_of(geo.size);
        MemoryRenderBufferRenderElement::from_buffer(renderer, (0.0, 0.0), &desktop.blurred, Some(progress), Some(src), Some(geo.size), Kind::Unspecified)
            .ok()
            .map(GlassDesktop::Blurred)
    }

    /// Makes the darkened desktop of output `name` on a worker thread and
    /// keeps it when it comes, if it is still wanted (`key`).
    fn make_darkened(&mut self, name: String, key: DesktopKey, job: DarkenJob) {
        let (sender, receiver) = smithay::reexports::calloop::channel::channel::<Option<MemoryRenderBuffer>>();
        let keep = move |state: &mut Boswas, made: Option<MemoryRenderBuffer>| {
            if let Some(desktop) = state.glass.desktops.get_mut(&name).filter(|d| d.key == key && d.darkened.is_none()) {
                if made.is_none() {
                    tracing::debug!("task view: the darkened desktop could not be made");
                }
                let kept = made.is_some();
                desktop.darkened = Some(made);
                desktop.used = Instant::now();
                if kept {
                    state.release_darkened_later(name.clone(), key);
                }
            }
        };
        let inserted = self.loop_handle.insert_source(receiver, move |event, _, state| match event {
            smithay::reexports::calloop::channel::Event::Msg(made) => keep(state, made),
            // The thread ended without one: the two layers stay.
            smithay::reexports::calloop::channel::Event::Closed => keep(state, None),
        });
        let spawned = inserted.is_ok()
            && std::thread::Builder::new()
                .name("task-view".into())
                .spawn(move || {
                    let _ = sender.send(darkened(&job));
                })
                .is_ok();
        if !spawned {
            tracing::debug!("task view: no thread for the darkened desktop");
        }
    }

    /// Lets go of the desktop behind Task View on output `name` once Task
    /// View is closed and has not faded for [`DARKENED_KEPT`]; the next
    /// opening makes it again.
    fn release_darkened_later(&mut self, name: String, key: DesktopKey) {
        use smithay::reexports::calloop::timer::{TimeoutAction, Timer};
        let checked = self.loop_handle.insert_source(Timer::from_duration(DARKENED_KEPT), move |_, _, state| {
            let Some(desktop) = state.glass.desktops.get(&name).filter(|d| d.key == key) else {
                return TimeoutAction::Drop;
            };
            match release_after(desktop.used.elapsed(), state.overview.is_some()) {
                Some(wait) => TimeoutAction::ToDuration(wait),
                None => {
                    state.glass.desktops.remove(&name);
                    tracing::debug!(target: "boswas_compositor::stats", "task view: let go of the darkened desktop of {name}");
                    TimeoutAction::Drop
                }
            }
        });
        if checked.is_err() {
            tracing::debug!("task view: the darkened desktop is kept (no timer)");
        }
    }

    /// The Mica of the active window's title bar: the wallpaper under it,
    /// blurred and covered with the `mica` material's tint (as Windows'
    /// Mica, it does not show the windows behind). Made again when the
    /// window comes to rest somewhere else or the wallpaper changed; while
    /// it is dragged it keeps the picture it had.
    pub fn titlebar_mica(&mut self, window: &crate::shell::element::WindowElement, scale: f64) -> Option<MicaBar> {
        if !self.glass_on() || window.titlebar_height() == 0 {
            return None;
        }
        let id = window.id();
        if self.pointer.is_grabbed() {
            if let Some(kept) = self.glass.titlebars.get(&id) {
                return Some(kept.clone());
            }
        }
        let rect = self.window_rect(window)?;
        let output = self.output_for_rect(rect)?;
        let geo = self.space.output_geometry(&output)?;
        let frame = window.frame().size;
        let origin = (rect.loc.x - geo.loc.x, rect.loc.y - geo.loc.y);
        let name = output.name();
        let generation = self.glass.mica.get(&name)?.generation;
        let key = (origin.0, origin.1, frame.w, generation);
        if let Some(kept) = self.glass.titlebars.get(&id).filter(|(k, _)| *k == key) {
            return Some(kept.clone());
        }
        let theme = self.theme;
        let glass = ui_glass::Glass::of(theme)?;
        let mica = boswas_ui::tokens().material(theme.scheme, "mica");
        let radius = if crate::shell::decoration::is_rounded(window.data().tile.get()) { boswas_ui::tokens().radius_window } else { 0.0 };
        let (w, h) = (frame.w as f32, crate::shell::decoration::TITLEBAR_HEIGHT as f32);
        let mut canvas = boswas_ui::Canvas::new(w, h, scale.ceil().max(1.0) as f32)?;
        let recipe = boswas_ui::material::Recipe {
            blur: mica.blur,
            saturation: glass.saturation,
            layers: [(mica.tint, mica.tint_opacity), (boswas_ui::Rgba::TRANSPARENT, 0.0)],
            noise: glass.noise,
        };
        let backdrop = &mut self.glass.mica.get_mut(&name)?.backdrop;
        backdrop.fill_recipe(&mut canvas, 0.0, 0.0, w, h, [radius, radius, 0.0, 0.0], (origin.0 as f32, origin.1 as f32), &recipe);
        let bar: MicaBar = (key, Rc::new(canvas.into_pixmap()));
        self.glass.titlebars.retain(|other, _| *other == id);
        self.glass.titlebars.insert(id, bar.clone());
        Some(bar)
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


/// `canvas`'s pixels in ARGB order, the output's (pixman enlarges a
/// picture in that order on its fast path; in RGBA order, the canvas's
/// own, it took its generic path: about 55 ms a frame of Task View's
/// opening at 2560 x 1440 without a GPU, against 15 to 20 ms in ARGB
/// order, the same pixels), and their size.
fn argb_pixels(canvas: &boswas_ui::Canvas) -> (Vec<u8>, (i32, i32)) {
    let (pw, ph) = canvas.pixel_size();
    let mut bytes = vec![0u8; pw as usize * ph as usize * 4];
    canvas.write_argb8888(&mut bytes);
    (bytes, (pw as i32, ph as i32))
}

/// `canvas` as a buffer in ARGB order (see [`argb_pixels`]).
#[cfg(test)]
fn output_order(canvas: &boswas_ui::Canvas) -> MemoryRenderBuffer {
    let (bytes, size) = argb_pixels(canvas);
    MemoryRenderBuffer::from_slice(&bytes, Fourcc::Argb8888, size, 1, Transform::Normal, None)
}

/// Whether a darkened desktop Task View last drew `idle` ago is let go of
/// now (`None`), or how long to wait before asking again: not while Task
/// View is open, and not before [`DARKENED_KEPT`].
fn release_after(idle: Duration, open: bool) -> Option<Duration> {
    if open {
        return Some(DARKENED_KEPT);
    }
    (idle < DARKENED_KEPT).then(|| (DARKENED_KEPT - idle).max(Duration::from_secs(1)))
}

/// The part of the blurred desktop's picture that covers an output of
/// `size` (half its size, rounded up).
fn half_of(size: Size<i32, Logical>) -> Rectangle<f64, Logical> {
    Rectangle::from_size(Size::from(((f64::from(size.w) / 2.0).ceil().max(1.0), (f64::from(size.h) / 2.0).ceil().max(1.0))))
}

/// All of a picture of `pixels` (at buffer scale 1), as the part of it to
/// draw: without it, Smithay takes as much of the picture as the output's
/// logical size, a quarter of it at 200 %.
fn whole(pixels: Size<i32, Physical>) -> Rectangle<f64, Logical> {
    Rectangle::from_size(Size::from((f64::from(pixels.w), f64::from(pixels.h))))
}

/// What the darkened desktop is made from: the blurred desktop at half
/// the output's size (ARGB pixels and their size), the output's size in
/// logical and device pixels and its scale, and the darkening.
struct DarkenJob {
    half: (Vec<u8>, (i32, i32)),
    size: Size<i32, Logical>,
    pixels: Size<i32, Physical>,
    scale: Scale<f64>,
    dim: f32,
}

/// The blurred desktop enlarged to the output with black at the opacity
/// `dim` over it, in ARGB order: drawn by pixman as it draws the two
/// layers on screen (the enlargement filtered linearly), with a renderer of
/// its own, so that it can be made on another thread.
fn darkened(job: &DarkenJob) -> Option<MemoryRenderBuffer> {
    use smithay::backend::renderer::pixman::PixmanRenderer;
    let DarkenJob { half: (bytes, half_size), size, pixels, scale, dim } = job;
    if pixels.w <= 0 || pixels.h <= 0 {
        return None;
    }
    let mut renderer = PixmanRenderer::new().ok()?;
    let half = MemoryRenderBuffer::from_slice(bytes, Fourcc::Argb8888, *half_size, 1, Transform::Normal, None);
    let blurred = MemoryRenderBufferRenderElement::from_buffer(&mut renderer, (0.0, 0.0), &half, None, Some(half_of(*size)), Some(*size), Kind::Unspecified).ok()?;
    let black = SolidColorBuffer::new(*size, [0.0, 0.0, 0.0, *dim]);
    let darkening = SolidColorRenderElement::from_buffer(&black, Point::from((0, 0)), *scale, 1.0, Kind::Unspecified);
    let elements: [OutputRenderElement<PixmanRenderer>; 2] = [OutputElement::Solid(darkening), OutputElement::Memory(blurred)];
    // Drawn in ARGB and copied out as it is, straight into the picture's
    // memory.
    let argb = Fourcc::Argb8888;
    let mut picture = MemoryRenderBuffer::new(argb, (pixels.w, pixels.h), 1, Transform::Normal, None);
    let filled = crate::backend::render_offscreen_with::<_, smithay::reexports::pixman::Image<'static, 'static>, _, _>(
        &mut renderer,
        argb,
        *pixels,
        scale.x,
        Transform::Normal,
        &elements,
        smithay::backend::renderer::Color32F::TRANSPARENT,
        argb,
        |read| {
            let mut filled = false;
            let _ = picture.render().draw(|memory| {
                filled = memory.len() == read.len();
                if filled {
                    memory.copy_from_slice(read);
                }
                Ok::<_, ()>(vec![Rectangle::from_size(Size::from((pixels.w, pixels.h)))])
            });
            filled
        },
    )
    .ok()?;
    filled.then_some(picture)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::render::OutputRenderElement;

    #[test]
    fn the_darkened_desktop_goes_some_minutes_after_task_view_last_faded() {
        assert_eq!(release_after(Duration::ZERO, false), Some(DARKENED_KEPT));
        assert_eq!(release_after(DARKENED_KEPT - Duration::from_secs(30), false), Some(Duration::from_secs(30)));
        assert_eq!(release_after(DARKENED_KEPT - Duration::from_millis(10), false), Some(Duration::from_secs(1)), "no busy loop");
        assert_eq!(release_after(DARKENED_KEPT, false), None);
        assert_eq!(release_after(DARKENED_KEPT * 2, true), Some(DARKENED_KEPT), "not while Task View is open");
    }

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

    /// What a buffer shows enlarged twice with `alpha` by pixman, as the
    /// blurred desktop is in Task View (ARGB bytes).
    fn enlarged(buffer: &MemoryRenderBuffer, size: (i32, i32), alpha: f32) -> Vec<u8> {
        use smithay::backend::renderer::{damage::OutputDamageTracker, pixman::PixmanRenderer, Bind, ExportMem, Offscreen};
        use smithay::reexports::pixman::Image;
        let mut renderer = PixmanRenderer::new().unwrap();
        let to = Size::from((size.0 * 2, size.1 * 2));
        let mut target: Image<'static, 'static> = renderer.create_buffer(Fourcc::Argb8888, to).unwrap();
        let src = Rectangle::from_size(Size::from((f64::from(size.0), f64::from(size.1))));
        let element = MemoryRenderBufferRenderElement::from_buffer(&mut renderer, (0.0, 0.0), buffer, Some(alpha), Some(src), Some(Size::from((size.0 * 2, size.1 * 2))), Kind::Unspecified).unwrap();
        let mut framebuffer = renderer.bind(&mut target).unwrap();
        let mut damage = OutputDamageTracker::new((to.w, to.h), 1.0, Transform::Normal);
        damage.render_output(&mut renderer, &mut framebuffer, 0, &[element], [0.0, 0.0, 0.0, 0.0]).unwrap();
        let mapping = renderer.copy_framebuffer(&framebuffer, Rectangle::from_size(to), Fourcc::Argb8888).unwrap();
        renderer.map_texture(&mapping).unwrap().to_vec()
    }

    #[test]
    fn the_blurred_desktop_in_the_outputs_order_shows_the_same_pixels() {
        let mut canvas = boswas_ui::Canvas::new(37.0, 23.0, 1.0).unwrap();
        // Opaque, with steep and gentle slopes in each channel.
        for (i, pixel) in canvas.pixmap_mut().data_mut().chunks_exact_mut(4).enumerate() {
            let (x, y) = ((i % 37) as u32, (i / 37) as u32);
            pixel.copy_from_slice(&[(x * 7) as u8, (y * 11) as u8, ((x * y) % 256) as u8, 255]);
        }
        let (pw, ph) = canvas.pixel_size();
        let canvas_order = MemoryRenderBuffer::from_slice(canvas.data(), Fourcc::Abgr8888, (pw as i32, ph as i32), 1, Transform::Normal, None);
        for alpha in [1.0, 0.6, 0.05] {
            let shown = enlarged(&output_order(&canvas), (37, 23), alpha);
            assert!(shown.chunks_exact(4).any(|p| p[0] != p[1] && p[1] != p[2]), "the picture is drawn, in colour");
            assert_eq!(shown, enlarged(&canvas_order, (37, 23), alpha), "opacity {alpha}");
        }
    }

    /// Task View's desktop in one picture (made by pixman, as on its
    /// worker thread), shown by `renderer` (into buffers of `format`):
    /// fully shown, it is the blurred desktop with the darkening over it as
    /// `renderer` draws them, over a desktop of any colours, within
    /// `inside` levels where it is opaque and `edges` levels at the
    /// outermost pixels, which pixman's enlargement leaves translucent
    /// (Task View at rest draws the two layers); on its way in it lies
    /// between the desktop and that.
    fn darkened_desktop_stands_for_the_two_layers<R, T>(renderer: &mut R, format: Fourcc, inside: u8, edges: u8)
    where
        R: BoswasRenderer + smithay::backend::renderer::Offscreen<T> + smithay::backend::renderer::Bind<T> + smithay::backend::renderer::ExportMem,
        R::TextureId: Clone + Send + Texture + 'static,
        R::Error: std::fmt::Display,
    {
        let dim = 0.45;
        for (scale, size) in [(1.0, Size::from((75, 45))), (2.0, Size::from((75, 45))), (1.25, Size::from((64, 40)))] {
            let scale = Scale::from(scale);
            let size: Size<i32, Logical> = size;
            let pixels = size.to_physical_precise_round(scale);
            let (hw, hh) = ((size.w as f32 / 2.0).ceil(), (size.h as f32 / 2.0).ceil());
            let mut canvas = boswas_ui::Canvas::new(hw, hh, 1.0).unwrap();
            let width = canvas.pixel_size().0 as usize;
            for (i, pixel) in canvas.pixmap_mut().data_mut().chunks_exact_mut(4).enumerate() {
                let (x, y) = ((i % width) as u32, (i / width) as u32);
                pixel.copy_from_slice(&[(x * 9) as u8, (y * 13) as u8, ((x * y * 3) % 256) as u8, 255]);
            }
            let half = output_order(&canvas);
            let folded = darkened(&DarkenJob { half: argb_pixels(&canvas), size, pixels, scale, dim }).expect("pixman draws it");
            // The desktop under Task View, in many colours.
            let under: Vec<u8> = (0..pixels.w * pixels.h).flat_map(|i| [(i * 5) as u8, (i * 7 / 3) as u8, (i / 11) as u8, 255]).collect();
            let under = MemoryRenderBuffer::from_slice(&under, Fourcc::Argb8888, (pixels.w, pixels.h), 1, Transform::Normal, None);
            let draw = |elements: &[OutputRenderElement<R>], renderer: &mut R| {
                crate::backend::render_offscreen::<_, T, _>(renderer, format, pixels, scale.x, elements, smithay::backend::renderer::Color32F::TRANSPARENT).unwrap()
            };
            let shown = |mut front: Vec<OutputRenderElement<R>>, renderer: &mut R| {
                let desktop = MemoryRenderBufferRenderElement::from_buffer(renderer, (0.0, 0.0), &under, None, Some(whole(pixels)), Some(size), Kind::Unspecified).unwrap();
                front.push(OutputElement::Memory(desktop));
                draw(&front, renderer)
            };
            // The two layers, as Task View draws them at rest.
            let blurred = MemoryRenderBufferRenderElement::from_buffer(renderer, (0.0, 0.0), &half, Some(1.0), Some(half_of(size)), Some(size), Kind::Unspecified).unwrap();
            let black = SolidColorBuffer::new(size, [0.0, 0.0, 0.0, dim]);
            let darkening = SolidColorRenderElement::from_buffer(&black, Point::from((0, 0)), scale, 1.0, Kind::Unspecified);
            let two = shown(vec![OutputElement::Solid(darkening), OutputElement::Memory(blurred)], renderer);
            let one_layer = |alpha: f32, renderer: &mut R| {
                let element = MemoryRenderBufferRenderElement::from_buffer(renderer, (0.0, 0.0), &folded, Some(alpha), Some(whole(pixels)), Some(size), Kind::Unspecified).unwrap();
                vec![OutputElement::Memory(element)]
            };
            let front = one_layer(1.0, renderer);
            let one = shown(front, renderer);
            assert!(two.chunks_exact(4).any(|p| p[0] != p[1]), "something is drawn");
            // Where the picture is opaque (drawn alone, over nothing).
            let front = one_layer(1.0, renderer);
            let coverage = draw(&front, renderer);
            let mut opaque = 0;
            for (i, ((a, b), c)) in one.chunks_exact(4).zip(two.chunks_exact(4)).zip(coverage.chunks_exact(4)).enumerate() {
                let worst = a.iter().zip(b).map(|(x, y)| x.abs_diff(*y)).max().unwrap_or(0);
                if c[3] == 255 {
                    assert!(worst <= inside, "pixel {i} at {scale:?}: {a:?} and {b:?}");
                    opaque += 1;
                } else {
                    assert!(worst <= edges, "pixel {i} at {scale:?}: {a:?} and {b:?}");
                }
            }
            assert!(opaque * 10 > one.len() / 4 * 8, "opaque but at the edges: {opaque} of {}", one.len() / 4);
            // On the way in: the desktop at first, then between it and the
            // end state.
            let front = one_layer(0.0, renderer);
            let start = shown(front, renderer);
            let front = one_layer(0.5, renderer);
            let midway = shown(front, renderer);
            let alone = shown(Vec::new(), renderer);
            assert!(start == alone, "nothing of Task View's desktop shows at first");
            let slack = inside.max(edges).max(1);
            for ((m, a), b) in midway.iter().zip(&alone).zip(&two) {
                assert!(*m >= (*a).min(*b).saturating_sub(slack) && *m <= (*a).max(*b).saturating_add(slack));
            }
        }
    }

    /// On pixman: exactly the two layers where it is opaque, within a
    /// level at the edges.
    #[test]
    fn the_darkened_desktop_stands_for_the_two_layers_on_pixman() {
        let mut renderer = smithay::backend::renderer::pixman::PixmanRenderer::new().unwrap();
        darkened_desktop_stands_for_the_two_layers::<_, smithay::reexports::pixman::Image<'static, 'static>>(&mut renderer, Fourcc::Argb8888, 0, 1);
    }

    /// On OpenGL ES (Mesa's software renderer where there is no GPU), which
    /// blends with other arithmetic and does not leave the edges
    /// translucent: close inside, and near at the edges, where the
    /// pixman-made picture lets a quarter of the desktop show through.
    #[cfg(feature = "udev")]
    #[test]
    fn the_darkened_desktop_stands_for_the_two_layers_on_opengl_es() {
        let Some(mut renderer) = crate::backend::udev::software_gles_renderer() else {
            eprintln!("no OpenGL ES renderer here (EGL surfaceless); the test did not run");
            return;
        };
        darkened_desktop_stands_for_the_two_layers::<_, smithay::backend::renderer::gles::GlesTexture>(&mut renderer, Fourcc::Abgr8888, 3, 128);
    }
}
