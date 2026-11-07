#!/bin/bash
# Move the PostgreSQL data folder onto the encrypted area (Shopify 7A item 9, 7 Nov 2026).
#   move_db_to_encrypted.sh move      do it (database down ~1 minute)
#   move_db_to_encrypted.sh undo      go back to the old folder
# Needs phixtra-pgcrypt.service active (/srv/pgcrypt mounted).
set -euo pipefail
OLD=/var/lib/postgresql/14/main
NEW=/srv/pgcrypt/14/main
CONF=/etc/postgresql/14/main/postgresql.conf
DROPIN=/etc/systemd/system/postgresql@14-main.service.d/pgcrypt.conf
B=/root/backups/item9_step2_$(date +%Y%m%d_%H%M)
q() { sudo -u postgres psql -XAtq -d ai_support -c "$1"; }

counts() {
  q "SELECT current_setting('data_directory')"
  q "SELECT (SELECT count(*) FROM tenants)||' tenants, '||(SELECT count(*) FROM chat_visitors)||' chat_visitors, '||(SELECT count(*) FROM shopify_b2b_contacts)||' shopify contacts'"
  q "SELECT string_agg(datname||'='||pg_database_size(datname)/1024/1024||'MB', ' ' ORDER BY datname) FROM pg_database WHERE datname NOT LIKE 'template%'"
}

case "${1:-}" in
move)
  mountpoint -q /srv/pgcrypt || { echo "encrypted area not mounted"; exit 1; }
  [ -e "$NEW/PG_VERSION" ] && { echo "$NEW already has a database, stop"; exit 1; }
  mkdir -p "$B"; chmod 700 "$B"
  echo "== before"; counts | tee "$B/before.txt"
  sudo -u postgres pg_dumpall | gzip > "$B/pg_dumpall.sql.gz"; echo "dump $(du -h "$B/pg_dumpall.sql.gz" | cut -f1)"
  cp -a "$CONF" "$B/postgresql.conf.before"
  echo "== stopping database $(date +%T)"
  systemctl stop postgresql@14-main
  mkdir -p "$NEW"; chown -R postgres:postgres /srv/pgcrypt/14; chmod 700 /srv/pgcrypt/14 "$NEW"
  rsync -aHAX "$OLD/" "$NEW/"
  sed -i "s|^data_directory = .*|data_directory = '$NEW'\t\t# moved to encrypted area 7 Nov 2026 (was $OLD)|" "$CONF"
  mkdir -p "$(dirname "$DROPIN")"
  printf '[Unit]\nRequires=phixtra-pgcrypt.service\nAfter=phixtra-pgcrypt.service\n' > "$DROPIN"
  systemctl daemon-reload
  systemctl start postgresql@14-main
  echo "== started $(date +%T)"
  mv "$OLD" "$OLD.old-unencrypted"; mkdir -m 700 "$OLD"; chown postgres:postgres "$OLD"
  echo "== after"; counts | tee "$B/after.txt"
  diff <(tail -n +2 "$B/before.txt") <(tail -n +2 "$B/after.txt") && echo "COUNTS MATCH" || echo "!! COUNTS DIFFER"
  echo "backup folder: $B" ;;
undo)
  systemctl stop postgresql@14-main
  [ -d "$OLD.old-unencrypted" ] && { rmdir "$OLD" 2>/dev/null || true; mv "$OLD.old-unencrypted" "$OLD"; }
  sed -i "s|^data_directory = .*|data_directory = '$OLD'\t\t# use ConfigDir|" "$CONF"
  rm -f "$DROPIN"; systemctl daemon-reload
  systemctl start postgresql@14-main
  counts ;;
*) echo "usage: $0 move|undo"; exit 2 ;;
esac
