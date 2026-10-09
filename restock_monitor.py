"""
FAKE KNIFE SHOP for testing the restock bot. Nothing here is real.

It behaves like a Shopify shop:
  /products.json        the product list the bot reads
  /password             the password page (when the shop is locked)
  /cart/<id>:1          a fake checkout page
  /admin?key=...        your remote control: lock, unlock, set the
                        password, flip decks in and out of stock

Run it:  python fake_shop.py        (uses the PORT variable, default 8080)
Remote control key: ADMIN_KEY variable (default "letmein").
"""
import html
import json
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.getenv("PORT", "8080"))
ADMIN_KEY = os.getenv("ADMIN_KEY", "letmein")
LOCK = threading.Lock()

STATE = {
    "locked": False,
    "password": "dropday",
    "message": "DROP TONIGHT 8PM. Password is in our IG story.",
}

DECKS = [
    ("Fake Deck One", "fake-deck-one", 100),
    ("Fake Deck Two", "fake-deck-two", 200),
    ("Fake Deck Three", "fake-deck-three", 300),
]
SIZES = [("32mm KH1", 1), ("32.5mm KL2", 2), ("34mm KL1", 3), ("34mm KB1", 4), ("29mm K01", 5)]
AVAILABLE = {base + n: False for _, _, base in DECKS for _, n in SIZES}


def products():
    out = []
    for title, handle, base in DECKS:
        out.append({
            "id": base,
            "title": title,
            "handle": handle,
            "images": [{"src": "https://placehold.co/800x1000/1D2B53/FFF1E8/png?text=" + urllib.parse.quote(title)}],
            "variants": [
                {"id": base + n, "title": name, "price": "48.00", "available": AVAILABLE[base + n]}
                for name, n in SIZES
            ],
        })
    return out


def variant_name(vid):
    for title, _, base in DECKS:
        for name, n in SIZES:
            if base + n == vid:
                return f"{title} / {name}"
    return f"variant {vid}"


PAGE = """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>body{{font-family:-apple-system,Helvetica,sans-serif;max-width:560px;margin:24px auto;padding:0 16px;background:#10163a;color:#fff1e8}}
a{{color:#29adff}}button,input{{font-size:16px;padding:10px;margin:4px 0}}.on{{color:#00e436}}.off{{color:#ff004d}}code{{background:#1d2b53;padding:2px 6px}}
</style></head><body>{body}</body></html>"""


