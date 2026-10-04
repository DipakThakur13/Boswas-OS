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
	packages/*/debian/postinst packages/*/debian/postrm desktop/defaults/plasma-workspace-env/*.sh; do
	[ -f "$f" ] && { sh -n "$f" 2>/dev/null || bad="$bad $f"; }
done
[ -z "$bad" ] && pass "shell scripts parse" || fail "shell syntax errors:$bad"

# Python syntax (compile to a throwaway location, never into the tree)
pyfail=""
while IFS= read -r f; do
	python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read(), sys.argv[1])" "$f" 2>/dev/null || pyfail="$pyfail $f"
done < <(find build packages desktop tests -name '*.py' -not -path '*/build/output/*'; echo packages/boswas-cli/bin/boswas)
[ -z "$pyfail" ] && pass "Python sources parse" || fail "Python syntax errors:$pyfail"

# Versions: release.conf is the single source of truth
mismatch=""
for cl in packages/*/debian/changelog; do
	v="$(dpkg-parsechangelog -l "$cl" -S Version 2>/dev/null)"
	[ "$v" = "$BOSWAS_VERSION_ID" ] || mismatch="$mismatch $(basename "$(dirname "$(dirname "$cl")")")=$v"
done
cli_v="$(python3 -B -c 'import sys; sys.path.insert(0, "packages/boswas-cli"); import boswas_cli; print(boswas_cli.__version__)' 2>/dev/null)"
[ "$cli_v" = "$BOSWAS_VERSION_ID" ] || mismatch="$mismatch boswas_cli=$cli_v"
[ -z "$mismatch" ] && pass "package versions match release.conf ($BOSWAS_VERSION_ID)" \
	|| fail "version mismatch vs release.conf $BOSWAS_VERSION_ID:$mismatch"

# Line endings: everything executed or shipped must be LF
crlf="$(grep -rlI $'\r' build.sh clean.sh test.sh Makefile build config desktop installer packages security tests 2>/dev/null \
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

finish
