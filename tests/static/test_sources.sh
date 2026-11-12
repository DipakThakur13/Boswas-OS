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
	compatibility/runners/* packages/boswas-device-agent/bin/agent-update; do
	[ -f "$f" ] && { sh -n "$f" 2>/dev/null || bad="$bad $f"; }
done
[ -z "$bad" ] && pass "shell scripts parse" || fail "shell syntax errors:$bad"

# Python syntax (compile to a throwaway location, never into the tree)
pyfail=""
while IFS= read -r f; do
	python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read(), sys.argv[1])" "$f" 2>/dev/null || pyfail="$pyfail $f"
done < <(find build packages desktop tests control-plane -name '*.py' -not -path '*/build/output/*'
	printf '%s\n' packages/boswas-cli/bin/boswas packages/boswas-compat/bin/boswas-winapp \
		packages/boswas-device-agent/bin/boswas-device packages/boswas-device-agent/bin/boswas-device-agent \
		packages/boswas-device-agent/bin/boswas-session-agent packages/boswas-compat-manager/bin/boswas-compat-manager \
		packages/boswas-sysops/bin/sysops packages/boswas-android/bin/boswas-android \
		control-plane/bin/boswas-cp)
[ -z "$pyfail" ] && pass "Python sources parse" || fail "Python syntax errors:$pyfail"

# Versions: release.conf is the single source of truth
mismatch=""
for cl in packages/*/debian/changelog control-plane/debian/changelog; do
	v="$(dpkg-parsechangelog -l "$cl" -S Version 2>/dev/null)"
	[ "$v" = "$BOSWAS_VERSION_ID" ] || mismatch="$mismatch $(basename "$(dirname "$(dirname "$cl")")")=$v"
done
for mod in packages/boswas-cli:boswas_cli packages/boswas-compat:boswas_compat \
	packages/boswas-device-agent:boswas_agent packages/boswas-compat-manager:boswas_manager control-plane:boswas_cp \
	packages/boswas-control-center:boswas_control_center packages/boswas-sysops:boswas_sysops \
	packages/boswas-android:boswas_android; do
	v="$(python3 -B -c 'import sys; sys.path.insert(0, sys.argv[1]); print(__import__(sys.argv[2]).__version__)' \
		"${mod%%:*}" "${mod#*:}" 2>/dev/null)"
	[ "$v" = "$BOSWAS_VERSION_ID" ] || mismatch="$mismatch ${mod#*:}=$v"
done
[ -z "$mismatch" ] && pass "package versions match release.conf ($BOSWAS_VERSION_ID)" \
	|| fail "version mismatch vs release.conf $BOSWAS_VERSION_ID:$mismatch"

# Line endings: everything executed or shipped must be LF
crlf="$(grep -rlI $'\r' build.sh clean.sh test.sh Makefile build compatibility config desktop installer packages security tests control-plane 2>/dev/null \
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
	--exclude-dir=tests config installer packages security desktop control-plane 2>/dev/null || true)"
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
loader32="$(sed -n 's/^WINE_LOADER32="\(.*\)"/\1/p' compatibility/wine/runtime.conf)"
server="$(sed -n 's/^WINE_SERVER="\(.*\)"/\1/p' compatibility/wine/runtime.conf)"
check "AppArmor profile grants exactly the runtime's Wine loaders (64-bit, 32-bit) and server" \
	bash -c "[ -n '$loader32' ] && grep -qF '$loader mrix,' '$profile' && grep -qF '$loader32 mrix,' '$profile' &&
		grep -qF '$server mrix,' '$profile'"
wine_exec="$(grep -E '^[[:space:]]*/usr/lib/wine/[^ ]+ [a-z]*x[a-z]*,' "$profile" | awk '{ print $1 }' | sort | tr '\n' ' ')"
check "AppArmor profile executes only the two loaders, wineserver64 and the wineserver wrapper (no preloaders, no wineserver32)" \
	test "$wine_exec" = "/usr/lib/wine/wine /usr/lib/wine/wine64 /usr/lib/wine/wineserver /usr/lib/wine/wineserver64 "
check "runner path agrees between profile, code and package" \
	bash -c "grep -q 'profile boswas-winapp /usr/lib/boswas/compat/winapp-exec ' '$profile' && grep -q 'RUNNER = \"/usr/lib/boswas/compat/winapp-exec\"' packages/boswas-compat/boswas_compat/paths.py && grep -q 'usr/lib/boswas/compat/winapp-exec' packages/boswas-compat/debian/rules"
