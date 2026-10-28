"""
KNIFE MFG CO DECK RADAR
Watches knifemfg.co for the KH1, KL2, KL1 and KB1 decks and tells you on Telegram the moment one
is in stock, with a button that opens a pre-filled checkout for your size.

What it does
  - Checks the shop every 20 seconds (calm) or every 4 seconds (drop mode), gently: it asks
    "has anything changed?" first, slows itself down if the shop pushes back (429), and
    remembers that across restarts.
  - Tidy drops: each deck is one card with its photo and sizes. Sold-out sizes and decks disappear,
    and one short summary is left when the drop is over.
  - Shop news: one message when a new product, a new size or a new price shows up.
  - A loud night alarm (Bark) that only rings while you are asleep. One tap stops it and opens
    the checkout.
  - A live radar page inside Telegram, a control panel with buttons, pickups and parcel tracking.

Railway variables (only the first two are required)
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID    required
  Speed
    CHECK_INTERVAL        seconds between checks in calm mode (default 20, minimum 2)
    DROP_INTERVAL         seconds between checks in drop mode (default 4, minimum 2)
    DROP_MANUAL_MINUTES   how long /drop keeps drop mode on (default 180)
    DROP_IDLE_MINUTES     automatic drop mode goes back to calm after this long quiet (default 60)
    AUTO_DROP_MAX_MINUTES stock that just stays there keeps drop mode on at most this long (default 120)
  Buying
    CHECKOUT_*            pre-fills checkout (saves typing during a drop): CHECKOUT_EMAIL, FIRST_NAME,
                          LAST_NAME, ADDRESS, CITY, ZIP, COUNTRY, PHONE
    USD_TO_SGD            a fixed exchange rate instead of the live one (optional)
    SHOP_PASSWORD         a shop password that never expires (optional, see /password)
    PASSWORD_TTL_HOURS    forget a /password after this long (default 24, 0 = never)
  Alarm
    BARK_KEY              the Bark app's key: the loud alarm that breaks through Silent (see /alarm)
    ALARM_AWAKE_MINUTES   no alarm for this long after you last used the bot or the radar (default 30)
    ALARM_RINGS           at most this many loud alerts per drop, one a minute (default 4, 1 = just one)
    BARK_SOUND            the alert's sound, by its name in the Bark app's sound list (default alarm)
    BARK_LOOP             1 = the sound repeats for 30 seconds (default), 0 = it plays once
  Parcels
    PARCELS_API_KEY       Parcels App key (parcelsapp.com)
    USPS_CLIENT_ID / USPS_CLIENT_SECRET   free USPS keys (optional)
    PARCEL_COUNTRY        where parcels are going (default SG)
    PARCEL_CHECK_MINUTES  how often parcels are checked (default 60)
    PARCEL_DAILY_HOUR     0-23: one parcel summary a day (default 9, empty = off)
  Messages and look
    STARTUP_PING          0 turns off the "radar online" message
    ALERT_ON_START        0 = stay silent about decks already in stock at start-up
    DAILY_PING_HOUR       0-23: one "still watching" message a day (off by default)
    TZ_OFFSET_HOURS       your hours from UTC (default 8, Singapore)
    FONT_STYLE            lettering for headings: plain (default), smallcaps, sans, serif, mono, ...
                          (easier: send /fonts and tap one; that choice wins over this variable)
    BUTTON_STYLE          lettering for buttons (empty = same as FONT_STYLE)
    LOG_EVERY             1 = log every check (default 60)
  Live radar page
    LIVE_RADAR            0 turns the page off
    RADAR_THEME           auto (follows Telegram, default), dark or light
    PUBLIC_URL            the page's https address (Railway fills in RAILWAY_PUBLIC_DOMAIN by itself)
    PORT                  the port the page listens on (Railway sets it, default 8080)
  For testing only
    SHOP_URL, STATE_FILE, BARK_SERVER, PARCELS_BASE, USPS_BASE

Commands: /panel has buttons for everything. /help lists the typed commands.

Sections in this file
  CONFIG · MEMORY · PRICES · CHECKOUT LINKS · READING THE SHOP · SHOP PASSWORD · TELEGRAM ·
  ALERTS · MY SIZES · BUY LINKS · DROP CARDS · SHOP NEWS · DROP INTELLIGENCE · LIVE RADAR · ALARM · CONTROL PANEL ·
  MY PICKUPS · PARCELS · STATUS AND HELP · STOCK CHECK · COMMANDS · MAIN
"""
import gzip
import hashlib
import hmac
import html
import http.cookiejar
import json
import os
import random
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone


# ============================================================
# CONFIG
# ============================================================

SITE = (os.getenv("SHOP_URL", "").strip() or "https://knifemfg.co").rstrip("/")
SHAPES = ["KH1", "KL2", "KL1", "KB1"]

VERSION = "6.7 (28 Oct 2026)"
STARTED_AT = time.time()

CHECK_INTERVAL = max(2.0, float(os.getenv("CHECK_INTERVAL", "20")))   # calm mode: easy on the shop
DROP_INTERVAL = max(2.0, float(os.getenv("DROP_INTERVAL", "4")))      # drop mode: fast
DROP_IDLE_MINUTES = max(5.0, float(os.getenv("DROP_IDLE_MINUTES", "60")))   # quiet this long = back to calm
DROP_MANUAL_MINUTES = max(5.0, float(os.getenv("DROP_MANUAL_MINUTES", "180")))   # what /drop gives you
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
#   FONT_STYLE = plain (default), sans, sanslight, sansitalic, sansbolditalic, serif, serifitalic,
#                serifbolditalic, script, scriptlight, gothic, gothiclight, double, mono, wide, circled,
#                circledblack, squared, tiny, smallcaps (capitals stay big), smallcapsall or spaced
#   /fonts in Telegram shows them all and lets you pick with a tap.
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
    "sansbolditalic": (0x1D63C, 0x1D656, None, {}),     # 𝙎𝙖𝙣𝙨 bold italic
    "serifbolditalic": (0x1D468, 0x1D482, None, {}),    # 𝑺𝒆𝒓𝒊𝒇 bold italic
    "scriptlight": (0x1D49C, 0x1D4B6, None,              # 𝒮𝒸𝓇𝒾𝓅𝓉 thin script
                    {"B": "ℬ", "E": "ℰ", "F": "ℱ", "H": "ℋ", "I": "ℐ", "L": "ℒ", "M": "ℳ", "R": "ℛ",
                     "e": "ℯ", "g": "ℊ", "o": "ℴ"}),
    "gothiclight": (0x1D504, 0x1D51E, None,              # 𝔊𝔬𝔱𝔥𝔦𝔠 thin fraktur
                    {"C": "ℭ", "H": "ℌ", "I": "ℑ", "R": "ℜ", "Z": "ℨ"}),
    "circled": (0x24B6, 0x24D0, None,                    # Ⓒⓘⓡⓒⓛⓔⓓ
                dict({"0": "⓪"}, **{str(d): chr(0x2460 + d - 1) for d in range(1, 10)})),
    "circledblack": (0x1F150, 0x1F150, None,             # 🅒🅘🅡🅒🅛🅔 (always capitals)
                     dict({"0": "⓿"}, **{str(d): chr(0x2776 + d - 1) for d in range(1, 10)})),
    "squared": (0x1F130, 0x1F130, None, {}),             # 🄱🄾🅇 (always capitals)
}
_TINY = dict(zip("abcdefghijklmnoprstuvwxyz0123456789", "ᵃᵇᶜᵈᵉᶠᵍʰⁱʲᵏˡᵐⁿᵒᵖʳˢᵗᵘᵛʷˣʸᶻ⁰¹²³⁴⁵⁶⁷⁸⁹"))
FONT_NAMES = ["plain", "smallcaps", "smallcapsall", "sans", "sanslight", "sansitalic", "sansbolditalic",
              "serif", "serifitalic", "serifbolditalic", "script", "scriptlight", "gothic", "gothiclight",
              "double", "mono", "wide", "circled", "circledblack", "squared", "tiny", "spaced"]


def font_style():
    """The headings' lettering: your /fonts choice, or FONT_STYLE."""
    chosen = META.get("font_style") if isinstance(META, dict) else None
    return chosen if chosen in FONT_NAMES else FONT_STYLE


def button_style():
    """Buttons: plain if you chose that in /fonts, else BUTTON_STYLE, else the headings' lettering."""
    chosen = META.get("button_style") if isinstance(META, dict) else None
    if chosen in FONT_NAMES:
        return chosen
    return BUTTON_STYLE or font_style()


def fancy(text, style=None):
    """Lettering from FONT_STYLE (or the style given). Only A-Z, a-z and 0-9 change."""
    style = font_style() if style is None else style
    text = str(text)
    if style == "tiny":                                    # ᵗⁱⁿʸ (there is no small q)
        return "".join(_TINY.get(ch.lower(), ch) for ch in text)
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
    return fancy(text, button_style())


def render(text):
    """<h>Heading</h> becomes bold text in the chosen lettering. Everything else is untouched."""
    return re.sub(r"<h>(.*?)</h>", lambda m: "<b>" + fancy(m.group(1)) + "</b>", str(text), flags=re.S)


# ============================================================
# MEMORY
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
META = {"last_drop": None, "shop_password": "", "shop_password_t": 0, "last_daily": "", "purchases": [], "parcels": [], "last_parcel_digest": "", "events": [], "smart": True, "smart_last": 0, "watches": [], "alarm": {"on": False, "hours": "23-9"}, "auto_drop": True,
        "board": True, "news": True, "seen": {}, "prices": {}}


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
# PRICES
# Prices are shown in Singapore dollars, from the live USD rate.
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
PACE = {"extra": 0.0, "last_limit": 0.0, "buster": True}


HEALTH = {"error": ""}      # why the last check failed ("" = all good)


DROP = {"from": 0.0, "until": 0.0, "sticky": False, "span": 0.0}     # drop mode window (unix seconds)


def fast_now():
    return DROP["from"] <= time.time() <= DROP["until"]


def base_interval():
    return DROP_INTERVAL if fast_now() else CHECK_INTERVAL


def current_interval():
    return base_interval() + PACE["extra"]


def clock(ts):
    """HH:MM in your time zone (TZ_OFFSET_HOURS, default Singapore)."""
    return datetime.fromtimestamp(ts, timezone(timedelta(hours=TZ_OFFSET_HOURS))).strftime("%H:%M")


def start_drop(minutes, begin=None, sticky=False):
    """Fast checking from `begin` (default now) for `minutes`. Only ever extends a running window.
    A sticky window (from /drop, a restock or the shop opening) stays on while things keep happening
    and ends once it has been quiet for its whole length."""
    now = time.time()
    begin = now if begin is None else begin
    end = begin + minutes * 60
    span = minutes
    if fast_now() and begin <= now:
        end = max(end, DROP["until"])
        sticky = sticky or DROP.get("sticky", False)
        span = max(float(DROP.get("span") or 0), minutes)
    DROP["from"], DROP["until"], DROP["sticky"], DROP["span"] = begin, end, sticky, span
    META["drop_from"], META["drop_until"], META["drop_sticky"] = begin, end, sticky
    save_meta()


def end_drop():
    DROP["from"] = DROP["until"] = 0.0
    DROP["sticky"] = False
    META["drop_from"] = META["drop_until"] = 0.0
    META["drop_sticky"] = False
    save_meta()


def touch_activity():
    """Something happened (you used the bot, stock changed...). A sticky drop mode stays on."""
    if not (DROP.get("sticky") and fast_now()):
        return
    now = time.time()
    if now + DROP_IDLE_MINUTES * 60 > DROP["until"]:
        DROP["span"] = DROP_IDLE_MINUTES                      # the quiet time that now counts
    DROP["until"] = max(DROP["until"], now + DROP_IDLE_MINUTES * 60)
    META["drop_until"] = DROP["until"]
    if now - DROP.get("saved_at", 0.0) > 60:
        DROP["saved_at"] = now
        save_meta()


_FAST = {"was": False}


AUTO_DROP_MAX_MINUTES = max(10.0, float(os.getenv("AUTO_DROP_MAX_MINUTES", "120")))
AUTO = {"since": 0.0}


def auto_drop_on():
    return switch_on("autodrop")


def auto_start_drop():
    """Drop mode switched on by the radar itself (a restock, the shop opening). Respects the Auto drop switch."""
    if auto_drop_on():
        start_drop(DROP_IDLE_MINUTES, sticky=True)


def auto_drop_when_in_stock(snapshot):
    """While anything is in stock, keep checking fast. If it is still there after AUTO_DROP_MAX_MINUTES
    (a size that is simply always available) it relaxes again, so it never stays fast for days."""
    if not any(it["available"] and is_mine(it) for it in snapshot.values()):
        AUTO["since"] = 0.0
        return
    if not auto_drop_on():
        return
    now = time.time()
    if not AUTO["since"]:
        AUTO["since"] = now
    if now - AUTO["since"] > AUTO_DROP_MAX_MINUTES * 60:
        return
    if fast_now():
        touch_activity()
    else:
        start_drop(DROP_IDLE_MINUTES, sticky=True)


def span_text(minutes):
    """180 -> '3 h', 60 -> '60 min'."""
    if minutes >= 120 and minutes % 60 == 0:
        return f"{int(minutes // 60)} h"
    return f"{minutes:g} min"


def check_drop_end():
    """Tells you once when drop mode ends by itself (it is not told when you switch it off)."""
    now_fast = fast_now()
    if _FAST["was"] and not now_fast and DROP["until"] > 0:
        if DROP.get("sticky"):
            why = f"No activity for {span_text(DROP.get('span') or DROP_IDLE_MINUTES)}."
        else:
            why = "The drop window is over."
        DROP["sticky"] = False
        META["drop_sticky"] = False
        save_meta()
        send_text(f"🐢 <h>Back to calm mode</h>\n{LINE}\n{why} Checking every {current_interval():g}s.\n/drop speeds it up.")
    _FAST["was"] = now_fast


def schedule_for(clock_text):
    """'20:00' -> (start, end): from 10 minutes before to 60 minutes after the next 20:00."""
    try:
        hour, minute = (int(x) for x in clock_text.split(":"))
    except Exception:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    tz = timezone(timedelta(hours=TZ_OFFSET_HOURS))
    now = datetime.now(tz)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target + timedelta(minutes=60) < now:
        target += timedelta(days=1)
    return target.timestamp() - 600, target.timestamp() + 3600


def mode_line():
    if fast_now():
        if DROP.get("sticky"):
            return f"⚡ drop mode · every {current_interval():g}s · calm at {clock(DROP['until'])} if quiet"
        return f"⚡ drop mode · every {current_interval():g}s · until {clock(DROP['until'])}"
    return f"🐢 calm mode · every {current_interval():g}s · /drop for fast"


PACE_STEP = 4.0              # seconds added to the calm pace each time the shop says 429
PACE_MAX_EXTRA = 40.0        # never more than this on top of the calm pace
BLOCK = {"until": 0.0}       # the shop said wait: no checks before this time (kept across restarts)
STREAK = {"since": 0.0, "hinted": False}     # how long the shop has been refusing in a row
STREAK_HINT_AFTER = 1800     # after this long, suggest changing the Railway region


def note_rate_limited():
    PACE["extra"] = min(PACE_MAX_EXTRA, PACE["extra"] + PACE_STEP)
    PACE["last_limit"] = time.time()
    PACE["buster"] = False
    META["pace_extra"], META["pace_t"] = PACE["extra"], PACE["last_limit"]
    save_meta()


def calm_down():
    """Called after every good check. Gives back 2 seconds per 30 calm minutes."""
    if PACE["extra"] <= 0 and PACE["buster"] == (True):
        return
    if time.time() - PACE["last_limit"] > 1800:
        PACE["extra"] = max(0.0, PACE["extra"] - 2.0)
        PACE["last_limit"] = time.time()
        if PACE["extra"] == 0:
            PACE["buster"] = True
        META["pace_extra"], META["pace_t"] = PACE["extra"], PACE["last_limit"]
        save_meta()


def load_pace_state():
    """After a restart, remember how gently the shop wants to be asked, and any pause it asked for."""
    PACE["extra"] = min(PACE_MAX_EXTRA, max(0.0, float(META.get("pace_extra") or 0)))
    PACE["last_limit"] = float(META.get("pace_t") or 0)
    if PACE["extra"] > 0:
        PACE["buster"] = False
    BLOCK["until"] = float(META.get("blocked_until") or 0)


def set_block(seconds):
    BLOCK["until"] = time.time() + seconds
    META["blocked_until"] = BLOCK["until"]
    save_meta()


def cooldown_remaining():
    return max(0.0, BLOCK["until"] - time.time())


def streak_note():
    """Called on every refusal. After a long unbroken streak, says once what else can be done."""
    now = time.time()
    if not STREAK["since"]:
        STREAK["since"] = now
    if now - STREAK["since"] >= STREAK_HINT_AFTER and not STREAK["hinted"]:
        STREAK["hinted"] = True
        send_text(
            "🌏 <h>Still blocked after 30 minutes</h>\n" + LINE + "\n"
            "This looks like the shop limiting Railway's address, not my speed, so waiting won't fix it.\n"
            "Try another region: Railway, your service, <b>Settings</b>, <b>Region</b>. "
            "Pick a different one (Singapore is the closest), then deploy once."
        )


def streak_clear():
    STREAK["since"] = 0.0
    STREAK["hinted"] = False


def retry_note():
    left = cooldown_remaining()
    if left > 0:
        return f"🔁 next try in {human(left)}"
    return f"🔁 still trying by myself, about every {current_interval():g}s or slower"


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


CONDITIONAL = {}                      # page address -> (etag, body, final address)
REQ = {"sent": 0, "unchanged": 0}     # how many shop requests were made, and how many came back "unchanged"


def fetch_page(url, timeout=20):
    """(bytes, final_url). A redirect to /password shows in the final URL.
    If the shop gave an ETag last time, asks "has it changed?" first. An unchanged page (a 304)
    costs the shop almost nothing, which keeps the radar polite."""
    key = url if "&_=" not in url else None            # a cache-busted address is always new
    headers = dict(HEADERS)
    cached = CONDITIONAL.get(key) if key else None
    if cached:
        headers["If-None-Match"] = cached[0]
    request = urllib.request.Request(url, headers=headers)
    REQ["sent"] += 1
    try:
        with OPENER.open(request, timeout=timeout) as response:
            body, etag, final = _read(response), response.headers.get("ETag"), response.geturl()
    except urllib.error.HTTPError as e:
        if e.code == 304 and cached:
            REQ["unchanged"] += 1
            return cached[1], cached[2]
        raise
    if key and etag and urllib.parse.urlparse(final).path.rstrip("/") != "/password":
        CONDITIONAL[key] = (etag, body, final)
        while len(CONDITIONAL) > 8:
            CONDITIONAL.pop(next(iter(CONDITIONAL)))
    return body, final


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
        url = f"{SITE}/products.json?limit=250&page={page}" + (f"&_={stamp}" if PACE["buster"] and fast_now() else "")
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


WATCH_LIMIT = 10


