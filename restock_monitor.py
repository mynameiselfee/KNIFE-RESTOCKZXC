import os
import json
import time
import html
import threading
import urllib.parse
import urllib.request


# ============================================================
# CONFIG
# ============================================================

SITE = "https://knifemfg.co"

STATE_FILE = os.getenv(
    "STATE_FILE",
    "/data/state.json"
)

CHECK_INTERVAL = int(
    os.getenv("CHECK_INTERVAL", "5")
)

SHAPES = [
    "KH1",
    "KL2",
    "KL1",
]

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN"
)

TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 "
        "(compatible; KnifeMFG-DeckRadar/1.0)"
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
# LOGGING
# ============================================================

def log(message):
    print(
        f"[RADAR] {message}",
        flush=True
    )


# ============================================================
# HTML ESCAPE
# ============================================================

def escape(text):
    return html.escape(
        str(text or "")
    )


# ============================================================
# FETCH URL
# ============================================================

def fetch_url(url, timeout=20):

    request = urllib.request.Request(
        url,
        headers=HEADERS
    )

    with urllib.request.urlopen(
        request,
        timeout=timeout
    ) as response:

        return response.read()


# ============================================================
# FETCH KNIFE MFG PRODUCTS
# ============================================================

def fetch_products():

    all_products = []
    page = 1

    while True:

        url = (
            f"{SITE}/products.json"
            f"?limit=250&page={page}"
        )

        try:

            raw = fetch_url(url)

            data = json.loads(
                raw.decode("utf-8")
            )

        except Exception as e:

            log(
                f"Product fetch failed: {e}"
            )

            break

        products = data.get(
            "products",
            []
        )

        if not products:
            break

        all_products.extend(
            products
        )

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


# ============================================================
# DETECT SHAPE
# ============================================================

def extract_shape(title):

    text = str(
        title or ""
    ).upper()

    for shape in SHAPES:

        if shape in text:
            return shape

    return None


# ============================================================
# BUILD STOCK SNAPSHOT
# ============================================================

def build_snapshot(products):

    snapshot = {}

    for product in products:

        product_title = product.get(
            "title",
            "Unknown Product"
        )

        handle = product.get(
            "handle",
            ""
        )

        if handle:

            product_url = (
                f"{SITE}/products/{handle}"
            )

        else:

            product_url = SITE

        images = product.get(
            "images",
            []
        )

        image_url = ""

        if images:

            image_url = images[0].get(
                "src",
                ""
            )

        variants = product.get(
            "variants",
            []
        )

        for variant in variants:

            variant_id = str(
                variant.get(
                    "id",
                    ""
                )
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
                variant.get(
                    "available",
                    False
                )
            )

            price = variant.get(
                "price",
                ""
            )

            cart_url = (
                f"{SITE}/cart/"
                f"{variant_id}:1"
            )

            snapshot[variant_id] = {
                "id": variant_id,
                "product": product_title,
                "variant": variant_title,
                "shape": shape,
                "price": price,
                "available": available,
                "image": image_url,
                "product_url": product_url,
                "cart_url": cart_url,
            }

    return snapshot


# ============================================================
# TELEGRAM API
# ============================================================

def telegram_api(
    method,
    params=None
):

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
        method="POST"
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=40
        ) as response:

            raw = response.read()

            result = json.loads(
                raw.decode("utf-8")
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
            "error": str(e)
        }


# ============================================================
# SEND TELEGRAM MESSAGE
# ============================================================

