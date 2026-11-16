//! The polkit authentication agent: a stand-in authority calls it, and a
//! shell script stands in for `polkit-agent-helper-1`.

use std::{collections::HashMap, os::unix::fs::MetadataExt, thread::JoinHandle};

use boswas_services::{AuthProgress, AuthRequest, Command, Event, Secret, Services};
use zbus::zvariant::{OwnedValue, Value};

use crate::{mocks::*, support::*};

/// Not the uid the tests run as, whatever that is.
const OTHER_UID: u32 = 4242;

fn own_uid() -> u32 {
    std::fs::metadata("/proc/self").unwrap().uid()
}

/// A helper that asks for a password and accepts "right". It records the
/// user, the cookie and every answer it was given.
fn password_helper(dir: &str) -> String {
    format!(
        r#"echo $$ >> {dir}/helper.pids
read cookie
echo "user=$1 cookie=$cookie" >> {dir}/helper.log
echo "PAM_PROMPT_ECHO_OFF Password: "
read answer
echo "answer=$answer" >> {dir}/helper.log
if [ "$answer" = "right" ]; then
  echo "SUCCESS"
else
  echo "PAM_ERROR_MSG Sorry, try again."
  echo "FAILURE"
fi"#
    )
}

struct Setup {
    harness: Harness,
    polkit: Peer,
    _logind: Peer,
    services: Option<Services>,
    /// The agent: (bus name, object path).
    agent: (String, String),
}

impl Setup {
    fn new(helper: Option<&str>) -> Setup {
        let harness = Harness::new();
        std::fs::write(
            harness.path("users"),
            format!("tester:x:{}:100::/home/tester:/bin/sh\nada:x:{OTHER_UID}:100::/home/ada:/bin/sh\n", own_uid()),
        )
        .unwrap();
        let dir = harness.dir.path().display().to_string();
        match helper {
            Some(body) => harness.script("polkit-agent-helper-1", body),
            None => harness.script("polkit-agent-helper-1", &password_helper(&dir)),
        };
        let polkit = polkit(&harness.system);
        let logind = logind(&harness.system);
        let services = harness.start(only(|config| config.auth_agent = true));
        let registration = polkit.wait_call("RegisterAuthenticationAgent");
        let ((kind, details), _locale, object): ((String, HashMap<String, OwnedValue>), String, String) = registration.args();
        assert_eq!(kind, "unix-session");
        assert_eq!(<&str>::try_from(&*details["session-id"]).unwrap(), "7");
        Setup { harness, polkit, _logind: logind, services: Some(services), agent: (registration.sender, object) }
    }

    fn services(&self) -> &Services {
        self.services.as_ref().unwrap()
    }

    fn dir(&self) -> String {
        self.harness.dir.path().display().to_string()
    }

    /// polkit asks the agent to authenticate; the call returns when the
    /// attempt is over, so it runs on a thread of its own.
    fn begin(&self, cookie: &str) -> JoinHandle<Result<(), String>> {
        self.begin_as(&self.polkit, cookie, &[OTHER_UID, own_uid()])
    }