def watch_match(product_title, watches=None):
    """The first watched name found in this product's title, or None."""
    low = str(product_title or "").lower()
    for name in (META.get("watches") if watches is None else watches) or []:
        if name.lower() in low:
            return name
    return None


def build_snapshot(products):
    snapshot = {}
    watches = list(META.get("watches") or [])
    for product in products:
        name = product.get("title", "Unknown Product")
        handle = product.get("handle", "")
        base_url = f"{SITE}/products/{handle}" if handle else SITE
        images = product.get("images") or []
        image = images[0].get("src", "") if images else ""

        for variant in product.get("variants", []):
            vid = str(variant.get("id", ""))
            shape = extract_shape(variant.get("title", ""))
            title = variant.get("title", "")
            watched = watch_match(name, watches)
            if not vid or not (shape or watched):
                continue
            snapshot[vid] = {
                "id": vid,
                "product": name,
                "handle": handle or name,
                "variant": title,
                "size": re.sub(rf"\b{shape}\b", "", title, flags=re.I).strip(" -/·") if shape else "",
                "shape": shape or (title or "variant"),          # a watched size is labelled by its own name
                "watch": bool(watched and not shape),
                "watch_name": watched or "",
                "price": variant.get("price", ""),
                "available": bool(variant.get("available", False)),
                "image": image,
                "product_url": base_url,
                "cart_url": checkout_url(vid),
                "cart_page_url": cart_page_url(vid),
            }
    return snapshot


# ============================================================
# SHOP PASSWORD
# You give it to me with /password.
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
    if not test:
        usual = [x for x in (typical_gone(it["shape"]) for it in group) if x]
        if usual:
            lines.append(f"⏱ usually gone in ~{human(min(usual))}")

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


def alert_buttons(group, test=False):
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

    first = next(iter(live.values())) if live else group[0]
    rows = [buttons[0:2], buttons[2:4]] if any(it.get("watch") is not True for it in group) else []
    extra = [{"text": btn(f"⚡ {it['shape']}"), "url": it["cart_url"]}
             for it in group if it.get("watch")]
    rows += [extra[i:i + 2] for i in range(0, len(extra), 2)]
    rows += [
        [{"text": btn("🔗 Open page"), "url": first["product_url"]},
         {"text": btn("🛒 Cart page"), "url": first["cart_page_url"]}],
    ]
    return {"inline_keyboard": rows}


def send_alert(group, test=False):
    """True once Telegram has accepted the alert (photo, or text if the photo fails)."""
    log(
        ("TEST ALERT: " if test else "RESTOCK DETECTED: ")
        + f"{group[0]['product']} ({', '.join(it['shape'] for it in group)})"
    )
    caption = alert_caption(group, test)
    markup = alert_buttons(group, test)
    image = group[0].get("image")

    def attempt():
        if image:
            result = send_photo(image, caption, markup)
            if result.get("ok"):
                return result
        return send_text(caption, markup)

    return deliver(attempt)


def send_alerts(items):
    """Only your sizes (and decks you /watch) are shown. Other sizes are noted quietly, never shown,
    so they never pop up later either. Returns the variant ids that are dealt with."""
    quiet = {it["id"] for it in items if not is_mine(it)}
    mine = [it for it in items if is_mine(it)]
    if quiet:
        log(f"Not your size, kept quiet: {len(quiet)} size(s)")
    return (send_alerts_now(mine) if mine else set()) | quiet


def send_alerts_now(items):
    """Sends the alerts and returns the variant ids Telegram accepted."""
    if board_on():
        try:
            return board_arrive(items)
        except Exception as e:                       # never let the board stand between you and an alert
            log(f"Drop board failed ({e}), sending classic alerts instead")
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
    if board_on():
        live = [it for it in items if it["available"]]
        if not live:
            first = items[0]
            live = [it for it in items if it["handle"] == first["handle"]][:2]
        board_preview(live)
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


# ============================================================
# MY SIZES
# The sizes you actually buy. The drop board shows buttons for these, and only these can wake you.
# Other sizes still appear in the message as text.
# ============================================================

FOCUS_SIZES = ["KH1", "KL2"]          # what you asked for: only these, until you switch more on in /sizes


def my_sizes():
    if not META.get("sizes_v67"):         # once: start strictly on KH1 and KL2, whatever was saved before
        META["my_sizes"] = [x for x in FOCUS_SIZES if x in SHAPES] or list(SHAPES)
        META["sizes_v67"] = True
        save_meta()
    chosen = META.get("my_sizes")
    if not isinstance(chosen, list) or not chosen:
        old = (META.get("alarm") or {}).get("shapes")          # the alarm's size choice from v5.6
        chosen = old if isinstance(old, list) and old else list(SHAPES)
    return [x for x in SHAPES if x in chosen] or list(SHAPES)


def is_mine(entry):
    """A size you buy, or any size of a deck you /watch."""
    return bool(entry.get("watch")) or entry.get("shape") in my_sizes()


def sizes_text():
    mine = my_sizes()
    return "\n".join([
        "🎯 <h>My sizes</h>", LINE,
        "Now: <b>" + (" · ".join(mine) if len(mine) < len(SHAPES) else "all four") + "</b>",
        "Only these show up: drop cards, pings and the alarm. Other sizes are ignored completely.",
    ])


def sizes_markup(back="pn:home"):
    mine = my_sizes()
    return {"inline_keyboard": [
        [{"text": btn(("✅ " if shape in mine else "⬜ ") + shape), "callback_data": f"sz:{shape}"} for shape in SHAPES],
        [{"text": btn("◀ Panel"), "callback_data": back}],
    ]}


def toggle_size(shape):
    """Flips one size. Returns the pop-up text."""
    mine = my_sizes()
    if shape not in SHAPES:
        return "✓"
    if shape in mine and len(mine) == 1:
        return "Keep at least one size"
    mine = [x for x in mine if x != shape] if shape in mine else mine + [shape]
    META["my_sizes"] = [x for x in SHAPES if x in mine]
    save_meta()
    return f"{shape} {'on' if shape in mine else 'off'}"


def handle_sizes(argument):
    """/sizes shows the buttons. /sizes KH1 KL2 sets them in one go."""
    wanted = [x for x in re.split(r"[\s,]+", argument.upper()) if x]
    if wanted:
        valid = [x for x in SHAPES if x in wanted]
        if not valid:
            send_text("🤔 Try <b>/sizes KH1</b> or <b>/sizes KH1 KL2</b>, or tap the buttons below.", sizes_markup())
            return
        META["my_sizes"] = valid
        save_meta()
    send_text(sizes_text(), sizes_markup())


# ============================================================
# BUY LINKS
# Buy buttons go through the radar's own address for a split second. That tells me you are up
# (so the alarm stops), then sends you straight on to the checkout.
# ============================================================

GO = {"token": ""}


def go_token():
    if not GO["token"]:
        GO["token"] = str(META.get("go_token") or "") or secrets.token_urlsafe(9)
        if META.get("go_token") != GO["token"]:
            META["go_token"] = GO["token"]
            save_meta()
    return GO["token"]


def go_link(vid, cart_url):
    url = public_url()
    return f"{url}/go/{go_token()}/{vid}" if url else cart_url


def go_target(token, vid):
    """Where a buy button lands: that size's checkout. Only a link I made can stop the alarm."""
    vid = str(vid or "")
    target = ""
    for card in BOARD["cards"].values():
        entry = card["items"].get(vid)
        if entry:
            target = entry.get("cart_url") or ""
    if not target:
        with DATA_LOCK:
            target = (CURRENT_SNAPSHOT.get(vid) or {}).get("cart_url") or ""
    if token and GO["token"] and hmac.compare_digest(str(token), GO["token"]):
        mark_awake()
    return target if target.startswith(SITE) else SITE


# ============================================================
# DROP CARDS
# Each deck in stock gets its own card: its photo, its sizes in stock, and a buy button per size.
# A size that sells out disappears from its card (edited quietly, no ping). A deck that sells out
# completely disappears from the chat. When the whole drop is over, one short summary is left.
# ============================================================

BOARD = {"cards": {}, "finished": [], "t0": 0.0}


def board_on():
    return switch_on("board")


def board_reset():
    BOARD.update(cards={}, finished=[], t0=0.0)


def board_save():
    """Keeps the cards on disk, so a restart in the middle of a drop carries on editing and removing them."""
    META["board_state"] = {"cards": BOARD["cards"], "finished": BOARD["finished"], "t0": BOARD["t0"]}
    save_meta()


def board_load():
    saved = META.get("board_state")
    if isinstance(saved, dict) and isinstance(saved.get("cards"), dict):
        BOARD.update(cards=saved["cards"], finished=list(saved.get("finished") or []), t0=float(saved.get("t0") or 0))


def short_time(seconds):
    """0:41, 4:30, 1h05: short enough for one line per deck."""
    seconds = max(0, int(round(seconds)))
    if seconds >= 3600:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}"
    return f"{seconds // 60}:{seconds % 60:02d}"


def card_live(card):
    """Your sizes still in stock on a card, in the usual order. Other sizes never show."""
    order = {s: i for i, s in enumerate(SHAPES)}
    live = [(vid, e) for vid, e in card["items"].items() if e["gone"] is None and is_mine(e)]
    return sorted(live, key=lambda x: order.get(x[1]["shape"], 99))


def card_text(card, test=False):
    live = card_live(card)
    prices = sorted({money(e["price"]) for _, e in live if money(e["price"])})
    lines = [("🧪 <h>TEST</h> · " if test else "⚡ <h>IN STOCK</h> · ") + f"<b>{esc(card['product'])}</b>", LINE]
    if len(prices) == 1:
        lines.append(f"💵 {prices[0]}")
    for _, e in live:
        lines.append(f"🟢 <b>{esc(e['shape'])}</b>" + (f" · {esc(e['size'])}" if e.get("size") else "")
                     + (f" · {money(e['price'])}" if len(prices) > 1 else ""))
    usual = [x for x in (typical_gone(e["shape"]) for _, e in live) if x]
    if usual:
        lines.append(f"⏱ usually gone in ~{human(min(usual))}")
    lines.append("⚡ <i>A sample. Nothing just restocked.</i>" if test
                 else f"🕒 since {local_time(card['t']).strftime('%H:%M:%S')}")
    return "\n".join(lines)[:1000]


def card_markup(card, test=False):
    live = card_live(card)
    buttons = [{"text": btn(f"⚡ {e['shape']}" + (f" · {e['size']}" if e.get("size") else "")),
                "url": e["cart_url"] if test else go_link(vid, e["cart_url"])} for vid, e in live]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([{"text": btn("🛒 Cart page"), "url": card["cart_page_url"]}])
    return {"inline_keyboard": rows}


def card_post(card, test=False):
    """Sends a card: the deck's photo with its sizes, or text if it has no photo. True once Telegram took it."""
    text, markup = card_text(card, test), card_markup(card, test)
    sent = {}

    def attempt():
        if card.get("image"):
            result = send_photo(card["image"], text, markup)
            if result.get("ok"):
                sent.update(kind="photo", result=result)
                return result
        result = send_text(text, markup)
        if result.get("ok"):
            sent.update(kind="text", result=result)
        return result

    if not deliver(attempt):
        return False
    if not test:
        result = sent["result"].get("result")
        card["msg"] = {"id": result.get("message_id") if isinstance(result, dict) else None, "kind": sent["kind"]}
        card["sig"] = text + json.dumps(markup, sort_keys=True)
    return True


def card_edit(card):
    """Updates a card in place when one of its sizes sells out: no new message, no notification."""
    msg = card.get("msg") or {}
    if not msg.get("id"):
        return
    text, markup = card_text(card), card_markup(card)
    sig = text + json.dumps(markup, sort_keys=True)
    if sig == card.get("sig"):
        return
    params = {"chat_id": TELEGRAM_CHAT_ID, "message_id": msg["id"], "parse_mode": "HTML",
              "reply_markup": json.dumps(markup)}
    if msg.get("kind") == "photo":
        method, params["caption"] = "editMessageCaption", render(text)
    else:
        method, params["text"], params["disable_web_page_preview"] = "editMessageText", render(text), "true"
    result = telegram_api(method, params)
    if result.get("ok") or "not modified" in str(result.get("description", "")):
        card["sig"] = sig
    else:
        log(f"A drop card could not be edited, sending it again: {card['product']}")
        card_post(card)


def card_remove(card):
    msg = card.get("msg") or {}
    if msg.get("id"):
        telegram_api("deleteMessage", {"chat_id": TELEGRAM_CHAT_ID, "message_id": msg["id"]})


def summary_text():
    lines = [f"🏁 <h>DROP OVER</h> · {len(BOARD['finished'])} deck" + ("s" if len(BOARD["finished"]) != 1 else ""), LINE]
    fastest = []
    for deck in BOARD["finished"]:
        times = " · ".join(f"{esc(x['shape'])} {short_time(x['gone'])}" for x in deck["items"])
        lines.append(f"⚫ <b>{esc(deck['product'])}</b>")
        if times:
            lines.append(f"      {times}")
        fastest += [x["gone"] for x in deck["items"]]
    lines += [LINE, heat_label(min(fastest) if fastest else None)]
    text = "\n".join(lines)
    return text if len(text) <= 3500 else text[:3490] + "\n…"


def board_arrive(items):
    """New stock: a new card per deck (a ping each), or more sizes added to a card that is already there."""
    now = time.time()
    delivered = set()
    groups = {}
    for it in items:
        groups.setdefault(it["handle"], []).append(it)
    for handle, group in groups.items():
        card = BOARD["cards"].get(handle)
        fresh = card is None
        if fresh:
            first = group[0]
            card = {"product": first["product"], "image": next((x.get("image") for x in group if x.get("image")), ""),
                    "product_url": first["product_url"], "cart_page_url": first["cart_page_url"],
                    "items": {}, "t": now, "msg": None, "sig": ""}
        for it in group:
            card["items"][it["id"]] = {"shape": it["shape"], "size": it["size"], "price": it["price"],
                                       "cart_url": it["cart_url"], "t": now, "gone": None, "watch": bool(it.get("watch"))}
        if fresh:
            if not card_post(card):
                continue                                     # not delivered: tried again on the next check
            BOARD["cards"][handle] = card
        else:
            card_edit(card)
        delivered |= {it["id"] for it in group}
    if delivered:
        BOARD["t0"] = BOARD["t0"] or now
        log("DROP CARDS: " + ", ".join(f"{c['product']} ({len(card_live(c))})" for c in BOARD["cards"].values()))
        board_save()
    return delivered


def board_tick(snapshot):
    """After every check: sold-out sizes vanish from their card, sold-out decks vanish from the chat,
    and when the whole drop is over one summary is sent."""
    if not BOARD["cards"] and not BOARD["finished"]:
        return
    now = time.time()
    changed = False
    for handle, card in list(BOARD["cards"].items()):
        moved = False
        for vid, entry in card["items"].items():
            if entry["gone"] is None and not snapshot.get(vid, {}).get("available"):
                entry["gone"] = round(now - entry["t"], 1)
                moved = True
        if not moved:
            continue
        changed = True
        if card_live(card):
            card_edit(card)
        else:
            card_remove(card)
            BOARD["finished"].append({"product": card["product"], "items": [
                {"shape": e["shape"], "gone": e["gone"]} for e in card["items"].values()]})
            del BOARD["cards"][handle]
    if not BOARD["cards"] and BOARD["finished"]:
        deliver(lambda: send_text(summary_text(), {"inline_keyboard": [[{"text": btn("🔗 Open shop"), "url": SITE}]]}))
        board_reset()
        changed = True
    if changed:
        board_save()


def board_preview(items):
    """/test with tidy drops on: one sample card for a deck in stock (or a sample deck). Nothing is remembered."""
    now = time.time()
    first = items[0]
    group = [it for it in items if it["handle"] == first["handle"]]
    card = {"product": first["product"], "image": next((x.get("image") for x in group if x.get("image")), ""),
            "product_url": first["product_url"], "cart_page_url": first["cart_page_url"], "t": now, "items": {
                it["id"]: {"shape": it["shape"], "size": it["size"], "price": it["price"], "cart_url": it["cart_url"],
                           "t": now, "gone": None, "watch": bool(it.get("watch"))} for it in group}}
    return card_post(card, test=True)


# ============================================================
# SHOP NEWS
# A new product, a new size, a price change, all in ONE message.
# Early warning: a deck is usually listed (sold out) before it drops.
# The first read after a fresh start only learns what is there, it never alerts.
# ============================================================

NEWS_LIMIT = 6


def news_on():
    return switch_on("news")


def detect_news(products, snapshot, alerted_ids):
    """Compares the shop with what was seen last time. Sends one message if anything is new."""
    seen = META.get("seen") or {}
    prices = META.get("prices") or {}
    baseline = not seen
    new_seen, new_prices, entries = {}, dict(prices), []

    for product in products:
        handle = product.get("handle") or product.get("title", "")
        title = product.get("title", "Unknown Product")
        variants = [v for v in product.get("variants", []) if v.get("id") is not None]
        vids = [str(v["id"]) for v in variants]
        new_seen[handle] = vids
        if baseline or any(vid in alerted_ids for vid in vids):      # a drop alert already covers it
            continue
        images = product.get("images") or []
        image = images[0].get("src", "") if images else ""
        url = f"{SITE}/products/{handle}" if handle else SITE
        if handle not in seen:
            cheapest = min((float(v.get("price") or 0) for v in variants), default=0) or ""
            entries.append({"kind": "product", "title": title, "image": image, "url": url,
                            "price": cheapest, "variants": [
                                (v.get("title", ""), bool(v.get("available"))) for v in variants]})
        else:
            for v in variants:
                if str(v["id"]) not in seen[handle]:
                    entries.append({"kind": "size", "title": title, "image": image, "url": url,
                                    "variant": v.get("title", ""), "available": bool(v.get("available")),
                                    "price": v.get("price", "")})
    for vid, it in snapshot.items():
        old, now = prices.get(vid), it.get("price", "")
        new_prices[vid] = now
        try:
            changed = old is not None and abs(float(old) - float(now)) > 0.004
        except (TypeError, ValueError):
            changed = False
        if changed and not baseline and vid not in alerted_ids:
            entries.append({"kind": "price", "title": it["product"], "image": it.get("image", ""),
                            "url": it["product_url"], "variant": it["shape"], "old": old, "new": now})

    if entries and news_on():
        if not send_news(entries):
            return                                  # not delivered: nothing is marked as seen, so it tries again
    META["seen"], META["prices"] = new_seen, new_prices
    save_meta()


def news_lines(entries):
    lines = []
    for e in entries[:NEWS_LIMIT]:
        if e["kind"] == "product":
            price = money(e["price"])
            lines.append(f"🆕 <b>{esc(e['title'])}</b>" + (f" · {price}" if price else ""))
            sizes = ", ".join(f"{esc(v)}{'' if ok else ' (sold out)'}" for v, ok in e["variants"][:5])
            if sizes:
                lines.append(f"      {sizes}")
        elif e["kind"] == "size":
            lines.append(f"➕ <b>{esc(e['title'])}</b> · new size {esc(e['variant'])}"
                         + ("" if e["available"] else " (sold out)"))
        else:
            lines.append(f"💲 <b>{esc(e['title'])}</b> {esc(e['variant'])}: {money(e['old'])} → {money(e['new'])}")
    if len(entries) > NEWS_LIMIT:
        lines.append(f"➕ {len(entries) - NEWS_LIMIT} more: see the shop")
    return lines


