"""
KNIFE MFG CO DECK RADAR
Watches knifemfg.co for KH1 / KL2 / KL1 / KB1 decks and alerts you on Telegram.

Railway variables
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID    required
  CHECK_INTERVAL        seconds between checks (default 5, minimum 2)
  LOG_EVERY             1 = log every check (default 60, about every 5 min)
  STARTUP_PING          0 turns off the "radar online" message
  ALERT_ON_START        0 = stay silent about decks already in stock at start-up
  DAILY_PING_HOUR       0-23: one "still watching" message a day (off by default)
  TZ_OFFSET_HOURS       hours from UTC for the daily ping (default 8, Singapore)
  PASSWORD_TTL_HOURS    forget a /password after this long (default 24, 0 = never)
  SHOP_PASSWORD         shop password that never expires (optional)
  USD_TO_SGD            fixed exchange rate instead of the live one (optional)
  CHECKOUT_*            details to pre-fill checkout (EMAIL, FIRST_NAME, LAST_NAME,
                        ADDRESS, CITY, ZIP, COUNTRY, PHONE)
  SHOP_URL              only for testing against a fake shop
"""
import gzip
import html
import http.cookiejar
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone


# ============================================================
# CONFIG
# ============================================================

SITE = (os.getenv("SHOP_URL", "").strip() or "https://knifemfg.co").rstrip("/")
SHAPES = ["KH1", "KL2", "KL1", "KB1"]

CHECK_INTERVAL = max(2.0, float(os.getenv("CHECK_INTERVAL", "5")))
LOG_EVERY = max(1, int(os.getenv("LOG_EVERY", "60")))
STARTUP_PING = os.getenv("STARTUP_PING", "1") == "1"
ALERT_ON_START = os.getenv("ALERT_ON_START", "1") == "1"
TZ_OFFSET_HOURS = float(os.getenv("TZ_OFFSET_HOURS", "8"))
PASSWORD_TTL = float(os.getenv("PASSWORD_TTL_HOURS", "24")) * 3600

_daily = os.getenv("DAILY_PING_HOUR", "").strip()
DAILY_PING_HOUR = int(_daily) if _daily.isdigit() and 0 <= int(_daily) <= 23 else None

OFFLINE_AFTER = 6            # failed checks in a row before a warning
MAX_ALERTS_PER_DROP = 4      # photo alerts per check, the rest go in one list
SEND_RETRY_WAITS = (0, 2, 4, 8, 12)   # seconds to wait before each send attempt

# Telegram's own font can't be changed by a bot, but headings can use special lettering:
#   BUTTON_STYLE = the same names, only for the buttons (empty = same as FONT_STYLE)
#   FONT_STYLE = plain (default), sans, sanslight, sansitalic, serif, serifitalic, script,
#                gothic, double, mono, wide, smallcaps (capitals stay big), smallcapsall or spaced
FONT_STYLE = os.getenv("FONT_STYLE", "plain").strip().lower()
# Buttons can use their own lettering. Empty = same as FONT_STYLE.
BUTTON_STYLE = os.getenv("BUTTON_STYLE", "").strip().lower()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; KnifeMFG-DeckRadar/2.0)",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Encoding": "gzip",
    "Cache-Control": "no-cache",
}

LINE = "━━━━━━━━━━━━━━"


def log(message):
    print(f"[RADAR] {message}", flush=True)


def esc(text):
    return html.escape(str(text or ""))


# Heading lettering styles: (capital A, small a, digit 0, exceptions). Telegram's own font
# can't be changed, so these are special symbols that look like other fonts.
_SMALL_CAPS = dict(zip("abcdefghijklmnopqrstuvwxyz", "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ"))
_LETTERING = {
    "sans": (0x1D5D4, 0x1D5EE, 0x1D7EC, {}),            # 𝗦𝗮𝗻𝘀 bold
    "sanslight": (0x1D5A0, 0x1D5BA, 0x1D7E2, {}),      # 𝖲𝖺𝗇𝗌 regular
    "sansitalic": (0x1D608, 0x1D622, None, {}),         # 𝘚𝘢𝘯𝘴 italic
    "serif": (0x1D400, 0x1D41A, 0x1D7CE, {}),           # 𝐒𝐞𝐫𝐢𝐟 bold
    "serifitalic": (0x1D434, 0x1D44E, None, {"h": "ℎ"}),  # 𝑆𝑒𝑟𝑖𝑓 italic
    "script": (0x1D4D0, 0x1D4EA, None, {}),             # 𝓢𝓬𝓻𝓲𝓹𝓽 bold script
    "gothic": (0x1D56C, 0x1D586, None, {}),             # 𝕲𝖔𝖙𝖍𝖎𝖈 bold fraktur
    "double": (0x1D538, 0x1D552, 0x1D7D8,               # 𝔻𝕠𝕦𝕓𝕝𝕖 double-struck
               {"C": "ℂ", "H": "ℍ", "N": "ℕ", "P": "ℙ", "Q": "ℚ", "R": "ℝ", "Z": "ℤ"}),
    "mono": (0x1D670, 0x1D68A, 0x1D7F6, {}),            # 𝙼𝚘𝚗𝚘 typewriter
    "wide": (0xFF21, 0xFF41, 0xFF10, {}),               # ｗｉｄｅ full-width
}


