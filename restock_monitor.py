import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
SITE = "https://knifemfg.co"
STATE_FILE = "state.json"
SHAPES = ["KH1", "KL2", "KL1"]
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Cache-Control": "no-cache",
}
LINE = "\u2501" * 14
# ============================================================
# HELPERS
# ============================================================
def esc(x):
    return html.escape(str(x or ""))
def shape_of(title):
    clean = " " + re.sub(
        r"[^A-Z0-9]+",
        " ",
        (title or "").upper()
    ) + " "
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
        req = urllib.request.Request(
            url,
            headers=HEADERS
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.load(response)
        batch = data.get("products", [])
        if not batch:
            break
        products.extend(batch)
        page += 1
    return products
def snapshot(products):
    snap = {}
    for product in products:
        images = product.get("images") or []
        image = None
        if images:
            image = images[0].get("src")
        for variant in product.get("variants", []):
            variant_title = variant.get("title") or ""
            shape = shape_of(variant_title)
            if not shape:
                continue
            size = re.sub(
                rf"\b{shape}\b",
                "",
                variant_title,
                flags=re.I
            ).strip()
            variant_id = str(variant["id"])
            snap[variant_id] = {
                "name": product.get("title", ""),
                "shape": shape,
                "size": size,
                "price": variant.get("price"),
                "image": image,
                "available": bool(
                    variant.get("available")
                ),
                "url": (
                    f"{SITE}/products/"
                    f"{product['handle']}"
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
def tg(method, params=None):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is missing"
        )
    data = urllib.parse.urlencode(
        params or {}
    ).encode()
    url = (
        f"https://api.telegram.org/"
        f"bot{token}/{method}"
    )
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "User-Agent": "KnifeMFG-RestockBot/1.0"
        }
    )
    with urllib.request.urlopen(
        request,
        timeout=35
    ) as response:
        result = json.load(response)
    if not result.get("ok"):
        raise RuntimeError(
            f"Telegram API error: {result}"
        )
    return result
def chat_id():
    value = os.environ.get(
        "TELEGRAM_CHAT_ID",
        ""
    ).strip()
    if not value:
        raise RuntimeError(
            "TELEGRAM_CHAT_ID is missing"
        )
    return value
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
def send_text(text, markup=None):
    params = {
        "chat_id": chat_id(),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }
    if markup:
        params["reply_markup"] = markup
    return tg(
        "sendMessage",
        params
    )
# ============================================================
# ALERTS
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
        "\U0001F7E2 <b>DECK IN STOCK</b>",
        LINE,
        f"<b>{esc(item['name'])}</b>",
        shape,
    ]
    if item.get("price"):
        lines.append(
            f"\U0001F4B5 {esc(item['price'])}"
        )
    lines += [
        LINE,
        "\u26A1 <i>Drops go fast. "
        "Tap Add to cart.</i>"
    ]
    return "\n".join(lines)
def send_alert(item):
    caption = alert_caption(item)
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
    if item.get("image"):
        try:
            tg(
                "sendPhoto",
                {
                    "chat_id": chat_id(),
                    "photo": item["image"],
                    "caption": caption,
                    "parse_mode": "HTML",
                    "reply_markup": markup,
                }
            )
            print(
                f"Telegram alert sent: "
                f"{item['name']} / "
                f"{item['shape']}"
            )
            return
        except Exception as error:
            print(
                "Photo failed:",
                error
            )
    send_text(
        caption,
        markup
    )
def send_alerts(items):
    for item in items[:3]:
        send_alert(item)
    remaining = items[3:]
    if remaining:
        lines = [
            "\U0001F7E2 <b>MORE IN STOCK</b>",
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
                    "\U0001F6D2 Open shop",
                    SITE
                )
            )
        )
