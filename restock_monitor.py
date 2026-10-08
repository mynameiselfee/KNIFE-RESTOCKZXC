import html
import json
import os
import re
import sys
import time
import threading
import urllib.parse
import urllib.request

# ============================================================
# CONFIG
# ============================================================

SITE = "https://knifemfg.co"

STATE_FILE = os.environ.get(
    "STATE_FILE",
    "/data/state.json"
)

SHAPES = ["KH1", "KL2", "KL1"]

# 5 seconds is aggressive but reasonable.
CHECK_INTERVAL = int(
    os.environ.get("CHECK_INTERVAL", "5")
)

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Cache-Control": "no-cache",
}

LINE = "\u2501" * 14


# ============================================================
# LIVE SHARED DATA
# ============================================================

CURRENT_SNAPSHOT = {}
STATE = {}

LOCK = threading.Lock()


# ============================================================
# HELPERS
# ============================================================

def esc(value):
    return html.escape(
        str(value or "")
    )


def shape_of(title):

    clean = (
        " "
        + re.sub(
            r"[^A-Z0-9]+",
            " ",
            (title or "").upper()
        )
        + " "
    )

    for shape in SHAPES:

        if f" {shape} " in clean:
            return shape

    return None


# ============================================================
# KNIFE MFG
# ============================================================

def fetch_products():

    products = []
    page = 1

    while True:

        url = (
            f"{SITE}/products.json"
            f"?limit=250"
            f"&page={page}"
            f"&_={int(time.time() * 1000)}"
        )

        request = urllib.request.Request(
            url,
            headers=HEADERS
        )

        with urllib.request.urlopen(
            request,
            timeout=20
        ) as response:

            data = json.load(response)

        batch = data.get(
            "products",
            []
        )

        if not batch:
            break

        products.extend(batch)

        page += 1

        if page > 50:
            break

    return products


def snapshot(products):

    snap = {}

    for product in products:

        images = (
            product.get("images")
            or []
        )

        image = (
            images[0].get("src")
            if images
            else None
        )

        for variant in product.get(
            "variants",
            []
        ):

            title = (
                variant.get("title")
                or ""
            )

            shape = shape_of(title)

            if not shape:
                continue

            size = re.sub(
                rf"\b{shape}\b",
                "",
                title,
                flags=re.I
            ).strip()

            variant_id = str(
                variant["id"]
            )

            handle = product.get(
                "handle",
                ""
            )

            snap[variant_id] = {

                "name":
                    product.get(
                        "title",
                        ""
                    ),

                "shape":
                    shape,

                "size":
                    size,

                "price":
                    variant.get(
                        "price"
                    ),

                "image":
                    image,

                "available":
                    bool(
                        variant.get(
                            "available"
                        )
                    ),

                "url":
                    f"{SITE}/products/"
                    f"{handle}"
                    f"?variant={variant_id}",

                "cart":
                    f"{SITE}/cart/"
                    f"{variant_id}:1",
            }

    return snap


# ============================================================
# TELEGRAM API
# ============================================================

def token():

    value = os.environ.get(
        "TELEGRAM_BOT_TOKEN",
        ""
    ).strip()

    if not value:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is missing"
        )

    return value


def my_chat_id():

    value = os.environ.get(
        "TELEGRAM_CHAT_ID",
        ""
    ).strip()

    if not value:
        raise RuntimeError(
            "TELEGRAM_CHAT_ID is missing"
        )

    return value


def tg(method, params=None):

    data = urllib.parse.urlencode(
        params or {}
    ).encode()

    url = (
        "https://api.telegram.org/"
        f"bot{token()}/{method}"
    )

    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "User-Agent":
                "KnifeMFG-Radar/1.0"
        }
    )

    with urllib.request.urlopen(
        request,
        timeout=35
    ) as response:

        result = json.load(response)

    if not result.get("ok"):

        raise RuntimeError(
            str(result)
        )

    return result


