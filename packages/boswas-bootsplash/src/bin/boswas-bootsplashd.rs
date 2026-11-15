//! `boswas-bootsplashd`: the Boswas OS boot screen daemon.
#![forbid(unsafe_code)]

use boswas_bootsplash::{daemon, launch};

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.first().map(String::as_str) {
        Some("--help") | Some("-h") => {
            print!("{}", daemon::USAGE);
            return;
        }
        Some("--version") => {
            println!("boswas-bootsplashd {}", boswas_bootsplash::VERSION);
            return;
        }
        _ => {}
    }
    let opts = match daemon::parse_args(&args) {
        Ok(opts) => opts,
        Err(reason) => {
            eprintln!("boswas-bootsplashd: {reason}\n{}", daemon::USAGE);
            std::process::exit(2);
        }
    };
    std::process::exit(launch::main(opts, &args));
}
