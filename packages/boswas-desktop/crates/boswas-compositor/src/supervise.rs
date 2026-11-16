//! When a shell that has ended is started again, and when no more.
//!
//! A shell that ends once is started again at once. One that keeps ending
//! right after it started is started again after a pause that doubles each
//! time, so a broken shell neither spins the machine nor is given up on: a
//! desktop session has the user's applications in it and stays. The login
//! screen has nothing but its shell, so there the compositor gives up after
//! a few tries and lets its caller show a text login instead.

use std::time::{Duration, Instant};

/// A shell that ran at least this long was working; its end is not counted
/// as a failed start.
const SETTLED: Duration = Duration::from_secs(30);
/// Failed starts in a row after which the login screen is given up.
const LOGIN_SCREEN_TRIES: u32 = 3;
/// The longest pause between two starts.
const LONGEST_PAUSE: Duration = Duration::from_secs(60);

/// What to do about a shell that is not running.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Next {
    /// Start it now.
    Start,
    /// Start it later; `Some(pause)` the first time this is said after an
    /// end (the moment to tell the log), `None` while the pause lasts.
    Wait(Option<Duration>),
    /// Stop trying: it failed this many times in a row.
    GiveUp(u32),
}

/// Follows the starts and ends of one supervised program.
#[derive(Debug)]
pub struct Supervisor {
    /// Without the program there is nothing to show (the login screen).
    essential: bool,
    /// When it was last started.
    started: Option<Instant>,
    /// Starts in a row that ended before [`SETTLED`].
    failures: u32,
    /// The end has been noticed; the next start is due then.
    retry_at: Option<Instant>,
}

impl Supervisor {
    pub fn new(essential: bool) -> Self {
        Self {
            essential,
            started: None,
            failures: 0,
            retry_at: None,
        }
    }

    /// The program was started (or the attempt was made) at `now`.
    pub fn started(&mut self, now: Instant) {
        self.started = Some(now);
        self.retry_at = None;
    }

    /// Failed starts in a row so far.
    pub fn failures(&self) -> u32 {
        self.failures
    }

    /// The program is found not running at `now`: what to do.
    pub fn not_running(&mut self, now: Instant) -> Next {
        let Some(started) = self.started else {
            return Next::Start;
        };
        if let Some(due) = self.retry_at {
            return if now >= due { Next::Start } else { Next::Wait(None) };
        }
        // Its end is noticed now.
        if now.saturating_duration_since(started) >= SETTLED {
            self.failures = 0;
            return Next::Start;
        }
        self.failures += 1;
        if self.essential && self.failures >= LOGIN_SCREEN_TRIES {
            return Next::GiveUp(self.failures);
        }
        let pause = pause_after(self.failures);
        if pause.is_zero() {
            return Next::Start;
        }
        self.retry_at = Some(now + pause);
        Next::Wait(Some(pause))
    }
}

/// The pause before the next start after `failures` failed starts in a row:
/// none after the first, then 2 s, 4 s, 8 s ... up to a minute.
pub fn pause_after(failures: u32) -> Duration {
    if failures < 2 {
        return Duration::ZERO;
    }
    let seconds = 1u64.checked_shl(failures - 1).unwrap_or(u64::MAX);
    Duration::from_secs(seconds).min(LONGEST_PAUSE)
}

#[cfg(test)]
mod tests {
    use super::*;

    const SECOND: Duration = Duration::from_secs(1);

    #[test]
    fn the_first_start_and_a_single_end_need_no_pause() {
        let t = Instant::now();
        let mut shell = Supervisor::new(false);
        assert_eq!(shell.not_running(t), Next::Start);
        shell.started(t);
        // It crashed after a second: started again at once.
        assert_eq!(shell.not_running(t + SECOND), Next::Start);
        assert_eq!(shell.failures(), 1);
    }

    #[test]
    fn a_desktop_shell_is_retried_for_ever_with_a_growing_pause() {
        let mut now = Instant::now();
        let mut shell = Supervisor::new(false);
        shell.started(now);
        let mut pauses = Vec::new();
        for _ in 0..12 {
            now += SECOND;
            match shell.not_running(now) {
                Next::Start => pauses.push(0),
                Next::Wait(Some(pause)) => {
                    pauses.push(pause.as_secs());
                    // Asked again during the pause: still waiting, and
                    // nothing more to log.
                    assert_eq!(shell.not_running(now + pause - SECOND), Next::Wait(None));
                    now += pause;
                    assert_eq!(shell.not_running(now), Next::Start);
                }
                other => panic!("a desktop shell is never given up: {other:?}"),
            }
            shell.started(now);
        }
        assert_eq!(pauses, [0, 2, 4, 8, 16, 32, 60, 60, 60, 60, 60, 60]);
    }

    #[test]
    fn a_shell_that_ran_for_a_while_starts_afresh() {
        let mut now = Instant::now();
        let mut shell = Supervisor::new(false);
        shell.started(now);
        for _ in 0..4 {
            now += SECOND;
            if let Next::Wait(Some(pause)) = shell.not_running(now) {
                now += pause;
            }
            shell.started(now);
        }
        assert_eq!(shell.failures(), 4);
        // This time it works, and ends an hour later.
        now += Duration::from_secs(3600);
        assert_eq!(shell.not_running(now), Next::Start);
        assert_eq!(shell.failures(), 0);
        shell.started(now);
        assert_eq!(shell.not_running(now + SECOND), Next::Start, "one early end after that is the first again");
    }

    #[test]
    fn the_login_screen_is_given_up_after_three_failed_starts() {
        let mut now = Instant::now();
        let mut greeter = Supervisor::new(true);
        assert_eq!(greeter.not_running(now), Next::Start);
        greeter.started(now);
        now += SECOND;
        assert_eq!(greeter.not_running(now), Next::Start);
        greeter.started(now);
        now += SECOND;
        assert_eq!(greeter.not_running(now), Next::Wait(Some(2 * SECOND)));
        now += 2 * SECOND;
        assert_eq!(greeter.not_running(now), Next::Start);
        greeter.started(now);
        now += SECOND;
        assert_eq!(greeter.not_running(now), Next::GiveUp(3));
    }

    #[test]
    fn a_login_screen_that_worked_is_not_given_up_for_one_end() {
        let now = Instant::now();
        let mut greeter = Supervisor::new(true);
        greeter.started(now);
        // It ran for ten minutes before it ended (a crash, or greetd
        // restarted it): one more try from the beginning.
        let later = now + Duration::from_secs(600);
        assert_eq!(greeter.not_running(later), Next::Start);
        assert_eq!(greeter.failures(), 0);
    }

    #[test]
    fn pauses_double_and_stop_at_a_minute() {
        let seconds: Vec<u64> = (0..10).map(|n| pause_after(n).as_secs()).collect();
        assert_eq!(seconds, [0, 0, 2, 4, 8, 16, 32, 60, 60, 60]);
        assert_eq!(pause_after(u32::MAX), LONGEST_PAUSE);
    }
}