for rule in 'deny capability,' 'deny mount,' 'deny pivot_root,' 'deny userns,' 'deny dbus,' 'audit deny @{HOME}/** rwklmx,' 'audit deny /home/** rwklmx,' 'deny /etc/machine-id r,' 'deny @{sys}/devices/**/net/** r,' 'deny @{sys}/devices/virtual/dmi/** r,'; do
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

# 64-bit and 32-bit Windows applications (ADR-0022, supersedes ADR-0014): the
# runtime declares both; the only i386 package the manifest names is wine32
# (its i386 libraries follow as dependencies); i386 is enabled by live-build
# from that entry, never by build scripts or hooks; the OS itself stays amd64.
check "Wine runtime declares x86_64 and x86" grep -qx 'ARCHITECTURES="x86_64 x86"' compatibility/wine/runtime.conf
check "the image manifest installs wine32 from the i386 architecture" \
	grep -rqx 'wine32:i386' config/live-build/config/package-lists
check_not "no other i386 package is named in the image manifest" \
	bash -c "grep -rhE '^[^#]*:i386' config/live-build/config/package-lists | grep -vx 'wine32:i386' | grep -q ."
check_not "no WoW64 or wine32 preloader package in the image manifest" \
	grep -rqiE '^[[:space:]]*[^#]*(wow64|wine32-preloader|wine64-preloader)' config/live-build/config/package-lists
check_not "the image architecture stays amd64 (no i386 base, no add-architecture in scripts and hooks)" \
	grep -rqE 'add-architecture|--architecture[[:space:]]+"?i386|BOSWAS_ARCH="i386"' build.sh build/scripts \
	build/container config/live-build/auto config/live-build/config/hooks config/boswas/release.conf
check "32-bit refusals (wine32 missing) use the product message" python3 -B -c '
import sys
sys.path.insert(0, "packages/boswas-compat")
from boswas_compat import UNSUPPORTED_32BIT_MESSAGE as m
assert m == "This application requires 32-bit Windows compatibility, which is not installed on this system."'

# Device management: no remote shell, no shell execution, no Boswas ID.
pysrc=(packages/boswas-compat/boswas_compat packages/boswas-device-agent/boswas_agent
	packages/boswas-compat-manager/boswas_manager control-plane/boswas_cp
	packages/boswas-control-center/boswas_control_center packages/boswas-sysops/boswas_sysops
	packages/boswas-android/boswas_android)
check_not "no shell execution primitives in the Boswas Python components (shell=True, os.system, os.popen, eval, exec)" \
	grep -rqE 'shell=True|os\.system\(|os\.popen\(|[^_.a-z]eval\(|[^_.a-z]exec\(' "${pysrc[@]}"
check_not "no shell-like remote command type" \
	grep -qE '^[[:space:]]+[A-Z_]*(SHELL|EXEC|SCRIPT|RUN_COMMAND|TERMINAL)[A-Z_]* = ' packages/boswas-device-agent/boswas_agent/commands.py
check "the only identity provider is NoIdentityProvider (Boswas ID not implemented)" \
	bash -c "[ \"\$(grep -rhoE 'class [A-Za-z]+\\(IdentityProvider\\)' ${pysrc[*]})\" = 'class NoIdentityProvider(IdentityProvider)' ]"
check_not "no OAuth/OIDC/SAML/JWT libraries imported" \
	grep -rqE '^[[:space:]]*(import|from)[[:space:]]+[A-Za-z_.]*(oauth|oidc|openid|saml|jwt)' "${pysrc[@]}"
agent_unit=packages/boswas-device-agent/systemd/boswas-device-agent.service
for key in NoNewPrivileges=yes ProtectSystem=strict ProtectHome=read-only CapabilityBoundingSet=CAP_DAC_READ_SEARCH \
	MemoryDenyWriteExecute=yes SystemCallFilter=@system-service RestrictNamespaces=yes; do
	contains "device agent unit keeps $key" "$agent_unit" "$key"
done
cp_unit=control-plane/systemd/boswas-control-plane.service
check "Control Plane service runs unprivileged (User=boswas-cp, no capabilities)" \
	bash -c "grep -qx 'User=boswas-cp' '$cp_unit' && grep -qx 'CapabilityBoundingSet=' '$cp_unit' && grep -qx 'NoNewPrivileges=yes' '$cp_unit'"
# Brand: the Boswas OS mark, Orbitron, the logo's gold accent (desktop/branding/README.md).
brand_src=(desktop installer config/live-build/config/bootloaders control-plane/dashboard
	packages/boswas-compat-manager/data packages/boswas-compat-manager/boswas_manager)