def fancy(text, style=None):
    """Lettering from FONT_STYLE (or the style given). Only A-Z, a-z and 0-9 change."""
    style = FONT_STYLE if style is None else style
    text = str(text)
    if style == "smallcaps":                              # KNIFE MFG CO · ᴅᴇᴄᴋ ʀᴀᴅᴀʀ
        return "".join(_SMALL_CAPS.get(ch, ch) for ch in text)    # capitals stay capitals
    if style == "smallcapsall":                           # ᴋɴɪꜰᴇ ᴍꜰɢ ᴄᴏ · ᴅᴇᴄᴋ ʀᴀᴅᴀʀ
        return "".join(_SMALL_CAPS.get(ch.lower(), ch) if ch.isalpha() else ch for ch in text)
    if style == "spaced":                            # d e c k  (a hair of space between letters)
        return " \u00a0".join("\u200a".join(word) for word in text.split(" "))
    letters = _LETTERING.get(style)
    if not letters:
        return text
    upper, lower, digit, exceptions = letters
    out = []
    for ch in text:
        if ch in exceptions:
            out.append(exceptions[ch])
        elif "A" <= ch <= "Z":
            out.append(chr(upper + ord(ch) - 65))
        elif "a" <= ch <= "z":
            out.append(chr(lower + ord(ch) - 97))
        elif "0" <= ch <= "9" and digit is not None:
            out.append(chr(digit + ord(ch) - 48))
        else:
            out.append(ch)
    return "".join(out)


def btn(text):
    """A button label in the button lettering (BUTTON_STYLE, or FONT_STYLE if not set)."""
    return fancy(text, BUTTON_STYLE or FONT_STYLE)


def render(text):
    """<h>Heading</h> becomes bold text in the chosen lettering. Everything else is untouched."""
    return re.sub(r"<h>(.*?)</h>", lambda m: "<b>" + fancy(m.group(1)) + "</b>", str(text), flags=re.S)


# ============================================================
# MEMORY (state file)
# ============================================================

def pick_state_file():
    """(path, persistent). On Railway the memory only survives deploys if
    a Volume is mounted at /data."""
    testing = bool(os.getenv("SHOP_URL"))      # a fake shop gets its own memory
    default = "/data/state_test.json" if testing else "/data/state.json"
    wanted = os.getenv("STATE_FILE", default)
    folder = os.path.dirname(wanted) or "."
    try:
        os.makedirs(folder, exist_ok=True)
        probe = wanted + ".probe"
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        persistent = bool(os.getenv("STATE_FILE")) or os.path.ismount(folder)
        return wanted, persistent
    except Exception as e:
        log(f"State path {wanted} is not writable ({e}). Using a local file.")
        return ("state_test.json" if testing else "state.json"), False


STATE_FILE, MEMORY_OK = pick_state_file()
META_FILE = STATE_FILE + ".meta"

STATE = {}               # variant_id -> was in stock when last alerted/seen
CURRENT_SNAPSHOT = {}    # variant_id -> item dict
DATA_LOCK = threading.Lock()
LAST_CHECK = 0.0         # when the shop was last read successfully
META = {"last_drop": None, "shop_password": "", "shop_password_t": 0, "last_daily": ""}


def _write_json(path, data):
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(temp, path)


def load_state():
    global STATE
    try:
        if not os.path.exists(STATE_FILE):
            STATE = {}
            log("No previous memory found.")
            return
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        STATE = {k: bool(v) for k, v in raw.items() if isinstance(v, bool)}
        log(f"Loaded memory: {len(STATE)} variants")
    except Exception as e:
        log(f"Could not load memory: {e}")
        STATE = {}


def save_state():
    try:
        _write_json(STATE_FILE, STATE)
    except Exception as e:
        log(f"Could not save memory: {e}")


def load_meta():
    try:
        if os.path.exists(META_FILE):
            with open(META_FILE, "r", encoding="utf-8") as f:
                META.update(json.load(f))
    except Exception as e:
        log(f"Could not load meta: {e}")


def save_meta():
    try:
        _write_json(META_FILE, META)
    except Exception as e:
        log(f"Could not save meta: {e}")


# ============================================================
# PRICES: USD -> SGD
# ============================================================

FX_FIXED = bool(os.getenv("USD_TO_SGD"))
FX = {"rate": float(os.getenv("USD_TO_SGD", "1.29")), "t": 0.0}
FX_SOURCES = [
    ("https://api.frankfurter.app/latest?from=USD&to=SGD", lambda d: d["rates"]["SGD"]),
    ("https://open.er-api.com/v6/latest/USD", lambda d: d["rates"]["SGD"]),
]


def refresh_fx():
    """Updates the rate at most every 6 hours. Never raises."""
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
    FX["t"] = time.time() - 6 * 3600 + 600   # try again in 10 minutes


def money(price):
    """Shop price (USD) shown as approximate Singapore dollars."""
    try:
        return f"~S${float(price) * FX['rate']:.0f}"
    except Exception:
        return ""


# ============================================================
# CHECKOUT LINKS
# Nothing is ever paid by the bot. These open the shop's own
# checkout, optionally pre-filled from your Railway variables.
# ============================================================

