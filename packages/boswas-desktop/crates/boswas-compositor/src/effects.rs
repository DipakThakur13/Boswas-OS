//! Rendering effects: window content clipped to rounded corners, and the
//! colour filters of the whole picture (Accessibility > Colour filters).
//!
//! The frame of a window (title bar, border, shadow) is drawn by the
//! compositor and has rounded corners of its own. The client's content
//! below the title bar is a plain rectangle; on the GL renderer it is
//! drawn through a fragment shader that fades it out outside a rounded
//! rectangle, so the bottom corners of a window are round as well. The
//! software renderer (tests, CI) draws content unclipped.

use smithay::{
    backend::renderer::{
        element::{surface::WaylandSurfaceRenderElement, Element, Id, Kind, RenderElement, UnderlyingStorage},
        pixman::PixmanRenderer,
        utils::{CommitCounter, DamageSet, OpaqueRegions},
        Color32F, Frame, ImportAll, ImportMem, Renderer, Texture,
    },
    utils::{Buffer as BufferCoords, Physical, Point, Rectangle, Scale, Size, Transform},
};

/// A renderer the compositor can draw with.
pub trait BoswasRenderer: Renderer + ImportAll + ImportMem + Sized
where
    Self::TextureId: Texture + 'static,
{
    /// Whether [`BoswasRenderer::draw_clipped`] really clips. Frames draw
    /// rounded bottom corners only when it does.
    const ROUNDS_CORNERS: bool = false;

    /// Draws a surface clipped to the rounded rectangle of `clip`.
    fn draw_clipped(
        frame: &mut Self::Frame<'_, '_>,
        element: &WaylandSurfaceRenderElement<Self>,
        clip: &Clip,
        src: Rectangle<f64, BufferCoords>,
        dst: Rectangle<i32, Physical>,
        damage: &[Rectangle<i32, Physical>],
        opaque_regions: &[Rectangle<i32, Physical>],
    ) -> Result<(), Self::Error> {
        let _ = clip;
        RenderElement::<Self>::draw(element, frame, src, dst, damage, opaque_regions)
    }

    /// Whether this renderer applies the colour filters: it draws a frame
    /// into a texture ([`BoswasRenderer::color_filter_frame`]) and that
    /// texture through the filter ([`BoswasRenderer::draw_filtered`]).
    const FILTERS_COLOR: bool = false;

    /// Draws `elements` into a texture of `size` (physical pixels, the
    /// output's own orientation), to be shown through a colour filter.
    /// `None`: the renderer cannot.
    fn color_filter_frame<E: RenderElement<Self>>(
        &mut self,
        size: Size<i32, Physical>,
        scale: Scale<f64>,
        elements: &[E],
        clear: Color32F,
    ) -> Option<Self::TextureId> {
        let _ = (size, scale, elements, clear);
        None
    }

    /// Draws a texture of [`BoswasRenderer::color_filter_frame`] through
    /// the colour matrix.
    #[allow(clippy::too_many_arguments)]
    fn draw_filtered(
        frame: &mut Self::Frame<'_, '_>,
        texture: &Self::TextureId,
        filter: &ColorMatrix,
        src: Rectangle<f64, BufferCoords>,
        dst: Rectangle<i32, Physical>,
        damage: &[Rectangle<i32, Physical>],
        opaque_regions: &[Rectangle<i32, Physical>],
    ) -> Result<(), Self::Error> {
        let _ = filter;
        frame.render_texture_from_to(texture, src, dst, damage, opaque_regions, Transform::Normal, 1.0)
    }
}

impl BoswasRenderer for PixmanRenderer {}

/// A colour filter as an affine map of (gamma-encoded) RGB: `matrix` (row
/// by row) times the colour, plus `offset`, clamped to 0..1.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct ColorMatrix {
    pub matrix: [[f32; 3]; 3],
    pub offset: [f32; 3],
}

/// Weights of the channels for the brightness a grey keeps (Rec. 709).
const LUMA: [f32; 3] = [0.2126, 0.7152, 0.0722];

