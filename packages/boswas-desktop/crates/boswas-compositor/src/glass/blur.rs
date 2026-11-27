//! Live blur: what really lies behind a translucent surface, blurred.
//!
//! For each surface with a blur region (`ext-background-effect-v1`, or one
//! of the compositor's own overlays) an element is put right behind the
//! surface in the output's elements: the scene behind the surface, blurred
//! and drawn within the region. Only OpenGL ES blurs (see
//! [`crate::effects::BoswasRenderer::blur_behind`]); pixman does not, and
//! the blur capability is not offered there.
//!
//! **How.** The scene behind the region (the region's bounding box grown by
//! the blur's reach, clipped to the output) is drawn at half resolution into
//! a texture that is kept, by a damage tracker of its own: only what changed
//! there is drawn again, and when nothing changed nothing else happens. When
//! something did, a dual Kawase blur runs over it: `iterations` passes that
//! halve the picture, sampling five points `offset` half-texels apart, and
//! passes back up to a quarter of the output's resolution (eight points). The
//! element draws that picture over the region, enlarged with linear
//! filtering, with its colours saturated like the glass backdrop and its
//! brightness kept within the scheme's luminosity band (`boswas_ui::glass`).
//! A static scene therefore costs nothing per frame; a window that moves
//! under a flyout costs drawing the part that changed and one blur of the
//! area.
//!
//! **Damage.** A change under the region changes the blurred picture as far
//! as the blur reaches ([`reach`]), so the element's damage is what changed,
//! grown by the reach and cut to the region ([`expand_damage`]).
//!
//! **Shape.** The region is the union of the client's rectangles; the
//! element draws only inside them (an exact staircase for rounded corners:
//! the shell describes its rounded panels row by row near the corners, and
//! its own translucent, anti-aliased edge lies over that). The compositor's
//! own overlays pass their corner radius and are cut round by the shader.

use smithay::{
    backend::renderer::{
        element::{Element, Id, Kind, RenderElement, UnderlyingStorage},
        utils::{CommitCounter, DamageSet, OpaqueRegions},
        Renderer, Texture,
    },
    utils::{Buffer as BufferCoords, Physical, Point, Rectangle, Scale, Size},
};

use crate::effects::BoswasRenderer;

/// How a blur is done: the dual Kawase passes and how the result is shown.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct BlurParams {
    /// Passes that halve the picture.
    pub iterations: u32,
    /// How far apart the samples are, in half-texels of the source.
    pub offset: f32,
    /// Saturation of the result (1.0: unchanged).
    pub saturation: f32,
    /// The relative luminance the result is kept within.
    pub band: (f32, f32),
}

impl BlurParams {
    /// The passes that come closest to a Gaussian of `sigma` physical
    /// pixels (measured on the reference below: a pass level of `n` gives
    /// about 0.7 * 2^n capture texels of sigma per unit of offset), with the
    /// fewest passes whose offset stays at most 3.
    pub fn for_sigma(sigma: f32, saturation: f32, band: (f32, f32)) -> Self {
        // The capture is at half resolution.
        let texels = (sigma / 2.0).max(0.5);
        let mut iterations = 2;
        let mut offset = texels / (0.7 * 4.0);
        while offset > 3.0 && iterations < 7 {
            iterations += 1;
            offset = texels / (0.7 * f32::from(1u16 << iterations));
        }
        Self { iterations, offset: offset.clamp(1.0, 3.0), saturation, band }
    }

    /// Effective sigma of these passes in physical pixels (inverse of
    /// [`BlurParams::for_sigma`]).
    pub fn sigma(&self) -> f32 {
        self.offset * 0.7 * f32::from(1u16 << self.iterations) * 2.0
    }
}

/// How far (physical pixels) a change outside a region can change the blur
/// inside it: about three sigma, where the passes' weights end (measured on
/// the reference), and a coarsest texel more.
pub fn reach(params: &BlurParams) -> i32 {
    (params.sigma() * 3.0).ceil() as i32 + (1 << (params.iterations + 1))
}

/// The part of a blurred region that a change of the scene behind it
/// changes: each changed rectangle grown by `reach` on every side and cut to
/// the region's rectangles. All rectangles in the same coordinates.
pub fn expand_damage(
    changed: &[Rectangle<i32, Physical>],
    reach: i32,
    region: &[Rectangle<i32, Physical>],
) -> Vec<Rectangle<i32, Physical>> {
    let mut out: Vec<Rectangle<i32, Physical>> = Vec::new();
    for rect in changed {
        let grown = Rectangle::new(
            rect.loc - Point::from((reach, reach)),
            rect.size + Size::from((2 * reach, 2 * reach)),
        );
        for part in region {
            if let Some(cut) = grown.intersection(*part) {
                if !out.iter().any(|o| o.contains_rect(cut)) {
                    out.push(cut);
                }
            }
        }
    }
    out
}

/// The capture of the scene behind a region: the area it covers (output
/// physical pixels) and its size at half resolution.
pub fn capture_size(area: Size<i32, Physical>) -> Size<i32, BufferCoords> {
    Size::from(((area.w + 1) / 2, (area.h + 1) / 2))
}

/// The size of each level of the passes, the capture first.
pub fn level_sizes(capture: Size<i32, BufferCoords>, iterations: u32) -> Vec<Size<i32, BufferCoords>> {
    let mut sizes = vec![capture];
    for _ in 0..iterations {
        let last = sizes[sizes.len() - 1];
        sizes.push(Size::from(((last.w + 1) / 2, (last.h + 1) / 2)));
    }
    sizes
}

/// The blurred picture a renderer made, and what changed in it.
pub struct Blurred<T> {
    pub texture: T,
    /// What the texture covers (output physical pixels).
    pub area: Rectangle<i32, Physical>,
    /// The scene changed since the last call for the same key: the
    /// changed rectangles (output physical pixels), before the blur spreads
    /// them. Empty: nothing changed, the texture is the same.
    pub changed: Vec<Rectangle<i32, Physical>>,
}

/// What the element draws with: the shader's settings.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct BlurLook {
    pub saturation: f32,
    pub band: (f32, f32),
    /// Corner radius in physical pixels of the element's geometry (0: the
    /// region's rectangles are the shape).
    pub radius: f32,
    /// The element's geometry size in physical pixels (for the corners).
    pub size: (f32, f32),
}