CHECKOUT_FIELDS = {
    "CHECKOUT_EMAIL": "checkout[email]",
    "CHECKOUT_FIRST_NAME": "checkout[shipping_address][first_name]",
    "CHECKOUT_LAST_NAME": "checkout[shipping_address][last_name]",
    "CHECKOUT_ADDRESS": "checkout[shipping_address][address1]",
    "CHECKOUT_CITY": "checkout[shipping_address][city]",
    "CHECKOUT_ZIP": "checkout[shipping_address][zip]",
    "CHECKOUT_COUNTRY": "checkout[shipping_address][country]",
    "CHECKOUT_PHONE": "checkout[shipping_address][phone]",
}


def checkout_url(variant_id):
    params = {
        field: os.getenv(var, "").strip()
        for var, field in CHECKOUT_FIELDS.items()
        if os.getenv(var, "").strip()
    }
    return f"{SITE}/cart/{variant_id}:1" + ("?" + urllib.parse.urlencode(params) if params else "")


def cart_page_url(variant_id):
    """Backup link: lands on the shop's normal cart page."""
    return f"{SITE}/cart/{variant_id}:1?storefront=true"


# ============================================================
# READING THE SHOP
# ============================================================

class FetchError(Exception):
    pass


class RateLimited(FetchError):
    """The shop answered 429 (too many requests). Hammering it makes it worse,
    so this is never retried straight away."""

    def __init__(self, message, retry_after=0.0):
        super().__init__(message)
        self.retry_after = retry_after


# How politely the radar is checking right now. After a 429 it adds a little
# time between checks and stops adding a cache-buster to the address, then
# eases back to normal after 30 calm minutes.
PACE = {"extra": 0.0, "last_limit": 0.0, "buster": os.getenv("CACHE_BUSTER", "1") == "1"}


HEALTH = {"error": ""}      # why the last check failed ("" = all good)


def current_interval():
    return CHECK_INTERVAL + PACE["extra"]


def note_rate_limited():
    PACE["extra"] = min(10.0, PACE["extra"] + 2.0)
    PACE["last_limit"] = time.time()
    PACE["buster"] = False


def calm_down():
    """Called after every good check. Gives back 1 second per 30 calm minutes."""
    if PACE["extra"] <= 0 and PACE["buster"] == (os.getenv("CACHE_BUSTER", "1") == "1"):
        return
    if time.time() - PACE["last_limit"] > 1800:
        PACE["extra"] = max(0.0, PACE["extra"] - 1.0)
        PACE["last_limit"] = time.time()
        if PACE["extra"] == 0:
            PACE["buster"] = os.getenv("CACHE_BUSTER", "1") == "1"


class ShopLocked(FetchError):
    """The shop is showing its password page (before a drop)."""

    def __init__(self, message, text=""):
        super().__init__(message)
        self.text = text


SHOP_LOCKED = False
COOKIES = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(COOKIES))


def _read(response):
    raw = response.read()
    if response.headers.get("Content-Encoding", "").lower() == "gzip":
        raw = gzip.decompress(raw)
    return raw


def fetch_page(url, timeout=20):
    """(bytes, final_url). A redirect to /password shows in the final URL."""
    request = urllib.request.Request(url, headers=HEADERS)
    with OPENER.open(request, timeout=timeout) as response:
        return _read(response), response.geturl()