def send_text(text):

    return telegram_api(
        "sendMessage",
        {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
    )


# ============================================================
# SEND PHOTO
# ============================================================

def send_photo(
    photo,
    caption,
    reply_markup=None
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
        params
    )


# ============================================================
# TELEGRAM BUTTONS
# ============================================================

def buttons(
    product_url,
    cart_url
):

    return {
        "inline_keyboard": [
            [
                {
                    "text": "🛒 BUY NOW",
                    "url": cart_url,
                },
                {
                    "text": "VIEW DECK",
                    "url": product_url,
                },
            ]
        ]
    }


# ============================================================
# /STATUS
#
# EXACT FORMAT REQUESTED
#
# 📡 Knife MFG · deck radar
# ━━━━━━━━━━━━━━
# ⚫ KH1 · sold out
# ⚫ KL2 · sold out
# ⚫ KL1 · sold out
# ━━━━━━━━━━━━━━
# 🕒 checked just now
# ============================================================

def status_text():

    with LOCK:
        snapshot = dict(
            CURRENT_SNAPSHOT
        )

    def is_in_stock(shape):

        return any(
            item.get("shape") == shape
            and item.get("available", False)
            for item in snapshot.values()
        )

    kh1_stock = is_in_stock("KH1")
    kl2_stock = is_in_stock("KL2")
    kl1_stock = is_in_stock("KL1")

    kh1_icon = (
        "🟢"
        if kh1_stock
        else "⚫"
    )

    kl2_icon = (
        "🟢"
        if kl2_stock
        else "⚫"
    )

    kl1_icon = (
        "🟢"
        if kl1_stock
        else "⚫"
    )

    kh1_status = (
        "in stock"
        if kh1_stock
        else "sold out"
    )

    kl2_status = (
        "in stock"
        if kl2_stock
        else "sold out"
    )

    kl1_status = (
        "in stock"
        if kl1_stock
        else "sold out"
    )

    return (
        "📡 <b>Knife MFG · deck radar</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"{kh1_icon} KH1 · {kh1_status}\n"
        f"{kl2_icon} KL2 · {kl2_status}\n"
        f"{kl1_icon} KL1 · {kl1_status}\n"
        "━━━━━━━━━━━━━━\n"
        "🕒 checked just now"
    )


# ============================================================
# /HELP
# ============================================================

HELP = (
    "📡 <b>Knife MFG · deck radar</b>\n"
    "━━━━━━━━━━━━━━\n"
    "\n"
    "🚨 <b>DROP WATCH ACTIVE</b>\n"
    "\n"
    "I'm watching Knife MFG 24/7.\n"
    "\n"
    "🎯 KH1\n"
    "🎯 KL2\n"
    "🎯 KL1\n"
    "\n"
    "Stock detected?\n"
    "<b>You'll know immediately.</b> ⚡\n"
    "\n"
    "━━━━━━━━━━━━━━\n"
    "\n"
    "<b>COMMANDS</b>\n"
    "\n"
    "/status — Check current stock\n"
    "/help — Show commands\n"
    "\n"
    "━━━━━━━━━━━━━━\n"
    "\n"
    "🟢 SYSTEM ONLINE\n"
    "⚡ RADAR ACTIVE\n"
    "🚨 DROP ALERTS ON"
)


# ============================================================
# RESTOCK ALERT
# ============================================================

def alert_caption(item):

    return (
        "🚨 <b>KNIFE MFG RESTOCK!</b>\n"
        "\n"
        f"🛹 <b>{escape(item['shape'])}</b>\n"
        f"📦 {escape(item['product'])}\n"
        f"📏 {escape(item['variant'])}\n"
        f"💰 ${escape(item['price'])}\n"
        "\n"
        "🟢 <b>IN STOCK NOW</b>\n"
        "\n"
        "⚡ <b>DROP DETECTED</b>"
    )


def send_alert(item):

    log(
        "RESTOCK DETECTED: "
        f"{item['shape']} "
        f"{item['variant']}"
    )

    caption = alert_caption(
        item
    )

    product_url = item.get(
        "product_url",
        SITE
    )

    cart_url = item.get(
        "cart_url",
        SITE
    )

    image = item.get(
        "image"
    )

    # Try sending an image first

    if image:

        result = send_photo(
            image,
            caption,
            buttons(
                product_url,
                cart_url
            )
        )

        if result.get("ok"):

            return

    # Fallback to text

    send_text(
        caption
        + "\n\n"
        + f'<a href="{escape(cart_url)}">'
        "🛒 BUY NOW"
        "</a>"
    )


# ============================================================
# LOAD STATE
# ============================================================

def load_state():

    global STATE

    try:

        if not os.path.exists(
            STATE_FILE
        ):

            STATE = {}

            log(
                "No previous state found."
            )

            return

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as file:

            STATE = json.load(
                file
            )

        log(
            f"Loaded state: "
            f"{len(STATE)} variants"
        )

    except Exception as e:

        log(
            f"Could not load state: {e}"
        )

        STATE = {}


# ============================================================
# SAVE STATE
# ============================================================

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
        ) as file:

            json.dump(
                STATE,
                file,
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
                "No KH1/KL2/KL1 variants found."
            )

            return

        alerts = []

        with LOCK:

            previous_state = dict(
                STATE
            )

            # First run:
            # establish baseline without sending alerts.

            if not previous_state:

                for key, item in snapshot.items():

                    STATE[key] = bool(
                        item.get(
                            "available",
                            False
                        )
                    )

                CURRENT_SNAPSHOT = snapshot

                save_state()

                log(
                    "Initial baseline created: "
                    f"{len(snapshot)} variants"
                )

                return

            # Detect sold out -> in stock

            for key, item in snapshot.items():

                now_available = bool(
                    item.get(
                        "available",
                        False
                    )
                )

                was_available = bool(
                    previous_state.get(
                        key,
                        False
                    )
                )

                if (
                    now_available
                    and not was_available
                ):

                    alerts.append(
                        item
                    )

                STATE[key] = (
                    now_available
                )

            CURRENT_SNAPSHOT = snapshot

            save_state()

        # Send alerts outside lock.
        # This keeps /status responsive.

        for item in alerts:

            send_alert(
                item
            )

        available_count = sum(
            1
            for item in snapshot.values()
            if item.get(
                "available",
                False
            )
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
# TELEGRAM LISTENER
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
                    chat.get(
                        "id",
                        ""
                    )
                )

                # Only allow your own chat

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
                    text
                    .split()[0]
                    .lower()
                    .split("@")[0]
                )

                log(
                    f"Command received: "
                    f"{command}"
                )

                # --------------------------------------------
                # /STATUS
                # --------------------------------------------

                if command == "/status":

                    send_text(
                        status_text()
                    )

                # --------------------------------------------
                # /HELP
                # --------------------------------------------

                elif command == "/help":

                    send_text(
                        HELP
                    )

                # --------------------------------------------
                # /START
                # --------------------------------------------

                elif command == "/start":

                    send_text(
                        "📡 <b>Knife MFG · deck radar</b>\n"
                        "\n"
                        "🟢 Radar is online.\n"
                        "⚡ Restock monitoring is active.\n"
                        "\n"
                        "Use /status to check stock."
                    )

                # --------------------------------------------
                # UNKNOWN COMMAND
                # --------------------------------------------

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

    # Verify bot

    result = telegram_api(
        "getMe"
    )

    if not result.get("ok"):

        raise RuntimeError(
            "Telegram bot connection failed."
        )

    bot = result["result"]

    log(
        "Telegram connected: "
        f"@{bot.get('username')}"
    )

    # Remove webhook so long polling works

    telegram_api(
        "deleteWebhook",
        {
            "drop_pending_updates":
                "false"
        }
    )

    # ONLY TWO COMMANDS IN TELEGRAM MENU

    commands = [
        {
            "command": "status",
            "description":
                "Check current stock",
        },
        {
            "command": "help",
            "description":
                "Show commands",
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
        "========================================"
    )

    log(
        "📡 KNIFE MFG DECK RADAR"
    )

    log(
        "========================================"
    )

    log(
        f"Watching: {', '.join(SHAPES)}"
    )

    log(
        f"Check interval: "
        f"{CHECK_INTERVAL} seconds"
    )

    log(
        f"State file: "
        f"{STATE_FILE}"
    )

    # Load previous stock state

    load_state()

    # Setup Telegram

    setup_telegram()

    # Initial stock check

    log(
        "Performing initial stock check..."
    )

    check_stock()

    # Start Telegram listener

    listener = threading.Thread(
        target=telegram_listener,
        daemon=True
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

    # Continuous monitoring

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