/// How people with each kind of colour blindness see colours (Machado,
/// Oliveira and Fernandes 2009, full severity), row by row.
const PROTANOPIA: [[f32; 3]; 3] = [[0.152_286, 1.052_583, -0.204_868], [0.114_503, 0.786_281, 0.099_216], [-0.003_882, -0.048_116, 1.051_998]];
const DEUTERANOPIA: [[f32; 3]; 3] = [[0.367_322, 0.860_646, -0.227_968], [0.280_085, 0.672_501, 0.047_413], [-0.011_820, 0.042_940, 0.968_881]];
const TRITANOPIA: [[f32; 3]; 3] = [[1.255_528, -0.076_749, -0.178_779], [-0.078_411, 0.930_809, 0.147_602], [0.004_733, 0.691_367, 0.303_900]];

impl ColorMatrix {
    const IDENTITY: [[f32; 3]; 3] = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]];

    /// The filter of a name of `[color_filter] filter`; `None` for an
    /// unknown name.
    pub fn named(name: &str) -> Option<Self> {
        let grey = [LUMA, LUMA, LUMA];
        let negate = |m: [[f32; 3]; 3]| m.map(|row| row.map(|v| -v));
        Some(match name {
            "grayscale" => Self { matrix: grey, offset: [0.0; 3] },
            "inverted" => Self { matrix: negate(Self::IDENTITY), offset: [1.0; 3] },
            "grayscale_inverted" => Self { matrix: negate(grey), offset: [1.0; 3] },
            // The three corrections move the colour differences a person
            // cannot see into channels they can (daltonization): red-green
            // differences into brightness and blue, blue-yellow ones into
            // red and green.
            "protanopia" => Self::correction(PROTANOPIA, [[0.0, 0.0, 0.0], [0.7, 1.0, 0.0], [0.7, 0.0, 1.0]]),
            "deuteranopia" => Self::correction(DEUTERANOPIA, [[0.0, 0.0, 0.0], [0.7, 1.0, 0.0], [0.7, 0.0, 1.0]]),
            "tritanopia" => Self::correction(TRITANOPIA, [[1.0, 0.0, 0.7], [0.0, 1.0, 0.7], [0.0, 0.0, 0.0]]),
            _ => return None,
        })
    }

    /// `I + shift * (I - seen)`: the colour plus the part of it that is
    /// lost, moved where it can be seen.
    fn correction(seen: [[f32; 3]; 3], shift: [[f32; 3]; 3]) -> Self {
        let mut lost = [[0.0f32; 3]; 3];
        for (r, row) in lost.iter_mut().enumerate() {
            for (c, value) in row.iter_mut().enumerate() {
                *value = Self::IDENTITY[r][c] - seen[r][c];
            }
        }
        let mut matrix = Self::IDENTITY;
        for (r, row) in matrix.iter_mut().enumerate() {
            for (c, value) in row.iter_mut().enumerate() {
                *value += (0..3).map(|k| shift[r][k] * lost[k][c]).sum::<f32>();
            }
        }
        Self { matrix, offset: [0.0; 3] }
    }

    /// The filtered colour of `rgb` (each 0..1).
    pub fn apply(&self, rgb: [f32; 3]) -> [f32; 3] {
        let mut out = [0.0f32; 3];
        for (channel, value) in out.iter_mut().enumerate() {
            let row = self.matrix[channel];
            *value = (row[0] * rgb[0] + row[1] * rgb[1] + row[2] * rgb[2] + self.offset[channel]).clamp(0.0, 1.0);
        }
        out
    }

    /// The matrix column by column, as GLSL's `mat3` takes it.
    fn columns(&self) -> [f32; 9] {
        let m = self.matrix;
        [m[0][0], m[1][0], m[2][0], m[0][1], m[1][1], m[2][1], m[0][2], m[1][2], m[2][2]]
    }
}

/// The whole picture of an output, drawn into a texture by
/// [`BoswasRenderer::color_filter_frame`], shown through a colour filter.
pub struct FilteredFrame<R: Renderer> {
    texture: R::TextureId,
    filter: ColorMatrix,
    size: Size<i32, Physical>,
    id: Id,
    commit: CommitCounter,
}

