# Encrypted database area (7 Nov 2026, Shopify 7A item 9)

- PostgreSQL data folder: `/srv/pgcrypt/14/main`, on a LUKS2 (aes-xts, 512-bit) container `/vault/pgcrypt.img` (30 GB).
- Unlocked at boot by `phixtra-pgcrypt.service` → `/usr/local/sbin/phixtra-pgcrypt start`, key file `/etc/phixtra/pg-volume.key`.
  `postgresql@14-main` has a drop-in (`pgcrypt.conf`) that Requires/After it.
- LUKS header backup: `/etc/phixtra/pgcrypt-luks-header.bak`.
- Buyer-detail field encryption key (chat_visitors, Shopify contact names): `/etc/phixtra/data.key` (see `data_crypto.py`).
- Both keys: copies in the owner's password manager and in the encrypted nightly restic backup (/etc). Never in git.
- `move_db_to_encrypted.sh` was the one-off move (run 7 Nov 2026 13:53, 4 s downtime).