def send_news(entries):
    """One message for everything new. True once Telegram accepted it."""
    caption = "\n".join(["🆕 <h>SHOP UPDATE</h>", LINE] + news_lines(entries) + [LINE,
                         "👀 <i>Not in stock yet? Be ready.</i>" if any(
                             e["kind"] != "price" for e in entries) else "💲 <i>Prices changed.</i>"])
    first = entries[0]
    markup = {"inline_keyboard": [[{"text": btn("🔗 Open page"), "url": first["url"]},
                                   {"text": btn("🛒 Open shop"), "url": SITE}]]}
    log("SHOP NEWS: " + "; ".join(f"{e['kind']} {e['title']}" for e in entries[:NEWS_LIMIT]))

    def attempt():
        if first.get("image"):
            result = send_photo(first["image"], caption[:1000], markup)
            if result.get("ok"):
                return result
        return send_text(caption, markup)

    return deliver(attempt)


def remember_drop(items):
    first = group_by_deck(items)[0]
    META["last_drop"] = {
        "t": time.time(),
        "shapes": [it["shape"] for it in first],
        "product": first[0]["product"],
    }
    save_meta()


# ============================================================
# DROP INTELLIGENCE
# The radar remembers every drop it alerts you about: when it started, and how
# fast each size sold out. From that it writes a report card after each drop,
# learns when your shop usually drops, and can switch itself into drop mode
# just before (SMART DROP).
# ============================================================

EVENT_LIMIT = 200
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
SPEED_STEPS = ((30, 8), (60, 7), (120, 6), (300, 5), (900, 4), (1800, 3), (3600, 2))
REPORT_AFTER = 3600           # report a drop after an hour even if something is still live


def human(seconds):
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def speed_bar(seconds):
    """8 blocks: full = gone in seconds, empty = still there."""
    score = 0 if seconds is None else next((n for limit, n in SPEED_STEPS if seconds <= limit), 1)
    return "▇" * score + "▁" * (8 - score)


def heat_label(fastest):
    if fastest is None:
        return "😴 CHILL · nothing sold out yet"
    if fastest < 60:
        return f"🔥🔥 INSANE · first one gone in {human(fastest)}"
    if fastest < 300:
        return f"🔥 HOT · first one gone in {human(fastest)}"
    if fastest < 1800:
        return f"🌤 WARM · first one gone in {human(fastest)}"
    return f"😴 CHILL · first one gone in {human(fastest)}"


def median(values):
    ordered = sorted(values)
    n = len(ordered)
    return ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2


def typical_gone(shape=None):
    """How long sizes usually last, in seconds (median of past drops). None until there are 2 samples."""
    times = [e["gone"] for ev in META.get("events") or [] for e in ev["items"].values()
             if e["gone"] is not None and (shape is None or e["shape"] == shape)]
    return median(times) if len(times) >= 2 else None


def live_since(vid):
    """Seconds this size has been live, if it came from a drop I alerted you about and is still there."""
    for event in reversed(META.get("events") or []):
        entry = event["items"].get(vid)
        if entry is not None:
            return (time.time() - event["t"]) if entry["gone"] is None else None
    return None


DRILL_DEFAULT_LIMIT = 45.0


def drill_stats():
    runs = META.get("drills") or []
    wins = [r["ms"] for r in runs if r.get("won")]
    return {
        "count": len(runs),
        "wins": len(wins),
        "best": min(wins) if wins else None,
        "avg": round(sum(wins) / len(wins)) if wins else None,
        "shape": SHAPES[0],
        "limit": typical_gone(SHAPES[0]) or typical_gone() or DRILL_DEFAULT_LIMIT,
    }


def record_drill(ms, won):
    META.setdefault("drills", []).append({"t": time.time(), "ms": int(ms), "won": bool(won)})
    del META["drills"][:-100]
    save_meta()


def local_time(ts):
    return datetime.fromtimestamp(ts, timezone(timedelta(hours=TZ_OFFSET_HOURS)))


def record_restock(items):
    """Remembers a drop you were alerted about, one entry per deck."""
    for group in group_by_deck(items):
        META.setdefault("events", []).append({
            "t": time.time(),
            "product": group[0]["product"],
            "handle": group[0]["handle"],
            "items": {it["id"]: {"shape": it["shape"], "size": it["size"], "gone": None} for it in group},
            "reported": False,
        })
    del META["events"][:-EVENT_LIMIT]
    save_meta()


def note_sold_out(vids):
    """A size that was part of a drop just sold out: remember how fast."""
    now = time.time()
    changed = False
    for vid in vids:
        for event in reversed(META.get("events") or []):
            entry = event["items"].get(vid)
            if entry is not None and entry["gone"] is None and now - event["t"] < 86400:
                entry["gone"] = round(now - event["t"], 1)
                changed = True
                break
    if changed:
        save_meta()


def report_text(event):
    rows = []
    for entry in event["items"].values():
        name = f"{entry['shape']} · {entry['size']}" if entry["size"] else entry["shape"]
        gone = entry["gone"]
        when = f"gone in {human(gone)}" if gone is not None else "still live"
        rows.append(f"<code>{esc(name):<14}</code> {speed_bar(gone)}\n      {when}")
    times = [e["gone"] for e in event["items"].values() if e["gone"] is not None]
    lines = [
        "🏁 <h>DROP REPORT</h>",
        LINE,
        f"🛹 <b>{esc(event['product'])}</b>",
        f"🕒 started {local_time(event['t']).strftime('%a %d %b, %H:%M')}",
        "",
    ] + rows + [LINE, heat_label(min(times) if times else None)]
    return "\n".join(lines)


def maybe_drop_reports():
    """One report card per drop: when everything sold out, or after an hour."""
    now = time.time()
    for event in META.get("events") or []:
        if event.get("reported"):
            continue
        everything_gone = all(e["gone"] is not None for e in event["items"].values())
        if everything_gone or now - event["t"] > REPORT_AFTER:
            event["reported"] = True
            save_meta()
            if not board_on():                  # with tidy drops on, the summary at the end is the report
                send_text(report_text(event))


def history_text():
    events = META.get("events") or []
    head = ["📜 <h>Drop history</h>", LINE]
    if not events:
        return "\n".join(head + ["No drops yet. I note every restock I alert you about."])
    lines = list(head)
    for event in reversed(events[-8:]):
        sizes = " ".join(
            f"{e['shape']} {human(e['gone']) if e['gone'] is not None else 'live'}" for e in event["items"].values()
        )
        lines.append(f"• {local_time(event['t']).strftime('%a %d %b %H:%M')} · {esc(event['product'])}")
        lines.append(f"      {sizes}")
    lines.append(LINE)
    lines.append(f"📊 {len(events)} drop{'s' if len(events) != 1 else ''} remembered")
    return "\n".join(lines)


def slot_counts():
    counts = {}
    for event in META.get("events") or []:
        moment = local_time(event["t"])
        key = (moment.weekday(), moment.hour)
        counts[key] = counts.get(key, 0) + 1
    return counts


def smart_slots():
    """The weekday and hour pairs where drops keep happening: at least 3 times and a fair share."""
    total = len(META.get("events") or [])
    return [(wd, hr, n, total) for (wd, hr), n in sorted(slot_counts().items(), key=lambda kv: -kv[1])
            if n >= 3 and n / total >= 0.25]


def slot_start(weekday, hour):
    """The start of the next (or current) occurrence of that weekday and hour."""
    now = datetime.now(timezone(timedelta(hours=TZ_OFFSET_HOURS)))
    start = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    start += timedelta(days=(weekday - start.weekday()) % 7)
    if start.timestamp() + 4200 < now.timestamp():
        start += timedelta(days=7)
    return start


def forecast_text():
    events = META.get("events") or []
    head = ["🔮 <h>Forecast</h>", LINE]
    if len(events) < 3:
        return "\n".join(head + [
            f"Still learning. I've seen {len(events)} drop{'s' if len(events) != 1 else ''}.",
            "After 3 or more I can spot your shop's rhythm.",
        ])
    counts = sorted(slot_counts().items(), key=lambda kv: -kv[1])[:3]
    lines = list(head) + [f"📊 learned from {len(events)} drops"]
    for rank, ((wd, hr), n) in enumerate(counts, 1):
        lines.append(f"{rank}. {DAYS[wd]} {hr:02d}:00 · {n} drop{'s' if n != 1 else ''} ({round(100 * n / len(events))}%)")
    slots = smart_slots()
    if slots:
        wd, hr, _, _ = slots[0]
        start = slot_start(wd, hr)
        wait = max(0, start.timestamp() - time.time())
        when = f"in {human(wait)}" if wait else "happening now"
        lines.append(f"⏭ next likely: {DAYS[wd]} {hr:02d}:00 ({when})")
    lines.append(LINE)
    lines.append("🤖 smart drop: " + ("on · I wake up 10 min before" if META.get("smart", True) else "off") + " · /smart")
    return "\n".join(lines)


def maybe_smart_drop():
    """Switches to drop mode by itself just before a time when drops usually happen."""
    if not META.get("smart", True) or fast_now():
        return
    now = time.time()
    for wd, hr, n, total in smart_slots():
        start = slot_start(wd, hr).timestamp()
        begin, end = start - 600, start + 4200
        if begin <= now <= end and abs(float(META.get("smart_last") or 0) - begin) > 60:
            META["smart_last"] = begin
            start_drop(DROP_IDLE_MINUTES, sticky=True)
            send_text(
                f"🔮 <h>SMART DROP</h>\n{LINE}\n"
                f"Drops often start {DAYS[wd]} around {hr:02d}:00 ({n} of {total} so far).\n"
                f"⚡ Fast mode is on. I'll relax by myself if it stays quiet.\n/smart off turns this off."
            )
            return


def handle_smart(argument):
    arg = argument.strip().lower()
    if arg in ("on", "off"):
        META["smart"] = arg == "on"
        save_meta()
    state = "on" if META.get("smart", True) else "off"
    send_text(
        f"🤖 <h>Smart drop</h>\n{LINE}\nIt is <b>{state}</b>.\n"
        "When drops keep happening at the same time each week, I switch to fast checking 10 minutes before.\n"
        "<b>/smart on</b> or <b>/smart off</b>. See /forecast."
    )


# ============================================================
# LIVE RADAR
# A page that opens inside Telegram.
# Four sizes, big BUY buttons, drop mode, your pickups and parcels, a sweeping
# radar. It reads the radar's own memory every 2 seconds, so it never adds
# a single request to the shop. Only YOUR Telegram can open it: the page checks
# the signature Telegram puts on every Mini App.
# ============================================================

LIVE_RADAR = os.getenv("LIVE_RADAR", "1") == "1"
RADAR_THEME = os.getenv("RADAR_THEME", "auto").strip().lower()


def public_url():
    """The https address of the radar page. Railway adds RAILWAY_PUBLIC_DOMAIN once a domain exists."""
    url = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
    if not url:
        domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
        url = f"https://{domain}" if domain else ""
    return url if url.startswith("https://") else ""


def radar_markup():
    url = public_url()
    if not (LIVE_RADAR and url):
        return None
    return {"inline_keyboard": [[{"text": btn("📡 Open live radar"), "web_app": {"url": url}}]]}


def verify_init_data(init_data, max_age=86400):
    """Checks Telegram's signature on a Mini App. Returns the user, or None if it is not genuine."""
    if not init_data or not TELEGRAM_BOT_TOKEN:
        return None
    try:
        fields = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        received = fields.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
        secret = hmac.new(b"WebAppData", TELEGRAM_BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received):
            return None
        if time.time() - int(fields.get("auth_date", "0")) > max_age:
            return None
        return json.loads(fields.get("user", "{}"))
    except Exception:
        return None


def dashboard_state():
    """Everything the live page shows, as plain data."""
    with DATA_LOCK:
        snapshot = dict(CURRENT_SNAPSHOT)
        last_check = LAST_CHECK
    now = time.time()
    tiles = []
    for shape in SHAPES:
        live = [it for it in snapshot.values() if it["shape"] == shape and it["available"]]
        tiles.append({
            "shape": shape,
            "count": len(live),
            "typical": typical_gone(shape),
            "live": [{"product": it["product"], "size": it["size"], "sgd": money(it["price"]),
                      "buy": it["cart_url"], "page": it["product_url"], "image": it["image"],
                      "since": live_since(it["id"])}
                     for it in live[:6]],
        })
    last_event = None
    events = META.get("events") or []
    if events:
        event = events[-1]
        gone = [e["gone"] for e in event["items"].values() if e["gone"] is not None]
        last_event = {
            "product": event["product"],
            "t": event["t"],
            "items": [{"shape": e["shape"], "size": e["size"], "gone": e["gone"]} for e in event["items"].values()],
            "heat": heat_label(min(gone) if gone else None),
        }
    slots = smart_slots()
    next_drop = slot_start(slots[0][0], slots[0][1]).timestamp() if slots else None
    return {
        "version": VERSION.split(" ")[0],
        "now": now,
        "checked_ago": (now - last_check) if last_check else None,
        "locked": bool(SHOP_LOCKED),
        "problem": HEALTH["error"],
        "mode": {"fast": fast_now(), "sticky": bool(DROP.get("sticky")), "until": DROP["until"],
                 "interval": current_interval(), "idle": DROP_IDLE_MINUTES,
                 "span": float(DROP.get("span") or DROP_IDLE_MINUTES)},
        "tiles": tiles,
        "last_event": last_event,
        "pickups": len(META.get("purchases") or []),
        "parcels": [{"label": p["label"], "status": status_label(p.get("status")), "event": p.get("event") or ""}
                    for p in META.get("parcels") or [] if not p.get("delivered")],
        "next_drop": next_drop,
        "drill": drill_stats(),
    }


