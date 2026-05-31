"""
Generate ~3,000 Lenovo business products across 5 categories:
  Lenovo Laptops (~950), Lenovo Desktops (~650), Lenovo Servers (~500),
  Lenovo Thin Clients (~400), Lenovo Monitors (~500)

Each product has: brand, model_name, variant (model_number), SKU,
description, category-specific attributes, and a Nigeria market price.

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
BRAND = "Lenovo"


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


# ─────────────────────────────────────────────────────────────────────────────
# CATEGORY + ATTRIBUTE DEFINITIONS
# ─────────────────────────────────────────────────────────────────────────────

CATEGORIES = [
    {
        "name": "Lenovo Laptops",
        "slug": "lenovo-laptops",
        "icon": "💻",
        "description": "Lenovo business and consumer laptops — ThinkPad, ThinkBook, IdeaPad, Yoga, Legion and LOQ series",
        "sort_order": 12,
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
        "name": "Lenovo Desktops",
        "slug": "lenovo-desktops",
        "icon": "🖥️",
        "description": "Lenovo business and workstation desktops — ThinkCentre, ThinkStation, IdeaCentre and Legion Tower",
        "sort_order": 13,
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
        "name": "Lenovo Servers",
        "slug": "lenovo-servers",
        "icon": "🖧",
        "description": "Lenovo ThinkSystem rack and tower servers for business data centres and enterprise infrastructure",
        "sort_order": 14,
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
        "name": "Lenovo Thin Clients",
        "slug": "lenovo-thin-clients",
        "icon": "🖱️",
        "description": "Lenovo ThinkCentre Tiny and ThinkEdge thin clients for VDI and cloud-managed enterprise deployments",
        "sort_order": 15,
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
        "name": "Lenovo Monitors",
        "slug": "lenovo-monitors",
        "icon": "🖥",
        "description": "Lenovo ThinkVision business, professional and gaming monitors for every workspace",
        "sort_order": 16,
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
        f"The Lenovo {model} is a {tier.lower()} laptop engineered for {use_case}. "
        f"Powered by an {cpu} with {ram} of memory and {storage} storage, it delivers "
        f"the performance and reliability demanded by modern business workflows. "
        f"The {display}-inch display provides clear, comfortable visuals for extended "
        f"use, while running {os}. MIL-SPEC tested for durability, it is built to "
        f"withstand the rigours of daily business travel and office environments.",

        f"Designed for {use_case} professionals, the Lenovo {model} combines an {cpu} "
        f"processor with {ram} RAM and {storage} SSD for fluid multitasking and fast "
        f"data access. The {display}-inch display and {os} platform integrate seamlessly "
        f"with enterprise IT infrastructure. Lenovo Vantage software provides intelligent "
        f"performance management and system health monitoring to keep productivity high.",

        f"The Lenovo {series} {model} delivers dependable {use_case} performance in a "
        f"slim, professional design. Its {cpu} paired with {ram} RAM and {storage} "
        f"ensures responsive performance across business applications. The {display}-inch "
        f"display and {os} make it a versatile tool for office, remote and hybrid work. "
        f"Lenovo's global service network and ThinkShield security suite provide "
        f"comprehensive endpoint protection.",

        f"Built for the demands of {use_case}, the Lenovo {model} features an {cpu}, "
        f"{ram} RAM and {storage} in a business-ready package. The {display}-inch screen "
        f"supports comfortable all-day computing, and {os} ensures broad software "
        f"compatibility. With rapid-charge technology delivering hours of battery life "
        f"from a short charge, it keeps business moving without interruption.",
    ]
    return descs[hash(model + cpu + ram + storage) % len(descs)]


def desktop_desc(series, model, cpu, ram, storage, form, os, use_case, tier):
    descs = [
        f"The Lenovo {model} is a {form} desktop built for {use_case}. Powered by an "
        f"{cpu} with {ram} of memory and {storage} storage, it handles business "
        f"applications, collaboration tools and data workloads with ease. Running {os}, "
        f"it supports enterprise deployment via Lenovo Device Manager. Its {form} "
        f"design fits any workspace and supports VESA mounting for flexible deployment.",

        f"Engineered for {use_case} environments, the Lenovo {model} {form} desktop "
        f"combines an {cpu} with {ram} RAM and {storage} to power productivity and "
        f"communication tools. The {os} platform ensures broad software compatibility "
        f"and enterprise security compliance. Lenovo ThinkShield provides hardware-level "
        f"protection from boot to runtime, giving IT administrators confidence across "
        f"large commercial deployments.",

        f"The Lenovo {series} {model} offers reliable {use_case} performance in a {form} "
        f"chassis. Its {cpu} processor, {ram} memory and {storage} deliver consistent "
        f"speed for all business tasks. {os} provides a stable, secure foundation while "
        f"Lenovo's commercial-grade build ensures a long service life. The energy-efficient "
        f"design meets ENERGY STAR standards, reducing total cost of ownership.",

        f"Designed for business efficiency, the Lenovo {model} {form} desktop is equipped "
        f"with an {cpu}, {ram} RAM and {storage} for {use_case} workloads. The {os} "
        f"environment supports enterprise software suites and Lenovo's security portfolio "
        f"protects endpoints from modern threats. Tool-less chassis access simplifies "
        f"upgrades and maintenance for IT teams managing large fleets.",
    ]
    return descs[hash(model + cpu + ram + storage) % len(descs)]


def server_desc(series, model, cpu, ram, storage, form, bays, max_r, ports):
    descs = [
        f"The Lenovo {model} is a {form} server built for reliable business infrastructure. "
        f"Powered by {cpu} with {ram} of ECC memory and {storage} storage, it handles "
        f"virtualisation, databases and mission-critical workloads. Supporting up to "
        f"{max_r} maximum RAM across {bays} drive bays, it scales with business growth. "
        f"{ports} network connectivity ensures high-throughput data access. Lenovo "
        f"XClarity Controller provides comprehensive remote server lifecycle management.",

        f"The Lenovo ThinkSystem {model} delivers enterprise-grade reliability in a "
        f"{form} form factor. Featuring {cpu}, {ram} RAM and {storage}, it is optimised "
        f"for web hosting, file services, ERP and virtualisation. With {bays} drive bays "
        f"and capacity for up to {max_r} RAM, it grows with your infrastructure. "
        f"{ports} provide fast, redundant connectivity. Lenovo XClarity simplifies "
        f"deployment, monitoring and firmware management across your server fleet.",

        f"Built for SME and enterprise data centres, the Lenovo {model} {form} server "
        f"offers {cpu} processing, {ram} ECC memory and {storage} storage. Designed for "
        f"24/7 operation with redundant power and cooling options, it includes Lenovo "
        f"XClarity remote management, {bays} drive bays and memory expandability to "
        f"{max_r}. {ports} network ports support high-speed connectivity for demanding "
        f"workloads. Lenovo's global support infrastructure ensures rapid incident response.",

        f"The Lenovo ThinkSystem {model} is engineered for demanding {form} deployments. "
        f"Starting with {ram} RAM and {storage}, it supports virtualisation, analytics "
        f"and hybrid cloud workloads powered by {cpu}. Scale storage across {bays} bays "
        f"and memory up to {max_r}. With {ports} and Lenovo's XClarity management "
        f"ecosystem, this server is a trusted backbone for modern business operations.",
    ]
    return descs[hash(model + cpu + ram + storage) % len(descs)]


def thin_client_desc(series, model, cpu, ram, storage, os, form, use_case):
    descs = [
        f"The Lenovo {model} is a {form} thin client designed for {use_case} environments. "
        f"Powered by {cpu} with {ram} RAM and {storage} storage, it delivers responsive "
        f"virtual desktop access with minimal power draw. Running {os}, it is optimised "
        f"for Citrix, VMware Horizon and Microsoft RDS deployments. Lenovo Device Manager "
        f"enables centralised policy management and zero-touch provisioning at scale.",

        f"Built for modern {use_case} computing, the Lenovo {model} offers {cpu} "
        f"performance, {ram} RAM and {storage} flash storage in a compact {form} design. "
        f"{os} provides a secure, locked-down environment for healthcare, finance, "
        f"education and call-centre deployments. Low power consumption and quiet "
        f"operation suit space and noise-sensitive environments. ThinkShield security "
        f"provides hardware-level endpoint protection.",

        f"The Lenovo {series} {model} delivers reliable virtual desktop access for "
        f"{use_case} users. With {cpu}, {ram} RAM and {storage}, it provides smooth "
        f"performance for cloud-hosted applications and VDI sessions. Its {form} design "
        f"and {os} platform support easy mass deployment and remote management through "
        f"Lenovo Device Manager. The compact chassis supports multiple monitor outputs "
        f"for multi-display productivity setups.",

        f"Ideal for shared workspaces and {use_case} deployments, the Lenovo {model} "
        f"combines {cpu} processing with {ram} RAM and {storage} in a {form} chassis. "
        f"{os} enables secure, centralised computing with minimal local data risk. "
        f"Lenovo's Device-as-a-Service offering simplifies acquisition, management and "
        f"refresh cycles, reducing the total cost of ownership for IT administrators.",
    ]
    return descs[hash(model + cpu + ram + storage) % len(descs)]


def monitor_desc(series, model, size, res, panel, refresh, resp, conn, use_case, tier):
    descs = [
        f"The Lenovo {model} is a {size}-inch {panel} monitor delivering {res} resolution "
        f"for {use_case} professionals. With a {refresh} refresh rate and {resp}ms "
        f"response time, it provides sharp, accurate visuals for productivity and "
        f"creative work. {conn} connectivity ensures broad compatibility with modern "
        f"laptops and docking stations. TÜV Rheinland-certified low blue light and "
        f"flicker-free technology protect eye health during extended work sessions.",

        f"Designed for {use_case} environments, the Lenovo {model} offers a {size}-inch "
        f"{res} {panel} panel with {refresh} refresh rate and {resp}ms response time. "
        f"Its slim bezel design supports multi-monitor setups, while {conn} ports "
        f"provide flexible connectivity options. Natural Low Blue Light technology "
        f"filters harmful blue light at the hardware level without colour distortion, "
        f"ensuring comfortable viewing throughout the working day.",

        f"The Lenovo {series} {model} brings {res} clarity to a {size}-inch {panel} "
        f"display for {use_case}. A {refresh} refresh rate ensures smooth rendering "
        f"and {resp}ms response time keeps motion crisp. {conn} connectivity and VESA "
        f"mount compatibility support flexible workspace configurations. Factory colour "
        f"calibration delivers consistent, accurate colour reproduction from day one.",

        f"Built to enhance {use_case} productivity, the Lenovo {model} features a "
        f"{size}-inch {panel} screen with {res} resolution, {refresh} refresh rate "
        f"and {resp}ms response time. {conn} connection options support the latest "
        f"docking solutions, and the anti-glare coating ensures comfortable viewing "
        f"under office lighting. Lenovo's Artery software enables easy multi-monitor "
        f"management and display customisation.",
    ]
    return descs[hash(model + res + refresh + panel) % len(descs)]


# ─────────────────────────────────────────────────────────────────────────────
# PRODUCT GENERATORS
# ─────────────────────────────────────────────────────────────────────────────

def gen_laptops():
    products = []

    # ── ThinkPad E-series (affordable business) ───────────────────────────────
    for model, display in [
        ("ThinkPad E14 Gen 4",  "14"), ("ThinkPad E14 Gen 5",  "14"),
        ("ThinkPad E15 Gen 4",  "15.6"), ("ThinkPad E16 Gen 1",  "16"),
        ("ThinkPad E16 Gen 2",  "16"),
    ]:
        for cpu in ["Intel Core i3-1215U", "Intel Core i5-1235U", "Intel Core i7-1255U",
                    "AMD Ryzen 5 5625U", "AMD Ryzen 7 5825U"]:
            for ram in ["8GB DDR4", "16GB DDR4", "32GB DDR4"]:
                for storage in ["256GB SSD", "512GB SSD", "1TB SSD"]:
                    price = {"8GB DDR4":280000,"16GB DDR4":375000,"32GB DDR4":530000}[ram]
                    price += {"256GB SSD":0,"512GB SSD":26000,"1TB SSD":62000}[storage]
                    price += {"Intel Core i3-1215U":0,"Intel Core i5-1235U":52000,
                               "Intel Core i7-1255U":125000,"AMD Ryzen 5 5625U":45000,
                               "AMD Ryzen 7 5825U":115000}[cpu]
                    variant = f"{ram} {storage}"
                    sku = slugify(f"lenovo_{model}_{cpu.split()[-1]}_{ram.split()[0]}_{storage.split()[0]}")
                    products.append((model, variant, sku,
                        laptop_desc("ThinkPad", model, cpu, ram, storage, display,
                                    "Windows 11 Pro", "business productivity", "Business"),
                        {"series":"ThinkPad E","processor":cpu,"ram":ram,"storage":storage,
                         "display_size":display,"display_type":"IPS","graphics":"Intel Iris Xe / AMD Radeon",
                         "operating_system":"Windows 11 Pro","battery_life":"9","weight_kg":"1.65",
                         "price_tier":"Business","nigeria_market_price_naira":str(price)}))

    # ── ThinkPad L-series (mainstream business) ───────────────────────────────
    for model, display in [
        ("ThinkPad L14 Gen 3",  "14"), ("ThinkPad L14 Gen 4",  "14"),
        ("ThinkPad L15 Gen 3",  "15.6"), ("ThinkPad L15 Gen 4",  "15.6"),
        ("ThinkPad L13 Gen 3",  "13.3"), ("ThinkPad L13 Gen 4",  "13.3"),
    ]:
        for cpu in ["Intel Core i5-1235U", "Intel Core i7-1255U", "Intel Core i5-1335U",
                    "AMD Ryzen 5 PRO 6650U", "AMD Ryzen 7 PRO 6850U"]:
            for ram in ["8GB DDR4", "16GB DDR4", "32GB DDR4"]:
                for storage in ["256GB SSD", "512GB SSD", "1TB SSD"]:
                    price = {"8GB DDR4":340000,"16GB DDR4":455000,"32GB DDR4":640000}[ram]
                    price += {"256GB SSD":0,"512GB SSD":27000,"1TB SSD":65000}[storage]
                    price += {"Intel Core i5-1235U":0,"Intel Core i7-1255U":110000,
                               "Intel Core i5-1335U":55000,"AMD Ryzen 5 PRO 6650U":50000,
                               "AMD Ryzen 7 PRO 6850U":130000}[cpu]
                    variant = f"{ram} {storage}"
                    sku = slugify(f"lenovo_{model}_{cpu.split()[-1]}_{ram.split()[0]}_{storage.split()[0]}")
                    products.append((model, variant, sku,
                        laptop_desc("ThinkPad", model, cpu, ram, storage, display,
                                    "Windows 11 Pro", "mainstream business use", "Business"),
                        {"series":"ThinkPad L","processor":cpu,"ram":ram,"storage":storage,
                         "display_size":display,"display_type":"IPS","graphics":"Intel Iris Xe / AMD Radeon",
                         "operating_system":"Windows 11 Pro","battery_life":"10","weight_kg":"1.58",
                         "price_tier":"Business","nigeria_market_price_naira":str(price)}))

    # ── ThinkPad T-series (premium business) ─────────────────────────────────
    for model, display, weight in [
        ("ThinkPad T14 Gen 3",  "14",   "1.21"),
        ("ThinkPad T14 Gen 4",  "14",   "1.21"),
        ("ThinkPad T14s Gen 3", "14",   "1.16"),
        ("ThinkPad T14s Gen 4", "14",   "1.16"),
        ("ThinkPad T16 Gen 1",  "16",   "1.76"),
        ("ThinkPad T16 Gen 2",  "16",   "1.76"),
        ("ThinkPad T15p Gen 3", "15.6", "1.85"),
    ]:
        for cpu in ["Intel Core i5-1245U", "Intel Core i7-1265U", "Intel Core i5-1335U",
                    "Intel Core i7-1365U", "AMD Ryzen 5 PRO 6650U", "AMD Ryzen 7 PRO 6850U"]:
            for ram in ["8GB DDR5", "16GB DDR5", "32GB DDR5"]:
                for storage in ["256GB SSD", "512GB SSD", "1TB SSD"]:
                    price = {"8GB DDR5":480000,"16GB DDR5":640000,"32GB DDR5":900000}[ram]
                    price += {"256GB SSD":0,"512GB SSD":28000,"1TB SSD":68000}[storage]
                    price += {"Intel Core i5-1245U":0,"Intel Core i7-1265U":130000,
                               "Intel Core i5-1335U":60000,"Intel Core i7-1365U":160000,
                               "AMD Ryzen 5 PRO 6650U":55000,"AMD Ryzen 7 PRO 6850U":140000}[cpu]
                    variant = f"{ram} {storage}"
                    sku = slugify(f"lenovo_{model}_{cpu.split()[-1]}_{ram.split()[0]}_{storage.split()[0]}")
                    products.append((model, variant, sku,
                        laptop_desc("ThinkPad", model, cpu, ram, storage, display,
                                    "Windows 11 Pro", "enterprise professionals", "Premium"),
                        {"series":"ThinkPad T","processor":cpu,"ram":ram,"storage":storage,
                         "display_size":display,"display_type":"IPS","graphics":"Intel Iris Xe / AMD Radeon",
                         "operating_system":"Windows 11 Pro","battery_life":"12","weight_kg":weight,
                         "price_tier":"Premium","nigeria_market_price_naira":str(price)}))

    # ── ThinkPad X1 series (flagship) ────────────────────────────────────────
    for model, display, weight in [
        ("ThinkPad X1 Carbon Gen 10",  "14",   "1.12"),
        ("ThinkPad X1 Carbon Gen 11",  "14",   "1.12"),
        ("ThinkPad X1 Carbon Gen 12",  "14",   "1.12"),
        ("ThinkPad X1 Extreme Gen 5",  "16",   "1.84"),
        ("ThinkPad X1 Extreme Gen 6",  "16",   "1.84"),
        ("ThinkPad X1 Yoga Gen 7",     "14",   "1.38"),
        ("ThinkPad X1 Yoga Gen 8",     "14",   "1.38"),
        ("ThinkPad X13 Gen 3",         "13.3", "1.18"),
        ("ThinkPad X13 Gen 4",         "13.3", "1.18"),
        ("ThinkPad X13s Gen 1",        "13.3", "1.06"),
    ]:
        for cpu in ["Intel Core i5-1245U", "Intel Core i7-1265U",
                    "Intel Core i7-1365U", "Intel Core Ultra 7 155U"]:
            for ram in ["16GB LPDDR5", "32GB LPDDR5", "64GB LPDDR5"]:
                for storage in ["512GB SSD", "1TB SSD", "2TB SSD"]:
                    price = {"16GB LPDDR5":850000,"32GB LPDDR5":1200000,"64GB LPDDR5":1750000}[ram]
                    price += {"512GB SSD":0,"1TB SSD":85000,"2TB SSD":200000}[storage]
                    price += {"Intel Core i5-1245U":0,"Intel Core i7-1265U":140000,
                               "Intel Core i7-1365U":170000,"Intel Core Ultra 7 155U":290000}[cpu]
                    variant = f"{ram} {storage}"
                    sku = slugify(f"lenovo_{model}_{cpu.split()[-1]}_{ram.split()[0]}_{storage.split()[0]}")
                    products.append((model, variant, sku,
                        laptop_desc("ThinkPad X1", model, cpu, ram, storage, display,
                                    "Windows 11 Pro", "senior executives and road warriors", "Premium"),
                        {"series":"ThinkPad X1","processor":cpu,"ram":ram,"storage":storage,
                         "display_size":display,"display_type":"IPS/OLED","graphics":"Intel Iris Xe",
                         "operating_system":"Windows 11 Pro","battery_life":"15","weight_kg":weight,
                         "price_tier":"Premium","nigeria_market_price_naira":str(price)}))

    # ── ThinkBook series (SMB) ────────────────────────────────────────────────
    for model, display in [
        ("ThinkBook 13s Gen 4",  "13.3"), ("ThinkBook 14 G4 IAP",  "14"),
        ("ThinkBook 14 G5 IRL",  "14"),   ("ThinkBook 15 G4 IAP",  "15.6"),
        ("ThinkBook 16 G4 IAP",  "16"),   ("ThinkBook 16 G6 IRL",  "16"),
        ("ThinkBook 14p Gen 3",  "14.2"),
    ]:
        for cpu in ["Intel Core i3-1215U", "Intel Core i5-1235U", "Intel Core i7-1255U",
                    "AMD Ryzen 5 5625U", "AMD Ryzen 7 5825U"]:
            for ram in ["8GB DDR4", "16GB DDR4", "32GB DDR4"]:
                for storage in ["256GB SSD", "512GB SSD", "1TB SSD"]:
                    price = {"8GB DDR4":295000,"16GB DDR4":395000,"32GB DDR4":560000}[ram]
                    price += {"256GB SSD":0,"512GB SSD":25000,"1TB SSD":60000}[storage]
                    price += {"Intel Core i3-1215U":0,"Intel Core i5-1235U":50000,
                               "Intel Core i7-1255U":120000,"AMD Ryzen 5 5625U":45000,
                               "AMD Ryzen 7 5825U":110000}[cpu]
                    variant = f"{ram} {storage}"
                    sku = slugify(f"lenovo_{model}_{cpu.split()[-1]}_{ram.split()[0]}_{storage.split()[0]}")
                    products.append((model, variant, sku,
                        laptop_desc("ThinkBook", model, cpu, ram, storage, display,
                                    "Windows 11 Pro", "small and medium businesses", "Business"),
                        {"series":"ThinkBook","processor":cpu,"ram":ram,"storage":storage,
                         "display_size":display,"display_type":"IPS","graphics":"Intel Iris Xe / AMD Radeon",
                         "operating_system":"Windows 11 Pro","battery_life":"10","weight_kg":"1.56",
                         "price_tier":"Business","nigeria_market_price_naira":str(price)}))

    # ── Yoga series (premium convertible) ────────────────────────────────────
    for model, display in [
        ("Yoga 7 Gen 7",  "14"), ("Yoga 7 Gen 8",  "14"), ("Yoga 7 Gen 8",  "16"),
        ("Yoga 9 Gen 7",  "14"), ("Yoga 9 Gen 8",  "14"),
        ("Yoga Slim 6 Gen 8", "14"), ("Yoga Slim 7 Gen 8", "14"),
    ]:
        for cpu in ["Intel Core i5-1235U", "Intel Core i7-1255U",
                    "AMD Ryzen 5 7530U", "AMD Ryzen 7 7730U"]:
            for ram in ["8GB LPDDR5", "16GB LPDDR5", "32GB LPDDR5"]:
                for storage in ["512GB SSD", "1TB SSD"]:
                    price = {"8GB LPDDR5":380000,"16GB LPDDR5":520000,"32GB LPDDR5":750000}[ram]
                    price += {"512GB SSD":0,"1TB SSD":80000}[storage]
                    price += {"Intel Core i5-1235U":0,"Intel Core i7-1255U":130000,
                               "AMD Ryzen 5 7530U":40000,"AMD Ryzen 7 7730U":145000}[cpu]
                    variant = f"{ram} {storage}"
                    sku = slugify(f"lenovo_{model}_{cpu.split()[-1]}_{ram.split()[0]}_{storage.split()[0]}")
                    products.append((model, variant, sku,
                        laptop_desc("Yoga", model, cpu, ram, storage, display,
                                    "Windows 11 Home", "creatives and professionals", "Premium"),
                        {"series":"Yoga","processor":cpu,"ram":ram,"storage":storage,
                         "display_size":display,"display_type":"IPS/OLED","graphics":"Intel Iris Xe / AMD Radeon",
                         "operating_system":"Windows 11 Home","battery_life":"13","weight_kg":"1.45",
                         "price_tier":"Premium","nigeria_market_price_naira":str(price)}))

    # ── Legion gaming ─────────────────────────────────────────────────────────
    for model, display in [
        ("Legion 5 Gen 7",  "15.6"), ("Legion 5 Gen 8",  "15.6"),
        ("Legion 5i Gen 7", "15.6"), ("Legion 5i Gen 8", "15.6"),
        ("Legion 7 Gen 7",  "15.6"), ("Legion 7i Gen 7", "15.6"),
        ("Legion Pro 5i Gen 8","16"), ("Legion Pro 7i Gen 8","16"),
    ]:
        for cpu in ["AMD Ryzen 5 6600H", "AMD Ryzen 7 6800H",
                    "Intel Core i5-12500H", "Intel Core i7-12700H", "Intel Core i9-12900HX"]:
            for ram in ["16GB DDR5", "32GB DDR5"]:
                for storage in ["512GB SSD", "1TB SSD"]:
                    price = {"16GB DDR5":620000,"32GB DDR5":880000}[ram]
                    price += {"512GB SSD":0,"1TB SSD":75000}[storage]
                    price += {"AMD Ryzen 5 6600H":0,"AMD Ryzen 7 6800H":110000,
                               "Intel Core i5-12500H":30000,"Intel Core i7-12700H":120000,
                               "Intel Core i9-12900HX":280000}[cpu]
                    variant = f"{ram} {storage}"
                    sku = slugify(f"lenovo_{model}_{cpu.split()[-1]}_{ram.split()[0]}_{storage.split()[0]}")
                    products.append((model, variant, sku,
                        laptop_desc("Legion", model, cpu, ram, storage, display,
                                    "Windows 11 Home", "gaming and content creation", "Premium"),
                        {"series":"Legion","processor":cpu,"ram":ram,"storage":storage,
                         "display_size":display,"display_type":"IPS 144Hz+",
                         "graphics":"NVIDIA GeForce RTX 3060/4060",
                         "operating_system":"Windows 11 Home","battery_life":"6","weight_kg":"2.40",
                         "price_tier":"Premium","nigeria_market_price_naira":str(price)}))

    # ── LOQ (affordable gaming) ───────────────────────────────────────────────
    for model, display in [
        ("LOQ 15IAX9",  "15.6"), ("LOQ 15IRH8",  "15.6"),
        ("LOQ 16IRH8",  "16"),   ("LOQ 15APH8",  "15.6"),
    ]:
        for cpu in ["Intel Core i5-12450HX", "Intel Core i7-13650HX", "AMD Ryzen 5 7640HS"]:
            for ram in ["8GB DDR5", "16GB DDR5", "32GB DDR5"]:
                for storage in ["512GB SSD", "1TB SSD"]:
                    price = {"8GB DDR5":370000,"16GB DDR5":500000,"32GB DDR5":720000}[ram]
                    price += {"512GB SSD":0,"1TB SSD":72000}[storage]
                    price += {"Intel Core i5-12450HX":0,"Intel Core i7-13650HX":120000,"AMD Ryzen 5 7640HS":30000}[cpu]
                    variant = f"{ram} {storage}"
                    sku = slugify(f"lenovo_{model}_{cpu.split()[-1]}_{ram.split()[0]}_{storage.split()[0]}")
                    products.append((model, variant, sku,
                        laptop_desc("LOQ", model, cpu, ram, storage, display,
                                    "Windows 11 Home", "gaming and everyday computing", "Standard"),
                        {"series":"LOQ","processor":cpu,"ram":ram,"storage":storage,
                         "display_size":display,"display_type":"IPS 144Hz",
                         "graphics":"NVIDIA GeForce RTX 3050/4050",
                         "operating_system":"Windows 11 Home","battery_life":"5","weight_kg":"2.20",
                         "price_tier":"Standard","nigeria_market_price_naira":str(price)}))

    # ── IdeaPad ───────────────────────────────────────────────────────────────
    for model, display in [
        ("IdeaPad 3 14",     "14"),   ("IdeaPad 3 15",     "15.6"),
        ("IdeaPad 5 14",     "14"),   ("IdeaPad 5 15",     "15.6"),
        ("IdeaPad Slim 5 14","14"),   ("IdeaPad Slim 5 16","16"),
        ("IdeaPad Slim 3 15","15.6"),
    ]:
        for cpu in ["Intel Core i3-1215U", "Intel Core i5-1235U", "AMD Ryzen 5 5500U"]:
            for ram in ["8GB DDR4", "16GB DDR4"]:
                for storage in ["256GB SSD", "512GB SSD"]:
                    price = {"8GB DDR4":165000,"16GB DDR4":240000}[ram]
                    price += {"256GB SSD":0,"512GB SSD":22000}[storage]
                    price += {"Intel Core i3-1215U":0,"Intel Core i5-1235U":42000,"AMD Ryzen 5 5500U":38000}[cpu]
                    variant = f"{ram} {storage}"
                    sku = slugify(f"lenovo_{model}_{cpu.split()[-1]}_{ram.split()[0]}_{storage.split()[0]}")
                    products.append((model, variant, sku,
                        laptop_desc("IdeaPad", model, cpu, ram, storage, display,
                                    "Windows 11 Home", "students and home users", "Budget"),
                        {"series":"IdeaPad","processor":cpu,"ram":ram,"storage":storage,
                         "display_size":display,"display_type":"IPS","graphics":"Intel Iris Xe / AMD Radeon",
                         "operating_system":"Windows 11 Home","battery_life":"8","weight_kg":"1.65",
                         "price_tier":"Budget","nigeria_market_price_naira":str(price)}))

    return products


def gen_desktops():
    products = []

    # ── ThinkCentre M-series ──────────────────────────────────────────────────
    for model, form in [
        ("ThinkCentre M70q Gen 3",  "Tiny"),
        ("ThinkCentre M70q Gen 4",  "Tiny"),
        ("ThinkCentre M70s Gen 3",  "Small Form Factor"),
        ("ThinkCentre M70s Gen 4",  "Small Form Factor"),
        ("ThinkCentre M70t Gen 3",  "Tower"),
        ("ThinkCentre M80q Gen 3",  "Tiny"),
        ("ThinkCentre M80s Gen 3",  "Small Form Factor"),
        ("ThinkCentre M80t Gen 3",  "Tower"),
        ("ThinkCentre M90q Gen 3",  "Tiny"),
        ("ThinkCentre M90s Gen 3",  "Small Form Factor"),
        ("ThinkCentre M90t Gen 3",  "Tower"),
        ("ThinkCentre Neo 50q Gen 4","Tiny"),
        ("ThinkCentre Neo 50s Gen 4","Small Form Factor"),
    ]:
        tier = "Business" if "M70" in model or "Neo" in model else "Premium"
        for cpu in ["Intel Core i3-12100", "Intel Core i5-12500", "Intel Core i7-12700"]:
            for ram in ["4GB DDR4", "8GB DDR4", "16GB DDR4", "32GB DDR4"]:
                for storage in ["256GB SSD", "512GB SSD", "1TB HDD + 256GB SSD"]:
                    price = {"4GB DDR4":140000,"8GB DDR4":185000,"16GB DDR4":260000,"32GB DDR4":395000}[ram]
                    price += {"256GB SSD":0,"512GB SSD":23000,"1TB HDD + 256GB SSD":37000}[storage]
                    price += {"Intel Core i3-12100":0,"Intel Core i5-12500":56000,"Intel Core i7-12700":140000}[cpu]
                    if "Tiny" in form:
                        price += 15000
                    variant = f"{ram} {storage} {form}"
                    sku = slugify(f"lenovo_{model}_{cpu.split()[-1]}_{ram.split()[0]}_{storage.split()[0]}")
                    products.append((model, variant, sku,
                        desktop_desc("ThinkCentre", model, cpu, ram, storage, form,
                                     "Windows 11 Pro", "corporate office deployments", tier),
                        {"series":"ThinkCentre","processor":cpu,"ram":ram,"storage":storage,
                         "form_factor":form,"graphics":"Intel UHD 770",
                         "operating_system":"Windows 11 Pro","optical_drive":"None",
                         "price_tier":tier,"nigeria_market_price_naira":str(price)}))

    # ── ThinkStation workstations ──────────────────────────────────────────────
    for model, form, base in [
        ("ThinkStation P3 Tiny",   "Tiny",  680000),
        ("ThinkStation P3 Tower",  "Tower", 820000),
        ("ThinkStation P5",        "Tower", 1600000),
        ("ThinkStation P7",        "Tower", 3200000),
        ("ThinkStation P360 Ultra","Ultra-Small", 900000),
    ]:
        for cpu in ["Intel Core i7-13700", "Intel Xeon W3-2423", "Intel Xeon W5-2445"]:
            for ram in ["16GB DDR5 ECC", "32GB DDR5 ECC", "64GB DDR5 ECC"]:
                for storage in ["512GB SSD", "1TB SSD", "2TB SSD"]:
                    price = base
                    price += {"16GB DDR5 ECC":0,"32GB DDR5 ECC":165000,"64GB DDR5 ECC":390000}[ram]
                    price += {"512GB SSD":0,"1TB SSD":82000,"2TB SSD":195000}[storage]
                    price += {"Intel Core i7-13700":0,"Intel Xeon W3-2423":320000,"Intel Xeon W5-2445":660000}[cpu]
                    variant = f"{ram} {storage}"
                    sku = slugify(f"lenovo_{model}_{cpu.split()[-1]}_{ram.split()[0]}_{storage.split()[0]}")
                    products.append((model, variant, sku,
                        desktop_desc("ThinkStation", model, cpu, ram, storage, form,
                                     "Windows 11 Pro for Workstations", "engineering and creative workloads", "Workstation"),
                        {"series":"ThinkStation","processor":cpu,"ram":ram,"storage":storage,
                         "form_factor":form,"graphics":"NVIDIA RTX A-series / AMD Radeon Pro",
                         "operating_system":"Windows 11 Pro for Workstations","optical_drive":"Optional",
                         "price_tier":"Workstation","nigeria_market_price_naira":str(price)}))

    # ── IdeaCentre & Legion Tower ─────────────────────────────────────────────
    for model, form, base, tier, os_c in [
        ("IdeaCentre 3 07ADA05",    "Tower",     130000, "Budget",  "Windows 11 Home"),
        ("IdeaCentre 5 14IAB7",     "Tower",     190000, "Budget",  "Windows 11 Home"),
        ("IdeaCentre AIO 3 24ALC6", "All-in-One",240000, "Standard","Windows 11 Home"),
        ("IdeaCentre AIO 5 27IAB7", "All-in-One",340000, "Standard","Windows 11 Home"),
        ("Legion Tower 5 Gen 7",    "Tower",     650000, "Premium", "Windows 11 Home"),
        ("Legion Tower 7 Gen 7",    "Tower",     950000, "Premium", "Windows 11 Home"),
    ]:
        for cpu in ["Intel Core i5-12400", "Intel Core i7-12700", "AMD Ryzen 5 5600G"]:
            for ram in ["8GB DDR4", "16GB DDR4", "32GB DDR4"]:
                for storage in ["512GB SSD", "1TB HDD + 256GB SSD", "2TB HDD + 512GB SSD"]:
                    price = base
                    price += {"8GB DDR4":0,"16GB DDR4":60000,"32GB DDR4":150000}[ram]
                    price += {"512GB SSD":0,"1TB HDD + 256GB SSD":35000,"2TB HDD + 512GB SSD":90000}[storage]
                    price += {"Intel Core i5-12400":0,"Intel Core i7-12700":115000,"AMD Ryzen 5 5600G":45000}[cpu]
                    variant = f"{ram} {storage}"
                    sku = slugify(f"lenovo_{model}_{cpu.split()[-1]}_{ram.split()[0]}_{storage.split()[0]}")
                    series = "Legion" if "Legion" in model else "IdeaCentre"
                    products.append((model, variant, sku,
                        desktop_desc(series, model, cpu, ram, storage, form,
                                     os_c, "home and everyday use", tier),
                        {"series":series,"processor":cpu,"ram":ram,"storage":storage,
                         "form_factor":form,"graphics":"NVIDIA GeForce / Intel UHD / AMD Radeon",
                         "operating_system":os_c,"optical_drive":"None",
                         "price_tier":tier,"nigeria_market_price_naira":str(price)}))

    return products


def gen_servers():
    products = []

    configs = [
        # (model, form_factor, drive_bays, max_ram, net_ports, base_price)
        ("ThinkSystem SR250 V2",   "1U Rack", "4 LFF",  "2TB",  "2x 1GbE",  1050000),
        ("ThinkSystem SR250 V3",   "1U Rack", "4 LFF",  "2TB",  "2x 1GbE",  1150000),
        ("ThinkSystem SR530",      "1U Rack", "8 SFF",  "3TB",  "4x 1GbE",  2500000),
        ("ThinkSystem SR550",      "2U Rack", "12 LFF", "3TB",  "4x 1GbE",  3100000),
        ("ThinkSystem SR630 V2",   "1U Rack", "8 SFF",  "4TB",  "4x 10GbE", 4500000),
        ("ThinkSystem SR630 V3",   "1U Rack", "8 SFF",  "8TB",  "4x 10GbE", 5000000),
        ("ThinkSystem SR650 V2",   "2U Rack", "24 SFF", "4TB",  "4x 10GbE", 5500000),
        ("ThinkSystem SR650 V3",   "2U Rack", "24 SFF", "8TB",  "4x 10GbE", 6200000),
        ("ThinkSystem SR850 V3",   "2U Rack", "24 SFF", "12TB", "4x 25GbE",10000000),
        ("ThinkSystem SR950 V3",   "4U Rack", "24 SFF", "24TB", "4x 25GbE",15000000),
        ("ThinkSystem ST250 V2",   "Tower",   "4 LFF",  "2TB",  "2x 1GbE",   980000),
        ("ThinkSystem ST250 V3",   "Tower",   "4 LFF",  "2TB",  "2x 1GbE",  1080000),
        ("ThinkSystem ST550",      "Tower",   "16 LFF", "3TB",  "4x 1GbE",  3300000),
        ("ThinkSystem ST650 V3",   "Tower",   "16 LFF", "8TB",  "4x 10GbE", 5800000),
    ]

    cpus_by_tier = {
        "Entry":           ["Intel Xeon E-2314","Intel Xeon E-2336","Intel Xeon E-2356G"],
        "Mid-range":       ["Intel Xeon Silver 4310","Intel Xeon Silver 4314","Intel Xeon Gold 5315Y"],
        "Enterprise":      ["Intel Xeon Gold 6330","Intel Xeon Gold 6348","Intel Xeon Platinum 8352V"],
        "Mission-Critical":["Intel Xeon Platinum 8358","Intel Xeon Platinum 8380"],
    }
    ram_by_tier = {
        "Entry":           ["16GB DDR4 ECC","32GB DDR4 ECC"],
        "Mid-range":       ["32GB DDR4 ECC","64GB DDR4 ECC"],
        "Enterprise":      ["64GB DDR4 ECC","128GB DDR4 ECC","256GB DDR4 ECC"],
        "Mission-Critical":["256GB DDR4 ECC","512GB DDR4 ECC"],
    }
    storage_opts = ["1x 1.2TB SAS HDD","2x 1.2TB SAS HDD","2x 960GB SATA SSD",
                    "4x 1.2TB SAS HDD","2x 1.92TB SATA SSD","4x 960GB SATA SSD"]
    cpu_price = {
        "Intel Xeon E-2314":0,"Intel Xeon E-2336":65000,"Intel Xeon E-2356G":190000,
        "Intel Xeon Silver 4310":240000,"Intel Xeon Silver 4314":340000,
        "Intel Xeon Gold 5315Y":640000,"Intel Xeon Gold 6330":980000,
        "Intel Xeon Gold 6348":1650000,"Intel Xeon Platinum 8352V":3100000,
        "Intel Xeon Platinum 8358":3900000,"Intel Xeon Platinum 8380":5100000,
    }
    ram_price = {
        "16GB DDR4 ECC":0,"32GB DDR4 ECC":105000,"64GB DDR4 ECC":250000,
        "128GB DDR4 ECC":560000,"256GB DDR4 ECC":1250000,"512GB DDR4 ECC":2700000,
    }
    stor_price = {
        "1x 1.2TB SAS HDD":0,"2x 1.2TB SAS HDD":72000,"2x 960GB SATA SSD":185000,
        "4x 1.2TB SAS HDD":135000,"2x 1.92TB SATA SSD":450000,"4x 960GB SATA SSD":350000,
    }

    for (model, form, bays, max_r, ports, base) in configs:
        tier = ("Entry" if base < 1500000 else
                "Mid-range" if base < 4000000 else
                "Enterprise" if base < 8000000 else "Mission-Critical")
        for cpu in cpus_by_tier[tier]:
            for ram in ram_by_tier[tier]:
                for storage in storage_opts:
                    price = base + cpu_price[cpu] + ram_price[ram] + stor_price[storage]
                    variant = f"{ram} {storage}"
                    stor_tag = "_".join(storage.split()[:2])
                    sku = slugify(f"lenovo_{model}_{cpu.split()[-1]}_{ram.split()[0]}_{stor_tag}")
                    products.append((model, variant, sku,
                        server_desc("ThinkSystem", model, cpu, ram, storage, form, bays, max_r, ports),
                        {"series":"ThinkSystem","processor":cpu,"ram":ram,"storage":storage,
                         "form_factor":form,"drive_bays":bays,"max_ram":max_r,
                         "network_ports":ports,"price_tier":tier,
                         "nigeria_market_price_naira":str(price)}))

    return products


def gen_thin_clients():
    products = []

    tc_configs = [
        # (model, series, cpu, form, base_price)
        ("ThinkCentre M70q Tiny TC",     "ThinkCentre Tiny","Intel Core i3-10100T Quad-core",  "Tiny",    145000),
        ("ThinkCentre M80q Tiny TC",     "ThinkCentre Tiny","Intel Core i5-10500T Hexa-core",  "Tiny",    200000),
        ("ThinkCentre M90q Tiny TC",     "ThinkCentre Tiny","Intel Core i7-10700T Octa-core",  "Tiny",    280000),
        ("ThinkCentre Neo 50q Tiny TC",  "ThinkCentre Tiny","Intel Core i5-12500T Hexa-core",  "Tiny",    230000),
        ("ThinkEdge SE30",               "ThinkEdge",       "Intel Atom x6413E Quad-core",     "Ultra Compact",95000),
        ("ThinkEdge SE50",               "ThinkEdge",       "Intel Core i5-10210U Quad-core",  "Ultra Compact",175000),
        ("ThinkEdge SE70",               "ThinkEdge",       "Intel Core i7-10510U Quad-core",  "Ultra Compact",250000),
        ("ThinkCentre Tiny-in-One 27",   "ThinkCentre AiO", "Intel Core i5-12500T Hexa-core",  "AiO",     320000),
        ("ThinkCentre Tiny-in-One 24",   "ThinkCentre AiO", "Intel Core i3-12100T Quad-core",  "AiO",     240000),
        ("ThinkCentre M70a AiO TC",      "ThinkCentre AiO", "Intel Core i5-12500T Hexa-core",  "AiO",     295000),
        ("ThinkStation P360 Tiny TC",    "ThinkStation",    "Intel Core i9-12900T 16-core",    "Tiny",    480000),
        ("ThinkEdge SE450",              "ThinkEdge",       "Intel Core i7-1185G7E Quad-core", "Compact", 340000),
    ]

    ram_opts     = ["4GB DDR4","8GB DDR4","16GB DDR4"]
    storage_opts = ["16GB eMMC","32GB eMMC","64GB eMMC","128GB SSD","256GB SSD"]
    os_opts      = ["Lenovo ThinOS","Windows 10 IoT Enterprise","Windows 11 IoT Enterprise","Ubuntu 22.04 LTS"]
    disp_opts    = ["Single 4K Display","Dual FHD Displays","Triple FHD Displays"]
    use_cases    = {
        "ThinkCentre Tiny":"virtual desktop and cloud computing",
        "ThinkEdge":       "edge computing and VDI",
        "ThinkCentre AiO": "space-efficient all-in-one VDI",
        "ThinkStation":    "high-performance thin client workstation",
    }
    ram_price  = {"4GB DDR4":0,"8GB DDR4":22000,"16GB DDR4":52000}
    stor_price = {"16GB eMMC":0,"32GB eMMC":7000,"64GB eMMC":15000,"128GB SSD":30000,"256GB SSD":62000}
    os_price   = {"Lenovo ThinOS":0,"Windows 10 IoT Enterprise":22000,
                  "Windows 11 IoT Enterprise":27000,"Ubuntu 22.04 LTS":0}

    for (model, series, cpu, form, base) in tc_configs:
        tier = "Entry" if base < 160000 else "Standard" if base < 300000 else "Advanced"
        r_opts = ram_opts[:2] if tier == "Entry" else ram_opts
        s_opts = storage_opts[:3] if tier == "Entry" else storage_opts[1:4]
        for ram in r_opts:
            for storage in s_opts:
                for os in os_opts:
                    price = base + ram_price[ram] + stor_price[storage] + os_price[os]
                    variant = f"{ram} {storage} {os}"
                    os_tag = slugify(os)[:12]
                    sku = slugify(f"lenovo_{model}_{ram.split()[0]}_{storage.replace(' ','_')}_{os_tag}")
                    products.append((model, variant, sku,
                        thin_client_desc(series, model, cpu, ram, storage, os, form,
                                         use_cases.get(series, "VDI")),
                        {"series":series,"processor":cpu,"ram":ram,"storage":storage,
                         "operating_system":os,"display_support":disp_opts[0],
                         "form_factor":form,"price_tier":tier,
                         "nigeria_market_price_naira":str(price)}))

    return products


def gen_monitors():
    products = []

    STAND_VARIANTS = [
        ("Standard Stand",   0,     "std"),
        ("Height-Adj Stand", 14000, "has"),
        ("VESA Mount Only",  -7000, "vesa"),
    ]
    COLOR_VARIANTS = [
        ("Raven Black",  0,    "blk"),
        ("Cloud Grey",   5000, "gry"),
        ("Tundra White", 5000, "wht"),
    ]

    # ── ThinkVision T-series (business) ───────────────────────────────────────
    t_series = [
        ("T22i-30",  "ThinkVision T","21.5","FHD",   "IPS","60Hz","4","VGA+HDMI+DP",         82000,"office productivity","Entry"),
        ("T23i-30",  "ThinkVision T","23.8","FHD",   "IPS","60Hz","4","VGA+HDMI+DP",         98000,"office productivity","Entry"),
        ("T23i-30",  "ThinkVision T","23.8","FHD",   "IPS","75Hz","4","VGA+HDMI+DP",        104000,"office productivity","Entry"),
        ("T24i-30",  "ThinkVision T","23.8","FHD",   "IPS","60Hz","4","HDMI+DP+USB-C 65W",  115000,"office productivity","Standard"),
        ("T24h-30",  "ThinkVision T","23.8","QHD",   "IPS","75Hz","4","HDMI+DP+USB-C 65W",  155000,"office productivity","Standard"),
        ("T27h-30",  "ThinkVision T","27",  "QHD",   "IPS","75Hz","4","HDMI+DP+USB-C 65W",  195000,"office productivity","Standard"),
        ("T27i-30",  "ThinkVision T","27",  "FHD",   "IPS","60Hz","4","VGA+HDMI+DP",        140000,"office productivity","Standard"),
        ("T27p-30",  "ThinkVision T","27",  "4K UHD","IPS","60Hz","4","HDMI+DP+USB-C 90W",  265000,"office productivity","Professional"),
        ("T32h-30",  "ThinkVision T","31.5","QHD",   "IPS","75Hz","4","HDMI+DP+USB-C 90W",  310000,"office productivity","Professional"),
        ("T32p-30",  "ThinkVision T","31.5","4K UHD","IPS","60Hz","4","HDMI+DP+USB-C 90W",  390000,"office productivity","Professional"),
        ("T34w-30",  "ThinkVision T","34",  "WQHD",  "IPS","60Hz","4","HDMI+DP+USB-C 90W",  430000,"widescreen office", "Professional"),
        ("T40p",     "ThinkVision T","39.7","5K2K",  "IPS","60Hz","4","TB3+HDMI+DP+USB-C",  720000,"workstation design","Workstation"),
    ]

    # ── ThinkVision P-series (performance) ────────────────────────────────────
    p_series = [
        ("P24h-30",  "ThinkVision P","23.8","QHD",   "IPS","75Hz","4","HDMI+DP+USB-C 65W",  170000,"professional use","Standard"),
        ("P24q-30",  "ThinkVision P","23.8","QHD",   "IPS","75Hz","4","HDMI+DP+USB-C 65W",  175000,"professional use","Standard"),
        ("P27h-30",  "ThinkVision P","27",  "QHD",   "IPS","75Hz","4","HDMI+DP+USB-C 65W",  218000,"professional use","Standard"),
        ("P27q-30",  "ThinkVision P","27",  "QHD",   "IPS","75Hz","4","HDMI+DP+USB-C 65W",  225000,"professional use","Standard"),
        ("P27u-30",  "ThinkVision P","27",  "4K UHD","IPS","60Hz","4","TB3+HDMI+DP+USB-C",  320000,"professional use","Professional"),
        ("P32u-30",  "ThinkVision P","31.5","4K UHD","IPS","60Hz","4","TB3+HDMI+DP+USB-C",  420000,"professional use","Professional"),
        ("P34w-30",  "ThinkVision P","34",  "WQHD",  "IPS","60Hz","4","TB3+HDMI+DP+USB-C",  490000,"professional use","Professional"),
        ("P40w-30",  "ThinkVision P","39.7","WUHD",  "IPS","72Hz","4","TB4+HDMI+DP+USB-C",  680000,"professional use","Workstation"),
        ("P32p-30",  "ThinkVision P","31.5","4K UHD","IPS","60Hz","4","TB3+HDMI+DP+USB-C",  435000,"workstation design","Professional"),
        ("P27h-30b", "ThinkVision P","27",  "FHD",   "IPS","75Hz","4","HDMI+DP+USB-C 65W",  185000,"professional use","Standard"),
    ]

    # ── ThinkVision S-series & L-series (standard) ───────────────────────────
    sl_series = [
        ("S22e-20",  "ThinkVision S","21.5","FHD",   "VA", "75Hz","4","VGA+HDMI",             58000,"home office","Entry"),
        ("S24e-20",  "ThinkVision S","23.8","FHD",   "VA", "75Hz","4","VGA+HDMI",             72000,"home office","Entry"),
        ("S27e-20",  "ThinkVision S","27",  "FHD",   "IPS","75Hz","4","VGA+HDMI",             90000,"home office","Entry"),
        ("S24i-10",  "ThinkVision S","23.8","FHD",   "IPS","60Hz","4","VGA+HDMI+DP",          85000,"home office","Entry"),
        ("S27i-10",  "ThinkVision S","27",  "FHD",   "IPS","75Hz","4","HDMI+DP",             108000,"home office","Standard"),
        ("S27q-30",  "ThinkVision S","27",  "QHD",   "IPS","75Hz","4","HDMI+DP+USB-C 65W",   165000,"home office","Standard"),
        ("S28u-10",  "ThinkVision S","27.9","4K UHD","IPS","60Hz","4","HDMI+DP+USB-C",       215000,"home office","Standard"),
        ("L22e-30",  "ThinkVision L","21.5","FHD",   "VA", "75Hz","4","VGA+HDMI",             55000,"home office","Entry"),
        ("L24e-30",  "ThinkVision L","23.8","FHD",   "VA", "75Hz","4","VGA+HDMI",             68000,"home office","Entry"),
        ("L27e-30",  "ThinkVision L","27",  "FHD",   "IPS","75Hz","4","VGA+HDMI",             85000,"home office","Entry"),
        ("L24i-30",  "ThinkVision L","23.8","FHD",   "IPS","60Hz","4","HDMI+DP",              78000,"home office","Entry"),
        ("L27i-30",  "ThinkVision L","27",  "FHD",   "IPS","75Hz","4","HDMI+DP",             100000,"home office","Standard"),
    ]

    # ── ThinkVision Creator / Gaming ─────────────────────────────────────────
    gaming_series = [
        ("G24e-30",  "ThinkVision G","23.8","FHD",   "VA", "165Hz","1","HDMI 2.0+DP 1.4",    148000,"gaming","Standard"),
        ("G25-30",   "ThinkVision G","24.5","FHD",   "IPS","165Hz","1","HDMI 2.0+DP 1.4",    162000,"gaming","Standard"),
        ("G27e-30",  "ThinkVision G","27",  "FHD",   "VA", "165Hz","1","HDMI 2.0+DP 1.4",    185000,"gaming","Standard"),
        ("G27-30",   "ThinkVision G","27",  "FHD",   "IPS","165Hz","1","HDMI 2.0+DP 1.4",    195000,"gaming","Standard"),
        ("G27q-30",  "ThinkVision G","27",  "QHD",   "IPS","165Hz","1","HDMI 2.1+DP 1.4",    255000,"gaming","Standard"),
        ("G32qc-30", "ThinkVision G","31.5","QHD",   "VA", "165Hz","1","HDMI 2.0+DP 1.4",    325000,"gaming","Standard"),
        ("G34w-30",  "ThinkVision G","34",  "UWQHD", "VA", "144Hz","1","HDMI 2.0+DP 1.4",    395000,"ultrawide gaming","Standard"),
        ("G27c-30",  "ThinkVision G","27",  "FHD",   "VA", "165Hz","1","HDMI 2.0+DP 1.4",    188000,"gaming","Standard"),
        ("G24-30",   "ThinkVision G","23.8","FHD",   "IPS","144Hz","1","HDMI 2.0+DP 1.4",    142000,"gaming","Entry"),
        ("G27-20",   "ThinkVision G","27",  "QHD",   "IPS","144Hz","1","HDMI 2.0+DP 1.4",    240000,"gaming","Standard"),
        ("G34wq-30", "ThinkVision G","34",  "UWQHD", "IPS","144Hz","1","HDMI 2.1+DP 1.4+USB-C",420000,"ultrawide gaming","Premium"),
        ("G27u-30",  "ThinkVision G","27",  "4K UHD","IPS","144Hz","1","HDMI 2.1+DP 1.4+USB-C",430000,"gaming","Premium"),
    ]

    def add_p(series_name, model, size, res, panel, refresh, resp, conn,
              base, use_case, tier, suffix="", delta=0):
        price = base + delta
        variant = f"{size}\" {res} {panel} {refresh}{' '+suffix if suffix else ''}"
        sku = slugify(f"lenovo_{model}_{size}_{res}_{panel}_{refresh}{'_'+slugify(suffix) if suffix else ''}")
        desc = monitor_desc(series_name, model, size, res, panel, refresh, resp, conn, use_case, tier)
        products.append((model, variant, sku, desc, {
            "series": series_name, "screen_size": size, "resolution": res,
            "panel_type": panel, "refresh_rate": refresh, "response_time": resp,
            "connectivity": conn, "price_tier": tier,
            "nigeria_market_price_naira": str(price),
        }))

    # T, P, S/L series: 3 stand variants each
    for (model, series, size, res, panel, refresh, resp, conn, base, use_case, tier) in (
            t_series + p_series + sl_series):
        for (slabel, sdelta, _) in STAND_VARIANTS:
            add_p(series, model, size, res, panel, refresh, resp, conn,
                  base, use_case, tier, suffix=slabel, delta=sdelta)

    # Gaming series: 3 color variants
    for (model, series, size, res, panel, refresh, resp, conn, base, use_case, tier) in gaming_series:
        for (clabel, cdelta, _) in COLOR_VARIANTS:
            add_p(series, model, size, res, panel, refresh, resp, conn,
                  base, use_case, tier, suffix=clabel, delta=cdelta)

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
        for (model_name, variant, sku, desc, attrs) in products[i:i + BATCH]:
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
                if not val:
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
        print(f"  [{cat_name}] {min(i+BATCH,total)}/{total}", end="\r", flush=True)
    print(f"\n  [{cat_name}] inserted {inserted} products, {attr_ins} attribute values")
    return inserted


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

GENERATORS = {
    "Lenovo Laptops":      gen_laptops,
    "Lenovo Desktops":     gen_desktops,
    "Lenovo Servers":      gen_servers,
    "Lenovo Thin Clients": gen_thin_clients,
    "Lenovo Monitors":     gen_monitors,
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

    cur.execute("""
        SELECT cc.name, COUNT(cp.id) AS cnt
        FROM catalogue_categories cc
        LEFT JOIN catalogue_products cp ON cp.category_id = cc.id AND cp.is_active
        WHERE cc.slug LIKE 'lenovo-%'
        GROUP BY cc.name ORDER BY cc.name
    """)
    print("\n── Final counts ─────────────────────────")
    total_check = 0
    for row in cur.fetchall():
        print(f"  {row['name']}: {row['cnt']}")
        total_check += row['cnt']
    print(f"  TOTAL LENOVO PRODUCTS: {total_check}")
    cur.close(); conn.close()
    print("Done.")


if __name__ == "__main__":
    run()