/// The blurred scene behind a surface, drawn within its region.
pub struct BlurElement<R: Renderer> {
    id: Id,
    commit: CommitCounter,
    texture: R::TextureId,
    /// The texture's part shown over the geometry.
    src: Rectangle<f64, BufferCoords>,
    geometry: Rectangle<i32, Physical>,
    /// The region, relative to the geometry.
    rects: Vec<Rectangle<i32, Physical>>,
    /// What changed since the commit before, relative to the geometry.
    damage: Vec<Rectangle<i32, Physical>>,
    alpha: f32,
    look: BlurLook,
}

impl<R: Renderer> BlurElement<R>
where
    R::TextureId: Texture,
{
    /// `region`: rectangles in output physical pixels inside
    /// `blurred.area`; `damage` relative to the region's bounding box.
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        id: Id,
        commit: CommitCounter,
        blurred: &Blurred<R::TextureId>,
        region: &[Rectangle<i32, Physical>],
        damage: Vec<Rectangle<i32, Physical>>,
        alpha: f32,
        saturation: f32,
        band: (f32, f32),
        radius: f32,
    ) -> Option<Self>
    where
        R::TextureId: Clone,
    {
        let geometry = bounds(region)?;
        let area = blurred.area;
        let tex = blurred.texture.size();
        if area.size.w <= 0 || area.size.h <= 0 || tex.w <= 0 || tex.h <= 0 {
            return None;
        }
        let (fx, fy) = (f64::from(tex.w) / f64::from(area.size.w), f64::from(tex.h) / f64::from(area.size.h));
        let src = Rectangle::new(
            Point::from((f64::from(geometry.loc.x - area.loc.x) * fx, f64::from(geometry.loc.y - area.loc.y) * fy)),
            Size::from((f64::from(geometry.size.w) * fx, f64::from(geometry.size.h) * fy)),
        );
        let rects = region
            .iter()
            .map(|r| Rectangle::new(r.loc - geometry.loc, r.size))
            .collect();
        Some(Self {
            id,
            commit,
            texture: blurred.texture.clone(),
            src,
            geometry,
            rects,
            damage,
            alpha: alpha.clamp(0.0, 1.0),
            look: BlurLook {
                saturation,
                band,
                radius,
                size: (geometry.size.w as f32, geometry.size.h as f32),
            },
        })
    }
}

/// The bounding box of rectangles.
pub fn bounds(rects: &[Rectangle<i32, Physical>]) -> Option<Rectangle<i32, Physical>> {
    rects.iter().copied().reduce(|a, b| a.merge(b)).filter(|r| r.size.w > 0 && r.size.h > 0)
}

impl<R: Renderer> Element for BlurElement<R>
where
    R::TextureId: Texture,
{
    fn id(&self) -> &Id {
        &self.id
    }

    fn current_commit(&self) -> CommitCounter {
        self.commit
    }

    fn src(&self) -> Rectangle<f64, BufferCoords> {
        self.src
    }

    fn geometry(&self, _scale: Scale<f64>) -> Rectangle<i32, Physical> {
        self.geometry
    }

    fn damage_since(&self, _scale: Scale<f64>, commit: Option<CommitCounter>) -> DamageSet<i32, Physical> {
        match self.commit.distance(commit) {
            Some(0) => DamageSet::default(),
            Some(1) => DamageSet::from_slice(&self.damage),
            _ => DamageSet::from_slice(&self.rects),
        }
    }

    fn opaque_regions(&self, _scale: Scale<f64>) -> OpaqueRegions<i32, Physical> {
        // The scene is opaque, and so is its blur; with round corners the
        // corners are not.
        if self.alpha < 1.0 || self.look.radius > 0.0 {
            return OpaqueRegions::default();
        }
        OpaqueRegions::from_slice(&self.rects)
    }

    fn alpha(&self) -> f32 {
        self.alpha
    }

    fn kind(&self) -> Kind {
        Kind::Unspecified
    }
}

impl<R> RenderElement<R> for BlurElement<R>
where
    R: BoswasRenderer,
    R::TextureId: Texture + 'static,
{
    fn draw(
        &self,
        frame: &mut R::Frame<'_, '_>,
        src: Rectangle<f64, BufferCoords>,
        dst: Rectangle<i32, Physical>,
        damage: &[Rectangle<i32, Physical>],
        _opaque_regions: &[Rectangle<i32, Physical>],
    ) -> Result<(), R::Error> {
        // Only within the region (scaled when the element is drawn
        // enlarged, by the magnifier).
        let (sx, sy) = (
            f64::from(dst.size.w) / f64::from(self.geometry.size.w.max(1)),
            f64::from(dst.size.h) / f64::from(self.geometry.size.h.max(1)),
        );
        let mut inside: Vec<Rectangle<i32, Physical>> = Vec::new();
        for rect in &self.rects {
            let scaled = Rectangle::<f64, Physical>::new(
                Point::from((f64::from(rect.loc.x) * sx, f64::from(rect.loc.y) * sy)),
                Size::from((f64::from(rect.size.w) * sx, f64::from(rect.size.h) * sy)),
            )
            .to_i32_round();
            for d in damage {
                if let Some(cut) = d.intersection(scaled) {
                    inside.push(cut);
                }
            }
        }
        if inside.is_empty() {
            return Ok(());
        }
        let look = BlurLook { size: (dst.size.w as f32, dst.size.h as f32), radius: self.look.radius * sx as f32, ..self.look };
        R::draw_blurred(frame, &self.texture, &look, src, dst, &inside, self.alpha)
    }

    fn underlying_storage(&self, _renderer: &mut R) -> Option<UnderlyingStorage<'_>> {
        None
    }
}

/// An element of the scene behind a blurred region, as the capture draws
/// it: moved so that the area's corner is the origin, at half size.
pub struct Behind<'a, E> {
    inner: &'a E,
    offset: Point<i32, Physical>,
}

impl<'a, E> Behind<'a, E> {
    pub fn new(inner: &'a E, offset: Point<i32, Physical>) -> Self {
        Self { inner, offset }
    }

    fn halve(&self, rect: Rectangle<i32, Physical>, outward: bool) -> Rectangle<i32, Physical> {
        let (x0, y0) = (rect.loc.x - self.offset.x, rect.loc.y - self.offset.y);
        let (x1, y1) = (x0 + rect.size.w, y0 + rect.size.h);
        let half = |v: i32, up: bool| if up { (v + 1).div_euclid(2) } else { v.div_euclid(2) };
        let (a, b, c, d) = if outward {
            (half(x0, false), half(y0, false), half(x1, true), half(y1, true))
        } else {
            (half(x0, true), half(y0, true), half(x1, false), half(y1, false))
        };
        Rectangle::new(Point::from((a, b)), Size::from(((c - a).max(0), (d - b).max(0))))
    }

