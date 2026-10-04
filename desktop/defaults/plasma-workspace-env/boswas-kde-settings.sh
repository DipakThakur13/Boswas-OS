# Boswas OS: make the Boswas KDE defaults visible to the Plasma session.
# Sourced by startplasma (installed as
# /etc/xdg/plasma-workspace/env/boswas-kde-settings.sh by boswas-branding).
#
# The Boswas directory is appended, so /etc/xdg (local administrator
# overrides) and the user's own settings keep precedence. This is the same
# mechanism Debian's desktop-base uses for its KDE defaults.
case ":${XDG_CONFIG_DIRS:-}:" in
	*:/usr/share/boswas/kde-settings:*) ;;
	*) XDG_CONFIG_DIRS="${XDG_CONFIG_DIRS:-/etc/xdg}:/usr/share/boswas/kde-settings" ;;
esac
export XDG_CONFIG_DIRS