def post_password(password, timeout=20):
    """Submits the shop's password form. Returns (bytes, final_url)."""
    data = urllib.parse.urlencode({
        "form_type": "storefront_password",
        "utf8": "✓",
        "password": password,
    }).encode("utf-8")
    request = urllib.request.Request(
        f"{SITE}/password",
        data=data,
        headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with OPENER.open(request, timeout=timeout) as response:
        return _read(response), response.geturl()


def fetch_url(url, timeout=20):
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return _read(response)


TRANSIENT_CODES = (500, 502, 503, 504)


def retry_after_seconds(error):
    try:
        return max(0.0, float(error.headers.get("Retry-After", 0)))
    except Exception:
        return 0.0


def fetch_page_retry(url, tries=2):
    """Like fetch_page, but if the shop has a momentary hiccup (503, a timeout...)
    it tries once more straight away. A 429 (too many requests) is raised at
    once as RateLimited so the radar can slow down instead of piling on."""
    last = None
    for attempt in range(tries):
        try:
            return fetch_page(url)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise RateLimited("shop answered 429 (too many requests)", retry_after_seconds(e))
            if e.code not in TRANSIENT_CODES:
                raise
            last = e
            log(f"Shop answered {e.code}, trying again")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            log(f"Shop did not answer ({e}), trying again")
        if attempt < tries - 1:
            time.sleep(0.8)
    raise last


def page_text(raw):
    """The readable message on the password page, minus Shopify boilerplate."""
    try:
        doc = raw.decode("utf-8", "ignore")
    except Exception:
        return ""
    doc = re.sub(r"(?is)<(script|style|noscript|svg|head)\b.*?</\1>", " ", doc)
    doc = re.sub(r"(?s)<[^>]+>", "\n", doc)
    skip = ("powered by shopify", "enter store using password", "enter using password",
            "are you the store owner", "log in here", "terms of service", "privacy policy")
    keep = []
    for line in doc.split("\n"):
        line = re.sub(r"\s+", " ", html.unescape(line)).strip()
        low = line.lower()
        if len(line) < 3 or any(b in low for b in skip) or low in ("password", "enter", "submit"):
            continue
        if line not in keep:
            keep.append(line)
    return " · ".join(keep)[:300]


def fetch_products_once():
    """All products, or raises FetchError. Never returns a partial list."""
    products = []
    stamp = int(time.time() * 1000)   # makes each request unique so the shop's CDN can't serve an old copy
    for page in range(1, 21):
        url = f"{SITE}/products.json?limit=250&page={page}" + (f"&_={stamp}" if PACE["buster"] else "")
        try:
            raw, final_url = fetch_page_retry(url)
        except RateLimited:
            raise
        except Exception as e:
            raise FetchError(f"page {page}: {e}")
        if urllib.parse.urlparse(final_url).path.rstrip("/") == "/password":
            raise ShopLocked("shop is behind a password page", page_text(raw))
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:
            if b'type="password"' in raw[:300000]:
                raise ShopLocked("shop is behind a password page", page_text(raw))
            raise FetchError(f"page {page}: {e}")
        batch = data.get("products", [])
        products.extend(batch)
        if len(batch) < 250:
            break
    if not products:
        raise FetchError("empty product list")
    return products


def fetch_products():
    """Reads the shop. If it is locked and you gave me the password,
    logs in with it and reads again."""
    try:
        return fetch_products_once()
    except ShopLocked:
        PW["using"] = False
        if try_unlock():
            return fetch_products_once()
        raise


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
            shape = extract_shape(variant.get("title", ""))
            if not vid or not shape:
                continue
            title = variant.get("title", "")
            snapshot[vid] = {
                "id": vid,
                "product": name,
                "handle": handle or name,
                "variant": title,
                "size": re.sub(rf"\b{shape}\b", "", title, flags=re.I).strip(" -/·"),
                "shape": shape,
                "price": variant.get("price", ""),
                "available": bool(variant.get("available", False)),
                "image": image,
                "product_url": base_url,
                "cart_url": checkout_url(vid),
                "cart_page_url": cart_page_url(vid),
            }
    return snapshot


# ============================================================
# SHOP PASSWORD (you give it to me with /password)
# ============================================================

PW = {"bad": None, "last_try": 0.0, "using": False}


def shop_password():
    return META.get("shop_password") or os.getenv("SHOP_PASSWORD", "")


def expire_password():
    """Forgets a /password after PASSWORD_TTL so an old one never lingers."""
    if not META.get("shop_password") or PASSWORD_TTL <= 0:
        return
    if time.time() - float(META.get("shop_password_t") or 0) > PASSWORD_TTL:
        META["shop_password"] = ""
        save_meta()
        PW.update(bad=None, using=False)
        log("Saved shop password expired and was removed.")
        send_text("🔑 The saved shop password expired and was removed.\nSend /password again if you still need it.")


def try_unlock():
    """Logs in with the password you gave me. Never guesses, and never
    retries a password the shop already rejected."""
    password = shop_password()
    if not password or password == PW["bad"]:
        return False
    if time.time() - PW["last_try"] < 20:
        return False
    PW["last_try"] = time.time()
    try:
        body, final_url = post_password(password)
    except Exception as e:
        log(f"Password login failed: {e}")
        return False
    accepted = (
        urllib.parse.urlparse(final_url).path.rstrip("/") != "/password"
        and b'type="password"' not in body[:300000]
    )
    if accepted:
        log("Shop password accepted.")
        PW.update(bad=None, using=True)
        return True
    PW.update(bad=password, using=False)
    log("The shop rejected the password.")
    send_text(
        "❌ <h>The shop rejected that password.</h>\n"
        "Send /password followed by the right one."
    )
    return False


# ============================================================
# TELEGRAM
# ============================================================

def telegram_api(method, params=None, _retry=True):
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set.")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(params or {}).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8")[:2000]      # read it all: the JSON must stay complete
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
        try:
            description = json.loads(body).get("description", "")
        except Exception:
            description = ""
        harmless = (e.code == 409 and method == "getUpdates") or "not modified" in description
        if not harmless:
            log(f"Telegram {method} HTTP {e.code}: {body[:300]}")
        return {"ok": False, "error": f"HTTP {e.code}", "description": description}
    except Exception as e:
        log(f"Telegram {method} error: {e}")
        return {"ok": False, "error": str(e)}

    if not result.get("ok"):
        log(f"Telegram {method} not ok: {result}")
    return result


def send_text(text, reply_markup=None):
    params = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": render(text),
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
        "caption": render(caption),
        "parse_mode": "HTML",
    }
    if reply_markup:
        params["reply_markup"] = json.dumps(reply_markup)
    return telegram_api("sendPhoto", params)


def deliver(send, waits=None):
    """Runs send() until Telegram accepts it. True only if it was delivered."""
    for i, wait in enumerate(SEND_RETRY_WAITS if waits is None else waits):
        if wait:
            time.sleep(wait)
        try:
            if send().get("ok"):
                return True
        except Exception as e:
            log(f"Send attempt {i + 1} failed: {e}")
    return False


# ============================================================
# ALERTS
# One alert per DECK: several shapes that restock together share
# one photo and get one quick checkout button each.
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
    head = "🧪 <h>TEST ALERT</h>" if test else "🟢 <h>DECK IN STOCK</h>"
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
        lines.append("⚡ <i>Drops go fast. Tap your size.</i>")
    return "\n".join(lines)