# ============================================================
# STATUS / HELP
# ============================================================
def status_text(snapshot_data):
    lines = [
        "\U0001F4E1 "
        "<b>Knife MFG \u00B7 deck radar</b>",
        LINE,
    ]
    for shape in SHAPES:
        live = [
            item
            for item in snapshot_data.values()
            if (
                item["shape"] == shape
                and item["available"]
            )
        ]
        if live:
            lines.append(
                f"\U0001F7E2 "
                f"<b>{shape}</b> "
                f"\u00B7 {len(live)} live"
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
    lines += [
        LINE,
        "\U0001F552 checked just now",
    ]
    return "\n".join(lines)
HELP = (
    "\U0001F6F9 "
    "<b>Knife MFG restock bot</b>\n"
    + LINE
    + "\n"
    "I watch KH1, KL2 and KL1 decks "
    "and ping you when one is back.\n\n"
    "/status \u2013 what's in stock right now\n"
    "/help \u2013 show this message\n"
    "/start \u2013 start the bot"
)
# ============================================================
# TELEGRAM COMMANDS
# ============================================================
def handle_commands(state, snapshot_data):
    offset = int(
        state.get("offset", 0)
    )
    try:
        result = tg(
            "getUpdates",
            {
                "offset": offset,
                "timeout": 5,
                "allowed_updates": json.dumps(
                    ["message"]
                ),
            }
        )
    except Exception as error:
        print(
            "getUpdates error:",
            error
        )
        return
    updates = result.get(
        "result",
        []
    )
    print(
        f"Telegram updates received: "
        f"{len(updates)}"
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
        message_chat = (
            message.get("chat")
            or {}
        )
        incoming_chat_id = str(
            message_chat.get("id", "")
        )
        if incoming_chat_id != str(
            chat_id()
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
            f"Telegram command received: "
            f"{command}"
        )
        if command in (
            "/status",
            "/now",
            "/stock",
        ):
            send_text(
                status_text(snapshot_data),
                buttons(
                    (
                        "\U0001F6D2 Open shop",
                        SITE
                    )
                )
            )
        elif command in (
            "/start",
            "/help",
        ):
            send_text(HELP)
# ============================================================
# STATE
# ============================================================
def load_state():
    if not os.path.exists(
        STATE_FILE
    ):
        return {
            "v": {},
            "offset": 0,
        }
    try:
        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as file:
            data = json.load(file)
        if isinstance(
            data.get("v"),
            dict
        ):
            return data
        return {
            "v": data,
            "offset": 0,
        }
    except Exception:
        print(
            "State file invalid. "
            "Starting fresh."
        )
        return {
            "v": {},
            "offset": 0,
        }
def save_state(state):
    temp_file = (
        STATE_FILE + ".tmp"
    )
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
# ============================================================
# ONE CHECK
# ============================================================
def check_once():
    print("Checking Knife MFG...")
    products = fetch_products()
    print(
        f"Products fetched: "
        f"{len(products)}"
    )
    snapshot_data = snapshot(
        products
    )
    state = load_state()
    old = state.get(
        "v",
        {}
    )
    restocked = [
        item
        for variant_id, item
        in snapshot_data.items()
        if (
            item["available"]
            and not old.get(
                variant_id,
                {}
            ).get(
                "available",
                False
            )
        )
    ]
    if restocked:
        print(
            f"RESTOCKS FOUND: "
            f"{len(restocked)}"
        )
        send_alerts(restocked)
    else:
        print(
            "No restocks."
        )
    # Always process Telegram commands
    handle_commands(
        state,
        snapshot_data
    )
    state["v"] = snapshot_data
    save_state(state)
    print(
        "State saved successfully."
    )
# ============================================================
# TELEGRAM SETUP
# ============================================================
def setup_telegram():
    print(
        "Checking Telegram bot..."
    )
    me = tg("getMe")
    bot = me.get(
        "result",
        {}
    )
    print(
        "Bot:",
        bot.get("username")
    )
    # We use polling, therefore make
    # absolutely sure no webhook is active.
    webhook = tg(
        "getWebhookInfo"
    )
    webhook_url = (
        webhook.get("result", {})
        .get("url", "")
    )
    if webhook_url:
        print(
            "Webhook detected. "
            "Deleting webhook..."
        )
        tg(
            "deleteWebhook",
            {
                "drop_pending_updates":
                    "false"
            }
        )
    tg(
        "setMyCommands",
        {
            "commands": json.dumps(
                [
                    {
                        "command": "status",
                        "description":
                            "What's in stock right now",
                    },
                    {
                        "command": "help",
                        "description":
                            "About this bot",
                    },
                ]
            )
        }
    )
    print(
        "Telegram setup complete."
    )
# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    try:
        setup_telegram()
        # Run immediately
        check_once()
        # Keep the runner alive for the
        # requested amount of time.
        #
        # Example:
        # LOOP_SECONDS=240
        #
        loop_seconds = int(
            os.environ.get(
                "LOOP_SECONDS",
                "240"
            )
        )
        interval = float(
            os.environ.get(
                "INTERVAL",
                "30"
            )
        )
        started = time.time()
        print(
            f"Bot loop started. "
            f"Running for "
            f"{loop_seconds} seconds."
        )
        while (
            time.time() - started
            < loop_seconds
        ):
            time.sleep(interval)
            try:
                check_once()
            except Exception as error:
                print(
                    "Loop error:",
                    error,
                    file=sys.stderr
                )
        print(
            "Run finished normally."
        )
    except Exception as error:
        print(
            "FATAL ERROR:",
            error,
            file=sys.stderr
        )
        sys.exit(1)
