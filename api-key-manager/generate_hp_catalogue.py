"""
Generate ~3,000 HP business products across 5 categories:
  HP Laptops (~800), HP Desktops (~600), HP Servers (~400),
  HP Thin Clients (~400), HP Monitors (~800)

Each product has: brand, model_name, variant (model_number), SKU, description,
category-specific attributes, and a Nigeria market price.

Safe to re-run — uses ON CONFLICT DO NOTHING throughout.
"""

import os, re
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

BATCH = 200
BRAND = "HP"


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


# ─────────────────────────────────────────────────────────────────────────────
# CATEGORY + ATTRIBUTE DEFINITIONS
# ─────────────────────────────────────────────────────────────────────────────

CATEGORIES = [
    {
        "name": "HP Laptops",
        "slug": "hp-laptops",
        "icon": "💻",
        "description": "HP business and consumer laptops — ProBook, EliteBook, ZBook, Pavilion, OMEN and Spectre series",
        "sort_order": 2,
        "attributes": [
            ("series",          "Series",           "text",   "",      True,  False, 1),
            ("processor",       "Processor",        "text",   "",      True,  True,  2),
            ("ram",             "RAM",              "text",   "",      True,  True,  3),
            ("storage",         "Storage",          "text",   "",      True,  True,  4),
            ("display_size",    "Display Size",     "number", "inches",True,  False, 5),
            ("display_type",    "Display Type",     "text",   "",      False, False, 6),
            ("graphics",        "Graphics",         "text",   "",      False, False, 7),
            ("operating_system","Operating System", "text",   "",      True,  False, 8),
            ("battery_life",    "Battery Life",     "text",   "hrs",   False, False, 9),
            ("weight_kg",       "Weight",           "text",   "kg",    False, False, 10),
            ("price_tier",      "Price Tier",       "text",   "",      True,  False, 11),
            ("nigeria_market_price_naira", "Price (₦)", "number", "₦", False, False, 12),
        ],
    },
    {
        "name": "HP Desktops",
        "slug": "hp-desktops",
        "icon": "🖥️",
        "description": "HP business and workstation desktops — ProDesk, EliteDesk and Z-series Workstations",
        "sort_order": 3,
        "attributes": [
            ("series",          "Series",           "text",   "",      True,  False, 1),
            ("processor",       "Processor",        "text",   "",      True,  True,  2),
            ("ram",             "RAM",              "text",   "",      True,  True,  3),
            ("storage",         "Storage",          "text",   "",      True,  True,  4),
            ("form_factor",     "Form Factor",      "text",   "",      True,  False, 5),
            ("graphics",        "Graphics",         "text",   "",      False, False, 6),
            ("operating_system","Operating System", "text",   "",      True,  False, 7),
            ("optical_drive",   "Optical Drive",    "text",   "",      False, False, 8),
            ("price_tier",      "Price Tier",       "text",   "",      True,  False, 9),
            ("nigeria_market_price_naira", "Price (₦)", "number", "₦", False, False, 10),
        ],
    },
    {
        "name": "HP Servers",
        "slug": "hp-servers",
        "icon": "🖧",
        "description": "HP ProLiant rack, tower and blade servers for business data centres and SME infrastructure",
        "sort_order": 4,
        "attributes": [
            ("series",          "Series",           "text",   "",      True,  False, 1),
            ("processor",       "Processor",        "text",   "",      True,  True,  2),
            ("ram",             "RAM",              "text",   "",      True,  True,  3),
            ("storage",         "Storage",          "text",   "",      True,  True,  4),
            ("form_factor",     "Form Factor",      "text",   "",      True,  False, 5),
            ("drive_bays",      "Drive Bays",       "text",   "",      False, False, 6),
            ("max_ram",         "Max RAM",          "text",   "",      False, False, 7),
            ("network_ports",   "Network Ports",    "text",   "",      False, False, 8),
            ("price_tier",      "Price Tier",       "text",   "",      True,  False, 9),
            ("nigeria_market_price_naira", "Price (₦)", "number", "₦", False, False, 10),
        ],
    },
    {
        "name": "HP Thin Clients",
        "slug": "hp-thin-clients",
        "icon": "🖱️",
        "description": "HP t-series and mt-series thin clients for virtual desktop infrastructure and cloud computing environments",
        "sort_order": 5,
        "attributes": [
            ("series",          "Series",           "text",   "",      True,  False, 1),
            ("processor",       "Processor",        "text",   "",      True,  False, 2),
            ("ram",             "RAM",              "text",   "",      True,  True,  3),
            ("storage",         "Storage",          "text",   "",      True,  True,  4),
            ("operating_system","Operating System", "text",   "",      True,  False, 5),
            ("display_support", "Display Support",  "text",   "",      False, False, 6),
            ("form_factor",     "Form Factor",      "text",   "",      True,  False, 7),
            ("price_tier",      "Price Tier",       "text",   "",      True,  False, 8),
            ("nigeria_market_price_naira", "Price (₦)", "number", "₦", False, False, 9),
        ],
    },
    {
        "name": "HP Monitors",
        "slug": "hp-monitors",
        "icon": "🖥",
        "description": "HP business, professional and gaming monitors — E-series, P-series, Z-series and OMEN displays",
        "sort_order": 6,
        "attributes": [
            ("series",          "Series",           "text",   "",      True,  False, 1),
            ("screen_size",     "Screen Size",      "number", "inches",True,  True,  2),
            ("resolution",      "Resolution",       "text",   "",      True,  False, 3),
            ("panel_type",      "Panel Type",       "text",   "",      True,  False, 4),
            ("refresh_rate",    "Refresh Rate",     "text",   "",      True,  False, 5),
            ("response_time",   "Response Time",    "text",   "ms",    False, False, 6),
            ("connectivity",    "Connectivity",     "text",   "",      False, False, 7),
            ("price_tier",      "Price Tier",       "text",   "",      True,  False, 8),
            ("nigeria_market_price_naira", "Price (₦)", "number", "₦", False, False, 9),
        ],
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# DESCRIPTION HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def laptop_desc(series, model, cpu, ram, storage, display, os, use_case, tier):
    descs = [
        f"The HP {model} is a {tier.lower()} laptop built for {use_case}. "
        f"Equipped with an {cpu}, {ram} of memory and {storage} storage, it handles "
        f"demanding workloads with ease. The {display}-inch display delivers crisp visuals "
        f"for extended work sessions. Running {os}, it comes with enterprise-grade security "
        f"features including HP Sure Start and HP Sure View, making it a dependable choice "
        f"for professionals who need performance and reliability.",

        f"Designed for modern {use_case}, the HP {model} combines powerful {cpu} processing "
        f"with {ram} RAM and {storage} SSD storage. The {display}-inch screen provides "
        f"comfortable viewing while the durable chassis withstands everyday business demands. "
        f"With {os} preinstalled, it integrates seamlessly into corporate IT environments "
        f"and supports HP Manageability tools for easy fleet management.",

        f"The HP {series} {model} delivers professional-grade performance for {use_case}. "
        f"Its {cpu} processor paired with {ram} RAM ensures smooth multitasking across "
        f"business applications. {storage} of fast SSD storage keeps your data accessible "
        f"instantly. The {display}-inch display and {os} make this an ideal productivity "
        f"machine for offices, remote workers and field teams alike.",

        f"Built to meet the demands of {use_case}, the HP {model} features an {cpu}, "
        f"{ram} RAM and {storage} storage in a professional-grade form factor. "
        f"The {display}-inch display supports comfortable all-day computing, and {os} "
        f"ensures compatibility with enterprise software suites. HP's comprehensive "
        f"warranty and business support options make this a low-risk investment for "
        f"any IT team.",
    ]
    idx = (hash(model + cpu + ram + storage) % len(descs))
    return descs[idx]


def desktop_desc(series, model, cpu, ram, storage, form, os, use_case, tier):
    descs = [
        f"The HP {model} is a {form} desktop PC ideal for {use_case}. "
        f"Powered by an {cpu} with {ram} of memory and {storage} storage, it delivers "
        f"the performance needed for business applications, data processing and "
        f"multitasking. Running {os}, it supports enterprise deployment tools and "
        f"HP's remote management suite. Its compact {form} design saves desk space "
        f"without compromising on capability.",

        f"Built for {use_case} environments, the HP {model} {form} desktop combines "
        f"an {cpu} with {ram} RAM and {storage} to handle office productivity, "
        f"ERP systems and communication tools effortlessly. The {os} platform ensures "
        f"broad software compatibility and enterprise security compliance. "
        f"HP's proven commercial-grade build quality means lower maintenance costs "
        f"and longer service life.",

        f"The HP {series} {model} offers reliable {use_case} performance in a {form} "
        f"package. Its {cpu} processor, {ram} memory and {storage} storage deliver "
        f"consistent speed for everyday business tasks. {os} provides a stable, "
        f"secure foundation while HP's manageability features simplify IT administration "
        f"across large deployments. An excellent value-for-money choice for growing businesses.",

        f"Designed for business efficiency, the HP {model} {form} desktop is equipped "
        f"with an {cpu}, {ram} RAM and {storage} to power through {use_case} demands. "
        f"The {os} environment supports a wide range of enterprise applications, and "
        f"HP's lifecycle services ensure ongoing support. The space-saving {form} "
        f"chassis fits neatly on any desk or can be mounted for flexible deployment.",
    ]
    idx = hash(model + cpu + ram + storage) % len(descs)
    return descs[idx]


def server_desc(series, model, cpu, ram, storage, form, bays, max_r, ports):
    descs = [
        f"The HP {model} is a {form} server built for reliable business infrastructure. "
        f"Powered by {cpu} with {ram} of ECC memory and {storage} storage, it handles "
        f"virtualisation, databases and business-critical workloads. Supporting up to "
        f"{max_r} maximum RAM across {bays} drive bays, it scales with your business needs. "
        f"{ports} network connectivity ensures high-throughput data access for demanding "
        f"enterprise environments.",

        f"The HP ProLiant {model} delivers enterprise-grade reliability in a {form} "
        f"form factor. Featuring {cpu}, {ram} RAM and {storage}, it is optimised for "
        f"web hosting, file services, ERP and virtualisation workloads. With {bays} "
        f"drive bays expandable to {max_r} RAM, it grows alongside your infrastructure. "
        f"{ports} provide fast, redundant network connectivity for high-availability deployments.",

        f"Built to power SME and enterprise data centres, the HP {model} {form} server "
        f"offers {cpu} processing, {ram} ECC memory and {storage} storage. Designed "
        f"for 24/7 operation, it includes iLO (Integrated Lights-Out) remote management, "
        f"redundant power supply options and {bays} drive bays. Maximum RAM expandability "
        f"to {max_r} ensures long-term investment value. {ports} network ports support "
        f"both standard and high-speed connectivity requirements.",

        f"The HP ProLiant {model} is engineered for demanding {form} deployments in "
        f"modern data centres. Starting with {ram} RAM and {storage}, it supports "
        f"virtualisation, analytics and cloud workloads powered by {cpu}. "
        f"Scale storage across {bays} bays and memory up to {max_r}. "
        f"With {ports} and HP's comprehensive support ecosystem, this server is a "
        f"trusted backbone for business operations.",
    ]
    idx = hash(model + cpu + ram + storage) % len(descs)
    return descs[idx]


def thin_client_desc(series, model, cpu, ram, storage, os, form, use_case):
    descs = [
        f"The HP {model} is a {form} thin client designed for {use_case} environments. "
        f"Powered by {cpu} with {ram} RAM and {storage} storage, it connects to virtual "
        f"desktop infrastructure (VDI) with minimal footprint and energy consumption. "
        f"Running {os}, it is optimised for Citrix, VMware Horizon and Microsoft RDS "
        f"deployments. Centralised management through HP Device Manager reduces IT "
        f"overhead and simplifies large-scale rollouts.",

        f"Built for modern {use_case} computing, the HP {model} thin client offers "
        f"{cpu} performance, {ram} of memory and {storage} flash storage in a compact "
        f"{form} design. {os} provides a secure, locked-down environment ideal for "
        f"healthcare, banking, education and call-centre deployments. Low power "
        f"consumption and fanless operation make it suitable for noise-sensitive "
        f"or space-constrained settings.",

        f"The HP {series} {model} delivers reliable virtual desktop access for "
        f"{use_case} users. With {cpu}, {ram} RAM and {storage}, it provides smooth "
        f"performance for cloud-hosted applications and VDI sessions. Its {form} "
        f"design and {os} platform support easy mass deployment, remote management "
        f"and HP Endpoint Security Controller for hardware-level protection.",

        f"Ideal for shared workspaces and {use_case} deployments, the HP {model} "
        f"combines {cpu} processing with {ram} RAM and {storage} in a {form} chassis. "
        f"{os} enables secure, centralised computing with minimal local storage risk. "
        f"HP's Device-as-a-Service (DaaS) options make acquisition, management and "
        f"refresh cycles straightforward for IT administrators.",
    ]
    idx = hash(model + cpu + ram + storage) % len(descs)
    return descs[idx]


def monitor_desc(series, model, size, res, panel, refresh, resp, conn, use_case, tier):
    descs = [
        f"The HP {model} is a {size}-inch {panel} monitor delivering {res} resolution "
        f"for {use_case} professionals. With a {refresh} refresh rate and {resp}ms "
        f"response time, it provides smooth, accurate visuals for productivity and "
        f"creative work. {conn} connectivity ensures broad compatibility with modern "
        f"laptops, desktops and docking stations. Low blue light and flicker-free "
        f"technology reduces eye strain during extended work sessions.",

        f"Designed for {use_case} environments, the HP {model} offers a {size}-inch "
        f"{res} {panel} panel with {refresh} refresh rate and {resp}ms response time. "
        f"Its slim bezel design is ideal for multi-monitor setups, while {conn} "
        f"ports provide flexible connectivity. The {tier} build quality and HP's "
        f"three-year warranty make this a reliable long-term investment for business "
        f"workstations.",

        f"The HP {series} {model} brings {res} clarity to a {size}-inch {panel} display, "
        f"making it suitable for {use_case}. A {refresh} refresh rate ensures smooth "
        f"rendering, while {resp}ms response time keeps motion crisp. {conn} "
        f"connectivity and VESA mount compatibility support flexible desk configurations. "
        f"HP's certified display calibration guarantees consistent colour accuracy "
        f"straight out of the box.",

        f"Built to enhance {use_case} productivity, the HP {model} features a {size}-inch "
        f"{panel} screen with {res} resolution, {refresh} refresh rate and {resp}ms "
        f"response time. Its {conn} connection options support the latest docking "
        f"solutions, and the anti-glare coating ensures comfortable viewing under "
        f"office lighting. The {tier} design balances performance and value for "
        f"businesses equipping large numbers of workstations.",
    ]
    idx = hash(model + res + refresh + panel) % len(descs)
    return descs[idx]


# ─────────────────────────────────────────────────────────────────────────────
# PRODUCT GENERATORS
# ─────────────────────────────────────────────────────────────────────────────

def gen_laptops():
    products = []

    # ── ProBook ──────────────────────────────────────────────────────────────
    for model, display in [
        ("ProBook 440 G9",  "14"),
        ("ProBook 440 G10", "14"),
        ("ProBook 450 G9",  "15.6"),
        ("ProBook 450 G10", "15.6"),
        ("ProBook 470 G9",  "17.3"),
        ("ProBook 470 G10", "17.3"),
    ]:
        for cpu in ["Intel Core i3-1315U", "Intel Core i5-1335U", "Intel Core i7-1355U"]:
            for ram in ["8GB DDR4", "16GB DDR4", "32GB DDR4"]:
                for storage in ["256GB SSD", "512GB SSD", "1TB SSD"]:
                    price = {"8GB DDR4":320000,"16GB DDR4":420000,"32GB DDR4":580000}[ram]
                    price += {"256GB SSD":0,"512GB SSD":30000,"1TB SSD":70000}[storage]
                    price += {"Intel Core i3-1315U":0,"Intel Core i5-1335U":50000,"Intel Core i7-1355U":120000}[cpu]
                    variant = f"{ram} {storage}"
                    sku = slugify(f"hp_{model}_{cpu.split()[-1]}_{ram.split()[0]}_{storage.split()[0]}")
                    desc = laptop_desc("ProBook", model, cpu, ram, storage, display,
                                       "Windows 11 Pro", "business productivity", "Business")
                    products.append((model, variant, sku, desc, {
                        "series": "ProBook", "processor": cpu, "ram": ram,
                        "storage": storage, "display_size": display,
                        "display_type": "IPS", "graphics": "Intel Iris Xe Graphics",
                        "operating_system": "Windows 11 Pro",
                        "battery_life": "10", "weight_kg": "1.74",
                        "price_tier": "Business",
                        "nigeria_market_price_naira": str(price),
                    }))

    # ── EliteBook ─────────────────────────────────────────────────────────────
    for model, display in [
        ("EliteBook 840 G8",  "14"),
        ("EliteBook 840 G9",  "14"),
        ("EliteBook 840 G10", "14"),
        ("EliteBook 850 G8",  "15.6"),
        ("EliteBook 850 G9",  "15.6"),
        ("EliteBook 860 G9",  "16"),
        ("EliteBook 860 G10", "16"),
    ]:
        for cpu in ["Intel Core i5-1245U", "Intel Core i5-1345U", "Intel Core i7-1265U", "Intel Core i7-1365U"]:
            for ram in ["8GB DDR5", "16GB DDR5", "32GB DDR5"]:
                for storage in ["512GB SSD", "1TB SSD"]:
                    price = {"8GB DDR5":480000,"16GB DDR5":620000,"32GB DDR5":850000}[ram]
                    price += {"512GB SSD":0,"1TB SSD":60000}[storage]
                    price += {"Intel Core i5-1245U":0,"Intel Core i5-1345U":40000,
                               "Intel Core i7-1265U":100000,"Intel Core i7-1365U":150000}[cpu]
                    variant = f"{ram} {storage}"
                    sku = slugify(f"hp_{model}_{cpu.split()[-1]}_{ram.split()[0]}_{storage.split()[0]}")
                    desc = laptop_desc("EliteBook", model, cpu, ram, storage, display,
                                       "Windows 11 Pro", "enterprise professionals", "Premium")
                    products.append((model, variant, sku, desc, {
                        "series": "EliteBook", "processor": cpu, "ram": ram,
                        "storage": storage, "display_size": display,
                        "display_type": "IPS", "graphics": "Intel Iris Xe Graphics",
                        "operating_system": "Windows 11 Pro",
                        "battery_life": "12", "weight_kg": "1.46",
                        "price_tier": "Premium",
                        "nigeria_market_price_naira": str(price),
                    }))

    # ── ZBook ─────────────────────────────────────────────────────────────────
    for model, display, weight in [
        ("ZBook Firefly 14 G9",  "14",   "1.36"),
        ("ZBook Firefly 14 G10", "14",   "1.36"),
        ("ZBook Firefly 16 G10", "16",   "1.97"),
        ("ZBook Power 15 G9",    "15.6", "1.83"),
        ("ZBook Power 15 G10",   "15.6", "1.83"),
        ("ZBook Fury 16 G9",     "16",   "2.59"),
        ("ZBook Fury 16 G10",    "16",   "2.59"),
    ]:
        for cpu in ["Intel Core i7-1260P", "Intel Core i7-1360P", "Intel Core i9-12900H", "Intel Xeon W-11855M"]:
            for ram in ["16GB DDR5", "32GB DDR5", "64GB DDR5"]:
                for storage in ["512GB SSD", "1TB SSD", "2TB SSD"]:
                    price = {"16GB DDR5":950000,"32GB DDR5":1300000,"64GB DDR5":1900000}[ram]
                    price += {"512GB SSD":0,"1TB SSD":80000,"2TB SSD":180000}[storage]
                    price += {"Intel Core i7-1260P":0,"Intel Core i7-1360P":100000,
                               "Intel Core i9-12900H":280000,"Intel Xeon W-11855M":450000}[cpu]
                    variant = f"{ram} {storage}"
                    sku = slugify(f"hp_{model}_{cpu.split()[-1]}_{ram.split()[0]}_{storage.split()[0]}")
                    desc = laptop_desc("ZBook", model, cpu, ram, storage, display,
                                       "Windows 11 Pro", "creative professionals and engineers", "Workstation")
                    products.append((model, variant, sku, desc, {
                        "series": "ZBook", "processor": cpu, "ram": ram,
                        "storage": storage, "display_size": display,
                        "display_type": "IPS DreamColor", "graphics": "NVIDIA RTX A-series",
                        "operating_system": "Windows 11 Pro",
                        "battery_life": "8", "weight_kg": weight,
                        "price_tier": "Workstation",
                        "nigeria_market_price_naira": str(price),
                    }))

    # ── Pavilion ─────────────────────────────────────────────────────────────
    for model, display in [
        ("Pavilion 14-dv2",  "14"),
        ("Pavilion 15-eg3",  "15.6"),
        ("Pavilion 17-cp3",  "17.3"),
        ("Pavilion x360 14", "14"),
        ("Pavilion x360 15", "15.6"),
    ]:
        for cpu in ["Intel Core i3-1215U", "Intel Core i5-1235U", "AMD Ryzen 5 7530U"]:
            for ram in ["8GB DDR4", "16GB DDR4"]:
                for storage in ["256GB SSD", "512GB SSD"]:
                    price = {"8GB DDR4":185000,"16GB DDR4":265000}[ram]
                    price += {"256GB SSD":0,"512GB SSD":25000}[storage]
                    price += {"Intel Core i3-1215U":0,"Intel Core i5-1235U":40000,"AMD Ryzen 5 7530U":35000}[cpu]
                    variant = f"{ram} {storage}"
                    sku = slugify(f"hp_{model}_{cpu.split()[-1]}_{ram.split()[0]}_{storage.split()[0]}")
                    os_choice = "Windows 11 Home"
                    desc = laptop_desc("Pavilion", model, cpu, ram, storage, display,
                                       os_choice, "students and home users", "Budget")
                    products.append((model, variant, sku, desc, {
                        "series": "Pavilion", "processor": cpu, "ram": ram,
                        "storage": storage, "display_size": display,
                        "display_type": "IPS", "graphics": "Intel Iris Xe / AMD Radeon",
                        "operating_system": os_choice,
                        "battery_life": "8", "weight_kg": "1.69",
                        "price_tier": "Budget",
                        "nigeria_market_price_naira": str(price),
                    }))

    # ── OMEN ─────────────────────────────────────────────────────────────────
    for model, display in [
        ("OMEN 16-b1",  "16.1"),
        ("OMEN 16-wf",  "16.1"),
        ("OMEN 17-cm",  "17.3"),
        ("OMEN Transcend 14", "14"),
        ("OMEN Transcend 16", "16.1"),
    ]:
        for cpu in ["Intel Core i7-12700H", "Intel Core i7-13700H", "Intel Core i9-13900HX", "AMD Ryzen 7 7745HX"]:
            for ram in ["16GB DDR5", "32GB DDR5"]:
                for storage in ["512GB SSD", "1TB SSD"]:
                    price = {"16GB DDR5":650000,"32GB DDR5":900000}[ram]
                    price += {"512GB SSD":0,"1TB SSD":70000}[storage]
                    price += {"Intel Core i7-12700H":0,"Intel Core i7-13700H":80000,
                               "Intel Core i9-13900HX":250000,"AMD Ryzen 7 7745HX":100000}[cpu]
                    variant = f"{ram} {storage}"
                    sku = slugify(f"hp_{model}_{cpu.split()[-1]}_{ram.split()[0]}_{storage.split()[0]}")
                    desc = laptop_desc("OMEN", model, cpu, ram, storage, display,
                                       "Windows 11 Home", "gaming and content creation", "Premium")
                    products.append((model, variant, sku, desc, {
                        "series": "OMEN", "processor": cpu, "ram": ram,
                        "storage": storage, "display_size": display,
                        "display_type": "IPS 144Hz", "graphics": "NVIDIA GeForce RTX 4060/4070",
                        "operating_system": "Windows 11 Home",
                        "battery_life": "6", "weight_kg": "2.20",
                        "price_tier": "Premium",
                        "nigeria_market_price_naira": str(price),
                    }))

    # ── Spectre ───────────────────────────────────────────────────────────────
    for model, display in [
        ("Spectre x360 13-aw",  "13.3"),
        ("Spectre x360 14-ef",  "13.5"),
        ("Spectre x360 16-aa",  "16"),
        ("Spectre x360 14-eu",  "14"),
    ]:
        for cpu in ["Intel Core i5-1335U", "Intel Core i7-1355U", "Intel Core Ultra 7 155H"]:
            for ram in ["16GB LPDDR5", "32GB LPDDR5"]:
                for storage in ["512GB SSD", "1TB SSD", "2TB SSD"]:
                    price = {"16GB LPDDR5":800000,"32GB LPDDR5":1150000}[ram]
                    price += {"512GB SSD":0,"1TB SSD":80000,"2TB SSD":190000}[storage]
                    price += {"Intel Core i5-1335U":0,"Intel Core i7-1355U":120000,"Intel Core Ultra 7 155H":300000}[cpu]
                    variant = f"{ram} {storage}"
                    sku = slugify(f"hp_{model}_{cpu.split()[-1]}_{ram.split()[0]}_{storage.split()[0]}")
                    desc = laptop_desc("Spectre", model, cpu, ram, storage, display,
                                       "Windows 11 Home", "executives and creatives", "Premium")
                    products.append((model, variant, sku, desc, {
                        "series": "Spectre", "processor": cpu, "ram": ram,
                        "storage": storage, "display_size": display,
                        "display_type": "OLED / IPS", "graphics": "Intel Iris Xe Graphics",
                        "operating_system": "Windows 11 Home",
                        "battery_life": "17", "weight_kg": "1.36",
                        "price_tier": "Premium",
                        "nigeria_market_price_naira": str(price),
                    }))

    # ── ENVY ──────────────────────────────────────────────────────────────────
    for model, display in [
        ("ENVY x360 13-bf",  "13.3"),
        ("ENVY x360 14-es",  "14"),
        ("ENVY x360 15-ey",  "15.6"),
        ("ENVY x360 16-h",   "16"),
        ("ENVY 16-h",        "16"),
        ("ENVY x360 14-fc",  "14"),
        ("ENVY x360 15-fe",  "15.6"),
    ]:
        for cpu in ["Intel Core i5-1335U", "Intel Core i7-1355U", "AMD Ryzen 5 7530U", "AMD Ryzen 7 7730U"]:
            for ram in ["8GB LPDDR5", "16GB LPDDR5", "32GB LPDDR5"]:
                for storage in ["512GB SSD", "1TB SSD"]:
                    price = {"8GB LPDDR5":340000,"16GB LPDDR5":470000,"32GB LPDDR5":680000}[ram]
                    price += {"512GB SSD":0,"1TB SSD":75000}[storage]
                    price += {"Intel Core i5-1335U":0,"Intel Core i7-1355U":130000,
                               "AMD Ryzen 5 7530U":20000,"AMD Ryzen 7 7730U":150000}[cpu]
                    variant = f"{ram} {storage}"
                    sku = slugify(f"hp_{model}_{cpu.split()[-1]}_{ram.split()[0]}_{storage.split()[0]}")
                    desc = laptop_desc("ENVY", model, cpu, ram, storage, display,
                                       "Windows 11 Home", "creatives and students", "Premium")
                    products.append((model, variant, sku, desc, {
                        "series": "ENVY", "processor": cpu, "ram": ram,
                        "storage": storage, "display_size": display,
                        "display_type": "OLED / IPS", "graphics": "Intel Iris Xe / AMD Radeon",
                        "operating_system": "Windows 11 Home",
                        "battery_life": "14", "weight_kg": "1.59",
                        "price_tier": "Premium",
                        "nigeria_market_price_naira": str(price),
                    }))

    return products


def gen_desktops():
    products = []

    # ── ProDesk ──────────────────────────────────────────────────────────────
    for model, form in [
        ("ProDesk 400 G9 SFF",   "Small Form Factor"),
        ("ProDesk 400 G9 MT",    "Mini Tower"),
        ("ProDesk 600 G6 SFF",   "Small Form Factor"),
        ("ProDesk 600 G9 SFF",   "Small Form Factor"),
        ("ProDesk 600 G9 MT",    "Mini Tower"),
        ("ProDesk 800 G9 SFF",   "Small Form Factor"),
        ("ProDesk 800 G9 Tower", "Tower"),
    ]:
        for cpu in ["Intel Core i3-12100", "Intel Core i5-12500", "Intel Core i7-12700"]:
            for ram in ["4GB DDR4", "8GB DDR4", "16GB DDR4", "32GB DDR4"]:
                for storage in ["256GB SSD", "512GB SSD", "1TB HDD + 256GB SSD"]:
                    price = {"4GB DDR4":155000,"8GB DDR4":200000,"16GB DDR4":280000,"32GB DDR4":420000}[ram]
                    price += {"256GB SSD":0,"512GB SSD":25000,"1TB HDD + 256GB SSD":40000}[storage]
                    price += {"Intel Core i3-12100":0,"Intel Core i5-12500":60000,"Intel Core i7-12700":150000}[cpu]
                    variant = f"{ram} {storage} {form}"
                    sku = slugify(f"hp_{model}_{cpu.split()[-1]}_{ram.split()[0]}_{storage.split()[0]}")
                    desc = desktop_desc("ProDesk", model, cpu, ram, storage, form,
                                        "Windows 11 Pro", "everyday business tasks", "Business")
                    products.append((model, variant, sku, desc, {
                        "series": "ProDesk", "processor": cpu, "ram": ram,
                        "storage": storage, "form_factor": form,
                        "graphics": "Intel UHD 770",
                        "operating_system": "Windows 11 Pro",
                        "optical_drive": "None",
                        "price_tier": "Business",
                        "nigeria_market_price_naira": str(price),
                    }))

    # ── EliteDesk ─────────────────────────────────────────────────────────────
    for model, form in [
        ("EliteDesk 800 G8 SFF",   "Small Form Factor"),
        ("EliteDesk 800 G8 Tower", "Tower"),
        ("EliteDesk 800 G9 SFF",   "Small Form Factor"),
        ("EliteDesk 880 G8 MT",    "Mini Tower"),
        ("EliteDesk 880 G9 MT",    "Mini Tower"),
    ]:
        for cpu in ["Intel Core i5-11500", "Intel Core i7-11700", "Intel Core i9-11900"]:
            for ram in ["8GB DDR4", "16GB DDR4", "32GB DDR4", "64GB DDR4"]:
                for storage in ["512GB SSD", "1TB SSD", "2TB HDD + 512GB SSD"]:
                    price = {"8GB DDR4":280000,"16GB DDR4":380000,"32GB DDR4":550000,"64GB DDR4":850000}[ram]
                    price += {"512GB SSD":0,"1TB SSD":60000,"2TB HDD + 512GB SSD":90000}[storage]
                    price += {"Intel Core i5-11500":0,"Intel Core i7-11700":120000,"Intel Core i9-11900":280000}[cpu]
                    variant = f"{ram} {storage} {form}"
                    sku = slugify(f"hp_{model}_{cpu.split()[-1]}_{ram.split()[0]}_{storage.split()[0]}")
                    desc = desktop_desc("EliteDesk", model, cpu, ram, storage, form,
                                        "Windows 11 Pro", "enterprise office deployments", "Premium")
                    products.append((model, variant, sku, desc, {
                        "series": "EliteDesk", "processor": cpu, "ram": ram,
                        "storage": storage, "form_factor": form,
                        "graphics": "Intel UHD 750",
                        "operating_system": "Windows 11 Pro",
                        "optical_drive": "Optional DVD-RW",
                        "price_tier": "Premium",
                        "nigeria_market_price_naira": str(price),
                    }))

    # ── Z Workstations ────────────────────────────────────────────────────────
    for model, form, base_price in [
        ("Z2 Mini G9 Workstation",    "Mini",  700000),
        ("Z2 Tower G9 Workstation",   "Tower", 850000),
        ("Z4 Tower G5 Workstation",   "Tower", 1400000),
        ("Z6 Tower G5 Workstation",   "Tower", 2200000),
        ("Z8 Fury Tower G5 Workstation", "Tower", 3500000),
    ]:
        for cpu in ["Intel Core i7-13700K", "Intel Xeon W3-2423", "Intel Xeon W5-2445"]:
            for ram in ["16GB DDR5 ECC", "32GB DDR5 ECC", "64GB DDR5 ECC"]:
                for storage in ["512GB SSD", "1TB SSD", "2TB SSD"]:
                    price = base_price
                    price += {"16GB DDR5 ECC":0,"32GB DDR5 ECC":180000,"64GB DDR5 ECC":420000}[ram]
                    price += {"512GB SSD":0,"1TB SSD":90000,"2TB SSD":210000}[storage]
                    price += {"Intel Core i7-13700K":0,"Intel Xeon W3-2423":350000,"Intel Xeon W5-2445":700000}[cpu]
                    variant = f"{ram} {storage}"
                    sku = slugify(f"hp_{model}_{cpu.split()[-1]}_{ram.split()[0]}_{storage.split()[0]}")
                    desc = desktop_desc("Z Workstation", model, cpu, ram, storage, form,
                                        "Windows 11 Pro for Workstations", "engineering and creative workloads", "Workstation")
                    products.append((model, variant, sku, desc, {
                        "series": "Z Workstation", "processor": cpu, "ram": ram,
                        "storage": storage, "form_factor": form,
                        "graphics": "NVIDIA RTX A-series / AMD Radeon Pro",
                        "operating_system": "Windows 11 Pro for Workstations",
                        "optical_drive": "Optional",
                        "price_tier": "Workstation",
                        "nigeria_market_price_naira": str(price),
                    }))

    # ── Pavilion Desktop & HP AiO ─────────────────────────────────────────────
    for model, form in [
        ("Pavilion Desktop TP01-3000", "Tower"),
        ("Pavilion Desktop TP01-4000", "Tower"),
        ("Pavilion Desktop 590-p0",    "Tower"),
        ("HP Desktop Pro A 300 G3",    "Mini Tower"),
        ("HP All-in-One 24-dn",        "All-in-One"),
        ("HP All-in-One 27-cr",        "All-in-One"),
    ]:
        for cpu in ["Intel Core i3-12100", "Intel Core i5-12400", "AMD Ryzen 5 5600G"]:
            for ram in ["8GB DDR4", "16GB DDR4", "32GB DDR4"]:
                for storage in ["256GB SSD", "512GB SSD", "1TB HDD + 256GB SSD"]:
                    price = {"8GB DDR4":140000,"16GB DDR4":200000,"32GB DDR4":310000}[ram]
                    price += {"256GB SSD":0,"512GB SSD":22000,"1TB HDD + 256GB SSD":38000}[storage]
                    price += {"Intel Core i3-12100":0,"Intel Core i5-12400":55000,"AMD Ryzen 5 5600G":48000}[cpu]
                    if "All-in-One" in form:
                        price += 80000
                    variant = f"{ram} {storage} {form}"
                    sku = slugify(f"hp_{model}_{cpu.split()[-1]}_{ram.split()[0]}_{storage.split()[0]}")
                    tier = "Budget"
                    os_choice = "Windows 11 Home"
                    desc = desktop_desc("Pavilion", model, cpu, ram, storage, form,
                                        os_choice, "home and small office use", tier)
                    products.append((model, variant, sku, desc, {
                        "series": "Pavilion", "processor": cpu, "ram": ram,
                        "storage": storage, "form_factor": form,
                        "graphics": "Intel UHD / AMD Radeon",
                        "operating_system": os_choice,
                        "optical_drive": "None",
                        "price_tier": tier,
                        "nigeria_market_price_naira": str(price),
                    }))

    return products


def gen_servers():
    products = []

    configs = [
        # (model, form_factor, drive_bays, max_ram, net_ports, base_price)
        ("ProLiant DL20 Gen10 Plus",  "1U Rack", "4 LFF",  "128GB",  "2x 1GbE",  1200000),
        ("ProLiant DL20 Gen10",       "1U Rack", "4 LFF",  "64GB",   "2x 1GbE",  1000000),
        ("ProLiant DL360 Gen10",      "1U Rack", "8 SFF",  "3TB",    "4x 1GbE",  2800000),
        ("ProLiant DL360 Gen10 Plus", "1U Rack", "8 SFF",  "4TB",    "4x 1GbE",  3200000),
        ("ProLiant DL380 Gen10",      "2U Rack", "12 LFF", "3TB",    "4x 1GbE",  3800000),
        ("ProLiant DL380 Gen10 Plus", "2U Rack", "24 SFF", "4TB",    "4x 10GbE", 4500000),
        ("ProLiant DL580 Gen10",      "4U Rack", "12 LFF", "12TB",   "4x 10GbE", 9000000),
        ("ProLiant ML30 Gen10 Plus",  "Tower",   "4 LFF",  "128GB",  "2x 1GbE",  1100000),
        ("ProLiant ML110 Gen10",      "Tower",   "4 LFF",  "192GB",  "2x 1GbE",  1600000),
        ("ProLiant ML350 Gen10",      "Tower",   "12 LFF", "3TB",    "4x 1GbE",  3500000),
        ("ProLiant ML350 Gen10 Plus", "Tower",   "12 LFF", "4TB",    "4x 1GbE",  4200000),
    ]

    cpus_by_tier = {
        "Entry":          ["Intel Xeon Bronze 3204", "Intel Xeon Bronze 3206R", "Intel Xeon Silver 4208"],
        "Mid-range":      ["Intel Xeon Silver 4210R", "Intel Xeon Silver 4214R", "Intel Xeon Gold 5218R"],
        "Enterprise":     ["Intel Xeon Gold 6226R", "Intel Xeon Gold 6248R", "Intel Xeon Platinum 8260"],
        "Mission-Critical":["Intel Xeon Platinum 8270", "Intel Xeon Platinum 8352Y"],
    }

    ram_by_tier = {
        "Entry":           ["16GB DDR4 ECC", "32GB DDR4 ECC"],
        "Mid-range":       ["32GB DDR4 ECC", "64GB DDR4 ECC"],
        "Enterprise":      ["64GB DDR4 ECC", "128GB DDR4 ECC", "256GB DDR4 ECC"],
        "Mission-Critical":["256GB DDR4 ECC", "512GB DDR4 ECC"],
    }

    storage_opts = ["1x 1TB 7.2K SATA HDD", "2x 1TB 7.2K SATA HDD", "2x 960GB SATA SSD",
                    "4x 1TB 7.2K SATA HDD", "2x 1.92TB SATA SSD", "4x 960GB SATA SSD"]

    for (model, form, bays, max_r, ports, base) in configs:
        tier = ("Entry" if base < 1500000 else
                "Mid-range" if base < 4000000 else
                "Enterprise" if base < 7000000 else "Mission-Critical")
        for cpu in cpus_by_tier[tier]:
            for ram in ram_by_tier[tier]:
                for storage in storage_opts:
                    price = base
                    price += {"16GB DDR4 ECC":0,"32GB DDR4 ECC":120000,"64GB DDR4 ECC":280000,
                               "128GB DDR4 ECC":600000,"256GB DDR4 ECC":1400000,
                               "512GB DDR4 ECC":3000000}[ram]
                    price += {"1x 1TB 7.2K SATA HDD":0,"2x 1TB 7.2K SATA HDD":80000,
                               "2x 960GB SATA SSD":200000,"4x 1TB 7.2K SATA HDD":150000,
                               "2x 1.92TB SATA SSD":480000,"4x 960GB SATA SSD":380000}[storage]
                    price += {"Intel Xeon Bronze 3204":0,"Intel Xeon Bronze 3206R":50000,
                               "Intel Xeon Silver 4208":200000,"Intel Xeon Silver 4210R":280000,
                               "Intel Xeon Silver 4214R":380000,"Intel Xeon Gold 5218R":700000,
                               "Intel Xeon Gold 6226R":1100000,"Intel Xeon Gold 6248R":1800000,
                               "Intel Xeon Platinum 8260":3500000,"Intel Xeon Platinum 8270":4200000,
                               "Intel Xeon Platinum 8352Y":5500000}[cpu]
                    variant = f"{ram} {storage}"
                    stor_tag = "_".join(storage.split()[:2])
                    sku = slugify(f"hp_{model}_{cpu.split()[-1]}_{ram.split()[0]}_{stor_tag}")
                    desc = server_desc("ProLiant", model, cpu, ram, storage, form, bays, max_r, ports)
                    products.append((model, variant, sku, desc, {
                        "series": "ProLiant", "processor": cpu, "ram": ram,
                        "storage": storage, "form_factor": form,
                        "drive_bays": bays, "max_ram": max_r,
                        "network_ports": ports, "price_tier": tier,
                        "nigeria_market_price_naira": str(price),
                    }))

    # ── Add ProLiant DL160 / DL80 / BL460c ───────────────────────────────────
    extra_configs = [
        ("ProLiant DL160 Gen10",   "2U Rack", "8 LFF",  "1.5TB", "4x 1GbE",  2400000),
        ("ProLiant DL160 Gen10 Plus","2U Rack","8 LFF",  "2TB",   "4x 1GbE",  2700000),
        ("ProLiant DL80 Gen9",     "2U Rack", "8 LFF",  "768GB", "2x 1GbE",  1800000),
    ]
    for (model, form, bays, max_r, ports, base) in extra_configs:
        tier = ("Mid-range" if base < 4000000 else "Enterprise")
        for cpu in ["Intel Xeon Silver 4210R", "Intel Xeon Silver 4214R", "Intel Xeon Gold 5218R"]:
            for ram in ["32GB DDR4 ECC", "64GB DDR4 ECC"]:
                for storage in storage_opts:
                    price = base
                    price += {"32GB DDR4 ECC":120000,"64GB DDR4 ECC":280000}[ram]
                    price += {"1x 1TB 7.2K SATA HDD":0,"2x 1TB 7.2K SATA HDD":80000,
                               "2x 960GB SATA SSD":200000,"4x 1TB 7.2K SATA HDD":150000,
                               "2x 1.92TB SATA SSD":480000,"4x 960GB SATA SSD":380000}[storage]
                    price += {"Intel Xeon Silver 4210R":280000,"Intel Xeon Silver 4214R":380000,
                               "Intel Xeon Gold 5218R":700000}[cpu]
                    variant = f"{ram} {storage}"
                    stor_tag = "_".join(storage.split()[:2])
                    sku = slugify(f"hp_{model}_{cpu.split()[-1]}_{ram.split()[0]}_{stor_tag}")
                    desc = server_desc("ProLiant", model, cpu, ram, storage, form, bays, max_r, ports)
                    products.append((model, variant, sku, desc, {
                        "series": "ProLiant", "processor": cpu, "ram": ram,
                        "storage": storage, "form_factor": form,
                        "drive_bays": bays, "max_ram": max_r,
                        "network_ports": ports, "price_tier": tier,
                        "nigeria_market_price_naira": str(price),
                    }))

    return products


def gen_thin_clients():
    products = []

    tc_configs = [
        # (model, series, cpu, form, base_price)
        ("t240 Thin Client",       "t-series", "ARM Cortex-A53 Quad-core",     "Ultra Slim", 95000),
        ("t430 Thin Client",       "t-series", "Intel Celeron N4020",           "Ultra Slim", 110000),
        ("t530 Thin Client",       "t-series", "AMD GX-209JA Dual-core",        "Ultra Slim", 130000),
        ("t630 Thin Client",       "t-series", "AMD GX-420GI Quad-core",        "Desktop",    190000),
        ("t640 Thin Client",       "t-series", "AMD Ryzen Embedded R1505G",     "Desktop",    250000),
        ("t740 Thin Client",       "t-series", "AMD Ryzen Embedded V2748C",     "Desktop",    350000),
        ("t755 Thin Client",       "t-series", "AMD Ryzen 5 PRO 5650GE",        "Desktop",    480000),
        ("mt21 Mobile Thin Client","mt-series","AMD A4-9120C Dual-core",         "Ultra Slim", 150000),
        ("mt44 Mobile Thin Client","mt-series","AMD A6-9220C Dual-core",         "Ultra Slim", 200000),
        ("mt45 Mobile Thin Client","mt-series","AMD Ryzen 3 3250U Dual-core",    "Ultra Slim", 280000),
        ("mt46 Mobile Thin Client","mt-series","AMD Ryzen 5 4500U Hexa-core",    "Ultra Slim", 370000),
        ("t310 All-in-One Thin Client","AiO",  "Intel Celeron N4120 Quad-core", "AiO",        220000),
        ("t430 All-in-One Thin Client","AiO",  "AMD GX-420GI Quad-core",        "AiO",        300000),
    ]

    ram_opts     = ["4GB DDR4", "8GB DDR4", "16GB DDR4"]
    storage_opts = ["16GB eMMC", "32GB eMMC", "64GB eMMC", "128GB SSD", "256GB SSD"]
    os_opts      = ["HP ThinPro OS", "Windows 10 IoT Enterprise", "Windows 11 IoT Enterprise", "FreeDOS"]
    disp_opts    = ["Single 4K Display", "Dual FHD Displays", "Triple FHD Displays"]

    use_cases = {
        "t-series":  "virtual desktop and cloud computing",
        "mt-series": "mobile VDI and field workforce",
        "AiO":       "space-constrained all-in-one VDI",
    }

    for (model, series, cpu, form, base) in tc_configs:
        tier = "Entry" if base < 180000 else "Standard" if base < 320000 else "Advanced"
        r_opts = ram_opts[:2] if tier == "Entry" else ram_opts
        s_opts = storage_opts[:3] if tier == "Entry" else storage_opts[1:4]
        for ram in r_opts:
            for storage in s_opts:
                for os in os_opts:
                    price = base
                    price += {"4GB DDR4":0,"8GB DDR4":25000,"16GB DDR4":60000}[ram]
                    price += {"16GB eMMC":0,"32GB eMMC":8000,"64GB eMMC":18000,
                               "128GB SSD":35000,"256GB SSD":70000}[storage]
                    price += {"HP ThinPro OS":0,"Windows 10 IoT Enterprise":25000,
                               "Windows 11 IoT Enterprise":30000,"FreeDOS":0}[os]
                    variant = f"{ram} {storage} {os}"
                    os_tag = slugify(os)[:12]
                    sku = slugify(f"hp_{model}_{ram.split()[0]}_{storage.replace(' ','_')}_{os_tag}")
                    desc = thin_client_desc(series, model, cpu, ram, storage, os, form,
                                            use_cases.get(series, "VDI"))
                    products.append((model, variant, sku, desc, {
                        "series": series, "processor": cpu, "ram": ram,
                        "storage": storage, "operating_system": os,
                        "display_support": disp_opts[0],
                        "form_factor": form, "price_tier": tier,
                        "nigeria_market_price_naira": str(price),
                    }))

    return products


def gen_monitors():
    products = []

    # Each entry: (model, series, size, res_label, panel, refresh, resp_ms,
    #              connectivity, base_price_ngn, use_case, tier)
    # Stand variants are added automatically below: Standard and HAS (height-adjustable)
    # giving ~2x the products per model entry.

    STAND_VARIANTS = [
        ("Standard Stand",   0,      "std"),
        ("Height-Adj Stand", 15000,  "has"),
        ("VESA Mount Only",  -8000,  "vesa"),
    ]

    COLOR_VARIANTS = [
        ("Black",  0,     "blk"),
        ("Silver", 5000,  "slv"),
        ("White",  5000,  "wht"),
    ]

    # ── E-series: Business Essentials ─────────────────────────────────────────
    e_series = [
        # Gen 4
        ("E22 G4",   "E-series","21.5","FHD",   "IPS","60Hz","5","VGA+HDMI+DP",         68000, "office productivity","Entry"),
        ("E24 G4",   "E-series","23.8","FHD",   "IPS","60Hz","5","VGA+HDMI+DP",         82000, "office productivity","Entry"),
        ("E24 G4",   "E-series","23.8","FHD",   "IPS","75Hz","5","VGA+HDMI+DP",         88000, "office productivity","Entry"),
        ("E24i G4",  "E-series","23.8","WUXGA", "IPS","60Hz","5","VGA+HDMI+DP",        105000, "office productivity","Standard"),
        ("E27 G4",   "E-series","27",  "FHD",   "IPS","60Hz","5","VGA+HDMI+DP",        118000, "office productivity","Standard"),
        ("E27 G4",   "E-series","27",  "QHD",   "IPS","60Hz","5","HDMI+DP",            148000, "office productivity","Standard"),
        ("E28 G4",   "E-series","27.9","4K UHD","IPS","60Hz","5","HDMI+DP+USB-C",      210000, "office productivity","Standard"),
        ("E34 G4",   "E-series","34",  "WQHD",  "IPS","60Hz","5","HDMI+DP+USB-C",      280000, "widescreen office",  "Standard"),
        # Gen 5
        ("E22 G5",   "E-series","21.5","FHD",   "IPS","60Hz","5","VGA+HDMI+DP",         75000, "office productivity","Entry"),
        ("E24 G5",   "E-series","23.8","FHD",   "IPS","60Hz","5","VGA+HDMI+DP",         90000, "office productivity","Entry"),
        ("E24 G5",   "E-series","23.8","FHD",   "IPS","75Hz","5","VGA+HDMI+DP",         96000, "office productivity","Entry"),
        ("E24i G5",  "E-series","23.8","WUXGA", "IPS","60Hz","5","VGA+HDMI+DP",        110000, "office productivity","Standard"),
        ("E27 G5",   "E-series","27",  "FHD",   "IPS","60Hz","5","VGA+HDMI+DP",        128000, "office productivity","Standard"),
        ("E27 G5",   "E-series","27",  "QHD",   "IPS","75Hz","5","HDMI+DP+USB-C",      162000, "office productivity","Standard"),
        ("E28 G5",   "E-series","27.9","4K UHD","IPS","60Hz","5","HDMI+DP+USB-C 65W",  225000, "office productivity","Standard"),
        ("E34 G5",   "E-series","34",  "WQHD",  "IPS","60Hz","5","HDMI+DP+USB-C 65W",  295000, "widescreen office",  "Standard"),
    ]

    # ── P-series: Business Performance ────────────────────────────────────────
    p_series = [
        # Gen 4
        ("P24h G4",  "P-series","23.8","FHD",   "IPS","75Hz","5","HDMI+DP+USB-C 65W",  115000,"professional use","Standard"),
        ("P24u G4",  "P-series","23.8","4K UHD","IPS","60Hz","5","HDMI+DP+USB-C 65W",  205000,"professional use","Professional"),
        ("P27h G4",  "P-series","27",  "FHD",   "IPS","75Hz","5","HDMI+DP+USB-C 65W",  160000,"professional use","Standard"),
        ("P27h G4",  "P-series","27",  "QHD",   "IPS","75Hz","5","HDMI+DP+USB-C 65W",  195000,"professional use","Standard"),
        ("P27u G4",  "P-series","27",  "4K UHD","IPS","60Hz","5","HDMI+DP+USB-C 65W",  265000,"professional use","Professional"),
        ("P32 G4",   "P-series","31.5","4K UHD","IPS","60Hz","5","HDMI+DP+USB-C 65W",  360000,"professional use","Professional"),
        ("P34hc G4", "P-series","34",  "WQHD",  "IPS","60Hz","5","HDMI+DP+USB-C 65W",  420000,"professional use","Professional"),
        # Gen 5
        ("P24h G5",  "P-series","23.8","FHD",   "IPS","75Hz","5","HDMI+DP+USB-C 65W",  120000,"professional use","Standard"),
        ("P24h G5",  "P-series","23.8","QHD",   "IPS","75Hz","5","HDMI+DP+USB-C 65W",  155000,"professional use","Standard"),
        ("P24u G5",  "P-series","23.8","4K UHD","IPS","60Hz","5","HDMI+DP+USB-C 65W",  220000,"professional use","Professional"),
        ("P27h G5",  "P-series","27",  "FHD",   "IPS","75Hz","5","HDMI+DP+USB-C 65W",  168000,"professional use","Standard"),
        ("P27h G5",  "P-series","27",  "QHD",   "IPS","75Hz","5","HDMI+DP+USB-C 65W",  210000,"professional use","Standard"),
        ("P27u G5",  "P-series","27",  "4K UHD","IPS","60Hz","5","HDMI+DP+USB-C 96W",  280000,"professional use","Professional"),
        ("P32 G5",   "P-series","31.5","4K UHD","IPS","60Hz","5","HDMI+DP+USB-C 96W",  380000,"professional use","Professional"),
        ("P34hc G5", "P-series","34",  "WQHD",  "IPS","60Hz","5","HDMI+DP+USB-C 96W",  435000,"professional use","Professional"),
        ("P40w G5",  "P-series","39.7","WUHD",  "IPS","60Hz","5","TB3+HDMI+DP+USB-C",  680000,"professional use","Workstation"),
    ]

    # ── Z-series: Workstation ─────────────────────────────────────────────────
    z_series = [
        ("Z22n G2",  "Z-series","21.5","FHD",   "IPS","60Hz","5","HDMI+DP+VGA",        145000,"workstation design","Professional"),
        ("Z22n G3",  "Z-series","21.5","FHD",   "IPS","60Hz","5","HDMI+DP+VGA",        155000,"workstation design","Professional"),
        ("Z24f G2",  "Z-series","23.8","FHD",   "IPS","75Hz","5","HDMI+DP+VGA",        152000,"workstation design","Professional"),
        ("Z24f G3",  "Z-series","23.8","FHD",   "IPS","75Hz","5","HDMI+DP+VGA",        160000,"workstation design","Professional"),
        ("Z24n G2",  "Z-series","24",  "WUXGA", "IPS","60Hz","5","HDMI+DP+USB-C",      185000,"workstation design","Professional"),
        ("Z24n G3",  "Z-series","24",  "WUXGA", "IPS","60Hz","5","HDMI+DP+USB-C 65W",  195000,"workstation design","Professional"),
        ("Z24nf G2", "Z-series","23.8","FHD",   "IPS","60Hz","5","DP+HDMI+USB-C",      170000,"workstation design","Professional"),
        ("Z24nf G3", "Z-series","23.8","FHD",   "IPS","75Hz","5","DP+HDMI+USB-C 65W",  182000,"workstation design","Professional"),
        ("Z27 G3",   "Z-series","27",  "QHD",   "IPS","60Hz","5","HDMI+DP+USB-C",      255000,"workstation design","Professional"),
        ("Z27 G3",   "Z-series","27",  "QHD",   "IPS","75Hz","5","HDMI+DP+USB-C",      268000,"workstation design","Professional"),
        ("Z27n G2",  "Z-series","27",  "QHD",   "IPS","60Hz","5","DP+HDMI+USB-C",      245000,"workstation design","Professional"),
        ("Z27n G3",  "Z-series","27",  "QHD",   "IPS","75Hz","5","DP+HDMI+USB-C 65W",  258000,"workstation design","Professional"),
        ("Z27q G2",  "Z-series","27",  "4K UHD","IPS","60Hz","5","DP+HDMI+USB-C",      320000,"workstation design","Workstation"),
        ("Z27q G3",  "Z-series","27",  "4K UHD","IPS","60Hz","5","DP+HDMI+USB-C 65W",  335000,"workstation design","Workstation"),
        ("Z27u G2",  "Z-series","27",  "4K UHD","IPS","60Hz","5","TB3+HDMI+DP+USB-C",  380000,"workstation design","Workstation"),
        ("Z27u G3",  "Z-series","27",  "4K UHD","IPS","60Hz","5","TB3+HDMI+DP+USB-C",  340000,"workstation design","Workstation"),
        ("Z27xs G2", "Z-series","27",  "4K UHD","IPS","60Hz","1","TB3+HDMI+DP",        640000,"workstation design","Workstation"),
        ("Z27xs G3", "Z-series","27",  "4K UHD","OLED","60Hz","1","TB3+HDMI+DP",       700000,"workstation design","Workstation"),
        ("Z32 G2",   "Z-series","31.5","4K UHD","IPS","60Hz","5","HDMI+DP+USB-C 65W",  430000,"workstation design","Workstation"),
        ("Z32 G3",   "Z-series","31.5","4K UHD","IPS","60Hz","5","HDMI+DP+USB-C 96W",  450000,"workstation design","Workstation"),
        ("Z38c G2",  "Z-series","37.5","WQHD+", "IPS","60Hz","5","TB3+HDMI+DP",        620000,"workstation design","Workstation"),
        ("Z38c G3",  "Z-series","37.5","WQHD+", "IPS","75Hz","5","TB3+HDMI+DP+USB-C",  650000,"workstation design","Workstation"),
        ("Z40c G2",  "Z-series","39.7","WUHD",  "IPS","60Hz","5","TB3+HDMI+DP",        750000,"workstation design","Workstation"),
        ("Z40c G3",  "Z-series","39.7","WUHD",  "IPS","72Hz","5","TB4+HDMI+DP+USB-C",  790000,"workstation design","Workstation"),
        ("Z43 G2",   "Z-series","42.5","4K UHD","IPS","60Hz","5","TB3+HDMI+4xDP",      980000,"workstation design","Workstation"),
    ]

    # ── OMEN: Gaming ──────────────────────────────────────────────────────────
    omen_series = [
        # model, size, res, panel, refresh, resp, conn, base, tier
        ("OMEN 24c 2022",    "OMEN","23.8","FHD",   "IPS", "144Hz","1","HDMI 2.0+DP 1.4",           175000,"gaming","Standard"),
        ("OMEN 24c 2023",    "OMEN","23.8","FHD",   "IPS", "165Hz","1","HDMI 2.0+DP 1.4",           185000,"gaming","Standard"),
        ("OMEN 24c 2024",    "OMEN","23.8","FHD",   "IPS", "165Hz","1","HDMI 2.0+DP 1.4+USB-C",     195000,"gaming","Standard"),
        ("OMEN 25 2022",     "OMEN","24.5","FHD",   "TN",  "144Hz","1","HDMI 2.0+DP 1.2",           155000,"gaming","Entry"),
        ("OMEN 25i 2022",    "OMEN","24.5","FHD",   "IPS", "165Hz","1","HDMI 2.0+DP 1.4",           195000,"gaming","Standard"),
        ("OMEN 25i 2023",    "OMEN","24.5","FHD",   "IPS", "240Hz","1","HDMI 2.0+DP 1.4",           235000,"gaming","Standard"),
        ("OMEN 27 2022",     "OMEN","27",  "QHD",   "IPS", "165Hz","1","HDMI 2.0+DP 1.4",           295000,"gaming","Standard"),
        ("OMEN 27c 2023",    "OMEN","27",  "QHD",   "IPS", "165Hz","1","HDMI 2.0+DP 1.4",           295000,"gaming","Standard"),
        ("OMEN 27c 2024",    "OMEN","27",  "QHD",   "IPS", "240Hz","1","HDMI 2.1+DP 1.4+USB-C",     355000,"gaming","Premium"),
        ("OMEN 27q 2023",    "OMEN","27",  "QHD",   "IPS", "165Hz","1","HDMI 2.1+DP 1.4+USB-C",     320000,"gaming","Premium"),
        ("OMEN 27q 2024",    "OMEN","27",  "QHD",   "IPS", "240Hz","1","HDMI 2.1+DP 1.4+USB-C 65W", 380000,"gaming","Premium"),
        ("OMEN 27u 2023",    "OMEN","27",  "4K UHD","IPS", "144Hz","1","HDMI 2.1+DP 1.4",           440000,"gaming","Premium"),
        ("OMEN 27u 2024",    "OMEN","27",  "4K UHD","IPS", "160Hz","1","HDMI 2.1+DP 1.4+USB-C",     470000,"gaming","Premium"),
        ("OMEN 32 2022",     "OMEN","31.5","QHD",   "VA",  "165Hz","1","HDMI 2.0+DP 1.4",           370000,"gaming","Standard"),
        ("OMEN 32 2023",     "OMEN","31.5","QHD",   "VA",  "165Hz","1","HDMI 2.1+DP 1.4",           385000,"gaming","Standard"),
        ("OMEN 34c 2022",    "OMEN","34",  "UWQHD", "VA",  "165Hz","1","HDMI 2.0+DP 1.4",           510000,"ultrawide gaming","Premium"),
        ("OMEN 34c 2023",    "OMEN","34",  "UWQHD", "IPS", "165Hz","1","HDMI 2.1+DP 1.4",           530000,"ultrawide gaming","Premium"),
        ("OMEN 34c 2024",    "OMEN","34",  "UWQHD", "IPS", "175Hz","1","HDMI 2.1+DP 1.4+USB-C",     560000,"ultrawide gaming","Premium"),
        ("OMEN 34u 2023",    "OMEN","34",  "4K UHD","IPS", "144Hz","1","HDMI 2.1+DP 1.4+USB-C",     680000,"ultrawide gaming","Premium"),
        ("OMEN 45L 2022",    "OMEN","44.5","4K UHD","VA",  "144Hz","1","HDMI 2.1+DP 1.4",           820000,"large gaming","Premium"),
        ("OMEN 45L 2023",    "OMEN","44.5","4K UHD","VA",  "144Hz","1","HDMI 2.1+DP 1.4",           850000,"large gaming","Premium"),
    ]

    # ── HP X-series: Affordable Gaming ────────────────────────────────────────
    x_series = [
        ("HP X24 2022",   "X-series","23.8","FHD",  "IPS","144Hz","1","HDMI 2.0+DP 1.2",         145000,"entry gaming","Entry"),
        ("HP X24 2023",   "X-series","23.8","FHD",  "IPS","165Hz","1","HDMI 2.0+DP 1.2",         155000,"entry gaming","Entry"),
        ("HP X24ih 2022", "X-series","23.8","FHD",  "IPS","165Hz","1","HDMI 2.0+DP 1.4",         168000,"entry gaming","Entry"),
        ("HP X24ih 2023", "X-series","23.8","FHD",  "IPS","165Hz","1","HDMI 2.0+DP 1.4+USB-C",   178000,"entry gaming","Entry"),
        ("HP X27 2022",   "X-series","27",  "FHD",  "IPS","144Hz","1","HDMI 2.0+DP 1.2",         185000,"entry gaming","Entry"),
        ("HP X27 2023",   "X-series","27",  "FHD",  "IPS","165Hz","1","HDMI 2.0+DP 1.2",         195000,"entry gaming","Entry"),
        ("HP X27q 2022",  "X-series","27",  "QHD",  "IPS","165Hz","1","HDMI 2.0+DP 1.4",         240000,"entry gaming","Standard"),
        ("HP X27q 2023",  "X-series","27",  "QHD",  "IPS","165Hz","1","HDMI 2.1+DP 1.4",         255000,"entry gaming","Standard"),
        ("HP X27i 2023",  "X-series","27",  "QHD",  "IPS","165Hz","1","HDMI 2.1+DP 1.4+USB-C",   275000,"entry gaming","Standard"),
        ("HP X32c 2022",  "X-series","31.5","FHD",  "VA", "165Hz","1","HDMI 2.0+DP 1.4",         290000,"entry gaming","Standard"),
        ("HP X32c 2023",  "X-series","31.5","QHD",  "VA", "165Hz","1","HDMI 2.1+DP 1.4",         330000,"entry gaming","Standard"),
        ("HP X34 2023",   "X-series","34",  "UWQHD","VA", "165Hz","1","HDMI 2.1+DP 1.4",         410000,"ultrawide gaming","Standard"),
    ]

    # ── HP M-series: Office Collaboration (webcam+speakers built-in) ─────────
    m_series = [
        ("HP M24 2022",  "M-series","23.8","FHD",  "IPS","60Hz","5","HDMI+USB-C 65W+DP",         170000,"collaboration","Standard"),
        ("HP M24 2023",  "M-series","23.8","FHD",  "IPS","75Hz","5","HDMI+USB-C 65W+DP",         180000,"collaboration","Standard"),
        ("HP M27 2022",  "M-series","27",  "FHD",  "IPS","60Hz","5","HDMI+USB-C 65W+DP",         220000,"collaboration","Standard"),
        ("HP M27 2023",  "M-series","27",  "QHD",  "IPS","75Hz","5","HDMI+USB-C 65W+DP",         270000,"collaboration","Standard"),
        ("HP M24fw 2022","M-series","23.8","FHD",  "IPS","60Hz","5","HDMI+USB-C 65W",             185000,"collaboration","Standard"),
        ("HP M27fw 2022","M-series","27",  "FHD",  "IPS","60Hz","5","HDMI+USB-C 65W",             235000,"collaboration","Standard"),
    ]

    # ── HP V-series: Value Business ──────────────────────────────────────────
    v_series = [
        ("HP V19 G4",   "V-series","18.5","HD",    "TN", "60Hz","5","VGA+HDMI",            45000,"office productivity","Entry"),
        ("HP V19 G5",   "V-series","18.5","HD",    "TN", "60Hz","5","VGA+HDMI",            48000,"office productivity","Entry"),
        ("HP V20 G4",   "V-series","19.5","HD+",   "TN", "60Hz","5","VGA+HDMI",            52000,"office productivity","Entry"),
        ("HP V20 G5",   "V-series","19.5","HD+",   "TN", "60Hz","5","VGA+HDMI",            55000,"office productivity","Entry"),
        ("HP V22 G5",   "V-series","21.5","FHD",   "IPS","60Hz","5","VGA+HDMI",            68000,"office productivity","Entry"),
        ("HP V22 G5",   "V-series","21.5","FHD",   "IPS","75Hz","5","HDMI+DP",             74000,"office productivity","Entry"),
        ("HP V22e G5",  "V-series","21.5","FHD",   "IPS","60Hz","5","VGA+HDMI+DP",         72000,"office productivity","Entry"),
        ("HP V22i G5",  "V-series","21.5","FHD",   "IPS","75Hz","5","HDMI+DP",             76000,"office productivity","Entry"),
        ("HP V24 G5",   "V-series","23.8","FHD",   "IPS","60Hz","5","VGA+HDMI",            80000,"office productivity","Entry"),
        ("HP V24 G5",   "V-series","23.8","FHD",   "IPS","75Hz","5","HDMI+DP",             86000,"office productivity","Entry"),
        ("HP V24i G5",  "V-series","23.8","FHD",   "IPS","75Hz","5","HDMI+DP",             88000,"office productivity","Entry"),
        ("HP V24e G5",  "V-series","23.8","FHD",   "IPS","75Hz","5","VGA+HDMI+DP",         85000,"office productivity","Entry"),
        ("HP V27 G5",   "V-series","27",  "FHD",   "IPS","75Hz","5","HDMI+DP",            105000,"office productivity","Entry"),
        ("HP V27i G5",  "V-series","27",  "FHD",   "IPS","75Hz","5","HDMI+DP",            108000,"office productivity","Entry"),
        ("HP V32 G5",   "V-series","31.5","FHD",   "VA", "75Hz","5","VGA+HDMI+DP",        135000,"office productivity","Standard"),
    ]

    # ── HP EliteDisplay series (commercial heritage) ──────────────────────────
    elitedisplay = [
        ("EliteDisplay E190i G1","EliteDisplay","18.9","WXGA+","IPS","60Hz","8","VGA+DVI+DP",      90000,"office productivity","Standard"),
        ("EliteDisplay E221i G1","EliteDisplay","21.5","FHD",  "IPS","60Hz","8","VGA+DVI+DP",     110000,"office productivity","Standard"),
        ("EliteDisplay E231 G1", "EliteDisplay","23",  "FHD",  "IPS","60Hz","8","VGA+DVI+DP",     118000,"office productivity","Standard"),
        ("EliteDisplay E241i G1","EliteDisplay","24",  "WUXGA","IPS","60Hz","8","VGA+DVI+DP",     135000,"office productivity","Standard"),
        ("EliteDisplay E242 G1", "EliteDisplay","23.8","FHD",  "IPS","60Hz","5","VGA+HDMI+DP",    125000,"office productivity","Standard"),
        ("EliteDisplay E243 G1", "EliteDisplay","23.8","FHD",  "IPS","60Hz","5","VGA+HDMI+DP",    130000,"office productivity","Standard"),
        ("EliteDisplay E243i G1","EliteDisplay","23.8","WUXGA","IPS","60Hz","5","HDMI+DP+USB-C",  155000,"office productivity","Standard"),
        ("EliteDisplay E272q G1","EliteDisplay","27",  "QHD",  "IPS","60Hz","5","VGA+HDMI+DP",    195000,"office productivity","Standard"),
        ("EliteDisplay E273 G1", "EliteDisplay","27",  "FHD",  "IPS","60Hz","5","VGA+HDMI+DP",    165000,"office productivity","Standard"),
        ("EliteDisplay E273q G1","EliteDisplay","27",  "QHD",  "IPS","60Hz","5","VGA+HDMI+DP",    205000,"office productivity","Standard"),
        ("EliteDisplay E274 G1", "EliteDisplay","27",  "4K UHD","IPS","60Hz","5","HDMI+DP+USB-C", 280000,"office productivity","Professional"),
        ("EliteDisplay E27d G1", "EliteDisplay","27",  "QHD",  "IPS","60Hz","5","TB3+HDMI+DP",    310000,"office productivity","Professional"),
        ("EliteDisplay S14 G1",  "EliteDisplay","14",  "FHD",  "IPS","60Hz","5","USB-C portable", 120000,"portable display",   "Standard"),
        ("EliteDisplay S240uj G1","EliteDisplay","23.8","4K UHD","IPS","60Hz","5","TB3+HDMI+DP",  295000,"office productivity","Professional"),
    ]

    # ── HP Series 3 / 5 / 7 ──────────────────────────────────────────────────
    series_357 = [
        ("HP 3 Series 27 4K 2023",   "Series 3","27",  "4K UHD","IPS","60Hz","5","HDMI+DP+USB-C 65W",  245000,"home office","Standard"),
        ("HP 3 Series 27 4K 2024",   "Series 3","27",  "4K UHD","IPS","60Hz","5","HDMI+DP+USB-C 65W",  258000,"home office","Standard"),
        ("HP 3 Series 32 QHD 2023",  "Series 3","31.5","QHD",   "IPS","75Hz","5","HDMI+DP+USB-C 65W",  290000,"home office","Standard"),
        ("HP 3 Series 32 QHD 2024",  "Series 3","31.5","QHD",   "IPS","75Hz","5","HDMI+DP+USB-C 65W",  305000,"home office","Standard"),
        ("HP 5 Series 27 4K 2023",   "Series 5","27",  "4K UHD","IPS","60Hz","5","TB3+HDMI+DP+USB-C",  320000,"home office","Professional"),
        ("HP 5 Series 27 4K 2024",   "Series 5","27",  "4K UHD","IPS","60Hz","5","TB4+HDMI+DP+USB-C",  340000,"home office","Professional"),
        ("HP 5 Series 34 WQHD 2023", "Series 5","34",  "WQHD",  "IPS","60Hz","5","TB3+HDMI+DP+USB-C",  420000,"home office","Professional"),
        ("HP 5 Series 40 Wide 2024", "Series 5","39.7","WUHD",  "IPS","72Hz","5","TB4+HDMI+DP+USB-C",  650000,"home office","Workstation"),
        ("HP 7 Series 32 4K 2023",   "Series 7","31.5","4K UHD","IPS","60Hz","5","TB4+HDMI+DP+USB-C 96W",560000,"professional use","Workstation"),
        ("HP 7 Series 32 4K 2024",   "Series 7","31.5","4K UHD","IPS","72Hz","5","TB4+HDMI+DP+USB-C 96W",590000,"professional use","Workstation"),
        ("HP 7 Series 40 Wide 2023", "Series 7","39.7","WUHD",  "IPS","72Hz","5","TB4+HDMI+2xDP+USB-C", 890000,"professional use","Workstation"),
        ("HP 7 Series 40 Wide 2024", "Series 7","39.7","WUHD",  "IPS","72Hz","5","TB4+HDMI+2xDP+USB-C", 940000,"professional use","Workstation"),
    ]

    def add_product(all_products, series_name, model, size, res_label, panel, refresh,
                    resp, conn, base_price, use_case, tier, suffix="", price_delta=0):
        full_model = model
        price = base_price + price_delta
        variant = f"{size}\" {res_label} {panel} {refresh}{' ' + suffix if suffix else ''}"
        sku = slugify(f"hp_{model}_{size}_{res_label}_{panel}_{refresh}{'_' + slugify(suffix) if suffix else ''}")
        desc = monitor_desc(series_name, full_model, size, res_label, panel,
                            refresh, resp, conn, use_case, tier)
        all_products.append((full_model, variant, sku, desc, {
            "series": series_name,
            "screen_size": size,
            "resolution": res_label,
            "panel_type": panel,
            "refresh_rate": refresh,
            "response_time": resp,
            "connectivity": conn,
            "price_tier": tier,
            "nigeria_market_price_naira": str(price),
        }))

    # E-series + P-series + Z-series + V-series + EliteDisplay: add stand variants
    for (model, series, size, res, panel, refresh, resp, conn, base, use_case, tier) in (
            e_series + p_series + z_series + v_series + elitedisplay):
        for (stand_label, stand_delta, stand_tag) in STAND_VARIANTS:
            add_product(products, series, model, size, res, panel, refresh,
                        resp, conn, base, use_case, tier,
                        suffix=stand_label, price_delta=stand_delta)

    # OMEN + X-series + M-series: add Black and Silver color variants
    for entry in (omen_series + x_series + m_series):
        (model, series, size, res, panel, refresh, resp, conn, base, use_case, tier) = entry
        for (color, color_delta, _) in COLOR_VARIANTS:
            add_product(products, series, model, size, res, panel, refresh,
                        resp, conn, base, use_case, tier,
                        suffix=color, price_delta=color_delta)

    # Series 3/5/7: add 3 connectivity variants
    conn_variants = [
        ("Standard",         0),
        ("USB Hub Edition",  12000),
        ("Daisy-Chain Ed.",  20000),
    ]
    for (model, series, size, res, panel, refresh, resp, conn, base, use_case, tier) in series_357:
        for (cv_label, cv_delta) in conn_variants:
            add_product(products, series, model, size, res, panel, refresh,
                        resp, conn, base, use_case, tier,
                        suffix=cv_label, price_delta=cv_delta)

    return products


# ─────────────────────────────────────────────────────────────────────────────
# INSERT HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def upsert_category(cur, cat):
    cur.execute("""
        INSERT INTO catalogue_categories
            (name, slug, icon, description, sort_order, is_active, created_by)
        VALUES (%s,%s,%s,%s,%s,TRUE,'system')
        ON CONFLICT (slug) DO NOTHING
        RETURNING id
    """, (cat["name"], cat["slug"], cat["icon"], cat["description"], cat["sort_order"]))
    row = cur.fetchone()
    if row:
        return row["id"]
    cur.execute("SELECT id FROM catalogue_categories WHERE slug=%s", (cat["slug"],))
    return cur.fetchone()["id"]


def upsert_attrs(cur, cat_id, attrs):
    id_map = {}
    for (key, label, dtype, unit, filterable, required, sort) in attrs:
        cur.execute("""
            INSERT INTO catalogue_attribute_definitions
                (category_id, attribute_key, attribute_label, data_type, unit,
                 is_filterable, is_required, sort_order)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT DO NOTHING
            RETURNING id
        """, (cat_id, key, label, dtype, unit, filterable, required, sort))
        row = cur.fetchone()
        if not row:
            cur.execute(
                "SELECT id FROM catalogue_attribute_definitions WHERE category_id=%s AND attribute_key=%s",
                (cat_id, key)
            )
            row = cur.fetchone()
        id_map[key] = row["id"]
    return id_map


def insert_products(cur, conn, cat_id, attr_id_map, products, cat_name):
    inserted = 0
    attr_ins  = 0
    total     = len(products)

    for i in range(0, total, BATCH):
        batch = products[i:i + BATCH]
        for (model_name, variant, sku, desc, attrs) in batch:
            cur.execute("""
                INSERT INTO catalogue_products
                    (category_id, brand, model_name, model_number, sku, description, is_active)
                VALUES (%s,%s,%s,%s,%s,%s,TRUE)
                ON CONFLICT (sku) DO NOTHING
                RETURNING id
            """, (cat_id, BRAND, model_name, variant, sku, desc))
            row = cur.fetchone()
            if not row:
                cur.execute("SELECT id FROM catalogue_products WHERE sku=%s", (sku,))
                row = cur.fetchone()
                if not row:
                    continue
            else:
                inserted += 1

            prod_id = row["id"]
            for key, val in attrs.items():
                if val is None or val == "":
                    continue
                def_id = attr_id_map.get(key)
                if not def_id:
                    continue
                cur.execute("""
                    INSERT INTO catalogue_product_attributes (product_id, attribute_def_id, value)
                    VALUES (%s,%s,%s)
                    ON CONFLICT (product_id, attribute_def_id) DO NOTHING
                """, (prod_id, def_id, val))
                attr_ins += 1

        conn.commit()
        done = min(i + BATCH, total)
        print(f"  [{cat_name}] {done}/{total}", end="\r", flush=True)

    print(f"\n  [{cat_name}] inserted {inserted} products, {attr_ins} attribute values")
    return inserted


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

GENERATORS = {
    "HP Laptops":      gen_laptops,
    "HP Desktops":     gen_desktops,
    "HP Servers":      gen_servers,
    "HP Thin Clients": gen_thin_clients,
    "HP Monitors":     gen_monitors,
}


def run():
    conn = psycopg2.connect(**DB)
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    grand_total = 0

    for cat_def in CATEGORIES:
        cat_id = upsert_category(cur, cat_def)
        conn.commit()

        attr_id_map = upsert_attrs(cur, cat_id, cat_def["attributes"])
        conn.commit()
        print(f"✓ {cat_def['name']}  category_id={cat_id}  attrs={len(attr_id_map)}")

        products = GENERATORS[cat_def["name"]]()
        print(f"  Generated {len(products)} products …")

        n = insert_products(cur, conn, cat_id, attr_id_map, products, cat_def["name"])
        grand_total += n

    # Final verification
    cur.execute("""
        SELECT cc.name, COUNT(cp.id) AS cnt
        FROM catalogue_categories cc
        LEFT JOIN catalogue_products cp ON cp.category_id = cc.id AND cp.is_active
        WHERE cc.slug LIKE 'hp-%'
        GROUP BY cc.name
        ORDER BY cc.name
    """)
    print("\n── Final counts ─────────────────────────")
    total_check = 0
    for row in cur.fetchall():
        print(f"  {row['name']}: {row['cnt']}")
        total_check += row['cnt']
    print(f"  TOTAL HP PRODUCTS: {total_check}")

    cur.close()
    conn.close()
    print("Done.")


if __name__ == "__main__":
    run()