def deck_variants(handle):
    """shape -> item, for one deck, from the latest read of the shop."""
    with DATA_LOCK:
        items = list(CURRENT_SNAPSHOT.values())
    return {it["shape"]: it for it in items if it["handle"] == handle}


def alert_buttons(group):
    """The same four size buttons every time, in the same order: KH1 KL2 KL1 KB1,
    two per row so the size fits on the button.
    ⚡ = in stock (opens checkout for that size), ✖ = sold out (tap shows a note)."""
    deck = deck_variants(group[0]["handle"])
    live = {it["shape"]: it for it in group}
    live.update({sh: it for sh, it in deck.items() if it["available"]})

    buttons = []
    for shape in SHAPES:
        item = live.get(shape) or deck.get(shape)
        size = item["size"] if item and item.get("size") else ""
        label = f"{shape} · {size}" if size else shape
        if shape in live:
            buttons.append({"text": btn(f"⚡ {label}"), "url": live[shape]["cart_url"]})
        else:
            buttons.append({"text": btn(f"✖ {label}"), "callback_data": f"so:{shape}"})

    first = next(iter(live.values()))
    return {"inline_keyboard": [
        buttons[0:2],
        buttons[2:4],
        [{"text": btn("🔗 Open page"), "url": first["product_url"]},
         {"text": btn("🛒 Cart page"), "url": first["cart_page_url"]}],
    ]}


def send_alert(group, test=False):
    """True once Telegram has accepted the alert (photo, or text if the photo fails)."""
    log(
        ("TEST ALERT: " if test else "RESTOCK DETECTED: ")
        + f"{group[0]['product']} ({', '.join(it['shape'] for it in group)})"
    )
    caption = alert_caption(group, test)
    markup = alert_buttons(group)
    image = group[0].get("image")

    def attempt():
        if image:
            result = send_photo(image, caption, markup)
            if result.get("ok"):
                return result
        return send_text(caption, markup)

    return deliver(attempt)


def send_alerts(items):
    """Sends the alerts and returns the variant ids Telegram accepted."""
    groups = group_by_deck(items)
    shown, rest = groups[:MAX_ALERTS_PER_DROP], groups[MAX_ALERTS_PER_DROP:]
    delivered = set()

    if shown:
        with ThreadPoolExecutor(max_workers=len(shown)) as pool:
            results = list(pool.map(send_alert, shown))
        for group, ok in zip(shown, results):
            if ok:
                delivered.update(it["id"] for it in group)

    if rest:
        lines = ["🟢 <h>MORE DECKS IN STOCK</h>", LINE]
        for group in rest:
            shapes = " ".join(it["shape"] for it in group)
            lines.append(f"• <b>{esc(shapes)}</b> {esc(group[0]['product'])}")
        markup = {"inline_keyboard": [[{"text": btn("🔗 Open shop"), "url": SITE}]]}
        if deliver(lambda: send_text("\n".join(lines), markup)):
            for group in rest:
                delivered.update(it["id"] for it in group)
    return delivered


def send_test_alerts(items):
    """Real alert layout with a TEST header, for every deck in stock now."""
    if not items:
        send_text("⏳ No data yet, try again in a few seconds.")
        return
    live = [it for it in items if it["available"]]
    if not live:
        send_text("🧪 <h>TEST</h> · nothing is in stock right now.\nHere is a sample so you can see the look:")
        first = items[0]
        send_alert([it for it in items if it["handle"] == first["handle"]][:2], test=True)
        return
    groups = group_by_deck(live)
    send_text(f"🧪 <h>TEST</h> · {len(groups)} deck(s) in stock right now. Sending their alerts:")
    for group in groups[:MAX_ALERTS_PER_DROP]:
        send_alert(group, test=True)
    if len(groups) > MAX_ALERTS_PER_DROP:
        send_text(f"🧪 +{len(groups) - MAX_ALERTS_PER_DROP} more deck(s) in stock, not shown.")


def remember_drop(items):
    first = group_by_deck(items)[0]
    META["last_drop"] = {
        "t": time.time(),
        "shapes": [it["shape"] for it in first],
        "product": first[0]["product"],
    }
    save_meta()


# ============================================================
# /status, /help and the daily message
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


def status_markup():
    return {"inline_keyboard": [[
        {"text": btn("🔄 Refresh"), "callback_data": "refresh"},
        {"text": btn("🛒 Open shop"), "url": SITE},
    ]]}


def help_markup():
    return {"inline_keyboard": [[
        {"text": btn("📡 Status"), "callback_data": "refresh"},
        {"text": btn("🛒 Open shop"), "url": SITE},
    ]]}


