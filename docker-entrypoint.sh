#!/bin/sh
# Starts as root only to make sure the data folder belongs to the app user,
# whoever created it on the host (Docker creates a missing bind mount as
# root), then runs the app as that user.
set -e

if [ "$(id -u)" = "0" ]; then
  mkdir -p /data
  chown -R social:social /data
  exec setpriv --reuid=social --regid=social --init-groups -- "$@"
fi

exec "$@"
