import html
import urllib.error
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request


# ============================================================
# CONFIG
# ============================================================

SITE = "https://knifemfg.co"
SHAPES = ["KH1", "KL2", "KL1", "KB1"]

CHECK_INTERVAL = max(2.0, float(os.getenv("CHECK_INTERVAL", "5")))
STARTUP_PING = os.getenv("STARTUP_PING", "1") == "1"
OFFLINE_AFTER = 6            # failed checks in a row before a warning
MAX_ALERTS_PER_DROP = 4      # photo alerts per check, the rest go in one list

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; KnifeMFG-DeckRadar/1.1)",
    "Accept": "application/json,text/plain,*/*",
    "Cache-Control": "no-cache",
}

LINE = "━━━━━━━━━━━━━━"


def log(message):
    print(f"[RADAR] {message}", flush=True)


# ============================================================
# STATE FILE
# Uses /data (Railway volume) when it is writable, otherwise
# falls back to a local file so the radar never crashes.
# ============================================================

def pick_state_file():
    wanted = os.getenv("STATE_FILE", "/data/state.json")
    try:
        os.makedirs(os.path.dirname(wanted) or ".", exist_ok=True)
        probe = wanted + ".probe"
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        return wanted
    except Exception as e:
        log(f"State path {wanted} is not writable ({e}). Using ./state.json")
        log("Add a Railway Volume mounted at /data to keep state between deploys.")
        return "state.json"


STATE_FILE = pick_state_file()

STATE = {}                 # variant_id -> was in stock (bool)
CURRENT_SNAPSHOT = {}      # variant_id -> item dict
LOCK = threading.Lock()

# ---- USD -> SGD (the shop's base prices are in USD) ----
# Set USD_TO_SGD on Railway to pin a fixed rate instead of the live one.
FX_FIXED = bool(os.getenv("USD_TO_SGD"))
FX = {"rate": float(os.getenv("USD_TO_SGD", "1.29")), "t": 0.0}
FX_SOURCES = [
    ("https://api.frankfurter.app/latest?from=USD&to=SGD", lambda d: d["rates"]["SGD"]),
    ("https://open.er-api.com/v6/latest/USD", lambda d: d["rates"]["SGD"]),
]


def refresh_fx():
    """Update the rate at most every 6 hours. Never raises."""
    if FX_FIXED or time.time() - FX["t"] < 6 * 3600:
        return
    for url, pick in FX_SOURCES:
        try:
            rate = float(pick(json.loads(fetch_url(url, timeout=8).decode("utf-8"))))
            if 0.5 < rate < 3:
                FX["rate"], FX["t"] = rate, time.time()
                log(f"USD to SGD rate: {rate:.4f}")
                return
        except Exception as e:
            log(f"Rate source failed: {e}")
    FX["t"] = time.time() - 6 * 3600 + 600  # try again in 10 minutes

LAST_CHECK = 0.0           # when the store was last read successfully
STARTED = time.time()
META_FILE = STATE_FILE + ".meta"
META = {"last_drop": None}  # {"t", "shapes", "product"}


def load_meta():
    try:
        if os.path.exists(META_FILE):
            with open(META_FILE, "r", encoding="utf-8") as f:
                META.update(json.load(f))
    except Exception as e:
        log(f"Could not load meta: {e}")


def save_meta():
    try:
        temp = META_FILE + ".tmp"
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(META, f)
        os.replace(temp, META_FILE)
    except Exception as e:
        log(f"Could not save meta: {e}")


def remember_drop(items):
    first = group_by_deck(items)[0]
    META["last_drop"] = {
        "t": time.time(),
        "shapes": [it["shape"] for it in first],
        "product": first[0]["product"],
    }
    save_meta()


def esc(text):
    return html.escape(str(text or ""))


# ============================================================
# FETCH
# ============================================================

class FetchError(Exception):
    pass


