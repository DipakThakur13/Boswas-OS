"""The reminder service of Boswas OS, and what Clock and Calendar share with it.

``boswas-reminders`` is a small program without a window that runs in the
desktop session. It reads the alarms, timers and focus session of Clock and
the events of Calendar from their JSON files, sleeps until the next one is
due and then shows a desktop notification, so alarms ring while the
applications are closed.

Modules
  paths      where the three programs keep their files (no Qt)
  jsonfile   bounded reading, atomic writing and locked updating of JSON files (no Qt)
  timeutil   the system time zone and the conversion of local times (no Qt)
  engine     what is due when, what was missed, snoozing; the service's state file (no Qt)
  textfmt    the words of the notifications (no Qt)
  chime      the notification sound as generated PCM (no Qt), played by a short-lived helper
  notifier   org.freedesktop.Notifications through QtDBus
  service    the event loop: file watching, one timer, suspend and resume
  client     for the applications: is the service running, start it
"""

APP_ID = "com.boswas.Reminders"
PROGRAM = "boswas-reminders"