def status_text():
    with DATA_LOCK:
        snapshot = dict(CURRENT_SNAPSHOT)
        last_check = LAST_CHECK
        last_drop = META.get("last_drop")

    head = ["📡 <h>KNIFE MFG CO · deck radar</h>", LINE]

    if SHOP_LOCKED:
        hint = "🔑 password saved" if shop_password() else "🔑 got the password? send /password"
        return "\n".join(head + ["🔒 shop is behind a password", "⚡ watching for it to open", hint])
    if not snapshot:
        if HEALTH["error"]:
            return "\n".join(head + [
                f"⚠️ I can't read the shop yet: {HEALTH['error']}",
                f"🔁 still trying by myself, about every {current_interval():g}s or slower",
                "Alerts start as soon as it answers.",
            ])
        return "\n".join(head + ["⏳ warming up, try again in a few seconds"])

    lines = list(head)
    for shape in SHAPES:
        live = [it for it in snapshot.values() if it["shape"] == shape and it["available"]]
        if live:
            lines.append(f"🟢 <b>{shape}</b> · {len(live)} in stock")
            for it in live[:5]:
                price = money(it["price"])
                lines.append(f"      ▸ {esc(it['product'])}" + (f" · {price}" if price else ""))
            if len(live) > 5:
                lines.append(f"      ▸ +{len(live) - 5} more")
        else:
            lines.append(f"⚫ <b>{shape}</b> · sold out")

    lines.append(LINE)
    age = time.time() - last_check
    if age > max(30, CHECK_INTERVAL * 6):
        reason = HEALTH["error"] or "store unreachable"
        lines.append(f"⚠️ {reason} · data is {ago(age)[:-4]} old")
    else:
        lines.append(f"🕒 checked {ago(age)} · every {current_interval():g}s")
    if last_drop:
        lines.append(
            f"🎯 last drop · {' '.join(last_drop['shapes'])} {esc(last_drop['product'])}"
            f" · {ago(time.time() - last_drop['t'])}"
        )
    return "\n".join(lines)


def help_text():
    shapes = "  ".join(f"🎯 {shape}" for shape in SHAPES)
    return (
        "📡 <h>KNIFE MFG CO · deck radar</h>\n"
        f"{LINE}\n"
        "I watch the shop 24/7 and ping you the second a deck is back.\n"
        "\n"
        f"{shapes}\n"
        "\n"
        "<h>Commands</h>\n"
        "/status — stock right now\n"
        "/help — this screen\n"
        f"{LINE}\n"
        f"🟢 online · checking every {current_interval():g}s\n"
        f"💱 prices ≈ S$ (1 USD = {FX['rate']:.2f})"
    )


def maybe_daily_ping():
    """One 'still watching' message a day, so silence means something is wrong."""
    if DAILY_PING_HOUR is None:
        return
    now = datetime.now(timezone(timedelta(hours=TZ_OFFSET_HOURS)))
    today = now.strftime("%Y-%m-%d")
    if now.hour != DAILY_PING_HOUR or META.get("last_daily") == today:
        return
    META["last_daily"] = today
    save_meta()
    with DATA_LOCK:
        total = len(CURRENT_SNAPSHOT)
        live = sum(1 for it in CURRENT_SNAPSHOT.values() if it["available"])
    send_text(
        "💚 <h>Still watching</h>\n"
        f"{LINE}\n"
        f"🎯 {total} variants · {live} in stock\n"
        f"🕒 checking every {current_interval():g}s"
    )


# ============================================================
# THE STOCK CHECK
# Raises FetchError on any problem, so the snapshot and the memory
# are never replaced by half-read data. A deck is only marked "seen"
# once Telegram has accepted its alert, so an alert can't be lost.
# ============================================================

_last_available = None
_quiet_checks = 0
LOCK_TEXT = {"last": ""}


def note_locked(text=""):
    """Tells you once per lock period, then keeps watching quietly.
    If the owner edits the password page message, tells you again."""
    global SHOP_LOCKED
    text = (text or "").strip()
    if SHOP_LOCKED:
        if text and text != LOCK_TEXT["last"]:
            LOCK_TEXT["last"] = text
            send_text(f"📝 <h>The password page changed:</h>\n{esc(text)}")
        return
    SHOP_LOCKED = True
    LOCK_TEXT["last"] = text
    log("The shop is behind a password page. Watching for it to open.")
    lines = ["🔒 <h>Shop is behind a password</h>", LINE, "I can't see the products yet."]
    if text:
        lines.append(f"📝 The page says: <i>{esc(text)}</i>")
    lines.append("⚡ Still watching. You'll get an alert the moment it opens.")
    if not shop_password():
        lines.append("🔑 Got the password? Send /password followed by it.")
    send_text("\n".join(lines))