def fetch_url(url, timeout=20):
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def fetch_products():
    """All products, or raises FetchError. Never returns a partial list."""
    products = []
    stamp = int(time.time() * 1000)  # cache buster for the store's CDN
    for page in range(1, 21):
        url = f"{SITE}/products.json?limit=250&page={page}&_={stamp}"
        try:
            data = json.loads(fetch_url(url).decode("utf-8"))
        except Exception as e:
            raise FetchError(f"page {page}: {e}")
        batch = data.get("products", [])
        products.extend(batch)
        if len(batch) < 250:
            break
    if not products:
        raise FetchError("empty product list")
    return products


# ============================================================
# SNAPSHOT
# ============================================================

def extract_shape(title):
    """Whole-word match, so KL1 never matches KL10 or XKL1."""
    clean = " " + re.sub(r"[^A-Z0-9]+", " ", str(title or "").upper()) + " "
    for shape in SHAPES:
        if f" {shape} " in clean:
            return shape
    return None


def build_snapshot(products):
    snapshot = {}
    for product in products:
        name = product.get("title", "Unknown Product")
        handle = product.get("handle", "")
        base_url = f"{SITE}/products/{handle}" if handle else SITE
        images = product.get("images") or []
        image = images[0].get("src", "") if images else ""

        for variant in product.get("variants", []):
            vid = str(variant.get("id", ""))
            if not vid:
                continue
            shape = extract_shape(variant.get("title", ""))
            if not shape:
                continue
            title = variant.get("title", "")
            size = re.sub(rf"\b{shape}\b", "", title, flags=re.I).strip(" -/·")
            snapshot[vid] = {
                "id": vid,
                "product": name,
                "handle": handle or name,
                "variant": title,
                "size": size,
                "shape": shape,
                "price": variant.get("price", ""),
                "available": bool(variant.get("available", False)),
                "image": image,
                "product_url": base_url,
                "variant_url": f"{base_url}?variant={vid}",
                "cart_url": f"{SITE}/cart/{vid}:1",
            }
    return snapshot


# ============================================================
# TELEGRAM API
# ============================================================

def telegram_api(method, params=None, _retry=True):
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set.")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    encoded = urllib.parse.urlencode(params or {}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=encoded,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8")[:200]
        except Exception:
            pass
        if e.code == 429 and _retry:
            try:
                wait = json.loads(body).get("parameters", {}).get("retry_after", 2)
            except Exception:
                wait = 2
            log(f"Telegram asked to slow down, waiting {wait}s")
            time.sleep(min(int(wait), 10))
            return telegram_api(method, params, _retry=False)
        log(f"Telegram {method} HTTP {e.code}: {body}")
        return {"ok": False, "error": f"HTTP {e.code}"}
    except Exception as e:
        log(f"Telegram {method} error: {e}")
        return {"ok": False, "error": str(e)}

    if not result.get("ok"):
        log(f"Telegram {method} not ok: {result}")
    return result


def send_text(text, reply_markup=None):
    params = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }
    if reply_markup:
        params["reply_markup"] = json.dumps(reply_markup)
    return telegram_api("sendMessage", params)


def send_photo(photo, caption, reply_markup=None):
    params = {
        "chat_id": TELEGRAM_CHAT_ID,
        "photo": photo,
        "caption": caption,
        "parse_mode": "HTML",
    }
    if reply_markup:
        params["reply_markup"] = json.dumps(reply_markup)
    return telegram_api("sendPhoto", params)


# ============================================================
# ALERTS
# One alert per DECK. If several shapes restock together they
# share one photo and get one BUY button each.
# ============================================================

def group_by_deck(items):
    groups = {}
    for item in items:
        groups.setdefault(item["handle"], []).append(item)
    order = {s: i for i, s in enumerate(SHAPES)}
    result = []
    for group in groups.values():
        group.sort(key=lambda it: order.get(it["shape"], 99))
        result.append(group)
    return result


def alert_caption(group, test=False):
    """Same look as the original alert: photo on top, then
    TEST ALERT / DECK IN STOCK, name, shape, price, buttons."""
    head = "🧪 <b>TEST ALERT</b>" if test else "🟢 <b>DECK IN STOCK</b>"
    prices = {money(it["price"]) for it in group}
    one_price = len(prices) == 1 and "" not in prices

    lines = [head, LINE, f"<b>{esc(group[0]['product'])}</b>"]
    for it in group:
        row = f"🛹 <b>{esc(it['shape'])}</b>"
        if it["size"]:
            row += f" · {esc(it['size'])}"
        if not one_price and money(it["price"]):
            row += f" · {money(it['price'])}"
        lines.append(row)
    if one_price:
        lines.append(f"💵 {prices.pop()}")

    lines.append(LINE)
    if test:
        lines.append("⚡ <i>This is a sample. Nothing just restocked.</i>")
    else:
        lines.append("⚡ <i>Drops go fast. Tap Add to cart.</i>")
    return "\n".join(lines)


