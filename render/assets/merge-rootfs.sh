#!/bin/sh
# Merge an imported image filesystem into the bundle's root.
#
# Used for services no recipe claimed, where the only way to get the software is to take
# it from its own image. A plain `COPY --from=x / /` would clobber the base image's
# /etc/passwd, /etc/nsswitch.conf and so on, breaking every other service in the bundle.
# So instead:
#
#   * files that already exist are kept (the base image and earlier imports win),
#   * kernel and scratch directories are skipped entirely,
#   * missing users and groups are appended rather than overwritten.
#
# Usage: merge-rootfs.sh /tmp/rootfs/<stage>
set -eu

src=${1:?usage: merge-rootfs.sh <rootfs-dir>}
[ -d "$src" ] || { echo "merge-rootfs: $src is not a directory" >&2; exit 1; }

# Append accounts the imported image defines that the bundle does not have yet, matching
# on name so that a re-run is idempotent.
append_missing() {
    file=$1
    [ -f "$src$file" ] || return 0
    [ -f "$file" ] || return 0
    while IFS= read -r line; do
        name=${line%%:*}
        [ -n "$name" ] || continue
        if ! cut -d: -f1 "$file" | grep -qx "$name"; then
            printf '%s\n' "$line" >> "$file"
        fi
    done < "$src$file"
}

append_missing /etc/passwd
append_missing /etc/group

tar -C "$src" \
    --exclude=./proc --exclude=./sys --exclude=./dev \
    --exclude=./tmp --exclude=./run --exclude=./var/run \
    --exclude=./etc/passwd --exclude=./etc/group --exclude=./etc/shadow \
    --exclude=./etc/hosts --exclude=./etc/hostname --exclude=./etc/resolv.conf \
    --exclude=./etc/nsswitch.conf --exclude=./etc/localtime \
    -cf - . \
| tar -C / --skip-old-files -xf -

rm -rf "$src"
echo "merge-rootfs: merged $src"
