//! Month arithmetic for the calendar under the notification centre.

use chrono::{Datelike, NaiveDate, Weekday};

/// One month as it is shown: six weeks of days.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Month {
    pub year: i32,
    /// 1 to 12.
    pub month: u32,
    /// 42 days, starting on the first day of the week of the row that
    /// holds the 1st.
    pub days: Vec<NaiveDate>,
    /// Short weekday names in column order.
    pub header: [&'static str; 7],
}

/// The first day of the week for a locale name such as `en_IN.UTF-8`.
/// Sunday in the countries that count from Sunday, Monday elsewhere.
pub fn week_start(locale: &str) -> Weekday {
    let territory = locale.split(['.', '@']).next().unwrap_or("").split('_').nth(1).unwrap_or("");
    const SUNDAY: &[&str] = &[
        "US", "CA", "MX", "IN", "JP", "KR", "TW", "HK", "IL", "PH", "BR", "AU", "ZA", "SA", "TH", "PK", "BD", "NP", "CO",
        "PE", "VE", "ZW", "KE", "ID", "SG",
    ];
    if SUNDAY.contains(&territory) {
        Weekday::Sun
    } else {
        Weekday::Mon
    }
}

fn short_name(day: Weekday) -> &'static str {
    match day {
        Weekday::Mon => "Mo",
        Weekday::Tue => "Tu",
        Weekday::Wed => "We",
        Weekday::Thu => "Th",
        Weekday::Fri => "Fr",
        Weekday::Sat => "Sa",
        Weekday::Sun => "Su",
    }
}

/// The month `offset` months away from the month of `today`.
pub fn month(today: NaiveDate, offset: i32, start: Weekday) -> Month {
    let total = today.year() * 12 + today.month0() as i32 + offset;
    let (year, month0) = (total.div_euclid(12), total.rem_euclid(12) as u32);
    let first = NaiveDate::from_ymd_opt(year, month0 + 1, 1).unwrap_or(today);
    let lead = (7 + first.weekday().num_days_from_monday() as i64 - start.num_days_from_monday() as i64) % 7;
    let origin = first - chrono::Duration::days(lead);
    let mut header = [""; 7];
    let mut day = start;
    for name in &mut header {
        *name = short_name(day);
        day = day.succ();
    }
    Month {
        year,
        month: month0 + 1,
        days: (0..42).map(|i| origin + chrono::Duration::days(i)).collect(),
        header,
    }
}

pub fn month_name(month: u32) -> &'static str {
    [
        "January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November",
        "December",
    ][(month.clamp(1, 12) - 1) as usize]
}

#[cfg(test)]
mod tests {
    use super::*;

    fn date(y: i32, m: u32, d: u32) -> NaiveDate {
        NaiveDate::from_ymd_opt(y, m, d).unwrap()
    }

    #[test]
    fn a_month_starts_on_the_right_weekday() {
        // 1 November 2026 is a Sunday.
        let monday = month(date(2026, 11, 15), 0, Weekday::Mon);
        assert_eq!((monday.year, monday.month), (2026, 11));
        assert_eq!(monday.days.len(), 42);
        assert_eq!(monday.days[0], date(2026, 10, 26), "the row with the 1st starts on Monday 26 October");
        assert_eq!(monday.days[6], date(2026, 11, 1));
        assert_eq!(monday.header, ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]);
        let sunday = month(date(2026, 11, 15), 0, Weekday::Sun);
        assert_eq!(sunday.days[0], date(2026, 11, 1));
        assert_eq!(sunday.header[0], "Su");
        assert!(sunday.days.contains(&date(2026, 11, 30)));
    }

    #[test]
    fn offsets_cross_years_in_both_directions() {
        let today = date(2026, 11, 15);
        let next = month(today, 2, Weekday::Mon);
        assert_eq!((next.year, next.month), (2027, 1));
        let before = month(today, -11, Weekday::Mon);
        assert_eq!((before.year, before.month), (2025, 12));
        let far = month(today, -24, Weekday::Mon);
        assert_eq!((far.year, far.month), (2024, 11));
        // February of a leap year is all there.
        let feb = month(date(2028, 1, 31), 1, Weekday::Mon);
        assert!(feb.days.contains(&date(2028, 2, 29)));
        assert_eq!(month_name(feb.month), "February");
    }

    #[test]
    fn the_week_starts_per_locale() {
        assert_eq!(week_start("en_IN.UTF-8"), Weekday::Sun);
        assert_eq!(week_start("en_US"), Weekday::Sun);
        assert_eq!(week_start("en_GB.UTF-8"), Weekday::Mon);
        assert_eq!(week_start("de_DE@euro"), Weekday::Mon);
        assert_eq!(week_start("C"), Weekday::Mon);
        assert_eq!(week_start(""), Weekday::Mon);
    }
}
