//! The scale Boswas recommends for an output, as Windows recommends one:
//! from how dense the monitor's pixels are (its EDID size in millimetres
//! and the mode's resolution) and how far from it people sit.
//!
//! The pixel density (pixels per inch along the diagonal) is divided by a
//! reference density, and the scale is the largest supported step that the
//! quotient reaches or nearly reaches (within 0.1: Windows leans towards
//! the smaller step, which keeps more room on the screen):
//!
//! * built-in panels (`eDP`, `LVDS`, `DSI` connectors), seen from arm's
//!   length: 110 pixels per inch;
//! * external monitors, seen from farther the larger they are: 95250 /
//!   (152.4 + 2.2 x the picture's height in mm) pixels per inch, about 118
//!   for a 24-inch monitor, 107 for 27 inches and 93 for 32 inches.
//!
//! A scale that would leave less than 1024 x 720 logical pixels (the room
//! the taskbar, Start and Settings are made for) is not recommended: small
//! panels of few pixels stay at 100 %, like Windows keeps them.
//!
//! The result is 100 % where nothing sensible is known: no size, a size
//! that does not fit the picture (an aspect ratio stored instead of the
//! size, a projector's zero, more than 1.6 m wide), or a virtual graphics
//! device (a virtual machine's display has no physical size, whatever its
//! EDID says).

use crate::config::SCALES;

/// DRM drivers of virtual graphics devices.
const VIRTUAL_DRIVERS: [&str; 9] = ["virtio_gpu", "virtio-gpu", "qxl", "bochs", "bochs-drm", "vmwgfx", "vboxvideo", "cirrus", "hyperv_drm"];

/// Whether a connector is a panel built into the device.
pub fn is_built_in(connector: &str) -> bool {
    ["eDP", "LVDS", "DSI"].iter().any(|prefix| connector.starts_with(prefix))
}

/// Whether a DRM driver drives a virtual machine's display.
pub fn is_virtual(driver: &str) -> bool {
    let driver = driver.trim().to_ascii_lowercase();
    VIRTUAL_DRIVERS.iter().any(|name| driver == *name || driver.starts_with(&format!("{name}-")))
}

/// The recommended scale of an output: `connector` its name (`eDP-1`),
/// `physical_mm` its size from the EDID, `mode` the resolution it shows in
/// pixels (as the panel stands), `driver` the DRM driver's name (empty
/// where unknown).
pub fn recommended(connector: &str, physical_mm: (i32, i32), mode: (i32, i32), driver: &str) -> f64 {
    let (w_mm, h_mm) = (f64::from(physical_mm.0), f64::from(physical_mm.1));
    let (w_px, h_px) = (f64::from(mode.0), f64::from(mode.1));
    if is_virtual(driver) || w_mm <= 0.0 || h_mm <= 0.0 || w_px <= 0.0 || h_px <= 0.0 {
        return 1.0;
    }
    // Never more than 1.6 m wide (a projector or a television, seen from
    // the room), and the size must have the picture's shape: some monitors
    // store their aspect ratio (16 x 9 cm) or nonsense instead.
    if w_mm.max(h_mm) > 1600.0 {
        return 1.0;
    }
    let shape = |a: f64, b: f64| a.max(b) / a.min(b);
    if (shape(w_mm, h_mm) / shape(w_px, h_px) - 1.0).abs() > 0.15 {
        return 1.0;
    }
    let built_in = is_built_in(connector);
    let diagonal_in = w_mm.hypot(h_mm) / 25.4;
    if diagonal_in < if built_in { 4.0 } else { 10.0 } {
        return 1.0;
    }
    let density = w_px.hypot(h_px) / diagonal_in;
    let reference = if built_in { 110.0 } else { 95250.0 / (152.4 + 2.2 * w_mm.min(h_mm)) };
    let (long, short) = (w_px.max(h_px), w_px.min(h_px));
    let mut scale = step(density / reference);
    while scale > 1.0 && (long / scale < MIN_LOGICAL.0 - 0.5 || short / scale < MIN_LOGICAL.1 - 0.5) {
        scale = SCALES.iter().copied().filter(|s| *s < scale).fold(1.0, f64::max);
    }
    scale
}

/// The least room a recommended scale leaves, in logical pixels.
const MIN_LOGICAL: (f64, f64) = (1024.0, 720.0);

