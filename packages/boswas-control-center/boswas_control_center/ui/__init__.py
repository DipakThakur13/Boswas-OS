"""Qt widgets of Boswas Control Center.

Widgets hold no business rules: they ask boswas_control_center.viewmodel
what to show and run every backend call through ui.worker.Runner, off the
GUI thread. Text from outside is only ever shown in plain-text widgets.

The look is the Boswas UI toolkit's: boswas_fluent, installed by
boswas-branding next to this package in /usr/lib/boswas/python (in the
monorepo: desktop/ui/qt, which the tests put on the import path).
"""