def check_stock():
    global CURRENT_SNAPSHOT, LAST_CHECK, SHOP_LOCKED, _last_available, _quiet_checks

    started = time.time()
    snapshot = build_snapshot(fetch_products())
    took = time.time() - started
    if not snapshot:
        raise FetchError("no tracked shapes in the feed")

    with DATA_LOCK:
        reopened = SHOP_LOCKED                 # was behind the password until now
        fresh_memory = not STATE
        # decks in stock now that weren't when we last looked
        wanted = [it for key, it in snapshot.items() if it["available"] and not STATE.get(key, False)]
        if reopened:
            wanted = [it for it in snapshot.values() if it["available"]]
        elif fresh_memory and not ALERT_ON_START:
            wanted = []
        pending = {it["id"] for it in wanted}

        changed = False
        for key, item in snapshot.items():
            if key in pending:
                continue                       # remembered only after the alert is delivered
            if STATE.get(key) != item["available"]:
                STATE[key] = item["available"]
                changed = True
        CURRENT_SNAPSHOT = snapshot
        LAST_CHECK = time.time()
        if changed:
            save_state()

    available = sum(1 for it in snapshot.values() if it["available"])

    if reopened:
        SHOP_LOCKED = False
        button = {"inline_keyboard": [[{"text": btn("🛒 Open shop"), "url": SITE}]]}
        if PW["using"]:
            log("Inside the shop with your password")
            send_text(
                "🔑 <h>Inside the shop</h>\n"
                f"{LINE}\n"
                "Your password worked. It may still be locked for everyone else, "
                "but I can see the products and I'm watching stock.",
                button,
            )
        else:
            log("The shop password is gone: the shop is OPEN")
            send_text(
                "🔓 <h>SHOP IS OPEN!</h>\n"
                f"{LINE}\n"
                "The password page is gone.\n"
                "⚡ Checking stock now...",
                button,
            )
    elif fresh_memory:
        log(f"Memory was empty: {len(snapshot)} variants, {available} in stock")

    if wanted:
        delivered = send_alerts(wanted)
        with DATA_LOCK:
            for vid in delivered:
                STATE[vid] = True
            save_state()
        if delivered:
            remember_drop([it for it in wanted if it["id"] in delivered])
        missed = len(pending - delivered)
        if missed:
            log(f"{missed} alert(s) could not be delivered. Retrying on the next check.")

    # quiet by default: only when something changes, or every LOG_EVERY checks
    _quiet_checks += 1
    if available != _last_available or _quiet_checks >= LOG_EVERY:
        log(f"{len(snapshot)} variants | {available} in stock | {took:.1f}s")
        _last_available = available
        _quiet_checks = 0


