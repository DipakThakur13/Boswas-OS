//! Kernel command line switches for the boot screen.
//!
//! `splash` turns the boot screen on (as it does for Plymouth); `nosplash`,
//! `plymouth.enable=0` or `boswas.splash=off` turn it off and always win;
//! `boswas.splash=static` keeps the screen but stops the animation.

/// What the kernel command line asks for.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct BootParams {
    /// Show the boot screen at all.
    pub splash: bool,
    /// Animate: the ring turns, the caret blinks, a rejected passphrase
    /// shakes the text box.
    pub animate: bool,
}

/// Parse the contents of `/proc/cmdline`.
pub fn parse(cmdline: &str) -> BootParams {
    let mut wanted = false;
    let mut refused = false;
    let mut animate = true;
    for token in cmdline.split_ascii_whitespace() {
        match token {
            "splash" => wanted = true,
            "nosplash" | "plymouth.enable=0" => refused = true,
            _ => {
                if token.starts_with("splash=") {
                    wanted = true;
                } else if let Some(value) = token.strip_prefix("boswas.splash=") {
                    match value {
                        "off" | "0" | "no" | "false" => refused = true,
                        "static" => animate = false,
                        _ => {}
                    }
                }
            }
        }
    }
    BootParams { splash: wanted && !refused, animate }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn splash_is_opt_in() {
        assert!(!parse("BOOT_IMAGE=/vmlinuz root=/dev/mapper/vg-root ro quiet").splash);
        assert!(parse("BOOT_IMAGE=/vmlinuz root=/dev/mapper/vg-root ro quiet splash").splash);
        assert!(parse("boot=live components quiet splash hostname=boswas-device\n").splash);
        assert!(parse("splash=silent").splash);
        assert!(!parse("").splash);
        // Not fooled by look-alikes.
        assert!(!parse("rd.splash nosplashx splashy").splash);
    }

    #[test]
    fn refusals_win_in_any_order() {
        assert!(!parse("splash nosplash").splash);
        assert!(!parse("nosplash splash").splash);
        assert!(!parse("quiet splash plymouth.enable=0").splash);
        assert!(!parse("boswas.splash=off splash").splash);
        assert!(!parse("splash boswas.splash=0").splash);
    }

    #[test]
    fn static_keeps_the_screen_without_animation() {
        let p = parse("quiet splash boswas.splash=static");
        assert!(p.splash && !p.animate);
        assert!(parse("quiet splash").animate);
        assert!(parse("splash boswas.splash=unknown").animate);
    }
}
