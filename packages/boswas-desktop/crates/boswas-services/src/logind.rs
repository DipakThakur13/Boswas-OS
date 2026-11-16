//! Finding this process's logind session.

use zbus::{zvariant::OwnedObjectPath, Connection};

use crate::{
    runtime::{call, get_property},
    var::{as_fields, as_path, as_str},
    Environment,
};

pub(crate) const LOGIN1: &str = "org.freedesktop.login1";
pub(crate) const MANAGER_PATH: &str = "/org/freedesktop/login1";
pub(crate) const MANAGER: &str = "org.freedesktop.login1.Manager";
pub(crate) const SESSION: &str = "org.freedesktop.login1.Session";

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct OwnSession {
    pub id: String,
    pub path: String,
}

/// A session id is a short token logind made up; anything else in
/// `XDG_SESSION_ID` is not passed on.
fn plausible_id(id: &str) -> bool {
    !id.is_empty() && id.len() <= 64 && id.chars().all(|ch| ch.is_ascii_alphanumeric() || ch == '_' || ch == '-')
}

/// The session of this process: the one `XDG_SESSION_ID` names, else the
/// one logind finds for our process id, else the user's display session
/// (a shell started by the user's service manager is in no session scope).
pub(crate) async fn own_session(conn: &Connection, env: &Environment) -> Option<OwnSession> {
    if let Some(id) = env.session_id.as_deref().filter(|id| plausible_id(id)) {
        let path: Result<OwnedObjectPath, _> = call(conn, LOGIN1, MANAGER_PATH, MANAGER, "GetSession", &(id,)).await;
        if let Ok(path) = path {
            return Some(OwnSession { id: id.to_owned(), path: path.to_string() });
        }
    }
    let by_pid: Result<OwnedObjectPath, _> =
        call(conn, LOGIN1, MANAGER_PATH, MANAGER, "GetSessionByPID", &(std::process::id(),)).await;
    if let Ok(path) = by_pid {
        let id = get_property(conn, LOGIN1, path.as_str(), SESSION, "Id").await.ok()?;
        let id = as_str(&id).filter(|id| plausible_id(id))?.to_owned();
        return Some(OwnSession { id, path: path.to_string() });
    }
    let display = get_property(
        conn,
        LOGIN1,
        "/org/freedesktop/login1/user/self",
        "org.freedesktop.login1.User",
        "Display",
    )
    .await
    .ok()?;
    let fields = as_fields(&display)?;
    let id = fields.first().and_then(as_str).filter(|id| plausible_id(id))?.to_owned();
    let path = fields.get(1).and_then(as_path)?.to_owned();
    Some(OwnSession { id, path })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn session_ids_are_tokens() {
        assert!(plausible_id("3"));
        assert!(plausible_id("c12"));
        assert!(plausible_id("_31000"));
        assert!(!plausible_id(""));
        assert!(!plausible_id("../1"));
        assert!(!plausible_id("a b"));
        assert!(!plausible_id(&"9".repeat(65)));
    }
}