impl<R: Renderer> FilteredFrame<R> {
    /// `id` stays the same from frame to frame; `commit` counts the frames.
    pub fn new(texture: R::TextureId, filter: ColorMatrix, size: Size<i32, Physical>, id: Id, commit: CommitCounter) -> Self {
        Self { texture, filter, size, id, commit }
    }
}

impl<R> Element for FilteredFrame<R>
where
    R: BoswasRenderer,
    R::TextureId: Texture + 'static,
{
    fn id(&self) -> &Id {
        &self.id
    }

    fn current_commit(&self) -> CommitCounter {
        self.commit
    }

    fn src(&self) -> Rectangle<f64, BufferCoords> {
        Rectangle::from_size(Size::from((f64::from(self.size.w), f64::from(self.size.h))))
    }

    fn geometry(&self, _scale: Scale<f64>) -> Rectangle<i32, Physical> {
        Rectangle::from_size(self.size)
    }

    fn opaque_regions(&self, _scale: Scale<f64>) -> OpaqueRegions<i32, Physical> {
        OpaqueRegions::from_slice(&[Rectangle::from_size(self.size)])
    }

    fn kind(&self) -> Kind {
        Kind::Unspecified
    }
}

impl<R> RenderElement<R> for FilteredFrame<R>
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
        opaque_regions: &[Rectangle<i32, Physical>],
    ) -> Result<(), R::Error> {
        R::draw_filtered(frame, &self.texture, &self.filter, src, dst, damage, opaque_regions)
    }

    fn underlying_storage(&self, _renderer: &mut R) -> Option<UnderlyingStorage<'_>> {
        None
    }
}

/// What the clip shader needs: the size of the rounded rectangle, its
/// corner radii and the matrix from texture coordinates into it.
#[derive(Clone, Debug, PartialEq)]
pub struct Clip {
    /// Size of the rounded rectangle in physical pixels.
    size: (f32, f32),
    /// Radii in physical pixels: top-left, top-right, bottom-right,
    /// bottom-left.
    radius: [f32; 4],
    /// Texture coordinates (0..1 over the buffer) to coordinates inside the
    /// rounded rectangle (0..1), column-major.
    input_to_geo: [f32; 9],
}

/// A 3x3 matrix, column-major, for 2D affine transforms.
#[derive(Clone, Copy, Debug, PartialEq)]
struct Mat3([f32; 9]);

impl Mat3 {
    fn translation(x: f32, y: f32) -> Self {
        Self([1.0, 0.0, 0.0, 0.0, 1.0, 0.0, x, y, 1.0])
    }

    fn scale(x: f32, y: f32) -> Self {
        Self([x, 0.0, 0.0, 0.0, y, 0.0, 0.0, 0.0, 1.0])
    }

    /// `self * other`: `other` is applied first.
    fn then_after(self, other: Mat3) -> Self {
        let (a, b) = (self.0, other.0);
        let mut out = [0.0f32; 9];
        for column in 0..3 {
            for row in 0..3 {
                out[column * 3 + row] = (0..3).map(|k| a[k * 3 + row] * b[column * 3 + k]).sum();
            }
        }
        Self(out)
    }

    #[cfg(test)]
    fn apply(self, x: f32, y: f32) -> (f32, f32) {
        let m = self.0;
        (m[0] * x + m[3] * y + m[6], m[1] * x + m[4] * y + m[7])
    }
}

