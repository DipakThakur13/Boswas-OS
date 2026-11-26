//! What a control does when it is activated. Every surface registers its
//! controls with one of these; the shell dispatches them in one place.

use crate::{capture, desktop_icons, dialogs, lock, notify, quick, start, taskbar, widgets};

#[derive(Clone, Debug, PartialEq)]
pub enum Action {
    /// Swallows the click: the area belongs to a surface but does nothing.
    None,
    /// Closes whatever flyout or menu is open (a click outside it).
    Dismiss,
    Taskbar(taskbar::Act),
    /// A window card in a task's preview flyout.
    Preview(taskbar::PreviewAct),
    Start(start::Act),
    Quick(quick::Act),
    Notify(notify::Act),
    /// Entry `index` of the menu at `level` of the open menu stack.
    Menu { level: usize, index: usize },
    Dialog(dialogs::Act),
    Capture(capture::Act),
    Lock(lock::Act),
    /// The desktop background (for its context menu).
    Desktop,
    /// The clipboard history's flyout.
    Clip(crate::clipview::Act),
    /// The emoji panel.
    Emoji(crate::emoji::Act),
    /// A control of the widgets board.
    Widgets(widgets::Act),
    /// A desktop icon.
    Desk(desktop_icons::Act),
    /// The security options screen (Ctrl+Alt+Del).
    Security(crate::security::Act),
    /// The display projection flyout (Super+P).
    Projection(crate::projection::Act),
}
