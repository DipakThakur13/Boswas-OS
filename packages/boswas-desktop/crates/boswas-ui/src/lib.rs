//! Boswas UI: the design system shared by the Boswas desktop.
//!
//! * [`tokens`]: colours, radii, type and motion from `desktop/ui/tokens.json`
//! * [`canvas`]: software drawing in logical pixels
//! * [`material`]: mica, acrylic and chrome drawn from the wallpaper
//! * [`text`]: text shaping and rasterisation (feature `text`)
//! * [`icons`]: icon theme lookup and SVG rasterisation (feature `icons`)
//!
//! The crate has no `unsafe` code of its own.
#![forbid(unsafe_code)]

pub mod canvas;
#[cfg(feature = "icons")]
pub mod icons;
pub mod material;
#[cfg(feature = "text")]
pub mod text;
pub mod tokens;

pub use canvas::Canvas;
pub use tokens::{tokens, Rgba, Scheme, Theme};
