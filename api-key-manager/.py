from flask import Flask, render_template, request, redirect, url_for, session
import bcrypt
import secrets
import string
from datetime import datetime
from db import get_db_connection

app = Flask(__name__)
app.secret_key = "super-secret-change-this"

ADMIN_USER = "admin"
ADMIN_PASS = "profitbuyzadmin"

def generate_api_key():
    alphabet = string.ascii_letters + string.digits
    plain_key = ''.join(secrets.choice(alphabet) for _ in range(20))
    hashed_key = bcrypt.hashpw(plain_key.encode(), bcrypt.gensalt()).decode()
    return plain_key, hashed_key

def login_required():
    return session.get("logged_in") is True

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form["username"] == ADMIN_USER and request.form["password"] == ADMIN_PASS:
            session["logged_in"] = True
            return redirect(url_for("index"))
        else:
            error = "Invalid login"
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/", methods=["GET", "POST"])
def index():
    if not login_required():
        return redirect(url_for("login"))

    plain_key = None
    created_at = None

    if request.method == "POST":
        website = request.form["website"]
        tenant_id = request.form["tenant_id"]

        plain_key, hashed_key = generate_api_key()
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO api_keys (tenant_id, api_key_hash, is_active, website)
            VALUES (%s, %s, 1, %s)
        """, (tenant_id, hashed_key, website))

        conn.commit()
        cursor.close()
        conn.close()

    return render_template("index.html", plain_key=plain_key, created_at=created_at)

if __name__ == "__main__":
    app.run()
