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
HEADERS = {"User-Agent": "Mozilla/5.0", "Cache-Control": "no-cache"}
LINE = "\u2501" * 14


def esc(x):
    return html.escape(str(x or ""))


def shape_of(title):
    clean = " " + re.sub(r"[^A-Z0-9]+", " ", (title or "").upper()) + " "
    for s in SHAPES:
        if f" {s} " in clean:
            return s
    return None


def fetch_products():
    products, page = [], 1
    while True:
        url = f"{SITE}/products.json?limit=250&page={page}&_={int(time.time() * 1000)}"
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as r:
            batch = json.load(r).get("products", [])
        if not batch:
            break
        products.extend(batch)
        page += 1
    return products


def snapshot(products):
    snap = {}
    for p in products:
        imgs = p.get("images") or []
        image = imgs[0].get("src") if imgs else None
        for v in p.get("variants", []):
            shape = shape_of(v.get("title"))
            if not shape:
                continue
            size = re.sub(rf"\b{shape}\b", "", v.get("title") or "", flags=re.I).strip()
            snap[str(v["id"])] = {
                "name": p["title"],
                "shape": shape,
                "size": size,
                "price": v.get("price"),
                "image": image,
                "available": bool(v.get("available")),
                "url": f"{SITE}/products/{p['handle']}?variant={v['id']}",
                "cart": f"{SITE}/cart/{v['id']}:1",
            }
    return snap


# ---------- Telegram ----------
def tg(method, params=None):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("Telegram token missing")
        return {}
    data = urllib.parse.urlencode(params or {}).encode()
    with urllib.request.urlopen(
        f"https://api.telegram.org/bot{token}/{method}", data=data, timeout=30
    ) as r:
        return json.load(r)


def chat_id():
    return os.environ.get("TELEGRAM_CHAT_ID", "")


def buttons(*pairs):
    return json.dumps({"inline_keyboard": [[{"text": t, "url": u} for t, u in pairs]]})


def send_text(text, markup=None):
    params = {
        "chat_id": chat_id(),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }
    if markup:
        params["reply_markup"] = markup
    return tg("sendMessage", params)


def alert_caption(it):
    shape = f"\U0001F6F9 <b>{esc(it['shape'])}</b>"
    if it.get("size"):
        shape += f" \u00B7 {esc(it['size'])}"
    lines = [
        "\U0001F7E2 <b>DECK IN STOCK</b>",
        LINE,
        f"<b>{esc(it['name'])}</b>",
        shape,
    ]
    if it.get("price"):
        lines.append(f"\U0001F4B5 {esc(it['price'])}")
    lines += [LINE, "\u26A1 <i>Drops go fast. Tap Add to cart.</i>"]
    return "\n".join(lines)


def send_alert(it):
    caption = alert_caption(it)
    markup = buttons(
        ("\U0001F6D2 Add to cart", it["cart"]), ("\U0001F517 Open page", it["url"])
    )
    if it.get("image"):
        try:
            tg("sendPhoto", {
                "chat_id": chat_id(),
                "photo": it["image"],
                "caption": caption,
                "parse_mode": "HTML",
                "reply_markup": markup,
            })
            return
        except Exception as e:
            print("Photo failed, sending text:", e)
    send_text(caption, markup)


def send_alerts(items):
    for it in items[:3]:
        send_alert(it)
    rest = items[3:]
    if rest:
        lines = ["\U0001F7E2 <b>MORE IN STOCK</b>", LINE]
        for it in rest:
            lines.append(f"\u2022 <b>{esc(it['shape'])}</b> {esc(it['name'])}")
        send_text("\n".join(lines), buttons(("\U0001F6D2 Open shop", SITE)))


def status_text(snap):
    lines = ["\U0001F4E1 <b>Knife MFG \u00B7 deck radar</b>", LINE]
    for sh in SHAPES:
        live = [v for v in snap.values() if v["shape"] == sh and v["available"]]
        if live:
            lines.append(f"\U0001F7E2 <b>{sh}</b> \u00B7 {len(live)} live")
            for v in live[:6]:
                lines.append(f"    \u2022 {esc(v['name'])}")
        else:
            lines.append(f"\u26AB <b>{sh}</b> \u00B7 sold out")
    lines += [LINE, "\U0001F552 checked just now"]
    return "\n".join(lines)


HELP = (
    "\U0001F6F9 <b>Knife MFG restock bot</b>\n" + LINE + "\n"
    "I watch KH1, KL2 and KL1 decks and ping you the moment one is back.\n\n"
    "/status \u2013 what's in stock right now\n"
    "/help \u2013 this message"
)


def handle_commands(state, snap):
    res = tg("getUpdates", {"offset": state.get("offset", 0), "timeout": 0})
    for u in res.get("result", []):
        state["offset"] = u["update_id"] + 1
        m = u.get("message") or {}
        if str((m.get("chat") or {}).get("id")) != str(chat_id()):
            continue
        parts = (m.get("text") or "").split()
        cmd = parts[0].split("@")[0].lower() if parts else ""
        if cmd in ("/status", "/now", "/stock"):
            send_text(status_text(snap), buttons(("\U0001F6D2 Open shop", SITE)))
        elif cmd in ("/start", "/help"):
            send_text(HELP)


# ---------- State ----------
def load_state():
    if os.path.exists(STATE_FILE):
        d = json.load(open(STATE_FILE))
        if isinstance(d.get("v"), dict):
            return d
        return {"v": d, "offset": 0}  # old flat format
    return {"v": {}, "offset": 0}


def save_state(state):
    json.dump(state, open(STATE_FILE, "w"))


def check_once():
    snap = snapshot(fetch_products())
    state = load_state()
    old = state["v"]
    restocked = [
        v for k, v in snap.items()
        if v["available"] and not old.get(k, {}).get("available", False)
    ]
    if restocked:
        send_alerts(restocked)
        print("Alerted on", len(restocked), "item(s).")
    else:
        print("No restocks.")
    state["v"] = snap
    try:
        handle_commands(state, snap)
    except Exception as e:
        print("Commands error:", e)
    save_state(state)


if __name__ == "__main__":
    try:
        tg("setMyCommands", {"commands": json.dumps([
            {"command": "status", "description": "What's in stock right now"},
            {"command": "help", "description": "About this bot"},
        ])})
    except Exception as e:
        print("setMyCommands failed:", e)
    loop_for = int(os.environ.get("LOOP_SECONDS", "0"))
    interval = float(os.environ.get("INTERVAL", "15"))
    end = time.time() + loop_for
    while True:
        try:
            check_once()
        except Exception as e:
            print("Error:", e, file=sys.stderr)
        if time.time() + interval >= end:
            break
        time.sleep(interval)
