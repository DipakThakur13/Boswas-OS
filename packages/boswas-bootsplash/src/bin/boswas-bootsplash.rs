//! `boswas-bootsplash`: command line client of the Boswas OS boot screen.
//!
//! Inside the initramfs this binary is also installed as `/bin/plymouth`
//! and then understands the Plymouth commands that the disk-unlock helper
//! and the initramfs scripts use (see `boswas_bootsplash::compat`).
#![forbid(unsafe_code)]

fn main() {
    let mut args = std::env::args();
    let argv0 = args.next().unwrap_or_default();
    let args: Vec<String> = args.collect();
    std::process::exit(boswas_bootsplash::cli::run(&argv0, &args));
}
