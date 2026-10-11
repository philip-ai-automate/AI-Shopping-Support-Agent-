"""Daily market exchange rates for selling currencies (2026-10-11).

Run once a day via cron:

  cd /root/phixtra-app/api-key-manager && ./venv/bin/python3 fx_rates_job.py

Fetches rates from exchangerate-api.com (free, no key — the same service the
WhatsApp gateway already uses) and saves them to fx_rates as units per 1 US
dollar. If the fetch fails or looks wrong, the last good rates are kept and
nothing is guessed.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime
from decimal import Decimal

from db import get_db_connection

RATES_URL = "https://api.exchangerate-api.com/v4/latest/USD"


def fetch_rates(url: str = RATES_URL) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "PhiXtra-Portal/fx-rates"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if (data.get("base") or "").upper() != "USD":
        raise ValueError(f"unexpected base {data.get('base')!r}")
    rates = {}
    for code, value in (data.get("rates") or {}).items():
        code = str(code).upper()
        if len(code) == 3 and code.isalpha():
            d = Decimal(str(value))
            if d > 0:
                rates[code] = d
    return rates


def run(url: str = RATES_URL) -> int:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db_connection()
    if not conn:
        print(f"[{stamp}] ❌ no database connection — rates unchanged")
        return 1
    try:
        cur = conn.cursor()
        cur.execute("SELECT code FROM selling_currencies")
        needed = {r[0].upper() for r in cur.fetchall()} | {"USD"}
        try:
            rates = fetch_rates(url)
        except Exception as e:
            print(f"[{stamp}] ❌ fetch failed ({e}) — last good rates kept")
            return 1
        missing = sorted(needed - set(rates))
        if missing or rates.get("USD") != 1:
            print(f"[{stamp}] ❌ rates look wrong (missing {missing}, USD={rates.get('USD')}) "
                  "— last good rates kept")
            return 1
        for code, per_usd in rates.items():
            cur.execute("""
                INSERT INTO fx_rates (currency, per_usd, fetched_at) VALUES (%s, %s, NOW())
                ON CONFLICT (currency) DO UPDATE
                   SET per_usd = EXCLUDED.per_usd, fetched_at = EXCLUDED.fetched_at""",
                        (code, per_usd))
        conn.commit()
        shown = ", ".join(f"{c} {rates[c]}" for c in sorted(needed))
        print(f"[{stamp}] ✅ saved {len(rates)} rates per 1 USD ({shown})")
        return 0
    except Exception as e:
        conn.rollback()
        print(f"[{stamp}] ❌ save failed ({e}) — last good rates kept")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(run(*sys.argv[1:2]))
