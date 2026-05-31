"""
One-time migration: phone_catalogue → catalogue_products + catalogue_product_attributes.

Creates a "Mobile Phones" category and inserts all active phones from the legacy
phone_catalogue table into the new multi-category catalogue system so the onboarding
wizard can surface them to merchants.

Safe to re-run: uses ON CONFLICT DO NOTHING throughout.
"""

import os, sys
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

DB = dict(
    host=os.environ["PG_HOST"],
    port=int(os.environ.get("PG_PORT", 5432)),
    user=os.environ["PG_USER"],
    password=os.environ["PG_PASSWORD"],
    dbname=os.environ["PG_DB"],
)

ATTRIBUTES = [
    # (attribute_key, attribute_label, data_type, unit, is_filterable, is_required, sort_order)
    ("price_category",          "Price Range",        "text",   "",      True,  False, 1),
    ("network_type",            "Network",            "text",   "",      True,  False, 2),
    ("ram",                     "RAM",                "text",   "",      True,  False, 3),
    ("storage",                 "Storage",            "text",   "",      True,  False, 4),
    ("release_year",            "Release Year",       "number", "",      True,  False, 5),
    ("screen_size_inches",      "Screen Size",        "number", "inches",False, False, 6),
    ("display_type",            "Display",            "text",   "",      False, False, 7),
    ("chipset_model",           "Chipset",            "text",   "",      False, False, 8),
    ("battery_capacity_mah",    "Battery",            "number", "mAh",   False, False, 9),
    ("fast_charging_watts",     "Fast Charging",      "number", "W",     False, False, 10),
    ("rear_camera_main_mp",     "Rear Camera",        "number", "MP",    False, False, 11),
    ("front_camera_mp",         "Front Camera",       "number", "MP",    False, False, 12),
    ("nfc",                     "NFC",                "text",   "",      False, False, 13),
    ("water_resistance",        "Water Resistance",   "text",   "",      False, False, 14),
    ("body_material",           "Body Material",      "text",   "",      False, False, 15),
    ("best_for",                "Best For",           "text",   "",      False, False, 16),
    ("nigeria_market_price_naira", "Price (₦)",       "number", "₦",     False, False, 17),
]

BATCH = 200


