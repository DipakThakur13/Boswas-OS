#!/usr/bin/env bash
# What does the greeter script do when the compositor ends?
#
# Runs the real packages/boswas-desktop/session/boswas-greeter with stand-ins
# for the compositor (ends with STATUS after SECONDS of pretended run time)
# and for greetd's text login.
#
# Usage: greeter_fallback.sh STATUS SECONDS
# Prints "done" (the greeter ended normally), "text-login: ARGS" (the text
# login was started with ARGS) or "exit: N" (the script ended with status N
# and greetd would start the greeter again).
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
status="${1:?usage: greeter_fallback.sh STATUS SECONDS}"
seconds="${2:?usage: greeter_fallback.sh STATUS SECONDS}"

root="$(mktemp -d)"
trap 'rm -rf "$root"' EXIT
mkdir -p "$root/usr/bin" "$root/usr/sbin" "$root/proc"
echo "100.00 400.00" > "$root/proc/uptime"
# The compositor: time passes, then it ends with the given status.
printf '#!/bin/sh\necho "%s.50 400.00" > "%s/proc/uptime"\nexit %s\n' "$((100 + seconds))" "$root" "$status" \
	> "$root/usr/bin/boswas-compositor"
# The text login: says how it was called.
printf '#!/bin/sh\necho "text-login: $*"\n' > "$root/usr/sbin/agreety"
chmod 0755 "$root/usr/bin/boswas-compositor" "$root/usr/sbin/agreety"

sed -e "s#/usr/bin/boswas-compositor#$root/usr/bin/boswas-compositor#g" \
	-e "s#/usr/sbin/agreety#$root/usr/sbin/agreety#g" \
	-e "s#/proc/uptime#$root/proc/uptime#g" \
	-e "s#/run/systemd/journal/stdout#$root/run/systemd/journal/stdout#g" \
	"$repo/packages/boswas-desktop/session/boswas-greeter" > "$root/greeter"

set +e
out="$(env -i PATH=/usr/bin:/bin sh "$root/greeter" 2>/dev/null)"
code=$?
set -e
if [ -n "$out" ]; then
	echo "$out"
elif [ "$code" -eq 0 ]; then
	echo "done"
else
	echo "exit: $code"
fi