# ============================================================
# TELEGRAM MESSAGE
# ============================================================

def buttons(*pairs):

    return json.dumps({
        "inline_keyboard": [
            [
                {
                    "text": text,
                    "url": url
                }
                for text, url in pairs
            ]
        ]
    })


def send_text(
    text,
    markup=None
):

    params = {

        "chat_id":
            my_chat_id(),

        "text":
            text,

        "parse_mode":
            "HTML",

        "disable_web_page_preview":
            "true",
    }

    if markup:

        params[
            "reply_markup"
        ] = markup

    return tg(
        "sendMessage",
        params
    )


# ============================================================
# STATUS
# ============================================================

def status_text(snapshot_data):

    lines = [

        "\U0001F6F9 "
        "<b>KNIFE MFG • LIVE RADAR</b>",

        LINE,
    ]

    for shape in SHAPES:

        live = [

            item

            for item
            in snapshot_data.values()

            if (
                item["shape"] == shape
                and item["available"]
            )
        ]

        if live:

            lines.append(

                f"\U0001F7E2 "
                f"<b>{shape}</b> "
                f"\u00B7 "
                f"{len(live)} IN STOCK"
            )

            for item in live[:6]:

                lines.append(

                    f"   • "
                    f"{esc(item['name'])}"
                )

        else:

            lines.append(

                f"\U0001F534 "
                f"<b>{shape}</b> "
                f"\u00B7 SOLD OUT"
            )

    lines.extend([

        LINE,

        "\u26A1 "
        "<b>RADAR: ONLINE</b>",

        "\U0001F504 "
        f"SCANNING EVERY {CHECK_INTERVAL}s",

        "\U0001F6A8 "
        "RESTOCK ALERTS: ON",
    ])

    return "\n".join(lines)


HELP = (

    "\U0001F6F9 "
    "<b>KNIFE MFG • DROP RADAR</b>\n"

    + LINE

    + "\n"

    "\U0001F6A8 "
    "<b>DROP WATCH ACTIVE</b>\n\n"

    "I'm watching Knife MFG 24/7.\n\n"

    "\U0001F3AF KH1\n"
    "\U0001F3AF KL2\n"
    "\U0001F3AF KL1\n\n"

    "Stock detected?\n"
    "<b>You'll know immediately.</b> \u26A1\n\n"

    "<b>COMMANDS</b>\n"

    "/status \u2014 Check stock\n"
    "/help \u2014 Show commands\n\n"

    LINE

    + "\n"

    "\U0001F7E2 SYSTEM ONLINE\n"
    "\u26A1 RADAR ACTIVE\n"
    "\U0001F6A8 DROP ALERTS ON"
)


# ============================================================
# FAST TELEGRAM LISTENER
# ============================================================

def telegram_listener():

    global STATE
    global CURRENT_SNAPSHOT

    print(
        "Telegram listener started."
    )

    while True:

        try:

            with LOCK:

                offset = int(
                    STATE.get(
                        "offset",
                        0
                    )
                )

            # LONG POLLING
            #
            # Telegram keeps this connection open.
            # When /status arrives, Telegram immediately
            # returns it to us.
            result = tg(

                "getUpdates",

                {
                    "offset":
                        offset,

                    "timeout":
                        30,

                    "allowed_updates":
                        json.dumps(
                            ["message"]
                        ),
                }
            )

            updates = result.get(
                "result",
                []
            )

            for update in updates:

                update_id = update.get(
                    "update_id"
                )

                if update_id is not None:

                    with LOCK:

                        STATE["offset"] = (
                            update_id + 1
                        )

                message = (
                    update.get("message")
                    or {}
                )

                chat = (
                    message.get("chat")
                    or {}
                )

                incoming_id = str(
                    chat.get(
                        "id",
                        ""
                    )
                )

                # Only YOUR chat
                if incoming_id != str(
                    my_chat_id()
                ):
                    continue

                text = (
                    message.get("text")
                    or ""
                ).strip()

                if not text:
                    continue

                command = (
                    text.split()[0]
                    .split("@")[0]
                    .lower()
                )

                print(
                    "COMMAND:",
                    command
                )

                # Copy current stock instantly
                with LOCK:

                    current = dict(
                        CURRENT_SNAPSHOT
                    )

                if command in (
                    "/status",
                    "/now",
                    "/stock",
                ):

                    # INSTANT RESPONSE
                    send_text(
                        status_text(
                            current
                        ),
                        buttons(
                            (
                                "\U0001F6D2 "
                                "Open shop",
                                SITE
                            )
                        )
                    )

                elif command == "/help":

                    send_text(
                        HELP
                    )

                with LOCK:

                    save_state(
                        STATE
                    )

        except Exception as error:

            print(
                "Telegram listener error:",
                error,
                file=sys.stderr
            )

            time.sleep(1)