def run():
    conn = psycopg2.connect(**DB)
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # ── 1. Create category ────────────────────────────────────────────────────
    cur.execute("""
        INSERT INTO catalogue_categories
            (name, slug, icon, description, sort_order, is_active, created_by)
        VALUES
            ('Mobile Phones', 'mobile-phones', '📱',
             'Smartphones and mobile devices from all major brands', 1, TRUE, 'system')
        ON CONFLICT (slug) DO NOTHING
        RETURNING id
    """)
    row = cur.fetchone()
    if row:
        cat_id = row["id"]
        print(f"✓ Created Mobile Phones category  id={cat_id}")
    else:
        cur.execute("SELECT id FROM catalogue_categories WHERE slug='mobile-phones'")
        cat_id = cur.fetchone()["id"]
        print(f"✓ Mobile Phones category already exists  id={cat_id}")

    # ── 2. Create attribute definitions ──────────────────────────────────────
    attr_id_map = {}   # attribute_key → attribute_def id
    for (key, label, dtype, unit, filterable, required, sort) in ATTRIBUTES:
        cur.execute("""
            INSERT INTO catalogue_attribute_definitions
                (category_id, attribute_key, attribute_label, data_type, unit,
                 is_filterable, is_required, sort_order)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT DO NOTHING
            RETURNING id
        """, (cat_id, key, label, dtype, unit, filterable, required, sort))
        r = cur.fetchone()
        if not r:
            cur.execute(
                "SELECT id FROM catalogue_attribute_definitions "
                "WHERE category_id=%s AND attribute_key=%s",
                (cat_id, key)
            )
            r = cur.fetchone()
        attr_id_map[key] = r["id"]
    conn.commit()
    print(f"✓ Upserted {len(attr_id_map)} attribute definitions")

    # ── 3. Count phones ───────────────────────────────────────────────────────
    cur.execute("SELECT COUNT(*) AS n FROM phone_catalogue WHERE is_active=TRUE")
    total = cur.fetchone()["n"]
    print(f"  Migrating {total} active phones …")

    # ── 4. Migrate phones in batches ─────────────────────────────────────────
    offset = 0
    inserted_products = 0
    inserted_attrs    = 0

    while True:
        cur.execute("""
            SELECT * FROM phone_catalogue
            WHERE is_active=TRUE
            ORDER BY id
            LIMIT %s OFFSET %s
        """, (BATCH, offset))
        rows = cur.fetchall()
        if not rows:
            break

        for phone in rows:
            # Insert into catalogue_products
            cur.execute("""
                INSERT INTO catalogue_products
                    (category_id, brand, model_name, model_number, sku,
                     description, is_active)
                VALUES (%s, %s, %s, %s, %s, %s, TRUE)
                ON CONFLICT (sku) DO NOTHING
                RETURNING id
            """, (
                cat_id,
                phone["brand"],
                phone["model_name"],
                phone["variant_name"],
                phone["product_id"],       # unique sku
                phone["ai_summary"],
            ))
            r = cur.fetchone()
            if not r:
                # Already exists — get its id for attribute sync
                cur.execute(
                    "SELECT id FROM catalogue_products WHERE sku=%s",
                    (phone["product_id"],)
                )
                r = cur.fetchone()
                if not r:
                    continue
            else:
                inserted_products += 1

            prod_id = r["id"]

            # Insert attributes
            attr_values = {
                "price_category":           phone["price_category"],
                "network_type":             phone["network_type"],
                "ram":                      phone["ram"],
                "storage":                  phone["storage"],
                "release_year":             str(phone["release_year"]) if phone["release_year"] else None,
                "screen_size_inches":       str(phone["screen_size_inches"]) if phone["screen_size_inches"] else None,
                "display_type":             phone["display_type"],
                "chipset_model":            phone["chipset_model"],
                "battery_capacity_mah":     str(phone["battery_capacity_mah"]) if phone["battery_capacity_mah"] else None,
                "fast_charging_watts":      str(phone["fast_charging_watts"]) if phone["fast_charging_watts"] else None,
                "rear_camera_main_mp":      str(phone["rear_camera_main_mp"]) if phone["rear_camera_main_mp"] else None,
                "front_camera_mp":          str(phone["front_camera_mp"]) if phone["front_camera_mp"] else None,
                "nfc":                      phone["nfc"],
                "water_resistance":         phone["water_resistance"],
                "body_material":            phone["body_material"],
                "best_for":                 phone["best_for"],
                "nigeria_market_price_naira": str(int(phone["nigeria_market_price_naira"])) if phone["nigeria_market_price_naira"] else None,
            }

            for key, val in attr_values.items():
                if val is None or val == "":
                    continue
                def_id = attr_id_map.get(key)
                if not def_id:
                    continue
                cur.execute("""
                    INSERT INTO catalogue_product_attributes (product_id, attribute_def_id, value)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (product_id, attribute_def_id) DO NOTHING
                """, (prod_id, def_id, val))
                inserted_attrs += 1

        conn.commit()
        offset += BATCH
        done = min(offset, total)
        print(f"  … {done}/{total} processed", end="\r", flush=True)

    print(f"\n✓ Products inserted : {inserted_products}")
    print(f"✓ Attributes inserted: {inserted_attrs}")

    # ── 5. Verify ─────────────────────────────────────────────────────────────
    cur.execute(
        "SELECT COUNT(*) AS n FROM catalogue_products WHERE category_id=%s AND is_active=TRUE",
        (cat_id,)
    )
    final = cur.fetchone()["n"]
    print(f"✓ Total Mobile Phones in catalogue_products: {final}")

    cur.close()
    conn.close()
    print("Done.")


if __name__ == "__main__":
    run()