    fn begin_as(&self, caller: &Peer, cookie: &str, uids: &[u32]) -> JoinHandle<Result<(), String>> {
        let conn = caller.conn.clone();
        let (agent, object) = self.agent.clone();
        let cookie = cookie.to_owned();
        let identities: Vec<(String, HashMap<String, Value<'static>>)> =
            uids.iter().map(|uid| ("unix-user".to_owned(), HashMap::from([("uid".to_owned(), Value::U32(*uid))]))).collect();
        std::thread::spawn(move || {
            let details: HashMap<&str, &str> = HashMap::from([("polkit.message", "raw")]);
            let body = ("org.example.settings.change", "Authentication is needed to change the settings", "preferences-system", details, cookie.as_str(), identities);
            match conn.call_method(Some(agent.as_str()), object.as_str(), Some(POLKIT_AGENT), "BeginAuthentication", &body) {
                Ok(_) => Ok(()),
                Err(zbus::Error::MethodError(name, _, _)) => Err(name.to_string()),
                Err(other) => Err(other.to_string()),
            }
        })
    }

    fn request(&self, cookie: &str) -> AuthRequest {
        self.harness.events.wait("the authentication request", |events| {
            events.iter().find_map(|event| match event {
                Event::AuthRequest(request) if request.cookie == cookie => Some(request.clone()),
                _ => None,
            })
        })
    }

    /// The progress events of `cookie` so far.
    fn progress(&self, cookie: &str) -> Vec<AuthProgress> {
        self.harness
            .events
            .all()
            .into_iter()
            .filter_map(|event| match event {
                Event::AuthProgress { cookie: of, progress } if of == cookie => Some(progress),
                _ => None,
            })
            .collect()
    }

    /// Waits until `cookie` has `count` progress events and returns them.
    fn progress_of(&self, cookie: &str, count: usize) -> Vec<AuthProgress> {
        self.harness.events.wait("authentication progress", |events| {
            let progress: Vec<AuthProgress> = events
                .iter()
                .filter_map(|event| match event {
                    Event::AuthProgress { cookie: of, progress } if of == cookie => Some(progress.clone()),
                    _ => None,
                })
                .collect();
            (progress.len() >= count).then_some(progress)
        })
    }

    fn progress_count(&self, cookie: &str) -> usize {
        self.harness.events.count(|event| matches!(event, Event::AuthProgress { cookie: of, .. } if of == cookie))
    }

    fn respond(&self, cookie: &str, answer: &str) {
        self.services().send(Command::AuthRespond { cookie: cookie.into(), response: Secret::new(answer.into()) });
    }

    fn helper_log(&self) -> Vec<String> {
        std::fs::read_to_string(self.harness.path("helper.log")).unwrap_or_default().lines().map(str::to_owned).collect()
    }

    fn helper_pids(&self) -> Vec<u32> {
        std::fs::read_to_string(self.harness.path("helper.pids")).unwrap_or_default().lines().filter_map(|line| line.parse().ok()).collect()
    }
}

fn prompt(text: &str, echo: bool) -> AuthProgress {
    AuthProgress::Prompt { text: text.into(), echo }
}

fn alive(pid: u32) -> bool {
    match std::fs::read_to_string(format!("/proc/{pid}/stat")) {
        Ok(stat) => !stat.rsplit(')').next().unwrap_or("").trim_start().starts_with('Z'),
        Err(_) => false,
    }
}

#[test]
fn the_agent_registers_for_the_session_and_unregisters_on_drop() {
    let mut setup = Setup::new(None);
    assert_eq!(setup.agent.1, "/org/boswas/shell/AuthenticationAgent");
    drop(setup.services.take());
    let call = setup.polkit.wait_call("UnregisterAuthenticationAgent");
    let ((kind, details), object): ((String, HashMap<String, OwnedValue>), String) = call.args();
    assert_eq!(kind, "unix-session");
    assert_eq!(<&str>::try_from(&*details["session-id"]).unwrap(), "7");
    assert_eq!(object, setup.agent.1);
}

#[test]
fn a_wrong_password_can_be_retried_and_the_right_one_authorizes() {
    let setup = Setup::new(None);
    let call = setup.begin("cookie-1");
    let request = setup.request("cookie-1");
    assert_eq!(
        request,
        AuthRequest {
            cookie: "cookie-1".into(),
            action_id: "org.example.settings.change".into(),
            message: "Authentication is needed to change the settings".into(),
            icon_name: "preferences-system".into(),
            // The session's own user first.
            users: vec!["tester".into(), "ada".into()],
        }
    );
    assert_eq!(setup.progress_of("cookie-1", 1), [prompt("Password:", false)]);

    setup.respond("cookie-1", "wrong");
    let progress = setup.progress_of("cookie-1", 4);
    assert_eq!(
        progress[1..],
        [
            AuthProgress::Info("Sorry, try again.".into()),
            // The attempt failed and the request is still open: the
            // helper asks again.
            AuthProgress::Failed("Sorry, try again.".into()),
            prompt("Password:", false),
        ]
    );
    assert!(!call.is_finished(), "polkit's call returns only when the attempt is over");

    setup.respond("cookie-1", "right");
    let progress = setup.progress_of("cookie-1", 5);
    assert_eq!(progress[4], AuthProgress::Done { authorized: true });
    assert_eq!(call.join().unwrap(), Ok(()));
    // The helper authenticated the session's user and was given the
    // cookie, both times.
    assert_eq!(
        setup.helper_log(),
        ["user=tester cookie=cookie-1", "answer=wrong", "user=tester cookie=cookie-1", "answer=right"]
    );
    // An answer after the end goes nowhere.
    setup.respond("cookie-1", "late");
    setup.services().send(Command::AuthCancel { cookie: "cookie-1".into() });
    let again = setup.begin("cookie-2");
    setup.request("cookie-2");
    setup.progress_of("cookie-2", 1);
    setup.respond("cookie-2", "right");
    assert_eq!(again.join().unwrap(), Ok(()));
    assert_eq!(setup.progress_count("cookie-1"), 5);
    assert!(!setup.helper_log().contains(&"answer=late".to_owned()));
}

#[test]
fn the_shell_can_cancel_and_the_helper_is_killed() {
    let setup = Setup::new(None);
    let call = setup.begin("cookie-c");
    setup.request("cookie-c");
    setup.progress_of("cookie-c", 1);
    let helper = setup.helper_pids()[0];
    assert!(alive(helper));
    // Cancelling something else changes nothing.
    setup.services().send(Command::AuthCancel { cookie: "another".into() });
    setup.services().send(Command::AuthCancel { cookie: "cookie-c".into() });
    assert_eq!(call.join().unwrap(), Err("org.freedesktop.PolicyKit1.Error.Cancelled".to_owned()));
    assert_eq!(setup.progress_of("cookie-c", 2)[1], AuthProgress::Done { authorized: false });
    eventually("the helper to be killed", || !alive(helper));
    assert_eq!(setup.helper_log(), ["user=tester cookie=cookie-c"]);
}

#[test]
fn the_person_can_choose_which_of_the_offered_users_authenticates() {
    let setup = Setup::new(None);
    let call = setup.begin("cookie-u");
    assert_eq!(setup.request("cookie-u").users, ["tester", "ada"]);
    assert_eq!(setup.progress_of("cookie-u", 1), [prompt("Password:", false)]);
    let first = setup.helper_pids()[0];
    assert!(alive(first));

    // Another of the offered users: the helper for the first is killed
    // and one for the chosen user asks anew, for the same request.
    setup.services().send(Command::AuthChooseUser { cookie: "cookie-u".into(), user: "ada".into() });
    assert_eq!(setup.progress_of("cookie-u", 2), [prompt("Password:", false), prompt("Password:", false)]);
    eventually("the first helper to be killed", || !alive(first));
    assert_eq!(setup.helper_log(), ["user=tester cookie=cookie-u", "user=ada cookie=cookie-u"]);
    assert!(!call.is_finished());

    // A user polkit did not offer, a user name that is an option, and a
    // request that is not the one shown: nothing is started for them.
    setup.services().send(Command::AuthChooseUser { cookie: "cookie-u".into(), user: "root".into() });
    setup.services().send(Command::AuthChooseUser { cookie: "cookie-u".into(), user: "--help".into() });
    setup.services().send(Command::AuthChooseUser { cookie: "cookie-u".into(), user: String::new() });
    setup.services().send(Command::AuthChooseUser { cookie: "another".into(), user: "tester".into() });
    // The answer still goes to the helper of the chosen user.
    setup.respond("cookie-u", "right");
    assert_eq!(call.join().unwrap(), Ok(()));
    assert_eq!(setup.progress_of("cookie-u", 3)[2], AuthProgress::Done { authorized: true });
    assert_eq!(setup.helper_log(), ["user=tester cookie=cookie-u", "user=ada cookie=cookie-u", "answer=right"]);
    assert_eq!(setup.helper_pids().len(), 2);

    // Choosing back and forth, and choosing after a failed attempt.
    let call = setup.begin("cookie-v");
    setup.request("cookie-v");
    setup.progress_of("cookie-v", 1);
    setup.respond("cookie-v", "wrong");
    setup.progress_of("cookie-v", 4);
    setup.services().send(Command::AuthChooseUser { cookie: "cookie-v".into(), user: "ada".into() });
    setup.progress_of("cookie-v", 5);
    setup.services().send(Command::AuthChooseUser { cookie: "cookie-v".into(), user: "tester".into() });
    setup.progress_of("cookie-v", 6);
    setup.respond("cookie-v", "right");
    assert_eq!(call.join().unwrap(), Ok(()));
    let log = setup.helper_log();
    assert_eq!(log[log.len() - 2..], ["user=tester cookie=cookie-v", "answer=right"]);
    // After the end the command does nothing.
    setup.services().send(Command::AuthChooseUser { cookie: "cookie-v".into(), user: "ada".into() });
}

#[test]
fn polkit_can_withdraw_a_request() {
    let setup = Setup::new(None);
    let call = setup.begin("cookie-w");
    setup.request("cookie-w");
    setup.progress_of("cookie-w", 1);
    let helper = setup.helper_pids()[0];
    setup.polkit.conn.call_method(Some(setup.agent.0.as_str()), setup.agent.1.as_str(), Some(POLKIT_AGENT), "CancelAuthentication", &("cookie-w",)).unwrap();
    assert_eq!(setup.progress_of("cookie-w", 2)[1], AuthProgress::Done { authorized: false });
    assert!(call.join().unwrap().is_err());
    eventually("the helper to be killed", || !alive(helper));
}

#[test]
fn requests_are_shown_one_at_a_time() {
    let setup = Setup::new(None);
    let first = setup.begin("cookie-a");
    setup.request("cookie-a");
    setup.progress_of("cookie-a", 1);
    let second = setup.begin("cookie-b");
    let third = setup.begin("cookie-x");
    // The same cookie twice is refused.
    setup.polkit.wait_call_n("RegisterAuthenticationAgent", 1);
    let duplicate = setup.begin("cookie-a");
    assert_eq!(duplicate.join().unwrap(), Err("org.freedesktop.PolicyKit1.Error.Failed".to_owned()));
    // polkit withdraws one that waits: it is never shown.
    eventually("the third request to be queued", || {
        let _ = setup.polkit.conn.call_method(Some(setup.agent.0.as_str()), setup.agent.1.as_str(), Some(POLKIT_AGENT), "CancelAuthentication", &("cookie-x",));
        third.is_finished()
    });
    assert_eq!(setup.harness.events.count(|event| matches!(event, Event::AuthRequest(_))), 1);

    setup.respond("cookie-b", "right");
    setup.respond("cookie-a", "right");
    assert_eq!(first.join().unwrap(), Ok(()));
    // Now the second one is shown, with a helper of its own.
    setup.request("cookie-b");
    setup.progress_of("cookie-b", 1);
    setup.respond("cookie-b", "right");
    assert_eq!(second.join().unwrap(), Ok(()));
    assert!(third.join().unwrap().is_err());
    assert_eq!(setup.harness.events.count(|event| matches!(event, Event::AuthRequest(_))), 2);
    assert_eq!(setup.progress_count("cookie-x"), 0);
    assert_eq!(
        setup.helper_log(),
        ["user=tester cookie=cookie-a", "answer=right", "user=tester cookie=cookie-b", "answer=right"]
    );
}

#[test]
fn only_polkit_may_ask_for_a_password() {
    let setup = Setup::new(None);
    let impostor = Peer::start(&setup.harness.system, &[]);
    let call = setup.begin_as(&impostor, "cookie-evil", &[own_uid()]);
    assert_eq!(call.join().unwrap(), Err("org.freedesktop.PolicyKit1.Error.NotAuthorized".to_owned()));
    let cancel = impostor.conn.call_method(Some(setup.agent.0.as_str()), setup.agent.1.as_str(), Some(POLKIT_AGENT), "CancelAuthentication", &("cookie-evil",));
    assert!(cancel.is_err());
    // The real one still works, and the impostor cannot cancel it.
    let call = setup.begin("cookie-real");
    setup.request("cookie-real");
    setup.progress_of("cookie-real", 1);
    let _ = impostor.conn.call_method(Some(setup.agent.0.as_str()), setup.agent.1.as_str(), Some(POLKIT_AGENT), "CancelAuthentication", &("cookie-real",));
    setup.respond("cookie-real", "right");
    assert_eq!(call.join().unwrap(), Ok(()));
    assert_eq!(setup.harness.events.count(|event| matches!(event, Event::AuthRequest(_))), 1);
    assert!(setup.helper_pids().len() == 1);
}

#[test]
fn prompts_with_echo_and_messages_are_passed_on_and_answers_are_one_line() {
    let helper = |dir: &str| {
        format!(
            r#"read cookie
printf '%s\n' 'PAM_TEXT_INFO Touch the\nsensor'
printf '%s\n' 'PAM_PROMPT_ECHO_ON Code\tfor caf\303\251: '
read answer
echo "first=$answer" >> {dir}/helper.log
echo "PAM_PROMPT_ECHO_OFF Password: "
read answer
echo "second=$answer" >> {dir}/helper.log
echo "some debugging chatter"
echo "SUCCESS""#
        )
    };
    // The helper's text needs the scratch directory, which exists only
    // once the setup does: write a placeholder first, then the real one.
    let setup = Setup::new(Some("exit 1"));
    setup.harness.script("polkit-agent-helper-1", &helper(&setup.dir()));
    let call = setup.begin("cookie-e");
    setup.request("cookie-e");
    let progress = setup.progress_of("cookie-e", 2);
    assert_eq!(progress, [AuthProgress::Info("Touch the\nsensor".into()), prompt("Code for café:", true)]);
    // Before the helper asked again, a second answer is not taken as the
    // answer to the next question; neither is the rest of a multi-line one.
    setup.respond("cookie-e", "123456\nSUCCESS\nhunter2");
    setup.respond("cookie-e", "too early");
    let progress = setup.progress_of("cookie-e", 3);
    assert_eq!(progress[2], prompt("Password:", false));
    eventually("the early answer to be dropped", || setup.helper_log() == ["first=123456"]);
    setup.respond("wrong-cookie", "not for you");
    setup.respond("cookie-e", "pass word with spaces ");
    assert_eq!(call.join().unwrap(), Ok(()));
    assert_eq!(setup.helper_log(), ["first=123456", "second=pass word with spaces"]);
    assert_eq!(setup.progress_of("cookie-e", 4)[3], AuthProgress::Done { authorized: true });
}

#[test]
fn too_many_wrong_answers_fail_the_request() {
    let setup = Setup::new(None);
    let call = setup.begin("cookie-f");
    setup.request("cookie-f");
    for attempt in 0..5 {
        // Each attempt: a prompt, then (after the answer) info and failed.
        setup.progress_of("cookie-f", attempt * 3 + 1);
        setup.respond("cookie-f", "wrong");
    }
    assert_eq!(call.join().unwrap(), Err("org.freedesktop.PolicyKit1.Error.Failed".to_owned()));
    let progress = setup.progress_of("cookie-f", 16);
    assert_eq!(progress.len(), 16);
    assert_eq!(progress[14], AuthProgress::Failed("Sorry, try again.".into()));
    assert_eq!(progress[15], AuthProgress::Done { authorized: false });
    assert_eq!(setup.helper_pids().len(), 5);
}

#[test]
fn a_helper_that_is_missing_or_dies_fails_the_request() {
    // Dies without a verdict, every time.
    let setup = Setup::new(Some("read cookie\nexit 3"));
    let call = setup.begin("cookie-d");
    setup.request("cookie-d");
    assert_eq!(call.join().unwrap(), Err("org.freedesktop.PolicyKit1.Error.Failed".to_owned()));
    let progress = setup.progress("cookie-d");
    assert_eq!(progress.last(), Some(&AuthProgress::Done { authorized: false }));
    assert!(progress[..progress.len() - 1].iter().all(|step| matches!(step, AuthProgress::Failed(_))));

    // Not there at all.
    std::fs::remove_file(setup.harness.path("polkit-agent-helper-1")).unwrap();
    let call = setup.begin("cookie-m");
    setup.request("cookie-m");
    assert_eq!(call.join().unwrap(), Err("org.freedesktop.PolicyKit1.Error.Failed".to_owned()));
    assert_eq!(setup.progress_of("cookie-m", 1), [AuthProgress::Done { authorized: false }]);

    // Prints without end and never asks.
    setup.harness.script("polkit-agent-helper-1", "read cookie\nwhile :; do printf 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'; done");
    let call = setup.begin("cookie-n");
    setup.request("cookie-n");
    assert_eq!(call.join().unwrap(), Err("org.freedesktop.PolicyKit1.Error.Failed".to_owned()));
}

#[test]
fn unusable_requests_are_refused() {
    let setup = Setup::new(None);
    let failed = Err("org.freedesktop.PolicyKit1.Error.Failed".to_owned());
    // A cookie that would be more than one line for the helper.
    assert_eq!(setup.begin("cookie\nSUCCESS").join().unwrap(), failed);
    assert_eq!(setup.begin("").join().unwrap(), failed);
    assert_eq!(setup.begin(&"c".repeat(5000)).join().unwrap(), failed);
    // Nobody to authenticate as: no identities, groups only, unknown uids.
    assert_eq!(setup.begin_as(&setup.polkit, "cookie-0", &[]).join().unwrap(), failed);
    assert_eq!(setup.begin_as(&setup.polkit, "cookie-1", &[3_999_999_999]).join().unwrap(), failed);
    // The wrong arguments altogether.
    let wrong = setup.polkit.conn.call_method(Some(setup.agent.0.as_str()), setup.agent.1.as_str(), Some(POLKIT_AGENT), "BeginAuthentication", &("just", 1u32));
    assert!(wrong.is_err());
    assert_eq!(setup.harness.events.count(|event| matches!(event, Event::AuthRequest(_) | Event::AuthProgress { .. })), 0);
    assert!(setup.helper_pids().is_empty());
    // A user the local file does not name is left out; the other is used.
    let call = setup.begin_as(&setup.polkit, "cookie-2", &[3_999_999_999, OTHER_UID]);
    assert_eq!(setup.request("cookie-2").users, ["ada"]);
    setup.progress_of("cookie-2", 1);
    setup.respond("cookie-2", "right");
    assert_eq!(call.join().unwrap(), Ok(()));
    assert_eq!(setup.helper_log()[0], "user=ada cookie=cookie-2");
}

#[test]
fn the_agent_follows_polkit_and_logind_coming_and_going() {
    // Neither polkit nor logind runs at first.
    let harness = Harness::new();
    let dir = harness.dir.path().display().to_string();
    harness.script("polkit-agent-helper-1", &password_helper(&dir));
    let _services = harness.start(only(|config| config.auth_agent = true));
    let polkit = polkit(&harness.system);
    // polkit alone is not enough: the session is not known without logind.
    let logind = logind(&harness.system);
    let registration = polkit.wait_call("RegisterAuthenticationAgent");
    let agent = (registration.sender.clone(), "/org/boswas/shell/AuthenticationAgent".to_owned());

    // A request is open when polkit restarts: it is closed, and the agent
    // registers with the new polkit.
    let conn = polkit.conn.clone();
    let target = agent.clone();
    let call = std::thread::spawn(move || {
        let identities: Vec<(&str, HashMap<&str, Value<'_>>)> = vec![("unix-user", HashMap::from([("uid", Value::U32(0))]))];
        let details: HashMap<&str, &str> = HashMap::new();
        conn.call_method(Some(target.0.as_str()), target.1.as_str(), Some(POLKIT_AGENT), "BeginAuthentication", &("a.b", "m", "", details, "cookie-r", identities)).is_ok()
    });
    harness.events.wait("the prompt", |events| events.iter().find(|event| matches!(event, Event::AuthProgress { progress: AuthProgress::Prompt { .. }, .. })).map(|_| ()));
    polkit.vanish();
    harness.events.wait("the request to close", |events| {
        events.iter().find(|event| matches!(event, Event::AuthProgress { progress: AuthProgress::Done { authorized: false }, .. })).map(|_| ())
    });
    let _ = call.join();
    let second = self::polkit(&harness.system);
    second.wait_call("RegisterAuthenticationAgent");
    drop(logind);
}
