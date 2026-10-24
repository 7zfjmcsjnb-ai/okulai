#!/bin/bash
set -e
cd /opt/okulai
curl -fsSL "https://github.com/$REPO_USER/okulai/archive/refs/heads/main.tar.gz" | tar xz --strip-components=1
[ -f .env ] || printf 'SECRET_KEY=%s\n' "$(openssl rand -hex 32)" > .env
D=$(tr -d ' \r\n' < domain.txt 2>/dev/null || true)
case "$D" in *.*) ;; *) D="" ;; esac
grep -v -e '^DOMAIN=' -e '^ADMIN_USER=' -e '^ADMIN_PASS=' .env > .env.new || true
printf 'DOMAIN=%s\nADMIN_USER=%s\nADMIN_PASS=%s\n' "$D" "$ADMIN_USER" "$ADMIN_PASS" >> .env.new
mv .env.new .env
mkdir -p data
docker compose up -d --build