impl Clip {
    /// The clip of a surface element drawn at `element` (physical output
    /// coordinates) against the rounded rectangle `rect`. `buffer` is the
    /// buffer's size and `source` the part of it that is shown, both in the
    /// same (logical buffer) units.
    fn new(
        element: Rectangle<f64, Physical>,
        rect: Rectangle<f64, Physical>,
        radius: [f32; 4],
        buffer: (f64, f64),
        source: Rectangle<f64, smithay::utils::Logical>,
    ) -> Option<Self> {
        if element.size.w <= 0.0 || element.size.h <= 0.0 || rect.size.w <= 0.0 || rect.size.h <= 0.0 {
            return None;
        }
        if buffer.0 <= 0.0 || buffer.1 <= 0.0 || source.size.w <= 0.0 || source.size.h <= 0.0 {
            return None;
        }
        // Texture coordinates to 0..1 over the shown part of the buffer...
        let to_source = Mat3::scale((buffer.0 / source.size.w) as f32, (buffer.1 / source.size.h) as f32)
            .then_after(Mat3::translation(-(source.loc.x / buffer.0) as f32, -(source.loc.y / buffer.1) as f32));
        // ...which is where the element is drawn; from there into the
        // rounded rectangle.
        let to_rect = Mat3::scale((element.size.w / rect.size.w) as f32, (element.size.h / rect.size.h) as f32)
            .then_after(Mat3::translation(
                ((element.loc.x - rect.loc.x) / element.size.w) as f32,
                ((element.loc.y - rect.loc.y) / element.size.h) as f32,
            ));
        Some(Self {
            size: (rect.size.w as f32, rect.size.h as f32),
            radius,
            input_to_geo: to_rect.then_after(to_source).0,
        })
    }
}

/// A client surface drawn clipped to a rounded rectangle.
pub struct ClippedSurface<R: Renderer> {
    inner: WaylandSurfaceRenderElement<R>,
    clip: Clip,
    /// The corner squares that are (partly) cut away, relative to the
    /// element's location: nothing there is opaque.
    corners: Vec<Rectangle<i32, Physical>>,
}

impl<R> ClippedSurface<R>
where
    R: BoswasRenderer,
    R::TextureId: Texture + 'static,
{
    /// Wraps a surface element so that it is drawn clipped to `rect` (in
    /// physical output coordinates) with the given corner radii. Returns the
    /// element unchanged when it does not reach a rounded corner, when the
    /// renderer does not clip, or when its buffer is rotated (which the
    /// shader's matrix does not cover).
    pub fn wrap(
        inner: WaylandSurfaceRenderElement<R>,
        rect: Rectangle<f64, Physical>,
        radius: [f32; 4],
        scale: Scale<f64>,
    ) -> Result<Self, WaylandSurfaceRenderElement<R>> {
        if !R::ROUNDS_CORNERS || inner.transform() != Transform::Normal {
            return Err(inner);
        }
        let geometry = inner.geometry(scale);
        let squares = corner_squares(rect, radius);
        let touched: Vec<Rectangle<i32, Physical>> = squares
            .iter()
            .filter_map(|square| square.intersection(geometry))
            .map(|mut square| {
                square.loc -= geometry.loc;
                square
            })
            .collect();
        if touched.is_empty() {
            return Err(inner);
        }
        let buffer = inner.buffer_size();
        let clip = Clip::new(
            geometry.to_f64(),
            rect,
            radius,
            (f64::from(buffer.w), f64::from(buffer.h)),
            inner.view().src,
        );
        match clip {
            Some(clip) => Ok(Self {
                inner,
                clip,
                corners: touched,
            }),
            None => Err(inner),
        }
    }
}

/// The squares around the rounded corners of `rect`, one pixel generous.
fn corner_squares(rect: Rectangle<f64, Physical>, radius: [f32; 4]) -> Vec<Rectangle<i32, Physical>> {
    let (x0, y0) = (rect.loc.x.floor() as i32, rect.loc.y.floor() as i32);
    let (x1, y1) = ((rect.loc.x + rect.size.w).ceil() as i32, (rect.loc.y + rect.size.h).ceil() as i32);
    let square = |x: i32, y: i32, r: i32| Rectangle::new(Point::from((x, y)), Size::from((r, r)));
    let mut out = Vec::with_capacity(4);
    for (index, r) in radius.iter().enumerate() {
        let r = r.ceil() as i32 + 1;
        if r <= 1 {
            continue;
        }
        out.push(match index {
            0 => square(x0, y0, r),
            1 => square(x1 - r, y0, r),
            2 => square(x1 - r, y1 - r, r),
            _ => square(x0, y1 - r, r),
        });
    }
    out
}

