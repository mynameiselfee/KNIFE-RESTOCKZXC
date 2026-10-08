import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error

# ============================================================
# CONFIG
# ============================================================

SITE = "https://knifemfg.co"

# If you create a Railway Volume mounted at /data,
# state will survive restarts.
#
# Without a volume, /tmp is used and state may reset after
# a redeploy/restart.
STATE_FILE = os.environ.get(
    "STATE_FILE",
    "/data/state.json"
)

SHAPES = ["KH1", "KL2", "KL1"]

CHECK_INTERVAL = int(
    os.environ.get(
        "CHECK_INTERVAL",
        "15"
    )
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 "
        "(Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/131.0 Safari/537.36"
    ),
    "Cache-Control": "no-cache",
}

LINE = "\u2501" * 14


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

        try:

            with urllib.request.urlopen(
                request,
                timeout=30
            ) as response:

                data = json.load(response)

        except Exception as error:

            print(
                "Knife MFG request failed:",
                error
            )

            raise

        batch = data.get(
            "products",
            []
        )

        if not batch:
            break

        products.extend(batch)

        page += 1

        # Safety limit
        if page > 50:
            print(
                "Reached page safety limit."
            )
            break

    return products


def snapshot(products):

    snap = {}

    for product in products:

        images = (
            product.get("images")
            or []
        )

        image = None

        if images:
            image = images[0].get(
                "src"
            )

        for variant in product.get(
            "variants",
            []
        ):

            variant_title = (
                variant.get("title")
                or ""
            )

            shape = shape_of(
                variant_title
            )

            if not shape:
                continue

            size = re.sub(
                rf"\b{shape}\b",
                "",
                variant_title,
                flags=re.I
            ).strip()

            variant_id = str(
                variant.get("id")
            )

            handle = product.get(
                "handle",
                ""
            )

            snap[variant_id] = {

                "name": product.get(
                    "title",
                    ""
                ),

                "shape": shape,

                "size": size,

                "price": variant.get(
                    "price"
                ),

                "image": image,

                "available": bool(
                    variant.get(
                        "available"
                    )
                ),

                "url": (
                    f"{SITE}/products/"
                    f"{handle}"
                    f"?variant={variant_id}"
                ),

                "cart": (
                    f"{SITE}/cart/"
                    f"{variant_id}:1"
                ),
            }

    return snap


# ============================================================
# TELEGRAM
# ============================================================

def telegram_token():

    token = os.environ.get(
        "TELEGRAM_BOT_TOKEN",
        ""
    ).strip()

    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not set."
        )

    return token


def telegram_chat_id():

    value = os.environ.get(
        "TELEGRAM_CHAT_ID",
        ""
    ).strip()

    if not value:
        raise RuntimeError(
            "TELEGRAM_CHAT_ID is not set."
        )

    return value


def tg(method, params=None):

    token = telegram_token()

    data = urllib.parse.urlencode(
        params or {}
    ).encode()

    url = (
        "https://api.telegram.org/"
        f"bot{token}/{method}"
    )

    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "User-Agent":
                "Knife-MFG-Restock-Bot/1.0"
        }
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=40
        ) as response:

            result = json.load(
                response
            )

    except urllib.error.HTTPError as error:

        body = error.read().decode(
            "utf-8",
            errors="replace"
        )

        raise RuntimeError(
            f"Telegram HTTP {error.code}: "
            f"{body}"
        )

    except Exception as error:

        raise RuntimeError(
            f"Telegram connection error: "
            f"{error}"
        )

    if not result.get("ok"):

        raise RuntimeError(
            f"Telegram API error: "
            f"{result}"
        )

    return result


# ============================================================
# TELEGRAM MESSAGES
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
            telegram_chat_id(),

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

        "\U0001F7E2 "
        "<b>DECK IN STOCK</b>",

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
        "<i>Drops go fast. "
        "Tap Add to cart.</i>",
    ])

    return "\n".join(lines)