    /// A rectangle relative to the element (damage, opaque parts), halved
    /// with the element's place.
    fn halve_local(&self, rect: Rectangle<i32, Physical>, geometry: Rectangle<i32, Physical>, outward: bool) -> Rectangle<i32, Physical> {
        let absolute = Rectangle::new(rect.loc + geometry.loc, rect.size);
        let halved = self.halve(absolute, outward);
        let origin = self.halve(geometry, true).loc;
        Rectangle::new(halved.loc - origin, halved.size)
    }
}

impl<E: Element> Element for Behind<'_, E> {
    fn id(&self) -> &Id {
        self.inner.id()
    }

    fn current_commit(&self) -> CommitCounter {
        self.inner.current_commit()
    }

    fn location(&self, scale: Scale<f64>) -> Point<i32, Physical> {
        self.geometry(scale).loc
    }

    fn src(&self) -> Rectangle<f64, BufferCoords> {
        self.inner.src()
    }

    fn transform(&self) -> smithay::utils::Transform {
        self.inner.transform()
    }

    fn geometry(&self, scale: Scale<f64>) -> Rectangle<i32, Physical> {
        self.halve(self.inner.geometry(scale), true)
    }

    fn damage_since(&self, scale: Scale<f64>, commit: Option<CommitCounter>) -> DamageSet<i32, Physical> {
        let geometry = self.inner.geometry(scale);
        self.inner
            .damage_since(scale, commit)
            .into_iter()
            .map(|rect| self.halve_local(rect, geometry, true))
            .collect()
    }

    fn opaque_regions(&self, scale: Scale<f64>) -> OpaqueRegions<i32, Physical> {
        let geometry = self.inner.geometry(scale);
        self.inner
            .opaque_regions(scale)
            .into_iter()
            .map(|rect| self.halve_local(rect, geometry, false))
            .filter(|rect| rect.size.w > 0 && rect.size.h > 0)
            .collect()
    }

    fn alpha(&self) -> f32 {
        self.inner.alpha()
    }

    fn kind(&self) -> Kind {
        self.inner.kind()
    }
}

impl<R: Renderer, E: RenderElement<R>> RenderElement<R> for Behind<'_, E> {
    fn draw(
        &self,
        frame: &mut R::Frame<'_, '_>,
        src: Rectangle<f64, BufferCoords>,
        dst: Rectangle<i32, Physical>,
        damage: &[Rectangle<i32, Physical>],
        opaque_regions: &[Rectangle<i32, Physical>],
    ) -> Result<(), R::Error> {
        self.inner.draw(frame, src, dst, damage, opaque_regions)
    }

    fn underlying_storage(&self, _renderer: &mut R) -> Option<UnderlyingStorage<'_>> {
        None
    }
}

/// The dual Kawase blur on the processor: the specification the shaders
/// follow, sample for sample (tests compare them).
#[cfg(test)]
pub mod reference {
    /// A picture of one channel per pixel, row by row.
    #[derive(Clone, Debug, PartialEq)]
    pub struct Plane {
        pub width: usize,
        pub height: usize,
        pub data: Vec<f32>,
    }

    impl Plane {
        pub fn new(width: usize, height: usize, data: Vec<f32>) -> Self {
            assert_eq!(data.len(), width * height);
            Self { width, height, data }
        }

        /// Bilinear sample at texture coordinates (0..1), edges clamped as
        /// the shaders clamp them.
        pub fn sample(&self, u: f32, v: f32) -> f32 {
            let (w, h) = (self.width as f32, self.height as f32);
            let u = u.clamp(0.5 / w, 1.0 - 0.5 / w);
            let v = v.clamp(0.5 / h, 1.0 - 0.5 / h);
            let (x, y) = (u * w - 0.5, v * h - 0.5);
            let (x0, y0) = (x.floor(), y.floor());
            let (fx, fy) = (x - x0, y - y0);
            let at = |xx: f32, yy: f32| {
                let xi = (xx as isize).clamp(0, self.width as isize - 1) as usize;
                let yi = (yy as isize).clamp(0, self.height as isize - 1) as usize;
                self.data[yi * self.width + xi]
            };
            let top = at(x0, y0) * (1.0 - fx) + at(x0 + 1.0, y0) * fx;
            let bottom = at(x0, y0 + 1.0) * (1.0 - fx) + at(x0 + 1.0, y0 + 1.0) * fx;
            top * (1.0 - fy) + bottom * fy
        }
    }

    /// One pass down: half the size, five samples.
    pub fn down(src: &Plane, offset: f32) -> Plane {
        let (dw, dh) = (src.width.div_ceil(2), src.height.div_ceil(2));
        let (hx, hy) = (0.5 / src.width as f32, 0.5 / src.height as f32);
        let mut data = Vec::with_capacity(dw * dh);
        for y in 0..dh {
            for x in 0..dw {
                let (u, v) = ((x as f32 + 0.5) / dw as f32, (y as f32 + 0.5) / dh as f32);
                let mut sum = src.sample(u, v) * 4.0;
                sum += src.sample(u - hx * offset, v - hy * offset);
                sum += src.sample(u + hx * offset, v + hy * offset);
                sum += src.sample(u + hx * offset, v - hy * offset);
                sum += src.sample(u - hx * offset, v + hy * offset);
                data.push(sum / 8.0);
            }
        }
        Plane::new(dw, dh, data)
    }

    /// One pass up to `width` x `height`: eight samples.
    pub fn up(src: &Plane, width: usize, height: usize, offset: f32) -> Plane {
        let (hx, hy) = (0.5 / src.width as f32, 0.5 / src.height as f32);
        let mut data = Vec::with_capacity(width * height);
        for y in 0..height {
            for x in 0..width {
                let (u, v) = ((x as f32 + 0.5) / width as f32, (y as f32 + 0.5) / height as f32);
                let mut sum = src.sample(u - 2.0 * hx * offset, v);
                sum += src.sample(u - hx * offset, v + hy * offset) * 2.0;
                sum += src.sample(u, v + 2.0 * hy * offset);
                sum += src.sample(u + hx * offset, v + hy * offset) * 2.0;
                sum += src.sample(u + 2.0 * hx * offset, v);
                sum += src.sample(u + hx * offset, v - hy * offset) * 2.0;
                sum += src.sample(u, v - 2.0 * hy * offset);
                sum += src.sample(u - hx * offset, v - hy * offset) * 2.0;
                data.push(sum / 12.0);
            }
        }
        Plane::new(width, height, data)
    }