DASHBOARD_HTML = r"""<!doctype html>
<html lang="en" data-theme="auto">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<title>KNIFE MFG CO</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{
  --bg:#0b0c0f;--surface:#14161b;--surface2:#1b1e25;--line:rgba(255,255,255,.09);
  --text:#f3f5f9;--dim:#8d95a6;--accent:#3ee6a8;--on-accent:#06130d;
  --accent-soft:rgba(62,230,168,.10);--accent-line:rgba(62,230,168,.42);
  --amber:#ffb454;--danger:#ff6b7a;--overlay:rgba(8,9,12,.82);--shadow:0 1px 0 rgba(255,255,255,.04) inset;
}
:root[data-resolved="light"]{
  --bg:#f4f5f8;--surface:#ffffff;--surface2:#eef0f4;--line:rgba(14,18,30,.10);
  --text:#12141a;--dim:#677086;--accent:#0fae78;--on-accent:#ffffff;
  --accent-soft:rgba(15,174,120,.08);--accent-line:rgba(15,174,120,.40);
  --amber:#c9741c;--danger:#d63c4f;--overlay:rgba(244,245,248,.86);--shadow:0 1px 2px rgba(14,18,30,.05);
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;background:var(--bg);color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Inter","Segoe UI",system-ui,sans-serif;
  -webkit-text-size-adjust:100%;font-variant-numeric:tabular-nums}
body{padding:max(16px,env(safe-area-inset-top)) 16px max(28px,env(safe-area-inset-bottom));min-height:100vh;overflow-x:hidden}
header{display:flex;justify-content:space-between;align-items:center;gap:10px;margin:2px 2px 16px}
.brand b{display:block;font-size:15px;font-weight:650;letter-spacing:.14em}
.brand span{display:block;font-size:12px;color:var(--dim);margin-top:3px}
.pill{display:inline-flex;align-items:center;gap:8px;padding:8px 13px;border-radius:999px;background:var(--surface2);
  border:1px solid var(--line);font-size:12px;font-weight:600;white-space:nowrap}
.pill i{width:8px;height:8px;border-radius:50%;background:var(--dim);display:block}
.pill.live i{background:var(--accent)}
.pill.fast i{background:var(--amber);animation:beat 1.4s ease-in-out infinite}
.pill.warn i,.pill.lock i{background:var(--danger)}
@keyframes beat{50%{opacity:.35}}
.card{background:var(--surface);border:1px solid var(--line);border-radius:20px;padding:16px;margin-bottom:12px;box-shadow:var(--shadow)}
.card h2{margin:0 0 12px;font-size:11px;font-weight:600;color:var(--dim);letter-spacing:.09em;text-transform:uppercase}

.hero{text-align:center;padding:22px 16px 18px}
#gauge{position:relative;width:224px;height:224px;margin:0 auto}
#gauge svg{display:block;width:224px;height:224px;overflow:visible}
.seg{fill:none;stroke:var(--line);stroke-width:14;stroke-linecap:round;transition:stroke .4s}
.seg.live{stroke:var(--accent);animation:glow 2.6s ease-in-out infinite}
.fast .seg.live{animation-duration:1.3s}
@keyframes glow{50%{opacity:.55}}
.segLbl{font-size:10px;font-weight:650;letter-spacing:.08em;fill:var(--dim);text-anchor:middle;dominant-baseline:central}
.segLbl.live{fill:var(--accent)}
.ringTrack{fill:none;stroke:var(--line);stroke-width:2}
#progress{fill:none;stroke:var(--accent);stroke-width:2;stroke-linecap:round;transition:stroke-dashoffset .3s linear;opacity:.8}
#core{position:absolute;left:0;right:0;top:50%;transform:translateY(-50%);text-align:center}
#coreNum{font-size:56px;font-weight:600;line-height:1;letter-spacing:-.02em;color:var(--dim)}
#coreNum.on{color:var(--accent)}
#coreLbl{font-size:12px;color:var(--dim);margin-top:8px}
#summary{margin-top:16px;font-size:14px;color:var(--text)}
#summary small{display:block;font-size:12px;color:var(--dim);margin-top:4px}

.shape{background:var(--surface);border:1px solid var(--line);border-radius:18px;margin-bottom:10px;padding:14px 16px}
.shape.live{border-color:var(--accent-line);background:linear-gradient(var(--accent-soft),var(--accent-soft)),var(--surface)}
.shape.sold{opacity:.55}
.shape .top{display:flex;justify-content:space-between;align-items:center}
.shape .nm{font-size:16px;font-weight:650;letter-spacing:.02em}
.tag{font-size:11px;font-weight:600;color:var(--dim)}
.tag.live{color:var(--accent)}
.deck{display:flex;gap:12px;align-items:center;margin-top:12px;padding-top:12px;border-top:1px solid var(--line)}
.deck img{width:48px;height:48px;object-fit:contain;background:#fff;border-radius:10px;flex:none}
.deck .meta{flex:1;min-width:0}
.deck .name{font-size:14px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.deck .sub{font-size:12px;color:var(--dim);margin-top:3px}
.buy{flex:none;font-family:inherit;font-weight:650;font-size:14px;color:var(--on-accent);background:var(--accent);border:0;
  border-radius:999px;padding:0 20px;min-height:44px;cursor:pointer}
.buy:active{transform:scale(.97)}
.clock{margin-top:10px}
.clock .txt{font-size:12px;color:var(--dim);margin-bottom:6px}
.clock .track{height:4px;border-radius:4px;background:var(--surface2);overflow:hidden}
.clock .fill{height:100%;border-radius:4px;background:var(--accent);transition:width .5s linear}
.clock.late .fill{background:var(--danger)}
.clock.late .txt{color:var(--danger)}

.seg2{display:flex;background:var(--surface2);border-radius:14px;padding:4px;gap:4px}
.btn{flex:1;font-family:inherit;font-size:14px;font-weight:600;color:var(--dim);background:transparent;border:0;border-radius:11px;min-height:44px;cursor:pointer}
.btn.on{background:var(--surface);color:var(--text);box-shadow:0 1px 3px rgba(0,0,0,.25)}
.btn.on.drop{color:var(--amber)}
.btn.on.calmBtn{color:var(--accent)}
#modeNote{font-size:12px;color:var(--dim);margin-top:10px;line-height:1.5}

.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:12px}
.stat{background:var(--surface);border:1px solid var(--line);border-radius:18px;padding:14px 8px;text-align:center}
.stat b{display:block;font-size:22px;font-weight:600}
.stat span{display:block;font-size:11px;color:var(--dim);margin-top:5px}

.bar{display:flex;align-items:center;gap:10px;font-size:12px;margin-top:10px}
.bar .k{width:104px;flex:none;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bar .track{flex:1;height:4px;border-radius:4px;background:var(--surface2);overflow:hidden}
.bar .fill{height:100%;border-radius:4px;background:var(--accent)}
.bar .v{width:64px;flex:none;text-align:right;color:var(--dim)}
.heat{margin-top:14px;font-size:13px;color:var(--amber)}
.parcel{font-size:13px;margin-top:10px;line-height:1.5}
.parcel i{font-style:normal;color:var(--dim)}
#drillStats{font-size:13px;color:var(--dim);line-height:1.55;white-space:pre-line}
.ghost{width:100%;margin-top:12px;font-family:inherit;font-size:14px;font-weight:600;color:var(--text);background:var(--surface2);border:0;border-radius:14px;min-height:46px;cursor:pointer}
footer{text-align:center;font-size:11px;color:var(--dim);margin-top:14px}
#locked{display:none;text-align:center;padding:90px 24px;color:var(--dim);line-height:1.7;font-size:14px}
#locked b{color:var(--text);letter-spacing:.14em}

#flash{position:fixed;left:0;top:0;right:0;bottom:0;background:radial-gradient(circle at 50% 40%,var(--accent-line),transparent 70%);opacity:0;pointer-events:none;z-index:8}
#flash.go{animation:flash 1s ease-out}
@keyframes flash{0%{opacity:1}100%{opacity:0}}
.ovl{position:fixed;left:0;top:0;right:0;bottom:0;z-index:20;background:var(--overlay);-webkit-backdrop-filter:blur(22px);backdrop-filter:blur(22px);
  display:none;flex-direction:column;justify-content:center;gap:14px;padding:max(20px,env(safe-area-inset-top)) 20px max(20px,env(safe-area-inset-bottom));overflow-y:auto}
.ovl.show{display:flex}
.ovl .title{text-align:center;font-size:12px;font-weight:650;letter-spacing:.16em;color:var(--dim);text-transform:uppercase}
.ovl .dtext{text-align:center;white-space:pre-line;font-size:16px;line-height:1.6}
.ovl .big{text-align:center;font-size:64px;font-weight:600;letter-spacing:-.02em;color:var(--accent)}
.ovl .big.bad{color:var(--danger)}
.ovl .clock .txt{text-align:center;font-size:14px}
.ovl button{width:100%}
.bigbtn{font-family:inherit;border:0;background:var(--accent);color:var(--on-accent);font-weight:650;font-size:22px;border-radius:24px;
  padding:26px 16px;min-height:112px;white-space:pre-line;line-height:1.35}
.bigbtn:active{transform:scale(.98)}
.bigbtn small{display:block;font-size:13px;font-weight:500;margin-top:8px;opacity:.8}
.bigbtn.dark{background:var(--text);color:var(--bg)}
.mock{border:1px solid var(--line);border-radius:18px;padding:14px 16px;background:var(--surface);font-size:14px;line-height:1.9;margin-bottom:12px;color:var(--dim)}
.mock .mockTitle{color:var(--text);font-weight:650;margin-bottom:2px}
.xbtn{font-family:inherit;background:transparent;border:1px solid var(--line);color:var(--dim);border-radius:16px;min-height:48px;font-size:14px;font-weight:600}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style>
</head>
<body>
<div id="flash"></div>
<div id="big" class="ovl"></div>
<div id="drill" class="ovl"></div>
<div id="locked"><b>KNIFE MFG CO</b><br><br>Open this page from the bot in Telegram:<br>send /radar and tap Open live radar.</div>

<div id="app" style="display:none">
  <header>
    <div class="brand"><b>KNIFE MFG CO</b><span>Live stock</span></div>
    <div id="chip" class="pill"><i></i><span>Calm</span></div>
  </header>

  <section class="card hero" id="hero">
    <div id="gauge">
      <svg id="gaugeSvg" viewBox="0 0 224 224" aria-hidden="true">
        <circle class="ringTrack" cx="112" cy="112" r="108"></circle>
        <circle id="progress" cx="112" cy="112" r="108" transform="rotate(-90 112 112)"></circle>
      </svg>
      <div id="core"><div id="coreNum">0</div><div id="coreLbl">in stock</div></div>
    </div>
    <div id="summary"></div>
  </section>

  <div id="shapes"></div>

  <div class="card">
    <h2>Speed</h2>
    <div class="seg2">
      <button id="dropBtn" class="btn drop">Drop mode</button>
      <button id="calmBtn" class="btn calmBtn">Calm</button>
    </div>
    <div id="modeNote"></div>
  </div>

  <div class="stats">
    <div class="stat"><b id="sPick">0</b><span>Pickups</span></div>
    <div class="stat"><b id="sParcel">0</b><span>Parcels</span></div>
    <div class="stat"><b id="sNext">--</b><span>Next drop</span></div>
  </div>

  <div class="card" id="reportCard" style="display:none">
    <h2>Last drop</h2>
    <div id="reportName" style="font-size:14px;font-weight:600"></div>
    <div id="reportBars"></div>
    <div id="heat" class="heat"></div>
  </div>

  <div class="card" id="parcelCard" style="display:none">
    <h2>Parcels on their way</h2>
    <div id="parcelList"></div>
  </div>

  <div class="card" id="drillCard">
    <h2>Practice drop</h2>
    <div id="drillStats"></div>
    <button id="drillBtn" class="ghost">Start a drill</button>
  </div>

  <footer id="foot">connecting…</footer>
</div>

<script>
(function () {
  "use strict";
  var tg = window.Telegram && window.Telegram.WebApp;
  var initData = tg && tg.initData ? tg.initData : "";
  var $ = function (id) { return document.getElementById(id); };
  var state = null, fetchedAt = 0, prevLive = null, okAt = 0;
  var clocks = [], bigTimer = null, drillTimers = [];
  var NS = "http://www.w3.org/2000/svg", RING = 2 * Math.PI * 108, built = 0;

  // ---- theme: follows Telegram's light or dark unless the bot says otherwise ----
  function applyTheme() {
    var want = document.documentElement.getAttribute("data-theme");
    var scheme = want === "light" || want === "dark" ? want
      : (tg && tg.colorScheme) ? tg.colorScheme
      : (window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
    document.documentElement.setAttribute("data-resolved", scheme);
    if (tg) {
      var bg = scheme === "light" ? "#f4f5f8" : "#0b0c0f";
      try { tg.setHeaderColor(bg); tg.setBackgroundColor(bg); } catch (e) {}
    }
  }
  applyTheme();
  if (tg) {
    try { tg.ready(); tg.expand(); if (tg.onEvent) tg.onEvent("themeChanged", applyTheme); } catch (e) {}
  }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }
  function dur(sec) {
    sec = Math.max(0, Math.round(sec));
    if (sec < 60) return sec + "s";
    if (sec < 3600) return Math.floor(sec / 60) + "m " + ("0" + (sec % 60)).slice(-2) + "s";
    if (sec < 86400) return Math.floor(sec / 3600) + "h " + ("0" + Math.floor((sec % 3600) / 60)).slice(-2) + "m";
    return Math.floor(sec / 86400) + "d " + Math.floor((sec % 86400) / 3600) + "h";
  }
  function mins(m) { m = Math.round(m); return (m >= 120 && m % 60 === 0) ? (m / 60) + " h" : m + " min"; }
  function openUrl(url) {
    if (!/^https?:\/\//i.test(url)) return;
    if (tg && tg.openLink) tg.openLink(url); else window.open(url, "_blank", "noopener");
  }
  function haptic(kind) {
    try { if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred(kind); else if (navigator.vibrate) navigator.vibrate([80, 60, 80]); } catch (e) {}
  }
  function celebrate() {
    var f = $("flash"); f.classList.remove("go"); void f.offsetWidth; f.classList.add("go");
    haptic("success");
  }

  // ---- the ring: four segments (one per size) and a hairline that fills until the next check ----
  function svgEl(tag, attrs) {
    var e = document.createElementNS(NS, tag);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  }
  function polar(r, deg) { var a = deg * Math.PI / 180; return [112 + r * Math.sin(a), 112 - r * Math.cos(a)]; }
  function arc(r, a0, a1) {
    var p0 = polar(r, a0), p1 = polar(r, a1);
    return "M" + p0[0].toFixed(2) + " " + p0[1].toFixed(2) + " A" + r + " " + r + " 0 0 1 " + p1[0].toFixed(2) + " " + p1[1].toFixed(2);
  }
  function buildGauge(names) {
    var svg = $("gaugeSvg"), old = svg.querySelectorAll(".seg,.segLbl");
    for (var i = 0; i < old.length; i++) svg.removeChild(old[i]);
    var span = 360 / names.length;
    names.forEach(function (n, idx) {
      svg.appendChild(svgEl("path", { "class": "seg", d: arc(88, idx * span + 7, (idx + 1) * span - 7), "data-shape": n }));
      var c = polar(64, idx * span + span / 2);
      var t = svgEl("text", { "class": "segLbl", x: c[0].toFixed(1), y: c[1].toFixed(1), "data-shape": n });
      t.textContent = n; svg.appendChild(t);
    });
    var prog = $("progress");
    prog.setAttribute("stroke-dasharray", RING.toFixed(2));
    prog.setAttribute("stroke-dashoffset", RING.toFixed(2));
    built = names.length;
  }
  function renderGauge(s) {
    if (built !== s.tiles.length) buildGauge(s.tiles.map(function (t) { return t.shape; }));
    var live = [], total = 0;
    s.tiles.forEach(function (t) {
      total += t.count;
      if (t.count) live.push(t.shape);
      var on = t.count > 0;
      var seg = document.querySelector('.seg[data-shape="' + t.shape + '"]');
      var lbl = document.querySelector('.segLbl[data-shape="' + t.shape + '"]');
      if (seg) seg.setAttribute("class", "seg" + (on ? " live" : ""));
      if (lbl) lbl.setAttribute("class", "segLbl" + (on ? " live" : ""));
    });
    $("hero").className = "card hero" + (s.mode.fast ? " fast" : "");
    var num = $("coreNum");
    num.textContent = String(total);
    num.className = total ? "on" : "";
    $("coreLbl").textContent = s.locked ? "shop locked" : (total ? "in stock" : "sold out");
    var sum = $("summary");
    while (sum.firstChild) sum.removeChild(sum.firstChild);
    if (s.locked) sum.appendChild(document.createTextNode("Waiting for the shop to open"));
    else if (live.length) sum.appendChild(document.createTextNode(live.join(" · ") + " available now"));
    else sum.appendChild(document.createTextNode("Nothing available right now"));
    sum.appendChild(el("small", "", "checking every " + Math.round(s.mode.interval) + "s"));
  }
  function updateProgress() {
    if (!state || state.checked_ago === null) return;
    var ago = state.checked_ago + (Date.now() - fetchedAt) / 1000;
    var frac = Math.max(0, Math.min(1, ago / Math.max(2, state.mode.interval)));
    $("progress").setAttribute("stroke-dashoffset", (RING * (1 - frac)).toFixed(2));
  }

  // ---- sell-out clock ----
  function makeClock(d, typical) {
    var box = el("div", "clock"), txt = el("div", "txt"), track = el("div", "track"), fill = el("div", "fill");
    track.appendChild(fill); box.appendChild(txt); box.appendChild(track);
    clocks.push({ box: box, txt: txt, fill: fill, since: d.since, typical: typical, t0: Date.now() });
    return box;
  }
  function updateClocks() {
    clocks.forEach(function (c) {
      var since = c.since + (Date.now() - c.t0) / 1000, left = c.typical - since;
      if (left > 0) {
        c.box.className = "clock";
        c.txt.textContent = "Usually gone in " + dur(c.typical) + " · about " + dur(left) + " left";
        c.fill.style.width = Math.max(2, Math.round(left / c.typical * 100)) + "%";
      } else {
        c.box.className = "clock late";
        c.txt.textContent = "Past the usual " + dur(c.typical) + " · live for " + dur(since);
        c.fill.style.width = "100%";
      }
    });
  }

  // ---- the full-screen buy button ----
  function hideBig() { clearTimeout(bigTimer); $("big").className = "ovl"; }
  function showBig(items) {
    var big = $("big");
    while (big.firstChild) big.removeChild(big.firstChild);
    big.appendChild(el("div", "title", "Just in stock"));
    items.slice(0, 3).forEach(function (d) {
      var b = el("button", "bigbtn", "Buy " + d.shape + (d.size ? " · " + d.size : ""));
      b.appendChild(el("small", "", d.product + (d.sgd ? " · " + d.sgd : "")));
      b.addEventListener("click", function () { haptic("warning"); hideBig(); openUrl(d.buy); });
      big.appendChild(b);
    });
    var x = el("button", "xbtn", "Close"); x.addEventListener("click", hideBig); big.appendChild(x);
    big.className = "ovl show";
    clearTimeout(bigTimer); bigTimer = setTimeout(hideBig, 25000);
  }

  // ---- the practice drill ----
  function renderDrill(s) {
    var d = s.drill, txt;
    function sec(ms) { return ms ? (ms / 1000).toFixed(1) + " s" : "--"; }
    if (!d.count) txt = "Race the clock: tap the size, then pay at the shop. The clock is how fast " + d.shape + " really sells out (about " + dur(d.limit) + ").";
    else txt = "Best " + sec(d.best) + " · average " + sec(d.avg) + " · " + d.count + " drill" + (d.count === 1 ? "" : "s") + "\n" + d.shape + " really sells out in about " + dur(d.limit);
    $("drillStats").textContent = txt;
  }
  function clearDrillTimers() { drillTimers.forEach(function (t) { clearTimeout(t); clearInterval(t); }); drillTimers = []; }

  function startDrill() {
    var limit = Math.max(8, (state && state.drill && state.drill.limit) || 45);
    var shape = (state && state.drill && state.drill.shape) || "KH1";
    var box = $("drill");
    box.className = "ovl show";
    function clear() { while (box.firstChild) box.removeChild(box.firstChild); }
    function button(text, cls, fn) { var b = el("button", cls, text); b.addEventListener("click", fn); return b; }
    function close() { clearDrillTimers(); box.className = "ovl"; }

    function wait() {
      clearDrillTimers(); clear();
      box.appendChild(el("div", "title", "Practice drop"));
      box.appendChild(el("div", "dtext", "Get ready…\nit can start any second"));
      box.appendChild(button("Cancel", "xbtn", close));
      drillTimers.push(setTimeout(go, 2500 + Math.random() * 5500));
    }

    function go() {
      var t0 = performance.now(), finished = false, stage = 1;
      var timerText = el("div", "txt", ""), track = el("div", "track"), fill = el("div", "fill");
      var clockBox = el("div", "clock"), area = el("div", "");
      track.appendChild(fill); clockBox.appendChild(timerText); clockBox.appendChild(track);
      haptic("warning"); clear();
      box.appendChild(el("div", "title", shape + " in stock"));
      box.appendChild(clockBox); box.appendChild(area);

      function draw() {
        while (area.firstChild) area.removeChild(area.firstChild);
        if (stage === 1) {
          area.appendChild(button(shape + "\n(tap the size)", "bigbtn", function () {
            stage = 2; haptic("warning"); draw();
            drillTimers.push(setTimeout(function () { if (!finished) { stage = 3; draw(); } }, 1200));
          }));
        } else if (stage === 2) {
          area.appendChild(el("div", "dtext", "Opening the shop checkout…"));
        } else {
          var mock = el("div", "mock");
          mock.appendChild(el("div", "mockTitle", "Shop checkout"));
          mock.appendChild(el("div", "", "Contact · filled in ✓"));
          mock.appendChild(el("div", "", "Shipping address · filled in ✓"));
          mock.appendChild(el("div", "", "Payment · paid at the shop"));
          area.appendChild(mock);
          area.appendChild(button("Pay now\n(tap to pay at the shop)", "bigbtn dark", function () { finish(true); }));
        }
      }

      function finish(won) {
        if (finished) return;
        finished = true; clearDrillTimers();
        var ms = Math.round(performance.now() - t0), secs = ms / 1000;
        clear();
        box.appendChild(el("div", "title", won ? "Nice" : "Too slow"));
        box.appendChild(el("div", "big" + (won ? "" : " bad"), won ? secs.toFixed(1) + " s" : "Sold out"));
        box.appendChild(el("div", "dtext", won
          ? "You paid with " + dur(limit - secs) + " to spare.\nA real " + shape + " sells out in about " + dur(limit) + "."
          : "A real " + shape + " sells out in about " + dur(limit) + ". It was gone before you paid.\nTry again, you can go faster."));
        haptic(won ? "success" : "error");
        if (won) celebrate();
        call("POST", "/api/drill", { ms: Math.max(300, ms), won: won }).then(render).catch(function () {});
        box.appendChild(button("Again", "bigbtn", wait));
        box.appendChild(button("Close", "xbtn", close));
      }

      draw();
      drillTimers.push(setInterval(function () {
        var s = (performance.now() - t0) / 1000, left = limit - s;
        if (left <= 0) { finish(false); return; }
        timerText.textContent = s.toFixed(1) + " s · gone in about " + dur(left);
        fill.style.width = Math.round(left / limit * 100) + "%";
      }, 100));
    }
    wait();
  }

  // ---- the lists ----
  function renderShapes(s) {
    var box = $("shapes");
    clocks = [];
    while (box.firstChild) box.removeChild(box.firstChild);
    s.tiles.forEach(function (t) {
      var live = t.count > 0;
      var card = el("div", "shape " + (live ? "live" : "sold"));
      var top = el("div", "top");
      top.appendChild(el("span", "nm", t.shape));
      top.appendChild(el("span", "tag " + (live ? "live" : "sold"), live ? (t.count + " in stock") : "Sold out"));
      card.appendChild(top);
      t.live.forEach(function (d) {
        var row = el("div", "deck");
        if (d.image) { var im = el("img"); im.src = d.image; im.alt = ""; im.loading = "lazy"; row.appendChild(im); }
        var meta = el("div", "meta");
        meta.appendChild(el("div", "name", d.product));
        meta.appendChild(el("div", "sub", [d.size, d.sgd].filter(Boolean).join(" · ")));
        row.appendChild(meta);
        var b = el("button", "buy", "Buy");
        b.addEventListener("click", function () { haptic("warning"); openUrl(d.buy); });
        row.appendChild(b);
        card.appendChild(row);
        if (t.typical && d.since !== null) card.appendChild(makeClock(d, t.typical));
      });
      box.appendChild(card);
    });
  }

  function renderMode(s) {
    var chip = $("chip"), label = "Calm", cls = "pill";
    if (s.locked) { label = "Locked"; cls = "pill lock"; }
    else if (s.problem) { label = "Limited"; cls = "pill warn"; }
    else if (s.mode.fast) { label = "Drop mode"; cls = "pill fast"; }
    else if (s.tiles.some(function (t) { return t.count > 0; })) { label = "In stock"; cls = "pill live"; }
    chip.className = cls; chip.lastChild.textContent = label;
    $("dropBtn").className = "btn drop" + (s.mode.fast ? " on" : "");
    $("calmBtn").className = "btn calmBtn" + (!s.mode.fast ? " on" : "");
    var note = "Checking every " + Math.round(s.mode.interval) + "s";
    if (s.mode.fast && s.mode.sticky) note += " · back to calm after " + mins(s.mode.span) + " with no activity";
    if (s.problem) note += " · " + s.problem;
    $("modeNote").textContent = note;
  }

  function renderStats(s) {
    $("sPick").textContent = String(s.pickups);
    $("sParcel").textContent = String(s.parcels.length);
    $("sNext").textContent = s.next_drop ? dur(Math.max(0, s.next_drop - Date.now() / 1000)) : "--";
  }

  function speedPct(sec) {
    var steps = [[30, 100], [60, 88], [120, 75], [300, 62], [900, 50], [1800, 38], [3600, 25]];
    for (var i = 0; i < steps.length; i++) { if (sec <= steps[i][0]) return steps[i][1]; }
    return 12;
  }
  function renderReport(s) {
    var card = $("reportCard");
    if (!s.last_event) { card.style.display = "none"; return; }
    card.style.display = "";
    $("reportName").textContent = s.last_event.product;
    var bars = $("reportBars");
    while (bars.firstChild) bars.removeChild(bars.firstChild);
    s.last_event.items.forEach(function (it) {
      var row = el("div", "bar");
      row.appendChild(el("div", "k", it.shape + (it.size ? " · " + it.size : "")));
      var track = el("div", "track"), fill = el("div", "fill");
      fill.style.width = (it.gone === null ? 0 : speedPct(it.gone)) + "%";
      track.appendChild(fill); row.appendChild(track);
      row.appendChild(el("div", "v", it.gone === null ? "live" : dur(it.gone)));
      bars.appendChild(row);
    });
    $("heat").textContent = s.last_event.heat || "";
  }

  function renderParcels(s) {
    var card = $("parcelCard"), list = $("parcelList");
    while (list.firstChild) list.removeChild(list.firstChild);
    if (!s.parcels.length) { card.style.display = "none"; return; }
    card.style.display = "";
    s.parcels.forEach(function (p) {
      var row = el("div", "parcel");
      row.appendChild(el("b", "", p.label));
      row.appendChild(el("br"));
      row.appendChild(el("i", "", p.status + (p.event ? " · " + p.event : "")));
      list.appendChild(row);
    });
  }

  function tick() {
    if (!state) return;
    var ago = (state.checked_ago === null ? null : state.checked_ago + (Date.now() - fetchedAt) / 1000);
    var offline = (Date.now() - okAt) > 10000;
    $("foot").textContent = offline ? "Offline · reconnecting…"
      : (ago === null ? "Waiting for the first check" : "Checked " + dur(ago) + " ago") + " · v" + state.version;
    $("sNext").textContent = state.next_drop ? dur(Math.max(0, state.next_drop - Date.now() / 1000)) : "--";
    updateClocks(); updateProgress();
  }

  function render(s) {
    var live = {}, arrivedItems = [];
    s.tiles.forEach(function (t) { live[t.shape] = t.count; });
    var arrived = false;
    if (prevLive) {
      s.tiles.forEach(function (t) {
        if (t.count > (prevLive[t.shape] || 0)) {
          arrived = true;
          t.live.forEach(function (d) { arrivedItems.push({ shape: t.shape, size: d.size, product: d.product, sgd: d.sgd, buy: d.buy }); });
        }
      });
    }
    prevLive = live;
    state = s; fetchedAt = Date.now(); okAt = fetchedAt;
    $("locked").style.display = "none"; $("app").style.display = "";
    renderGauge(s); renderShapes(s); renderMode(s); renderStats(s); renderReport(s); renderParcels(s); renderDrill(s); tick();
    if (arrived) { celebrate(); showBig(arrivedItems); }
  }

  function showLocked() { $("app").style.display = "none"; $("locked").style.display = "block"; }

  function call(method, path, body) {
    var opt = { method: method, cache: "no-store", headers: { "X-Init-Data": initData } };
    if (body) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
    return fetch(path, opt).then(function (r) {
      if (r.status === 401 || r.status === 403) { showLocked(); throw new Error("locked"); }
      return r.json();
    });
  }
  function poll() { call("GET", "/api/state").then(render).catch(function (e) { if (e && e.message !== "locked") console.error(e && e.stack || e); }); }

  $("dropBtn").addEventListener("click", function () { haptic("warning"); call("POST", "/api/drop", { on: true }).then(render).catch(function () {}); });
  $("calmBtn").addEventListener("click", function () { call("POST", "/api/drop", { on: false }).then(render).catch(function () {}); });
  $("drillBtn").addEventListener("click", function () { haptic("warning"); startDrill(); });
  document.addEventListener("visibilitychange", function () { if (!document.hidden) poll(); });
  setInterval(poll, 2000);
  setInterval(tick, 500);
  poll();
})();
</script>
</body>
</html>
"""


