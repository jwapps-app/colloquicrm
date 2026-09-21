#!/bin/sh
set -e

# The container starts as root only so this script can hand the attachments
# directory to the unprivileged `app` user (created in the Dockerfile); the
# migrations and the server then run as that user. Deployments that predate
# this have a bind mount full of root-owned files — those are chowned once,
# here, on the first start of the new image.
#
# Storage ownership must never stop the app from starting. If the directory
# can't be handed over (a filesystem that refuses chown, a read-only mount,
# NAS ACLs, dropped capabilities), say so in one line and carry on as root,
# exactly as every image before this one did.
APP_UID=10001
APP_GID=10001
DIR="${ATTACHMENTS_DIR:-./data/attachments}"
DROP="setpriv --reuid=$APP_UID --regid=$APP_GID --clear-groups --no-new-privs"

# True when something under DIR (or DIR itself) isn't the app user's.
foreign_files() {
    [ -n "$(find "$DIR" \( ! -user "$APP_UID" -o ! -group "$APP_GID" \) -print 2>/dev/null | head -n 1)" ]
}

# The check that actually matters: through the very command that will launch
# the server, can the app user create and remove a file in DIR?
app_can_write() {
    $DROP sh -c 'f="$1/.write-test-$$" && : > "$f" && rm -f "$f"' sh "$DIR" 2>/dev/null
}

run=""
if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DIR" 2>/dev/null || true
    # "/" would mean chowning the whole image; nobody means that.
    if [ "$(cd "$DIR" 2>/dev/null && pwd -P)" != "/" ] && foreign_files; then
        echo "entrypoint: handing $DIR to uid $APP_UID (one-time chown)"
        chown -Rh "$APP_UID:$APP_GID" "$DIR" 2>/dev/null || true
    fi
    # Writable isn't enough: a root-owned 0600 file left behind by a chown
    # that half-worked would break its download, so ownership is re-checked.
    if app_can_write && ! foreign_files; then
        run="$DROP"
        export HOME=/srv
    else
        echo "entrypoint: WARNING: cannot run as uid $APP_UID with $DIR writable (chown refused, read-only mount, or privilege drop not permitted) - running as root, as before" >&2
    fi
fi
# Not root (started with an explicit --user): nothing to prepare and no
# privileges to drop. Whoever chose the user owns making DIR writable.

# Migrations run as the same user as the server: they only need the database,
# and nothing they touch on disk should end up root-owned.
$run alembic upgrade head
exec $run uvicorn app.main:app --host 0.0.0.0 --port 8000