def alert_buttons(group):
    if len(group) == 1:
        return {"inline_keyboard": [[
            {"text": "🛒 Add to cart", "url": group[0]["cart_url"]},
            {"text": "🔗 Open page", "url": group[0]["product_url"]},
        ]]}
    buy = [
        {"text": f"🛒 {it['shape']}", "url": it["cart_url"]}
        for it in group
    ]
    view = [{"text": "🔗 Open page", "url": group[0]["product_url"]}]
    return {"inline_keyboard": [buy, view]}


def send_alert(group, test=False):
    log(
        ("TEST ALERT: " if test else "RESTOCK DETECTED: ")
        + f"{group[0]['product']} ({', '.join(it['shape'] for it in group)})"
    )
    caption = alert_caption(group, test)
    markup = alert_buttons(group)
    image = group[0].get("image")

    if image:
        if send_photo(image, caption, markup).get("ok"):
            return
    # Fallback: same message as text, buttons kept
    send_text(caption, markup)


def send_alerts(items):
    groups = group_by_deck(items)
    for group in groups[:MAX_ALERTS_PER_DROP]:
        send_alert(group)
    rest = groups[MAX_ALERTS_PER_DROP:]
    if rest:
        lines = ["🟢 <b>MORE DECKS IN STOCK</b>", LINE]
        for group in rest:
            shapes = " ".join(it["shape"] for it in group)
            lines.append(f"• <b>{esc(shapes)}</b> {esc(group[0]['product'])}")
        send_text(
            "\n".join(lines),
            {"inline_keyboard": [[{"text": "🔗 Open shop", "url": SITE}]]},
        )


# ============================================================
# /STATUS and /HELP
# ============================================================

