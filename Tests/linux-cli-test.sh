#!/bin/bash
# End-to-end test of the keyguard CLI on Linux.
#
# The CLI is macOS-only in production: Keychain, LocalAuthentication, CryptoKit.
# Tests/LinuxShims supplies stand-in modules under those exact names, so the
# whole binary - including migrate, the biometric gate and the recipient-set
# check - can be built and driven here instead of only on the Mac. The shims
# are never part of the shipped binary; `make build` uses the real SDK.
#
# Skips itself anywhere but Linux, and anywhere age or swiftc is missing.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly REPO_ROOT
readonly SHIM_MODULES=(Security LocalAuthentication CryptoKit Darwin)

passes=0
failures=0
work=""

clean_up() { [ -n "$work" ] && /bin/rm -rf "$work"; }

pass() { printf '  \xe2\x9c\x93 %s\n' "$1"; passes=$((passes + 1)); }
fail() { printf '  \xe2\x9c\x97 %s: %s\n' "$1" "$2"; failures=$((failures + 1)); }

assert_equals() {
    if [ "$2" = "$3" ]; then pass "$1"; else fail "$1" "got [$3], want [$2]"; fi
}

assert_contains() {
    case "$2" in
        *"$3"*) pass "$1" ;;
        *) fail "$1" "[$2] does not contain [$3]" ;;
    esac
}

assert_absent() {
    case "$2" in
        *"$3"*) fail "$1" "[$2] should not contain [$3]" ;;
        *) pass "$1" ;;
    esac
}