impl<R> Element for ClippedSurface<R>
where
    R: BoswasRenderer,
    R::TextureId: Texture + 'static,
{
    fn id(&self) -> &Id {
        self.inner.id()
    }

    fn current_commit(&self) -> CommitCounter {
        self.inner.current_commit()
    }

    fn location(&self, scale: Scale<f64>) -> Point<i32, Physical> {
        self.inner.location(scale)
    }

    fn src(&self) -> Rectangle<f64, BufferCoords> {
        self.inner.src()
    }

    fn transform(&self) -> Transform {
        self.inner.transform()
    }

    fn geometry(&self, scale: Scale<f64>) -> Rectangle<i32, Physical> {
        self.inner.geometry(scale)
    }

    fn damage_since(&self, scale: Scale<f64>, commit: Option<CommitCounter>) -> DamageSet<i32, Physical> {
        self.inner.damage_since(scale, commit)
    }

    fn opaque_regions(&self, scale: Scale<f64>) -> OpaqueRegions<i32, Physical> {
        let regions: Vec<Rectangle<i32, Physical>> = self.inner.opaque_regions(scale).into_iter().collect();
        Rectangle::subtract_rects_many(regions, self.corners.iter().copied())
            .into_iter()
            .collect()
    }

    fn alpha(&self) -> f32 {
        self.inner.alpha()
    }

    fn kind(&self) -> Kind {
        self.inner.kind()
    }
}

impl<R> RenderElement<R> for ClippedSurface<R>
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
        opaque_regions: &[Rectangle<i32, Physical>],
    ) -> Result<(), R::Error> {
        R::draw_clipped(frame, &self.inner, &self.clip, src, dst, damage, opaque_regions)
    }

    fn underlying_storage(&self, _renderer: &mut R) -> Option<UnderlyingStorage<'_>> {
        // Scanning the buffer out directly would show it unclipped.
        None
    }
}

#[cfg(any(feature = "udev", feature = "winit"))]
mod gles {
    use std::cell::RefCell;

    use smithay::backend::{
        allocator::Fourcc,
        renderer::{
            damage::OutputDamageTracker,
            gles::{GlesRenderer, GlesTexProgram, GlesTexture, Uniform, UniformName, UniformType, UniformValue},
            Bind, Offscreen,
        },
    };
    use tracing::warn;

    use super::*;

    /// The compiled clip shader, kept with the GL context it belongs to.
    struct ClipProgram(Option<GlesTexProgram>);

    /// The compiled colour filter shader.
    struct FilterProgram(Option<GlesTexProgram>);

    /// The texture the last filtered frame was drawn into, reused while
    /// the output keeps its size.
    struct FilterTexture(RefCell<Option<GlesTexture>>);

    const FILTER_SHADER: &str = r#"
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

uniform mat3 color_matrix;
uniform vec3 color_offset;

void main() {
    vec4 color = texture2D(tex, v_coords);
#if defined(NO_ALPHA)
    color = vec4(color.rgb, 1.0);
#endif
    // The filter works on straight colour; the texture is premultiplied.
    vec3 rgb = color.a > 0.0 ? color.rgb / color.a : vec3(0.0);
    rgb = clamp(color_matrix * rgb + color_offset, 0.0, 1.0);
    color = vec4(rgb * color.a, color.a) * alpha;

#if defined(DEBUG_FLAGS)
    if (tint == 1.0)
        color = vec4(0.0, 0.2, 0.0, 0.2) + color * 0.8;
#endif

    gl_FragColor = color;
}
"#;

    const CLIP_SHADER: &str = r#"
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

uniform vec2 geo_size;
uniform vec4 corner_radius;
uniform mat3 input_to_geo;

// Coverage of a pixel at `p` inside a rectangle of `size` whose corners are
// rounded by `radius` (top-left, top-right, bottom-right, bottom-left).
float rounding(vec2 p, vec2 size, vec4 radius) {
    vec2 center;
    float r;
    if (p.x < radius.x && p.y < radius.x) {
        r = radius.x;
        center = vec2(r, r);
    } else if (p.x > size.x - radius.y && p.y < radius.y) {
        r = radius.y;
        center = vec2(size.x - r, r);
    } else if (p.x > size.x - radius.z && p.y > size.y - radius.z) {
        r = radius.z;
        center = vec2(size.x - r, size.y - r);
    } else if (p.x < radius.w && p.y > size.y - radius.w) {
        r = radius.w;
        center = vec2(r, size.y - r);
    } else {
        return 1.0;
    }
    return 1.0 - smoothstep(r - 0.5, r + 0.5, distance(p, center));
}

void main() {
    vec4 color = texture2D(tex, v_coords);
#if defined(NO_ALPHA)
    color = vec4(color.rgb, 1.0);
#endif

    vec3 geo = input_to_geo * vec3(v_coords, 1.0);
    if (geo.x < 0.0 || geo.x > 1.0 || geo.y < 0.0 || geo.y > 1.0) {
        color = vec4(0.0);
    } else {
        color = color * rounding(geo.xy * geo_size, geo_size, corner_radius);
    }
    color = color * alpha;

#if defined(DEBUG_FLAGS)
    if (tint == 1.0)
        color = vec4(0.0, 0.2, 0.0, 0.2) + color * 0.8;
#endif

    gl_FragColor = color;
}
"#;

