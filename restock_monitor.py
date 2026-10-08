import os
import json
import time
import html
import threading
import urllib.parse
import urllib.request
import urllib.error


# ============================================================
# CONFIG
# ============================================================

SITE = "https://knifemfg.co"

STATE_FILE = os.getenv("STATE_FILE", "/data/state.json")

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "5"))

SHAPES = ["KH1", "KL2", "KL1"]

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; KnifeMFG-Restock-Radar/1.0)"
    ),
    "Accept": "application/json,text/plain,*/*",
}


# ============================================================
# GLOBAL STATE
# ============================================================

CURRENT_SNAPSHOT = {}
STATE = {}

LOCK = threading.Lock()


# ============================================================
# UTILITY
# ============================================================

def log(message):
    print(f"[RADAR] {message}", flush=True)


def escape(text):
    return html.escape(str(text or ""))


# ============================================================
# KNIFE MFG
# ============================================================

def fetch_url(url, timeout=20):
    request = urllib.request.Request(
        url,
        headers=HEADERS,
    )

    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def fetch_products():
    """
    Download Knife MFG products.
    """

    all_products = []

    page = 1

    while True:

        url = (
            f"{SITE}/products.json"
            f"?limit=250&page={page}"
        )

        try:
            raw = fetch_url(url)

            data = json.loads(raw.decode("utf-8"))

        except Exception as e:
            log(f"Product fetch failed: {e}")
            break

        products = data.get("products", [])

        if not products:
            break

        all_products.extend(products)

        log(
            f"Fetched page {page}: "
            f"{len(products)} products"
        )

        if len(products) < 250:
            break

        page += 1

        # Safety limit
        if page > 20:
            break

    return all_products


def extract_shape(title):
    """
    Detect KH1 / KL2 / KL1 from a variant title.
    """

    text = str(title or "").upper()

    for shape in SHAPES:
        if shape in text:
            return shape

    return None


def build_snapshot(products):
    """
    Build a clean stock snapshot.
    """

    snapshot = {}

    for product in products:

        product_title = product.get("title", "Unknown Product")
        handle = product.get("handle", "")

        product_url = (
            f"{SITE}/products/{handle}"
            if handle
            else SITE
        )

        variants = product.get("variants", [])

        images = product.get("images", [])

        image_url = ""

        if images:
            image_url = images[0].get("src", "")

        for variant in variants:

            variant_id = str(
                variant.get("id", "")
            )

            if not variant_id:
                continue

            variant_title = variant.get(
                "title",
                ""
            )

            shape = extract_shape(
                variant_title
            )

            if shape not in SHAPES:
                continue

            available = bool(
                variant.get("available", False)
            )

            price = variant.get(
                "price",
                ""
            )

            size = variant_title

            key = variant_id

            cart_url = (
                f"{SITE}/cart/"
                f"{variant_id}:1"
            )

            snapshot[key] = {
                "id": variant_id,
                "product": product_title,
                "variant": variant_title,
                "shape": shape,
                "size": size,
                "price": price,
                "available": available,
                "image": image_url,
                "product_url": product_url,
                "cart_url": cart_url,
            }

    return snapshot


# ============================================================
# TELEGRAM
# ============================================================

def telegram_api(method, params=None):

    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not set."
        )

    url = (
        "https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/"
        f"{method}"
    )

    params = params or {}

    encoded = urllib.parse.urlencode(
        params
    ).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=encoded,
        headers={
            "Content-Type":
                "application/x-www-form-urlencoded"
        },
        method="POST",
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=40
        ) as response:

            data = response.read()

            result = json.loads(
                data.decode("utf-8")
            )

            if not result.get("ok"):
                log(
                    f"Telegram error: {result}"
                )

            return result

    except Exception as e:

        log(
            f"Telegram API error: {e}"
        )

        return {
            "ok": False,
            "error": str(e),
        }