# ============================================================
# RESTOCK ALERT
# ============================================================

def alert_caption(item):

    shape = (
        f"\U0001F6F9 "
        f"<b>{esc(item['shape'])}</b>"
    )

    if item.get("size"):

        shape += (
            f" \u00B7 "
            f"{esc(item['size'])}"
        )

    lines = [

        "\U0001F6A8 "
        "<b>RESTOCK DETECTED</b>",

        LINE,

        f"<b>{esc(item['name'])}</b>",

        shape,
    ]

    if item.get("price"):

        lines.append(
            f"\U0001F4B5 "
            f"{esc(item['price'])}"
        )

    lines.extend([

        LINE,

        "\u26A1 "
        "<b>DROP IS LIVE</b>",
    ])

    return "\n".join(lines)


def send_alert(item):

    caption = alert_caption(
        item
    )

    markup = buttons(

        (
            "\U0001F6D2 "
            "ADD TO CART",
            item["cart"]
        ),

        (
            "\U0001F517 "
            "OPEN PAGE",
            item["url"]
        ),
    )

    if item.get("image"):

        try:

            tg(
                "sendPhoto",
                {
                    "chat_id":
                        my_chat_id(),

                    "photo":
                        item["image"],

                    "caption":
                        caption,

                    "parse_mode":
                        "HTML",

                    "reply_markup":
                        markup,
                }
            )

            print(
                "RESTOCK ALERT SENT:",
                item["name"]
            )

            return

        except Exception as error:

            print(
                "Photo alert failed:",
                error
            )

    send_text(
        caption,
        markup
    )


# ============================================================
# STATE
# ============================================================

def load_state():

    directory = os.path.dirname(
        STATE_FILE
    )

    if directory:

        try:
            os.makedirs(
                directory,
                exist_ok=True
            )
        except Exception:
            pass

    if not os.path.exists(
        STATE_FILE
    ):

        return {
            "initialized": False,
            "offset": 0,
            "v": {},
        }

    try:

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as file:

            return json.load(file)

    except Exception:

        return {
            "initialized": False,
            "offset": 0,
            "v": {},
        }


def save_state(state):

    directory = os.path.dirname(
        STATE_FILE
    )

    if directory:

        try:
            os.makedirs(
                directory,
                exist_ok=True
            )
        except Exception:
            pass

    temp = (
        STATE_FILE + ".tmp"
    )

    try:

        with open(
            temp,
            "w",
            encoding="utf-8"
        ) as file:

            json.dump(
                state,
                file,
                indent=2
            )

        os.replace(
            temp,
            STATE_FILE
        )

    except Exception as error:

        print(
            "State save error:",
            error
        )


# ============================================================
# STOCK CHECK
# ============================================================