    /// Compiles the clip shader for a GL renderer. Without it (an old or
    /// broken driver) windows are drawn with square bottom corners.
    pub fn init(renderer: &mut GlesRenderer) {
        let program = renderer
            .compile_custom_texture_shader(
                CLIP_SHADER,
                &[
                    UniformName::new("geo_size", UniformType::_2f),
                    UniformName::new("corner_radius", UniformType::_4f),
                    UniformName::new("input_to_geo", UniformType::Matrix3x3),
                ],
            )
            .map_err(|err| warn!("the rounded-corner shader did not compile ({err}); corners stay square"))
            .ok();
        renderer.egl_context().user_data().insert_if_missing(|| ClipProgram(program));
        let filter = renderer
            .compile_custom_texture_shader(
                FILTER_SHADER,
                &[UniformName::new("color_matrix", UniformType::Matrix3x3), UniformName::new("color_offset", UniformType::_3f)],
            )
            .map_err(|err| warn!("the colour filter shader did not compile ({err}); colour filters are not available"))
            .ok();
        renderer.egl_context().user_data().insert_if_missing(|| FilterProgram(filter));
        renderer.egl_context().user_data().insert_if_missing(|| FilterTexture(RefCell::new(None)));
    }

    impl BoswasRenderer for GlesRenderer {
        const ROUNDS_CORNERS: bool = true;
        const FILTERS_COLOR: bool = true;

        fn color_filter_frame<E: RenderElement<Self>>(
            &mut self,
            size: Size<i32, Physical>,
            scale: Scale<f64>,
            elements: &[E],
            clear: Color32F,
        ) -> Option<GlesTexture> {
            let compiled = self.egl_context().user_data().get::<FilterProgram>().is_some_and(|p| p.0.is_some());
            if !compiled || size.w <= 0 || size.h <= 0 {
                return None;
            }
            let kept = self.egl_context().user_data().get::<FilterTexture>().and_then(|t| t.0.borrow_mut().take());
            let mut texture = match kept.filter(|t| t.size() == Size::from((size.w, size.h))) {
                Some(texture) => texture,
                None => Offscreen::<GlesTexture>::create_buffer(self, Fourcc::Abgr8888, Size::from((size.w, size.h)))
                    .map_err(|err| warn!("no texture for the colour filter: {err}"))
                    .ok()?,
            };
            {
                let mut framebuffer = self.bind(&mut texture).map_err(|err| warn!("colour filter: {err}")).ok()?;
                let mut tracker = OutputDamageTracker::new(size, scale, Transform::Normal);
                tracker.render_output(self, &mut framebuffer, 0, elements, clear).map_err(|err| warn!("colour filter: {err:?}")).ok()?;
            }
            if let Some(kept) = self.egl_context().user_data().get::<FilterTexture>() {
                *kept.0.borrow_mut() = Some(texture.clone());
            }
            Some(texture)
        }