    /// The whole blur of a capture: `iterations` passes down, then up to
    /// level 1 (half the capture), which is what the element shows.
    pub fn blur(capture: &Plane, iterations: u32, offset: f32) -> Plane {
        let mut levels = vec![capture.clone()];
        for _ in 0..iterations.max(1) {
            let next = down(&levels[levels.len() - 1], offset);
            levels.push(next);
        }
        let mut current = levels.pop().expect("a level");
        while levels.len() > 1 {
            let target = levels.pop().expect("a level");
            current = up(&current, target.width, target.height, offset);
        }
        current
    }
}

/// The passes and the final draw on OpenGL ES.
#[cfg(any(feature = "udev", feature = "winit"))]
pub mod gles {
    use std::{
        cell::RefCell,
        collections::HashMap,
        time::{Duration, Instant},
    };

    use smithay::{
        backend::{
            allocator::Fourcc,
            renderer::{
                damage::OutputDamageTracker,
                element::{Id, RenderElement},
                gles::{GlesError, GlesFrame, GlesRenderer, GlesTexProgram, GlesTexture, Uniform, UniformName, UniformType},
                Bind, Color32F, Frame, Offscreen, Renderer, Texture,
            },
        },
        utils::{Buffer as BufferCoords, Physical, Point, Rectangle, Scale, Size, Transform},
    };
    use tracing::warn;

    use super::{capture_size, level_sizes, Behind, BlurLook, BlurParams, Blurred};

    /// Pictures of an area not asked for this long are dropped.
    const KEEP: Duration = Duration::from_secs(3);

    /// The header every texture shader of the renderer has.
    const HEADER: &str = r#"
#version 100

//_DEFINES_

#if defined(EXTERNAL)
#extension GL_OES_EGL_image_external : require
#endif

precision highp float;
#if defined(EXTERNAL)
uniform samplerExternalOES tex;
#else
uniform sampler2D tex;
#endif

uniform float alpha;
varying vec2 v_coords;

#if defined(DEBUG_FLAGS)
uniform float tint;
#endif

uniform vec2 halfpixel;
"#;

    /// Samples are kept half a texel inside the edges: the textures repeat,
    /// and the blur continues the edge outwards instead.
    const DOWN: &str = r#"
uniform float offset;

vec4 at(vec2 uv) {
    return texture2D(tex, clamp(uv, halfpixel, vec2(1.0) - halfpixel));
}

void main() {
    vec2 uv = v_coords;
    vec4 sum = at(uv) * 4.0;
    sum += at(uv - halfpixel * offset);
    sum += at(uv + halfpixel * offset);
    sum += at(uv + vec2(halfpixel.x, -halfpixel.y) * offset);
    sum += at(uv - vec2(halfpixel.x, -halfpixel.y) * offset);
    gl_FragColor = sum / 8.0;
}
"#;

    const UP: &str = r#"
uniform float offset;

vec4 at(vec2 uv) {
    return texture2D(tex, clamp(uv, halfpixel, vec2(1.0) - halfpixel));
}

void main() {
    vec2 uv = v_coords;
    vec2 h = halfpixel * offset;
    vec4 sum = at(uv + vec2(-h.x * 2.0, 0.0));
    sum += at(uv + vec2(-h.x, h.y)) * 2.0;
    sum += at(uv + vec2(0.0, h.y * 2.0));
    sum += at(uv + vec2(h.x, h.y)) * 2.0;
    sum += at(uv + vec2(h.x * 2.0, 0.0));
    sum += at(uv + vec2(h.x, -h.y)) * 2.0;
    sum += at(uv + vec2(0.0, -h.y * 2.0));
    sum += at(uv + vec2(-h.x, -h.y)) * 2.0;
    gl_FragColor = sum / 12.0;
}
"#;

    /// The final draw: saturation, the luminosity band (`super::tone`) and
    /// round corners.
    const SHOW: &str = r#"
uniform float saturation;
uniform vec2 band;
uniform vec4 src_rect;
uniform vec2 geo_size;
uniform float radius;

float rounding(vec2 p, vec2 size, float r) {
    vec2 c;
    if (p.x < r && p.y < r) c = vec2(r, r);
    else if (p.x > size.x - r && p.y < r) c = vec2(size.x - r, r);
    else if (p.x > size.x - r && p.y > size.y - r) c = vec2(size.x - r, size.y - r);
    else if (p.x < r && p.y > size.y - r) c = vec2(r, size.y - r);
    else return 1.0;
    return 1.0 - smoothstep(r - 0.5, r + 0.5, distance(p, c));
}

void main() {
    vec4 color = texture2D(tex, clamp(v_coords, halfpixel, vec2(1.0) - halfpixel));
    vec3 rgb = color.a > 0.0 ? color.rgb / color.a : vec3(0.0);
    float grey = dot(rgb, vec3(0.2126, 0.7152, 0.0722));
    rgb = clamp(vec3(grey) + (rgb - vec3(grey)) * saturation, 0.0, 1.0);
    vec3 lin = pow(rgb, vec3(2.2));
    float lum = dot(lin, vec3(0.2126, 0.7152, 0.0722));
    if (lum > band.y) {
        rgb = rgb * pow(band.y / lum, 1.0 / 2.2);
    } else if (lum < band.x) {
        // Brighter by the factor that brings the luminance up to the band;
        // where a channel would pass white, the colour goes towards white
        // instead, as far as the band needs.
        vec3 lifted = vec3(band.x);
        if (lum > 0.00001) {
            lifted = lin * (band.x / lum);
            float top = max(max(lifted.r, lifted.g), lifted.b);
            if (top > 1.0) {
                vec3 cut = lin / max(max(lin.r, lin.g), lin.b);
                float lc = dot(cut, vec3(0.2126, 0.7152, 0.0722));
                lifted = cut + (vec3(1.0) - cut) * clamp((band.x - lc) / max(1.0 - lc, 0.00001), 0.0, 1.0);
            }
        }
        rgb = pow(lifted, vec3(1.0 / 2.2));
    }
    float cover = 1.0;
    if (radius > 0.0) {
        vec2 p = (v_coords - src_rect.xy) / src_rect.zw * geo_size;
        cover = rounding(p, geo_size, radius);
    }
    color = vec4(rgb, 1.0) * color.a * cover * alpha;

#if defined(DEBUG_FLAGS)
    if (tint == 1.0)
        color = vec4(0.0, 0.2, 0.0, 0.2) + color * 0.8;
#endif

    gl_FragColor = color;
}
"#;

    struct Programs {
        down: GlesTexProgram,
        up: GlesTexProgram,
        show: GlesTexProgram,
    }

    /// The compiled blur shaders, kept with the GL context.
    struct Kept(Option<Programs>);

