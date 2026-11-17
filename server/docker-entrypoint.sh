#!/bin/sh
# Entrypoint: prepare the data volume, then drop root privileges.
#
# Why: docker creates a bind-mounted ./data owned by root, so a non-root image
# user could not write to it.  We chown the data directory once (cheap when
# empty), then re-exec as the unprivileged dgxchat user with setpriv.  Set
# DGXCHAT_SKIP_CHOWN=1 to skip the recursive chown on every start.
set -e

DATA_DIR="${DGXCHAT_DATA_DIR:-/data}"
UID_NUM="${DGXCHAT_UID:-10001}"
GID_NUM="${DGXCHAT_GID:-10001}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR"
    if [ "${DGXCHAT_SKIP_CHOWN:-0}" != "1" ]; then
        chown -R "$UID_NUM:$GID_NUM" "$DATA_DIR" 2>/dev/null || true
    fi
    if command -v setpriv >/dev/null 2>&1; then
        exec setpriv --reuid="$UID_NUM" --regid="$GID_NUM" --init-groups "$@"
    fi
    echo "dgxchat: setpriv not available, staying as root" >&2
fi

exec "$@"
