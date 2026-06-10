"""
Add 14 new mobile phone brands to the Mobile Phones catalogue:
Nokia, Huawei, Honor, Realme, Vivo, OnePlus, Motorola, Poco,
Gionee, Alcatel, Wiko, ZTE, Google Pixel, Sony Xperia

Targets ~3,000 new phones across all 14 brands.
Safe to re-run — uses ON CONFLICT DO NOTHING.
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


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


# ─────────────────────────────────────────────────────────────────────────────
# DESCRIPTION TEMPLATES  (4 per brand, selected by hash)
# ─────────────────────────────────────────────────────────────────────────────

BRAND_DESCS = {
    "Nokia": [
        "The {model} brings Nokia's legendary durability to everyday mobile use. "
        "Powered by {chipset} with {ram} RAM and {storage} storage, it delivers "
        "reliable performance for calls, messaging and social media. Running stock "
        "Android with guaranteed security updates, it is ideal for users who value "
        "simplicity, long battery life and a phone that just works.",

        "Built tough and designed to last, the Nokia {model} features {chipset}, "
        "{ram} RAM and {storage} storage in a {material} body. Its {battery}mAh "
        "battery ensures all-day use on a single charge, while stock Android "
        "guarantees a clean, bloatware-free experience. Nokia's monthly security "
        "patches keep your data safe year after year.",

        "The Nokia {model} is a dependable {tier} smartphone combining {chipset} "
        "processing with {ram} RAM and {storage} storage. The {screen}-inch display "
        "provides comfortable viewing for daily tasks. With Nokia's commitment to "
        "two-year Android OS updates and three years of security patches, this phone "
        "delivers long-term value for budget-conscious users.",

        "Offering a pure Android experience, the Nokia {model} pairs {chipset} with "
        "{ram} RAM and {storage} storage for smooth everyday performance. Its {battery}mAh "
        "battery and {screen}-inch screen make it a practical choice for {best_for}. "
        "Nokia's renowned build quality and timely software updates make it one of the "
        "most reliable options in its price category.",
    ],

    "Huawei": [
        "The Huawei {model} delivers premium performance with {chipset} processing, "
        "{ram} RAM and {storage} storage. Its advanced camera system captures stunning "
        "photos with rich detail, while the large {battery}mAh battery keeps you "
        "connected throughout the day. The elegant {material} design and vibrant "
        "{screen}-inch display make it a stylish companion for modern life.",

        "Engineered for excellence, the Huawei {model} features {chipset}, {ram} RAM "
        "and {storage} storage in a premium build. Huawei's renowned camera technology "
        "delivers professional-grade photography, while the {battery}mAh battery with "
        "{fast_charge}W fast charging ensures rapid power top-ups. Ideal for "
        "{best_for} users who demand performance and style.",

        "The Huawei {model} combines powerful {chipset} processing with {ram} RAM and "
        "{storage} storage for a seamless {tier} experience. The {screen}-inch display "
        "with vibrant colours supports immersive media consumption and productivity. "
        "Fast {fast_charge}W charging technology refills the {battery}mAh battery "
        "quickly, keeping you productive throughout your day.",

        "Packed with {chipset}, {ram} RAM and {storage} storage, the Huawei {model} "
        "is built for {best_for}. Its outstanding camera captures every moment with "
        "clarity, while the robust {battery}mAh battery provides extended usage. "
        "The sleek {material} body and {screen}-inch display deliver a premium "
        "feel that rivals phones at higher price points.",
    ],

    "Honor": [
        "The Honor {model} offers flagship-class features at a compelling price. "
        "Featuring {chipset}, {ram} RAM and {storage} storage, it handles everything "
        "from gaming to content creation with ease. The {screen}-inch display delivers "
        "vivid, sharp visuals while the {battery}mAh battery with {fast_charge}W "
        "charging keeps you powered all day long.",

        "Built for performance-driven users, the Honor {model} packs {chipset} "
        "processing, {ram} RAM and {storage} storage into a stylish {material} design. "
        "Its high-resolution camera system captures professional-quality shots, "
        "while MagicOS delivers a smooth, intelligent user experience tailored to "
        "your daily habits.",

        "The Honor {model} delivers exceptional value with {chipset}, {ram} RAM and "
        "{storage} storage. The {screen}-inch display supports comfortable long-form "
        "content viewing, and the {battery}mAh battery ensures extended screen time. "
        "Honor's AI-powered camera enhancements capture stunning images in any "
        "lighting condition, making it perfect for {best_for}.",

        "Combining style and substance, the Honor {model} features {chipset} "
        "processing, {ram} RAM and {storage} storage in a premium {material} chassis. "
        "The intelligent MagicOS platform optimises battery life and app performance "
        "automatically, while the multi-lens camera system delivers versatile "
        "photography for every scene.",
    ],

    "Realme": [
        "The Realme {model} delivers performance beyond its price with {chipset}, "
        "{ram} RAM and {storage} storage. Its {fast_charge}W fast charging technology "
        "fills the {battery}mAh battery rapidly, minimising downtime. The {screen}-inch "
        "display provides smooth, responsive visuals perfect for gaming and streaming. "
        "Realme UI brings smart features that enhance daily productivity.",

        "Designed for the performance-hungry generation, the Realme {model} features "
        "{chipset}, {ram} RAM and {storage} storage at a budget-friendly price. "
        "The flagship-grade {fast_charge}W fast charge and {battery}mAh battery combo "
        "keeps you powered through intense usage. Perfect for {best_for} users who "
        "want maximum specs for their money.",

        "The Realme {model} packs {chipset} processing with {ram} RAM and {storage} "
        "storage into a sleek {material} body. Its {screen}-inch display renders "
        "content with vivid colours, while the AI-enhanced camera captures clear, "
        "detailed photos. With {fast_charge}W charging and a {battery}mAh battery, "
        "you spend less time charging and more time doing.",

        "Value meets performance in the Realme {model}. Equipped with {chipset}, "
        "{ram} RAM and {storage} storage, it handles multitasking, gaming and content "
        "creation smoothly. The large {battery}mAh battery and {fast_charge}W fast "
        "charging deliver all-day power, while the intelligent Realme UI keeps the "
        "experience fluid and intuitive for {best_for}.",
    ],

    "Vivo": [
        "The Vivo {model} is engineered for photography enthusiasts and style-conscious "
        "users. Featuring {chipset}, {ram} RAM and {storage} storage, it captures "
        "stunning portraits with its advanced camera system. The {screen}-inch display "
        "renders vivid content, and {fast_charge}W fast charging replenishes the "
        "{battery}mAh battery in record time.",

        "With {chipset}, {ram} RAM and {storage} storage, the Vivo {model} delivers "
        "a smooth, responsive experience for everyday use. Its slim {material} design "
        "fits comfortably in hand, while the powerful camera system captures detailed "
        "shots day and night. Ideal for {best_for} users who want performance "
        "and an elegant aesthetic.",

        "The Vivo {model} stands out with {chipset} processing, {ram} RAM and "
        "{storage} storage in a premium {material} body. The {screen}-inch display "
        "provides immersive viewing for videos and games, while the {battery}mAh "
        "battery ensures you stay connected all day. Vivo's photography algorithms "
        "deliver brilliant images even in low-light conditions.",

        "Packed with {chipset}, {ram} RAM and {storage}, the Vivo {model} offers "
        "exceptional performance for its price tier. The {battery}mAh battery with "
        "{fast_charge}W fast charge minimises charging time, keeping you focused on "
        "what matters. Its intuitive camera features and {screen}-inch display "
        "make it the go-to choice for {best_for}.",
    ],

    "OnePlus": [
        "The OnePlus {model} lives up to the brand's 'Never Settle' philosophy with "
        "{chipset}, {ram} RAM and {storage} storage. Its signature {fast_charge}W "
        "Warp/SUPERVOOC charging fills the {battery}mAh battery in under 30 minutes. "
        "OxygenOS delivers a clean, fast Android experience with thoughtful features "
        "that power users love.",

        "Engineered for speed, the OnePlus {model} features {chipset}, {ram} RAM and "
        "{storage} storage in a premium {material} design. The {screen}-inch display "
        "with high refresh rate ensures buttery-smooth visuals for gaming and "
        "scrolling. Hasselblad-tuned cameras capture professional-quality images "
        "with natural colour accuracy.",

        "The OnePlus {model} delivers flagship performance with {chipset}, {ram} RAM "
        "and {storage} storage. OxygenOS optimises every aspect of the experience "
        "for speed and smoothness. The {battery}mAh battery with {fast_charge}W "
        "fast charging means a quick top-up keeps you going all day — perfect "
        "for {best_for}.",

        "Built for enthusiasts, the OnePlus {model} packs {chipset}, {ram} RAM and "
        "{storage} storage with a focus on speed and smooth performance. The "
        "{screen}-inch display and carefully tuned camera system deliver a premium "
        "experience. OnePlus's regular software updates ensure your phone stays "
        "fast and secure long after purchase.",
    ],

    "Motorola": [
        "The Motorola {model} combines reliable performance with near-stock Android "
        "for a pure, uncluttered experience. Featuring {chipset}, {ram} RAM and "
        "{storage} storage, it handles daily tasks with ease. Motorola's Moto "
        "Actions and gesture controls add practical shortcuts, while the {battery}mAh "
        "battery provides all-day endurance.",

        "With {chipset}, {ram} RAM and {storage} storage, the Motorola {model} "
        "delivers smooth everyday performance in a well-built {material} body. "
        "The {screen}-inch display offers comfortable viewing for browsing and "
        "streaming. Near-stock Android means faster updates and a clutter-free "
        "interface — ideal for users who value simplicity.",

        "The Motorola {model} offers exceptional battery life with its {battery}mAh "
        "cell, complemented by {chipset} processing, {ram} RAM and {storage} "
        "storage. The IP-rated water resistance adds peace of mind for everyday "
        "adventures. Motorola's Ready For feature enables laptop-style productivity "
        "when connected to a display.",

        "Reliable, practical and great value — the Motorola {model} delivers "
        "{chipset}, {ram} RAM and {storage} storage with Motorola's signature "
        "near-stock Android experience. The {screen}-inch display and multi-lens "
        "camera system cover all everyday needs, while the {battery}mAh battery "
        "keeps you going from morning to night. Perfect for {best_for}.",
    ],

    "Poco": [
        "The Poco {model} is the ultimate budget performance champion, packing "
        "{chipset}, {ram} RAM and {storage} storage at an aggressive price point. "
        "Its high-refresh-rate {screen}-inch display and powerful chipset deliver "
        "a gaming experience that rivals phones twice the price. Poco's performance "
        "mode squeezes every drop of speed from the hardware.",

        "Spec hunters will love the Poco {model}: {chipset}, {ram} RAM and {storage} "
        "storage in a no-compromise package. The {battery}mAh battery with "
        "{fast_charge}W fast charging keeps you in the game longer. MIUI for Poco "
        "adds customisation options while keeping the interface snappy and responsive.",

        "The Poco {model} delivers flagship-tier specs without the flagship price. "
        "Powered by {chipset} with {ram} RAM and {storage} storage, it handles "
        "gaming, multitasking and content creation smoothly. The {screen}-inch display "
        "and capable camera system add to the package, making it ideal for "
        "{best_for} on a budget.",

        "Built for performance enthusiasts, the Poco {model} features {chipset}, "
        "{ram} RAM and {storage} storage with {fast_charge}W fast charging. Its "
        "{battery}mAh battery handles extended gaming sessions, while the aggressive "
        "pricing makes premium Android performance accessible to everyone. "
        "The ideal choice for value-driven {best_for}.",
    ],

    "Gionee": [
        "The Gionee {model} delivers reliable everyday performance with {chipset}, "
        "{ram} RAM and {storage} storage. Its slim {material} design and large "
        "{battery}mAh battery make it a practical companion for long days. "
        "Gionee's optimised software ensures smooth performance across social media, "
        "calls and everyday applications.",

        "Designed for everyday reliability, the Gionee {model} pairs {chipset} "
        "with {ram} RAM and {storage} storage in a comfortable {material} body. "
        "The {screen}-inch display provides clear visuals for browsing and video "
        "calls, while the {battery}mAh battery delivers extended usage between "
        "charges. A dependable budget choice for {best_for}.",

        "The Gionee {model} offers solid value with {chipset}, {ram} RAM and "
        "{storage} storage at a wallet-friendly price. Its large-capacity "
        "{battery}mAh battery outlasts most competitors, ensuring you stay connected "
        "throughout the day. The dual-SIM support makes it practical for Nigerian "
        "users managing multiple network lines.",

        "Built for the budget-conscious user, the Gionee {model} provides {chipset} "
        "processing, {ram} RAM and {storage} storage with a focus on battery "
        "longevity. The {screen}-inch display and straightforward interface deliver "
        "a no-fuss smartphone experience ideal for first-time smartphone owners and "
        "{best_for} users seeking reliability.",
    ],

    "Alcatel": [
        "The Alcatel {model} makes smartphone ownership accessible with {chipset}, "
        "{ram} RAM and {storage} storage at an entry-level price. Its simple, "
        "intuitive interface is perfect for first-time smartphone users, while the "
        "{battery}mAh battery provides dependable all-day power. A practical and "
        "affordable choice for everyday communication.",

        "With {chipset}, {ram} RAM and {storage} storage, the Alcatel {model} "
        "delivers the essentials of modern smartphone life without breaking the "
        "budget. The {screen}-inch display offers comfortable viewing, and the "
        "dual-SIM capability suits Nigerian users who manage multiple lines. "
        "Reliable and straightforward — perfect for {best_for}.",

        "The Alcatel {model} provides reliable everyday performance with {chipset} "
        "processing, {ram} RAM and {storage} storage. Its lightweight {material} "
        "design is comfortable to carry all day, and the {battery}mAh battery "
        "handles standard daily use with ease. An ideal starter smartphone for "
        "those upgrading from a feature phone.",

        "Affordable doesn't mean basic with the Alcatel {model}. Featuring {chipset}, "
        "{ram} RAM and {storage} storage, it handles social media, WhatsApp and "
        "everyday browsing smoothly. The {screen}-inch display and simple Android "
        "interface make it easy to use straight out of the box — a great value "
        "pick for {best_for}.",
    ],

    "Wiko": [
        "The Wiko {model} brings French-inspired style to the budget smartphone "
        "segment. Featuring {chipset}, {ram} RAM and {storage} storage, it delivers "
        "the essentials of modern mobile life in a colourful, eye-catching design. "
        "Its {battery}mAh battery and {screen}-inch display make it a stylish, "
        "practical companion for everyday use.",

        "With {chipset}, {ram} RAM and {storage} storage, the Wiko {model} covers "
        "all daily smartphone needs at an affordable price. The vivid {screen}-inch "
        "display and dual-SIM support make it well-suited for Nigerian users who "
        "value both style and practicality. A great entry-level pick for "
        "{best_for}.",

        "The Wiko {model} combines playful European design with reliable {chipset} "
        "performance, {ram} RAM and {storage} storage. Its {battery}mAh battery "
        "ensures all-day usage, while the camera captures everyday moments "
        "clearly. Ideal for students, first-time smartphone buyers and anyone "
        "seeking a stylish, budget-friendly device.",

        "Offering {chipset}, {ram} RAM and {storage} storage at a competitive price, "
        "the Wiko {model} delivers a capable and stylish everyday smartphone. "
        "The {screen}-inch display and intuitive interface make navigation effortless, "
        "while Wiko's distinctive design sets it apart in the entry-level market. "
        "Perfect for {best_for} on a tight budget.",
    ],

    "ZTE": [
        "The ZTE {model} leverages the brand's deep telecom expertise to deliver "
        "strong connectivity and reliable performance. Featuring {chipset}, {ram} RAM "
        "and {storage} storage, it handles everyday tasks with ease. The "
        "{battery}mAh battery and {screen}-inch display provide a comfortable "
        "daily experience at a compelling price point.",

        "With {chipset}, {ram} RAM and {storage} storage, the ZTE {model} delivers "
        "efficient, reliable performance for everyday use. ZTE's telecom heritage "
        "ensures excellent signal reception and call quality, making it a dependable "
        "choice for users who prioritise connectivity. Ideal for {best_for}.",

        "The ZTE {model} offers {chipset} processing, {ram} RAM and {storage} "
        "storage with a focus on network performance and battery efficiency. "
        "The {screen}-inch display and Android interface provide a straightforward "
        "user experience, while the {battery}mAh battery delivers extended usage. "
        "A solid, no-nonsense choice for value-conscious buyers.",

        "Combining reliability with affordability, the ZTE {model} features {chipset}, "
        "{ram} RAM and {storage} storage. Its dual-SIM capability and strong network "
        "performance make it ideal for Nigerian users managing multiple lines. "
        "The {battery}mAh battery and practical camera system cover all daily needs "
        "for {best_for}.",
    ],

    "Google": [
        "The {model} sets the benchmark for pure Android with Google's own Tensor "
        "chip ({chipset}), {ram} RAM and {storage} storage. Its AI-powered camera "
        "system produces stunning photos in any lighting condition, leveraging "
        "computational photography that no other phone can match. Seven years of "
        "Android OS and security updates ensure long-term investment value.",

        "Powered by {chipset}, {ram} RAM and {storage} storage, the {model} delivers "
        "the definitive Android experience directly from Google. Real-time translation, "
        "Call Screen and Magic Eraser are just some of the AI features that make "
        "everyday life smarter. The {screen}-inch display and flagship camera "
        "system make it the benchmark for {best_for}.",

        "The {model} showcases Google's vision of the perfect smartphone: {chipset}, "
        "{ram} RAM, {storage} storage and an AI camera that consistently tops camera "
        "rankings. Guaranteed seven years of OS and security updates provide "
        "unmatched longevity. The clean Android interface and Pixel-exclusive "
        "features deliver a uniquely intelligent daily experience.",

        "For the purist who wants the best of Android, the {model} delivers {chipset}, "
        "{ram} RAM and {storage} storage with Google's industry-leading camera "
        "intelligence. Features like Photo Unblur, Night Sight and Magic Eraser "
        "transform ordinary photos into professional shots. The fastest Android "
        "updates and longest software support make it the smart long-term buy.",
    ],

    "Sony": [
        "The Sony {model} brings cinematic excellence to mobile photography with "
        "{chipset}, {ram} RAM and {storage} storage. ZEISS optics and Sony's "
        "advanced image processing deliver unparalleled photo and video quality. "
        "The {screen}-inch 4K HDR display with up to 120Hz refresh rate provides "
        "a genuinely cinematic viewing experience.",

        "Engineered for creators and media enthusiasts, the Sony {model} features "
        "{chipset}, {ram} RAM and {storage} storage. Its ZEISS-branded camera system "
        "with 4K HDR video recording sets new standards for mobile filmmaking. "
        "The ultra-wide, side-mounted fingerprint sensor and IP68 water resistance "
        "add practical durability to a premium package.",

        "The Sony {model} combines {chipset} processing with {ram} RAM and {storage} "
        "storage in Sony's distinctive tall, ergonomic form factor. The "
        "{screen}-inch display with accurate colours supports HDR content with "
        "breathtaking clarity. Sony's Creator mode ensures content looks exactly "
        "as the director intended — ideal for {best_for}.",

        "With {chipset}, {ram} RAM and {storage} storage, the Sony {model} delivers "
        "a premium multimedia experience backed by Sony's decades of imaging "
        "expertise. ZEISS optics, optical image stabilisation and 4K video capture "
        "produce professional-quality content. The long-lasting {battery}mAh battery "
        "and IP68 rating add to its premium credentials.",
    ],
}


def phone_desc(brand, model, chipset, ram, storage, screen, battery, best_for, tier, fast_charge):
    templates = BRAND_DESCS.get(brand, BRAND_DESCS["Nokia"])
    t = templates[hash(model + ram + storage) % len(templates)]
    return t.format(
        model=model, chipset=chipset, ram=ram, storage=storage,
        screen=screen, battery=battery, best_for=best_for,
        tier=tier.lower(), fast_charge=fast_charge, material="glass/plastic"
    )


def make_phone(brand, model, year, screen, display, chipset, battery, fast_charge,
               rear_cam, front_cam, nfc, water_res, material, best_for,
               configs):
    """
    configs = list of (ram, storage, network, price_tier, price_naira)
    Returns list of (brand, model, variant, sku, desc, attrs) tuples.
    """
    products = []
    for (ram, storage, network, price_tier, price) in configs:
        variant  = f"{storage} {ram} {network}"
        sku      = slugify(f"{brand}_{model}_{ram}_{storage}_{network}_{year}")
        desc     = phone_desc(brand, model, chipset, ram, storage, screen,
                              battery, best_for, price_tier, fast_charge)
        attrs = {
            "price_category":           price_tier,
            "network_type":             network,
            "ram":                      ram,
            "storage":                  storage,
            "release_year":             str(year),
            "screen_size_inches":       str(screen),
            "display_type":             display,
            "chipset_model":            chipset,
            "battery_capacity_mah":     str(battery),
            "fast_charging_watts":      str(fast_charge),
            "rear_camera_main_mp":      str(rear_cam),
            "front_camera_mp":          str(front_cam),
            "nfc":                      nfc,
            "water_resistance":         water_res,
            "body_material":            material,
            "best_for":                 best_for,
            "nigeria_market_price_naira": str(price),
        }
        products.append((brand, model, variant, sku, desc, attrs))
    return products


P = make_phone  # shorthand


# ─────────────────────────────────────────────────────────────────────────────
# BRAND GENERATORS
# ─────────────────────────────────────────────────────────────────────────────



def _gen_brand(brand, model_list):
    """model_list = list of make_phone(...) calls, each returning a list of tuples."""
    results = []
    for phone_variants in model_list:
        results.extend(phone_variants)
    return results


def gen_all_phones():
    B = lambda b: b
    all_phones = []

    # ── Nokia ─────────────────────────────────────────────────────────────────
    nokia = _gen_brand("Nokia", [
        P("Nokia","Nokia C12",2023,6.3,"IPS LCD","Unisoc SC9863A",3000,10,8,5,"No","None","Plastic","first-time users",[("2GB","32GB","4G","Budget",48000),("3GB","32GB","4G","Budget",58000),("3GB","64GB","4G","Budget",68000)]),
        P("Nokia","Nokia C21",2022,6.5,"IPS LCD","Unisoc SC9863A",4000,10,8,5,"No","None","Plastic","first-time users",[("2GB","32GB","4G","Budget",52000),("2GB","64GB","4G","Budget",60000),("3GB","64GB","4G","Budget",72000)]),
        P("Nokia","Nokia C21 Plus",2022,6.5,"IPS LCD","Unisoc SC9863A",5050,10,13,8,"No","None","Plastic","battery life",[("2GB","32GB","4G","Budget",58000),("3GB","32GB","4G","Budget",65000),("3GB","64GB","4G","Budget",78000)]),
        P("Nokia","Nokia C31",2022,6.7,"IPS LCD","Unisoc SC9863A",5050,10,13,8,"No","None","Plastic","battery life",[("2GB","32GB","4G","Budget",62000),("3GB","64GB","4G","Budget",82000),("4GB","64GB","4G","Budget",95000)]),
        P("Nokia","Nokia C32",2023,6.5,"IPS LCD","Unisoc T606",5000,10,50,8,"No","None","Plastic","everyday use",[("4GB","64GB","4G","Budget",75000),("4GB","128GB","4G","Budget",88000),("6GB","128GB","4G","Budget",105000)]),
        P("Nokia","Nokia C42",2023,6.56,"IPS LCD","Unisoc T606",5000,18,50,8,"No","None","Plastic","everyday use",[("4GB","64GB","4G","Budget",80000),("4GB","128GB","4G","Budget",95000),("6GB","128GB","4G","Budget",112000)]),
        P("Nokia","Nokia G11 Plus",2022,6.5,"IPS LCD","Unisoc T606",5000,18,50,8,"No","None","Plastic","students",[("4GB","64GB","4G","Budget",82000),("4GB","128GB","4G","Budget",98000)]),
        P("Nokia","Nokia G21",2022,6.5,"IPS LCD","Unisoc T606",5050,18,50,8,"No","None","Plastic","students",[("4GB","64GB","4G","Budget",85000),("4GB","128GB","4G","Budget",98000),("6GB","128GB","4G","Budget",118000)]),
        P("Nokia","Nokia G22",2023,6.52,"IPS LCD","Unisoc T606",5050,20,50,8,"No","None","Plastic","repairability",[("4GB","64GB","4G","Budget",90000),("4GB","128GB","4G","Budget",108000),("6GB","128GB","4G","Budget",128000)]),
        P("Nokia","Nokia G42 5G",2023,6.56,"IPS LCD","Snapdragon 480+",4500,20,50,8,"Yes","IP52","Plastic","5G value",[("6GB","128GB","5G","Midrange",148000),("8GB","128GB","5G","Midrange",168000),("8GB","256GB","5G","Midrange",195000)]),
        P("Nokia","Nokia G60 5G",2022,6.58,"IPS LCD","Snapdragon 695",4500,20,50,8,"Yes","IP52","Plastic","everyday 5G",[("4GB","128GB","5G","Midrange",158000),("6GB","128GB","5G","Midrange",178000),("6GB","256GB","5G","Midrange",205000)]),
        P("Nokia","Nokia X20",2021,6.67,"IPS LCD","Snapdragon 480",4470,18,64,32,"Yes","None","Plastic","video calling",[("6GB","128GB","5G","Midrange",155000),("8GB","128GB","5G","Midrange",175000)]),
        P("Nokia","Nokia X30 5G",2022,6.43,"PureDisplay","Snapdragon 695",4200,33,50,16,"Yes","IP67","Recycled Plastic","eco-conscious",[("6GB","128GB","5G","Midrange",188000),("8GB","256GB","5G","Midrange",225000)]),
        P("Nokia","Nokia 5.4",2021,6.39,"IPS LCD","Snapdragon 662",4000,18,48,16,"Yes","None","Plastic","all-round",[("4GB","64GB","4G","Budget",92000),("4GB","128GB","4G","Budget",112000),("6GB","128GB","4G","Midrange",132000)]),
        P("Nokia","Nokia 6.3",2021,6.44,"IPS LCD","Snapdragon 720G",4000,18,64,8,"Yes","None","Plastic","photography",[("4GB","64GB","4G","Budget",105000),("4GB","128GB","4G","Midrange",125000),("6GB","128GB","4G","Midrange",148000)]),
        P("Nokia","Nokia 7.2",2020,6.3,"PureDisplay","Snapdragon 660",3500,18,48,20,"Yes","None","Aluminium","professionals",[("4GB","64GB","4G","Midrange",118000),("6GB","128GB","4G","Midrange",145000)]),
        P("Nokia","Nokia 8.3 5G",2020,6.81,"IPS LCD","Snapdragon 765G",4500,18,64,24,"Yes","None","Aluminium","flagship 5G",[("8GB","128GB","5G","Premium",245000),("8GB","256GB","5G","Premium",285000)]),
        P("Nokia","Nokia XR21",2023,6.49,"IPS LCD","Snapdragon 695",4800,18,64,8,"Yes","IP68","Aluminium","rugged outdoor",[("6GB","128GB","5G","Midrange",195000),("8GB","128GB","5G","Midrange",225000),("8GB","256GB","5G","Midrange",260000)]),
    ])
    all_phones.extend(nokia)
    print(f"  Nokia: {len(nokia)}")

    # ── Huawei ────────────────────────────────────────────────────────────────
    huawei = _gen_brand("Huawei", [
        P("Huawei","Huawei Y6p",2020,6.3,"IPS LCD","Helio P35",5000,10,13,8,"No","None","Plastic","battery life",[("3GB","64GB","4G","Budget",65000),("4GB","64GB","4G","Budget",78000)]),
        P("Huawei","Huawei Y7a",2020,6.67,"IPS LCD","Helio P65",5000,22,48,8,"No","None","Plastic","social media",[("4GB","128GB","4G","Budget",88000),("6GB","128GB","4G","Budget",105000)]),
        P("Huawei","Huawei Y8p",2020,6.3,"AMOLED","Kirin 710F",4000,22,48,16,"No","None","Plastic","selfie",[("6GB","128GB","4G","Midrange",118000),("8GB","128GB","4G","Midrange",138000)]),
        P("Huawei","Huawei Y9a",2020,6.63,"IPS LCD","Kirin 800",4200,40,64,16,"No","None","Plastic","gaming",[("6GB","128GB","4G","Midrange",135000),("8GB","128GB","4G","Midrange",155000)]),
        P("Huawei","Huawei Nova 8i",2021,6.67,"IPS LCD","Snapdragon 662",4300,66,64,16,"No","None","Plastic","all-round",[("6GB","128GB","4G","Midrange",145000),("8GB","128GB","4G","Midrange",168000)]),
        P("Huawei","Huawei Nova 9",2021,6.57,"OLED","Snapdragon 778G",4300,66,50,32,"No","None","Glass","photography",[("8GB","128GB","4G","Midrange",195000),("8GB","256GB","4G","Midrange",228000)]),
        P("Huawei","Huawei Nova 10",2022,6.67,"OLED","Snapdragon 778G 4G",4000,66,50,60,"No","None","Glass","selfie",[("8GB","128GB","4G","Midrange",215000),("8GB","256GB","4G","Midrange",248000)]),
        P("Huawei","Huawei Nova 11",2023,6.7,"OLED","Snapdragon 778G",4500,66,50,60,"No","None","Glass","selfie photography",[("8GB","256GB","4G","Midrange",255000),("12GB","256GB","4G","Premium",295000)]),
        P("Huawei","Huawei Nova 11 Pro",2023,6.78,"OLED","Snapdragon 778G",4500,100,50,60,"Yes","None","Glass","premium selfie",[("8GB","256GB","4G","Premium",320000),("12GB","512GB","4G","Premium",385000)]),
        P("Huawei","Huawei P50",2021,6.5,"OLED","Kirin 9000",4100,66,50,13,"No","IP68","Glass","Leica photography",[("8GB","128GB","4G","Premium",385000),("8GB","256GB","4G","Premium",445000)]),
        P("Huawei","Huawei P50 Pro",2021,6.6,"OLED","Kirin 9000",4360,66,50,13,"Yes","IP68","Glass","pro photography",[("8GB","256GB","4G","Premium",495000),("12GB","512GB","4G","Flagship",585000)]),
        P("Huawei","Huawei P60 Pro",2023,6.67,"OLED","Snapdragon 8+ Gen 1 4G",4815,88,48,13,"Yes","IP68","Glass","Leica pro camera",[("8GB","256GB","4G","Premium",545000),("12GB","256GB","4G","Flagship",625000),("12GB","512GB","4G","Flagship",725000)]),
        P("Huawei","Huawei Mate 50",2022,6.7,"OLED","Snapdragon 8+ Gen 1 4G",4460,66,50,13,"Yes","IP65","Glass","business flagship",[("8GB","256GB","4G","Flagship",595000),("12GB","512GB","4G","Flagship",695000)]),
        P("Huawei","Huawei Mate 50 Pro",2022,6.74,"OLED","Snapdragon 8+ Gen 1 4G",4700,66,50,13,"Yes","IP68","Ceramic","ultimate flagship",[("8GB","256GB","4G","Flagship",695000),("12GB","512GB","4G","Flagship",825000)]),
        P("Huawei","Huawei Mate 60 Pro",2023,6.82,"LTPO OLED","Kirin 9000S",5000,88,50,13,"Yes","IP68","Titanium","ultimate flagship",[("12GB","256GB","4G","Flagship",895000),("12GB","512GB","4G","Flagship",1050000)]),
    ])
    all_phones.extend(huawei)
    print(f"  Huawei: {len(huawei)}")

    # ── Honor ─────────────────────────────────────────────────────────────────
    honor = _gen_brand("Honor", [
        P("Honor","Honor X6",2022,6.5,"IPS LCD","Helio G25",5000,10,13,5,"No","None","Plastic","budget value",[("4GB","64GB","4G","Budget",62000),("4GB","128GB","4G","Budget",75000)]),
        P("Honor","Honor X7",2022,6.74,"IPS LCD","Snapdragon 680",5000,22,48,8,"No","None","Plastic","battery life",[("4GB","128GB","4G","Budget",82000),("6GB","128GB","4G","Budget",98000)]),
        P("Honor","Honor X8",2022,6.7,"IPS LCD","Snapdragon 680",4000,22,64,16,"No","None","Plastic","all-round",[("6GB","128GB","4G","Budget",98000),("8GB","128GB","4G","Midrange",118000)]),
        P("Honor","Honor X8a",2023,6.7,"IPS LCD","Helio G88",4500,35,100,16,"No","None","Plastic","selfie",[("6GB","128GB","4G","Budget",105000),("8GB","128GB","4G","Midrange",125000)]),
        P("Honor","Honor X9a",2023,6.67,"AMOLED","Snapdragon 695",5100,35,64,16,"Yes","IP53","Plastic","gaming",[("8GB","256GB","5G","Midrange",155000),("8GB","512GB","5G","Midrange",185000)]),
        P("Honor","Honor X9b",2023,6.78,"AMOLED","Snapdragon 6 Gen 1",5230,35,108,16,"Yes","IP53","Plastic","gaming",[("8GB","256GB","5G","Midrange",165000),("12GB","256GB","5G","Midrange",195000)]),
        P("Honor","Honor 70",2022,6.67,"OLED","Snapdragon 778G+",4800,66,54,50,"No","None","Glass","photography",[("6GB","128GB","5G","Midrange",178000),("8GB","256GB","5G","Midrange",215000)]),
        P("Honor","Honor 80",2022,6.67,"OLED","Snapdragon 782G",4800,66,54,50,"No","None","Glass","all-round",[("8GB","256GB","5G","Midrange",225000),("12GB","256GB","5G","Midrange",265000)]),
        P("Honor","Honor 90",2023,6.72,"AMOLED","Snapdragon 7 Gen 1 Ace",5000,66,200,50,"No","IP54","Glass","photography flagship",[("8GB","256GB","5G","Midrange",248000),("12GB","256GB","5G","Premium",288000),("12GB","512GB","5G","Premium",335000)]),
        P("Honor","Honor 90 Pro",2023,6.78,"AMOLED","Snapdragon 8+ Gen 1",5000,66,200,50,"Yes","IP54","Glass","pro photography",[("12GB","256GB","5G","Premium",325000),("16GB","512GB","5G","Premium",395000)]),
        P("Honor","Honor Magic 5",2023,6.73,"OLED","Snapdragon 8 Gen 2",5100,66,50,12,"Yes","IP54","Glass","flagship",[("8GB","256GB","5G","Flagship",445000),("12GB","256GB","5G","Flagship",525000)]),
        P("Honor","Honor Magic 5 Pro",2023,6.81,"LTPO OLED","Snapdragon 8 Gen 2",5450,66,50,12,"Yes","IP68","Ceramic","ultimate flagship",[("12GB","256GB","5G","Flagship",545000),("12GB","512GB","5G","Flagship",645000)]),
        P("Honor","Honor Magic 6 Pro",2024,6.8,"LTPO OLED","Snapdragon 8 Gen 3",5600,80,180,50,"Yes","IP68","Titanium","2024 flagship",[("12GB","256GB","5G","Flagship",625000),("16GB","512GB","5G","Flagship",745000)]),
    ])
    all_phones.extend(honor)
    print(f"  Honor: {len(honor)}")

    # ── Realme ────────────────────────────────────────────────────────────────
    realme = _gen_brand("Realme", [
        P("Realme","Realme C30",2022,6.5,"IPS LCD","Unisoc T612",5000,10,8,5,"No","None","Plastic","first-time users",[("2GB","32GB","4G","Budget",45000),("3GB","32GB","4G","Budget",52000),("4GB","64GB","4G","Budget",62000)]),
        P("Realme","Realme C33",2022,6.5,"IPS LCD","Unisoc T612",5000,10,50,5,"No","None","Plastic","budget value",[("3GB","32GB","4G","Budget",55000),("4GB","64GB","4G","Budget",68000),("4GB","128GB","4G","Budget",82000)]),
        P("Realme","Realme C35",2022,6.6,"IPS LCD","Unisoc T616",5000,18,50,8,"No","None","Plastic","all-round",[("4GB","64GB","4G","Budget",72000),("4GB","128GB","4G","Budget",88000),("6GB","128GB","4G","Budget",105000)]),
        P("Realme","Realme C51",2023,6.74,"IPS LCD","Unisoc T612",5000,33,50,5,"No","None","Plastic","fast charge budget",[("4GB","64GB","4G","Budget",68000),("4GB","128GB","4G","Budget",82000),("6GB","128GB","4G","Budget",98000)]),
        P("Realme","Realme C55",2023,6.72,"IPS LCD","Helio G88",5000,33,64,8,"No","None","Plastic","all-round",[("6GB","128GB","4G","Budget",88000),("8GB","256GB","4G","Budget",108000)]),
        P("Realme","Realme Narzo 50",2022,6.6,"IPS LCD","Helio G96",5000,33,50,16,"No","None","Plastic","gaming",[("4GB","64GB","4G","Budget",88000),("4GB","128GB","4G","Budget",102000),("6GB","128GB","4G","Midrange",122000)]),
        P("Realme","Realme Narzo 50 Pro 5G",2022,6.4,"Super AMOLED","Dimensity 920",5000,33,48,16,"No","None","Plastic","5G gaming",[("6GB","128GB","5G","Midrange",135000),("8GB","128GB","5G","Midrange",155000)]),
        P("Realme","Realme Narzo 60",2023,6.43,"AMOLED","Dimensity 6020",5000,33,64,16,"No","IP54","Plastic","5G budget",[("6GB","128GB","5G","Midrange",128000),("8GB","128GB","5G","Midrange",148000),("8GB","256GB","5G","Midrange",172000)]),
        P("Realme","Realme Narzo 60 Pro",2023,6.7,"Super AMOLED","Dimensity 7050",5000,67,100,16,"No","IP54","Plastic","5G all-round",[("8GB","128GB","5G","Midrange",165000),("8GB","256GB","5G","Midrange",195000),("12GB","256GB","5G","Midrange",228000)]),
        P("Realme","Realme 10",2022,6.5,"Super AMOLED","Helio G99",5000,33,50,16,"No","None","Plastic","AMOLED budget",[("4GB","64GB","4G","Budget",92000),("4GB","128GB","4G","Budget",108000),("6GB","128GB","4G","Midrange",128000),("8GB","256GB","4G","Midrange",155000)]),
        P("Realme","Realme 10 Pro+ 5G",2022,6.7,"Super AMOLED","Dimensity 1080",5000,67,108,16,"No","None","Plastic","premium AMOLED",[("6GB","128GB","5G","Midrange",148000),("8GB","128GB","5G","Midrange",172000),("8GB","256GB","5G","Midrange",205000)]),
        P("Realme","Realme 11",2023,6.43,"Super AMOLED","Helio G99",5000,67,108,16,"No","None","Plastic","photography",[("6GB","128GB","4G","Midrange",135000),("8GB","256GB","4G","Midrange",165000)]),
        P("Realme","Realme 11 Pro+ 5G",2023,6.7,"Super AMOLED","Dimensity 7050",5000,100,200,32,"No","IP54","Glass","flagship camera",[("8GB","256GB","5G","Midrange",188000),("12GB","256GB","5G","Premium",228000),("12GB","512GB","5G","Premium",275000)]),
        P("Realme","Realme 12 Pro+ 5G",2024,6.7,"Super AMOLED","Snapdragon 7s Gen 2",5000,67,50,32,"Yes","IP65","Glass","zoom photography",[("8GB","256GB","5G","Midrange",215000),("12GB","256GB","5G","Premium",255000),("12GB","512GB","5G","Premium",305000)]),
        P("Realme","Realme GT Neo 5",2023,6.74,"AMOLED","Snapdragon 8+ Gen 1",5000,240,50,16,"No","None","Plastic","fastest charging",[("8GB","256GB","5G","Premium",285000),("16GB","256GB","5G","Premium",355000),("16GB","1TB","5G","Flagship",445000)]),
        P("Realme","Realme GT 2 Pro",2022,6.7,"LTPO AMOLED","Snapdragon 8 Gen 1",5000,65,50,32,"Yes","None","Bio-based Plastic","flagship",[("8GB","128GB","5G","Premium",295000),("12GB","256GB","5G","Flagship",365000)]),
    ])
    all_phones.extend(realme)
    print(f"  Realme: {len(realme)}")

    # ── Vivo ─────────────────────────────────────────────────────────────────
    vivo = _gen_brand("Vivo", [
        P("Vivo","Vivo Y16",2022,6.51,"IPS LCD","Helio P35",5000,10,13,8,"No","None","Plastic","battery life",[("3GB","32GB","4G","Budget",55000),("4GB","64GB","4G","Budget",68000),("4GB","128GB","4G","Budget",82000)]),
        P("Vivo","Vivo Y22",2022,6.55,"IPS LCD","Helio G85",5000,18,50,8,"No","None","Plastic","everyday use",[("4GB","64GB","4G","Budget",72000),("4GB","128GB","4G","Budget",88000),("6GB","128GB","4G","Budget",105000)]),
        P("Vivo","Vivo Y35",2022,6.58,"AMOLED","Snapdragon 680",5000,44,50,16,"No","None","Plastic","AMOLED budget",[("8GB","128GB","4G","Midrange",118000),("8GB","256GB","4G","Midrange",142000)]),
        P("Vivo","Vivo Y36",2023,6.64,"IPS LCD","Snapdragon 680",5000,44,50,16,"No","None","Plastic","all-round",[("8GB","128GB","4G","Midrange",125000),("8GB","256GB","4G","Midrange",148000)]),
        P("Vivo","Vivo Y36 5G",2023,6.64,"IPS LCD","Dimensity 6020",5000,44,50,16,"No","None","Plastic","5G budget",[("8GB","128GB","5G","Midrange",138000),("8GB","256GB","5G","Midrange",162000)]),
        P("Vivo","Vivo Y78 5G",2023,6.78,"AMOLED","Dimensity 7020",5000,44,64,16,"No","IP54","Plastic","5G photography",[("8GB","256GB","5G","Midrange",168000),("12GB","256GB","5G","Midrange",198000)]),
        P("Vivo","Vivo V27",2023,6.78,"AMOLED","Helio G99",4600,44,64,50,"No","IP54","Glass","selfie photography",[("8GB","256GB","4G","Midrange",195000),("12GB","256GB","4G","Midrange",228000)]),
        P("Vivo","Vivo V27e",2023,6.78,"AMOLED","Helio G99",4800,44,64,32,"No","None","Glass","photography",[("8GB","256GB","4G","Midrange",178000),("8GB","512GB","4G","Midrange",215000)]),
        P("Vivo","Vivo V29",2023,6.78,"AMOLED","Snapdragon 778G",4600,80,50,50,"Yes","IP64","Glass","premium selfie",[("8GB","256GB","4G","Midrange",225000),("12GB","256GB","4G","Premium",265000)]),
        P("Vivo","Vivo V29e 5G",2023,6.78,"AMOLED","Dimensity 6020",5000,44,64,32,"No","IP54","Glass","5G photography",[("8GB","256GB","5G","Midrange",205000),("12GB","256GB","5G","Midrange",245000)]),
        P("Vivo","Vivo X80",2022,6.78,"AMOLED","Dimensity 9000",4500,80,50,12,"Yes","None","Glass","Zeiss photography",[("8GB","256GB","5G","Premium",345000),("12GB","256GB","5G","Premium",395000)]),
        P("Vivo","Vivo X80 Pro",2022,6.78,"LTPO AMOLED","Snapdragon 8 Gen 1",4700,80,50,32,"Yes","IP68","Ceramic","flagship photography",[("12GB","256GB","5G","Flagship",495000),("12GB","512GB","5G","Flagship",585000)]),
        P("Vivo","Vivo X90",2022,6.78,"AMOLED","Dimensity 9200",4810,120,50,32,"Yes","None","Glass","flagship camera",[("8GB","256GB","5G","Flagship",445000),("12GB","256GB","5G","Flagship",525000)]),
        P("Vivo","Vivo X90 Pro",2022,6.78,"LTPO AMOLED","Dimensity 9200",4870,120,50,32,"Yes","IP68","Ceramic","pro flagship",[("12GB","256GB","5G","Flagship",545000),("12GB","512GB","5G","Flagship",645000)]),
        P("Vivo","Vivo X100",2023,6.78,"AMOLED","Dimensity 9300",5000,120,50,32,"Yes","IP68","Glass","2023 flagship",[("12GB","256GB","5G","Flagship",595000),("16GB","512GB","5G","Flagship",745000)]),
    ])
    all_phones.extend(vivo)
    print(f"  Vivo: {len(vivo)}")

    # ── OnePlus ───────────────────────────────────────────────────────────────
    oneplus = _gen_brand("OnePlus", [
        P("OnePlus","OnePlus Nord CE 2 Lite 5G",2022,6.59,"IPS LCD","Snapdragon 695",5000,33,64,16,"No","None","Plastic","5G value",[("6GB","128GB","5G","Midrange",135000),("8GB","128GB","5G","Midrange",155000)]),
        P("OnePlus","OnePlus Nord CE 3 Lite 5G",2023,6.72,"IPS LCD","Snapdragon 695",5000,67,108,16,"No","None","Plastic","fast charge 5G",[("8GB","128GB","5G","Midrange",148000),("8GB","256GB","5G","Midrange",175000)]),
        P("OnePlus","OnePlus Nord CE 3 5G",2023,6.7,"Super AMOLED","Dimensity 7050",5000,80,50,32,"No","IP54","Plastic","premium mid",[("8GB","128GB","5G","Midrange",195000),("12GB","256GB","5G","Midrange",238000)]),
        P("OnePlus","OnePlus Nord 3 5G",2023,6.74,"Super AMOLED","Dimensity 9000",5000,80,50,16,"No","IP54","Plastic","flagship mid",[("8GB","128GB","5G","Midrange",235000),("12GB","256GB","5G","Premium",285000),("16GB","256GB","5G","Premium",335000)]),
        P("OnePlus","OnePlus Nord 4 5G",2024,6.74,"LTPO AMOLED","Snapdragon 7+ Gen 3",5500,100,50,16,"Yes","IP65","Metal","metal mid-range",[("8GB","128GB","5G","Midrange",255000),("12GB","256GB","5G","Premium",308000),("16GB","512GB","5G","Premium",368000)]),
        P("OnePlus","OnePlus 11",2023,6.7,"LTPO AMOLED","Snapdragon 8 Gen 2",5000,100,50,16,"Yes","IP64","Glass","flagship",[("8GB","128GB","5G","Flagship",445000),("12GB","256GB","5G","Flagship",525000),("16GB","256GB","5G","Flagship",595000)]),
        P("OnePlus","OnePlus 11R 5G",2023,6.74,"Super AMOLED","Snapdragon 8+ Gen 1",5000,100,50,16,"No","None","Glass","flagship value",[("8GB","128GB","5G","Premium",365000),("16GB","256GB","5G","Flagship",445000)]),
        P("OnePlus","OnePlus 12",2024,6.82,"LTPO AMOLED","Snapdragon 8 Gen 3",5400,100,50,32,"Yes","IP65","Glass","2024 flagship",[("12GB","256GB","5G","Flagship",545000),("16GB","256GB","5G","Flagship",645000),("16GB","512GB","5G","Flagship",745000)]),
        P("OnePlus","OnePlus 12R 5G",2024,6.78,"LTPO AMOLED","Snapdragon 8 Gen 2",5500,100,50,16,"No","IP54","Glass","flagship lite",[("8GB","128GB","5G","Premium",385000),("16GB","256GB","5G","Flagship",465000)]),
    ])
    all_phones.extend(oneplus)
    print(f"  OnePlus: {len(oneplus)}")

    # ── Motorola ──────────────────────────────────────────────────────────────
    motorola = _gen_brand("Motorola", [
        P("Motorola","Moto E13",2023,6.5,"IPS LCD","Unisoc T606",5000,10,13,5,"No","None","Plastic","first-time users",[("2GB","64GB","4G","Budget",45000),("4GB","64GB","4G","Budget",58000),("4GB","128GB","4G","Budget",72000)]),
        P("Motorola","Moto E22",2022,6.5,"IPS LCD","Helio G37",4020,10,16,8,"No","None","Plastic","essentials",[("3GB","32GB","4G","Budget",55000),("4GB","64GB","4G","Budget",68000)]),
        P("Motorola","Moto E22s",2022,6.5,"IPS LCD","Helio G37",4020,10,16,8,"No","None","Plastic","essentials",[("3GB","32GB","4G","Budget",58000),("4GB","64GB","4G","Budget",72000),("4GB","128GB","4G","Budget",88000)]),
        P("Motorola","Moto G13",2023,6.5,"IPS LCD","Helio G85",5000,20,50,8,"No","None","Plastic","all-round",[("4GB","128GB","4G","Budget",72000),("6GB","128GB","4G","Budget",88000)]),
        P("Motorola","Moto G23",2023,6.5,"IPS LCD","Helio G85",5000,30,50,16,"No","None","Plastic","photography",[("8GB","128GB","4G","Budget",88000),("8GB","256GB","4G","Midrange",108000)]),
        P("Motorola","Moto G34 5G",2024,6.5,"IPS LCD","Snapdragon 695",5000,18,50,16,"No","None","Plastic","budget 5G",[("4GB","64GB","5G","Budget",85000),("4GB","128GB","5G","Budget",98000),("8GB","128GB","5G","Midrange",118000)]),
        P("Motorola","Moto G54 5G",2023,6.5,"IPS LCD","Dimensity 7020",6000,33,50,16,"No","IP52","Plastic","battery champion",[("8GB","256GB","5G","Midrange",135000),("12GB","256GB","5G","Midrange",158000)]),
        P("Motorola","Moto G84 5G",2023,6.55,"OLED","Snapdragon 695",5000,33,50,16,"No","IP54","Plastic","OLED mid",[("12GB","256GB","5G","Midrange",162000),("12GB","512GB","5G","Midrange",192000)]),
        P("Motorola","Moto G Power 5G 2024",2024,6.7,"IPS LCD","Snapdragon 4 Gen 2",6000,30,50,16,"No","IP52","Plastic","battery life",[("8GB","256GB","5G","Midrange",148000),("8GB","512GB","5G","Midrange",178000)]),
        P("Motorola","Moto G Stylus 5G 2023",2023,6.6,"IPS LCD","Snapdragon 6 Gen 1",5000,30,50,16,"No","None","Plastic","productivity",[("6GB","256GB","5G","Midrange",165000),("8GB","256GB","5G","Midrange",195000)]),
        P("Motorola","Moto Edge 40",2023,6.55,"pOLED","Dimensity 8020",4400,68,50,32,"Yes","IP68","Vegan Leather","premium mid",[("8GB","256GB","5G","Premium",225000),("12GB","256GB","5G","Premium",265000)]),
        P("Motorola","Moto Edge 40 Pro",2023,6.67,"pOLED","Snapdragon 8 Gen 2",4600,125,50,60,"Yes","IP68","Vegan Leather","flagship",[("12GB","256GB","5G","Flagship",395000),("12GB","512GB","5G","Flagship",465000)]),
        P("Motorola","Moto Edge 40 Neo",2023,6.55,"pOLED","Dimensity 7030",5000,68,50,32,"No","IP68","Vegan Leather","eco flagship",[("8GB","256GB","5G","Midrange",195000),("12GB","256GB","5G","Premium",235000)]),
        P("Motorola","Moto Edge 50 Pro",2024,6.7,"pOLED","Snapdragon 7 Gen 3",4500,125,50,50,"Yes","IP68","Vegan Leather","2024 premium",[("12GB","256GB","5G","Premium",265000),("12GB","512GB","5G","Premium",315000)]),
        P("Motorola","Moto Edge 50 Ultra",2024,6.67,"pOLED","Snapdragon 8s Gen 3",4500,125,50,50,"Yes","IP68","Vegan Leather","2024 flagship",[("12GB","256GB","5G","Flagship",395000),("16GB","512GB","5G","Flagship",475000)]),
    ])
    all_phones.extend(motorola)
    print(f"  Motorola: {len(motorola)}")

    # ── Poco ─────────────────────────────────────────────────────────────────
    poco = _gen_brand("Poco", [
        P("Poco","Poco C50",2022,6.52,"IPS LCD","Helio A22",5000,10,8,5,"No","None","Plastic","entry value",[("2GB","32GB","4G","Budget",42000),("3GB","32GB","4G","Budget",50000)]),
        P("Poco","Poco C55",2023,6.71,"IPS LCD","Helio G85",5000,18,50,5,"No","None","Plastic","budget value",[("4GB","64GB","4G","Budget",55000),("6GB","128GB","4G","Budget",72000)]),
        P("Poco","Poco C65",2023,6.74,"IPS LCD","Helio G85",5000,18,50,8,"No","None","Plastic","budget camera",[("4GB","128GB","4G","Budget",68000),("6GB","128GB","4G","Budget",82000),("8GB","256GB","4G","Budget",98000)]),
        P("Poco","Poco M4 Pro",2022,6.43,"Super AMOLED","Dimensity 810",5000,33,64,16,"No","None","Plastic","AMOLED value",[("6GB","128GB","5G","Midrange",118000),("8GB","256GB","5G","Midrange",145000)]),
        P("Poco","Poco M5",2022,6.58,"IPS LCD","Helio G99",5000,18,50,8,"No","None","Plastic","performance budget",[("4GB","64GB","4G","Budget",82000),("4GB","128GB","4G","Budget",98000),("6GB","128GB","4G","Midrange",118000)]),
        P("Poco","Poco M5s",2022,6.43,"Super AMOLED","Helio G95",5000,33,64,13,"No","None","Plastic","AMOLED gaming",[("4GB","64GB","4G","Budget",92000),("4GB","128GB","4G","Budget",108000),("6GB","128GB","4G","Midrange",128000)]),
        P("Poco","Poco M6 Pro",2024,6.67,"AMOLED","Snapdragon 4 Gen 2",5000,67,64,16,"No","None","Plastic","AMOLED value",[("8GB","256GB","4G","Midrange",128000),("12GB","512GB","4G","Midrange",162000)]),
        P("Poco","Poco X4 Pro 5G",2022,6.67,"Super AMOLED","Snapdragon 695",5000,67,108,16,"No","None","Plastic","5G AMOLED",[("6GB","128GB","5G","Midrange",145000),("8GB","256GB","5G","Midrange",175000)]),
        P("Poco","Poco X5 Pro 5G",2023,6.67,"Super AMOLED","Snapdragon 778G",5000,67,108,16,"No","None","Plastic","flagship value",[("6GB","128GB","5G","Midrange",168000),("8GB","256GB","5G","Midrange",205000)]),
        P("Poco","Poco X6 5G",2024,6.67,"AMOLED","Dimensity 8300-Ultra",5100,67,64,20,"Yes","IP64","Glass","2024 flagship mid",[("8GB","256GB","5G","Midrange",198000),("12GB","256GB","5G","Premium",238000)]),
        P("Poco","Poco X6 Pro 5G",2024,6.67,"AMOLED","Dimensity 8300-Ultra",5100,67,64,20,"Yes","IP64","Glass","performance flagship",[("8GB","256GB","5G","Premium",248000),("12GB","256GB","5G","Premium",295000),("12GB","512GB","5G","Premium",348000)]),
        P("Poco","Poco F5",2023,6.67,"AMOLED","Snapdragon 7+ Gen 2",5000,67,64,16,"Yes","IP53","Glass","flagship killer",[("8GB","256GB","5G","Premium",245000),("12GB","256GB","5G","Premium",295000)]),
        P("Poco","Poco F5 Pro",2023,6.67,"LTPO AMOLED","Snapdragon 8 Gen 2",5160,67,64,16,"Yes","IP53","Glass","true flagship",[("8GB","256GB","5G","Flagship",385000),("12GB","256GB","5G","Flagship",445000)]),
    ])
    all_phones.extend(poco)
    print(f"  Poco: {len(poco)}")

    # ── Gionee ────────────────────────────────────────────────────────────────
    gionee = _gen_brand("Gionee", [
        P("Gionee","Gionee P15 Pro",2022,6.5,"IPS LCD","Unisoc SC9863A",5000,18,13,8,"No","None","Plastic","battery life",[("3GB","64GB","4G","Budget",45000),("4GB","64GB","4G","Budget",55000),("4GB","128GB","4G","Budget",68000)]),
        P("Gionee","Gionee P16",2023,6.5,"IPS LCD","Unisoc T606",5000,18,50,8,"No","None","Plastic","budget value",[("4GB","64GB","4G","Budget",52000),("4GB","128GB","4G","Budget",65000),("6GB","128GB","4G","Budget",78000)]),
        P("Gionee","Gionee F11S Pro",2021,6.55,"IPS LCD","Helio P65",5000,18,16,16,"No","None","Plastic","selfie",[("4GB","64GB","4G","Budget",62000),("4GB","128GB","4G","Budget",78000),("6GB","128GB","4G","Budget",95000)]),
        P("Gionee","Gionee F12",2022,6.5,"IPS LCD","Unisoc T610",4500,18,13,8,"No","None","Plastic","everyday use",[("3GB","32GB","4G","Budget",48000),("4GB","64GB","4G","Budget",60000),("4GB","128GB","4G","Budget",75000)]),
        P("Gionee","Gionee F15 Pro",2022,6.55,"IPS LCD","Helio G85",5000,22,50,16,"No","None","Plastic","photography",[("4GB","64GB","4G","Budget",68000),("4GB","128GB","4G","Budget",82000),("6GB","128GB","4G","Budget",98000)]),
        P("Gionee","Gionee G13 Pro",2022,6.52,"IPS LCD","Unisoc T606",5000,22,13,8,"No","None","Plastic","dual SIM",[("4GB","64GB","4G","Budget",58000),("4GB","128GB","4G","Budget",72000)]),
        P("Gionee","Gionee M12",2021,6.53,"IPS LCD","Helio G85",6000,18,48,13,"No","None","Plastic","battery champion",[("6GB","128GB","4G","Budget",88000),("8GB","128GB","4G","Midrange",108000)]),
        P("Gionee","Gionee M15",2022,6.67,"IPS LCD","Helio G96",6000,33,64,20,"No","None","Plastic","power user",[("6GB","128GB","4G","Midrange",98000),("8GB","128GB","4G","Midrange",118000),("8GB","256GB","4G","Midrange",142000)]),
    ])
    all_phones.extend(gionee)
    print(f"  Gionee: {len(gionee)}")

    # ── Alcatel ───────────────────────────────────────────────────────────────
    alcatel = _gen_brand("Alcatel", [
        P("Alcatel","Alcatel 1L 2021",2021,5.5,"IPS LCD","Helio A22",3000,5,8,5,"No","None","Plastic","first-time users",[("1GB","16GB","4G","Budget",28000),("2GB","32GB","4G","Budget",38000)]),
        P("Alcatel","Alcatel 1S 2021",2021,6.52,"IPS LCD","Helio G25",4000,10,48,5,"No","None","Plastic","budget value",[("2GB","32GB","4G","Budget",38000),("3GB","32GB","4G","Budget",48000)]),
        P("Alcatel","Alcatel 3L 2021",2021,6.52,"IPS LCD","Helio G25",4000,10,48,8,"No","None","Plastic","entry camera",[("4GB","64GB","4G","Budget",52000),("4GB","128GB","4G","Budget",65000)]),
        P("Alcatel","Alcatel 3X 2020",2020,6.52,"IPS LCD","Helio P22",5000,10,48,8,"No","None","Plastic","triple camera",[("4GB","64GB","4G","Budget",55000),("4GB","128GB","4G","Budget",68000)]),
        P("Alcatel","Alcatel 5V",2020,6.58,"IPS LCD","Helio P22",4000,10,48,13,"No","None","Plastic","selfie",[("3GB","32GB","4G","Budget",48000),("4GB","64GB","4G","Budget",62000)]),
        P("Alcatel","Alcatel 1S 2023",2023,6.52,"IPS LCD","Unisoc T606",5000,18,50,8,"No","None","Plastic","reliable budget",[("3GB","64GB","4G","Budget",45000),("4GB","64GB","4G","Budget",55000),("4GB","128GB","4G","Budget",68000)]),
        P("Alcatel","Alcatel 3L 2023",2023,6.74,"IPS LCD","Helio G25",4000,10,50,5,"No","None","Plastic","entry level",[("4GB","64GB","4G","Budget",52000),("4GB","128GB","4G","Budget",65000)]),
    ])
    all_phones.extend(alcatel)
    print(f"  Alcatel: {len(alcatel)}")

    # ── Wiko ────────────────────────────────────────────────────────────────
    wiko = _gen_brand("Wiko", [
        P("Wiko","Wiko T3",2022,6.6,"IPS LCD","Unisoc T606",5000,15,13,8,"No","None","Plastic","style budget",[("2GB","32GB","4G","Budget",38000),("3GB","32GB","4G","Budget",48000),("4GB","64GB","4G","Budget",58000)]),
        P("Wiko","Wiko T10",2023,6.74,"IPS LCD","Unisoc T612",5000,18,50,8,"No","None","Plastic","colourful budget",[("4GB","64GB","4G","Budget",52000),("4GB","128GB","4G","Budget",65000)]),
        P("Wiko","Wiko Y62",2022,6.1,"IPS LCD","Unisoc SC9863A",4000,10,8,5,"No","None","Plastic","compact budget",[("2GB","16GB","4G","Budget",32000),("2GB","32GB","4G","Budget",40000),("3GB","32GB","4G","Budget",48000)]),
        P("Wiko","Wiko Y82",2022,6.6,"IPS LCD","Unisoc SC9863A",5000,10,13,8,"No","None","Plastic","large screen",[("3GB","64GB","4G","Budget",48000),("4GB","64GB","4G","Budget",58000)]),
        P("Wiko","Wiko View 5",2021,6.55,"IPS LCD","Helio G25",5000,15,48,16,"No","None","Plastic","photography",[("4GB","64GB","4G","Budget",58000),("4GB","128GB","4G","Budget",72000)]),
        P("Wiko","Wiko Power U10",2021,6.82,"IPS LCD","Helio G25",6000,10,13,8,"No","None","Plastic","super battery",[("3GB","32GB","4G","Budget",52000),("4GB","64GB","4G","Budget",65000)]),
        P("Wiko","Wiko Power U20",2021,6.82,"IPS LCD","Helio G25",6000,15,13,8,"No","None","Plastic","power user",[("4GB","64GB","4G","Budget",68000),("4GB","128GB","4G","Budget",82000)]),
    ])
    all_phones.extend(wiko)
    print(f"  Wiko: {len(wiko)}")

    # ── ZTE ──────────────────────────────────────────────────────────────────
    zte = _gen_brand("ZTE", [
        P("ZTE","ZTE Blade A53",2022,6.52,"IPS LCD","Unisoc T606",5000,18,50,8,"No","None","Plastic","reliable budget",[("2GB","32GB","4G","Budget",42000),("3GB","64GB","4G","Budget",55000),("4GB","64GB","4G","Budget",68000)]),
        P("ZTE","ZTE Blade A54",2023,6.6,"IPS LCD","Unisoc T606",5000,22,50,8,"No","None","Plastic","everyday use",[("3GB","64GB","4G","Budget",50000),("4GB","128GB","4G","Budget",65000)]),
        P("ZTE","ZTE Blade A72",2022,6.74,"IPS LCD","Helio G25",5000,10,13,8,"No","None","Plastic","large display",[("3GB","64GB","4G","Budget",55000),("4GB","64GB","4G","Budget",68000),("4GB","128GB","4G","Budget",82000)]),
        P("ZTE","ZTE Blade A73",2023,6.74,"IPS LCD","Helio G85",5000,22,50,8,"No","None","Plastic","all-round",[("4GB","128GB","4G","Budget",72000),("6GB","128GB","4G","Budget",88000)]),
        P("ZTE","ZTE Blade V40",2022,6.67,"IPS LCD","Unisoc T618",4000,22,50,8,"No","None","Plastic","connectivity",[("4GB","128GB","4G","Midrange",85000),("6GB","128GB","4G","Midrange",102000)]),
        P("ZTE","ZTE Blade V40 Pro",2022,6.67,"AMOLED","Dimensity 810",4000,33,64,16,"No","None","Plastic","AMOLED value",[("8GB","128GB","5G","Midrange",118000),("8GB","256GB","5G","Midrange",142000)]),
        P("ZTE","ZTE Axon 40 Ultra",2022,6.8,"AMOLED","Snapdragon 8 Gen 1",5000,65,64,16,"Yes","None","Glass","under-display cam",[("8GB","128GB","5G","Premium",295000),("12GB","256GB","5G","Premium",365000)]),
        P("ZTE","ZTE Axon 50 Ultra",2023,6.8,"AMOLED","Snapdragon 8 Gen 2",5100,80,64,16,"Yes","IP68","Glass","2023 flagship",[("12GB","256GB","5G","Flagship",425000),("16GB","512GB","5G","Flagship",525000)]),
        P("ZTE","ZTE nubia Red Magic 8 Pro",2023,6.8,"AMOLED","Snapdragon 8 Gen 2",6000,165,50,16,"No","None","Glass","gaming flagship",[("12GB","256GB","5G","Flagship",465000),("16GB","256GB","5G","Flagship",545000),("16GB","512GB","5G","Flagship",645000)]),
    ])
    all_phones.extend(zte)
    print(f"  ZTE: {len(zte)}")

    # ── Google Pixel ──────────────────────────────────────────────────────────
    pixel = _gen_brand("Google", [
        P("Google","Pixel 7",2022,6.3,"LTPO OLED","Google Tensor G2",4355,20,50,10.8,"Yes","IP68","Aluminium","AI photography",[("8GB","128GB","5G","Premium",385000),("8GB","256GB","5G","Premium",445000)]),
        P("Google","Pixel 7 Pro",2022,6.7,"LTPO OLED","Google Tensor G2",5000,23,50,10.8,"Yes","IP68","Aluminium","pro AI photography",[("12GB","128GB","5G","Flagship",495000),("12GB","256GB","5G","Flagship",565000),("12GB","512GB","5G","Flagship",645000)]),
        P("Google","Pixel 7a",2023,6.1,"OLED","Google Tensor G2",4385,18,64,13,"Yes","IP67","Aluminium","compact flagship",[("8GB","128GB","5G","Premium",345000),("8GB","256GB","5G","Premium",395000)]),
        P("Google","Pixel 8",2023,6.2,"OLED","Google Tensor G3",4575,27,50,10.5,"Yes","IP68","Aluminium","pure Android",[("8GB","128GB","5G","Flagship",445000),("8GB","256GB","5G","Flagship",525000)]),
        P("Google","Pixel 8 Pro",2023,6.7,"LTPO OLED","Google Tensor G3",5050,30,50,10.5,"Yes","IP68","Aluminium","pro photography",[("12GB","128GB","5G","Flagship",575000),("12GB","256GB","5G","Flagship",665000),("12GB","1TB","5G","Flagship",875000)]),
        P("Google","Pixel 8a",2024,6.1,"OLED","Google Tensor G3",4492,18,64,13,"Yes","IP67","Aluminium","affordable Pixel",[("8GB","128GB","5G","Premium",385000),("8GB","256GB","5G","Premium",445000)]),
        P("Google","Pixel 9",2024,6.3,"OLED","Google Tensor G4",4700,27,50,10.5,"Yes","IP68","Aluminium","2024 Pixel",[("12GB","128GB","5G","Flagship",525000),("12GB","256GB","5G","Flagship",625000)]),
        P("Google","Pixel 9 Pro",2024,6.3,"LTPO OLED","Google Tensor G4",4700,27,50,10.5,"Yes","IP68","Titanium","2024 pro",[("16GB","128GB","5G","Flagship",645000),("16GB","256GB","5G","Flagship",745000),("16GB","512GB","5G","Flagship",895000)]),
    ])
    all_phones.extend(pixel)
    print(f"  Google Pixel: {len(pixel)}")

    # ── Sony Xperia ───────────────────────────────────────────────────────────
    sony = _gen_brand("Sony", [
        P("Sony","Xperia 10 IV",2022,6.0,"OLED","Snapdragon 695",5000,30,12,8,"No","IP68","Aluminium","compact multimedia",[("6GB","128GB","5G","Premium",285000),("6GB","256GB","5G","Premium",335000)]),
        P("Sony","Xperia 10 V",2023,6.1,"OLED","Snapdragon 695",5000,30,48,12,"No","IP68","Aluminium","compact OLED",[("6GB","128GB","5G","Premium",295000),("6GB","256GB","5G","Premium",348000)]),
        P("Sony","Xperia 5 IV",2022,6.1,"OLED","Snapdragon 8 Gen 1",5000,30,12,12,"Yes","IP68","Aluminium","compact flagship",[("8GB","128GB","5G","Flagship",495000),("8GB","256GB","5G","Flagship",575000)]),
        P("Sony","Xperia 5 V",2023,6.1,"OLED","Snapdragon 8 Gen 2",5000,30,50,12,"Yes","IP68","Aluminium","compact 2023",[("8GB","256GB","5G","Flagship",545000),("8GB","256GB","5G","Flagship",545000)]),
        P("Sony","Xperia 1 IV",2022,6.5,"OLED 4K","Snapdragon 8 Gen 1",5000,30,12,12,"Yes","IP68","Aluminium","pro creator",[("12GB","256GB","5G","Flagship",695000),("12GB","512GB","5G","Flagship",825000)]),
        P("Sony","Xperia 1 V",2023,6.5,"OLED 4K","Snapdragon 8 Gen 2",5000,30,52,12,"Yes","IP68","Aluminium","2023 pro creator",[("12GB","256GB","5G","Flagship",745000),("12GB","512GB","5G","Flagship",895000)]),
        P("Sony","Xperia 1 VI",2024,6.5,"OLED","Snapdragon 8 Gen 3",5000,30,52,12,"Yes","IP68","Aluminium","2024 flagship",[("12GB","256GB","5G","Flagship",825000),("12GB","512GB","5G","Flagship",995000)]),
        P("Sony","Xperia PRO-I",2021,6.5,"OLED 4K","Snapdragon 888",4500,30,12,8,"Yes","IPX8","Aluminium","professional video",[("12GB","512GB","5G","Flagship",895000)]),
    ])
    all_phones.extend(sony)
    print(f"  Sony: {len(sony)}")

    return all_phones


# ─────────────────────────────────────────────────────────────────────────────
# INSERT
# ─────────────────────────────────────────────────────────────────────────────

def run():
    conn = psycopg2.connect(**DB)
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Get Mobile Phones category
    cur.execute("SELECT id FROM catalogue_categories WHERE slug='mobile-phones'")
    cat_id = cur.fetchone()["id"]
    print(f"Mobile Phones category id={cat_id}")

    # Get attribute def IDs
    cur.execute("SELECT id, attribute_key FROM catalogue_attribute_definitions WHERE category_id=%s", (cat_id,))
    attr_map = {row["attribute_key"]: row["id"] for row in cur.fetchall()}
    print(f"Attribute defs: {len(attr_map)}")

    # Generate all phones
    print("\nGenerating phones per brand:")
    all_phones = gen_all_phones()
    print(f"\nTotal generated: {len(all_phones)}")

    # Deduplicate SKUs (sku is index 3 in (brand, model, variant, sku, desc, attrs))
    seen = set()
    unique = []
    for phone in all_phones:
        sku = phone[3]
        if sku not in seen:
            seen.add(sku)
            unique.append(phone)
    print(f"After dedup: {len(unique)}")

    # Insert in batches
    inserted = 0
    attr_ins  = 0
    total     = len(unique)

    for i in range(0, total, BATCH):
        batch = unique[i:i + BATCH]
        for (brand, model_name, variant, sku, desc, attrs) in batch:
            cur.execute("""
                INSERT INTO catalogue_products
                    (category_id, brand, model_name, model_number, sku, description, is_active)
                VALUES (%s,%s,%s,%s,%s,%s,TRUE)
                ON CONFLICT (sku) DO NOTHING
                RETURNING id
            """, (cat_id, brand, model_name, variant, sku, desc))
            row = cur.fetchone()
            if not row:
                continue
            inserted += 1
            prod_id = row["id"]
            for key, val in attrs.items():
                if key.startswith("_") or not val:
                    continue
                def_id = attr_map.get(key)
                if not def_id:
                    continue
                cur.execute("""
                    INSERT INTO catalogue_product_attributes (product_id, attribute_def_id, value)
                    VALUES (%s,%s,%s)
                    ON CONFLICT DO NOTHING
                """, (prod_id, def_id, str(val)))
                attr_ins += 1

        conn.commit()
        print(f"  {min(i+BATCH,total)}/{total}", end="\r", flush=True)

    print(f"\n✓ Inserted {inserted} phones, {attr_ins} attribute values")

    # Final count per brand
    cur.execute("""
        SELECT brand, COUNT(*) AS cnt
        FROM catalogue_products
        WHERE category_id=%s AND is_active=TRUE
        GROUP BY brand ORDER BY cnt DESC
    """, (cat_id,))
    print("\n── Mobile Phones by brand ───────────────────────")
    grand = 0
    for row in cur.fetchall():
        print(f"  {row['brand']}: {row['cnt']}")
        grand += row['cnt']
    print(f"  TOTAL: {grand}")

    cur.close(); conn.close()
    print("Done.")


if __name__ == "__main__":
    # Fix brand on products: brand is the first word of model_name for most
    # We pass brand via a wrapper
    run()