def run_test_once():
    """python restock_monitor.py --test
    Reads the shop once, sends test alerts for the decks in stock, then exits.
    Safe to run while the radar is online: it only sends messages."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set.")
    refresh_fx()
    items = list(build_snapshot(fetch_products()).values())
    log(f"Test: {len(items)} tracked variants, {sum(1 for i in items if i['available'])} in stock")
    send_test_alerts(items)
    log("Test alerts sent.")


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

def drop_stale_updates():
    """Skips commands sent while the radar was off, so a restart
    never replays old /status or /test messages."""
    result = telegram_api("getUpdates", {"offset": -1, "timeout": 0})
    updates = result.get("result", []) if result.get("ok") else []
    return updates[-1]["update_id"] + 1 if updates else None


def handle_password(chat_id, message_id, argument):
    """/password <word> saves it and tries it. /password clear removes it."""
    telegram_api("deleteMessage", {"chat_id": chat_id, "message_id": message_id})  # keep it out of the chat
    argument = argument.strip()

    if not argument:
        send_text(
            "🔑 Send <b>/password</b> followed by the shop password.\n"
            "I'll delete your message and use it to look inside the shop.\n"
            "<b>/password clear</b> removes it."
        )
        return
    if argument.lower() == "clear":
        META["shop_password"] = ""
        save_meta()
        PW.update(bad=None, using=False)
        send_text("🔑 Password removed.")
        return

    META["shop_password"] = argument
    META["shop_password_t"] = time.time()
    save_meta()
    PW.update(bad=None, last_try=0.0)
    masked = argument if len(argument) <= 2 else argument[0] + "•" * (len(argument) - 2) + argument[-1]
    if SHOP_LOCKED:
        send_text(f"🔑 Got it: <code>{esc(masked)}</code> ({len(argument)} characters). Trying it on the shop now...")
        try_unlock()    # says so itself if rejected; a success is announced by the next check
    else:
        send_text(
            f"🔑 Saved: <code>{esc(masked)}</code> ({len(argument)} characters). "
            "The shop isn't locked right now, I'll use it when it is."
        )


def send_test_alert():
    """Hidden command: /test"""
    with DATA_LOCK:
        items = list(CURRENT_SNAPSHOT.values())
    send_test_alerts(items)


def handle_command(command):
    if command == "/status":
        send_text(status_text(), status_markup())
    elif command in ("/help", "/start"):
        send_text(help_text(), help_markup())
    elif command == "/test":
        send_test_alert()
    else:
        send_text("❓ Unknown command.\n\nUse /help.")


def handle_callback(query):
    chat_id = str(query.get("message", {}).get("chat", {}).get("id", ""))
    if chat_id != str(TELEGRAM_CHAT_ID):
        return
    data = query.get("data", "")

    if data.startswith("so:"):
        toast = f"{data[3:]} is sold out right now."
    elif data == "refresh":
        result = telegram_api("editMessageText", {
            "chat_id": chat_id,
            "message_id": query["message"]["message_id"],
            "text": render(status_text()),
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
            "reply_markup": json.dumps(status_markup()),
        })
        unchanged = "not modified" in str(result.get("description", ""))
        toast = "Already up to date ✓" if unchanged else "Refreshed ✓"
    else:
        toast = "✓"
    telegram_api("answerCallbackQuery", {"callback_query_id": query["id"], "text": toast})


def telegram_listener(offset):
    log("Telegram listener started.")
    last_conflict_log = 0.0
    while True:
        try:
            params = {"timeout": 30, "allowed_updates": json.dumps(["message", "callback_query"])}
            if offset is not None:
                params["offset"] = offset

            result = telegram_api("getUpdates", params)
            if not result.get("ok"):
                if "409" in str(result.get("error", "")):
                    # another copy is still reading this bot (normal for a minute
                    # during a redeploy). Wait quietly until it stops.
                    if time.time() - last_conflict_log > 60:
                        log("Another copy of this bot is still running (409). "
                            "Waiting for it to stop. Commands resume by themselves.")
                        last_conflict_log = time.time()
                    time.sleep(10)
                else:
                    time.sleep(3)
                continue

            for update in result.get("result", []):
                offset = update["update_id"] + 1

                if update.get("callback_query"):
                    handle_callback(update["callback_query"])
                    continue

                message = update.get("message")
                if not message or str(message.get("chat", {}).get("id", "")) != str(TELEGRAM_CHAT_ID):
                    continue                    # only your own chat
                text = (message.get("text") or "").strip()
                if not text.startswith("/"):
                    continue
                command = text.split()[0].lower().split("@")[0]
                log(f"Command received: {command}")
                if command == "/password":
                    parts = text.split(None, 1)
                    handle_password(message["chat"]["id"], message["message_id"],
                                    parts[1] if len(parts) > 1 else "")
                else:
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
    telegram_api("deleteWebhook", {"drop_pending_updates": "false"})   # long polling needs it off
    telegram_api("setMyCommands", {"commands": json.dumps([
        {"command": "status", "description": "Check current stock"},
        {"command": "help", "description": "Show commands"},
    ])})
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
    log("📡 KNIFE MFG CO DECK RADAR")
    log("========================================")
    log(f"Watching: {', '.join(SHAPES)}")
    log(f"Check interval: {CHECK_INTERVAL:g} seconds")
    log(f"Memory file: {STATE_FILE}" + ("" if MEMORY_OK else "  (NOT saved between deploys)"))
    if not MEMORY_OK:
        log("Add a Railway Volume mounted at /data so the radar remembers what it has seen.")

    load_state()
    load_meta()
    refresh_fx()
    setup_telegram()
    offset = drop_stale_updates()

    log("Performing initial stock check...")
    try:
        check_stock()
    except ShopLocked as e:
        note_locked(e.text)
    except RateLimited:
        note_rate_limited()
        HEALTH["error"] = "the shop is limiting my checks (429)"
        log("Initial check: the shop is rate limiting (429). The radar will keep trying.")
    except Exception as e:
        HEALTH["error"] = "the shop isn't answering"
        log(f"Initial check failed: {e}")

    threading.Thread(target=telegram_listener, args=(offset,), daemon=True).start()

    if STARTUP_PING and not SHOP_LOCKED:
        with DATA_LOCK:
            total = len(CURRENT_SNAPSHOT)
            live = sum(1 for it in CURRENT_SNAPSHOT.values() if it["available"])
        lines = [
            "📡 <h>Deck radar online</h>",
            LINE,
            f"🎯 tracking {total} variants · {live} in stock",
            f"⚡ checking every {current_interval():g}s",
        ]
        if not MEMORY_OK:
            lines.append("⚠️ no saved memory: add a Railway Volume at /data")
        send_text("\n".join(lines))

    log("🟢 RADAR ONLINE")

    fails = 0
    warned = False
    limited = 0            # 429 answers in a row
    limited_warned = False
    throttle = 0.0         # extra seconds to wait while the shop is rate limiting
    while True:
        started = time.time()
        refresh_fx()
        expire_password()
        maybe_daily_ping()
        try:
            check_stock()
            HEALTH["error"] = ""
            calm_down()
            throttle = throttle / 2 if throttle > 5 else 0.0
            limited = 0
            if limited_warned:
                send_text("🟢 <h>Checks are back to normal.</h>")
                limited_warned = False
            if warned:
                send_text("🟢 <h>Radar is back online.</h>")
                warned = False
            fails = 0
        except ShopLocked as e:
            note_locked(e.text)       # normal before a drop: no error, no backoff
            fails = 0
        except RateLimited as e:
            HEALTH["error"] = "the shop is limiting my checks (429)"
            note_rate_limited()
            limited += 1
            throttle = min(300.0, max(e.retry_after, throttle * 2 if throttle else 20.0))
            log(f"Shop is rate limiting (429). Waiting {throttle:.0f}s, then checking a little slower "
                f"(every {current_interval():g}s). {limited} in a row.")
            if limited >= 3 and not limited_warned:
                send_text(
                    "⚠️ <h>The shop is limiting my checks (429).</h>\n"
                    "I'm slowing down by myself. Alerts can be a few seconds later until it calms down."
                )
                limited_warned = True
        except Exception as e:
            fails += 1
            HEALTH["error"] = "the shop isn't answering"
            log(f"Check failed ({fails} in a row): {e}")
            if fails >= OFFLINE_AFTER and not warned:
                send_text("⚠️ <h>Radar can't reach the store.</h>\nStill trying. I'll tell you when it's back.")
                warned = True

        # back off when the shop is struggling; a little jitter keeps the rhythm irregular
        base = current_interval() if fails <= 1 else min(60, current_interval() * 2 ** min(fails - 1, 4))
        time.sleep(max(0, base + throttle + random.uniform(0, 0.4) - (time.time() - started)))


if __name__ == "__main__":
    try:
        if "--test" in sys.argv:
            run_test_once()
        else:
            main()
    except KeyboardInterrupt:
        log("Radar stopped.")
    except Exception as e:
        log(f"FATAL ERROR: {e}")
        raise