/// The largest supported scale that `ratio` reaches within 0.1.
fn step(ratio: f64) -> f64 {
    SCALES.iter().copied().filter(|scale| *scale <= ratio + 0.1).fold(1.0, f64::max)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The size in millimetres of a panel of `diagonal` inches with the
    /// shape of `pixels`.
    fn mm(diagonal: f64, pixels: (i32, i32)) -> (i32, i32) {
        let aspect = f64::from(pixels.0) / f64::from(pixels.1);
        let height = diagonal * 25.4 / (1.0 + aspect * aspect).sqrt();
        ((height * aspect).round() as i32, height.round() as i32)
    }

    fn panel(connector: &str, diagonal: f64, pixels: (i32, i32)) -> f64 {
        recommended(connector, mm(diagonal, pixels), pixels, "i915")
    }

    #[test]
    fn built_in_panels_like_windows() {
        const FHD: (i32, i32) = (1920, 1080);
        const HD: (i32, i32) = (1366, 768);
        let table = [
            (13.3, FHD, 1.5),
            (14.0, FHD, 1.5),
            (15.6, FHD, 1.25),
            (13.3, HD, 1.0),
            (15.6, HD, 1.0),
            (14.0, (2560, 1600), 2.0),
            (12.5, FHD, 1.5),
            (17.3, FHD, 1.25),
            (11.6, HD, 1.0),
            (10.1, (1280, 800), 1.0),
            (13.3, (3840, 2160), 2.0),
            (15.6, (3840, 2160), 2.0),
        ];
        for (diagonal, pixels, expected) in table {
            for connector in ["eDP-1", "LVDS-1", "DSI-1"] {
                assert_eq!(panel(connector, diagonal, pixels), expected, "{connector} {diagonal}\" {pixels:?}");
            }
        }
    }

    #[test]
    fn external_monitors_like_windows() {
        let table = [
            (24.0, (1920, 1080), 1.0),
            (21.5, (1920, 1080), 1.0),
            (27.0, (1920, 1080), 1.0),
            (27.0, (2560, 1440), 1.0),
            (32.0, (2560, 1440), 1.0),
            (34.0, (3440, 1440), 1.0),
            (24.0, (3840, 2160), 1.5),
            (27.0, (3840, 2160), 1.5),
            (28.0, (3840, 2160), 1.5),
            (32.0, (3840, 2160), 1.5),
            (27.0, (5120, 2880), 2.0),
        ];
        for (diagonal, pixels, expected) in table {
            for connector in ["DP-1", "HDMI-A-1", "DVI-D-1", "HEADLESS-1"] {
                assert_eq!(panel(connector, diagonal, pixels), expected, "{connector} {diagonal}\" {pixels:?}");
            }
        }
    }

    #[test]
    fn nothing_sensible_known_means_100_percent() {
        let fhd = (1920, 1080);
        // No size, or a projector's zero.
        assert_eq!(recommended("eDP-1", (0, 0), fhd, "i915"), 1.0);
        assert_eq!(recommended("HDMI-A-1", (0, 190), fhd, "amdgpu"), 1.0);
        assert_eq!(recommended("HDMI-A-1", (-5, -5), fhd, "amdgpu"), 1.0);
        // The aspect ratio stored as the size (16 x 9 cm), or a shape that
        // does not fit the picture.
        assert_eq!(recommended("HDMI-A-1", (160, 90), fhd, "amdgpu"), 1.0);
        assert_eq!(recommended("eDP-1", (290, 290), fhd, "i915"), 1.0);
        // A television or a projector screen more than 1.6 m wide.
        assert_eq!(recommended("HDMI-A-1", (1650, 930), (3840, 2160), "amdgpu"), 1.0);
        // A virtual machine, whatever its EDID says.
        for driver in ["virtio_gpu", "qxl", "bochs-drm", "bochs", "vmwgfx", "vboxvideo", "cirrus", "hyperv_drm", " VirtIO_GPU "] {
            assert!(is_virtual(driver), "{driver}");
            assert_eq!(recommended("Virtual-1", mm(13.3, fhd), fhd, driver), 1.0, "{driver}");
        }
        for driver in ["i915", "amdgpu", "nouveau", "simpledrm", "", "virtio"] {
            assert!(!is_virtual(driver), "{driver}");
        }
        // No mode.
        assert_eq!(recommended("eDP-1", mm(13.3, fhd), (0, 0), "i915"), 1.0);
        // A portrait panel (a tablet's DSI panel) is judged as it stands.
        assert_eq!(recommended("DSI-1", (166, 295), (1080, 1920), "msm"), 1.5);
        assert_eq!(recommended("DSI-1", mm(13.3, fhd), (1080, 1920), "msm"), 1.5, "a size given the other way round still has the picture's shape");
        assert!(is_built_in("eDP-1") && is_built_in("LVDS-2") && is_built_in("DSI-1") && !is_built_in("DP-1") && !is_built_in("HDMI-A-1"));
        // Every result is a supported scale.
        for diagonal in [5.0, 8.0, 10.1, 12.0, 14.0, 17.0, 20.0, 24.0, 32.0, 43.0, 55.0] {
            for pixels in [(1280, 720), (1920, 1080), (2560, 1440), (3840, 2160), (2256, 1504)] {
                for connector in ["eDP-1", "DP-1"] {
                    assert!(SCALES.contains(&panel(connector, diagonal, pixels)));
                }
            }
        }
    }
}
