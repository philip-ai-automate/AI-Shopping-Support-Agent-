"""One-off (Nov 2026): turn every Admin plain-text password into a scrambled
(bcrypt) copy and empty the plain-text column. Run once at go-live, after a
copy of admin_users is saved. Safe to run again (already-done rows skipped).

    venv/bin/python scramble_admin_passwords.py            # do it
    venv/bin/python scramble_admin_passwords.py --check    # just count
"""
import sys
import bcrypt
from db import get_db_connection

conn = get_db_connection()
cur = conn.cursor()
cur.execute("SELECT id, username, password, password_hash FROM admin_users ORDER BY id")
rows = cur.fetchall()
todo = [r for r in rows if r[2]]
print(f"{len(rows)} admin logins; {len(todo)} still have a plain-text password")
if "--check" in sys.argv:
    sys.exit(0)
for i, username, plain, old_hash in todo:
    # The login checks the plain-text column today, so that is the password
    # that works now; it becomes the scrambled copy.
    new_hash = bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()
    cur.execute("UPDATE admin_users SET password_hash=%s, password='' WHERE id=%s", (new_hash, i))
    print(f"  scrambled: {username}")
conn.commit()
cur.close(); conn.close()
print("done")
