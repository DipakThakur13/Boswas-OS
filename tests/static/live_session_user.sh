#!/usr/bin/env bash
# Which user does the live session sign in?
#
# Runs the real live-config component (config/boswas/live-config-boswas) the
# way live-config runs it at boot, in a scratch root: live-config's defaults
# are exported first (LIVE_USERNAME="user", as /usr/lib/live/init-config.sh
# does), then the Boswas settings file is read, then the component runs as a
# process of its own with the kernel command line in LIVE_CONFIG_CMDLINE.
# The users that exist are given as arguments.
#
# Usage: live_session_user.sh "KERNEL COMMAND LINE" [EXISTING_USER...]
# Prints the user of greetd's [initial_session], or "none" when the component
# wrote no automatic sign-in (the login screen is shown then).
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cmdline="${1:?usage: live_session_user.sh \"KERNEL COMMAND LINE\" [EXISTING_USER...]}"
shift

root="$(mktemp -d)"
trap 'rm -rf "$root"' EXIT
mkdir -p "$root/usr/lib/live" "$root/etc/boswas" "$root/bin" "$root/var/lib/live/config"

# The part of live-config's function library the component uses.
cat > "$root/usr/lib/live/config.sh" <<'EOF'
component_was_executed () { [ -e "/var/lib/live/config/${1}" ]; }
EOF
# The login configuration as the package ships it.
cp "$repo/packages/boswas-desktop/session/greetd.toml" "$root/etc/boswas/greetd.toml"
# getent: only the users named on the command line exist.
{
	echo '#!/bin/sh'
	echo '[ "$1" = passwd ] || exit 2'
	echo 'case " '"$*"' " in *" $2 "*) echo "$2:x:1000:1000::/home/$2:/bin/bash"; exit 0 ;; esac'
	echo 'exit 2'
} > "$root/bin/getent"
chmod 0755 "$root/bin/getent"

# The component, with its absolute paths moved into the scratch root.
sed -e "s#/usr/lib/live/config.sh#$root/usr/lib/live/config.sh#" \
	-e "s#/etc/boswas/#$root/etc/boswas/#g" \
	-e "s#/usr/local/share/#$root/usr/local/share/#g" \
	-e "s#/run/boswas#$root/run/boswas#g" \
	-e "s#/var/lib/live/#$root/var/lib/live/#g" \
	"$repo/config/boswas/live-config-boswas" > "$root/component"

env -i PATH="$root/bin:/usr/bin:/bin" sh -c '
	# live-config: defaults, exported (init-config.sh) ...
	LIVE_HOSTNAME="debian"; LIVE_USERNAME="user"; LIVE_USER_FULLNAME="Debian Live user"
	export LIVE_HOSTNAME LIVE_USERNAME LIVE_USER_FULLNAME
	# ... then the configuration files, with every assignment exported ...
	set -o allexport
	. "$1"
	set +o allexport
	# ... then each component as a process of its own.
	LIVE_CONFIG_CMDLINE="$2"; export LIVE_CONFIG_CMDLINE
	sh "$3" > /dev/null 2>&1
' _ "${BOSWAS_LIVE_CONF:-$repo/config/boswas/live-config.conf}" "$cmdline" "$root/component"

user="$(sed -n '/^\[initial_session\]/,$s/^user = "\(.*\)"$/\1/p' "$root/etc/boswas/greetd.toml")"
echo "${user:-none}"
