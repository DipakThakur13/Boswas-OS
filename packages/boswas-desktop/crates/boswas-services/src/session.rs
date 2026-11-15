//! logind: lock and unlock requests for our session, and the machine going
//! to sleep or waking up.

use std::time::Duration;

use async_channel::Receiver;

use crate::{
    logind::{own_session, LOGIN1, MANAGER, MANAGER_PATH, SESSION},
    runtime::{idle, owner_rule, signal_rule, subscribe, wake, Backoff, Bus, BusKind, CallResult, Ctx, Exec, IdleEnd, Service, Wake},
    Command, Event, SessionEvent,
};

pub(crate) struct Session {
    ctx: Ctx,
    ex: Exec,
}

enum Msg {
    Event(SessionEvent),
    /// logind came or went: find the session again.
    Owner,
    Lost,
}

enum End {
    Stop,
    Lost,
    /// Subscribe again (the session path may have changed).
    Again,
}

impl Service for Session {
    fn new(ctx: Ctx, ex: Exec) -> Self {
        Self { ctx, ex }
    }

    async fn run(&mut self, commands: Receiver<Command>) {
        let mut backoff = Backoff::new(Duration::from_secs(30));
        loop {
            match Bus::connect(BusKind::System, &self.ctx.env, &self.ex).await {
                Ok(bus) => {
                    backoff.reset();
                    loop {
                        match self.serve(&bus, &commands).await {
                            Ok(End::Again) => continue,
                            Ok(End::Stop) => return,
                            Ok(End::Lost) => break,
                            Err(error) => {
                                tracing::debug!(%error, "session: the system bus stopped answering");
                                break;
                            }
                        }
                    }
                }
                Err(error) => tracing::debug!(%error, "session: no system bus"),
            }
            if let IdleEnd::Closed = idle(&commands, backoff.next(), |_| false).await {
                return;
            }
        }
    }
}

impl Session {
    async fn serve(&mut self, bus: &Bus, commands: &Receiver<Command>) -> CallResult<End> {
        let (inbox, messages) = async_channel::unbounded::<Msg>();
        let conn = &bus.conn;
        let mut tasks = Vec::new();
        tasks.push(subscribe(conn, &self.ex, owner_rule(LOGIN1)?, &inbox, |_| Some(Msg::Owner), || Msg::Lost).await?);
        tasks.push(
            subscribe(
                conn,
                &self.ex,
                signal_rule(Some(LOGIN1), MANAGER, Some("PrepareForSleep"), Some(MANAGER_PATH))?.build(),
                &inbox,
                |message| {
                    let body = message.body();
                    let (sleeping,) = body.deserialize::<(bool,)>().ok()?;
                    Some(Msg::Event(SessionEvent::PrepareForSleep(sleeping)))
                },
                || Msg::Lost,
            )
            .await?,
        );
        // Without a session (logind not running, or this process is in
        // none) only sleep is reported; the session is looked for again
        // when logind appears.
        if let Some(session) = own_session(conn, &self.ctx.env).await {
            for (member, event) in [("Lock", SessionEvent::Lock), ("Unlock", SessionEvent::Unlock)] {
                tasks.push(
                    subscribe(
                        conn,
                        &self.ex,
                        signal_rule(Some(LOGIN1), SESSION, Some(member), Some(&session.path))?.build(),
                        &inbox,
                        move |_| Some(Msg::Event(event)),
                        || Msg::Lost,
                    )
                    .await?,
                );
            }
        }
        loop {
            match wake(commands, &messages, None).await {
                Wake::Closed => return Ok(End::Stop),
                Wake::Command(_) | Wake::Timer => {}
                Wake::Message(Msg::Event(event)) => self.ctx.emit(Event::Session(event)),
                Wake::Message(Msg::Owner) => return Ok(End::Again),
                Wake::Message(Msg::Lost) => return Ok(End::Lost),
            }
        }
    }
}
