"""Qt widgets of Boswas Control Center.

Widgets hold no business rules: they ask boswas_control_center.viewmodel
what to show and run every backend call through ui.worker.Runner, off the
GUI thread. Text from outside is only ever shown in plain-text widgets.
"""
