import json, os, re, sys, time
import urllib.parse
import urllib.request

SITE = "https://knifemfg.co"
STATE_FILE = "state.json"
SHAPES = ["KH1", "KL2", "KL1"]
HEADERS = {"User-Agent": "Mozilla/5.0", "Cache-Control": "no-cache"}


def wanted(title):
    clean = " " + re.sub(r"[^A-Z0-9]+", " ", (title or "").upper()) + " "
    return any(" " + s + " " in clean for s in SHAPES)


def fetch_products():
    products, page = [], 1
    while True:
        url = f"{SITE}/products.json?limit=250&page={page}&_={int(time.time()*1000)}"
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
        for v in p.get("variants", []):
            if not wanted(v.get("title")):
                continue
            snap[str(v["id"])] = {
                "title": p["title"] + " - " + v["title"],
                "available": bool(v.get("available")),
                "url": f"{SITE}/products/{p['handle']}?variant={v['id']}",
            }
    return snap


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("Telegram secrets missing")
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    urllib.request.urlopen(
        f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=30
    )


def check_once():
    new = snapshot(fetch_products())
    old = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}
    restocked = [
        v for k, v in new.items()
        if v["available"] and not old.get(k, {}).get("available", False)
    ]
    json.dump(new, open(STATE_FILE, "w"))
    if not restocked:
        print("No restocks.")
        return
    lines = [v["title"] + "\n" + v["url"] for v in restocked]
    send_telegram("DECK IN STOCK - Knife MFG\n\n" + "\n\n".join(lines))
    print("Alerted on", len(restocked), "item(s).")


if __name__ == "__main__":
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
