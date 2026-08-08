#!/bin/sh
set -eu

die() {
    echo "docker-entrypoint: $*" >&2
    exit 1
}

PUID=${PUID-10001}
PGID=${PGID-10001}

case "$PUID" in
    ''|*[!0-9]*) die "PUID must be a positive integer" ;;
esac
case "$PGID" in
    ''|*[!0-9]*) die "PGID must be a positive integer" ;;
esac
[ "$PUID" -gt 0 ] 2>/dev/null || die "PUID must be a positive integer"
[ "$PGID" -gt 0 ] 2>/dev/null || die "PGID must be a positive integer"

[ "$(id -u)" -eq 0 ] || die "entrypoint must start as root; remove the Compose user override"

existing_user=$(getent passwd "$PUID" || true)
if [ -n "$existing_user" ] && [ "${existing_user%%:*}" != appuser ]; then
    die "PUID $PUID is already used by another user"
fi

existing_group=$(getent group "$PGID" || true)
if [ -n "$existing_group" ] && [ "${existing_group%%:*}" != appgroup ]; then
    die "PGID $PGID is already used by another group"
fi

if [ "$(id -g appuser)" -ne "$PGID" ]; then
    groupmod --gid "$PGID" appgroup || die "could not set appgroup to PGID $PGID"
fi

if [ "$(id -u appuser)" -ne "$PUID" ]; then
    usermod --uid "$PUID" appuser || die "could not set appuser to PUID $PUID"
fi

if [ "$(id -g appuser)" -ne "$PGID" ]; then
    usermod --gid "$PGID" appuser || die "could not set appuser to PGID $PGID"
fi

[ "$(id -u appuser)" -eq "$PUID" ] \
    || die "appuser UID does not match PUID"
[ "$(id -g appuser)" -eq "$PGID" ] \
    || die "appuser GID does not match PGID"

chown -R appuser:appgroup /data/downloads /opt/cloakbrowser-cache /home/appuser \
    || die "could not initialize writable directory ownership"

export HOME=/home/appuser
export USER=appuser
export LOGNAME=appuser

exec gosu appuser:appgroup "$@"