    /// What is kept of one blurred area between frames.
    struct Entry {
        tracker: OutputDamageTracker,
        capture: GlesTexture,
        /// Levels 1 to `iterations`; level 1 is the result.
        levels: Vec<GlesTexture>,
        area: Rectangle<i32, Physical>,
        scale: Scale<f64>,
        params: BlurParams,
        used: Instant,
        /// The capture has never been drawn.
        fresh: bool,
    }

    struct Cache(RefCell<HashMap<(String, Id), Entry>>);

    /// Compiles the blur shaders. Without them (an old or broken driver)
    /// the renderer does not blur and the capability is not offered.
    pub fn init(renderer: &mut GlesRenderer) {
        let compile = |renderer: &mut GlesRenderer, body: &str, extra: &[UniformName<'_>]| {
            let mut uniforms = vec![UniformName::new("halfpixel", UniformType::_2f)];
            uniforms.extend_from_slice(extra);
            renderer.compile_custom_texture_shader(format!("{HEADER}{body}"), &uniforms)
        };
        let offset = [UniformName::new("offset", UniformType::_1f)];
        let show = [
            UniformName::new("saturation", UniformType::_1f),
            UniformName::new("band", UniformType::_2f),
            UniformName::new("src_rect", UniformType::_4f),
            UniformName::new("geo_size", UniformType::_2f),
            UniformName::new("radius", UniformType::_1f),
        ];
        let programs = (|| -> Result<Programs, GlesError> {
            Ok(Programs {
                down: compile(renderer, DOWN, &offset)?,
                up: compile(renderer, UP, &offset)?,
                show: compile(renderer, SHOW, &show)?,
            })
        })()
        .map_err(|err| warn!("the blur shaders did not compile ({err}); no live blur"))
        .ok();
        renderer.egl_context().user_data().insert_if_missing(|| Kept(programs));
        renderer.egl_context().user_data().insert_if_missing(|| Cache(RefCell::new(HashMap::new())));
    }

    /// Whether the blur shaders are there.
    pub fn blurs(renderer: &GlesRenderer) -> bool {
        renderer.egl_context().user_data().get::<Kept>().is_some_and(|kept| kept.0.is_some())
    }

    fn texture(renderer: &mut GlesRenderer, size: Size<i32, BufferCoords>) -> Result<GlesTexture, GlesError> {
        Offscreen::<GlesTexture>::create_buffer(renderer, Fourcc::Abgr8888, Size::from((size.w.max(1), size.h.max(1))))
    }

    /// One pass from `src` into all of `dst`.
    fn pass(renderer: &mut GlesRenderer, program: &GlesTexProgram, src: &GlesTexture, dst: &mut GlesTexture, offset: f32) -> Result<(), GlesError> {
        let size = dst.size();
        let source = src.size();
        let full = Rectangle::from_size(Size::<i32, Physical>::from((size.w, size.h)));
        let mut framebuffer = renderer.bind(dst)?;
        let mut frame = renderer.render(&mut framebuffer, full.size, Transform::Normal)?;
        frame.clear(Color32F::TRANSPARENT, &[full])?;
        let uniforms = [
            Uniform::new("halfpixel", [0.5 / source.w.max(1) as f32, 0.5 / source.h.max(1) as f32]),
            Uniform::new("offset", offset),
        ];
        frame.render_texture_from_to(src, Rectangle::from_size(source.to_f64()), full, &[full], &[], Transform::Normal, 1.0, Some(program), &uniforms)?;
        let _ = frame.finish()?;
        Ok(())
    }

    /// Brings the blurred picture of `area` up to date (see the module).
    pub fn blur_behind<E: RenderElement<GlesRenderer>>(
        renderer: &mut GlesRenderer,
        key: &(String, Id),
        area: Rectangle<i32, Physical>,
        scale: Scale<f64>,
        elements: &[E],
        params: &BlurParams,
    ) -> Option<Blurred<GlesTexture>> {
        let (down, up) = {
            let kept = renderer.egl_context().user_data().get::<Kept>()?;
            let programs = kept.0.as_ref()?;
            (programs.down.clone(), programs.up.clone())
        };
        if area.size.w <= 0 || area.size.h <= 0 {
            return None;
        }
        let now = Instant::now();
        let mut entry = {
            let cache = renderer.egl_context().user_data().get::<Cache>()?;
            let mut map = cache.0.borrow_mut();
            map.retain(|_, entry| now.duration_since(entry.used) < KEEP);
            map.remove(key)
        };
        let capture = capture_size(area.size);
        let fits = entry.as_ref().is_some_and(|e| {
            e.area.size == area.size && e.params.iterations == params.iterations && e.scale == scale && e.capture.size() == capture
        });
        if !fits {
            let sizes = level_sizes(capture, params.iterations);
            let mut levels = Vec::with_capacity(sizes.len() - 1);
            for size in &sizes[1..] {
                levels.push(texture(renderer, *size).map_err(|err| warn!("no texture for the blur: {err}")).ok()?);
            }
            entry = Some(Entry {
                tracker: OutputDamageTracker::new(Size::from((capture.w, capture.h)), scale, Transform::Normal),
                capture: texture(renderer, capture).map_err(|err| warn!("no texture for the blur: {err}")).ok()?,
                levels,
                area,
                scale,
                params: *params,
                used: now,
                fresh: true,
            });
        }
        let mut entry = entry?;
        let moved = entry.area.loc != area.loc;
        entry.area = area;
        entry.used = now;
        let retuned = entry.params != *params;
        entry.params = *params;

        // The scene behind, drawn where it changed.
        let behind: Vec<Behind<'_, E>> = elements.iter().map(|e| Behind::new(e, area.loc)).collect();
        let age = if entry.fresh || moved { 0 } else { 1 };
        let changed: Option<Vec<Rectangle<i32, Physical>>> = {
            let mut framebuffer = renderer.bind(&mut entry.capture).map_err(|err| warn!("blur: {err}")).ok()?;
            match entry.tracker.render_output(renderer, &mut framebuffer, age, &behind, Color32F::BLACK) {
                Ok(result) => result.damage.cloned(),
                Err(err) => {
                    warn!("blur: {err:?}");
                    return None;
                }
            }
        };
        let changed: Vec<Rectangle<i32, Physical>> = changed
            .unwrap_or_default()
            .into_iter()
            .map(|r| Rectangle::new(Point::from((r.loc.x * 2, r.loc.y * 2)) + area.loc, Size::from((r.size.w * 2, r.size.h * 2))))
            .collect();
        let redo = entry.fresh || retuned || !changed.is_empty();
        if redo {
            let n = entry.levels.len();
            let result = (|| -> Result<(), GlesError> {
                for i in 0..n {
                    let (before, rest) = entry.levels.split_at_mut(i);
                    let src = if i == 0 { &entry.capture } else { &before[i - 1] };
                    pass(renderer, &down, src, &mut rest[0], params.offset)?;
                }
                for k in (1..n).rev() {
                    let (before, rest) = entry.levels.split_at_mut(k);
                    pass(renderer, &up, &rest[0], &mut before[k - 1], params.offset)?;
                }
                Ok(())
            })();
            if let Err(err) = result {
                warn!("blur: {err}");
                return None;
            }
        }
        let whole = vec![area];
        let reported = if entry.fresh || retuned { whole } else { changed };
        entry.fresh = false;
        let texture = entry.levels.first()?.clone();
        if let Some(cache) = renderer.egl_context().user_data().get::<Cache>() {
            cache.0.borrow_mut().insert(key.clone(), entry);
        }
        Some(Blurred { texture, area, changed: reported })
    }

