//! Accessibility of the keyboard: sticky keys and filter keys
//! (`[keyboard] sticky_keys`, `filter_keys`; Settings, Accessibility >
//! Keyboard).
//!
//! Every key event from a device passes through [`KeyAccess::key`] before
//! the compositor handles it, and comes out as the events that count, in
//! order:
//!
//! * **Filter keys.** A key pressed again within `bounce_keys_ms` of its
//!   release is ignored, press and release (bounce keys). A key counts only
//!   once it has been held for `slow_keys_ms`; released earlier, it never
//!   happened (slow keys). The caller is told when to come back
//!   ([`KeyAccess::due`]) to let a held key through.
//! * **Sticky keys.** Shift, Ctrl and Alt (and AltGr) pressed and released
//!   on their own stay down for the next key: their release is held back
//!   until that key is released. With `sticky_keys_lock`, pressing the same
//!   modifier a second time keeps it down until it is pressed a third time.
//!   A modifier held while another key is pressed works as always. Super
//!   keeps its own behaviour (a tap opens Start).
//!
//! Key codes are XKB key codes (the evdev code plus 8).

use std::{
    collections::{HashMap, HashSet},
    time::{Duration, Instant},
};

use crate::config::Keyboard;

/// XKB key codes of the modifiers sticky keys applies to: left and right
/// Shift, Ctrl, Alt and AltGr.
const STICKY: [u32; 6] = [50, 62, 37, 105, 64, 108];

pub fn is_sticky_modifier(keycode: u32) -> bool {
    STICKY.contains(&keycode)
}

/// What a latched modifier is doing.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Latch {
    /// Down for the next key.
    Once,
    /// Pressed a second time: it locks when that press is released.
    Locking,
    /// Down until pressed again.
    Locked,
    /// Pressed while locked: it comes up when that press is released.
    Unlocking,
}

#[derive(Debug, Default)]
pub struct KeyAccess {
    // Filter keys.
    last_release: HashMap<u32, Instant>,
    /// Presses that were ignored: their releases are ignored too.
    dropped: HashSet<u32>,
    /// A press waiting to have been held long enough.
    slow: Option<(u32, Instant)>,
    // Sticky keys.
    /// Modifiers held down, and whether another key was pressed meanwhile.
    held: Vec<(u32, bool)>,
    /// Modifiers kept down by sticky keys.
    latched: Vec<(u32, Latch)>,
    /// The key whose release lets the latched modifiers go.
    releases_latched: Option<u32>,
}

/// The events that count, and when to call [`KeyAccess::due`].
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Outcome {
    /// Key code and pressed (true) or released.
    pub keys: Vec<(u32, bool)>,
    pub wake: Option<Instant>,
}

impl KeyAccess {
    /// A key event of a device.
    pub fn key(&mut self, config: &Keyboard, keycode: u32, pressed: bool, now: Instant) -> Outcome {
        let mut out = Outcome::default();
        if !config.filter_keys {
            self.slow = None;
            self.dropped.clear();
            self.sticky(config, keycode, pressed, &mut out.keys);
            return out;
        }
        if pressed {
            let bounce = Duration::from_millis(u64::from(config.bounce_keys_ms));
            if config.bounce_keys_ms > 0 && self.last_release.get(&keycode).is_some_and(|at| now.duration_since(*at) < bounce) {
                self.dropped.insert(keycode);
                return out;
            }
            if config.slow_keys_ms > 0 {
                // Another key before the first counted: the first never did.
                if let Some((waiting, _)) = self.slow.take() {
                    self.dropped.insert(waiting);
                }
                let due = now + Duration::from_millis(u64::from(config.slow_keys_ms));
                self.slow = Some((keycode, due));
                out.wake = Some(due);
                return out;
            }
            self.sticky(config, keycode, true, &mut out.keys);
        } else {
            self.last_release.insert(keycode, now);
            if self.last_release.len() > 256 {
                let limit = Duration::from_millis(u64::from(config.bounce_keys_ms.max(1)));
                self.last_release.retain(|_, at| now.duration_since(*at) < limit);
            }
            if self.dropped.remove(&keycode) {
                return out;
            }
            if self.slow.is_some_and(|(waiting, _)| waiting == keycode) {
                self.slow = None;
                return out;
            }
            self.sticky(config, keycode, false, &mut out.keys);
        }
        out
    }

    /// Lets a held slow key through once its time has come.
    pub fn due(&mut self, config: &Keyboard, now: Instant) -> Outcome {
        let mut out = Outcome::default();
        match self.slow {
            Some((keycode, due)) if due <= now => {
                self.slow = None;
                self.sticky(config, keycode, true, &mut out.keys);
            }
            Some((_, due)) => out.wake = Some(due),
            None => {}
        }
        out
    }