def page_html():
    """The radar page, with the theme the bot is set to (auto follows Telegram's light or dark)."""
    theme = RADAR_THEME if RADAR_THEME in ("dark", "light") else "auto"
    return DASHBOARD_HTML.replace('data-theme="auto"', f'data-theme="{theme}"', 1)


class RadarHandler(BaseHTTPRequestHandler):
    server_version = "KnifeRadar"

    def log_message(self, *args):
        pass

    def reply(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else str(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def owner(self):
        """True only for your own Telegram. Anyone else gets 401 or 403 and nothing else."""
        user = verify_init_data(self.headers.get("X-Init-Data", ""))
        if user is None:
            self.reply(401, '{"error": "open this from Telegram"}')
            return False
        if str(user.get("id")) != str(TELEGRAM_CHAT_ID):
            self.reply(403, '{"error": "not yours"}')
            return False
        mark_awake()                                    # the live radar is open: you are up
        return True

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self.reply(200, page_html(), "text/html; charset=utf-8")
        elif path == "/health":
            self.reply(200, "ok", "text/plain")
        elif path.startswith("/go/"):
            parts = path[len("/go/"):].strip("/").split("/")
            target = go_target(parts[0] if parts else "", parts[1] if len(parts) > 1 else "")
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path.startswith("/awake/"):
            target = awake_target(path[len("/awake/"):].strip("/"))
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path == "/api/state":
            if self.owner():
                self.reply(200, json.dumps(dashboard_state()))
        else:
            self.reply(404, '{"error": "not found"}')

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path not in ("/api/drop", "/api/drill"):
            self.reply(404, '{"error": "not found"}')
            return
        if not self.owner():
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(length, 2000)).decode("utf-8") or "{}")
        except Exception:
            self.reply(400, '{"error": "bad request"}')
            return
        if path == "/api/drill":
            ms = body.get("ms")
            if isinstance(ms, bool) or not isinstance(ms, (int, float)) or not 300 <= ms <= 600000:
                self.reply(400, '{"error": "bad request"}')
                return
            record_drill(ms, bool(body.get("won")))
            self.reply(200, json.dumps(dashboard_state()))
            return
        if body.get("on"):
            start_drop(DROP_MANUAL_MINUTES, sticky=True)
        else:
            end_drop()
        self.reply(200, json.dumps(dashboard_state()))


def start_dashboard(port=None):
    """Runs the page on its own thread. Returns the server (or None if it could not start)."""
    port = int(os.getenv("PORT", "8080")) if port is None else port
    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), RadarHandler)
    except OSError as e:
        log(f"Live radar page could not start on port {port}: {e}")
        return None
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = public_url()
    log(f"Live radar page on port {server.server_address[1]}" + (f" · {url}" if url else " · no public domain yet"))
    return server


def handle_radar():
    markup = radar_markup()
    if markup:
        send_text(
            "📡 <h>Live radar</h>\n" + LINE + "\n"
            "A live page inside Telegram: the four sizes with a sell-out clock, a giant BUY button the moment "
            "something lands, drop mode, your pickups and parcels, and a practice drill against your shop's real "
            "sell-out times. It updates every 2 seconds from me, so it adds nothing to the shop.",
            markup,
        )
    else:
        send_text(
            "📡 <h>Live radar needs a web address</h>\n" + LINE + "\n"
            "1. Railway, your bot's service, <b>Settings</b>, <b>Networking</b>, <b>Generate Domain</b>.\n"
            "2. Deploy once. Then send /radar again."
        )


# ============================================================
# ALARM
# Wakes you when a drop lands, but only while you are asleep.
# One loud Bark alert (it ignores Silent and Focus, and loops its sound for 30 seconds). It rings again once a
# minute, at most ALARM_RINGS times, and stops the moment you show a sign of life: tapping the alert, any buy
# button, any command or button in Telegram, or the live radar. It also stops when everything has sold out.
# It stays quiet while you are active: no alarm for 30 minutes after you last used the bot or the
# live radar page. It never rings for the first read after a restart.
# ============================================================

BARK_KEY = os.getenv("BARK_KEY", "").strip()
BARK_SERVER = os.getenv("BARK_SERVER", "https://api.day.app").rstrip("/")
BARK_SOUND = (os.getenv("BARK_SOUND", "alarm").strip() or "alarm").removesuffix(".caf")
BARK_LOOP = os.getenv("BARK_LOOP", "1").strip() != "0"
BARK_EVERY = 60                    # seconds between Bark alerts (one alert loops its sound for 30 s, then a pause)
ALARM_RINGS = max(1, int(float(os.getenv("ALARM_RINGS", "4"))))                # at most this many alerts per drop
ALARM_AWAKE_MINUTES = max(1.0, float(os.getenv("ALARM_AWAKE_MINUTES", "30")))  # quiet this long after you were active
ALARM_ASYNC = True                 # the alarm runs on its own thread so the stock checks never wait for it
ALARM = {"acked": False, "running": False, "headline": "", "buy": "", "ids": [], "token": "", "token_t": 0.0}
AWAKE = {"t": 0.0}                 # when you last used the bot or the live radar
NIGHT_HOURS = "23-9"


def mark_awake():
    """A sign of life (a command, a button, the live radar open). It also stops an alarm that is ringing."""
    AWAKE["t"] = time.time()
    if ALARM["running"]:
        ALARM["acked"] = True


def user_awake():
    return time.time() - AWAKE["t"] < ALARM_AWAKE_MINUTES * 60


def alarm_settings():
    settings = META.get("alarm")
    if not isinstance(settings, dict):
        settings = {"on": False, "hours": NIGHT_HOURS}
        META["alarm"] = settings
    return settings


def parse_hours(text):
    """'23-9' -> (23, 9). 'all' or '' -> None. Raises ValueError for anything else."""
    text = str(text or "").strip().lower()
    if text in ("", "all", "always"):
        return None
    start, end = (int(x) for x in text.replace(" ", "").split("-"))
    if not (0 <= start <= 23 and 0 <= end <= 24) or start == end:
        raise ValueError(text)
    return start, end


def in_alarm_hours(now=None):
    window = parse_hours(alarm_settings().get("hours") or "")
    if window is None:
        return True
    start, end = window
    hour = local_time(time.time() if now is None else now).hour
    return (start <= hour < end) if start < end else (hour >= start or hour < end)


def alarm_active():
    """The alarm is on and it is inside its hours."""
    return bool(alarm_settings().get("on")) and in_alarm_hours()


def alarm_mode():
    settings = alarm_settings()
    if not settings.get("on"):
        return "off"
    hours = settings.get("hours") or ""
    return "night" if hours == NIGHT_HOURS else ("always" if hours == "" else "custom")