    /// Draws a blurred picture over the region (see `BlurElement::draw`).
    pub fn draw_blurred(
        frame: &mut GlesFrame<'_, '_>,
        texture: &GlesTexture,
        look: &BlurLook,
        src: Rectangle<f64, BufferCoords>,
        dst: Rectangle<i32, Physical>,
        damage: &[Rectangle<i32, Physical>],
        alpha: f32,
    ) -> Result<(), GlesError> {
        let program = frame.egl_context().user_data().get::<Kept>().and_then(|kept| kept.0.as_ref().map(|p| p.show.clone()));
        let tex = texture.size();
        let (tw, th) = (f64::from(tex.w.max(1)), f64::from(tex.h.max(1)));
        let uniforms = [
            Uniform::new("halfpixel", [0.5 / tw as f32, 0.5 / th as f32]),
            Uniform::new("saturation", look.saturation),
            Uniform::new("band", [look.band.0, look.band.1]),
            Uniform::new("src_rect", [(src.loc.x / tw) as f32, (src.loc.y / th) as f32, (src.size.w / tw) as f32, (src.size.h / th) as f32]),
            Uniform::new("geo_size", [look.size.0, look.size.1]),
            Uniform::new("radius", look.radius),
        ];
        frame.render_texture_from_to(texture, src, dst, damage, &[], Transform::Normal, alpha, program.as_ref(), &uniforms)
    }

    /// The shaders on a real OpenGL ES renderer (Mesa's software one where
    /// there is no GPU), against the reference.
    #[cfg(all(test, feature = "udev"))]
    mod tests {
        use smithay::backend::renderer::{
            element::{
                solid::{SolidColorBuffer, SolidColorRenderElement},
                Kind,
            },
            ExportMem,
        };

        use super::super::{reference, tone, BlurElement};
        use super::*;

        fn renderer() -> Option<GlesRenderer> {
            let renderer = crate::backend::udev::software_gles_renderer();
            if renderer.is_none() {
                eprintln!("no OpenGL ES renderer here (EGL surfaceless); the shader tests did not run");
            }
            renderer
        }

        fn pixels(renderer: &mut GlesRenderer, texture: &GlesTexture) -> Vec<u8> {
            let size = texture.size();
            let mapping = renderer.copy_texture(texture, Rectangle::from_size(size), Fourcc::Abgr8888).unwrap();
            renderer.map_texture(&mapping).unwrap().to_vec()
        }

        fn rect(x: i32, y: i32, w: i32, h: i32) -> Rectangle<i32, Physical> {
            Rectangle::new(Point::from((x, y)), Size::from((w, h)))
        }

        #[test]
        fn the_shaders_blur_as_the_reference_and_only_when_the_scene_changed() {
            let Some(mut renderer) = renderer() else {
                return;
            };
            assert!(blurs(&renderer), "the blur shaders compile");
            let scale = Scale::from(1.0);
            // The area: 256 x 64 at (100, 50); black on the left half, white
            // on the right (front to back: white, then black).
            let area = rect(100, 50, 256, 64);
            let mut white = SolidColorBuffer::new(Size::from((128, 64)), [1.0, 1.0, 1.0, 1.0]);
            let black = SolidColorBuffer::new(Size::from((128, 64)), [0.0, 0.0, 0.0, 1.0]);
            let scene = |white: &SolidColorBuffer| {
                vec![
                    SolidColorRenderElement::from_buffer(white, Point::from((228, 50)), scale, 1.0, Kind::Unspecified),
                    SolidColorRenderElement::from_buffer(&black, Point::from((100, 50)), scale, 1.0, Kind::Unspecified),
                ]
            };
            let params = BlurParams { iterations: 3, offset: 1.5, saturation: 1.0, band: (0.0, 1.0) };
            let key = ("TEST-1".to_owned(), Id::new());
            let first = blur_behind(&mut renderer, &key, area, scale, &scene(&white), &params).expect("a blur");
            assert_eq!(first.changed, vec![area], "the first picture is new everywhere");
            assert_eq!(first.texture.size(), Size::from((64, 16)), "a quarter of the area");
            let got = pixels(&mut renderer, &first.texture);
            // The reference: the capture (128 x 32, half the area) blurred.
            let capture = reference::Plane::new(128, 32, (0..128 * 32).map(|i| if i % 128 >= 64 { 1.0 } else { 0.0 }).collect());
            let want = reference::blur(&capture, 3, 1.5);
            assert_eq!((want.width, want.height), (64, 16));
            let mut worst = 0.0f32;
            for (i, value) in want.data.iter().enumerate() {
                let red = f32::from(got[i * 4]) / 255.0;
                worst = worst.max((red - value).abs());
                assert_eq!(got[i * 4 + 3], 255, "opaque");
            }
            assert!(worst <= 6.0 / 255.0, "the shaders differ from the reference by {worst}");
            // Not left-right mirrored or upside down: dark on the left.
            assert!(got[0] < 40 && got[(16 * 64 - 1) * 4] > 215);

            // Nothing changed: no new picture.
            let again = blur_behind(&mut renderer, &key, area, scale, &scene(&white), &params).unwrap();
            assert!(again.changed.is_empty(), "a static scene costs no blur: {:?}", again.changed);

            // The right half changes colour: its part changed.
            white.update(Size::from((128, 64)), [0.0, 0.0, 1.0, 1.0]);
            let changed = blur_behind(&mut renderer, &key, area, scale, &scene(&white), &params).unwrap();
            assert!(changed.changed.iter().any(|r| r.contains_rect(rect(228, 50, 128, 64))), "{:?}", changed.changed);
            let blue = pixels(&mut renderer, &changed.texture);
            let last = (16 * 64 - 1) * 4;
            assert!(blue[last + 2] > 215 && blue[last] < 40, "the blur shows the new colour: {:?}", &blue[last..last + 4]);
        }

