#!/bin/bash
# ccnm P24 on hpsrv: the two privileged steps of the plan (A1, A2). Run as root.
#
#   --check        read-only: what is there now, what this round recorded
#   --apply        record first, then append this round's one-time public key
#                  to ccrun's authorized_keys (A1) and install bubblewrap (A2)
#   --revoke-key   take back only the key line (the plan's default cleanup:
#                  bubblewrap stays, since hpsrv keeps serving this chain)
#   --revert       take back everything the record says this round did
#
# The key is pinned here and its fingerprint recomputed before use, so a
# swapped key cannot slip in under the old fingerprint. The private half is on
# the Agent machine and never reaches this host.
#
# Why bubblewrap at all: Codex's workspace-write sandbox on Linux is bwrap. With
# no bwrap every sandboxed command fails (ccnm P21.4), which is refusal, not
# escape -- but then the chain cannot work. Unprivileged user namespaces are
# already on here (kernel.unprivileged_userns_clone=1, no AppArmor restriction),
# so nothing else changes.
#
# --revert only undoes what the record says: bubblewrap is purged only if the
# record says this round installed it; other lines in authorized_keys are kept.
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
umask 022
case "${1:-}" in
    --check|--apply|--revoke-key|--revert) action=$1 ;;
    *) echo 'usage: hpsrv-runtime.sh --check|--apply|--revoke-key|--revert' >&2; exit 2 ;;
esac
[[ $# == 1 ]] || exit 2
[[ $(uname -s) == Linux && -f /etc/debian_version ]] || { echo 'Debian-family Linux required' >&2; exit 1; }
[[ $EUID == 0 ]] || { echo 'Run as root on the Runtime Node' >&2; exit 1; }

user=ccrun
home=/home/$user
keys=$home/.ssh/authorized_keys
record=/var/lib/ccnm-p24-20260916
key='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOT6thxUZkyv3BYXcCsFunLWtNyfDmDjgwYFdZs9tW1e ccnm-p24-20260916 (one-time, ccrun@hpsrv)'
fingerprint=SHA256:LnBiBH6kMDhT94E6zB4OM90wsGBv7U3Qe8GfQhZRlNc
options=no-agent-forwarding,no-port-forwarding,no-X11-forwarding,no-user-rc,no-pty
line="$options $key"

[[ $key != *$'\n'* && $key == 'ssh-ed25519 '* ]]
actual=$(printf '%s\n' "$key" | ssh-keygen -lf /dev/stdin -E sha256 | awk '{print $2}')
[[ $actual == "$fingerprint" ]] || {
    echo "Key fingerprint is $actual, not the pinned $fingerprint; refusing" >&2; exit 1; }

id "$user" >/dev/null 2>&1 || { echo "$user does not exist; this round does not create accounts" >&2; exit 1; }
[[ $(id -u "$user") == 1002 ]] || { echo "$user is not uid 1002; refusing to guess" >&2; exit 1; }

key_present() { [[ -f $keys ]] && grep -qxF "$line" "$keys"; }
bwrap_installed() { dpkg-query -W -f='${db:Status-Abbrev}' bubblewrap 2>/dev/null | grep -q '^ii'; }

report() {
    echo "account: $(id "$user")"
    echo "authorized_keys: $( [[ -f $keys ]] && wc -l < "$keys" || echo missing ) line(s); this round's key present: $(key_present && echo yes || echo no)"
    echo "bubblewrap: $(bwrap_installed && dpkg-query -W -f='${Version}' bubblewrap || echo 'not installed')"
    echo "record: $( [[ -d $record ]] && ls -1 "$record" | tr '\n' ' ' || echo none )"
}

if [[ $action == --check ]]; then
    report
    exit 0
fi

if [[ $action == --apply ]]; then
    if [[ ! -d $record ]]; then
        mkdir -m 700 "$record"
    fi
    [[ ! -L $record && $(stat -c '%u:%a' "$record") == 0:700 ]]
    date -Is > "$record/applied-at"
    # Keys: record exactly the bytes appended, so the revoke removes that line only.
    if key_present; then
        echo 'This round'"'"'s key is already present; not appending a duplicate.'
    else
        [[ -f $keys && ! -L $keys ]] || { echo "$keys missing or a symlink; refusing" >&2; exit 1; }
        [[ -f $record/authorized_keys.before ]] || cp -p "$keys" "$record/authorized_keys.before"
        printf '%s\n' "$line" > "$record/appended-line"
        printf '%s\n' "$line" >> "$keys"
        chown "$user:$user" "$keys"
        chmod 600 "$keys"
    fi
    # Package: only record it as ours if it was not there before this round.
    if bwrap_installed; then
        [[ -f $record/installed-bubblewrap ]] || echo 'bubblewrap was already installed; not recording it as this round'"'"'s.'
    else
        printf 'bubblewrap\n' > "$record/installed-bubblewrap"
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends bubblewrap
        dpkg-query -W -f='${Version}\n' bubblewrap >> "$record/installed-bubblewrap"
    fi
    report
    exit 0
fi

[[ -d $record && ! -L $record && $(stat -c '%u:%a' "$record") == 0:700 ]] || {
    echo "Missing this round's record $record" >&2; exit 1; }

revoke_key() {
    if [[ -f $record/appended-line ]] && key_present; then
        tmp=$(mktemp "$home/.ssh/.authorized_keys.p24.XXXXXX")
        trap 'rm -f "$tmp"' EXIT
        grep -vxF "$line" "$keys" > "$tmp" || true
        chown "$user:$user" "$tmp"
        chmod 600 "$tmp"
        mv "$tmp" "$keys"
        trap - EXIT
        ! key_present
        echo "Removed this round's key line; $(wc -l < "$keys") other line(s) kept."
    else
        echo "This round's key line is not present; nothing removed."
    fi
    date -Is > "$record/key-revoked-at"
}

if [[ $action == --revoke-key ]]; then
    revoke_key
    report
    exit 0
fi

# --revert
revoke_key
if [[ -f $record/installed-bubblewrap ]] && bwrap_installed; then
    DEBIAN_FRONTEND=noninteractive apt-get purge -y bubblewrap
fi
rm -rf -- "$record"
report
