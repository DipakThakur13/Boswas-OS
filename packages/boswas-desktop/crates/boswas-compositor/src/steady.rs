//! Steady stacking: what appears in front of the windows does not make the
//! windows and the wallpaper be drawn again.
//!
//! Smithay's damage tracker damages an element whole when its place in the
//! list of visible elements changes, and counts that place from the front.
//! So a flyout, a menu, a tooltip, a toast or the pointer appearing in front
//! of everything moved the wallpaper and every window back by one, and the
//! whole screen was drawn again for one frame (about 13 ms at 2560 x 1440
//! without a GPU) to show a tooltip. The elements in front of the windows are
//! therefore followed by spacers (one transparent pixel each, where nothing
//! opaque covers it) up to a fixed count: the windows and the wallpaper keep
//! their places, and only the spacers, a pixel each, move.

use std::cell::RefCell;

use smithay::{
    backend::renderer::{
        element::{solid::SolidColorRenderElement, Element, Id, Kind},
        utils::CommitCounter,
        Color32F,
    },
    utils::{Physical, Point, Rectangle, Scale, Size},
};

/// Visible elements in front of the windows the picture is filled up to
/// (more than any desktop shows: the pointer, a few layers and menus).
pub const FRONT: usize = 48;

thread_local! {
    /// The spacers' identities, kept so that a spacer is the same element
    /// from one frame to the next.
    static SPACERS: RefCell<Vec<Id>> = const { RefCell::new(Vec::new()) };
}

/// How many of `elements` (front to back) the damage tracker counts as
/// visible on an output of `output` (physical pixels), as it decides it: an
/// element is visible unless it lies off the output or wholly behind the
/// opaque parts of visible elements in front of it. Also returns those
/// opaque parts.
pub fn visible<E: Element>(elements: &[E], output: Rectangle<i32, Physical>, scale: Scale<f64>) -> (usize, Vec<Rectangle<i32, Physical>>) {
    let mut opaque: Vec<Rectangle<i32, Physical>> = Vec::new();
    let mut count = 0;
    for element in elements {
        let geometry = element.geometry(scale);
        let Some(on) = geometry.intersection(output) else {
            continue;
        };
        let rest = Rectangle::subtract_rects_many_in_place(vec![on], opaque.iter().copied());
        if rest.iter().all(|r| r.size.w <= 0 || r.size.h <= 0) {
            continue;
        }
        count += 1;
        opaque.extend(element.opaque_regions(scale).into_iter().filter_map(|mut region| {
            region.loc += geometry.loc;
            region.intersection(output)
        }));
    }
    (count, opaque)
}

/// The spacers that follow `front`, the elements in front of the windows,
/// so that the windows and what is behind them keep their places. None
/// when the front already holds `FRONT` visible elements, or when its opaque
/// parts cover the whole output (then nothing behind it shows anyway).
pub fn spacers<E: Element>(front: &[E], output: Rectangle<i32, Physical>, scale: Scale<f64>) -> Vec<SolidColorRenderElement> {
    let (count, opaque) = visible(front, output, scale);
    let wanted = FRONT.saturating_sub(count);
    if wanted == 0 || output.size.w <= 0 || output.size.h <= 0 {
        return Vec::new();
    }
    let (w, h) = (output.size.w, output.size.h);
    let candidates = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1), (w / 2, h / 2), (w / 2, 0), (0, h / 2)];
    let free = candidates.into_iter().map(|(x, y)| Rectangle::new(Point::from((x, y)) + output.loc, Size::from((1, 1)))).find(|pixel| {
        !Rectangle::subtract_rects_many_in_place(vec![*pixel], opaque.iter().copied()).is_empty()
    });
    let Some(pixel) = free else {
        return Vec::new();
    };
    SPACERS.with(|ids| {
        let mut ids = ids.borrow_mut();
        while ids.len() < wanted {
            ids.push(Id::new());
        }
        ids[..wanted]
            .iter()
            .map(|id| SolidColorRenderElement::new(id.clone(), pixel, CommitCounter::default(), Color32F::TRANSPARENT, Kind::Unspecified))
            .collect()
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn solid(rect: (i32, i32, i32, i32), opaque: bool) -> SolidColorRenderElement {
        let color = if opaque { Color32F::new(0.2, 0.2, 0.2, 1.0) } else { Color32F::new(0.1, 0.1, 0.1, 0.5) };
        SolidColorRenderElement::new(Id::new(), Rectangle::new((rect.0, rect.1).into(), (rect.2, rect.3).into()), CommitCounter::default(), color, Kind::Unspecified)
    }

    #[test]
    fn the_front_is_filled_up_to_a_fixed_count() {
        let output = Rectangle::from_size(Size::from((800, 600)));
        let scale = Scale::from(1.0);
        let taskbar = solid((0, 552, 800, 48), true);
        let flyout = solid((200, 100, 300, 400), false);
        let off_screen = solid((900, 0, 10, 10), false);
        let hidden = solid((10, 560, 20, 20), false);
        let front = vec![flyout.clone(), taskbar.clone(), hidden.clone(), off_screen.clone()];
        assert_eq!(visible(&front, output, scale).0, 2, "off the output and behind the taskbar do not count");
        // With or without the flyout, the front plus its spacers is the same
        // count: what is behind keeps its place.
        for front in [vec![taskbar.clone()], front] {
            let spacers = spacers(&front, output, scale);
            let mut all: Vec<SolidColorRenderElement> = front.clone();
            all.extend(spacers.iter().cloned());
            assert_eq!(visible(&all, output, scale).0, FRONT);
            // A spacer is a transparent pixel where nothing opaque is.
            assert!(spacers.iter().all(|s| s.geometry(scale).size == Size::from((1, 1)) && s.color() == Color32F::TRANSPARENT));
        }
        // The spacers are the same elements from one frame to the next.
        let a = spacers(&[taskbar.clone()], output, scale);
        let b = spacers(&[taskbar.clone()], output, scale);
        assert!(a.iter().zip(&b).all(|(a, b)| a.id() == b.id() && a.geometry(scale) == b.geometry(scale)));
        // Nothing shows behind an opaque cover: no spacers.
        assert!(spacers(&[solid((0, 0, 800, 600), true)], output, scale).is_empty());
    }
}