def check_stock():

    global CURRENT_SNAPSHOT
    global STATE

    print(
        "Checking Knife MFG..."
    )

    try:

        products = fetch_products()

        current = snapshot(
            products
        )

        with LOCK:

            old = STATE.get(
                "v",
                {}
            )

            initialized = STATE.get(
                "initialized",
                False
            )

        print(
            "Products:",
            len(products),
            "| Variants:",
            len(current)
        )

        # First run creates baseline
        if not initialized:

            print(
                "Creating first stock baseline."
            )

            with LOCK:

                STATE["v"] = current

                STATE["initialized"] = True

                CURRENT_SNAPSHOT = current

                save_state(
                    STATE
                )

            return

        # Detect unavailable -> available
        restocked = []

        for variant_id, item in current.items():

            old_item = old.get(
                variant_id,
                {}
            )

            was_available = bool(
                old_item.get(
                    "available",
                    False
                )
            )

            is_available = bool(
                item.get(
                    "available",
                    False
                )
            )

            if (
                is_available
                and not was_available
            ):

                restocked.append(
                    item
                )

        # Update cached stock FIRST
        with LOCK:

            CURRENT_SNAPSHOT = current

            STATE["v"] = current

            save_state(
                STATE
            )

        if restocked:

            print(
                "RESTOCK FOUND:",
                len(restocked)
            )

            for item in restocked:

                try:

                    send_alert(
                        item
                    )

                except Exception as error:

                    print(
                        "Alert error:",
                        error
                    )

        else:

            print(
                "No restocks."
            )

    except Exception as error:

        print(
            "Stock check error:",
            error,
            file=sys.stderr
        )


# ============================================================
# TELEGRAM SETUP
# ============================================================

def setup_telegram():

    print(
        "Connecting to Telegram..."
    )

    me = tg(
        "getMe"
    )

    bot = me.get(
        "result",
        {}
    )

    print(
        "Bot:",
        bot.get("username")
    )

    # Remove webhook
    webhook = tg(
        "getWebhookInfo"
    )

    webhook_url = (
        webhook
        .get("result", {})
        .get("url", "")
    )

    if webhook_url:

        print(
            "Webhook detected."
        )

        tg(
            "deleteWebhook",
            {
                "drop_pending_updates":
                    "false"
            }
        )

        print(
            "Webhook removed."
        )

    # Command menu
    tg(

        "setMyCommands",

        {
            "commands":
                json.dumps(
                    [
                        {
                            "command":
                                "status",

                            "description":
                                "Check live stock",
                        },
                        {
                            "command":
                                "help",

                            "description":
                                "Show bot info",
                        },
                    ]
                )
        }
    )

    print(
        "Telegram ready."
    )


# ============================================================
# MAIN
# ============================================================

def main():

    global STATE

    print(
        "================================"
    )

    print(
        "KNIFE MFG • DROP RADAR"
    )

    print(
        "FAST RAILWAY MODE"
    )

    print(
        "================================"
    )

    print(
        f"Stock scan: every "
        f"{CHECK_INTERVAL}s"
    )

    print(
        "Telegram: LONG POLLING"
    )

    print(
        "Commands: NEAR-INSTANT"
    )

    print(
        "================================"
    )

    # Load state
    STATE = load_state()

    # Telegram
    setup_telegram()

    # Initial stock check
    check_stock()

    # Start FAST Telegram listener
    listener = threading.Thread(
        target=telegram_listener,
        daemon=True
    )

    listener.start()

    print(
        "================================"
    )

    print(
        "🟢 BOT ONLINE 24/7"
    )

    print(
        "⚡ TELEGRAM COMMANDS ACTIVE"
    )

    print(
        "🔄 STOCK SCANNER ACTIVE"
    )

    print(
        "================================"
    )

    # Main stock loop
    while True:

        time.sleep(
            CHECK_INTERVAL
        )

        check_stock()


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    try:

        main()

    except KeyboardInterrupt:

        print(
            "Bot stopped."
        )

    except Exception as error:

        print(
            "FATAL ERROR:",
            error,
            file=sys.stderr
        )

        sys.exit(1)
