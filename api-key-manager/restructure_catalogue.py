"""
Restructure Phixtra catalogue from brand-specific categories to unified device-type categories.

Before (15 brand categories + Mobile Phones + legacy Laptops):
  HP Laptops, Dell Laptops, Lenovo Laptops
  HP Desktops, Dell Desktops, Lenovo Desktops
  HP Servers, Dell Servers, Lenovo Servers
  HP Thin Clients, Dell Thin Clients, Lenovo Thin Clients
  HP Monitors, Dell Monitors, Lenovo Monitors

After (6 unified categories):
  Laptops    — HP + Dell + Lenovo + legacy, brand filterable via brand column
  Desktops   — HP + Dell + Lenovo
  Servers    — HP + Dell + Lenovo
  Thin Clients — HP + Dell + Lenovo
  Monitors   — HP + Dell + Lenovo
  Mobile Phones — unchanged

The UI already filters by brand via catalogue_products.brand — no code changes needed.
"""

import os
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

# ─────────────────────────────────────────────────────────────────────────────
# TARGET CATEGORY DEFINITIONS
# Each target maps to the slugs of the source categories to absorb.
# "reuse_slug" means update the existing category with this slug rather than
# creating a new one (avoids slug conflict).
# ─────────────────────────────────────────────────────────────────────────────

TARGETS = [
    {
        "name":        "Laptops",
        "slug":        "laptops",
        "icon":        "💻",
        "description": "Business and consumer laptops — HP, Dell, Lenovo and more. Filter by brand to narrow your selection.",
        "sort_order":  1,
        "reuse_slug":  "laptops",   # update the existing legacy Laptops category
        "sources":     ["hp-laptops", "dell-laptops", "lenovo-laptops"],
        "attributes": [
            # key, label, type, unit, filterable, required, sort
            ("series",           "Series",           "text",   "",      True,  False, 1),
            ("processor",        "Processor",        "text",   "",      True,  True,  2),
            ("ram",              "RAM",              "text",   "",      True,  True,  3),
            ("storage",          "Storage",          "text",   "",      True,  True,  4),
            ("display_size",     "Display Size",     "number", "inches",True,  False, 5),
            ("display_type",     "Display Type",     "text",   "",      False, False, 6),
            ("graphics",         "Graphics",         "text",   "",      False, False, 7),
            ("operating_system", "Operating System", "text",   "",      True,  False, 8),
            ("battery_life",     "Battery Life",     "text",   "hrs",   False, False, 9),
            ("weight_kg",        "Weight",           "text",   "kg",    False, False, 10),
            ("price_tier",       "Price Tier",       "text",   "",      True,  False, 11),
            ("nigeria_market_price_naira", "Price (₦)", "number", "₦",  False, False, 12),
        ],
    },
    {
        "name":        "Desktops",
        "slug":        "desktops",
        "icon":        "🖥️",
        "description": "Business and workstation desktops — HP, Dell, Lenovo and more. Filter by brand to narrow your selection.",
        "sort_order":  3,
        "reuse_slug":  None,
        "sources":     ["hp-desktops", "dell-desktops", "lenovo-desktops"],
        "attributes": [
            ("series",           "Series",           "text",   "",      True,  False, 1),
            ("processor",        "Processor",        "text",   "",      True,  True,  2),
            ("ram",              "RAM",              "text",   "",      True,  True,  3),
            ("storage",          "Storage",          "text",   "",      True,  True,  4),
            ("form_factor",      "Form Factor",      "text",   "",      True,  False, 5),
            ("graphics",         "Graphics",         "text",   "",      False, False, 6),
            ("operating_system", "Operating System", "text",   "",      True,  False, 7),
            ("optical_drive",    "Optical Drive",    "text",   "",      False, False, 8),
            ("price_tier",       "Price Tier",       "text",   "",      True,  False, 9),
            ("nigeria_market_price_naira", "Price (₦)", "number", "₦",  False, False, 10),
        ],
    },
    {
        "name":        "Servers",
        "slug":        "servers",
        "icon":        "🖧",
        "description": "Rack and tower servers — HP ProLiant, Dell PowerEdge, Lenovo ThinkSystem and more.",
        "sort_order":  4,
        "reuse_slug":  None,
        "sources":     ["hp-servers", "dell-servers", "lenovo-servers"],
        "attributes": [
            ("series",           "Series",           "text",   "",      True,  False, 1),
            ("processor",        "Processor",        "text",   "",      True,  True,  2),
            ("ram",              "RAM",              "text",   "",      True,  True,  3),
            ("storage",          "Storage",          "text",   "",      True,  True,  4),
            ("form_factor",      "Form Factor",      "text",   "",      True,  False, 5),
            ("drive_bays",       "Drive Bays",       "text",   "",      False, False, 6),
            ("max_ram",          "Max RAM",          "text",   "",      False, False, 7),
            ("network_ports",    "Network Ports",    "text",   "",      False, False, 8),
            ("price_tier",       "Price Tier",       "text",   "",      True,  False, 9),
            ("nigeria_market_price_naira", "Price (₦)", "number", "₦",  False, False, 10),
        ],
    },
    {
        "name":        "Thin Clients",
        "slug":        "thin-clients",
        "icon":        "🖱️",
        "description": "Thin clients for VDI and cloud computing — HP, Dell Wyse, Lenovo ThinkEdge and more.",
        "sort_order":  5,
        "reuse_slug":  None,
        "sources":     ["hp-thin-clients", "dell-thin-clients", "lenovo-thin-clients"],
        "attributes": [
            ("series",           "Series",           "text",   "",      True,  False, 1),
            ("processor",        "Processor",        "text",   "",      True,  False, 2),
            ("ram",              "RAM",              "text",   "",      True,  True,  3),
            ("storage",          "Storage",          "text",   "",      True,  True,  4),
            ("operating_system", "Operating System", "text",   "",      True,  False, 5),
            ("display_support",  "Display Support",  "text",   "",      False, False, 6),
            ("form_factor",      "Form Factor",      "text",   "",      True,  False, 7),
            ("price_tier",       "Price Tier",       "text",   "",      True,  False, 8),
            ("nigeria_market_price_naira", "Price (₦)", "number", "₦",  False, False, 9),
        ],
    },
    {
        "name":        "Monitors",
        "slug":        "monitors",
        "icon":        "🖥",
        "description": "Business, professional and gaming monitors — HP, Dell, Lenovo and more. Filter by brand, size and resolution.",
        "sort_order":  6,
        "reuse_slug":  None,
        "sources":     ["hp-monitors", "dell-monitors", "lenovo-monitors"],
        "attributes": [
            ("series",           "Series",           "text",   "",      True,  False, 1),
            ("screen_size",      "Screen Size",      "number", "inches",True,  True,  2),
            ("resolution",       "Resolution",       "text",   "",      True,  False, 3),
            ("panel_type",       "Panel Type",       "text",   "",      True,  False, 4),
            ("refresh_rate",     "Refresh Rate",     "text",   "",      True,  False, 5),
            ("response_time",    "Response Time",    "text",   "ms",    False, False, 6),
            ("connectivity",     "Connectivity",     "text",   "",      False, False, 7),
            ("price_tier",       "Price Tier",       "text",   "",      True,  False, 8),
            ("nigeria_market_price_naira", "Price (₦)", "number", "₦",  False, False, 9),
        ],
    },
]


