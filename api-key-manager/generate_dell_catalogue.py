"""
Generate ~3,000 Dell business products across 5 categories:
  Dell Laptops (~950), Dell Desktops (~650), Dell Servers (~500),
  Dell Thin Clients (~400), Dell Monitors (~500)

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
BRAND = "Dell"


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


# ─────────────────────────────────────────────────────────────────────────────
# CATEGORY + ATTRIBUTE DEFINITIONS
# ─────────────────────────────────────────────────────────────────────────────

CATEGORIES = [
    {
        "name": "Dell Laptops",
        "slug": "dell-laptops",
        "icon": "💻",
        "description": "Dell business and consumer laptops — Latitude, Vostro, XPS, Inspiron, Precision, Alienware and G-series",
        "sort_order": 7,
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
        "name": "Dell Desktops",
        "slug": "dell-desktops",
        "icon": "🖥️",
        "description": "Dell business and workstation desktops — OptiPlex, Vostro, XPS and Precision Tower series",
        "sort_order": 8,
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
        "name": "Dell Servers",
        "slug": "dell-servers",
        "icon": "🖧",
        "description": "Dell PowerEdge rack and tower servers for business data centres and SME infrastructure",
        "sort_order": 9,
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
        "name": "Dell Thin Clients",
        "slug": "dell-thin-clients",
        "icon": "🖱️",
        "description": "Dell Wyse and OptiPlex thin clients for virtual desktop infrastructure and cloud environments",
        "sort_order": 10,
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
        "name": "Dell Monitors",
        "slug": "dell-monitors",
        "icon": "🖥",
        "description": "Dell UltraSharp, Professional, Standard, Gaming and Alienware monitors for every workspace",
        "sort_order": 11,
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
        f"The Dell {model} is a {tier.lower()} laptop engineered for {use_case}. "
        f"Powered by an {cpu} with {ram} of memory and {storage} storage, it delivers "
        f"the performance needed for demanding business workloads. The {display}-inch "
        f"display provides sharp, comfortable visuals for extended work sessions. "
        f"Running {os}, it integrates seamlessly into enterprise IT environments and "
        f"supports Dell's ProSupport services for rapid business-day response.",

        f"Designed for {use_case} professionals, the Dell {model} combines an {cpu} "
        f"processor with {ram} RAM and {storage} SSD for smooth multitasking. "
        f"The {display}-inch anti-glare display keeps productivity high in any lighting "
        f"condition. With {os} and Dell Optimizer AI software, performance is "
        f"automatically tuned to your work style. Built-in security features include "
        f"Dell SafeGuard and Response for enterprise-grade endpoint protection.",

        f"The Dell {series} {model} delivers reliable {use_case} performance in a "
        f"durable, professional chassis. Its {cpu} paired with {ram} RAM and {storage} "
        f"storage ensures fast application launches and responsive multitasking. "
        f"The {display}-inch screen and {os} platform make it a dependable daily driver "
        f"for offices, remote teams and field workers. Dell's global warranty and "
        f"certified service network add peace of mind for IT departments.",

        f"Built to meet the rigours of {use_case}, the Dell {model} features an {cpu}, "
        f"{ram} RAM and {storage} storage. The {display}-inch display supports comfortable "
        f"all-day computing, and {os} ensures compatibility with enterprise software. "
        f"Dell's ExpressConnect automatically switches to the strongest WiFi signal, "
        f"while the MIL-STD-810H tested build quality withstands the demands of "
        f"business travel and everyday office use.",
    ]
    return descs[hash(model + cpu + ram + storage) % len(descs)]


def desktop_desc(series, model, cpu, ram, storage, form, os, use_case, tier):
    descs = [
        f"The Dell {model} is a {form} desktop built for {use_case}. "
        f"Powered by an {cpu} with {ram} of memory and {storage} storage, it handles "
        f"business applications, ERP systems and multitasking with ease. Running {os}, "
        f"it supports enterprise deployment via Dell's commercial management tools. "
        f"The {form} design saves valuable desk space while delivering full desktop "
        f"performance for day-to-day business operations.",

        f"Engineered for {use_case} environments, the Dell {model} {form} desktop "
        f"combines an {cpu} with {ram} RAM and {storage} to power productivity "
        f"applications, collaboration tools and data entry tasks. The {os} platform "
        f"ensures broad software compatibility and enterprise security compliance. "
        f"Dell's Trusted Device feature verifies BIOS integrity on every boot, "
        f"providing an additional layer of hardware-level security.",

        f"The Dell {series} {model} offers dependable {use_case} performance in a "
        f"{form} package. Its {cpu} processor, {ram} memory and {storage} deliver "
        f"consistent speed for all business workloads. {os} provides a stable, "
        f"secure foundation while Dell Command | Update keeps drivers and firmware "
        f"current automatically. The compact {form} chassis fits any workspace "
        f"and supports VESA mounting for flexible deployment options.",

        f"Designed for business efficiency, the Dell {model} {form} desktop is "
        f"equipped with an {cpu}, {ram} RAM and {storage} to power through {use_case} "
        f"demands. The {os} environment supports a wide range of enterprise applications "
        f"and Dell's security suite protects endpoints from modern threats. "
        f"Its energy-efficient design meets ENERGY STAR certification, reducing "
        f"operational costs over the product lifecycle.",
    ]
    return descs[hash(model + cpu + ram + storage) % len(descs)]


def server_desc(series, model, cpu, ram, storage, form, bays, max_r, ports):
    descs = [
        f"The Dell {model} is a {form} server built for reliable business infrastructure. "
        f"Powered by {cpu} with {ram} of ECC memory and {storage} storage, it handles "
        f"virtualisation, databases and business-critical workloads. Supporting up to "
        f"{max_r} maximum RAM across {bays} drive bays, it scales with your business. "
        f"{ports} network connectivity ensures high-throughput data access. "
        f"iDRAC9 remote management enables full server lifecycle management without "
        f"requiring physical access to the data centre.",

        f"The Dell PowerEdge {model} delivers enterprise-grade reliability in a {form} "
        f"form factor. Featuring {cpu}, {ram} RAM and {storage}, it is optimised for "
        f"web hosting, file services, ERP and virtualisation workloads. With {bays} "
        f"drive bays expandable to {max_r} RAM, it grows with your infrastructure. "
        f"{ports} provide fast, redundant connectivity. Dell's OpenManage suite "
        f"simplifies deployment, monitoring and maintenance across your server fleet.",

        f"Built to power SME and enterprise data centres, the Dell {model} {form} server "
        f"offers {cpu} processing, {ram} ECC memory and {storage} storage. Designed "
        f"for 24/7 operation with redundant power supply support, it includes iDRAC9 "
        f"remote management, {bays} drive bays and capacity for up to {max_r} RAM. "
        f"{ports} network ports support high-speed connectivity for demanding workloads. "
        f"Dell ProSupport ensures rapid response for business-critical deployments.",

        f"The Dell PowerEdge {model} is engineered for demanding {form} deployments. "
        f"Starting with {ram} RAM and {storage}, it supports virtualisation, analytics "
        f"and cloud workloads powered by {cpu}. Scale storage across {bays} bays and "
        f"memory up to {max_r}. With {ports} and Dell's comprehensive OpenManage "
        f"ecosystem, this server is a trusted backbone for modern business operations.",
    ]
    return descs[hash(model + cpu + ram + storage) % len(descs)]


def thin_client_desc(series, model, cpu, ram, storage, os, form, use_case):
    descs = [
        f"The Dell {model} is a {form} thin client designed for {use_case} environments. "
        f"Powered by {cpu} with {ram} RAM and {storage} storage, it delivers responsive "
        f"virtual desktop access with minimal power consumption. Running {os}, it is "
        f"optimised for Citrix, VMware Horizon and Microsoft RDS deployments. "
        f"Dell Wyse Management Suite enables centralised policy management and "
        f"zero-touch provisioning across large-scale deployments.",

        f"Built for modern {use_case} computing, the Dell {model} thin client offers "
        f"{cpu} performance, {ram} of memory and {storage} flash storage in a compact "
        f"{form} design. {os} provides a secure, locked-down environment ideal for "
        f"healthcare, finance, education and call-centre deployments. Low power draw "
        f"and fanless operation suit noise-sensitive or space-constrained environments. "
        f"Dell's Device-as-a-Service option simplifies acquisition and refresh cycles.",

        f"The Dell {series} {model} delivers reliable virtual desktop access for "
        f"{use_case} users. With {cpu}, {ram} RAM and {storage}, it provides smooth "
        f"performance for cloud-hosted applications and VDI sessions. Its {form} "
        f"design and {os} platform support easy mass deployment and remote management "
        f"through Dell Wyse Management Suite. Built-in security features protect "
        f"sensitive data at the endpoint level.",

        f"Ideal for shared workspaces and {use_case} deployments, the Dell {model} "
        f"combines {cpu} processing with {ram} RAM and {storage} in a {form} chassis. "
        f"{os} enables secure, centralised computing with minimal local data risk. "
        f"Dell's comprehensive thin client ecosystem includes management software, "
        f"accessories and global support to simplify large-scale rollouts for "
        f"IT administrators.",
    ]
    return descs[hash(model + cpu + ram + storage) % len(descs)]


def monitor_desc(series, model, size, res, panel, refresh, resp, conn, use_case, tier):
    descs = [
        f"The Dell {model} is a {size}-inch {panel} monitor delivering {res} resolution "
        f"for {use_case} professionals. With a {refresh} refresh rate and {resp}ms "
        f"response time, it provides smooth, accurate visuals for productivity and "
        f"creative work. {conn} connectivity ensures broad compatibility with modern "
        f"laptops, desktops and docking stations. Dell's three-year Advanced Exchange "
        f"warranty and Premium Panel Guarantee deliver confidence in every purchase.",

        f"Designed for {use_case} environments, the Dell {model} offers a {size}-inch "
        f"{res} {panel} panel with {refresh} refresh rate and {resp}ms response time. "
        f"Its slim bezel design supports multi-monitor setups, while {conn} ports "
        f"provide flexible connectivity. ComfortView Plus technology reduces harmful "
        f"blue light without compromising colour accuracy, protecting eye health "
        f"during long work sessions.",

        f"The Dell {series} {model} brings {res} clarity to a {size}-inch {panel} "
        f"display, making it ideal for {use_case}. A {refresh} refresh rate ensures "
        f"smooth rendering, while {resp}ms response time keeps motion crisp. "
        f"{conn} connectivity and 100mm VESA compatibility support flexible desk "
        f"configurations. Factory colour calibration at Delta-E less than 2 ensures "
        f"consistent, accurate colour reproduction straight from the box.",

        f"Built to enhance {use_case} productivity, the Dell {model} features a "
        f"{size}-inch {panel} screen with {res} resolution, {refresh} refresh rate "
        f"and {resp}ms response time. {conn} connection options support the latest "
        f"docking solutions, and the anti-glare coating ensures comfortable viewing "
        f"under office lighting. Dell's Premium Panel Guarantee means free replacement "
        f"if even a single bright pixel appears during the warranty period.",
    ]
    return descs[hash(model + res + refresh + panel) % len(descs)]


# ─────────────────────────────────────────────────────────────────────────────
# PRODUCT GENERATORS
# ─────────────────────────────────────────────────────────────────────────────

def gen_laptops():
    products = []

    # ── Latitude 3000 series ──────────────────────────────────────────────────
    for model, display in [
        ("Latitude 3340", "13.3"), ("Latitude 3440", "14"), ("Latitude 3540", "15.6"),
        ("Latitude 3330", "13.3"), ("Latitude 3430", "14"), ("Latitude 3530", "15.6"),
    ]:
        for cpu in ["Intel Core i3-1315U", "Intel Core i5-1335U", "Intel Core i7-1355U"]:
            for ram in ["8GB DDR4", "16GB DDR4", "32GB DDR4"]:
                for storage in ["256GB SSD", "512GB SSD", "1TB SSD"]:
                    price = {"8GB DDR4":300000,"16GB DDR4":400000,"32GB DDR4":560000}[ram]
                    price += {"256GB SSD":0,"512GB SSD":28000,"1TB SSD":65000}[storage]
                    price += {"Intel Core i3-1315U":0,"Intel Core i5-1335U":55000,"Intel Core i7-1355U":130000}[cpu]
                    variant = f"{ram} {storage}"
                    sku = slugify(f"dell_{model}_{cpu.split()[-1]}_{ram.split()[0]}_{storage.split()[0]}")
                    products.append((model, variant, sku,
                        laptop_desc("Latitude", model, cpu, ram, storage, display, "Windows 11 Pro", "business productivity", "Business"),
                        {"series":"Latitude","processor":cpu,"ram":ram,"storage":storage,
                         "display_size":display,"display_type":"IPS","graphics":"Intel Iris Xe",
                         "operating_system":"Windows 11 Pro","battery_life":"10","weight_kg":"1.52",
                         "price_tier":"Business","nigeria_market_price_naira":str(price)}))

    # ── Latitude 5000 series ──────────────────────────────────────────────────
    for model, display in [
        ("Latitude 5340", "13.3"), ("Latitude 5440", "14"), ("Latitude 5540", "15.6"),
        ("Latitude 5350", "13.3"), ("Latitude 5450", "14"), ("Latitude 5550", "15.6"),
    ]:
        for cpu in ["Intel Core i5-1345U", "Intel Core i7-1365U", "Intel Core Ultra 5 135U"]:
            for ram in ["8GB DDR5", "16GB DDR5", "32GB DDR5"]:
                for storage in ["256GB SSD", "512GB SSD", "1TB SSD"]:
                    price = {"8GB DDR5":420000,"16GB DDR5":560000,"32GB DDR5":780000}[ram]
                    price += {"256GB SSD":0,"512GB SSD":30000,"1TB SSD":70000}[storage]
                    price += {"Intel Core i5-1345U":0,"Intel Core i7-1365U":120000,"Intel Core Ultra 5 135U":160000}[cpu]
                    variant = f"{ram} {storage}"
                    sku = slugify(f"dell_{model}_{cpu.split()[-1]}_{ram.split()[0]}_{storage.split()[0]}")
                    products.append((model, variant, sku,
                        laptop_desc("Latitude", model, cpu, ram, storage, display, "Windows 11 Pro", "enterprise professionals", "Premium"),
                        {"series":"Latitude","processor":cpu,"ram":ram,"storage":storage,
                         "display_size":display,"display_type":"IPS","graphics":"Intel Iris Xe",
                         "operating_system":"Windows 11 Pro","battery_life":"12","weight_kg":"1.37",
                         "price_tier":"Premium","nigeria_market_price_naira":str(price)}))

    # ── Latitude 7000 series ──────────────────────────────────────────────────
    for model, display, weight in [
        ("Latitude 7340", "13.3", "1.10"), ("Latitude 7440", "14", "1.17"),
        ("Latitude 7640", "16",   "1.72"), ("Latitude 7350", "13.3","1.15"),
        ("Latitude 7450", "14",   "1.19"),
    ]:
        for cpu in ["Intel Core i5-1345U", "Intel Core i7-1365U", "Intel Core Ultra 7 165U"]:
            for ram in ["16GB DDR5", "32GB DDR5", "64GB DDR5"]:
                for storage in ["512GB SSD", "1TB SSD", "2TB SSD"]:
                    price = {"16GB DDR5":680000,"32GB DDR5":950000,"64GB DDR5":1400000}[ram]
                    price += {"512GB SSD":0,"1TB SSD":70000,"2TB SSD":170000}[storage]
                    price += {"Intel Core i5-1345U":0,"Intel Core i7-1365U":140000,"Intel Core Ultra 7 165U":280000}[cpu]
                    variant = f"{ram} {storage}"
                    sku = slugify(f"dell_{model}_{cpu.split()[-1]}_{ram.split()[0]}_{storage.split()[0]}")
                    products.append((model, variant, sku,
                        laptop_desc("Latitude", model, cpu, ram, storage, display, "Windows 11 Pro", "senior executives and road warriors", "Premium"),
                        {"series":"Latitude","processor":cpu,"ram":ram,"storage":storage,
                         "display_size":display,"display_type":"IPS/OLED","graphics":"Intel Iris Xe",
                         "operating_system":"Windows 11 Pro","battery_life":"14","weight_kg":weight,
                         "price_tier":"Premium","nigeria_market_price_naira":str(price)}))

    # ── Vostro series ─────────────────────────────────────────────────────────
    for model, display in [
        ("Vostro 3420", "14"), ("Vostro 3520", "15.6"),
        ("Vostro 5620", "16"), ("Vostro 5630", "16"), ("Vostro 5640", "16"),
        ("Vostro 5410", "14"), ("Vostro 5510", "15.6"),
    ]:
        for cpu in ["Intel Core i3-1215U", "Intel Core i5-1235U", "Intel Core i7-1255U"]:
            for ram in ["8GB DDR4", "16GB DDR4"]:
                for storage in ["256GB SSD", "512GB SSD", "1TB SSD"]:
                    price = {"8GB DDR4":210000,"16GB DDR4":295000}[ram]
                    price += {"256GB SSD":0,"512GB SSD":25000,"1TB SSD":60000}[storage]
                    price += {"Intel Core i3-1215U":0,"Intel Core i5-1235U":50000,"Intel Core i7-1255U":120000}[cpu]
                    variant = f"{ram} {storage}"
                    sku = slugify(f"dell_{model}_{cpu.split()[-1]}_{ram.split()[0]}_{storage.split()[0]}")
                    products.append((model, variant, sku,
                        laptop_desc("Vostro", model, cpu, ram, storage, display, "Windows 11 Pro", "small and medium businesses", "Business"),
                        {"series":"Vostro","processor":cpu,"ram":ram,"storage":storage,
                         "display_size":display,"display_type":"WVA","graphics":"Intel Iris Xe",
                         "operating_system":"Windows 11 Pro","battery_life":"8","weight_kg":"1.65",
                         "price_tier":"Business","nigeria_market_price_naira":str(price)}))

    # ── XPS series ────────────────────────────────────────────────────────────
    for model, display, weight in [
        ("XPS 13 9315",  "13.4", "1.17"), ("XPS 13 9320",  "13.4", "1.17"),
        ("XPS 13 Plus",  "13.4", "1.24"), ("XPS 14 9440",  "14.5", "1.64"),
        ("XPS 15 9520",  "15.6", "1.86"), ("XPS 15 9530",  "15.6", "1.86"),
        ("XPS 17 9720",  "17",   "2.21"), ("XPS 17 9730",  "17",   "2.21"),
    ]:
        for cpu in ["Intel Core i5-1340P", "Intel Core i7-1360P", "Intel Core i9-13900H"]:
            for ram in ["16GB LPDDR5", "32GB LPDDR5"]:
                for storage in ["512GB SSD", "1TB SSD", "2TB SSD"]:
                    price = {"16GB LPDDR5":850000,"32GB LPDDR5":1200000}[ram]
                    price += {"512GB SSD":0,"1TB SSD":90000,"2TB SSD":210000}[storage]
                    price += {"Intel Core i5-1340P":0,"Intel Core i7-1360P":150000,"Intel Core i9-13900H":350000}[cpu]
                    variant = f"{ram} {storage}"
                    sku = slugify(f"dell_{model}_{cpu.split()[-1]}_{ram.split()[0]}_{storage.split()[0]}")
                    products.append((model, variant, sku,
                        laptop_desc("XPS", model, cpu, ram, storage, display, "Windows 11 Home", "creatives and executives", "Premium"),
                        {"series":"XPS","processor":cpu,"ram":ram,"storage":storage,
                         "display_size":display,"display_type":"OLED/IPS","graphics":"NVIDIA GeForce / Intel Iris Xe",
                         "operating_system":"Windows 11 Home","battery_life":"13","weight_kg":weight,
                         "price_tier":"Premium","nigeria_market_price_naira":str(price)}))

    # ── Precision mobile workstations ─────────────────────────────────────────
    for model, display, weight in [
        ("Precision 3580", "15.6", "1.86"), ("Precision 3581", "15.6", "1.90"),
        ("Precision 5570", "15.6", "1.86"), ("Precision 5580", "15.6", "1.86"),
        ("Precision 7680", "16",   "3.06"), ("Precision 7780", "17.3", "3.56"),
    ]:
        for cpu in ["Intel Core i7-13700H", "Intel Core i9-13900H", "Intel Xeon W-11955M"]:
            for ram in ["16GB DDR5", "32GB DDR5", "64GB DDR5"]:
                for storage in ["512GB SSD", "1TB SSD", "2TB SSD"]:
                    price = {"16GB DDR5":1100000,"32GB DDR5":1600000,"64GB DDR5":2400000}[ram]
                    price += {"512GB SSD":0,"1TB SSD":90000,"2TB SSD":220000}[storage]
                    price += {"Intel Core i7-13700H":0,"Intel Core i9-13900H":300000,"Intel Xeon W-11955M":600000}[cpu]
                    variant = f"{ram} {storage}"
                    sku = slugify(f"dell_{model}_{cpu.split()[-1]}_{ram.split()[0]}_{storage.split()[0]}")
                    products.append((model, variant, sku,
                        laptop_desc("Precision", model, cpu, ram, storage, display, "Windows 11 Pro for Workstations", "engineers and content creators", "Workstation"),
                        {"series":"Precision","processor":cpu,"ram":ram,"storage":storage,
                         "display_size":display,"display_type":"IPS/OLED","graphics":"NVIDIA RTX A-series",
                         "operating_system":"Windows 11 Pro for Workstations","battery_life":"8","weight_kg":weight,
                         "price_tier":"Workstation","nigeria_market_price_naira":str(price)}))

    # ── Alienware gaming ──────────────────────────────────────────────────────
    for model, display, weight in [
        ("Alienware m15 R7",  "15.6", "2.40"), ("Alienware m16 R1",  "16",   "3.02"),
        ("Alienware m18 R1",  "18",   "3.86"), ("Alienware x14 R2",  "14",   "1.83"),
        ("Alienware x15 R2",  "15.6", "2.16"),
    ]:
        for cpu in ["Intel Core i7-12700H", "Intel Core i9-12900HK", "AMD Ryzen 9 6900HX"]:
            for ram in ["16GB DDR5", "32GB DDR5"]:
                for storage in ["512GB SSD", "1TB SSD"]:
                    price = {"16GB DDR5":750000,"32GB DDR5":1050000}[ram]
                    price += {"512GB SSD":0,"1TB SSD":80000}[storage]
                    price += {"Intel Core i7-12700H":0,"Intel Core i9-12900HK":300000,"AMD Ryzen 9 6900HX":250000}[cpu]
                    variant = f"{ram} {storage}"
                    sku = slugify(f"dell_{model}_{cpu.split()[-1]}_{ram.split()[0]}_{storage.split()[0]}")
                    products.append((model, variant, sku,
                        laptop_desc("Alienware", model, cpu, ram, storage, display, "Windows 11 Home", "gaming and immersive computing", "Premium"),
                        {"series":"Alienware","processor":cpu,"ram":ram,"storage":storage,
                         "display_size":display,"display_type":"IPS 144Hz+","graphics":"NVIDIA GeForce RTX 3070/4070",
                         "operating_system":"Windows 11 Home","battery_life":"6","weight_kg":weight,
                         "price_tier":"Premium","nigeria_market_price_naira":str(price)}))

    # ── G-series gaming ───────────────────────────────────────────────────────
    for model, display in [
        ("G15 5520", "15.6"), ("G15 5530", "15.6"),
        ("G16 7620", "16"),   ("G16 7630", "16"),
    ]:
        for cpu in ["Intel Core i5-12500H", "Intel Core i7-12700H", "AMD Ryzen 7 6800H"]:
            for ram in ["8GB DDR5", "16GB DDR5", "32GB DDR5"]:
                for storage in ["256GB SSD", "512GB SSD", "1TB SSD"]:
                    price = {"8GB DDR5":380000,"16GB DDR5":520000,"32GB DDR5":750000}[ram]
                    price += {"256GB SSD":0,"512GB SSD":28000,"1TB SSD":70000}[storage]
                    price += {"Intel Core i5-12500H":0,"Intel Core i7-12700H":110000,"AMD Ryzen 7 6800H":100000}[cpu]
                    variant = f"{ram} {storage}"
                    sku = slugify(f"dell_{model}_{cpu.split()[-1]}_{ram.split()[0]}_{storage.split()[0]}")
                    products.append((model, variant, sku,
                        laptop_desc("G-series", model, cpu, ram, storage, display, "Windows 11 Home", "gaming and content creation", "Standard"),
                        {"series":"G-series","processor":cpu,"ram":ram,"storage":storage,
                         "display_size":display,"display_type":"IPS 120Hz+","graphics":"NVIDIA GeForce RTX 3060/4060",
                         "operating_system":"Windows 11 Home","battery_life":"6","weight_kg":"2.50",
                         "price_tier":"Standard","nigeria_market_price_naira":str(price)}))

    # ── Inspiron ──────────────────────────────────────────────────────────────
    for model, display in [
        ("Inspiron 14 5430",  "14"),   ("Inspiron 14 7430",  "14"),
        ("Inspiron 15 3520",  "15.6"), ("Inspiron 15 5530",  "15.6"),
        ("Inspiron 16 5630",  "16"),   ("Inspiron 16 Plus 7630", "16"),
    ]:
        for cpu in ["Intel Core i3-1215U", "Intel Core i5-1335U", "AMD Ryzen 5 7530U"]:
            for ram in ["8GB DDR4", "16GB DDR4"]:
                for storage in ["256GB SSD", "512GB SSD"]:
                    price = {"8GB DDR4":175000,"16GB DDR4":255000}[ram]
                    price += {"256GB SSD":0,"512GB SSD":23000}[storage]
                    price += {"Intel Core i3-1215U":0,"Intel Core i5-1335U":45000,"AMD Ryzen 5 7530U":40000}[cpu]
                    variant = f"{ram} {storage}"
                    sku = slugify(f"dell_{model}_{cpu.split()[-1]}_{ram.split()[0]}_{storage.split()[0]}")
                    products.append((model, variant, sku,
                        laptop_desc("Inspiron", model, cpu, ram, storage, display, "Windows 11 Home", "students and home users", "Budget"),
                        {"series":"Inspiron","processor":cpu,"ram":ram,"storage":storage,
                         "display_size":display,"display_type":"WVA","graphics":"Intel Iris Xe / AMD Radeon",
                         "operating_system":"Windows 11 Home","battery_life":"8","weight_kg":"1.59",
                         "price_tier":"Budget","nigeria_market_price_naira":str(price)}))

    return products


def gen_desktops():
    products = []

    # ── OptiPlex series ───────────────────────────────────────────────────────
    for model, form in [
        ("OptiPlex 3000 Micro",  "Micro"),        ("OptiPlex 3000 SFF",    "Small Form Factor"),
        ("OptiPlex 3000 Tower",  "Tower"),         ("OptiPlex 5000 Micro",  "Micro"),
        ("OptiPlex 5000 SFF",    "Small Form Factor"), ("OptiPlex 5000 Tower",  "Tower"),
        ("OptiPlex 7000 Micro",  "Micro"),         ("OptiPlex 7000 SFF",    "Small Form Factor"),
        ("OptiPlex 7000 Tower",  "Tower"),         ("OptiPlex 3010 SFF",    "Small Form Factor"),
        ("OptiPlex 5010 SFF",    "Small Form Factor"), ("OptiPlex 7010 Tower",  "Tower"),
    ]:
        tier = "Business" if "3000" in model or "3010" in model else "Premium"
        for cpu in ["Intel Core i3-12100", "Intel Core i5-12500", "Intel Core i7-12700"]:
            for ram in ["4GB DDR4", "8GB DDR4", "16GB DDR4", "32GB DDR4"]:
                for storage in ["256GB SSD", "512GB SSD", "1TB HDD + 256GB SSD"]:
                    price = {"4GB DDR4":145000,"8GB DDR4":190000,"16GB DDR4":270000,"32GB DDR4":410000}[ram]
                    price += {"256GB SSD":0,"512GB SSD":24000,"1TB HDD + 256GB SSD":38000}[storage]
                    price += {"Intel Core i3-12100":0,"Intel Core i5-12500":58000,"Intel Core i7-12700":145000}[cpu]
                    if "Micro" in form:
                        price += 20000
                    variant = f"{ram} {storage} {form}"
                    sku = slugify(f"dell_{model}_{cpu.split()[-1]}_{ram.split()[0]}_{storage.split()[0]}")
                    products.append((model, variant, sku,
                        desktop_desc("OptiPlex", model, cpu, ram, storage, form, "Windows 11 Pro", "corporate office deployments", tier),
                        {"series":"OptiPlex","processor":cpu,"ram":ram,"storage":storage,
                         "form_factor":form,"graphics":"Intel UHD 770",
                         "operating_system":"Windows 11 Pro","optical_drive":"None",
                         "price_tier":tier,"nigeria_market_price_naira":str(price)}))

    # ── Vostro Desktop ────────────────────────────────────────────────────────
    for model, form in [
        ("Vostro 3020 SFF",  "Small Form Factor"), ("Vostro 3020 Tower", "Tower"),
        ("Vostro 3910 Tower","Tower"),
    ]:
        for cpu in ["Intel Core i3-12100", "Intel Core i5-12400", "Intel Core i7-12700"]:
            for ram in ["8GB DDR4", "16GB DDR4", "32GB DDR4"]:
                for storage in ["256GB SSD", "512GB SSD", "1TB HDD + 256GB SSD"]:
                    price = {"8GB DDR4":175000,"16GB DDR4":245000,"32GB DDR4":380000}[ram]
                    price += {"256GB SSD":0,"512GB SSD":22000,"1TB HDD + 256GB SSD":36000}[storage]
                    price += {"Intel Core i3-12100":0,"Intel Core i5-12400":55000,"Intel Core i7-12700":140000}[cpu]
                    variant = f"{ram} {storage} {form}"
                    sku = slugify(f"dell_{model}_{cpu.split()[-1]}_{ram.split()[0]}_{storage.split()[0]}")
                    products.append((model, variant, sku,
                        desktop_desc("Vostro", model, cpu, ram, storage, form, "Windows 11 Pro", "small business use", "Business"),
                        {"series":"Vostro","processor":cpu,"ram":ram,"storage":storage,
                         "form_factor":form,"graphics":"Intel UHD 730",
                         "operating_system":"Windows 11 Pro","optical_drive":"Optional DVD-RW",
                         "price_tier":"Business","nigeria_market_price_naira":str(price)}))

    # ── Precision Tower workstations ──────────────────────────────────────────
    for model, form, base in [
        ("Precision 3660 Tower", "Tower",  750000),
        ("Precision 5860 Tower", "Tower", 1500000),
        ("Precision 7960 Tower", "Tower", 3000000),
        ("Precision 3460 SFF",   "Small Form Factor", 700000),
    ]:
        for cpu in ["Intel Core i7-13700", "Intel Xeon W3-2423", "Intel Xeon W5-2445"]:
            for ram in ["16GB DDR5 ECC", "32GB DDR5 ECC", "64GB DDR5 ECC"]:
                for storage in ["512GB SSD", "1TB SSD", "2TB SSD"]:
                    price = base
                    price += {"16GB DDR5 ECC":0,"32GB DDR5 ECC":170000,"64GB DDR5 ECC":400000}[ram]
                    price += {"512GB SSD":0,"1TB SSD":85000,"2TB SSD":200000}[storage]
                    price += {"Intel Core i7-13700":0,"Intel Xeon W3-2423":330000,"Intel Xeon W5-2445":680000}[cpu]
                    variant = f"{ram} {storage}"
                    sku = slugify(f"dell_{model}_{cpu.split()[-1]}_{ram.split()[0]}_{storage.split()[0]}")
                    products.append((model, variant, sku,
                        desktop_desc("Precision", model, cpu, ram, storage, form, "Windows 11 Pro for Workstations", "engineering and creative workloads", "Workstation"),
                        {"series":"Precision","processor":cpu,"ram":ram,"storage":storage,
                         "form_factor":form,"graphics":"NVIDIA RTX A-series / AMD Radeon Pro",
                         "operating_system":"Windows 11 Pro for Workstations","optical_drive":"Optional",
                         "price_tier":"Workstation","nigeria_market_price_naira":str(price)}))

    # ── XPS Desktop & Inspiron Desktop ────────────────────────────────────────
    for model, form, base, tier, os_c in [
        ("XPS 8960",         "Tower", 700000,  "Premium",  "Windows 11 Home"),
        ("Inspiron 3030 SFF","Small Form Factor",135000,"Budget","Windows 11 Home"),
        ("Inspiron 3030 MT", "Mini Tower",       150000,"Budget","Windows 11 Home"),
        ("Inspiron 5030 Tower","Tower",           185000,"Budget","Windows 11 Home"),
    ]:
        for cpu in ["Intel Core i5-13400", "Intel Core i7-13700", "Intel Core i9-13900"]:
            for ram in ["8GB DDR5", "16GB DDR5", "32GB DDR5"]:
                for storage in ["512GB SSD", "1TB SSD", "2TB HDD + 512GB SSD"]:
                    price = base
                    price += {"8GB DDR5":0,"16GB DDR5":65000,"32GB DDR5":160000}[ram]
                    price += {"512GB SSD":0,"1TB SSD":70000,"2TB HDD + 512GB SSD":120000}[storage]
                    price += {"Intel Core i5-13400":0,"Intel Core i7-13700":120000,"Intel Core i9-13900":280000}[cpu]
                    variant = f"{ram} {storage}"
                    sku = slugify(f"dell_{model}_{cpu.split()[-1]}_{ram.split()[0]}_{storage.split()[0]}")
                    products.append((model, variant, sku,
                        desktop_desc("XPS" if "XPS" in model else "Inspiron", model, cpu, ram, storage, form, os_c, "home and creative use", tier),
                        {"series":"XPS" if "XPS" in model else "Inspiron","processor":cpu,"ram":ram,"storage":storage,
                         "form_factor":form,"graphics":"NVIDIA GeForce / Intel UHD",
                         "operating_system":os_c,"optical_drive":"Optional",
                         "price_tier":tier,"nigeria_market_price_naira":str(price)}))

    return products


def gen_servers():
    products = []

    configs = [
        # (model, form_factor, drive_bays, max_ram, net_ports, base_price)
        ("PowerEdge R250",  "1U Rack", "4 LFF",  "128GB",  "2x 1GbE",   950000),
        ("PowerEdge R350",  "1U Rack", "8 SFF",  "128GB",  "2x 1GbE",  1300000),
        ("PowerEdge R450",  "1U Rack", "8 SFF",  "4TB",    "4x 1GbE",  2600000),
        ("PowerEdge R550",  "2U Rack", "12 LFF", "4TB",    "4x 1GbE",  3200000),
        ("PowerEdge R650",  "1U Rack", "10 SFF", "8TB",    "4x 10GbE", 4800000),
        ("PowerEdge R650xs","1U Rack", "10 SFF", "8TB",    "4x 10GbE", 5200000),
        ("PowerEdge R750",  "2U Rack", "24 SFF", "8TB",    "4x 10GbE", 5800000),
        ("PowerEdge R750xs","2U Rack", "24 SFF", "8TB",    "4x 10GbE", 6200000),
        ("PowerEdge R760",  "2U Rack", "24 SFF", "8TB",    "4x 25GbE", 7500000),
        ("PowerEdge R960",  "2U Rack", "24 SFF", "12TB",   "4x 25GbE",12000000),
        ("PowerEdge T150",  "Tower",   "4 LFF",  "128GB",  "2x 1GbE",   900000),
        ("PowerEdge T350",  "Tower",   "8 LFF",  "128GB",  "2x 1GbE",  1200000),
        ("PowerEdge T550",  "Tower",   "16 LFF", "4TB",    "4x 1GbE",  3500000),
        ("PowerEdge T650",  "Tower",   "16 LFF", "8TB",    "4x 10GbE", 5500000),
    ]

    cpus_by_tier = {
        "Entry":          ["Intel Xeon E-2314","Intel Xeon E-2334","Intel Xeon E-2356G"],
        "Mid-range":      ["Intel Xeon Silver 4310","Intel Xeon Silver 4314","Intel Xeon Gold 5315Y"],
        "Enterprise":     ["Intel Xeon Gold 6330","Intel Xeon Gold 6348","Intel Xeon Platinum 8352V"],
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
        "Intel Xeon E-2314":0,"Intel Xeon E-2334":60000,"Intel Xeon E-2356G":180000,
        "Intel Xeon Silver 4310":250000,"Intel Xeon Silver 4314":350000,
        "Intel Xeon Gold 5315Y":650000,"Intel Xeon Gold 6330":1000000,
        "Intel Xeon Gold 6348":1700000,"Intel Xeon Platinum 8352V":3200000,
        "Intel Xeon Platinum 8358":4000000,"Intel Xeon Platinum 8380":5200000,
    }
    ram_price = {
        "16GB DDR4 ECC":0,"32GB DDR4 ECC":110000,"64GB DDR4 ECC":260000,
        "128GB DDR4 ECC":580000,"256GB DDR4 ECC":1300000,"512GB DDR4 ECC":2800000,
    }
    stor_price = {
        "1x 1.2TB SAS HDD":0,"2x 1.2TB SAS HDD":75000,"2x 960GB SATA SSD":190000,
        "4x 1.2TB SAS HDD":140000,"2x 1.92TB SATA SSD":460000,"4x 960GB SATA SSD":360000,
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
                    sku = slugify(f"dell_{model}_{cpu.split()[-1]}_{ram.split()[0]}_{stor_tag}")
                    products.append((model, variant, sku,
                        server_desc("PowerEdge", model, cpu, ram, storage, form, bays, max_r, ports),
                        {"series":"PowerEdge","processor":cpu,"ram":ram,"storage":storage,
                         "form_factor":form,"drive_bays":bays,"max_ram":max_r,
                         "network_ports":ports,"price_tier":tier,
                         "nigeria_market_price_naira":str(price)}))

    return products


def gen_thin_clients():
    products = []

    tc_configs = [
        # (model, series, cpu, form, base_price)
        ("Wyse 3040",               "Wyse",    "Intel Atom x5-Z8350 Quad-core",    "Ultra Slim", 88000),
        ("Wyse 5070",               "Wyse",    "Intel Celeron J4105 Quad-core",     "Desktop",    130000),
        ("Wyse 5470 Thin Client",   "Wyse",    "Intel Celeron 4205U Dual-core",     "Ultra Slim", 175000),
        ("Wyse 5470 All-in-One",    "Wyse",    "Intel Celeron 4205U Dual-core",     "AiO",        230000),
        ("Wyse 5480 All-in-One",    "Wyse",    "Intel Pentium Silver J5005",        "AiO",        270000),
        ("Wyse 5020",               "Wyse",    "AMD GX-415GA Quad-core",            "Desktop",    110000),
        ("Wyse 7020",               "Wyse",    "AMD GX-424CC Quad-core",            "Desktop",    185000),
        ("OptiPlex 3000 Thin Client","OptiPlex","Intel Celeron 6305E Dual-core",    "Ultra Slim", 160000),
        ("OptiPlex 5000 Thin Client","OptiPlex","Intel Core i3-1115G4 Dual-core",   "Ultra Slim", 225000),
        ("OptiPlex 3000 All-in-One TC","OptiPlex","Intel Core i3-12100T Quad-core", "AiO",        310000),
        ("OptiPlex 5000 All-in-One TC","OptiPlex","Intel Core i5-12500T Hexa-core", "AiO",        420000),
        ("Wyse 3040 2.0",           "Wyse",    "Intel Atom x6212RE Dual-core",     "Ultra Slim", 95000),
    ]

    ram_opts     = ["4GB DDR4","8GB DDR4","16GB DDR4"]
    storage_opts = ["16GB eMMC","32GB eMMC","64GB eMMC","128GB SSD","256GB SSD"]
    os_opts      = ["Wyse ThinOS","Windows 10 IoT Enterprise","Windows 11 IoT Enterprise","Ubuntu 20.04 LTS"]
    disp_opts    = ["Single 4K Display","Dual FHD Displays","Triple FHD Displays"]
    use_cases    = {
        "Wyse":    "virtual desktop and cloud computing",
        "OptiPlex":"enterprise VDI and cloud-managed",
    }
    ram_price  = {"4GB DDR4":0,"8GB DDR4":23000,"16GB DDR4":55000}
    stor_price = {"16GB eMMC":0,"32GB eMMC":7500,"64GB eMMC":16000,"128GB SSD":32000,"256GB SSD":65000}
    os_price   = {"Wyse ThinOS":0,"Windows 10 IoT Enterprise":22000,"Windows 11 IoT Enterprise":28000,"Ubuntu 20.04 LTS":0}

    for (model, series, cpu, form, base) in tc_configs:
        tier = "Entry" if base < 160000 else "Standard" if base < 280000 else "Advanced"
        r_opts = ram_opts[:2] if tier == "Entry" else ram_opts
        s_opts = storage_opts[:3] if tier == "Entry" else storage_opts[1:4]
        for ram in r_opts:
            for storage in s_opts:
                for os in os_opts:
                    price = base + ram_price[ram] + stor_price[storage] + os_price[os]
                    variant = f"{ram} {storage} {os}"
                    os_tag = slugify(os)[:12]
                    sku = slugify(f"dell_{model}_{ram.split()[0]}_{storage.replace(' ','_')}_{os_tag}")
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
        ("VESA Mount Only",  -8000, "vesa"),
    ]
    COLOR_VARIANTS = [
        ("Black",        0,    "blk"),
        ("Platinum Silver", 5000, "slv"),
        ("White",        5000, "wht"),
    ]

    # ── UltraSharp U-series ────────────────────────────────────────────────────
    ultrasharp = [
        ("U2022H",   "UltraSharp","19.5","FHD",    "IPS","60Hz","5","HDMI+DP+USB-C",        115000,"office productivity","Standard"),
        ("U2222H",   "UltraSharp","21.5","FHD",    "IPS","60Hz","5","HDMI+DP+USB-C 65W",    130000,"office productivity","Standard"),
        ("U2422H",   "UltraSharp","23.8","FHD",    "IPS","60Hz","5","HDMI+DP+USB-C 65W",    155000,"office productivity","Standard"),
        ("U2422D",   "UltraSharp","23.8","QHD",    "IPS","60Hz","5","HDMI+DP+USB-C 65W",    195000,"office productivity","Professional"),
        ("U2422DE",  "UltraSharp","23.8","QHD",    "IPS","60Hz","5","TB3+HDMI+DP+USB-C 90W",245000,"office productivity","Professional"),
        ("U2423H",   "UltraSharp","23.8","FHD",    "IPS","60Hz","5","HDMI+DP+USB-C 65W",    160000,"office productivity","Standard"),
        ("U2723D",   "UltraSharp","27",  "QHD",    "IPS","60Hz","5","HDMI+DP+USB-C 65W",    240000,"office productivity","Professional"),
        ("U2723DE",  "UltraSharp","27",  "QHD",    "IPS","60Hz","5","TB3+HDMI+DP+USB-C 90W",305000,"office productivity","Professional"),
        ("U2724D",   "UltraSharp","27",  "QHD",    "IPS","60Hz","5","HDMI+DP+USB-C 65W",    248000,"office productivity","Professional"),
        ("U2724DE",  "UltraSharp","27",  "QHD",    "IPS","60Hz","5","TB4+HDMI+DP+USB-C 90W",315000,"office productivity","Professional"),
        ("U3223QE",  "UltraSharp","31.5","4K UHD", "IPS","60Hz","5","TB4+HDMI+DP+USB-C 90W",480000,"workstation design","Workstation"),
        ("U3224KBA", "UltraSharp","31.5","4K UHD", "IPS","60Hz","5","TB4+HDMI+DP+4xUSB-A",  530000,"workstation design","Workstation"),
        ("U4323QE",  "UltraSharp","42.5","4K UHD", "IPS","60Hz","5","TB3+HDMI+DP+USB-C 90W",750000,"workstation design","Workstation"),
        ("U3421WE",  "UltraSharp","34",  "WQHD+",  "IPS","60Hz","5","TB3+HDMI+DP+USB-C 90W",520000,"workstation design","Workstation"),
        ("U3423WE",  "UltraSharp","34",  "WQHD+",  "IPS","60Hz","5","TB4+HDMI+DP+USB-C 90W",545000,"workstation design","Workstation"),
        ("U4021QW",  "UltraSharp","39.7","5K2K",   "IPS","60Hz","5","TB3+HDMI+DP+USB-C 90W",820000,"workstation design","Workstation"),
    ]

    # ── Professional P-series ─────────────────────────────────────────────────
    professional = [
        ("P2222H",  "Professional","21.5","FHD",   "IPS","60Hz","5","VGA+HDMI+DP",          88000,"office productivity","Entry"),
        ("P2422H",  "Professional","23.8","FHD",   "IPS","60Hz","5","VGA+HDMI+DP",         105000,"office productivity","Standard"),
        ("P2422HE", "Professional","23.8","FHD",   "IPS","60Hz","5","HDMI+DP+USB-C 65W",   130000,"office productivity","Standard"),
        ("P2423D",  "Professional","23.8","QHD",   "IPS","60Hz","5","HDMI+DP+USB-C 65W",   165000,"office productivity","Standard"),
        ("P2723D",  "Professional","27",  "QHD",   "IPS","60Hz","5","HDMI+DP+USB-C 65W",   200000,"office productivity","Standard"),
        ("P2723DE", "Professional","27",  "QHD",   "IPS","60Hz","5","TB3+HDMI+DP+USB-C 90W",255000,"office productivity","Professional"),
        ("P3223DE", "Professional","31.5","4K UHD","IPS","60Hz","5","TB3+HDMI+DP+USB-C 90W",420000,"office productivity","Professional"),
        ("P3424WE", "Professional","34",  "WQHD",  "IPS","60Hz","5","TB3+HDMI+DP+USB-C 90W",480000,"office productivity","Professional"),
        ("P2424HT", "Professional","23.8","FHD",   "IPS","60Hz","5","HDMI+DP+USB-C 65W",   190000,"collaboration touch","Professional"),
    ]

    # ── Standard S-series ─────────────────────────────────────────────────────
    standard = [
        ("S2422H",  "S-series","23.8","FHD",   "IPS","75Hz","5","HDMI+DP",               75000,"home office","Entry"),
        ("S2422HZ", "S-series","23.8","FHD",   "IPS","60Hz","5","HDMI+USB-C+DP",        120000,"collaboration","Standard"),
        ("S2722H",  "S-series","27",  "FHD",   "IPS","75Hz","5","HDMI+DP",               92000,"home office","Entry"),
        ("S2722DC", "S-series","27",  "QHD",   "IPS","75Hz","5","HDMI+DP+USB-C 65W",    175000,"home office","Standard"),
        ("S2722DZ", "S-series","27",  "QHD",   "IPS","75Hz","5","HDMI+USB-C+DP",        190000,"collaboration","Standard"),
        ("S3222DGM","S-series","31.5","QHD",   "VA", "165Hz","1","HDMI+DP",             245000,"gaming/office","Standard"),
        ("S3222HN", "S-series","31.5","FHD",   "VA", "75Hz","5","HDMI+DP",             165000,"home office","Standard"),
        ("S3422DW", "S-series","34",  "WQHD",  "VA", "75Hz","5","HDMI+DP+USB-C 65W",   285000,"home office","Standard"),
        ("S3423DWC","S-series","34",  "WQHD",  "VA", "100Hz","5","HDMI+DP+USB-C 90W",  310000,"home office","Standard"),
        ("S2721D",  "S-series","27",  "QHD",   "IPS","75Hz","5","HDMI+DP",             148000,"home office","Standard"),
        ("S2721DS", "S-series","27",  "QHD",   "IPS","75Hz","5","HDMI+DP",             160000,"home office","Standard"),
        ("S2421H",  "S-series","23.8","FHD",   "IPS","75Hz","5","HDMI",                 65000,"home office","Entry"),
        ("S2421HS", "S-series","23.8","FHD",   "IPS","75Hz","5","HDMI+DP",              72000,"home office","Entry"),
    ]

    # ── Gaming G-series ───────────────────────────────────────────────────────
    gaming = [
        ("G2422HS",  "Gaming","23.8","FHD",   "IPS","165Hz","1","HDMI 2.0+DP 1.2",   145000,"gaming","Entry"),
        ("G2522H",   "Gaming","24.5","FHD",   "IPS","165Hz","1","HDMI 2.0+DP 1.4",   158000,"gaming","Entry"),
        ("G2522HS",  "Gaming","24.5","FHD",   "IPS","165Hz","1","HDMI 2.0+DP 1.4",   165000,"gaming","Entry"),
        ("G2722D",   "Gaming","27",  "QHD",   "IPS","165Hz","1","HDMI 2.0+DP 1.4",   230000,"gaming","Standard"),
        ("G2722HS",  "Gaming","27",  "FHD",   "IPS","165Hz","1","HDMI 2.0+DP 1.4",   195000,"gaming","Standard"),
        ("G3223D",   "Gaming","31.5","QHD",   "IPS","165Hz","1","HDMI 2.0+DP 1.4",   340000,"gaming","Standard"),
        ("G3223Q",   "Gaming","31.5","4K UHD","IPS","144Hz","1","HDMI 2.1+DP 1.4",   450000,"gaming","Premium"),
        ("G3422DW",  "Gaming","34",  "UWQHD", "VA", "144Hz","1","HDMI 2.0+DP 1.4",   420000,"ultrawide gaming","Standard"),
        ("G3423DW",  "Gaming","34",  "UWQHD", "VA", "165Hz","1","HDMI 2.1+DP 1.4",   455000,"ultrawide gaming","Standard"),
        ("G2422HS 2023","Gaming","23.8","FHD","IPS","165Hz","1","HDMI 2.0+DP 1.4+USB",152000,"gaming","Entry"),
        ("G2722D 2023","Gaming","27", "QHD",  "IPS","165Hz","1","HDMI 2.1+DP 1.4+USB",240000,"gaming","Standard"),
        ("G3223D 2023","Gaming","31.5","QHD", "IPS","165Hz","1","HDMI 2.1+DP 1.4+USB",352000,"gaming","Standard"),
    ]

    # ── Alienware ─────────────────────────────────────────────────────────────
    alienware = [
        ("AW2523HF",  "Alienware","24.5","FHD",   "IPS","360Hz","0.5","HDMI 2.0+DP 1.4+USB-C",   385000,"competitive gaming","Premium"),
        ("AW2524H",   "Alienware","24.5","FHD",   "IPS","360Hz","0.5","HDMI 2.0+DP 1.4+USB-C",   410000,"competitive gaming","Premium"),
        ("AW2724HF",  "Alienware","27",  "FHD",   "IPS","280Hz","0.5","HDMI 2.0+DP 1.4+USB-C",   440000,"competitive gaming","Premium"),
        ("AW2723D",   "Alienware","27",  "QHD",   "IPS","165Hz","1",  "HDMI 2.1+DP 1.4+USB-C",   495000,"gaming","Premium"),
        ("AW2723DF",  "Alienware","27",  "QHD",   "IPS","165Hz","1",  "HDMI 2.1+DP 1.4+USB-C",   520000,"gaming","Premium"),
        ("AW3423D",   "Alienware","34",  "UWQHD", "QD-OLED","175Hz","0.1","HDMI 2.1+DP 1.4+USB-C",820000,"ultrawide gaming","Premium"),
        ("AW3423DWF", "Alienware","34",  "UWQHD", "QD-OLED","165Hz","0.1","HDMI 2.1+DP 1.4+USB-C",780000,"ultrawide gaming","Premium"),
        ("AW3423DW",  "Alienware","34",  "UWQHD", "QD-OLED","175Hz","0.1","HDMI 2.1+DP 1.4+USB-C",850000,"ultrawide gaming","Premium"),
        ("AW3225QF",  "Alienware","32",  "4K UHD","QD-OLED","240Hz","0.03","HDMI 2.1+DP 1.4+USB-C",1200000,"gaming","Premium"),
    ]

    def add_p(series_name, model, size, res, panel, refresh, resp, conn,
              base, use_case, tier, suffix="", delta=0):
        price = base + delta
        variant = f"{size}\" {res} {panel} {refresh}{' '+suffix if suffix else ''}"
        sku = slugify(f"dell_{model}_{size}_{res}_{panel}_{refresh}{'_'+slugify(suffix) if suffix else ''}")
        desc = monitor_desc(series_name, model, size, res, panel, refresh, resp, conn, use_case, tier)
        products.append((model, variant, sku, desc, {
            "series": series_name, "screen_size": size, "resolution": res,
            "panel_type": panel, "refresh_rate": refresh, "response_time": resp,
            "connectivity": conn, "price_tier": tier,
            "nigeria_market_price_naira": str(price),
        }))

    # UltraSharp + Professional + Standard: 3 stand variants
    for (model, series, size, res, panel, refresh, resp, conn, base, use_case, tier) in (
            ultrasharp + professional + standard):
        for (slabel, sdelta, _) in STAND_VARIANTS:
            add_p(series, model, size, res, panel, refresh, resp, conn, base, use_case, tier,
                  suffix=slabel, delta=sdelta)

    # Gaming + Alienware: 3 color variants
    for entry in gaming + alienware:
        (model, series, size, res, panel, refresh, resp, conn, base, use_case, tier) = entry
        for (clabel, cdelta, _) in COLOR_VARIANTS:
            add_p(series, model, size, res, panel, refresh, resp, conn, base, use_case, tier,
                  suffix=clabel, delta=cdelta)

    return products


# ─────────────────────────────────────────────────────────────────────────────
# INSERT HELPERS  (identical to HP script)
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
    "Dell Laptops":      gen_laptops,
    "Dell Desktops":     gen_desktops,
    "Dell Servers":      gen_servers,
    "Dell Thin Clients": gen_thin_clients,
    "Dell Monitors":     gen_monitors,
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
        WHERE cc.slug LIKE 'dell-%'
        GROUP BY cc.name ORDER BY cc.name
    """)
    print("\n── Final counts ─────────────────────────")
    total_check = 0
    for row in cur.fetchall():
        print(f"  {row['name']}: {row['cnt']}")
        total_check += row['cnt']
    print(f"  TOTAL DELL PRODUCTS: {total_check}")
    cur.close(); conn.close()
    print("Done.")


if __name__ == "__main__":
    run()