        fn draw_filtered(
            frame: &mut Self::Frame<'_, '_>,
            texture: &GlesTexture,
            filter: &ColorMatrix,
            src: Rectangle<f64, BufferCoords>,
            dst: Rectangle<i32, Physical>,
            damage: &[Rectangle<i32, Physical>],
            opaque_regions: &[Rectangle<i32, Physical>],
        ) -> Result<(), Self::Error> {
            let program = frame.egl_context().user_data().get::<FilterProgram>().and_then(|p| p.0.clone());
            let uniforms = [
                Uniform::new(
                    "color_matrix",
                    UniformValue::Matrix3x3 {
                        matrices: vec![filter.columns()],
                        transpose: false,
                    },
                ),
                Uniform::new("color_offset", filter.offset),
            ];
            frame.render_texture_from_to(texture, src, dst, damage, opaque_regions, Transform::Normal, 1.0, program.as_ref(), &uniforms)
        }

        fn draw_clipped(
            frame: &mut Self::Frame<'_, '_>,
            element: &WaylandSurfaceRenderElement<Self>,
            clip: &Clip,
            src: Rectangle<f64, BufferCoords>,
            dst: Rectangle<i32, Physical>,
            damage: &[Rectangle<i32, Physical>],
            opaque_regions: &[Rectangle<i32, Physical>],
        ) -> Result<(), Self::Error> {
            let program = frame
                .egl_context()
                .user_data()
                .get::<ClipProgram>()
                .and_then(|program| program.0.clone());
            let Some(program) = program else {
                return RenderElement::<Self>::draw(element, frame, src, dst, damage, opaque_regions);
            };
            frame.override_default_tex_program(
                program,
                vec![
                    Uniform::new("geo_size", clip.size),
                    Uniform::new("corner_radius", clip.radius),
                    Uniform::new(
                        "input_to_geo",
                        UniformValue::Matrix3x3 {
                            matrices: vec![clip.input_to_geo],
                            transpose: false,
                        },
                    ),
                ],
            );
            let result = RenderElement::<Self>::draw(element, frame, src, dst, damage, opaque_regions);
            frame.clear_tex_program_override();
            result
        }
    }
}

#[cfg(any(feature = "udev", feature = "winit"))]
pub use gles::init as init_gles;

#[cfg(test)]
mod tests {
    use super::*;
    use smithay::utils::Logical;

    fn rect<K>(x: f64, y: f64, w: f64, h: f64) -> Rectangle<f64, K> {
        Rectangle::new(Point::from((x, y)), Size::from((w, h)))
    }

    #[test]
    fn matrices_compose_right_to_left() {
        let m = Mat3::scale(2.0, 4.0).then_after(Mat3::translation(1.0, 1.0));
        assert_eq!(m.apply(0.0, 0.0), (2.0, 4.0), "translate first, then scale");
        let m = Mat3::translation(1.0, 1.0).then_after(Mat3::scale(2.0, 4.0));
        assert_eq!(m.apply(1.0, 1.0), (3.0, 5.0));
    }

    #[test]
    fn a_surface_that_fills_the_rectangle_maps_one_to_one() {
        let clip = Clip::new(
            rect::<Physical>(100.0, 50.0, 400.0, 300.0),
            rect::<Physical>(100.0, 50.0, 400.0, 300.0),
            [0.0, 0.0, 8.0, 8.0],
            (400.0, 300.0),
            rect::<Logical>(0.0, 0.0, 400.0, 300.0),
        )
        .unwrap();
        let m = Mat3(clip.input_to_geo);
        for (x, y) in [(0.0, 0.0), (1.0, 1.0), (0.25, 0.75)] {
            let (gx, gy) = m.apply(x, y);
            assert!((gx - x).abs() < 1e-5 && (gy - y).abs() < 1e-5);
        }
        assert_eq!(clip.size, (400.0, 300.0));
    }

    #[test]
    fn a_subsurface_and_a_viewport_land_at_their_place_in_the_rectangle() {
        // A 100x100 subsurface in the bottom-right quarter of a 400x300
        // client area, showing the right half of a 200x100 buffer.
        let clip = Clip::new(
            rect::<Physical>(300.0, 200.0, 100.0, 100.0),
            rect::<Physical>(0.0, 0.0, 400.0, 300.0),
            [0.0, 0.0, 8.0, 8.0],
            (200.0, 100.0),
            rect::<Logical>(100.0, 0.0, 100.0, 100.0),
        )
        .unwrap();
        let m = Mat3(clip.input_to_geo);
        let (x, y) = m.apply(0.5, 0.0);
        assert!((x - 0.75).abs() < 1e-5 && (y - 2.0 / 3.0).abs() < 1e-5, "the shown part starts at ({x}, {y})");
        let (x, y) = m.apply(1.0, 1.0);
        assert!((x - 1.0).abs() < 1e-5 && (y - 1.0).abs() < 1e-5, "and ends in the corner: ({x}, {y})");
    }

