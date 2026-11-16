//! Integration tests: the services run against private message buses with
//! stand-ins for the system's daemons, and real clients talk to the names
//! the services own. Nothing here touches the machine's own buses,
//! hardware or network.

mod mocks;
mod support;

mod audio;
mod auth;
mod bluetooth;
mod lifecycle;
mod media;
mod network;
mod notifications;
mod status;
mod tray;