def run():
    conn = psycopg2.connect(**DB)
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    for target in TARGETS:
        print(f"\n{'='*60}")
        print(f"Processing: {target['name']}")

        # ── 1. Get or create target category ─────────────────────────────
        if target["reuse_slug"]:
            cur.execute("SELECT id FROM catalogue_categories WHERE slug=%s", (target["reuse_slug"],))
            row = cur.fetchone()
            if row:
                target_id = row["id"]
                cur.execute("""
                    UPDATE catalogue_categories
                    SET name=%s, icon=%s, description=%s, sort_order=%s, is_active=TRUE
                    WHERE id=%s
                """, (target["name"], target["icon"], target["description"], target["sort_order"], target_id))
                print(f"  Reusing existing category id={target_id} ({target['reuse_slug']})")
            else:
                cur.execute("""
                    INSERT INTO catalogue_categories (name, slug, icon, description, sort_order, is_active, created_by)
                    VALUES (%s,%s,%s,%s,%s,TRUE,'system') RETURNING id
                """, (target["name"], target["slug"], target["icon"], target["description"], target["sort_order"]))
                target_id = cur.fetchone()["id"]
                print(f"  Created new category id={target_id}")
        else:
            cur.execute("""
                INSERT INTO catalogue_categories (name, slug, icon, description, sort_order, is_active, created_by)
                VALUES (%s,%s,%s,%s,%s,TRUE,'system')
                ON CONFLICT (slug) DO UPDATE
                    SET name=EXCLUDED.name, icon=EXCLUDED.icon,
                        description=EXCLUDED.description, sort_order=EXCLUDED.sort_order,
                        is_active=TRUE
                RETURNING id
            """, (target["name"], target["slug"], target["icon"], target["description"], target["sort_order"]))
            row = cur.fetchone()
            if not row:
                cur.execute("SELECT id FROM catalogue_categories WHERE slug=%s", (target["slug"],))
                target_id = cur.fetchone()["id"]
            else:
                target_id = row["id"]
            print(f"  Target category id={target_id}")

        # ── 2. Upsert unified attribute definitions ───────────────────────
        target_attr_map = {}  # key → def_id in target category
        for (key, label, dtype, unit, filterable, required, sort) in target["attributes"]:
            cur.execute("""
                INSERT INTO catalogue_attribute_definitions
                    (category_id, attribute_key, attribute_label, data_type, unit,
                     is_filterable, is_required, sort_order)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING
                RETURNING id
            """, (target_id, key, label, dtype, unit, filterable, required, sort))
            row = cur.fetchone()
            if not row:
                cur.execute(
                    "SELECT id FROM catalogue_attribute_definitions WHERE category_id=%s AND attribute_key=%s",
                    (target_id, key)
                )
                row = cur.fetchone()
            target_attr_map[key] = row["id"]
        conn.commit()
        print(f"  Upserted {len(target_attr_map)} attribute definitions")

        # ── 3. Migrate each source category ──────────────────────────────
        for source_slug in target["sources"]:
            cur.execute("SELECT id, name FROM catalogue_categories WHERE slug=%s", (source_slug,))
            src = cur.fetchone()
            if not src:
                print(f"  Source '{source_slug}' not found — skipping")
                continue
            src_id   = src["id"]
            src_name = src["name"]

            # Count products to move
            cur.execute("SELECT COUNT(*) AS n FROM catalogue_products WHERE category_id=%s", (src_id,))
            n_products = cur.fetchone()["n"]

            # Build attribute def ID mapping: src_def_id → target_def_id
            cur.execute(
                "SELECT id, attribute_key FROM catalogue_attribute_definitions WHERE category_id=%s",
                (src_id,)
            )
            src_attrs = cur.fetchall()
            def_map = {}
            for sa in src_attrs:
                new_id = target_attr_map.get(sa["attribute_key"])
                if new_id:
                    def_map[sa["id"]] = new_id

            # Remap product attribute def IDs (bulk update per old def)
            attr_updated = 0
            for old_id, new_id in def_map.items():
                cur.execute("""
                    UPDATE catalogue_product_attributes
                    SET attribute_def_id = %s
                    WHERE attribute_def_id = %s
                """, (new_id, old_id))
                attr_updated += cur.rowcount

            # Move products to target category
            cur.execute(
                "UPDATE catalogue_products SET category_id=%s WHERE category_id=%s",
                (target_id, src_id)
            )
            moved = cur.rowcount

            # Delete old attribute defs (product_attributes now point to target defs)
            cur.execute("DELETE FROM catalogue_attribute_definitions WHERE category_id=%s", (src_id,))

            # Delete old category
            cur.execute("DELETE FROM catalogue_categories WHERE id=%s", (src_id,))

            conn.commit()
            print(f"  ✓ {src_name}: moved {moved} products, remapped {attr_updated} attribute values, deleted category")

        # ── 4. Verify target count ────────────────────────────────────────
        cur.execute(
            "SELECT COUNT(*) AS n FROM catalogue_products WHERE category_id=%s AND is_active=TRUE",
            (target_id,)
        )
        final_count = cur.fetchone()["n"]
        print(f"  → {target['name']} now has {final_count} products")

    # ── Final summary ─────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("FINAL CATALOGUE STATE:")
    cur.execute("""
        SELECT cc.name, cc.icon, cc.sort_order, COUNT(cp.id) AS cnt
        FROM catalogue_categories cc
        LEFT JOIN catalogue_products cp ON cp.category_id = cc.id AND cp.is_active
        WHERE cc.is_active
        GROUP BY cc.id ORDER BY cc.sort_order, cc.name
    """)
    total = 0
    for row in cur.fetchall():
        print(f"  {row['icon']}  {row['name']}: {row['cnt']} products")
        total += row['cnt']
    print(f"\n  GRAND TOTAL: {total} products across all categories")

    cur.close()
    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    run()
