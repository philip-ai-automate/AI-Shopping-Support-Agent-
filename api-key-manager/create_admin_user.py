import os
import getpass
import mysql.connector
from dotenv import load_dotenv

load_dotenv()


def main():
    host = os.getenv("DB_HOST")
    user = os.getenv("DB_USER")
    password = os.getenv("DB_PASSWORD")
    db = os.getenv("DB_NAME")

    if not all([host, user, password, db]):
        print("ERROR: DB_HOST, DB_USER, DB_PASSWORD, DB_NAME must be set in .env")
        return

    username = input("Admin username (e.g. admin): ").strip()
    if not username:
        print("ERROR: username required")
        return

    pw1 = getpass.getpass("Admin password: ").strip()
    pw2 = getpass.getpass("Confirm password: ").strip()
    if not pw1 or pw1 != pw2:
        print("ERROR: passwords do not match or are empty")
        return

    conn = mysql.connector.connect(host=host, user=user, password=password, database=db)
    cur = conn.cursor()

    # Ensure table exists (compatible with existing key-manager admin login)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_users (
            id INT PRIMARY KEY AUTO_INCREMENT,
            username VARCHAR(255) NOT NULL UNIQUE,
            password VARCHAR(255) NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB
        """
    )

    try:
        cur.execute("INSERT INTO admin_users (username, password) VALUES (%s, %s)", (username, pw1))
        conn.commit()
    except Exception as e:
        print(f"ERROR inserting admin: {e}")
        conn.rollback()
        return
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass

    print("✅ Admin created successfully!")
    print("Login here: https://portal.phixtra.com/admin/login")


if __name__ == "__main__":
    main()