def bark_push(title, body, url=""):
    """A Bark critical alert: ignores Silent and Do Not Disturb, plays at full volume and loops its sound
    for 30 seconds. Tapping it opens `url`. Returns Bark's answer as one short line, e.g. 'success'."""
    if not BARK_KEY:
        return ""
    payload = {"device_key": BARK_KEY, "title": title[:100], "body": body[:300], "level": "critical",
               "volume": 10, "sound": BARK_SOUND, "group": "knife-radar"}
    if BARK_LOOP:
        payload["call"] = "1"
    if url:
        payload["url"] = url
    request = urllib.request.Request(
        f"{BARK_SERVER}/push", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    with urllib.request.urlopen(request, timeout=15) as response:
        reply = json.loads(response.read().decode("utf-8", "ignore") or "{}")
    code = reply.get("code")
    return "success" if code == 200 else f"error {code}: {reply.get('message') or 'no message'}"


def bark_problem(reply):
    return not str(reply or "").startswith("success")


def bark_help(detail=""):
    return (
        "📲 <h>The Bark alert didn't go through</h>\n" + LINE + "\n"
        + (f"Bark said: <i>{esc(detail)}</i>\n" if detail else "")
        + "Check: <b>BARK_KEY</b> is the key from the Bark app (the code after <b>day.app/</b> in its test address), "
        "and Bark is allowed to send Critical Alerts in iPhone Settings, Notifications, Bark."
    )


def alarm_headline(items):
    names = []
    for it in group_by_deck(items)[:2]:
        shapes = " ".join(x["shape"] for x in it)
        names.append(f"{shapes} {it[0]['product']}")
    return " and ".join(names)


def alarm_gone(ids):
    """True once none of these sizes is in stock any more."""
    with DATA_LOCK:
        snapshot = dict(CURRENT_SNAPSHOT)
    return not any(snapshot.get(i, {}).get("available") for i in ids)


def awake_link(token):
    """The address inside the Bark alert. Tapping it stops the alarm and sends the phone to the checkout."""
    url = public_url()
    return f"{url}/awake/{token}" if url else ""


BOT = {"username": ""}             # filled in at start-up, for the link back to this chat


def awake_target(token):
    """What happens when the alert is tapped: the alarm stops. One deck of your size: straight to its checkout.
    Several decks: back to this Telegram chat, where the drop message has a button for each.
    The token belongs to the latest alarm and works for an hour, so a second tap still lands somewhere useful."""
    token = str(token or "")
    good = bool(token and ALARM.get("token")) and hmac.compare_digest(token, ALARM["token"]) \
        and time.time() - float(ALARM.get("token_t") or 0) < 3600
    if not good:
        return SITE
    ALARM["acked"] = True
    AWAKE["t"] = time.time()
    return ALARM.get("buy") or SITE


def alarm_landing(items):
    """Where tapping the alert should take you."""
    mine = [it for it in items if it.get("watch") or it["shape"] in my_sizes()]
    decks = {it["handle"] for it in mine}
    if len(decks) <= 1 and mine:
        return mine[0]["cart_url"]
    return f"https://t.me/{BOT['username']}" if BOT["username"] else (mine or items)[0]["cart_url"]


def alarm_run(items, test=False):
    """One alarm: a Bark alert now, then once a minute, at most ALARM_RINGS times (a test rings 2 times).
    It stops when you show a sign of life or when everything has sold out. Failures are explained once."""
    token = secrets.token_urlsafe(12)
    ALARM.update(acked=False, running=True, ids=[it["id"] for it in items], token=token, token_t=time.time(),
                 headline="TEST · this is only a test" if test else alarm_headline(items),
                 buy=SITE if test else alarm_landing(items))
    link = awake_link(token)
    max_sends = 2 if test else ALARM_RINGS
    sends = fails = 0
    told = False
    outcome = "acked"
    started = time.time()
    try:
        while not ALARM["acked"]:
            if not test and alarm_gone(ALARM["ids"]):
                outcome = "gone"                       # it's over: no point ringing any more
                break
            if sends >= max_sends:
                outcome = "stopped"
                break
            sends += 1
            body = ALARM["headline"] + "\nTap to stop and buy."
            try:
                reply = bark_push("Knife Mfg Co: TEST" if test else "Knife Mfg Co: IN STOCK", body, link or ALARM["buy"])
                failed = bark_problem(reply)
            except Exception as e:
                log(f"Bark alert failed: {e}")
                reply, failed = f"{type(e).__name__}: {e}", True
            if failed:
                fails += 1
                if not told:
                    told = True
                    send_text(bark_help(reply))
                if fails >= 3:
                    outcome = "failed"
                    break
            else:
                fails = 0
            for _ in range(BARK_EVERY):
                if ALARM["acked"] or (not test and alarm_gone(ALARM["ids"])):
                    break
                time.sleep(1)
        if outcome == "acked":
            if test:
                send_text("✅ <h>It works</h>\n" + LINE + "\nYou tapped the test alert: it stopped, and the shop opened.")
        elif outcome == "failed":
            send_text("⏹ <h>Alarm stopped</h>\n" + LINE + "\nBark kept failing. Check the key, then try the test again.")
        elif test:
            send_text("⏹ <h>Test finished</h>\n" + LINE + "\nNobody tapped it. Tap the alert next time: it stops and opens the shop.")
        elif outcome == "stopped":
            log(f"Alarm rang {sends} times with no answer, stopped")
    except Exception as e:
        log(f"Alarm problem: {e}")
    finally:
        ALARM["running"] = False


def ring_alarm(items, test=False):
    """Called right after a drop alert was delivered. Rings only if the alarm is on, it is inside its hours,
    Bark is set up and you have been away for a while. If an alarm is already ringing it switches to the newest drop."""
    if not test:
        if not (alarm_active() and BARK_KEY):
            return
        items = [it for it in items if it.get("watch") or it["shape"] in my_sizes()]
        if not items:
            return                                  # only sizes you did not choose: the normal alert is enough
        if ALARM["running"]:
            ALARM["ids"] = list(dict.fromkeys(ALARM["ids"] + [it["id"] for it in items]))
            return
        if user_awake():
            return
    elif ALARM["running"]:
        return
    if ALARM_ASYNC:
        threading.Thread(target=alarm_run, args=(items, test), daemon=True).start()
    else:
        alarm_run(items, test)


def edit_message(query, text, markup):
    """Redraws the message a button was tapped on."""
    telegram_api("editMessageText", {
        "chat_id": str(query.get("message", {}).get("chat", {}).get("id", "")),
        "message_id": query["message"]["message_id"],
        "text": render(text),
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
        "reply_markup": json.dumps(markup),
    })


def hours_label(hours):
    window = parse_hours(hours or "")
    if window is None:
        return "any hour"
    return f"{window[0]:02d}:00 to {window[1] % 24:02d}:00 (your time)"


def alarm_summary():
    """One line for the panel and the alarm screen."""
    mode = alarm_mode()
    hours = alarm_settings().get("hours")
    if mode == "off":
        return "🔕 Alarm off"
    if mode == "night":
        return f"🌙 Night alarm on · {hours_label(hours)}"
    if mode == "always":
        return "🔔 Alarm on · any hour"
    return f"🔔 Alarm on · {hours_label(hours)}"


def alarm_text():
    lines = ["🚨 <h>Alarm</h>", LINE, alarm_summary()]
    if not BARK_KEY:
        lines.append("📲 Bark not set up: add BARK_KEY in Railway")
    elif not public_url():
        lines.append("⚠️ No web address yet: the alert can't tell it was tapped (see /radar)")
    lines += [
        "🎯 My sizes: " + (" · ".join(my_sizes()) if len(my_sizes()) < len(SHAPES) else "all four"),
        f"😴 Rings only if you haven't used the bot for {ALARM_AWAKE_MINUTES:g} min",
        LINE,
        f"One loud alert, then once a minute (at most {ALARM_RINGS}). It stops as soon as you tap it, any buy "
        "button, or anything in the bot, and when the drop is over.",
    ]
    return "\n".join(lines)


def alarm_markup():
    mode = alarm_mode()
    night_only = (alarm_settings().get("hours") or "") != ""
    return {"inline_keyboard": [
        [{"text": btn(f"📲 Alarm: {'Off' if mode == 'off' else 'On'}"), "callback_data": "al:toggle"}],
        [{"text": btn(f"🌙 Only 23:00-09:00: {'On' if night_only else 'Off'}"), "callback_data": "al:hours"}],
        [{"text": btn("🎯 My sizes"), "callback_data": "sz:open"}],
        [{"text": btn("🧪 Test"), "callback_data": "al:test"},
         {"text": btn("◀ Panel"), "callback_data": "pn:home"}],
    ]}


def test_alarm_items():
    return [{"id": "test", "product": "Test Deck", "shape": "KH1", "handle": "test", "cart_url": SITE}]


def switch_alarm(settings, on):
    """Turns the alarm on or off. It can't be on without Bark. Returns (changed, pop-up text)."""
    if on and not BARK_KEY:
        return False, "Set up Bark first (BARK_KEY)"
    settings["on"] = on
    return True, "Alarm on 📲" if on else "Alarm off"


def handle_alarm_tap(query, data):
    """A button on the alarm screen. Returns the little pop-up text."""
    key = data[3:]
    settings = alarm_settings()
    if key == "toggle":
        _, toast = switch_alarm(settings, not settings.get("on"))
    elif key == "hours":
        settings["hours"] = "" if settings.get("hours") else NIGHT_HOURS
        toast = "Rings only 23:00 to 09:00 🌙" if settings["hours"] else "Rings at any hour"
    elif key == "test":
        ring_alarm(test_alarm_items(), test=True)
        toast = "Test alert sent 📲" if BARK_KEY else "Set up Bark first (BARK_KEY)"
    else:
        return "✓"
    save_meta()
    edit_message(query, alarm_text(), alarm_markup())
    return toast


def handle_alarm(argument):
    """/alarm shows the buttons. Typing works too: /alarm on, /alarm off, /alarm hours 23-9 (or all), /alarm test."""
    parts = argument.strip().lower().split()
    settings = alarm_settings()
    if parts and parts[0] in ("on", "off"):
        changed, _ = switch_alarm(settings, parts[0] == "on")
        if not changed:
            send_text("📲 <h>Bark not set up</h>\n" + LINE + "\nInstall Bark, copy its key, add <b>BARK_KEY</b> in Railway "
                      "Variables and deploy. Then switch the alarm on here.", alarm_markup())
            return
        save_meta()
    elif parts and parts[0] == "hours" and len(parts) >= 2:
        try:
            parse_hours(parts[1])
        except Exception:
            send_text("🤔 Try <b>/alarm hours 23-9</b> (24-hour clock) or tap a button below.", alarm_markup())
            return
        settings["hours"] = "" if parts[1] in ("all", "always") else parts[1]
        save_meta()
    elif parts and parts[0] == "test":
        if not BARK_KEY:
            send_text("📲 <h>Bark not set up</h>\n" + LINE + "\nInstall Bark, copy its key, add <b>BARK_KEY</b> in Railway "
                      "Variables and deploy.")
            return
        ring_alarm(test_alarm_items(), test=True)
        return
    elif parts:
        send_text("🤔 I didn't understand that. Use the buttons below.", alarm_markup())
        return
    send_text(alarm_text(), alarm_markup())


# ============================================================
# CONTROL PANEL
# Everything on buttons.
# ============================================================

def smart_on():
    return switch_on("smart")


def panel_text():
    fast = fast_now()
    return "\n".join([
        "🎛 <h>Control panel</h>",
        LINE,
        (f"⚡ Drop mode on · every {current_interval():g}s" if fast else f"🐢 Calm · every {current_interval():g}s"),
        alarm_summary(),
        ("🔮 Smart drop on" if smart_on() else "🔮 Smart drop off"),
        ("⚡ Auto drop on: fast checking whenever something is in stock" if auto_drop_on()
         else "⚡ Auto drop off"),
        "🎯 My sizes: " + (" · ".join(my_sizes()) if len(my_sizes()) < len(SHAPES) else "all four"),
    ] + ([] if checkout_prefilled() else
         ["✍️ <b>Checkout is not pre-filled.</b> Add the CHECKOUT_ variables in Railway: it saves you typing during a drop."]))


def checkout_prefilled():
    return bool(os.getenv("CHECKOUT_EMAIL", "").strip())


def panel_markup():
    def state(label, on):
        return btn(f"{label}: {'On' if on else 'Off'}")
    radar = radar_markup()
    radar_button = radar["inline_keyboard"][0][0] if radar else {"text": btn("📈 Live radar"), "callback_data": "pn:radar"}
    return {"inline_keyboard": [
        [{"text": btn("📡 Status"), "callback_data": "pn:status"}, radar_button],
        [{"text": state("⚡ Drop mode", fast_now()), "callback_data": "pn:drop"},
         {"text": state("🚨 Alarm", alarm_settings().get("on")), "callback_data": "pn:alarm"}],
        [{"text": btn("🎯 My sizes"), "callback_data": "sz:open"},
         {"text": btn("⚙ Alarm settings"), "callback_data": "pn:alarmset"}],
        [{"text": state("🔮 Smart drop", smart_on()), "callback_data": "pn:smart"},
         {"text": state("⚡ Auto drop", auto_drop_on()), "callback_data": "pn:auto"}],
        [{"text": state("🧹 Tidy drops", board_on()), "callback_data": "pn:board"},
         {"text": state("🆕 New products", news_on()), "callback_data": "pn:news"}],
        [{"text": btn("👀 Watching"), "callback_data": "pn:watch"},
         {"text": btn("🏆 Pickups"), "callback_data": "pn:stats"}],
        [{"text": btn("📦 Parcels"), "callback_data": "pn:parcels"},
         {"text": btn("📜 History"), "callback_data": "pn:history"}],
        [{"text": btn("🔮 Forecast"), "callback_data": "pn:forecast"},
         {"text": btn("🔤 Fonts"), "callback_data": "pn:fonts"}],
        [{"text": btn("❔ Help"), "callback_data": "pn:help"}],
    ]}


def send_panel():
    send_text(panel_text(), panel_markup())


def handle_panel_tap(query, data):
    """A button on the control panel. Switches redraw the panel, the others open their own message."""
    key = data[3:]
    if key == "drop":
        if fast_now():
            end_drop()
            toast = "Calm mode"
        else:
            start_drop(DROP_MANUAL_MINUTES, sticky=True)
            toast = "Drop mode on ⚡"
    elif key == "alarm":
        settings = alarm_settings()
        changed, toast = switch_alarm(settings, not settings.get("on"))
        if changed:
            save_meta()
    elif key in ("smart", "auto", "board", "news"):
        toast = flip_switch("autodrop" if key == "auto" else key)
    elif key == "alarmset":
        edit_message(query, alarm_text(), alarm_markup())
        return "Alarm settings"
    elif key == "home":
        edit_message(query, panel_text(), panel_markup())
        return "Panel"
    elif key == "status":
        send_text(status_text(), status_markup())
        return "Status ⬇"
    elif key == "radar":
        handle_radar()
        return "Live radar ⬇"
    elif key == "watch":
        handle_watch("")
        return "Watching ⬇"
    elif key == "stats":
        send_text(stats_text())
        return "Pickups ⬇"
    elif key == "parcels":
        send_text(parcels_text())
        return "Parcels ⬇"
    elif key == "history":
        send_text(history_text())
        return "History ⬇"
    elif key == "forecast":
        send_text(forecast_text())
        return "Forecast ⬇"
    elif key == "help":
        send_text(help_text(), help_markup())
        return "Help ⬇"
    elif key == "fonts":
        edit_message(query, fonts_text(), fonts_markup())
        return "Fonts"
    else:
        return "✓"
    edit_message(query, panel_text(), panel_markup())
    return toast


# ============================================================
# MY PICKUPS
# The bot can't see your payment (it happens in the shop's own checkout),
# so a pickup is counted when YOU send /bought.
# ============================================================

CONGRATS = [
    "Nice grab! 🛹",
    "Another one for the collection! 🛹",
    "You got it! ⚡",
    "Smooth. That one's yours. 🎯",
    "Clean catch! 🙌",
    "Fast fingers! ⚡",
]
MILESTONES = (1, 5, 10, 25, 50, 100)


def record_purchase(item=None, note=""):
    """Adds one pickup to the saved list. Returns (count, entry)."""
    sgd = 0.0
    if item:
        try:
            sgd = round(float(item["price"]) * FX["rate"], 2)
        except Exception:
            sgd = 0.0
    entry = {
        "t": time.time(),
        "vid": item["id"] if item else "",
        "product": item["product"] if item else (note.strip() or "a deck"),
        "shape": item["shape"] if item else "",
        "size": item["size"] if item else "",
        "sgd": sgd,
    }
    META.setdefault("purchases", []).append(entry)
    save_meta()
    return len(META["purchases"]), entry


def describe(entry):
    parts = [entry.get("product") or "a deck"]
    if entry.get("shape"):
        parts.append(entry["shape"])
    if entry.get("size"):
        parts.append(entry["size"])
    return " · ".join(parts)


def congrats_text(count, entry):
    lines = [
        "🎉 <h>CONGRATS!</h>",
        LINE,
        f"🛹 {esc(describe(entry))}",
        f"🏆 Pickup #{count}",
        CONGRATS[(count - 1) % len(CONGRATS)],
    ]
    lines.append("📦 <i>Got the tracking number? Send /track NUMBER</i>")
    if count in MILESTONES:
        lines.append(f"🏅 <b>Milestone: {count} deck{'s' if count != 1 else ''}!</b>")
    return "\n".join(lines)


def stats_text():
    items = META.get("purchases") or []
    head = ["🏆 <h>KNIFE MFG CO · my pickups</h>", LINE]
    if not items:
        return "\n".join(head + ["No pickups yet.", "After you buy, send /bought to count it."])
    tz = timezone(timedelta(hours=TZ_OFFSET_HOURS))
    this_month = datetime.now(tz).strftime("%Y-%m")
    month = sum(1 for p in items if datetime.fromtimestamp(p["t"], tz).strftime("%Y-%m") == this_month)
    by_shape = "  ".join(
        f"{s} {n}" for s, n in ((s, sum(1 for p in items if p.get("shape") == s)) for s in SHAPES) if n
    )
    spent = sum(float(p.get("sgd") or 0) for p in items)
    last = items[-1]
    lines = head + [
        f"🎉 <b>{len(items)}</b> deck{'s' if len(items) != 1 else ''} bought",
        f"📅 this month · {month}",
        f"🕒 last · {esc(describe(last))} · {ago(time.time() - last['t'])}",
    ]
    if by_shape:
        lines.append(f"🛹 {by_shape}")
    if spent:
        lines.append(f"💰 about S${spent:.0f} spent")
    return "\n".join(lines)


def handle_bought_tap(data):
    """A tap on '✅ I bought it' (b:ids) or on a size choice (c:id). Returns the little pop-up text."""
    kind, _, payload = data.partition(":")
    if payload == "test":
        return "That was only a test alert, nothing counted ✓"
    ids = [x for x in payload.split(",") if x.isdigit()]
    with DATA_LOCK:
        snapshot = dict(CURRENT_SNAPSHOT)
    items = [snapshot[i] for i in ids if i in snapshot]
    if kind == "c" and ids and not items:
        past = item_from_events(ids[0])
        items = [past] if past else []

    if kind == "b" and len(items) > 1:
        buttons = [
            {"text": btn(f"{it['shape']} · {it['size']}" if it["size"] else it["shape"]), "callback_data": f"c:{it['id']}"}
            for it in items
        ]
        rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        rows.append([{"text": btn("🤷 Not sure"), "callback_data": "c:0"}])
        send_text("🛹 <h>Which size did you get?</h>", {"inline_keyboard": rows})
        return "Pick your size 👇"

    chosen = items[0] if items else None
    last = (META.get("purchases") or [None])[-1]
    same = last and (last.get("vid") == chosen["id"] if chosen else last.get("vid") == "")
    if same and time.time() - last["t"] < 120:
        return "Already counted ✓"        # a double tap

    count, entry = record_purchase(chosen)
    send_text(congrats_text(count, entry))
    return "Counted 🎉"


def item_from_events(vid):
    """A size from a past drop, for when the shop list has changed since."""
    for event in reversed(META.get("events") or []):
        entry = event["items"].get(vid)
        if entry is not None:
            return {"id": vid, "product": event["product"], "shape": entry["shape"],
                    "size": entry["size"], "price": ""}
    return None


def handle_bought_command(argument):
    """/bought All Over 2 KL1   adds it straight away.
       /bought                 asks which size of the latest drop (so a menu tap never counts by accident)"""
    note = argument.strip()
    if note:
        count, entry = record_purchase(None, note)
        send_text(congrats_text(count, entry))
        return
    events = META.get("events") or []
    last = events[-1] if events and time.time() - events[-1]["t"] < 7 * 86400 else None
    if not last:
        send_text(
            "🛒 Send <b>/bought</b> and what you got, like <b>/bought All Over 2 KL1</b>."
        )
        return
    buttons = [
        {"text": btn(f"{e['shape']} · {e['size']}" if e["size"] else e["shape"]), "callback_data": f"c:{vid}"}
        for vid, e in last["items"].items()
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([{"text": btn("🤷 Something else"), "callback_data": "c:0"},
                 {"text": btn("✖ Cancel"), "callback_data": "x:0"}])
    send_text(f"🛹 <h>Which did you buy?</h>\n{LINE}\n{esc(last['product'])}", {"inline_keyboard": rows})


def handle_undo(confirmed=False):
    items = META.get("purchases") or []
    if not items:
        send_text("↩️ Nothing to undo. You have no pickups saved.")
        return
    if not confirmed:
        send_text(
            f"↩️ Remove your last pickup?\n{esc(describe(items[-1]))}",
            {"inline_keyboard": [[
                {"text": btn("✅ Yes, remove"), "callback_data": "undo:yes"},
                {"text": btn("✖ Keep it"), "callback_data": "x:0"},
            ]]},
        )
        return
    removed = items.pop()
    save_meta()
    send_text(f"↩️ Removed your last pickup ({esc(describe(removed))}). You now have <b>{len(items)}</b>.")


# ============================================================
# PARCELS
# From the shop to your home.
# The shop's shipping email has a tracking number. Send it with
# /track NUMBER and the radar follows the parcel for you. Live
# updates come from USPS and Parcels App (keys in Railway).
# ============================================================

PARCELS_KEY = os.getenv("PARCELS_API_KEY", "").strip()
PARCELS_BASE = os.getenv("PARCELS_BASE", "https://parcelsapp.com/api/v4").rstrip("/")
PARCELS_COUNTRY = (os.getenv("PARCEL_COUNTRY", "SG").strip() or "SG")
PARCELS_POLLS = 8                  # how many times to ask for an unfinished lookup (3 s apart)
ASYNC_FIRST_LOOKUP = True          # the first lookup after /track runs on its own thread
PARCEL_CHECK_MINUTES = max(10.0, float(os.getenv("PARCEL_CHECK_MINUTES", "60")))
_ph = os.getenv("PARCEL_DAILY_HOUR", "9").strip()
PARCEL_DAILY_HOUR = int(_ph) if _ph.isdigit() and 0 <= int(_ph) <= 23 else None
PARCEL_LOCK = threading.Lock()

PARCEL_STATUS = {
    "NotFound": ("🔎", "not scanned yet"),
    "InfoReceived": ("📝", "label created"),
    "InTransit": ("🚚", "on the way"),
    "OutForDelivery": ("🏃", "out for delivery"),
    "AvailableForPickup": ("📍", "ready for pickup"),
    "DeliveryFailure": ("⚠️", "delivery failed"),
    "Delivered": ("📦", "delivered"),
    "Exception": ("⚠️", "a problem was reported"),
    "Expired": ("⌛", "tracking expired"),
}


def status_label(status):
    emoji, text = PARCEL_STATUS.get(status, ("📦", status or "waiting for the first scan"))
    return f"{emoji} {text}"


def track_link(number):
    return f"https://parcelsapp.com/en/tracking/{urllib.parse.quote(number)}"


TRACK_HEALTH = {"service": "", "error": "", "told": False}


def note_tracking_problem(error):
    """Logs which tracking service failed. A rejected key (401/403) or a used-up plan (402) is
    explained once in Telegram, with the variable to fix."""
    service = TRACK_HEALTH.get("service") or "the tracking service"
    code = getattr(error, "code", None)
    TRACK_HEALTH["error"] = f"{service}: {error}"
    log(f"Parcel check failed via {service}: {error}")
    if code in (401, 402, 403) and not TRACK_HEALTH["told"]:
        TRACK_HEALTH["told"] = True
        if service == "USPS":
            advice = ("🔑 <h>USPS didn't accept the keys</h>\n" + LINE + "\n"
                      "Delete <b>USPS_CLIENT_ID</b> and <b>USPS_CLIENT_SECRET</b> in Railway "
                      "(the bot works fine without them), or paste the right Consumer Key and Secret.")
        elif code == 402:
            advice = ("💳 <h>Parcels App plan used up</h>\n" + LINE + "\n"
                      "Parcels App says your plan limit is reached. Check your plan on parcelsapp.com.")
        else:
            advice = ("🔑 <h>Parcels App didn't accept the key</h>\n" + LINE + "\n"
                      "Check <b>PARCELS_API_KEY</b> in Railway: the key only, no spaces or quotes. "
                      "Also confirm your email on parcelsapp.com. Or delete the variable to go back to links only.")
        send_text(advice)


def parcels_call(method, path, payload=None, timeout=25):
    """One call to the Parcels App API. Raises on any problem."""
    TRACK_HEALTH["service"] = "Parcels App"
    request = urllib.request.Request(
        f"{PARCELS_BASE}{path}",
        data=None if payload is None else json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {PARCELS_KEY}", "Content-Type": "application/json",
                 "Accept": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fmt_iso(text):
    """An ISO time from the carrier, shown in your time zone."""
    try:
        moment = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone(timedelta(hours=TZ_OFFSET_HOURS))).strftime("%d %b, %H:%M")
    except Exception:
        return str(text)[:16]


def parcels_status(status, latest_text):
    """Turns Parcels App wording into the radar's own status names."""
    word = str(status or "").lower().replace("_", " ").replace("-", " ")
    text = f"{word} {str(latest_text or '').lower()}"
    if "delivered" in word and "out" not in word and "not" not in word:
        return "Delivered"
    if "out for delivery" in text:
        return "OutForDelivery"
    if "pickup" in word or "ready for pickup" in text or "available for pickup" in text:
        return "AvailableForPickup"
    if any(w in word for w in ("exception", "failed", "undelivered", "returned", "alert")):
        return "Exception"
    if "expired" in word:
        return "Expired"
    if "not found" in word or "notfound" in word:
        return "NotFound"
    if "info" in word or "pre " in word or "label" in text:
        return "InfoReceived"
    if "transit" in word or "arrived" in word or "shipping" in word or word.strip():
        return "InTransit"
    return "InTransit" if str(latest_text or "").strip() else ""


def parse_parcels_reply(reply, number):
    """Status and newest scan out of a Parcels App reply. None if it has nothing usable yet."""
    results = reply.get("results") or []
    result = next((r for r in results if str(r.get("tracking_number", "")).upper() == number.upper()), None)
    if result is None and results:
        result = results[0]
    if not result:
        return None
    shipment = result.get("shipment") or {}
    if not shipment or shipment.get("error"):
        return None
    states = shipment.get("states") or []
    if states and all(st.get("date") for st in states):
        states = sorted(states, key=lambda st: str(st["date"]), reverse=True)
    latest = states[0] if states else {}
    text = str(latest.get("state") or "")
    status = parcels_status(shipment.get("status"), text)
    if not (status or text):
        return None
    return {
        "status": status,
        "event": text,
        "where": str(latest.get("location") or ""),
        "when": str(latest.get("date") or ""),
        "eta": "",
        "handoff": False,
    }


USPS_CLIENT_ID = os.getenv("USPS_CLIENT_ID", "").strip()
USPS_CLIENT_SECRET = os.getenv("USPS_CLIENT_SECRET", "").strip()
USPS_BASE = os.getenv("USPS_BASE", "https://apis.usps.com").rstrip("/")
USPS_TOKEN = {"value": "", "until": 0.0}
HANDOFF = re.compile(r"foreign|destination (country|post)|customs|international|singapore|local post", re.I)


def usps_ready():
    return bool(USPS_CLIENT_ID and USPS_CLIENT_SECRET)


def live_tracking():
    """True when at least one tracking service is set up."""
    return usps_ready() or bool(PARCELS_KEY)


def usps_link(number):
    return f"https://tools.usps.com/go/TrackConfirmAction?tLabels={urllib.parse.quote(number)}"


def usps_token(force=False):
    """OAuth token for the USPS API, kept until it is about to expire."""
    TRACK_HEALTH["service"] = "USPS"
    if not force and USPS_TOKEN["value"] and time.time() < USPS_TOKEN["until"] - 60:
        return USPS_TOKEN["value"]
    fields = {"grant_type": "client_credentials", "client_id": USPS_CLIENT_ID,
              "client_secret": USPS_CLIENT_SECRET, "scope": "tracking"}
    url = f"{USPS_BASE}/oauth2/v3/token"
    attempts = [
        ({"Content-Type": "application/json"}, json.dumps(fields).encode("utf-8")),
        ({"Content-Type": "application/x-www-form-urlencoded"}, urllib.parse.urlencode(fields).encode("utf-8")),
    ]
    last_error = None
    for headers, body in attempts:
        try:
            request = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(request, timeout=20) as response:
                data = json.loads(response.read().decode("utf-8"))
            USPS_TOKEN["value"] = data["access_token"]
            USPS_TOKEN["until"] = time.time() + float(data.get("expires_in") or 3000)
            return USPS_TOKEN["value"]
        except urllib.error.HTTPError as e:
            last_error = e
            if e.code not in (400, 415):      # a wrong format gets a second try, anything else stops
                break
    raise last_error


def usps_call(number, retry=True):
    """The raw USPS tracking reply, or None if USPS doesn't know the number yet (404)."""
    TRACK_HEALTH["service"] = "USPS"
    request = urllib.request.Request(
        f"{USPS_BASE}/tracking/v3/tracking/{urllib.parse.quote(number)}?expand=DETAIL",
        headers={"Authorization": f"Bearer {usps_token()}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        if e.code == 401 and retry:
            usps_token(force=True)
            return usps_call(number, retry=False)
        raise


def usps_status(category, summary):
    text = f"{category} {summary}".lower()
    if "delivered" in str(category).lower():
        return "Delivered"
    if "out for delivery" in text:
        return "OutForDelivery"
    if "available for pickup" in text or "ready for pickup" in text:
        return "AvailableForPickup"
    if any(word in text for word in ("alert", "attempt", "return to sender", "undeliverable", "exception")):
        return "Exception"
    if "pre-shipment" in text or "label created" in text or "shipping label" in text:
        return "InfoReceived"
    return "InTransit" if text.strip() else ""


def parse_usps_reply(data):
    """Status, newest scan and ETA out of a USPS tracking reply. None if there is nothing."""
    if not isinstance(data, dict):
        return None
    if "trackingNumber" not in data and isinstance(data.get("data"), dict):
        data = data["data"]
    category = str(data.get("statusCategory") or data.get("status") or "")
    summary = str(data.get("statusSummary") or "")
    events = data.get("trackingEvents") or data.get("events") or []
    if events and all(e.get("eventTimestamp") for e in events):
        events = sorted(events, key=lambda e: str(e["eventTimestamp"]), reverse=True)
    event = events[0] if events else {}
    text = str(event.get("eventType") or event.get("eventDescription") or event.get("event") or summary or category)
    if not (category or summary or event):
        return None
    where = ", ".join(str(x) for x in (event.get("eventCity"), event.get("eventState"), event.get("eventCountry")) if x)
    return {
        "status": usps_status(category, summary or text),
        "event": text,
        "where": where,
        "when": event.get("eventTimestamp") or event.get("eventDate") or "",
        "eta": data.get("expectedDeliveryTimeStamp") or data.get("expectedDeliveryDate")
               or data.get("predictedDeliveryDate") or "",
        "handoff": bool(HANDOFF.search(f"{text} {summary}")),
    }


def parcels_info(number):
    """Latest info from Parcels App (they follow the parcel through the carriers, including the
    local post abroad). Asks, then asks again until the lookup is finished. None without a key."""
    if not PARCELS_KEY:
        return None
    reply = parcels_call("POST", "/trackings", {
        "language": "en",
        "shipments": [{"tracking_number": number, "destination_country": PARCELS_COUNTRY}],
    })
    for _ in range(PARCELS_POLLS):
        if reply.get("done") or not reply.get("request_id"):
            break
        time.sleep(3)
        reply = parcels_call("GET", f"/trackings/{urllib.parse.quote(str(reply['request_id']))}")
    return parse_parcels_reply(reply, number)


def parcel_info(number):
    """Latest info for one parcel, or None. USPS first (the shop ships with USPS). Once USPS hands
    the parcel to the local post, Parcels App (if set up) takes over for the last leg."""
    if usps_ready():
        info = None
        try:
            info = parse_usps_reply(usps_call(number))
        except Exception as e:
            log(f"USPS tracking failed ({e})")
            if not PARCELS_KEY:
                raise
        if info and not (info.get("handoff") and PARCELS_KEY):
            return info
        if not PARCELS_KEY:
            return info
        return parcels_info(number) or info
    return parcels_info(number)


USPS_NUMBER = re.compile(r"9\d{19,25}|82\d{8}|[A-Z]{2}\d{9}US")


def looks_like_usps(number):
    """USPS tracking numbers: 9400... (20 to 26 digits), 82... (10 digits) or LZ123456789US."""
    return bool(USPS_NUMBER.fullmatch(str(number).upper()))


def parcel_markup(number):
    if usps_ready() or looks_like_usps(number):
        return {"inline_keyboard": [[
            {"text": btn("🔎 USPS page"), "url": usps_link(number)},
            {"text": btn("🌏 Parcels"), "url": track_link(number)},
        ]]}
    return {"inline_keyboard": [[{"text": btn("🔎 Track page"), "url": track_link(number)}]]}


def parcel_text(parcel, heading="PARCEL UPDATE"):
    lines = [f"📦 <h>{heading}</h>", LINE, f"🛹 {esc(parcel['label'])}", status_label(parcel.get("status"))]
    scan = " · ".join(x for x in (parcel.get("event"), parcel.get("where"), fmt_iso(parcel["when"]) if parcel.get("when") else "") if x)
    if scan:
        lines.append(f"📍 {esc(scan)}")
    if parcel.get("eta") and not parcel.get("delivered"):
        lines.append(f"🏁 expected {fmt_iso(parcel['eta'])[:6]}")
    if parcel.get("handoff") and not parcel.get("delivered"):
        lines.append("🌏 Handed to the local post. Tap Parcels for the last leg.")
    lines.append(f"🔢 {esc(parcel['number'])}")
    return "\n".join(lines)


def find_parcel(number):
    for parcel in META.get("parcels") or []:
        if parcel["number"].upper() == number.upper():
            return parcel
    return None


def add_parcel(number, label):
    parcel = {"number": number, "label": label, "added": time.time(), "status": "", "event": "",
              "where": "", "when": "", "eta": "", "delivered": False}
    META.setdefault("parcels", []).append(parcel)
    save_meta()
    return parcel


def poll_parcels(only=None):
    """Checks every parcel on its way and messages you whenever something changed."""
    if not live_tracking():
        return
    with PARCEL_LOCK:
        todo = [p for p in META.get("parcels") or []
                if not p.get("delivered") and (only is None or p["number"].upper() == only.upper())]
    for parcel in todo:
        try:
            info = parcel_info(parcel["number"])
        except Exception as e:
            note_tracking_problem(e)
            continue
        TRACK_HEALTH["error"] = ""
        TRACK_HEALTH["told"] = False
        if not info:
            continue
        changed = (info["status"], info["event"]) != (parcel.get("status"), parcel.get("event"))
        with PARCEL_LOCK:
            parcel.update(info)
            parcel["checked"] = time.time()
            if info["status"] == "Delivered":
                parcel["delivered"] = True
            save_meta()
        if info["status"] == "Delivered" and changed:
            send_text(
                f"🎉 <h>DELIVERED!</h>\n{LINE}\n"
                f"📦 {esc(parcel['label'])} has arrived. Enjoy it! 🛹",
                parcel_markup(parcel["number"]),
            )
        elif changed and (info["status"] or info["event"]):
            send_text(parcel_text(parcel), parcel_markup(parcel["number"]))
        time.sleep(0.4)


def parcels_text():
    parcels = META.get("parcels") or []
    head = ["📦 <h>My parcels</h>", LINE]
    if not parcels:
        return "\n".join(head + ["Nothing on its way.", "Got a tracking number? Send /track NUMBER"])
    lines = list(head)
    for parcel in parcels[-6:]:
        scan = parcel.get("event") or ""
        lines.append(f"🛹 <b>{esc(parcel['label'])}</b>")
        lines.append(f"      {status_label(parcel.get('status'))}" + (f" · {esc(scan)}" if scan else ""))
        lines.append(f"      🔢 {esc(parcel['number'])}")
    if not live_tracking():
        lines += [LINE, "ℹ️ No live updates yet: add the USPS or Parcels App keys in Railway."]
    return "\n".join(lines)


def handle_track(argument):
    """/track NUMBER   or   /track NUMBER All Over 2   (the note is optional)"""
    parts = argument.strip().split(None, 1)
    if not parts:
        send_text(
            "📦 Send <b>/track</b> followed by the tracking number from the shop's shipping email.\n"
            "Example: <b>/track SG123456789</b>\n"
            "Add a name if you like: <b>/track SG123456789 All Over 2</b>"
        )
        return
    number = re.sub(r"\s+", "", parts[0])
    if not re.fullmatch(r"[A-Za-z0-9\-]{8,40}", number):
        send_text("🤔 That doesn't look like a tracking number. It's 8 to 40 letters and numbers, with no spaces.")
        return
    if find_parcel(number):
        send_text("📦 I'm already following that one. See /parcels.")
        return
    last = (META.get("purchases") or [None])[-1]
    label = parts[1].strip() if len(parts) > 1 else (describe(last) if last else "my parcel")
    parcel = add_parcel(number, label)
    if live_tracking():
        send_text(
            f"📦 <h>Tracking started</h>\n{LINE}\n🛹 {esc(label)}\n🔢 {esc(number)}\n"
            "✅ I'll message you on every update.",
            parcel_markup(number),
        )
        def first_lookup():
            try:
                info = parcel_info(number)
                if info:
                    seen_already = (parcel.get("status"), parcel.get("event")) == (info["status"], info["event"])
                    parcel.update(info)
                    save_meta()
                    if not seen_already:          # the hourly check may have got there first
                        send_text(parcel_text(parcel, "FIRST UPDATE"), parcel_markup(number))
                else:
                    send_text("🔎 Nothing scanned yet. I'll tell you when the carrier does.")
            except Exception as e:
                note_tracking_problem(e)
                if getattr(e, "code", None) not in (401, 402, 403):
                    send_text("⚠️ I couldn't reach the tracking service just now. I'll keep trying.")

        if ASYNC_FIRST_LOOKUP:
            threading.Thread(target=first_lookup, daemon=True).start()
        else:
            first_lookup()
    else:
        send_text(
            f"📦 <h>Tracking saved</h>\n{LINE}\n🛹 {esc(label)}\n🔢 {esc(number)}\n"
            "ℹ️ No live updates yet. Add the USPS keys (<b>USPS_CLIENT_ID</b>, <b>USPS_CLIENT_SECRET</b>) "
            "or a Parcels App key (<b>PARCELS_API_KEY</b>) in Railway and I'll message you on every scan. "
            "Meanwhile, tap the button.",
            parcel_markup(number),
        )


def handle_untrack(argument):
    number = re.sub(r"\s+", "", argument)
    parcel = find_parcel(number) if number else None
    if not parcel:
        send_text("🤔 I couldn't find that tracking number. See /parcels.")
        return
    META["parcels"].remove(parcel)
    save_meta()
    send_text(f"🗑 Stopped following {esc(parcel['label'])} ({esc(parcel['number'])}).")


def maybe_parcel_digest():
    """One summary a day while something is on its way."""
    if PARCEL_DAILY_HOUR is None or not live_tracking():
        return
    now = datetime.now(timezone(timedelta(hours=TZ_OFFSET_HOURS)))
    today = now.strftime("%Y-%m-%d")
    if now.hour != PARCEL_DAILY_HOUR or META.get("last_parcel_digest") == today:
        return
    active = [p for p in META.get("parcels") or [] if not p.get("delivered")]
    if not active:
        return
    META["last_parcel_digest"] = today
    save_meta()
    lines = ["📦 <h>Parcel update</h>", LINE]
    for parcel in active:
        lines.append(f"🛹 <b>{esc(parcel['label'])}</b> · {status_label(parcel.get('status'))}")
        if parcel.get("event"):
            lines.append(f"      📍 {esc(parcel['event'])}")
    send_text("\n".join(lines))


def tracking_loop():
    """Runs on its own thread, so a slow tracking service never delays stock checks."""
    log("Parcel tracking started." + ("" if live_tracking() else " (no tracking keys: links only)"))
    last = 0.0
    while True:
        try:
            time.sleep(60)
            if time.time() - last >= PARCEL_CHECK_MINUTES * 60:
                last = time.time()
                poll_parcels()
            maybe_parcel_digest()
        except Exception as e:
            log(f"Parcel loop error: {e}")


# ============================================================
# STATUS AND HELP
# /status, /help and the daily message.
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
    rows = [[
        {"text": btn("🔄 Refresh"), "callback_data": "refresh"},
        {"text": btn("🛒 Open shop"), "url": SITE},
    ]]
    radar = radar_markup()
    if radar:
        rows.append(radar["inline_keyboard"][0])
    return {"inline_keyboard": rows}


def help_markup():
    return {"inline_keyboard": [
        [{"text": btn("🎛 Control panel"), "callback_data": "pn:home"}],
        [{"text": btn("📡 Status"), "callback_data": "refresh"},
         {"text": btn("🛒 Open shop"), "url": SITE}],
    ]}


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
                retry_note(),
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

    watches = META.get("watches") or []
    if watches:
        lines.append(LINE)
        for name in watches:
            hits = [it for it in snapshot.values() if it.get("watch_name") == name and it["available"]]
            seen = any(it.get("watch_name") == name for it in snapshot.values()) or any(
                name.lower() in it["product"].lower() for it in snapshot.values())
            if hits:
                lines.append(f"👀 <b>{esc(name)}</b> · {len(hits)} in stock")
                for it in hits[:3]:
                    lines.append(f"      ▸ {esc(it['product'])} · {esc(it['shape'])}")
            else:
                lines.append(f"👀 <b>{esc(name)}</b> · " + ("sold out" if seen else "no match yet"))

    lines.append(LINE)
    age = time.time() - last_check
    if age > max(30, CHECK_INTERVAL * 6):
        reason = HEALTH["error"] or "store unreachable"
        lines.append(f"⚠️ {reason} · data is {ago(age)[:-4]} old")
    else:
        lines.append(f"🕒 checked {ago(age)}")
    lines.append(mode_line())
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
        f"{shapes}\n"
        "\n"
        "Tap /panel for buttons. Every command also works typed.\n"
        "\n"
        "<h>Stock</h>\n"
        "/status · /panel · /radar\n"
        "\n"
        "<h>Speed</h>\n"
        "/drop (/drop 45, /drop 20:00) · /calm · /autodrop\n"
        "/smart · /forecast · /history\n"
        "\n"
        "<h>Alerts</h>\n"
        "/sizes · /alarm · /watch NAME · /unwatch NAME\n"
        "\n"
        "<h>Buying</h>\n"
        "/stats · /bought · /undo\n"
        "\n"
        "<h>Parcels</h>\n"
        "/parcels · /track NUMBER · /untrack NUMBER\n"
        "\n"
        "<h>More</h>\n"
        "/password WORD · /test · /info · /fonts · /help\n"
        "/board · /news (on or off)\n"
        f"{LINE}\n"
        f"🟢 online · {mode_line()}\n"
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
# STOCK CHECK
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
    products = fetch_products()
    snapshot = build_snapshot(products)
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
        sold_out = []
        for key, item in snapshot.items():
            if key in pending:
                continue                       # remembered only after the alert is delivered
            if STATE.get(key) != item["available"]:
                if STATE.get(key) and not item["available"]:
                    sold_out.append(key)
                STATE[key] = item["available"]
                changed = True
        CURRENT_SNAPSHOT = snapshot
        LAST_CHECK = time.time()
        if changed:
            save_state()
    if sold_out:
        note_sold_out(sold_out)
    try:
        board_tick(snapshot)
    except Exception as e:
        log(f"Drop board update failed: {e}")
    if changed and not fresh_memory:
        touch_activity()                               # stock moved: keep drop mode going

    available = sum(1 for it in snapshot.values() if it["available"])
    auto_drop_when_in_stock(snapshot)

    if reopened:
        SHOP_LOCKED = False
        auto_start_drop()
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
            mine = [it for it in wanted if it["id"] in delivered and is_mine(it)]
            if mine:
                remember_drop(mine)
                record_restock(mine)
                auto_start_drop()                          # more usually follows a restock
            if not fresh_memory:
                ring_alarm(mine)
        missed = len(pending - delivered)
        if missed:
            log(f"{missed} alert(s) could not be delivered. Retrying on the next check.")
    try:
        detect_news(products, snapshot, {it["id"] for it in wanted})
    except Exception as e:
        log(f"Shop news check failed: {e}")

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
# COMMANDS
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


def handle_drop(argument):
    """/drop            fast checking now for 3 hours (DROP_MANUAL_MINUTES), longer while things happen
       /drop 45         fast checking now for 45 minutes
       /drop 20:00      fast from 19:50 to 21:00 (your time zone)
       /drop off        back to calm"""
    arg = argument.strip().lower()
    if arg in ("off", "stop", "calm"):
        end_drop()
        send_text(f"🐢 <h>Calm mode</h>\n{LINE}\n{mode_line()}")
        return
    if not arg:
        start_drop(DROP_MANUAL_MINUTES, sticky=True)
    elif arg.replace(".", "", 1).isdigit():
        start_drop(min(float(arg), 720))
    elif ":" in arg:
        window = schedule_for(arg)
        if not window:
            send_text("🤔 I couldn't read that time. Try <b>/drop 20:00</b> (24-hour clock).")
            return
        begin, end = window
        start_drop((end - begin) / 60, begin=begin)
        if begin > time.time():
            send_text(
                f"📅 <h>Drop mode scheduled</h>\n{LINE}\n"
                f"⚡ {clock(begin)} to {clock(end)}, checking every {DROP_INTERVAL:g}s.\n"
                "/drop off cancels it."
            )
            return
    else:
        send_text("🤔 Try <b>/drop</b>, <b>/drop 45</b> (minutes), <b>/drop 20:00</b> or <b>/drop off</b>.")
        return
    idle = (f"\n🕒 On for {span_text(DROP_MANUAL_MINUTES)}, and longer while things are happening. "
            "If it stays quiet I go back to calm by myself.") if DROP.get("sticky") else ""
    send_text(f"⚡ <h>Drop mode on</h>\n{LINE}\n{mode_line()}{idle}\n/calm switches it off.")


def info_text():
    up = ago(time.time() - STARTED_AT)[:-4]
    return "\n".join([
        "🛠 <h>Radar info</h>",
        LINE,
        f"version {VERSION}",
        f"running for {up}",
        f"memory {'saved' if MEMORY_OK else 'NOT saved (add a Volume at /data)'}",
        mode_line(),
        f"calm {CHECK_INTERVAL:g}s · drop {DROP_INTERVAL:g}s · extra {PACE['extra']:g}s",
        f"fonts: headings {font_style()}, buttons {button_style()}",
        f"watching {', '.join(SHAPES)}",
        "live radar page: " + (public_url() if LIVE_RADAR and public_url() else "needs a Railway domain (see /radar)"),
        f"shop requests: {REQ['sent']} · answered \"unchanged\": {REQ['unchanged']}",
        f"tracking: USPS keys {'yes' if usps_ready() else 'no'} · Parcels key {'yes' if PARCELS_KEY else 'no'}"
        + (f"\n⚠️ last tracking problem: {esc(TRACK_HEALTH['error'])}" if TRACK_HEALTH["error"] else ""),
    ])


def handle_watch(argument):
    """/watch              lists what you watch
       /watch All Over     alerts for any size of a deck with that name"""
    name = re.sub(r"\s+", " ", argument).strip()
    watches = META.setdefault("watches", [])
    if not name:
        if watches:
            body = "\n".join(f"👀 <b>{esc(w)}</b>" for w in watches)
        else:
            body = "Nothing yet."
        send_text(
            f"👀 <h>Watching</h>\n{LINE}\n{body}\n{LINE}\n"
            "<b>/watch All Over</b> alerts you for any size of a deck with that name.\n"
            "<b>/unwatch All Over</b> stops it."
        )
        return
    if not 2 <= len(name) <= 40:
        send_text("🤔 A name should be 2 to 40 characters.")
        return
    if any(w.lower() == name.lower() for w in watches):
        send_text(f"👀 I'm already watching <b>{esc(name)}</b>.")
        return
    if len(watches) >= WATCH_LIMIT:
        send_text(f"👀 That's {WATCH_LIMIT} names already. Use /unwatch to free one.")
        return
    watches.append(name)
    save_meta()
    send_text(
        f"👀 <h>Watching {esc(name)}</h>\n{LINE}\n"
        "Any size of a deck with that name. If one is in stock now, or comes back, you get an alert. "
        "Changes show from my next check."
    )


def handle_unwatch(argument):
    name = re.sub(r"\s+", " ", argument).strip().lower()
    watches = META.get("watches") or []
    found = next((w for w in watches if w.lower() == name), None)
    if not found:
        send_text("🤔 I'm not watching that. See /watch.")
        return
    watches.remove(found)
    save_meta()
    send_text(f"👀 Stopped watching <b>{esc(found)}</b>.")


# The simple on/off settings: /autodrop, /board, /news, and the matching buttons in /panel.
SWITCHES = {
    "autodrop": ("auto_drop", "⚡ Auto drop",
                 "When something is in stock, I switch to fast checking by myself and stay fast while it lasts."),
    "board": ("board", "🧹 Tidy drops",
              "Each deck gets its own card with its photo. Sold-out sizes vanish from it, sold-out decks vanish "
              "from the chat, and one summary is left at the end. Off: classic alerts that stay."),
    "news": ("news", "🆕 New products",
             "One message when the shop lists a new product, adds a size or changes a price."),
    "smart": ("smart", "🔮 Smart drop",
              "Drop mode switches on by itself 10 minutes before the times your shop usually drops."),
}


def switch_on(name):
    return bool(META.get(SWITCHES[name][0], True))


def flip_switch(name, on=None):
    """Sets (or flips) a switch, saves it, and returns the little pop-up text."""
    key, label, _ = SWITCHES[name]
    META[key] = (not switch_on(name)) if on is None else bool(on)
    save_meta()
    return f"{label}: {'on' if META[key] else 'off'}"


def handle_switch(name, argument):
    """/autodrop, /board or /news on its own shows the setting. Add on or off to change it."""
    arg = argument.strip().lower()
    if arg in ("on", "off"):
        flip_switch(name, arg == "on")
    elif arg:
        send_text(f"🤔 Try <b>/{name} on</b> or <b>/{name} off</b>, or use the button in /panel.")
        return
    _, label, explain = SWITCHES[name]
    send_text(f"{label.split(' ', 1)[0]} <h>{label.split(' ', 1)[1]}</h>\n{LINE}\n"
              f"It is <b>{'on' if switch_on(name) else 'off'}</b>.\n{explain}\n"
              f"<b>/{name} on</b> or <b>/{name} off</b>.")


def fonts_text():
    sample = "Knife Mfg Co 123"
    lines = ["🔤 <h>Lettering</h>", LINE,
             f"Headings: <b>{font_style()}</b> · buttons: <b>{button_style()}</b>", ""]
    lines += [f"{'✅' if name == font_style() else '▫️'} {fancy(sample, name)}  <i>{name}</i>" for name in FONT_NAMES]
    lines += [LINE, "Tap a style below. Telegram's own font can't change, so these are special letters."]
    return "\n".join(lines)


def fonts_markup():
    buttons = [{"text": ("✅ " if name == font_style() else "") + fancy(name if name != "spaced" else "spaced", name),
                "callback_data": f"ft:{name}"} for name in FONT_NAMES]
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    plain_buttons = META.get("button_style") == "plain"
    rows.append([{"text": "Buttons: plain" if plain_buttons else "Buttons: same as headings", "callback_data": "ft:buttons"}])
    rows.append([{"text": btn("◀ Panel"), "callback_data": "pn:home"}])
    return {"inline_keyboard": rows}


def handle_font_tap(query, data):
    """A tap on /fonts: changes the lettering straight away (no restart needed)."""
    key = data[3:]
    if key == "buttons":
        META["button_style"] = "" if META.get("button_style") == "plain" else "plain"
        toast = "Buttons in plain letters" if META["button_style"] else "Buttons match the headings"
    elif key in FONT_NAMES:
        META["font_style"] = key
        toast = f"Lettering: {key}"
    else:
        return "✓"
    save_meta()
    edit_message(query, fonts_text(), fonts_markup())
    return toast


def handle_command(command):
    if command == "/status":
        send_text(status_text(), status_markup())
    elif command == "/start":
        send_panel()
    elif command == "/help":
        send_text(help_text(), help_markup())
    elif command == "/test":
        send_test_alert()
    elif command == "/calm":
        handle_drop("off")
    elif command == "/stats":
        send_text(stats_text())
    elif command == "/forecast":
        send_text(forecast_text())
    elif command == "/radar":
        handle_radar()
    elif command == "/alarm":
        handle_alarm("")
    elif command == "/sizes":
        handle_sizes("")
    elif command == "/fonts":
        send_text(fonts_text(), fonts_markup())
    elif command == "/panel":
        send_panel()
    elif command == "/history":
        send_text(history_text())
    elif command == "/parcels":
        send_text(parcels_text(), None)
    elif command == "/undo":
        handle_undo()
    elif command == "/info":
        send_text(info_text())
    else:
        send_text("❓ Unknown command.\n\nUse /help.")


def handle_callback(query):
    chat_id = str(query.get("message", {}).get("chat", {}).get("id", ""))
    if chat_id != str(TELEGRAM_CHAT_ID):
        return
    touch_activity()
    mark_awake()
    data = query.get("data", "")

    if data.startswith("so:"):
        toast = f"{data[3:]} is sold out right now."
    elif data.startswith(("b:", "c:")):
        toast = handle_bought_tap(data)
    elif data.startswith("al:"):
        toast = handle_alarm_tap(query, data)
    elif data.startswith("pn:"):
        toast = handle_panel_tap(query, data)
    elif data.startswith("ft:"):
        toast = handle_font_tap(query, data)
    elif data.startswith("sz:"):
        if data == "sz:open":
            edit_message(query, sizes_text(), sizes_markup())
            toast = "My sizes"
        else:
            toast = toggle_size(data[3:])
            edit_message(query, sizes_text(), sizes_markup())
    elif data == "undo:yes":
        handle_undo(True)
        toast = "Removed ✓"
    elif data.startswith("x:"):
        toast = "Okay, nothing changed ✓"
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
                touch_activity()
                mark_awake()
                text = (message.get("text") or "").strip()
                if not text.startswith("/"):
                    continue
                command = text.split()[0].lower().split("@")[0]
                log(f"Command received: {command}")
                if command == "/sizes":
                    parts = text.split(None, 1)
                    handle_sizes(parts[1] if len(parts) > 1 else "")
                    continue
                if command in ("/board", "/news", "/autodrop"):
                    parts = text.split(None, 1)
                    handle_switch(command[1:], parts[1] if len(parts) > 1 else "")
                    continue
                if command == "/alarm":
                    parts = text.split(None, 1)
                    handle_alarm(parts[1] if len(parts) > 1 else "")
                    continue
                if command == "/watch":
                    parts = text.split(None, 1)
                    handle_watch(parts[1] if len(parts) > 1 else "")
                    continue
                if command == "/unwatch":
                    parts = text.split(None, 1)
                    handle_unwatch(parts[1] if len(parts) > 1 else "")
                    continue
                if command == "/smart":
                    parts = text.split(None, 1)
                    handle_smart(parts[1] if len(parts) > 1 else "")
                    continue
                if command == "/track":
                    parts = text.split(None, 1)
                    handle_track(parts[1] if len(parts) > 1 else "")
                    continue
                if command == "/untrack":
                    parts = text.split(None, 1)
                    handle_untrack(parts[1] if len(parts) > 1 else "")
                    continue
                if command == "/bought":
                    parts = text.split(None, 1)
                    handle_bought_command(parts[1] if len(parts) > 1 else "")
                    continue
                if command == "/drop":
                    parts = text.split(None, 1)
                    handle_drop(parts[1] if len(parts) > 1 else "")
                    continue
                if command == "/password":
                    parts = text.split(None, 1)
                    handle_password(message["chat"]["id"], message["message_id"],
                                    parts[1] if len(parts) > 1 else "")
                else:
                    handle_command(command)

        except Exception as e:
            log(f"Telegram listener error: {e}")
            time.sleep(3)


# Every command, in the order of the Menu button in Telegram.
MENU_COMMANDS = [
    ("panel", "Control panel with buttons"),
    ("status", "Stock right now"),
    ("sizes", "The sizes I buy"),
    ("fonts", "Change the lettering"),
    ("alarm", "Loud alarm for drops while I sleep"),
    ("radar", "Open the live radar page"),
    ("drop", "Fast checking for a drop"),
    ("autodrop", "Auto drop mode on or off"),
    ("calm", "Back to calm checking"),
    ("watch", "Alerts for a deck by name"),
    ("unwatch", "Stop watching a name"),
    ("forecast", "When drops usually happen"),
    ("history", "Past drops and sell-out times"),
    ("smart", "Smart drop on or off"),
    ("stats", "My pickups and spending"),
    ("bought", "Add a pickup"),
    ("undo", "Remove my last pickup"),
    ("parcels", "Parcels on their way"),
    ("track", "Follow a parcel: /track NUMBER"),
    ("untrack", "Stop following a parcel"),
    ("password", "Send the shop password"),
    ("test", "Send a sample alert"),
    ("info", "Version and settings"),
    ("help", "Show all commands"),
]


def setup_telegram():
    log("Checking Telegram connection...")
    result = telegram_api("getMe")
    if not result.get("ok"):
        raise RuntimeError("Telegram bot connection failed (check the token).")
    BOT["username"] = str(result["result"].get("username") or "")
    log(f"Telegram connected: @{BOT['username']}")
    telegram_api("deleteWebhook", {"drop_pending_updates": "false"})   # long polling needs it off
    telegram_api("setMyCommands", {"commands": json.dumps(
        [{"command": name, "description": text} for name, text in MENU_COMMANDS]
    )})
    log("Telegram commands configured.")


# ============================================================
# MAIN
# ============================================================

def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set. In Railway open your service, tap Variables and add it.")
    if not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID is not set. In Railway open your service, tap Variables and add it.")

    log("========================================")
    log("📡 KNIFE MFG CO DECK RADAR")
    log("========================================")
    log(f"Watching: {', '.join(SHAPES)}")
    log(f"Version {VERSION}")
    log(f"Check interval: {CHECK_INTERVAL:g}s calm, {DROP_INTERVAL:g}s in drop mode")
    log(f"Memory file: {STATE_FILE}" + ("" if MEMORY_OK else "  (NOT saved between deploys)"))
    if not MEMORY_OK:
        log("Add a Railway Volume mounted at /data so the radar remembers what it has seen.")

    load_state()
    load_meta()
    DROP["from"] = float(META.get("drop_from") or 0)
    DROP["until"] = float(META.get("drop_until") or 0)
    DROP["sticky"] = bool(META.get("drop_sticky"))
    _FAST["was"] = fast_now()
    load_pace_state()
    board_load()
    refresh_fx()
    setup_telegram()
    offset = drop_stale_updates()

    if cooldown_remaining() > 0:
        # the shop asked for a pause before this restart: a restart is not a reason to ask again at once
        HEALTH["error"] = "the shop asked me to wait (429)"
        log(f"The shop asked for a pause. First check in {cooldown_remaining():.0f}s.")
    else:
        log("Performing initial stock check...")
        try:
            check_stock()
        except ShopLocked as e:
            note_locked(e.text)
        except RateLimited:
            note_rate_limited()
            set_block(20.0)
            HEALTH["error"] = "the shop is limiting my checks (429)"
            log("Initial check: the shop is rate limiting (429). The radar will keep trying.")
        except Exception as e:
            HEALTH["error"] = "the shop isn't answering"
            log(f"Initial check failed: {e}")

    threading.Thread(target=telegram_listener, args=(offset,), daemon=True).start()
    threading.Thread(target=tracking_loop, daemon=True).start()
    if LIVE_RADAR:
        start_dashboard()

    if STARTUP_PING and not SHOP_LOCKED:
        with DATA_LOCK:
            total = len(CURRENT_SNAPSHOT)
            live = sum(1 for it in CURRENT_SNAPSHOT.values() if it["available"])
        lines = [
            "📡 <h>Deck radar online</h>",
            LINE,
            (f"🎯 tracking {total} variants · {live} in stock" if total
             else "⏳ waiting for the shop to answer before the first check"),
            mode_line(),
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
        check_drop_end()
        for housekeeping in (maybe_drop_reports, maybe_smart_drop):
            try:
                housekeeping()
            except Exception as e:
                log(f"{housekeeping.__name__} failed: {e}")
        expire_password()
        maybe_daily_ping()
        if cooldown_remaining() > 0:
            time.sleep(min(cooldown_remaining(), 5.0))
            continue
        try:
            check_stock()
            HEALTH["error"] = ""
            streak_clear()
            BLOCK["until"] = 0.0
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
            set_block(throttle)
            log(f"Shop is rate limiting (429). Waiting {throttle:.0f}s, then checking a little slower "
                f"(every {current_interval():g}s). {limited} in a row.")
            if limited >= 3 and not limited_warned:
                send_text(
                    "⚠️ <h>The shop is limiting my checks (429).</h>\n"
                    f"I'm slowing down by myself: calm checks are now every {current_interval():g}s. "
                    "Alerts can be a little later until it calms down."
                )
                limited_warned = True
            streak_note()
        except Exception as e:
            fails += 1
            HEALTH["error"] = "the shop isn't answering"
            log(f"Check failed ({fails} in a row): {e}")
            if fails >= OFFLINE_AFTER and not warned:
                send_text("⚠️ <h>Radar can't reach the store.</h>\nStill trying. I'll tell you when it's back.")
                warned = True

        # back off when the shop is struggling; a little jitter keeps the rhythm irregular
        base = current_interval() if fails <= 1 else min(60, current_interval() * 2 ** min(fails - 1, 4))
        time.sleep(max(0, base * random.uniform(0.9, 1.3) + throttle - (time.time() - started)))


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
