"""Boswas Task Manager: processes, performance, startup apps, users, details and services.

The package is split in two layers.

Without Qt (unit-tested against a fake ``/proc`` and ``/sys`` tree):

  textutil    cleaning text that comes from outside, sizes, rates, durations
  procfs      parsers of the files under /proc
  sysfs       readers of /sys: CPU topology and frequency, disks, network, GPU, battery
  sampler     the incremental sampler: one ``tick`` reads what is asked for and returns a Sample
  desktopfile desktop entries: parsing, the installed applications, XDG autostart
  grouping    Apps / Background processes / System processes, sessions
  services    the replies of systemd and logind as plain data
  actions     signals, priorities, properties of a process, starting a program
  settings    the user's settings (JSON under the configuration directory)
  history     the 60 samples behind every performance graph

With Qt (``ui`` and ``bus``): the window, its pages, the models, the graphs
and the D-Bus calls to systemd and logind.
"""

APP_ID = "com.boswas.TaskManager"
DISPLAY_NAME = "Task Manager"
PROGRAM = "boswas-task-manager"

PAGES = ("processes", "performance", "startup", "users", "details", "services", "settings")
PAGE_TITLES = {
    "processes": "Processes",
    "performance": "Performance",
    "startup": "Startup apps",
    "users": "Users",
    "details": "Details",
    "services": "Services",
    "settings": "Settings",
}
