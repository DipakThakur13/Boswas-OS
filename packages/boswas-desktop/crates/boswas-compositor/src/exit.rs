//! How the compositor ends, and what the program that started it can read
//! from the exit status (boswas-compositor(1), EXIT STATUS).
//!
//! The login screen's start script falls back to the text login on
//! [`STARTUP`] and [`SHELL`]: both mean that starting the compositor again
//! would end the same way.

use std::fmt;

/// The session ended as asked (a login succeeded, the user signed out).
pub const NORMAL: i32 = 0;
/// Any failure that is none of the others.
pub const ERROR: i32 = 1;
/// The command line was wrong.
pub const USAGE: i32 = 2;
/// The compositor could not start: no usable seat, no graphics device, or
/// (for the login screen) no output.
pub const STARTUP: i32 = 3;
/// The shell kept failing and the compositor gave up; only the login screen
/// does, a desktop session keeps trying.
pub const SHELL: i32 = 4;

/// A failure with an exit status of its own. Its text is the one line that
/// explains the failure on standard error.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Failure {
    /// No seat, no graphics device or no output.
    Startup(String),
    /// The shell kept failing.
    Shell(String),
}

impl Failure {
    pub fn status(&self) -> i32 {
        match self {
            Failure::Startup(_) => STARTUP,
            Failure::Shell(_) => SHELL,
        }
    }
}

impl fmt::Display for Failure {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Failure::Startup(message) | Failure::Shell(message) => f.write_str(message),
        }
    }
}

impl std::error::Error for Failure {}

/// The exit status of an error that ended the compositor.
pub fn status_of(error: &(dyn std::error::Error + 'static)) -> i32 {
    error.downcast_ref::<Failure>().map_or(ERROR, Failure::status)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_way_to_end_has_its_own_status() {
        let statuses = [NORMAL, ERROR, USAGE, STARTUP, SHELL];
        for (i, a) in statuses.iter().enumerate() {
            assert!(statuses[i + 1..].iter().all(|b| a != b), "{statuses:?}");
        }
        // The values are a contract with the login screen's start script.
        assert_eq!((NORMAL, ERROR, STARTUP, SHELL), (0, 1, 3, 4));
    }

    #[test]
    fn failures_carry_their_status_through_a_boxed_error() {
        let startup: Box<dyn std::error::Error> = Failure::Startup("no graphics device".into()).into();
        assert_eq!(status_of(startup.as_ref()), STARTUP);
        assert_eq!(startup.to_string(), "no graphics device");
        let shell: Box<dyn std::error::Error> = Failure::Shell("the login screen kept failing".into()).into();
        assert_eq!(status_of(shell.as_ref()), SHELL);
        let other: Box<dyn std::error::Error> = "XDG_RUNTIME_DIR is not set".into();
        assert_eq!(status_of(other.as_ref()), ERROR);
    }
}
