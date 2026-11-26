//! How long the shell takes to draw, for finding out why the desktop is
//! slow.
//!
//! With `BOSWAS_LOG=info,boswas_shell::stats=debug` every drawing of a
//! surface is logged on a line of its own, target `boswas_shell::stats`:
//! the surface, its size in pixels, the time the drawing itself took and
//! the time until it was handed to the compositor (comparing, converting
//! and copying included), the rectangle sent as changed, and the time
//! since the surface was first marked as changed (by an input event, as a
//! rule), which includes waiting for the compositor's frame callback.
//! Without that level nothing is measured.
use std::{
    cell::Cell,
    time::{Duration, Instant},
};

use tracing::{debug, enabled, Level};

thread_local! {
    /// Whether drawings are logged (asked once).
    static ENABLED: Cell<Option<bool>> = const { Cell::new(None) };
}

/// Whether drawings are measured and logged.
pub fn enabled() -> bool {
    ENABLED.with(|cell| {
        cell.get().unwrap_or_else(|| {
            let on = enabled!(target: "boswas_shell::stats", Level::DEBUG);
            cell.set(Some(on));
            on
        })
    })
}

/// A surface is marked as changed: remembers when, unless it already was.
pub fn changed(since: &mut Option<Instant>) {
    if since.is_none() && enabled() {
        *since = Some(Instant::now());
    }
}

/// A drawing of `surface` (`pixels` wide and high) took `paint` to draw and
/// `total` until it was sent with `damage` (x, y, width, height in buffer
/// pixels; `None`: it changed nothing and was not sent). `since`: when the
/// surface was marked as changed; taken.
pub fn drawn(surface: &dyn std::fmt::Debug, pixels: (u32, u32), paint: Duration, total: Duration, damage: Option<(u32, u32, u32, u32)>, since: &mut Option<Instant>) {
    let since = since.take();
    if !enabled() {
        return;
    }
    let waited = since
        .filter(|_| damage.is_some())
        .map_or_else(String::new, |at| format!(" input_us={}", at.elapsed().as_micros()));
    let damage = damage.map_or_else(|| "none".to_owned(), |(x, y, w, h)| format!("{w}x{h}+{x}+{y}"));
    debug!(
        target: "boswas_shell::stats",
        "draw {surface:?} {}x{} paint_us={} total_us={} damage={damage}{waited}",
        pixels.0,
        pixels.1,
        paint.as_micros(),
        total.as_micros(),
    );
}