check_not "brand sources use the gold accent (no retired teal #17C6C0/#2ED3CD; docs and tests may name it)" \
	bash -c 'grep -rliE "#17C6C0|#2ED3CD|23,198,192|46,211,205" "$@" | grep -v -e "/README.md$" -e "/tests/" | grep -q .' _ "${brand_src[@]}"
check_not "brand sources no longer use the Boswas Group gear logo" \
	grep -rqE 'boswas-symbol\.svg|boswas-group-logo\.svg' "${brand_src[@]}" packages/boswas-branding/debian
check "brand mark and wordmarks are generated (traced mark, Orbitron outlines)" \
	bash -c "grep -q 'trace_mark.py' desktop/branding/boswas-os-mark.svg && grep -q 'trace_mark.py' desktop/branding/boswas-os-mark-mono.svg &&
		for w in boswas-os boswas os; do grep -q 'Orbitron SemiBold' desktop/branding/wordmark-\$w.svg || exit 1; done"
check "user-facing Boswas sources do not brand the system as Debian (ADR-0005)" python3 -B -c '
import glob, json, re, sys
bad = []
def visible_lines(path):
    for n, line in enumerate(open(path, encoding="utf-8"), 1):
        s = line.strip()
        if s and not s.startswith(("#", "//", "/*", "*")):
            yield n, s
# Identity files, KDE defaults, live session, terminal: every non-comment line
# (ID_LIKE=debian is the intended technical compatibility marker).
for path in ["config/boswas/os-release.in", "config/boswas/issue.in", "config/boswas/issue.net.in",
             "config/boswas/motd.in", "config/boswas/live-config.conf", "config/boswas/live-config-boswas",
             "desktop/terminal/bashrc", "desktop/terminal/boswas-welcome",
             "desktop/branding/splash/contents/splash/Splash.qml",
             *glob.glob("desktop/defaults/kde-settings/*"), *glob.glob("desktop/defaults/sddm/*")]:
    for n, s in visible_lines(path):
        if "debian" in s.lower() and s != "ID_LIKE=debian":
            bad.append(f"{path}:{n}: {s}")
# Artwork: rendered text only (titles and comments are not shown).
for path in glob.glob("desktop/**/*.svg", recursive=True) + glob.glob("installer/**/*.svg", recursive=True):
    for m in re.finditer(r"<text[^>]*>([^<]*)", open(path, encoding="utf-8").read()):
        if "debian" in m.group(1).lower():
            bad.append(f"{path}: {m.group(1)}")
# Names and descriptions users see in settings (Global Themes, wallpapers, applets).
for path in glob.glob("desktop/**/metadata.json", recursive=True):
    plugin = json.load(open(path, encoding="utf-8")).get("KPlugin", {})
    for key, value in plugin.items():
        if key.startswith(("Name", "Description")) and "debian" in str(value).lower():
            bad.append(f"{path}: {key}={value}")
if bad:
    sys.exit("\n".join(bad))
'
check "live USB: the default boot entry is the live session; the installer is a separate entry" \
	bash -c "grep -m1 '^menuentry' config/live-build/config/bootloaders/grub-pc/grub.cfg | grep -q 'Live session' &&
		grep -q 'splash' config/live-build/auto/config && ! grep -qE 'auto=true|priority=critical' config/live-build/auto/config"
check "Boswas Launcher reuses Kickoff (X-Plasma-RootPath) and provides the launcher menu (Meta key)" \
	bash -c "grep -q '\"X-Plasma-RootPath\": \"org.kde.plasma.kickoff\"' desktop/launcher/metadata.json &&
		grep -q 'org.kde.plasma.launchermenu' desktop/launcher/metadata.json"
check "Orbitron is shipped with its SIL OFL 1.1 licence" \
	bash -c "grep -q 'SIL OPEN FONT LICENSE Version 1.1' desktop/fonts/orbitron/OFL.txt &&
		test \$(ls desktop/fonts/orbitron/static/Orbitron-*.ttf | wc -l) -eq 6"

check "agent and Control Plane share one command set (schema enum == model)" python3 -B -c '
import json, sys
sys.path[:0] = ["packages/boswas-device-agent", "packages/boswas-compat"]
from boswas_agent.commands import CommandType
schema = json.load(open("packages/boswas-device-agent/schemas/device-command-v2.schema.json"))
assert schema["properties"]["type"]["enum"] == [c.value for c in CommandType]'

finish
