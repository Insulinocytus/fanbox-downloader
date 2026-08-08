#!/bin/sh
set -eu

root=$(mktemp -d)
trap 'rm -rf "$root"' EXIT

mkdir -p "$root/bin" "$root/data" "$root/cache" "$root/home"
cp docker-entrypoint.sh "$root/entrypoint.sh"
chmod 755 "$root/entrypoint.sh"

printf '10001\n' > "$root/user.uid"
printf '10001\n' > "$root/user.gid"

cat > "$root/bin/id" <<'EOF'
#!/bin/sh
if [ "${FAKE_ROOT_MODE-}" = 1 ] && [ "${1-}" = '-u' ] && [ "$#" -eq 1 ]; then
    printf '0\n'
elif [ "${1-}" = '-u' ]; then
    cat "$FAKE_ROOT/user.uid"
elif [ "${1-}" = '-g' ]; then
    cat "$FAKE_ROOT/user.gid"
else
    printf '%s:%s\n' "$(cat "$FAKE_ROOT/user.uid")" "$(cat "$FAKE_ROOT/user.gid")"
fi
EOF

cat > "$root/bin/getent" <<'EOF'
#!/bin/sh
exit 2
EOF

cat > "$root/bin/groupmod" <<'EOF'
#!/bin/sh
exit 0
EOF

cat > "$root/bin/usermod" <<'EOF'
#!/bin/sh
if [ "$1" = '--uid' ]; then
    printf '%s\n' "$2" > "$FAKE_ROOT/user.uid"
elif [ "$1" = '--gid' ]; then
    printf '%s\n' "$2" > "$FAKE_ROOT/user.gid"
fi
EOF

cat > "$root/bin/chown" <<'EOF'
#!/bin/sh
exit 0
EOF

cat > "$root/bin/gosu" <<'EOF'
#!/bin/sh
shift
exec env FAKE_ROOT_MODE=0 "$@"
EOF

cat > "$root/bin/python" <<'EOF'
printf '%s:%s\n' "$(id -u)" "$(id -g)" > "$IDENTITY_FILE"
EOF

PATH="$root/bin:$PATH" \
FAKE_ROOT="$root" \
FAKE_ROOT_MODE=1 \
PUID=3000 \
PGID=3000 \
IDENTITY_FILE="$root/identity" \
HOME="$root/home" \
"$root/entrypoint.sh" python -c 'ignored'

test "$(cat "$root/identity")" = "3000:3000"
