#!/usr/bin/env python3
"""
migrate_data.py — Copy all rows from MySQL ai_support → PostgreSQL ai_support
Safe: reads from MySQL, writes to PostgreSQL, never touches MySQL.
Run: python3 /root/phixtra-app/migrate_data.py
"""
import os, sys, json, datetime, decimal

import mysql.connector
import psycopg2
import psycopg2.extras

PG_PASS = open("/root/phixtra-app/.pg_password").read().strip()

MY = mysql.connector.connect(
    host="localhost", user="ai_user",
    password="./Admin@15365858!", database="ai_support"
)
PG = psycopg2.connect(
    host="localhost", port=5432, user="phixtra_pg",
    password=PG_PASS, dbname="ai_support"
)
PG.autocommit = False

def val(v):
    """Convert MySQL types to PostgreSQL-safe Python types."""
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, bytearray):
        return bytes(v)
    return v

def get_bool_cols(table):
    """Return set of column names that are BOOLEAN type in PostgreSQL."""
    cur = PG.cursor()
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s AND data_type='boolean'",
        (table,)
    )
    cols = {row[0] for row in cur.fetchall()}
    cur.close()
    return cols

def migrate_table(table, order_by="id", skip_cols=None):
    """Copy all rows from a MySQL table into the matching PostgreSQL table."""
    skip_cols = skip_cols or []
    my_cur = MY.cursor(dictionary=True)
    my_cur.execute(f"SELECT * FROM `{table}` ORDER BY {order_by}")
    rows = my_cur.fetchall()
    my_cur.close()

    if not rows:
        print(f"  {table}: 0 rows (empty)")
        return 0

    bool_cols = get_bool_cols(table)

    pg_cur = PG.cursor()
    pg_cur.execute(f"DELETE FROM {table}")   # clear any previous run

    cols = [c for c in rows[0].keys() if c not in skip_cols]
    placeholders = ",".join(["%s"] * len(cols))
    col_list = ",".join(f'"{c}"' for c in cols)
    sql = f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders}) ON CONFLICT DO NOTHING'

    def row_val(col, v):
        if col in bool_cols and isinstance(v, int):
            return bool(v)
        return val(v)

    count = 0
    for row in rows:
        try:
            pg_cur.execute(sql, [row_val(c, row[c]) for c in cols])
            count += 1
        except Exception as e:
            PG.rollback()
            print(f"  ✗ {table} row error: {e} — row keys: {list(row.keys())[:5]}")
            pg_cur = PG.cursor()

    PG.commit()
    pg_cur.close()
    print(f"  ✓ {table}: {count}/{len(rows)} rows")
    return count

# ── Migration order respects FK dependencies ─────────────────────────────────
tables = [
    ("tenants",                  "id"),
    ("admin_users",              "id"),
    ("admins",                   "id"),
    ("api_keys",                 "id"),
    ("audit_logs",               "id"),
    ("chat_sessions",            "session_id"),
    ("chat_messages",            "id"),
    ("chat_summaries",           "session_id"),
    ("cart_events",              "id"),
    ("abandonment_queue",        "id"),
    ("credit_packages",          "id"),
    ("customer_alert_state",     "customer_id"),
    ("customers",                "id"),
    ("data_sources",             "id"),
    ("handoff_requests",         "id"),
    ("handoff_rules",            "id"),
    ("invoices",                 "id"),
    ("merchant_bank_accounts",   "id"),
    ("onboarding_state",         "customer_id"),
    ("orders",                   "id"),
    ("order_items",              "id"),
    ("order_reference_seq",      "tenant_id"),
    ("payment_gateways",         "id"),
    ("plugin_downloads",         "id"),
    ("portal_settings",          "id"),
    ("products",                 "id"),
    ("push_subscriptions",       "id"),
    ("recovery_log",             "id"),
    ("saved_payment_methods",    "id"),
    ("stock_notifications",      "id"),
    ("subscription_invoices",    "id"),
    ("subscriptions",            "id"),
    ("system_settings",          "setting_key"),
    ("tenant_balances",          "tenant_id"),
    ("trial_reminder_state",     "api_key_id"),
    ("trial_reminders",          "id"),
    ("trial_signups",            "id"),
    ("usage_events",             "id"),
    ("wa_campaigns",             "id"),
    ("wa_contacts",              "id"),
    ("wa_handoff_state",         "id"),
    ("wa_merchant_onboarding",   "id"),
    ("wa_message_log",           "id"),
    ("wa_portal_otp",            "id"),
    ("wa_proactive_log",         "id"),
    ("wa_product_cache",         "session_id"),
    ("wa_templates",             "id"),
    ("wa_tenants",               "id"),
]

print("Starting MySQL → PostgreSQL data migration...")
print("(MySQL ai_support is NOT modified)\n")

total = 0
for table, order in tables:
    try:
        total += migrate_table(table, order_by=order)
    except Exception as e:
        print(f"  ✗ SKIPPED {table}: {e}")

print(f"\nDone. Total rows migrated: {total}")
MY.close()
PG.close()