    #[test]
    fn degenerate_geometry_is_not_clipped() {
        let ok = rect::<Physical>(0.0, 0.0, 10.0, 10.0);
        let src = rect::<Logical>(0.0, 0.0, 10.0, 10.0);
        assert!(Clip::new(rect(0.0, 0.0, 0.0, 10.0), ok, [8.0; 4], (10.0, 10.0), src).is_none());
        assert!(Clip::new(ok, rect(0.0, 0.0, 10.0, 0.0), [8.0; 4], (10.0, 10.0), src).is_none());
        assert!(Clip::new(ok, ok, [8.0; 4], (0.0, 10.0), src).is_none());
    }

    #[test]
    fn only_rounded_corners_have_squares() {
        let squares = corner_squares(rect(10.0, 20.0, 200.0, 100.0), [0.0, 0.0, 8.0, 8.0]);
        assert_eq!(squares.len(), 2, "square top corners need no clipping");
        assert_eq!(squares[0], Rectangle::new(Point::from((201, 111)), Size::from((9, 9))));
        assert_eq!(squares[1], Rectangle::new(Point::from((10, 111)), Size::from((9, 9))));
        assert!(corner_squares(rect(0.0, 0.0, 50.0, 50.0), [0.0; 4]).is_empty());
    }

    #[test]
    fn colour_filters_do_what_they_say() {
        let close = |a: [f32; 3], b: [f32; 3]| a.iter().zip(b).all(|(x, y)| (x - y).abs() < 0.01);
        let grey = ColorMatrix::named("grayscale").unwrap();
        let red = grey.apply([1.0, 0.0, 0.0]);
        assert!(red[0] == red[1] && red[1] == red[2], "{red:?}");
        assert!(close(grey.apply([1.0; 3]), [1.0; 3]), "white stays white");
        let inverted = ColorMatrix::named("inverted").unwrap();
        assert!(close(inverted.apply([1.0; 3]), [0.0; 3]));
        assert!(close(inverted.apply([0.2, 0.4, 0.6]), [0.8, 0.6, 0.4]));
        let both = ColorMatrix::named("grayscale_inverted").unwrap();
        let blue = both.apply([0.0, 0.0, 1.0]);
        assert!(blue[0] == blue[1] && blue[0] > 0.9, "dark blue becomes a light grey: {blue:?}");
        for name in ["protanopia", "deuteranopia", "tritanopia"] {
            let filter = ColorMatrix::named(name).unwrap();
            // Greys carry no colour difference to move: they stay as they are.
            for level in [0.0, 0.25, 0.5, 1.0] {
                assert!(close(filter.apply([level; 3]), [level; 3]), "{name} {level}");
            }
            // Red and green, which look alike to a red-green blind person,
            // come apart; so do blue and yellow for blue-yellow blindness.
            let (a, b) = if name == "tritanopia" { ([0.0, 0.0, 1.0], [1.0, 1.0, 0.0]) } else { ([1.0, 0.0, 0.0], [0.0, 1.0, 0.0]) };
            assert_ne!(filter.apply(a), a, "{name} changes the colours it is for");
            assert!(filter.matrix.iter().flatten().all(|v| v.is_finite()));
            assert_ne!(filter.apply(a), filter.apply(b));
        }
        assert!(ColorMatrix::named("sepia").is_none());
        for name in crate::config::COLOR_FILTERS {
            assert!(ColorMatrix::named(name).is_some(), "{name}");
        }
        // GLSL takes the matrix column by column.
        let m = ColorMatrix { matrix: [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0], [7.0, 8.0, 9.0]], offset: [0.0; 3] };
        assert_eq!(m.columns(), [1.0, 4.0, 7.0, 2.0, 5.0, 8.0, 3.0, 6.0, 9.0]);
    }
}