    /// The modifiers sticky keys holds down, to let go of when sticky keys
    /// is turned off.
    pub fn release_all(&mut self) -> Vec<(u32, bool)> {
        self.held.clear();
        self.releases_latched = None;
        self.slow = None;
        self.dropped.clear();
        self.latched.drain(..).map(|(keycode, _)| (keycode, false)).collect()
    }

    /// Whether any modifier is kept down by sticky keys.
    pub fn has_latched(&self) -> bool {
        !self.latched.is_empty()
    }

    fn sticky(&mut self, config: &Keyboard, keycode: u32, pressed: bool, keys: &mut Vec<(u32, bool)>) {
        if !config.sticky_keys {
            keys.extend(self.release_all());
            keys.push((keycode, pressed));
            return;
        }
        let latched = self.latched.iter().position(|(code, _)| *code == keycode);
        if is_sticky_modifier(keycode) {
            if pressed {
                match latched.map(|index| (index, self.latched[index].1)) {
                    // Already down for the clients: the press is not passed on.
                    Some((index, Latch::Once)) => {
                        self.latched[index].1 = if config.sticky_keys_lock { Latch::Locking } else { Latch::Unlocking };
                    }
                    Some((index, Latch::Locked)) => self.latched[index].1 = Latch::Unlocking,
                    Some(_) => {}
                    None => {
                        self.held.retain(|(code, _)| *code != keycode);
                        self.held.push((keycode, false));
                        keys.push((keycode, true));
                    }
                }
            } else {
                match latched.map(|index| (index, self.latched[index].1)) {
                    Some((index, Latch::Locking)) => self.latched[index].1 = Latch::Locked,
                    Some((index, Latch::Unlocking)) => {
                        self.latched.remove(index);
                        keys.push((keycode, false));
                    }
                    Some(_) => {}
                    None => {
                        let used = self.held.iter().find(|(code, _)| *code == keycode).is_some_and(|(_, used)| *used);
                        self.held.retain(|(code, _)| *code != keycode);
                        if used {
                            keys.push((keycode, false));
                        } else {
                            // Pressed and released alone: it stays down.
                            self.latched.push((keycode, Latch::Once));
                        }
                    }
                }
            }
            return;
        }
        if pressed {
            for (_, used) in &mut self.held {
                *used = true;
            }
            if self.latched.iter().any(|(_, latch)| *latch == Latch::Once) {
                self.releases_latched = Some(keycode);
            }
            keys.push((keycode, true));
        } else {
            keys.push((keycode, false));
            if self.releases_latched == Some(keycode) {
                self.releases_latched = None;
                self.latched.retain(|(code, latch)| {
                    if *latch == Latch::Once {
                        keys.push((*code, false));
                        false
                    } else {
                        true
                    }
                });
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SHIFT: u32 = 50;
    const CTRL: u32 = 37;
    const A: u32 = 38;
    const B: u32 = 56;

    fn sticky() -> Keyboard {
        Keyboard {
            sticky_keys: true,
            ..Keyboard::default()
        }
    }

    fn run(access: &mut KeyAccess, config: &Keyboard, events: &[(u32, bool)]) -> Vec<(u32, bool)> {
        let now = Instant::now();
        events.iter().flat_map(|(code, pressed)| access.key(config, *code, *pressed, now).keys).collect()
    }

    #[test]
    fn without_either_feature_every_key_passes() {
        let mut access = KeyAccess::default();
        let events = [(SHIFT, true), (SHIFT, false), (A, true), (A, false), (A, true), (A, false)];
        assert_eq!(run(&mut access, &Keyboard::default(), &events), events);
    }

    #[test]
    fn a_modifier_pressed_alone_holds_for_the_next_key() {
        let mut access = KeyAccess::default();
        let config = sticky();
        let out = run(&mut access, &config, &[(SHIFT, true), (SHIFT, false)]);
        assert_eq!(out, [(SHIFT, true)], "the release is held back");
        assert!(access.has_latched());
        let out = run(&mut access, &config, &[(A, true), (A, false)]);
        assert_eq!(out, [(A, true), (A, false), (SHIFT, false)], "Shift+A, then Shift lets go");
        assert!(!access.has_latched());
        let out = run(&mut access, &config, &[(A, true), (A, false)]);
        assert_eq!(out, [(A, true), (A, false)], "only the next key");
    }

    #[test]
    fn two_latched_modifiers_combine_and_a_chord_works_as_always() {
        let mut access = KeyAccess::default();
        let config = sticky();
        let out = run(&mut access, &config, &[(CTRL, true), (CTRL, false), (SHIFT, true), (SHIFT, false), (A, true), (A, false)]);
        assert_eq!(out, [(CTRL, true), (SHIFT, true), (A, true), (A, false), (CTRL, false), (SHIFT, false)]);
        let chord = [(SHIFT, true), (A, true), (A, false), (SHIFT, false)];
        assert_eq!(run(&mut access, &config, &chord), chord, "held while typing: an ordinary Shift");
        assert!(!access.has_latched());
    }

    #[test]
    fn pressing_a_modifier_twice_locks_it_and_a_third_time_frees_it() {
        let mut access = KeyAccess::default();
        let config = sticky();
        let out = run(&mut access, &config, &[(SHIFT, true), (SHIFT, false), (SHIFT, true), (SHIFT, false)]);
        assert_eq!(out, [(SHIFT, true)]);
        let out = run(&mut access, &config, &[(A, true), (A, false), (B, true), (B, false)]);
        assert_eq!(out, [(A, true), (A, false), (B, true), (B, false)], "Shift stays down for every key");
        let out = run(&mut access, &config, &[(SHIFT, true), (SHIFT, false)]);
        assert_eq!(out, [(SHIFT, false)]);
        assert!(!access.has_latched());
        // Without locking the second press frees it.
        let config = Keyboard { sticky_keys_lock: false, ..sticky() };
        let out = run(&mut access, &config, &[(SHIFT, true), (SHIFT, false), (SHIFT, true), (SHIFT, false)]);
        assert_eq!(out, [(SHIFT, true), (SHIFT, false)]);
    }

    #[test]
    fn turning_sticky_keys_off_lets_go_of_what_it_holds() {
        let mut access = KeyAccess::default();
        run(&mut access, &sticky(), &[(SHIFT, true), (SHIFT, false)]);
        let out = run(&mut access, &Keyboard::default(), &[(A, true), (A, false)]);
        assert_eq!(out, [(SHIFT, false), (A, true), (A, false)]);
    }

    #[test]
    fn bounce_keys_ignore_a_key_pressed_again_too_soon() {
        let mut access = KeyAccess::default();
        let config = Keyboard { filter_keys: true, slow_keys_ms: 0, bounce_keys_ms: 500, ..Keyboard::default() };
        let start = Instant::now();
        assert_eq!(access.key(&config, A, true, start).keys, [(A, true)]);
        assert_eq!(access.key(&config, A, false, start + Duration::from_millis(50)).keys, [(A, false)]);
        assert!(access.key(&config, A, true, start + Duration::from_millis(200)).keys.is_empty(), "a bounce");
        assert!(access.key(&config, A, false, start + Duration::from_millis(250)).keys.is_empty(), "and its release");
        assert_eq!(access.key(&config, B, true, start + Duration::from_millis(260)).keys, [(B, true)], "other keys are not held back");
        assert_eq!(access.key(&config, A, true, start + Duration::from_millis(900)).keys, [(A, true)], "later it counts");
    }

    #[test]
    fn slow_keys_count_only_when_held() {
        let mut access = KeyAccess::default();
        let config = Keyboard { filter_keys: true, slow_keys_ms: 300, bounce_keys_ms: 0, ..Keyboard::default() };
        let start = Instant::now();
        let out = access.key(&config, A, true, start);
        assert!(out.keys.is_empty());
        assert_eq!(out.wake, Some(start + Duration::from_millis(300)));
        assert!(access.key(&config, A, false, start + Duration::from_millis(100)).keys.is_empty(), "too brief: nothing");
        assert!(access.due(&config, start + Duration::from_millis(400)).keys.is_empty());
        access.key(&config, B, true, start + Duration::from_secs(1));
        assert_eq!(access.due(&config, start + Duration::from_millis(1200)).wake, Some(start + Duration::from_millis(1300)), "not yet");
        assert_eq!(access.due(&config, start + Duration::from_millis(1300)).keys, [(B, true)]);
        assert_eq!(access.key(&config, B, false, start + Duration::from_secs(2)).keys, [(B, false)]);
        // A second key before the first counted: the first never happened.
        access.key(&config, A, true, start + Duration::from_secs(3));
        access.key(&config, B, true, start + Duration::from_millis(3100));
        assert!(access.key(&config, A, false, start + Duration::from_millis(3200)).keys.is_empty());
        assert_eq!(access.due(&config, start + Duration::from_millis(3400)).keys, [(B, true)]);
    }

    #[test]
    fn filter_and_sticky_keys_work_together() {
        let mut access = KeyAccess::default();
        let config = Keyboard { filter_keys: true, slow_keys_ms: 0, bounce_keys_ms: 300, ..sticky() };
        let start = Instant::now();
        let mut keys = Vec::new();
        for (offset, code, pressed) in [(0, SHIFT, true), (10, SHIFT, false), (50, SHIFT, true), (60, SHIFT, false), (500, A, true), (510, A, false)] {
            keys.extend(access.key(&config, code, pressed, start + Duration::from_millis(offset)).keys);
        }
        assert_eq!(keys, [(SHIFT, true), (A, true), (A, false), (SHIFT, false)], "the bounced Shift does not lock it");
    }
}