        #[test]
        fn the_element_draws_the_blur_within_its_region_in_the_band() {
            let Some(mut renderer) = renderer() else {
                return;
            };
            let scale = Scale::from(1.0);
            let area = rect(0, 0, 128, 64);
            let white = SolidColorBuffer::new(Size::from((128, 64)), [1.0, 1.0, 1.0, 1.0]);
            let scene = vec![SolidColorRenderElement::from_buffer(&white, Point::from((0, 0)), scale, 1.0, Kind::Unspecified)];
            let band = (0.0, 0.07);
            let params = BlurParams { iterations: 2, offset: 1.5, saturation: 1.3, band };
            let blurred = blur_behind(&mut renderer, &("TEST-2".to_owned(), Id::new()), area, scale, &scene, &params).unwrap();
            // The region: two rectangles, a staircase.
            let region = [rect(16, 8, 96, 40), rect(20, 48, 88, 4)];
            let element =
                BlurElement::<GlesRenderer>::new(Id::new(), smithay::backend::renderer::utils::CommitCounter::default(), &blurred, &region, Vec::new(), 1.0, 1.3, band, 0.0).unwrap();
            let size = Size::<i32, Physical>::from((128, 64));
            let rgba = crate::backend::render_offscreen::<_, GlesTexture, _>(&mut renderer, Fourcc::Abgr8888, size, 1.0, &[element], Color32F::TRANSPARENT).unwrap();
            let at = |x: usize, y: usize| &rgba[(y * 128 + x) * 4..(y * 128 + x) * 4 + 4];
            let want = tone([1.0; 3], 1.3, band);
            let inside = at(60, 30);
            assert!((f32::from(inside[0]) / 255.0 - want[0]).abs() < 0.03, "white is brought into the band: {inside:?} vs {want:?}");
            assert_eq!(inside[3], 255);
            assert_eq!(at(4, 4)[3], 0, "nothing outside the region");
            assert_eq!(at(17, 50)[3], 0, "the staircase is the shape");
            assert_eq!(at(22, 50)[3], 255);

            // Light glass over a dark green window: lifted to the band's
            // lower end, still green.
            let green = SolidColorBuffer::new(Size::from((128, 64)), [0.05, 0.25, 0.05, 1.0]);
            let scene = vec![SolidColorRenderElement::from_buffer(&green, Point::from((0, 0)), scale, 1.0, Kind::Unspecified)];
            let band = (0.55, 1.0);
            let params = BlurParams { iterations: 2, offset: 1.5, saturation: 1.0, band };
            let blurred = blur_behind(&mut renderer, &("TEST-3".to_owned(), Id::new()), area, scale, &scene, &params).unwrap();
            let element =
                BlurElement::<GlesRenderer>::new(Id::new(), smithay::backend::renderer::utils::CommitCounter::default(), &blurred, &region, Vec::new(), 1.0, 1.0, band, 0.0).unwrap();
            let rgba = crate::backend::render_offscreen::<_, GlesTexture, _>(&mut renderer, Fourcc::Abgr8888, size, 1.0, &[element], Color32F::TRANSPARENT).unwrap();
            let p = &rgba[(30 * 128 + 60) * 4..(30 * 128 + 60) * 4 + 3];
            let lin = |v: u8| (f32::from(v) / 255.0).powf(2.2);
            let lum = 0.2126 * lin(p[0]) + 0.7152 * lin(p[1]) + 0.0722 * lin(p[2]);
            assert!((lum - 0.55).abs() < 0.03 && p[1] > p[0] && p[1] > p[2], "lifted into the band: {p:?} ({lum})");
        }
    }
}

/// The saturation and luminosity band of the shader on one colour
/// (gamma-encoded RGB, 0..1): the specification of the final draw.
#[cfg(test)]
pub fn tone(rgb: [f32; 3], saturation: f32, band: (f32, f32)) -> [f32; 3] {
    let grey = 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2];
    let mut c = rgb.map(|v| (grey + (v - grey) * saturation).clamp(0.0, 1.0));
    let linear = |v: f32| v.max(0.0).powf(2.2);
    let luminance = 0.2126 * linear(c[0]) + 0.7152 * linear(c[1]) + 0.0722 * linear(c[2]);
    if luminance > band.1 {
        // Darker by the factor that brings the luminance to the band.
        let k = (band.1 / luminance).powf(1.0 / 2.2);
        c = c.map(|v| v * k);
    } else if luminance < band.0 {
        // Brighter by the factor that brings the luminance up to the band;
        // where a channel would pass white, towards white instead.
        let lin = c.map(linear);
        let lifted = if luminance > 1e-5 {
            let up = lin.map(|v| v * band.0 / luminance);
            if up.iter().copied().fold(0.0f32, f32::max) > 1.0 {
                let top = lin.iter().copied().fold(0.0f32, f32::max);
                let cut = lin.map(|v| v / top);
                let lc = 0.2126 * cut[0] + 0.7152 * cut[1] + 0.0722 * cut[2];
                let t = ((band.0 - lc) / (1.0 - lc).max(1e-5)).clamp(0.0, 1.0);
                cut.map(|v| v + (1.0 - v) * t)
            } else {
                up
            }
        } else {
            [band.0; 3]
        };
        c = lifted.map(|v| v.powf(1.0 / 2.2));
    }
    c
}

#[cfg(test)]
mod tests {
    use super::reference::*;
    use super::*;

    fn rect(x: i32, y: i32, w: i32, h: i32) -> Rectangle<i32, Physical> {
        Rectangle::new(Point::from((x, y)), Size::from((w, h)))
    }

    fn step(width: usize) -> Plane {
        let height = 8;
        let data = (0..width * height).map(|i| if i % width >= width / 2 { 1.0 } else { 0.0 }).collect();
        Plane::new(width, height, data)
    }

    /// Sigma of a blurred step in capture texels (10 to 90 % width).
    fn measured_sigma(params: &BlurParams) -> f32 {
        let capture = step(1024);
        let out = blur(&capture, params.iterations, params.offset);
        // Back at capture size to measure.
        let row: Vec<f32> = (0..1024).map(|x| out.sample((x as f32 + 0.5) / 1024.0, 0.5)).collect();
        let i10 = row.iter().position(|v| *v >= 0.1).unwrap();
        let i90 = row.iter().position(|v| *v >= 0.9).unwrap();
        (i90 - i10) as f32 / 2.563
    }