def send_text(text):

    return telegram_api(
        "sendMessage",
        {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
    )


def send_photo(
    photo,
    caption,
    reply_markup=None,
):

    params = {
        "chat_id": TELEGRAM_CHAT_ID,
        "photo": photo,
        "caption": caption,
        "parse_mode": "HTML",
    }

    if reply_markup:
        params["reply_markup"] = json.dumps(
            reply_markup
        )

    return telegram_api(
        "sendPhoto",
        params,
    )


# ============================================================
# TELEGRAM BUTTONS
# ============================================================

def buttons(product_url, cart_url):

    return {
        "inline_keyboard": [
            [
                {
                    "text": "🛒 BUY NOW",
                    "url": cart_url,
                },
                {
                    "text": "🔎 VIEW",
                    "url": product_url,
                },
            ]
        ]
    }


# ============================================================
# HELP
# ============================================================

LINE = "━━━━━━━━━━━━━━━━━━━━"

HELP = f"""
🛹 <b>KNIFE MFG • DROP RADAR</b>
{LINE}

🚨 <b>DROP WATCH ACTIVE</b>

I'm watching Knife MFG 24/7.

🎯 KH1
🎯 KL2
🎯 KL1

Stock detected?
<b>You'll know immediately.</b> ⚡

<b>COMMANDS</b>

/status — Check current stock
/help — Show commands

{LINE}

🟢 SYSTEM ONLINE
⚡ RADAR ACTIVE
🚨 DROP ALERTS ON
""".strip()


# ============================================================
# STATUS
# ============================================================

def status_text():

    with LOCK:
        snapshot = dict(CURRENT_SNAPSHOT)

    if not snapshot:

        return (
            "🛹 <b>KNIFE MFG RADAR</b>\n\n"
            "⏳ Stock data is loading..."
        )

    available = [
        item
        for item in snapshot.values()
        if item.get("available")
    ]

    lines = [
        "🛹 <b>KNIFE MFG • STATUS</b>",
        LINE,
        "",
    ]

    for shape in SHAPES:

        items = [
            item
            for item in available
            if item.get("shape") == shape
        ]

        if items:

            lines.append(
                f"🟢 <b>{shape}</b>"
            )

            for item in items:

                lines.append(
                    f"  • "
                    f"{escape(item['variant'])}"
                    f" — "
                    f"${escape(item['price'])}"
                )

        else:

            lines.append(
                f"⚫ <b>{shape}</b> — Out of stock"
            )

        lines.append("")

    lines.extend([
        LINE,
        f"⚡ Checked every {CHECK_INTERVAL}s",
    ])

    return "\n".join(lines)


# ============================================================
# RESTOCK ALERT
# ============================================================

def alert_caption(item):

    return (
        "🚨 <b>KNIFE MFG RESTOCK!</b>\n\n"

        f"🛹 <b>{escape(item['shape'])}</b>\n"
        f"📦 {escape(item['product'])}\n"
        f"📏 {escape(item['variant'])}\n"
        f"💰 ${escape(item['price'])}\n\n"

        "🟢 <b>IN STOCK NOW</b>\n\n"

        "⚡ <b>DROP DETECTED</b>"
    )


def send_alert(item):

    caption = alert_caption(item)

    product_url = item.get(
        "product_url",
        SITE
    )

    cart_url = item.get(
        "cart_url",
        SITE
    )

    image = item.get("image")

    log(
        f"RESTOCK: "
        f"{item['shape']} "
        f"{item['variant']}"
    )

    if image:

        result = send_photo(
            image,
            caption,
            buttons(
                product_url,
                cart_url
            ),
        )

        if result.get("ok"):
            return

    # Fallback if image sending fails

    send_text(
        caption
        + "\n\n"
        f"🛒 <a href=\"{escape(cart_url)}\">"
        "BUY NOW"
        "</a>"
    )


# ============================================================
# STATE
# ============================================================

def load_state():

    global STATE

    try:

        if not os.path.exists(
            STATE_FILE
        ):
            STATE = {}
            return

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            STATE = json.load(f)

        log(
            f"Loaded state: "
            f"{len(STATE)} variants"
        )

    except Exception as e:

        log(
            f"Could not load state: {e}"
        )

        STATE = {}


def save_state():

    try:

        directory = os.path.dirname(
            STATE_FILE
        )

        if directory:
            os.makedirs(
                directory,
                exist_ok=True
            )

        temp_file = (
            STATE_FILE + ".tmp"
        )

        with open(
            temp_file,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                STATE,
                f,
                indent=2
            )

        os.replace(
            temp_file,
            STATE_FILE
        )

    except Exception as e:

        log(
            f"Could not save state: {e}"
        )


# ============================================================
# STOCK CHECK
# ============================================================

def check_stock():

    global CURRENT_SNAPSHOT
    global STATE

    try:

        products = fetch_products()

        if not products:

            log(
                "No products received."
            )

            return

        snapshot = build_snapshot(
            products
        )

        if not snapshot:

            log(
                "No matching KH1/KL2/KL1 variants found."
            )

            return

        alerts = []

        with LOCK:

            previous_state = dict(
                STATE
            )

            # ------------------------------------------------
            # FIRST RUN
            # ------------------------------------------------

            if not previous_state:

                for key, item in snapshot.items():

                    STATE[key] = bool(
                        item["available"]
                    )

                CURRENT_SNAPSHOT = snapshot

                save_state()

                log(
                    f"Initial baseline created: "
                    f"{len(snapshot)} variants"
                )

                return

            # ------------------------------------------------
            # CHECK FOR RESTOCK
            # ------------------------------------------------

            for key, item in snapshot.items():

                now_available = bool(
                    item["available"]
                )

                was_available = bool(
                    previous_state.get(
                        key,
                        False
                    )
                )

                # OUT OF STOCK -> IN STOCK
                if (
                    now_available
                    and not was_available
                ):

                    alerts.append(item)

                STATE[key] = now_available

            CURRENT_SNAPSHOT = snapshot

            save_state()

        # Send alerts outside the lock
        # so Telegram never blocks /status

        for item in alerts:

            send_alert(item)

        available_count = sum(
            1
            for item in snapshot.values()
            if item["available"]
        )

        log(
            f"Stock check complete: "
            f"{len(snapshot)} variants | "
            f"{available_count} available"
        )

    except Exception as e:

        log(
            f"Stock check error: {e}"
        )


# ============================================================
# TELEGRAM COMMAND LISTENER
# ============================================================

def telegram_listener():

    log(
        "Telegram listener started."
    )

    offset = None

    while True:

        try:

            params = {
                "timeout": 30,
                "allowed_updates": json.dumps(
                    ["message"]
                ),
            }

            if offset is not None:

                params["offset"] = offset

            result = telegram_api(
                "getUpdates",
                params
            )

            if not result.get("ok"):

                time.sleep(2)

                continue

            updates = result.get(
                "result",
                []
            )

            for update in updates:

                offset = (
                    update["update_id"] + 1
                )

                message = update.get(
                    "message"
                )

                if not message:
                    continue

                chat = message.get(
                    "chat",
                    {}
                )

                chat_id = str(
                    chat.get("id", "")
                )

                # Only YOU can use this bot
                if (
                    str(TELEGRAM_CHAT_ID)
                    != chat_id
                ):
                    continue

                text = message.get(
                    "text",
                    ""
                ).strip()

                if not text:
                    continue

                command = (
                    text.split()[0]
                    .lower()
                    .split("@")[0]
                )

                log(
                    f"Command received: "
                    f"{command}"
                )

                if command == "/start":

                    send_text(
                        "🛹 <b>KNIFE MFG RADAR</b>\n\n"
                        "🟢 I'm online and watching.\n\n"
                        "Use /status to check stock."
                    )

                elif command == "/help":

                    send_text(
                        HELP
                    )

                elif command in (
                    "/status",
                    "/now",
                    "/stock",
                ):

                    # Uses cached data.
                    # No website request required.
                    send_text(
                        status_text()
                    )

                else:

                    send_text(
                        "❓ Unknown command.\n\n"
                        "Use /help."
                    )

        except Exception as e:

            log(
                f"Telegram listener error: {e}"
            )

            time.sleep(2)


# ============================================================
# TELEGRAM SETUP
# ============================================================

def setup_telegram():

    log(
        "Checking Telegram connection..."
    )

    result = telegram_api(
        "getMe"
    )

    if result.get("ok"):

        bot = result["result"]

        log(
            f"Telegram connected: "
            f"@{bot.get('username')}"
        )

    else:

        raise RuntimeError(
            "Telegram bot connection failed."
        )

    # Remove webhook so getUpdates works
    telegram_api(
        "deleteWebhook",
        {
            "drop_pending_updates": "false"
        }
    )

    # Telegram command menu

    commands = [
        {
            "command": "status",
            "description": "Check current stock",
        },
        {
            "command": "help",
            "description": "Show help",
        },
    ]

    telegram_api(
        "setMyCommands",
        {
            "commands": json.dumps(
                commands
            )
        }
    )

    log(
        "Telegram commands configured."
    )


# ============================================================
# MAIN
# ============================================================

def main():

    if not TELEGRAM_BOT_TOKEN:

        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not set."
        )

    if not TELEGRAM_CHAT_ID:

        raise RuntimeError(
            "TELEGRAM_CHAT_ID is not set."
        )

    log(
        "================================"
    )

    log(
        "🛹 KNIFE MFG DROP RADAR"
    )

    log(
        "================================"
    )

    log(
        f"Check interval: "
        f"{CHECK_INTERVAL}s"
    )

    log(
        f"State file: "
        f"{STATE_FILE}"
    )

    log(
        f"Watching: "
        f"{', '.join(SHAPES)}"
    )

    load_state()

    setup_telegram()

    # Initial stock check
    log(
        "Performing initial stock check..."
    )

    check_stock()

    # Start Telegram listener
    # This runs independently from stock checking.

    listener = threading.Thread(
        target=telegram_listener,
        daemon=True,
    )

    listener.start()

    log(
        "🟢 RADAR ONLINE"
    )

    log(
        "⚡ Telegram commands are live."
    )

    log(
        "🚨 Restock monitoring is active."
    )

    # Continuous stock monitoring

    while True:

        started = time.time()

        check_stock()

        elapsed = (
            time.time() - started
        )

        sleep_time = max(
            0,
            CHECK_INTERVAL - elapsed
        )

        time.sleep(
            sleep_time
        )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    try:

        main()

    except KeyboardInterrupt:

        log(
            "Radar stopped."
        )

    except Exception as e:

        log(
            f"FATAL ERROR: {e}"
        )

        raise