def ago(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def money(price):
    """Shop price (USD) shown as approximate Singapore dollars."""
    try:
        return f"~S${float(price) * FX['rate']:.0f}"
    except Exception:
        return ""


STATUS_MARKUP = {
    "inline_keyboard": [[
        {"text": "🔄 Refresh", "callback_data": "refresh"},
        {"text": "🛒 Open shop", "url": SITE},
    ]]
}


def status_text():
    with LOCK:
        snapshot = dict(CURRENT_SNAPSHOT)
        last_check = LAST_CHECK
        last_drop = META.get("last_drop")

    head = ["📡 <b>Knife MFG · deck radar</b>", LINE]

    if not snapshot:
        return "\n".join(head + ["⏳ warming up, try again in a few seconds"])

    lines = list(head)
    for shape in SHAPES:
        live = [
            it for it in snapshot.values()
            if it["shape"] == shape and it["available"]
        ]
        if live:
            lines.append(f"🟢 <b>{shape}</b> · {len(live)} in stock")
            for it in live[:5]:
                price = money(it["price"])
                tail = f" · {price}" if price else ""
                lines.append(f"      ▸ {esc(it['product'])}{tail}")
            if len(live) > 5:
                lines.append(f"      ▸ +{len(live) - 5} more")
        else:
            lines.append(f"⚫ <b>{shape}</b> · sold out")

    lines.append(LINE)
    age = time.time() - last_check
    if age > max(30, CHECK_INTERVAL * 6):
        lines.append(f"⚠️ store unreachable · data is {ago(age)[:-4]} old")
    else:
        lines.append(f"🕒 checked {ago(age)} · every {CHECK_INTERVAL:g}s")
    if last_drop:
        what = " ".join(last_drop["shapes"])
        lines.append(
            f"🎯 last drop · {what} {esc(last_drop['product'])} · {ago(time.time() - last_drop['t'])}"
        )
    return "\n".join(lines)


def help_text():
    shapes = "  ".join(f"🎯 {shape}" for shape in SHAPES)
    return (
        "📡 <b>Knife MFG · deck radar</b>\n"
        f"{LINE}\n"
        "I watch the shop 24/7 and ping you the second a deck is back.\n"
        "\n"
        f"{shapes}\n"
        "\n"
        "<b>Commands</b>\n"
        "/status — stock right now\n"
        "/help — this screen\n"
        f"{LINE}\n"
        f"🟢 online · checking every {CHECK_INTERVAL:g}s\n"
        f"💱 prices ≈ S$ (1 USD = {FX['rate']:.2f})"
    )


HELP_MARKUP = {
    "inline_keyboard": [[
        {"text": "📡 Status", "callback_data": "refresh"},
        {"text": "🛒 Open shop", "url": SITE},
    ]]
}


def send_test_alert():
    """Hidden command: /test sends a sample alert (not in the menu)."""
    with LOCK:
        items = list(CURRENT_SNAPSHOT.values())
    if not items:
        send_text("⏳ No data yet, try again in a few seconds.")
        return
    pick = [it for it in items if it["available"]] or items
    first = pick[0]
    group = [it for it in items if it["handle"] == first["handle"]][:3]
    send_alert(group, test=True)


# ============================================================
# STATE LOAD / SAVE
# ============================================================

def load_state():
    global STATE
    try:
        if not os.path.exists(STATE_FILE):
            STATE = {}
            log("No previous state found.")
            return
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        # accepts {"id": true/false} (this script) and ignores other formats
        STATE = {k: bool(v) for k, v in raw.items() if isinstance(v, bool)}
        log(f"Loaded state: {len(STATE)} variants")
    except Exception as e:
        log(f"Could not load state: {e}")
        STATE = {}


def save_state():
    try:
        temp = STATE_FILE + ".tmp"
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(STATE, f)
        os.replace(temp, STATE_FILE)
    except Exception as e:
        log(f"Could not save state: {e}")


# ============================================================
# STOCK CHECK
# Raises FetchError on any problem, so the snapshot and state
# are never replaced by half-fetched data.
# ============================================================

_last_available = None
_quiet_checks = 0


def check_stock():
    global CURRENT_SNAPSHOT, LAST_CHECK, _last_available, _quiet_checks

    snapshot = build_snapshot(fetch_products())
    if not snapshot:
        raise FetchError("no tracked shapes in the feed")

    alerts = []
    changed = False

    with LOCK:
        first_run = not STATE
        for key, item in snapshot.items():
            now = item["available"]
            was = STATE.get(key, False)
            if not first_run and now and not was:
                alerts.append(item)
            if STATE.get(key) != now:
                STATE[key] = now
                changed = True
        CURRENT_SNAPSHOT = snapshot
        LAST_CHECK = time.time()
        if changed:
            save_state()

    available = sum(1 for it in snapshot.values() if it["available"])

    if first_run:
        log(f"Baseline created: {len(snapshot)} variants, {available} in stock")
        return

    if alerts:
        remember_drop(alerts)
        send_alerts(alerts)

    # quiet logging: only when something changes, or every ~5 minutes
    _quiet_checks += 1
    if available != _last_available or _quiet_checks >= 60:
        log(f"{len(snapshot)} variants | {available} in stock")
        _last_available = available
        _quiet_checks = 0


# ============================================================
# TELEGRAM LISTENER
# ============================================================

def drop_stale_updates():
    """Skip commands sent while the radar was off, so a restart
    never replays old /status or /test messages."""
    result = telegram_api("getUpdates", {"offset": -1, "timeout": 0})
    updates = result.get("result", []) if result.get("ok") else []
    if updates:
        return updates[-1]["update_id"] + 1
    return None


def handle_command(command):
    if command == "/status":
        send_text(status_text(), STATUS_MARKUP)
    elif command in ("/help", "/start"):
        send_text(help_text(), HELP_MARKUP)
    elif command == "/test":
        send_test_alert()
    else:
        send_text("❓ Unknown command.\n\nUse /help.")


def handle_callback(query):
    chat_id = str(query.get("message", {}).get("chat", {}).get("id", ""))
    if chat_id != str(TELEGRAM_CHAT_ID):
        return
    telegram_api("answerCallbackQuery", {
        "callback_query_id": query["id"],
        "text": "Refreshed ✓",
    })
    if query.get("data") == "refresh":
        telegram_api("editMessageText", {
            "chat_id": chat_id,
            "message_id": query["message"]["message_id"],
            "text": status_text(),
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
            "reply_markup": json.dumps(STATUS_MARKUP),
        })


def telegram_listener(offset):
    log("Telegram listener started.")
    while True:
        try:
            params = {
                "timeout": 30,
                "allowed_updates": json.dumps(["message", "callback_query"]),
            }
            if offset is not None:
                params["offset"] = offset

            result = telegram_api("getUpdates", params)
            if not result.get("ok"):
                time.sleep(3)
                continue

            for update in result.get("result", []):
                offset = update["update_id"] + 1

                if update.get("callback_query"):
                    handle_callback(update["callback_query"])
                    continue

                message = update.get("message")
                if not message:
                    continue
                if str(message.get("chat", {}).get("id", "")) != str(TELEGRAM_CHAT_ID):
                    continue  # only your own chat
                text = (message.get("text") or "").strip()
                if not text.startswith("/"):
                    continue
                command = text.split()[0].lower().split("@")[0]
                log(f"Command received: {command}")
                handle_command(command)

        except Exception as e:
            log(f"Telegram listener error: {e}")
            time.sleep(3)


def setup_telegram():
    log("Checking Telegram connection...")
    result = telegram_api("getMe")
    if not result.get("ok"):
        raise RuntimeError("Telegram bot connection failed (check the token).")
    log(f"Telegram connected: @{result['result'].get('username')}")

    # long polling needs the webhook removed
    telegram_api("deleteWebhook", {"drop_pending_updates": "false"})

    commands = [
        {"command": "status", "description": "Check current stock"},
        {"command": "help", "description": "Show commands"},
    ]
    telegram_api("setMyCommands", {"commands": json.dumps(commands)})
    log("Telegram commands configured.")


# ============================================================
# MAIN
# ============================================================

def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set.")
    if not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID is not set.")

    log("========================================")
    log("📡 KNIFE MFG DECK RADAR")
    log("========================================")
    log(f"Watching: {', '.join(SHAPES)}")
    log(f"Check interval: {CHECK_INTERVAL:g} seconds")
    log(f"State file: {STATE_FILE}")

    load_state()
    load_meta()
    refresh_fx()
    setup_telegram()
    offset = drop_stale_updates()

    log("Performing initial stock check...")
    try:
        check_stock()
    except Exception as e:
        log(f"Initial check failed: {e}")

    threading.Thread(
        target=telegram_listener, args=(offset,), daemon=True
    ).start()

    if STARTUP_PING:
        with LOCK:
            total = len(CURRENT_SNAPSHOT)
            live = sum(1 for it in CURRENT_SNAPSHOT.values() if it["available"])
        send_text(
            "📡 <b>Deck radar online</b>\n"
            f"{LINE}\n"
            f"🎯 tracking {total} variants · {live} in stock\n"
            f"⚡ checking every {CHECK_INTERVAL:g}s"
        )

    log("🟢 RADAR ONLINE")

    fails = 0
    warned = False
    while True:
        started = time.time()
        refresh_fx()
        try:
            check_stock()
            if warned:
                send_text("🟢 <b>Radar is back online.</b>")
                warned = False
            fails = 0
        except Exception as e:
            fails += 1
            log(f"Check failed ({fails} in a row): {e}")
            if fails >= OFFLINE_AFTER and not warned:
                send_text(
                    "⚠️ <b>Radar can't reach the store.</b>\n"
                    "Still trying. I'll tell you when it's back."
                )
                warned = True

        # back off when the store is struggling or rate limiting
        delay = CHECK_INTERVAL if fails == 0 else min(60, CHECK_INTERVAL * 2 ** min(fails, 4))
        time.sleep(max(0, delay - (time.time() - started)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Radar stopped.")
    except Exception as e:
        log(f"FATAL ERROR: {e}")
        raise
