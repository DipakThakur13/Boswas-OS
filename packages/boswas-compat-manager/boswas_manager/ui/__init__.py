"""Qt widgets of the Compatibility Manager.

Widgets hold no business rules: they ask boswas_manager.viewmodel what to
show and run every backend call through ui.worker.Runner, off the GUI thread.
Untrusted text is only ever shown in plain-text widgets.
"""