def send_alert(item):

    caption = alert_caption(
        item
    )

    markup = buttons(

        (
            "\U0001F6D2 Add to cart",
            item["cart"]
        ),

        (
            "\U0001F517 Open page",
            item["url"]
        ),
    )

    # Try sending the product image
    if item.get("image"):

        try:

            tg(
                "sendPhoto",
                {
                    "chat_id":
                        telegram_chat_id(),

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
                "ALERT SENT:",
                item["name"],
                "|",
                item["shape"]
            )

            return

        except Exception as error:

            print(
                "Photo failed. "
                "Falling back to text:",
                error
            )

    send_text(
        caption,
        markup
    )

    print(
        "TEXT ALERT SENT:",
        item["name"],
        "|",
        item["shape"]
    )


def send_alerts(items):

    for item in items[:3]:

        send_alert(item)

    remaining = items[3:]

    if remaining:

        lines = [

            "\U0001F7E2 "
            "<b>MORE IN STOCK</b>",

            LINE,
        ]

        for item in remaining:

            lines.append(

                f"\u2022 "
                f"<b>{esc(item['shape'])}</b> "
                f"{esc(item['name'])}"
            )

        send_text(

            "\n".join(lines),

            buttons(
                (
                    "\U0001F6D2 "
                    "Open shop",
                    SITE
                )
            )
        )


# ============================================================
# STATUS
# ============================================================

def status_text(snapshot_data):

    lines = [

        "\U0001F4E1 "
        "<b>Knife MFG "
        "\u00B7 deck radar</b>",

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
                f"{len(live)} live"
            )

            for item in live[:6]:

                lines.append(

                    f"    \u2022 "
                    f"{esc(item['name'])}"
                )

        else:

            lines.append(

                f"\u26AB "
                f"<b>{shape}</b> "
                f"\u00B7 sold out"
            )

    lines.extend([

        LINE,

        "\U0001F552 "
        "checked just now",

        "\U0001F7E2 "
        "Bot is running 24/7",
    ])

    return "\n".join(lines)


HELP = (

    "\U0001F6F9 "
    "<b>Knife MFG Restock Bot</b>\n"

    + LINE

    + "\n"

    "I'm watching KH1, KL2 and KL1 decks "
    "and will automatically notify you "
    "when they come back in stock.\n\n"

    "<b>Commands</b>\n"

    "/status \u2013 check current stock\n"

    "/help \u2013 show this message\n\n"

    "\U0001F7E2 "
    "Automatic restock alerts: ON"
)


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

def handle_commands(
    state,
    snapshot_data
):

    offset = int(
        state.get(
            "offset",
            0
        )
    )

    try:

        result = tg(

            "getUpdates",

            {
                "offset":
                    offset,

                "timeout":
                    5,

                "allowed_updates":
                    json.dumps(
                        ["message"]
                    ),
            }
        )

    except Exception as error:

        print(
            "Telegram getUpdates error:",
            error
        )

        return

    updates = result.get(
        "result",
        []
    )

    if updates:

        print(
            "Telegram updates:",
            len(updates)
        )

    for update in updates:

        update_id = update.get(
            "update_id"
        )

        if update_id is not None:

            state["offset"] = (
                update_id + 1
            )

        message = (
            update.get("message")
            or {}
        )

        incoming_chat = (
            message.get("chat")
            or {}
        )

        incoming_chat_id = str(
            incoming_chat.get(
                "id",
                ""
            )
        )

        # SECURITY:
        # Only your Telegram chat can
        # control this bot.
        if incoming_chat_id != str(
            telegram_chat_id()
        ):

            continue

        text = (
            message.get("text")
            or ""
        ).strip()

        if not text:
            continue

        parts = text.split()

        command = (
            parts[0]
            .split("@")[0]
            .lower()
        )

        print(
            "COMMAND:",
            command
        )

        if command in (
            "/status",
            "/now",
            "/stock",
        ):

            send_text(

                status_text(
                    snapshot_data
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


# ============================================================
# STATE
# ============================================================

def default_state():

    return {
        "initialized": False,
        "offset": 0,
        "v": {},
    }


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

        return default_state()

    try:

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as file:

            data = json.load(file)

        if not isinstance(
            data,
            dict
        ):

            return default_state()

        return data

    except Exception as error:

        print(
            "Could not load state:",
            error
        )

        return default_state()


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

    temp_file = (
        STATE_FILE + ".tmp"
    )

    try:

        with open(
            temp_file,
            "w",
            encoding="utf-8"
        ) as file:

            json.dump(
                state,
                file,
                indent=2
            )

        os.replace(
            temp_file,
            STATE_FILE
        )

    except Exception as error:

        print(
            "Could not save state:",
            error
        )


# ============================================================
# STOCK CHECK
# ============================================================

def check_stock():

    print(
        "\n"
        "================================"
    )

    print(
        "Checking Knife MFG..."
    )

    products = fetch_products()

    print(
        "Products found:",
        len(products)
    )

    current = snapshot(
        products
    )

    print(
        "Tracked variants:",
        len(current)
    )

    state = load_state()

    old = state.get(
        "v",
        {}
    )

    initialized = bool(
        state.get(
            "initialized",
            False
        )
    )

    # First run:
    # Save current stock as baseline.
    #
    # This prevents the bot from sending
    # 20 alerts immediately when Railway
    # first starts.
    if not initialized:

        print(
            "FIRST RUN: "
            "creating stock baseline."
        )

        state["v"] = current

        state["initialized"] = True

        save_state(
            state
        )

        return current, state

    # Find items that changed from
    # unavailable -> available.
    restocked = []

    for variant_id, item in current.items():

        was_available = bool(
            old.get(
                variant_id,
                {}
            ).get(
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

    if restocked:

        print(
            "RESTOCKS FOUND:",
            len(restocked)
        )

        try:

            send_alerts(
                restocked
            )

        except Exception as error:

            print(
                "Could not send restock "
                "alert:",
                error
            )

    else:

        print(
            "No restocks."
        )

    state["v"] = current

    save_state(
        state
    )

    return current, state


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
        "Telegram bot:",
        bot.get("username")
    )

    print(
        "Bot ID:",
        bot.get("id")
    )

    # --------------------------------------------------------
    # Remove webhook because this bot uses polling.
    # --------------------------------------------------------

    webhook = tg(
        "getWebhookInfo"
    )

    webhook_result = webhook.get(
        "result",
        {}
    )

    webhook_url = webhook_result.get(
        "url",
        ""
    )

    if webhook_url:

        print(
            "Webhook detected:"
        )

        print(
            webhook_url
        )

        print(
            "Removing webhook..."
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

    else:

        print(
            "No webhook detected."
        )

    # --------------------------------------------------------
    # Telegram command menu
    # --------------------------------------------------------

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
                                "Check current stock",
                        },
                        {
                            "command":
                                "help",

                            "description":
                                "Show bot help",
                        },
                    ]
                )
        }
    )

    print(
        "Telegram command menu set."
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "================================"
    )

    print(
        "KNIFE MFG RESTOCK BOT"
    )

    print(
        "Railway 24/7 mode"
    )

    print(
        "================================"
    )

    print(
        "Check interval:",
        CHECK_INTERVAL,
        "seconds"
    )

    print(
        "State file:",
        STATE_FILE
    )

    # Telegram setup
    setup_telegram()

    # Initial stock check
    current, state = check_stock()

    # Process any Telegram commands
    handle_commands(
        state,
        current
    )

    save_state(
        state
    )

    print(
        "================================"
    )

    print(
        "BOT IS NOW RUNNING 24/7"
    )

    print(
        "Commands: /status /help"
    )

    print(
        "================================"
    )

    # --------------------------------------------------------
    # FOREVER LOOP
    # --------------------------------------------------------

    while True:

        try:

            time.sleep(
                CHECK_INTERVAL
            )

            current, state = (
                check_stock()
            )

            # Check Telegram commands
            handle_commands(
                state,
                current
            )

            save_state(
                state
            )

        except KeyboardInterrupt:

            print(
                "Bot stopped."
            )

            break

        except Exception as error:

            print(
                "MAIN LOOP ERROR:",
                error,
                file=sys.stderr
            )

            # Don't kill the bot because
            # of a temporary network error.
            time.sleep(5)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    try:

        main()

    except Exception as error:

        print(
            "FATAL ERROR:",
            error,
            file=sys.stderr
        )

        sys.exit(1)