build_binary() {
    local shims="$1" module
    for module in "${SHIM_MODULES[@]}"; do
        swiftc -emit-module -emit-library -module-name "$module" \
               -emit-module-path "$shims/$module.swiftmodule" -o "$shims/lib$module.so" \
               "$REPO_ROOT/Tests/LinuxShims/$module.swift" >/dev/null 2>&1 || return 1
    done

    swiftc -emit-module -emit-library -module-name KeyguardCore \
           -emit-module-path "$shims/KeyguardCore.swiftmodule" -o "$shims/libKeyguardCore.so" \
           "$REPO_ROOT"/Sources/KeyguardCore/*.swift >/dev/null 2>&1 || return 1

    # TerminalInput is the one file that cannot build off macOS - tcflag_t is
    # UInt there and UInt32 here - so the shim replaces exactly that file.
    local sources=()
    local source
    for source in "$REPO_ROOT"/Sources/keyguard/*.swift; do
        [ "$(basename "$source")" = "TerminalInput.swift" ] && continue
        sources+=("$source")
    done

    swiftc -o "$shims/keyguard" \
        -I "$shims" -L "$shims" \
        -lKeyguardCore -lSecurity -lLocalAuthentication -lCryptoKit -lDarwin \
        -Xlinker -rpath -Xlinker "$shims" \
        "${sources[@]}" "$REPO_ROOT/Tests/LinuxShims/TerminalInput.swift" >/dev/null 2>&1
}

seed_legacy_store() {
    python3 - "$1" <<'PY'
import base64, json, os, sys
work = sys.argv[1]
key = bytes(range(32))
plaintext = b"\n".join([
    b"GITHUB_TOKEN=gh-value",
    b"JIRA_TOKEN=jira-value",
    b"SSH_KEY=base64:" + base64.b64encode(b"-----BEGIN-----\nline2\n-----END-----"),
    b"WEIRD=a=b=c==",
])
blob = b"FAKE" + bytes(b ^ key[i % len(key)] for i, b in enumerate(plaintext))
with open(os.path.join(work, "drive", "keyguard.enc"), "wb") as handle:
    handle.write(blob)
with open(os.path.join(work, "keychain.json"), "w") as handle:
    json.dump({"keyguard/encryption-key": base64.b64encode(key).decode()}, handle)
PY
}

poison_pinned_recipients() {
    python3 - "$1" <<'PY'
import json, sys
path = sys.argv[1]
with open(path) as handle:
    pinned = json.load(handle)
pinned["tiers"]["high"].append("age1" + "q" * 55)
with open(path, "w") as handle:
    json.dump(pinned, handle)
PY
}

main() {
    if [ "$(uname -s)" != "Linux" ]; then
        echo "not Linux - the CLI builds against the real SDK here, skipping"
        return 0
    fi
    if ! command -v swiftc >/dev/null 2>&1 || ! command -v age >/dev/null 2>&1; then
        echo "swiftc or age missing - skipping"
        return 0
    fi

    work="$(mktemp -d)"
    trap 'clean_up' EXIT

    mkdir -p "$work/drive" "$work/home/.keyguard" "$work/shims"

    if ! build_binary "$work/shims"; then
        echo "failed to build the CLI against the shims" >&2
        return 1
    fi
    pass "should build the whole CLI off macOS"

    local keyguard="$work/shims/keyguard"
    export KEYGUARD_FAKE_KEYCHAIN="$work/keychain.json"
    export KEYGUARD_FAKE_PROMPTS="$work/prompts.log"
    export KEYGUARD_SECRETS_FILE="$work/drive/keyguard.enc"
    export KEYGUARD_RECIPIENTS_FILE="$work/home/.keyguard/recipients"
    KEYGUARD_AGE_BIN="$(command -v age)"
    export KEYGUARD_AGE_BIN

    seed_legacy_store "$work"

    echo "migrate"
    local output
    output="$("$keyguard" migrate 2>&1)"
    assert_contains "should report what it migrated" "$output" "Migrated 4 secrets"
    assert_contains "should say it verified the result" "$output" "read back identically"
    assert_equals "should leave the legacy file in place, so rollback is reinstalling the old binary" \
        "yes" "$([ -f "$work/drive/keyguard.enc" ] && echo yes || echo no)"
    assert_equals "should keep the legacy Keychain key" \
        "yes" "$(grep -q "encryption-key" "$work/keychain.json" && echo yes || echo no)"
    assert_equals "should put the store beside the secrets file rather than under home" \
        "yes" "$([ -d "$work/drive/keyguard-store" ] && echo yes || echo no)"
    assert_equals "should prompt exactly once" "1" "$(wc -l < "$work/prompts.log" | tr -d ' ')"
    assert_equals "should refuse to migrate twice without --force" \
        "1" "$("$keyguard" migrate >/dev/null 2>&1; echo $?)"

    echo ""
    echo "reads"
    assert_equals "should return a single value with no trailing newline" \
        "jira-value" "$("$keyguard" get JIRA_TOKEN)"
    assert_equals "should return a batch as KEY=VALUE" \
        "JIRA_TOKEN=jira-value
GITHUB_TOKEN=gh-value" "$("$keyguard" get JIRA_TOKEN GITHUB_TOKEN)"
    assert_equals "should round-trip a multi-line secret byte for byte" \
        "-----BEGIN-----
line2
-----END-----" "$("$keyguard" get SSH_KEY)"
    assert_equals "should round-trip a value full of equals signs" \
        "a=b=c==" "$("$keyguard" get WEIRD)"
    assert_equals "should list every name" \
        "GITHUB_TOKEN
JIRA_TOKEN
SSH_KEY
WEIRD" "$("$keyguard" list)"
    assert_contains "should report an intact store" "$("$keyguard" verify 2>&1)" "is intact"

    : > "$work/prompts.log"
    "$keyguard" get JIRA_TOKEN GITHUB_TOKEN SSH_KEY WEIRD >/dev/null
    assert_equals "should prompt once for a four-variable batch, matching envify" \
        "1" "$(wc -l < "$work/prompts.log" | tr -d ' ')"

    echo ""
    echo "the biometric gate"
    output="$(KEYGUARD_FAKE_DENY=1 "$keyguard" get JIRA_TOKEN 2>&1)"
    assert_equals "should exit 2 when biometrics are denied" \
        "2" "$(KEYGUARD_FAKE_DENY=1 "$keyguard" get JIRA_TOKEN >/dev/null 2>&1; echo $?)"
    assert_absent "should emit no secret when biometrics are denied" "$output" "jira-value"
    assert_equals "should emit nothing at all on stdout when denied" \
        "" "$(KEYGUARD_FAKE_DENY=1 "$keyguard" get JIRA_TOKEN 2>/dev/null)"

    echo ""
    echo "writes"
    assert_contains "should store a value from stdin, the way the bridge posts one" \
        "$(printf 'brand-new' | "$keyguard" set NEW_TOKEN 2>&1)" "Set 'NEW_TOKEN'"
    assert_equals "should read the new value back" "brand-new" "$("$keyguard" get NEW_TOKEN)"
    printf 'rotated' | "$keyguard" set NEW_TOKEN >/dev/null 2>&1
    assert_equals "should overwrite without blocking when not attached to a terminal" \
        "rotated" "$("$keyguard" get NEW_TOKEN)"
    "$keyguard" mv NEW_TOKEN RENAMED >/dev/null 2>&1
    assert_equals "should carry the value through a rename" "rotated" "$("$keyguard" get RENAMED)"
    assert_equals "should drop the old name on rename" \
        "1" "$("$keyguard" get NEW_TOKEN >/dev/null 2>&1; echo $?)"
    "$keyguard" delete RENAMED >/dev/null 2>&1
    assert_equals "should remove a deleted name" \
        "1" "$("$keyguard" get RENAMED >/dev/null 2>&1; echo $?)"
    assert_contains "should stay intact across writes" "$("$keyguard" verify 2>&1)" "is intact"
    assert_contains "should name a key it does not hold" "$("$keyguard" get NOPE 2>&1)" "Keys not found: NOPE"

    echo ""
    echo "recipient set integrity"
    poison_pinned_recipients "$KEYGUARD_RECIPIENTS_FILE"
    output="$("$keyguard" get JIRA_TOKEN 2>&1)"
    assert_equals "should refuse to read when the pinned set diverges" \
        "1" "$("$keyguard" get JIRA_TOKEN >/dev/null 2>&1; echo $?)"
    assert_absent "should emit no secret when the pinned set diverges" "$output" "jira-value"
    assert_contains "should say nothing was read or written" "$output" "Nothing was read or written"

    echo ""
    if [ "$failures" -gt 0 ]; then
        printf '%d failure(s), %d passed\n' "$failures" "$passes" >&2
        return 1
    fi
    printf 'All %d checks passed\n' "$passes"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
