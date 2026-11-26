//! Night light turned on or off "now", as Windows does it: with a schedule,
//! the choice holds until the schedule's next change (its start or its
//! end), after which the schedule decides again; without one it is simply
//! the session's switch. The quick settings tile asks for this
//! (`night-light` with `active`).

use crate::config::{clock_span_contains, parse_clock, NightLight};

/// A choice made against the schedule: `on` while the schedule was inside
/// its span (`inside`) or outside it. It ends when that changes.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Override {
    pub on: bool,
    pub inside: bool,
}

/// Whether the clock is inside the schedule at `minutes` after midnight;
/// `None` when no schedule applies (night light is off, or on without a
/// schedule, or the times are not valid).
pub fn schedule_inside(settings: &NightLight, minutes: u32) -> Option<bool> {
    if !settings.enabled || !settings.scheduled || parse_clock(&settings.from).is_none() || parse_clock(&settings.to).is_none() {
        return None;
    }
    Some(clock_span_contains(&settings.from, &settings.to, minutes))
}

/// Whether the screens are tinted at `minutes`.
pub fn active(settings: &NightLight, choice: Option<Override>, minutes: u32) -> bool {
    if !settings.enabled {
        return false;
    }
    match (schedule_inside(settings, minutes), choice) {
        (Some(inside), Some(choice)) if choice.inside == inside => choice.on,
        (Some(inside), _) => inside,
        (None, _) => true,
    }
}

/// Whether a choice still holds at `minutes`: the schedule has not changed
/// since it was made.
pub fn holds(settings: &NightLight, choice: Override, minutes: u32) -> bool {
    schedule_inside(settings, minutes) == Some(choice.inside)
}

/// Turning night light on or off now: the session's switch and the choice
/// against the schedule that follow.
pub fn set_now(settings: &NightLight, on: bool, minutes: u32) -> (bool, Option<Override>) {
    let enabled = settings.enabled || on;
    let switched = NightLight { enabled, ..settings.clone() };
    match schedule_inside(&switched, minutes) {
        Some(inside) => (enabled, (inside != on).then_some(Override { on, inside })),
        // No schedule: on and off are the switch itself.
        None => (on, None),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scheduled(from: &str, to: &str) -> NightLight {
        NightLight {
            enabled: true,
            scheduled: true,
            from: from.into(),
            to: to.into(),
            ..NightLight::default()
        }
    }

    const NOON: u32 = 12 * 60;
    const EVENING: u32 = 21 * 60;

    #[test]
    fn turned_on_by_day_it_stays_on_through_the_evening_and_goes_off_with_the_schedule() {
        let mut settings = scheduled("20:00", "06:00");
        assert!(!active(&settings, None, NOON), "outside the schedule: neutral");
        let (enabled, choice) = set_now(&settings, true, NOON);
        assert!(enabled);
        assert_eq!(choice, Some(Override { on: true, inside: false }));
        assert!(active(&settings, choice, NOON), "on now");
        assert!(active(&settings, choice, 19 * 60 + 59), "until the schedule's next change");
        // At 20:00 the schedule starts: the choice has ended, the schedule
        // (on) decides, and at 06:00 it ends.
        assert!(!holds(&settings, choice.unwrap(), 20 * 60));
        assert!(active(&settings, None, EVENING));
        assert!(!active(&settings, None, 7 * 60));
        settings.enabled = enabled;
    }

    #[test]
    fn turned_off_in_the_evening_it_stays_off_until_the_morning() {
        let settings = scheduled("20:00", "06:00");
        assert!(active(&settings, None, EVENING));
        let (enabled, choice) = set_now(&settings, false, EVENING);
        assert!(enabled, "the switch stays on: the schedule goes on tomorrow");
        let choice = choice.unwrap();
        assert!(!active(&settings, Some(choice), EVENING));
        assert!(!active(&settings, Some(choice), 3 * 60), "past midnight still off");
        assert!(holds(&settings, choice, 5 * 60 + 59));
        assert!(!holds(&settings, choice, 6 * 60), "the schedule's end ends it");
        assert!(active(&settings, None, EVENING), "and the next evening it is on again");
    }

    #[test]
    fn a_choice_the_schedule_agrees_with_is_no_override() {
        let settings = scheduled("20:00", "06:00");
        assert_eq!(set_now(&settings, true, EVENING), (true, None));
        assert_eq!(set_now(&settings, false, NOON), (true, None));
    }

    #[test]
    fn without_a_schedule_it_is_the_switch() {
        let mut settings = NightLight { enabled: false, ..NightLight::default() };
        assert_eq!(set_now(&settings, true, NOON), (true, None));
        settings.enabled = true;
        assert!(active(&settings, None, NOON));
        assert_eq!(set_now(&settings, false, NOON), (false, None));
        // Switched off, with a schedule in the settings: turning it on now
        // switches it on and holds against the schedule.
        let off = NightLight { enabled: false, ..scheduled("20:00", "06:00") };
        assert!(!active(&off, None, EVENING));
        let (enabled, choice) = set_now(&off, true, NOON);
        assert!(enabled && choice == Some(Override { on: true, inside: false }));
        assert_eq!(schedule_inside(&off, EVENING), None, "a switched-off schedule does not apply");
        assert_eq!(schedule_inside(&scheduled("late", "06:00"), EVENING), None, "nor one with a broken time");
    }
}
