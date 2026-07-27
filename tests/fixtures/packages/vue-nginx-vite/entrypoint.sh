#!/bin/sh
set -e
[ -d node_modules ] || npm install
exec "$@"
