#!/usr/bin/env bash
# Static checks of the repository: script syntax, Python syntax, version
# consistency, line endings, and that no secrets or private keys are committed.
set -uo pipefail
. "$(dirname "$0")/../lib.sh"
. "$BOSWAS_REPO_ROOT/build/scripts/lib.sh"
load_release
cd "$BOSWAS_REPO_ROOT"

# Shell syntax
bad=""
for f in build.sh clean.sh test.sh build/scripts/*.sh tests/lib.sh tests/*/*.sh; do
	bash -n "$f" 2>/dev/null || bad="$bad $f"
done
for f in config/live-build/auto/* config/live-build/config/hooks/live/*.hook.chroot \
	packages/*/debian/postinst packages/*/debian/postrm desktop/defaults/plasma-workspace-env/*.sh \
	compatibility/runners/*; do
	[ -f "$f" ] && { sh -n "$f" 2>/dev/null || bad="$bad $f"; }
done
[ -z "$bad" ] && pass "shell scripts parse" || fail "shell syntax errors:$bad"

# Python syntax (compile to a throwaway location, never into the tree)
pyfail=""
while IFS= read -r f; do
	python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read(), sys.argv[1])" "$f" 2>/dev/null || pyfail="$pyfail $f"
done < <(find build packages desktop tests -name '*.py' -not -path '*/build/output/*'; echo packages/boswas-cli/bin/boswas packages/boswas-compat/bin/boswas-winapp | tr ' ' '\n')
[ -z "$pyfail" ] && pass "Python sources parse" || fail "Python syntax errors:$pyfail"

# Versions: release.conf is the single source of truth
mismatch=""
for cl in packages/*/debian/changelog; do
	v="$(dpkg-parsechangelog -l "$cl" -S Version 2>/dev/null)"
	[ "$v" = "$BOSWAS_VERSION_ID" ] || mismatch="$mismatch $(basename "$(dirname "$(dirname "$cl")")")=$v"
done
for mod in boswas-cli:boswas_cli boswas-compat:boswas_compat boswas-device-agent:boswas_agent; do
	v="$(python3 -B -c 'import sys; sys.path.insert(0, sys.argv[1]); print(__import__(sys.argv[2]).__version__)' \
		"packages/${mod%%:*}" "${mod#*:}" 2>/dev/null)"
	[ "$v" = "$BOSWAS_VERSION_ID" ] || mismatch="$mismatch ${mod#*:}=$v"
done
[ -z "$mismatch" ] && pass "package versions match release.conf ($BOSWAS_VERSION_ID)" \
	|| fail "version mismatch vs release.conf $BOSWAS_VERSION_ID:$mismatch"

# Line endings: everything executed or shipped must be LF
crlf="$(grep -rlI $'\r' build.sh clean.sh test.sh Makefile build compatibility config desktop installer packages security tests 2>/dev/null \
	| grep -v -E '^build/(output|logs|manifest|cache)/' || true)"
[ -z "$crlf" ] && pass "no CRLF line endings" || fail "CRLF line endings in: $crlf"

# No key material or credentials in the repository. The marker is assembled
# at runtime so this file does not match its own pattern.
priv="PRIVATE"" KEY"
keys="$(grep -rlI -E -- "-----BEGIN ([A-Z]+ )?${priv}( BLOCK)?-----" \
	--exclude-dir=.git --exclude-dir=output --exclude-dir=logs --exclude-dir=cache . 2>/dev/null || true)"
[ -z "$keys" ] && pass "no private keys in repository" || fail "private key material found: $keys"
# Unit-test fixtures deliberately contain fake secrets (to prove redaction).
creds="$(grep -rnI -i -E '(password|passwd|secret|token)[[:space:]]*=[[:space:]]*"[^"$@]+"' \
	--exclude-dir=tests config installer packages security desktop 2>/dev/null || true)"
[ -z "$creds" ] && pass "no hard-coded credentials in configuration" || fail "possible hard-coded credential: $creds"
check_not "preseed contains no passwords" grep -E -i '^d-i[[:space:]]+(passwd/.*password|partman-crypto/passphrase)' installer/configuration/preseed.cfg

# JSON data and schemas parse
badjson=""
while IFS= read -r f; do
	python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$f" 2>/dev/null || badjson="$badjson $f"
done < <(find compatibility packages/*/schemas -name '*.json' 2>/dev/null)
[ -z "$badjson" ] && pass "JSON files parse (compatibility/, agent schemas)" || fail "invalid JSON:$badjson"

# Compatibility manifests: catalog and examples validate against format v1
manifests=(compatibility/manifests/catalog/*.json compatibility/manifests/examples/*.json)
existing=()
for f in "${manifests[@]}"; do [ -e "$f" ] && existing+=("$f"); done
if out="$(python3 -B -c '
import sys
sys.path.insert(0, "packages/boswas-compat")
from boswas_compat.cli import main
sys.exit(main(["manifest", "validate", *sys.argv[1:]]))' "${existing[@]}" 2>&1)"; then
	pass "compatibility manifests valid (${#existing[@]} files)"
else
	fail "compatibility manifests: $out"
fi

# WinCompat: runtime facts, AppArmor profile and code agree; the profile
# keeps its denials and grants nothing that escapes confinement.
profile=compatibility/policies/apparmor/boswas-winapp
loader="$(sed -n 's/^WINE_LOADER="\(.*\)"/\1/p' compatibility/wine/runtime.conf)"
server="$(sed -n 's/^WINE_SERVER="\(.*\)"/\1/p' compatibility/wine/runtime.conf)"
check "AppArmor profile grants exactly the runtime's Wine loader and server" \
	bash -c "grep -qF '$loader mrix,' '$profile' && grep -qF '$server mrix,' '$profile'"
check "runner path agrees between profile, code and package" \
	bash -c "grep -q 'profile boswas-winapp /usr/lib/boswas/compat/winapp-exec ' '$profile' && grep -q 'RUNNER = \"/usr/lib/boswas/compat/winapp-exec\"' packages/boswas-compat/boswas_compat/paths.py && grep -q 'usr/lib/boswas/compat/winapp-exec' packages/boswas-compat/debian/rules"
for rule in 'deny capability,' 'deny mount,' 'deny pivot_root,' 'deny userns,' 'deny dbus,' 'audit deny @{HOME}/** rwklmx,' 'audit deny /home/** rwklmx,' 'deny /etc/machine-id r,'; do
	contains "AppArmor profile keeps '$rule'" "$profile" "$rule"
done
check_not "AppArmor profile grants no capability" grep -qE '^[[:space:]]*capability' "$profile"
rules="$(grep -vE '^[[:space:]]*#' "$profile")"
check_not "AppArmor profile has no exec transitions out of the profile (px/cx/ux)" \
	grep -qE '[[:space:]][a-zA-Z]*[pPcCuU]i?x[a-zA-Z]*,' <<<"$rules"
check_not "AppArmor profile is neither unconfined nor complain mode" grep -qE 'flags=.*(unconfined|complain)' <<<"$rules"
check_not "AppArmor profile lets nothing in the prefix run as a Linux program" \
	grep -qE '/(var/lib/boswas/wine|tmp|dev/shm)/[^ ]* [a-z]*x[a-z]*,' <<<"$rules"
check "Windows code never starts as root (runner refuses unless confined; tool refuses root)" \
	bash -c "grep -q 'boswas-winapp (enforce)' compatibility/runners/winapp-exec && grep -q 'os.geteuid() == 0' packages/boswas-compat/boswas_compat/ops.py"

finish
