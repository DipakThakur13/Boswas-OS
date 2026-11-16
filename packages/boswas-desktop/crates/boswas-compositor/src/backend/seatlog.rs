//! libseat's own messages in the compositor's log.
//!
//! libseat tries its backends in turn (seatd, logind, its built-in one) and
//! says nothing unless asked: a seat that logind refused to activate shows
//! up only as a later "Operation not permitted" on the first device. The
//! libseat crate hands the library's messages to the `log` crate; this
//! module passes them on to the compositor's log (target `libseat`, so
//! `BOSWAS_LOG=libseat=debug` shows all of them) and keeps the errors, to
//! name them when the seat turns out to be unusable.

use std::sync::Mutex;

/// Errors kept for the failure message; older ones are dropped.
const KEPT: usize = 4;

static ERRORS: Mutex<Vec<String>> = Mutex::new(Vec::new());

struct Bridge;

impl log::Log for Bridge {
    fn enabled(&self, metadata: &log::Metadata<'_>) -> bool {
        metadata.target().starts_with("libseat")
    }

    fn log(&self, record: &log::Record<'_>) {
        if !self.enabled(record.metadata()) {
            return;
        }
        let message = record.args().to_string();
        let message = message.trim_end();
        match record.level() {
            // An error of one backend is not yet one of the compositor: the
            // next backend may open the seat.
            log::Level::Error => {
                tracing::warn!(target: "libseat", "{message}");
                remember(message);
            }
            log::Level::Warn => tracing::warn!(target: "libseat", "{message}"),
            log::Level::Info => tracing::info!(target: "libseat", "{message}"),
            log::Level::Debug | log::Level::Trace => tracing::debug!(target: "libseat", "{message}"),
        }
    }

    fn flush(&self) {}
}

/// Starts passing libseat's messages on. Call before the seat is opened.
pub fn install() {
    // Another logger can only be there in a test; then that one stays.
    if log::set_logger(&Bridge).is_ok() {
        log::set_max_level(log::LevelFilter::Debug);
    }
}

fn remember(message: &str) {
    let mut errors = ERRORS.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
    if errors.len() == KEPT {
        errors.remove(0);
    }
    errors.push(without_source_location(message).to_owned());
}

/// The errors libseat reported so far, oldest first.
pub fn errors() -> Vec<String> {
    ERRORS.lock().unwrap_or_else(|poisoned| poisoned.into_inner()).clone()
}

/// libseat starts its messages with the source file and line in brackets;
/// a user has no use for them.
fn without_source_location(message: &str) -> &str {
    match message.strip_prefix('[').and_then(|rest| rest.split_once("] ")) {
        Some((location, text)) if location.contains(".c:") => text,
        _ => message,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn source_locations_are_dropped_from_what_the_user_reads() {
        assert_eq!(
            without_source_location("[libseat/backend/logind.c:310] Could not activate session: Access denied"),
            "Could not activate session: Access denied"
        );
        assert_eq!(without_source_location("No backend was able to open a seat"), "No backend was able to open a seat");
        assert_eq!(without_source_location("[not a location] text"), "[not a location] text");
    }

    #[test]
    fn errors_of_libseat_are_kept_and_other_crates_are_not_passed_on() {
        use log::Log;
        let metadata = |target| log::Metadata::builder().target(target).level(log::Level::Error).build();
        assert!(Bridge.enabled(&metadata("libseat::log_handler")));
        assert!(!Bridge.enabled(&metadata("cosmic_text::shape")));
        let before = errors().len();
        Bridge.log(&log::Record::builder().target("cosmic_text").level(log::Level::Error).args(format_args!("x")).build());
        Bridge.log(&log::Record::builder().target("libseat").level(log::Level::Info).args(format_args!("x")).build());
        assert_eq!(errors().len(), before, "only libseat's errors are kept");
        for _ in 0..KEPT + 2 {
            Bridge.log(
                &log::Record::builder()
                    .target("libseat::log_handler")
                    .level(log::Level::Error)
                    .args(format_args!("[libseat/backend/seatd.c:64] Could not connect to socket"))
                    .build(),
            );
        }
        let kept = errors();
        assert_eq!(kept.len(), KEPT);
        assert!(kept.iter().all(|error| error == "Could not connect to socket"), "{kept:?}");
    }
}