    #[test]
    fn the_passes_come_close_to_the_glass_blur() {
        for sigma in [20.0f32, 40.0, 60.0, 80.0] {
            let params = BlurParams::for_sigma(sigma, 1.0, (0.0, 1.0));
            assert!((1.0..=3.0).contains(&params.offset), "{params:?}");
            let got = measured_sigma(&params) * 2.0;
            assert!((got - sigma).abs() / sigma < 0.2, "sigma {sigma}: the passes give {got} ({params:?})");
        }
    }

    #[test]
    fn the_blur_keeps_flat_areas_and_the_average() {
        let flat = Plane::new(64, 40, vec![0.25; 64 * 40]);
        let out = blur(&flat, 3, 1.5);
        assert!(out.data.iter().all(|v| (v - 0.25).abs() < 1e-5));
        let edge = blur(&step(256), 3, 1.5);
        let mean: f32 = edge.data.iter().sum::<f32>() / edge.data.len() as f32;
        assert!((mean - 0.5).abs() < 0.02, "{mean}");
        assert_eq!((edge.width, edge.height), (128, 4), "level 1: half the capture");
    }

    #[test]
    fn nothing_reaches_further_than_the_reach() {
        let params = BlurParams::for_sigma(40.0, 1.0, (0.0, 1.0));
        let width = 1024;
        let mut data = vec![0.0; width * 4];
        for y in 0..4 {
            data[y * width + width / 2] = 1.0;
        }
        let out = blur(&Plane::new(width, 4, data), params.iterations, params.offset);
        // Output texels are 4 physical pixels.
        let furthest = (0..out.width).filter(|x| out.data[out.width + x] > 1e-4).map(|x| (x as i32 * 4 - width as i32).abs()).max().unwrap();
        assert!(furthest <= reach(&params), "{furthest} > {}", reach(&params));
    }

    #[test]
    fn a_change_damages_the_region_as_far_as_the_blur_reaches() {
        let region = [rect(100, 100, 200, 100), rect(108, 200, 184, 8)];
        // A change far away damages nothing.
        assert!(expand_damage(&[rect(0, 0, 10, 10)], 20, &region).is_empty());
        // One beside the region, within reach, damages its near edge.
        let near = expand_damage(&[rect(70, 120, 10, 10)], 30, &region);
        assert_eq!(near, vec![rect(100, 100, 10, 60)]);
        // One under it damages a band around it, inside the region only.
        let under = expand_damage(&[rect(190, 150, 20, 20)], 25, &region);
        assert_eq!(under[0], rect(165, 125, 70, 70));
        assert!(under.iter().all(|r| region.iter().any(|p| p.contains_rect(*r))));
        // The lower strip gets its part too.
        let low = expand_damage(&[rect(150, 195, 4, 4)], 10, &region);
        assert!(low.contains(&rect(140, 185, 24, 15)) && low.contains(&rect(140, 200, 24, 8)), "{low:?}");
    }

    #[test]
    fn the_capture_and_its_levels_halve() {
        assert_eq!(capture_size(Size::from((1001, 600))), Size::from((501, 300)));
        let sizes = level_sizes(Size::from((501, 300)), 3);
        assert_eq!(sizes, vec![Size::from((501, 300)), Size::from((251, 150)), Size::from((126, 75)), Size::from((63, 38))]);
    }

    #[test]
    fn the_band_keeps_brightness_and_saturation_strengthens_colour() {
        let dark = (0.0, 0.07);
        let white = tone([1.0; 3], 1.0, dark);
        let lum = |c: [f32; 3]| 0.2126 * c[0].powf(2.2) + 0.7152 * c[1].powf(2.2) + 0.0722 * c[2].powf(2.2);
        assert!((lum(white) - 0.07).abs() < 0.005, "white is brought down to the band: {white:?}");
        assert_eq!(tone([0.1, 0.1, 0.1], 1.0, dark), [0.1, 0.1, 0.1], "inside the band nothing changes");
        let light = (0.55, 1.0);
        // Every colour comes up to the band's lower end, keeping its hue
        // where it can: black, a dark blue, a mixed green, a saturated red.
        for colour in [[0.0; 3], [0.05, 0.1, 0.45], [0.44, 0.7, 0.3], [0.9, 0.1, 0.1]] {
            let lifted = tone(colour, 1.0, light);
            assert!((lum(lifted) - 0.55).abs() < 0.01, "{colour:?} -> {lifted:?} ({})", lum(lifted));
        }
        let green = tone([0.44, 0.7, 0.3], 1.0, light);
        assert!(green[1] > green[0] && green[1] > green[2], "still green: {green:?}");
        let vivid = tone([0.6, 0.4, 0.4], 1.3, (0.0, 1.0));
        assert!(vivid[0] - vivid[1] > 0.2 + 1e-4, "{vivid:?}");
    }

    #[test]
    fn halved_elements_keep_their_damage_inside_and_their_opaque_parts_inward() {
        struct Fake(Id);
        impl Element for Fake {
            fn id(&self) -> &Id {
                &self.0
            }
            fn current_commit(&self) -> CommitCounter {
                CommitCounter::default()
            }
            fn src(&self) -> Rectangle<f64, BufferCoords> {
                Rectangle::from_size(Size::from((10.0, 10.0)))
            }
            fn geometry(&self, _scale: Scale<f64>) -> Rectangle<i32, Physical> {
                rect(101, 51, 41, 21)
            }
            fn damage_since(&self, _scale: Scale<f64>, _commit: Option<CommitCounter>) -> DamageSet<i32, Physical> {
                DamageSet::from_slice(&[rect(1, 1, 3, 3)])
            }
            fn opaque_regions(&self, _scale: Scale<f64>) -> OpaqueRegions<i32, Physical> {
                OpaqueRegions::from_slice(&[rect(0, 0, 41, 21)])
            }
        }
        let fake = Fake(Id::new());
        let behind = Behind::new(&fake, Point::from((100, 50)));
        let scale = Scale::from(1.0);
        assert_eq!(behind.geometry(scale), rect(0, 0, 21, 11), "grown outwards");
        let damage: Vec<_> = behind.damage_since(scale, None).into_iter().collect();
        assert_eq!(damage, vec![rect(1, 1, 2, 2)]);
        let opaque: Vec<_> = behind.opaque_regions(scale).into_iter().collect();
        assert_eq!(opaque, vec![rect(1, 1, 20, 10)], "shrunk inwards");
    }
}