class Shop(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("[FAKE SHOP]", self.address_string(), fmt % args, flush=True)

    # ---------- helpers ----------
    def send(self, status, body, ctype="text/html; charset=utf-8", headers=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, where, headers=None):
        self.send(302, "", headers={"Location": where, **(headers or {})})

    def has_cookie(self):
        return "storefront_digest=ok" in self.headers.get("Cookie", "")

    def page(self, title, body, status=200):
        self.send(status, PAGE.format(title=html.escape(title), body=body))

    def locked_for_me(self):
        with LOCK:
            return STATE["locked"] and not self.has_cookie()

    def password_page(self, error=""):
        with LOCK:
            msg = STATE["message"]
        err = f"<p class='off'>{html.escape(error)}</p>" if error else ""
        self.page("Password", (
            "<h1>KNIFE MFG (FAKE)</h1>"
            f"<p>{html.escape(msg)}</p>{err}"
            "<form method='post' action='/password'>"
            "<input type='hidden' name='form_type' value='storefront_password'>"
            "<label>Enter store using password</label><br>"
            "<input type='password' name='password' autocomplete='off'><br>"
            "<button type='submit'>Enter</button></form>"
            "<p>Are you the store owner? <a href='/admin'>Log in here</a></p>"
            "<footer>Powered by Shopify (not really)</footer>"
        ))

    # ---------- routes ----------
    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(url.query)

        if path == "/admin":
            return self.admin(query)
        if path == "/password":
            with LOCK:
                locked = STATE["locked"]
            return self.password_page() if locked else self.redirect("/")
        if self.locked_for_me():
            return self.redirect("/password")

        if path == "/products.json":
            page = int((query.get("page") or ["1"])[0])
            body = {"products": products() if page == 1 else []}
            return self.send(200, json.dumps(body), "application/json")
        if path.startswith("/cart/"):
            ids = path.split("/cart/")[1]
            vid = int(ids.split(":")[0]) if ids.split(":")[0].isdigit() else 0
            fields = "".join(
                f"<li><code>{html.escape(k)}</code> = {html.escape(v[0])}</li>" for k, v in query.items()
            )
            return self.page("Fake checkout", (
                "<h1>FAKE CHECKOUT</h1>"
                f"<p>Item: <b>{html.escape(variant_name(vid))}</b></p>"
                f"<p>Details the link pre-filled:</p><ul>{fields or '<li>(none)</li>'}</ul>"
                "<p>No payment is taken here. This page only proves your button works.</p>"
            ))
        if path.startswith("/products/"):
            return self.page("Product", f"<h1>{html.escape(path.split('/')[-1])}</h1><p>Fake product page.</p>")
        return self.page("Fake shop", "<h1>KNIFE MFG (FAKE)</h1><p>This is a test shop. Open /admin to control it.</p>")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        length = int(self.headers.get("Content-Length", "0") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "ignore"))
        if path != "/password":
            return self.send(404, "not found")
        with LOCK:
            correct = STATE["password"]
            locked = STATE["locked"]
        if not locked:
            return self.redirect("/")
        if (form.get("password") or [""])[0] == correct:
            return self.redirect("/", {"Set-Cookie": "storefront_digest=ok; Path=/; Max-Age=86400"})
        return self.password_page("Wrong password")

    # ---------- remote control ----------
    def admin(self, q):
        def first(name):
            return (q.get(name) or [""])[0]

        if first("key") != ADMIN_KEY:
            return self.page("Admin", (
                "<h1>Remote control</h1><form><input name='key' placeholder='key'>"
                "<button>Open</button></form>"
            ))
        with LOCK:
            if first("lock"):
                STATE["locked"] = first("lock") == "1"
            if first("pw"):
                STATE["password"] = first("pw")
            if first("msg"):
                STATE["message"] = first("msg")
            if first("toggle").isdigit():
                v = int(first("toggle"))
                if v in AVAILABLE:
                    AVAILABLE[v] = not AVAILABLE[v]
            if first("all") in ("on", "off"):
                for v in AVAILABLE:
                    AVAILABLE[v] = first("all") == "on"
        if any(first(n) for n in ("lock", "pw", "msg", "toggle", "all")):
            return self.redirect(f"/admin?key={urllib.parse.quote(ADMIN_KEY)}")

        k = urllib.parse.quote(ADMIN_KEY)
        with LOCK:
            locked, pw, msg = STATE["locked"], STATE["password"], STATE["message"]
            avail = dict(AVAILABLE)
        rows = ""
        for title, _, base in DECKS:
            rows += f"<h3>{html.escape(title)}</h3>"
            for name, n in SIZES:
                vid = base + n
                cls, word = ("on", "IN STOCK") if avail[vid] else ("off", "sold out")
                rows += f"<div><span class='{cls}'>{word}</span> {html.escape(name)} <a href='/admin?key={k}&toggle={vid}'>flip</a></div>"
        self.page("Remote control", (
            "<h1>Fake shop remote</h1>"
            f"<p>Shop is <b>{'LOCKED' if locked else 'OPEN'}</b> · "
            f"<a href='/admin?key={k}&lock=1'>lock</a> · <a href='/admin?key={k}&lock=0'>open</a></p>"
            f"<p>Password: <code>{html.escape(pw)}</code></p>"
            f"<form><input type='hidden' name='key' value='{html.escape(ADMIN_KEY)}'>"
            "<input name='pw' placeholder='new password'><button>Set password</button></form>"
            f"<form><input type='hidden' name='key' value='{html.escape(ADMIN_KEY)}'>"
            f"<input name='msg' placeholder='password page message' value='{html.escape(msg)}'><button>Set message</button></form>"
            f"<p><a href='/admin?key={k}&all=on'>everything in stock</a> · <a href='/admin?key={k}&all=off'>everything sold out</a></p>"
            + rows
        ))


if __name__ == "__main__":
    print(f"[FAKE SHOP] running on port {PORT}. Remote control: /admin?key={ADMIN_KEY}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Shop).serve_forever()
