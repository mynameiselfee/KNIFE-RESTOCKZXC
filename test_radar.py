"""
Automatic checks for the deck radar. Run:  python test_radar.py
Set RADAR_FILE if the script has another name (default restock_monitor.py).
Nothing here touches Telegram or the real shop.
"""
import gzip
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "restock_monitor.py")

if os.path.abspath(__file__) == os.path.abspath(FILE):
    sys.exit(
        "WRONG FILE: this is the TEST file, but it is saved under the radar's name "
        f"({os.path.basename(FILE)}). Replace its contents with restock_radar_railway.py."
    )
TMP = tempfile.mkdtemp()
os.environ.update(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="42", STATE_FILE=os.path.join(TMP, "state.json"))
for name in list(os.environ):
    if name.startswith("CHECKOUT_") or name in ("SHOP_URL", "SHOP_PASSWORD", "USD_TO_SGD"):
        del os.environ[name]


def load():
    spec = importlib.util.spec_from_file_location("radar_under_test", FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.time.sleep = lambda s: None          # never wait in tests
    module.FX["rate"] = 1.29
    module.FX["t"] = time.time()
    return module


def shop(*states, extra=None):
    """A fake product list. states = availability of KH1, KL2, KL1, KB1 of one deck."""
    names = ["32mm KH1", "32.5mm KL2", "34mm KL1", "34mm KB1"]
    variants = [{"id": 10 + i, "title": n, "price": "48.00", "available": s} for i, (n, s) in enumerate(zip(names, states))]
    variants.append({"id": 99, "title": "29mm K01", "price": "48.00", "available": True})   # not tracked
    products = [{"title": "Test Deck", "handle": "test-deck", "images": [{"src": "https://img/x.jpg"}], "variants": variants}]
    return products + (extra or [])


class RadarTests(unittest.TestCase):
    def setUp(self):
        for f in os.listdir(TMP):
            os.remove(os.path.join(TMP, f))
        self.r = load()
        self.sent = []
        self.fail_sends = 0

        def api(method, params=None, _retry=True):
            if method in ("sendMessage", "sendPhoto"):
                if self.fail_sends > 0:
                    self.fail_sends -= 1
                    return {"ok": False}
                self.sent.append((method, params or {}))
            return {"ok": True, "result": []}

        self.original_api = self.r.telegram_api
        self.r.telegram_api = api
        self.world = {"products": shop(False, False, False, False)}
        self.real_fetch_products = self.r.fetch_products
        self.r.fetch_products = lambda: self.world["products"]
        self.r.META["board"] = False                     # most checks are about the classic one alert per deck
        self.r.META["sizes_v67"] = True                  # most checks use all four sizes
        self.r.PARCEL_AUTO = True                        # most parcel checks are about automatic tracking
        self.r.META["my_sizes"] = list(self.r.SHAPES)
        self.r.META["news"] = False

    @staticmethod
    def size_buttons(markup_json):
        rows = json.loads(markup_json)["inline_keyboard"]
        return rows[0] + rows[1], rows[2]

    def alerts(self):
        return [p for m, p in self.sent if m == "sendPhoto" or "DECK IN STOCK" in p.get("text", "")]

    # ---- reading the shop ----
    def test_shapes_match_whole_words_only(self):
        e = self.r.extract_shape
        self.assertEqual(e("32mm KH1"), "KH1")
        self.assertEqual(e("34mm kb1"), "KB1")
        self.assertIsNone(e("34mm KL10"))
        self.assertIsNone(e("29mm K01"))

    def test_a_shop_with_its_decks_hidden_is_a_real_answer_not_an_outage(self):
        self.world["products"] = shop(True, False, False, False)
        self.r.check_stock()
        self.assertTrue(self.r.STATE["10"])
        merch = [{"title": "Shirt", "handle": "shirt", "variants": [{"id": 500, "title": "M", "available": True}]}]
        self.world["products"] = merch
        self.r.check_stock()                                  # must not raise
        self.assertFalse(self.r.STATE["10"])                  # hidden deck counts as sold out
        self.assertEqual(self.r.CURRENT_SNAPSHOT, {})
        self.assertTrue(self.r.FEED["empty"])
        text = self.r.status_text()
        self.assertIn("decks listed on the shop right now", text)
        self.assertNotIn("isn't answering", text)
        self.assertNotIn("in stock", text.split("decks listed")[0])
        self.sent.clear()
        self.world["products"] = shop(True, False, False, False)
        self.r.check_stock()                                  # back on the shop and buyable: alert
        self.assertFalse(self.r.FEED["empty"])
        self.assertTrue(self.r.STATE["10"])
        self.assertTrue(self.alerts())

    def test_a_deck_taken_off_the_shop_is_no_longer_shown_in_stock(self):
        other = [{"title": "Other Deck", "handle": "other", "variants": [{"id": 700, "title": "32mm KH1", "available": True}]}]
        self.world["products"] = shop(True, False, False, False, extra=other)
        self.r.check_stock()
        self.world["products"] = other
        self.r.check_stock()
        self.assertFalse(self.r.STATE["10"])
        self.assertTrue(self.r.STATE["700"])

    def test_daily_ping_says_no_decks_listed_when_the_shop_hides_them(self):
        self.r.FEED["empty"] = True
        self.r.CURRENT_SNAPSHOT = {}
        self.r.DAILY_PING_HOUR = self.r.datetime.now(self.r.timezone(self.r.timedelta(hours=self.r.TZ_OFFSET_HOURS))).hour
        self.r.META.pop("last_daily", None)
        self.r.maybe_daily_ping()
        self.assertIn("decks listed right now", self.sent[-1][1]["text"])

    def test_a_locked_shop_answering_401_reads_as_locked_not_as_an_outage(self):
        import io, urllib.error
        r = self.r
        page = b'<html><form action="/password"><input type="password" name="password"></form><p>DROP FRIDAY</p></html>'
        class Opener:
            def open(self, request, timeout=20):
                raise urllib.error.HTTPError(r.SITE + "/password", 401, "Unauthorized", {}, io.BytesIO(page))
        r.OPENER = Opener()
        with self.assertRaises(r.ShopLocked) as caught:
            r.fetch_products_once()
        self.assertIn("DROP FRIDAY", caught.exception.text)

    def _opener(self, routes):
        import io, urllib.error
        r = self.r
        calls = []
        class Resp:
            def __init__(self, body, url): self.body, self.url, self.headers = body, url, {}
            def read(self): return self.body
            def geturl(self): return self.url
            def __enter__(self): return self
            def __exit__(self, *a): return False
        class Opener:
            def open(self, request, timeout=20):
                url = request.full_url
                phone = "iPhone" in request.headers.get("User-agent", "")
                calls.append((url, phone))
                for (path, want_phone), (code, body) in routes.items():
                    if urllib.parse.urlparse(url).path == path and (want_phone is None or want_phone == phone):
                        if code == 200:
                            return Resp(body, url)
                        raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(body))
                raise urllib.error.HTTPError(url, 404, "x", {}, io.BytesIO(b""))
        import urllib.parse
        r.OPENER = Opener()
        r.fetch_products = self.real_fetch_products
        return calls

    def test_a_refused_feed_switches_to_another_way_of_reading_the_shop(self):
        r = self.r
        good = json.dumps({"products": shop(True, False, False, False)}).encode()
        self._opener({("/products.json", None): (401, b""), ("/collections/all/products.json", True): (200, good)})
        products = r.fetch_products()
        self.assertEqual(products[0]["title"], "Test Deck")
        self.assertEqual(r.FEED_SOURCE, {"path": "/collections/all/products.json", "browser": True})

    def test_refused_feed_with_an_open_front_page_tells_you_the_shop_is_open(self):
        r = self.r
        self._opener({("/products.json", None): (401, b""), ("/collections/all/products.json", None): (401, b""),
                      ("/", True): (200, b"<html>welcome</html>")})
        with self.assertRaises(r.FetchError) as caught:
            r.fetch_products()
        self.assertNotIsInstance(caught.exception, r.ShopLocked)
        self.assertIn("open but refuses", str(caught.exception))
        self.assertIn("The shop looks OPEN", self.sent[-1][1]["text"])
        self.assertEqual(r.describe_failure(caught.exception), "the shop is open but refusing my stock checks")
        n = len(self.sent)
        with self.assertRaises(r.FetchError):
            r.fetch_products()                            # told once, not every check
        self.assertEqual(len(self.sent), n)

    def test_refused_feed_with_a_password_front_page_is_locked(self):
        r = self.r
        page = b'<form action="/password"><input type="password"></form><p>DROP 8PM</p>'
        self._opener({("/products.json", None): (401, b""), ("/collections/all/products.json", None): (401, b""),
                      ("/", True): (401, page)})
        with self.assertRaises(r.ShopLocked) as caught:
            r.fetch_products()
        self.assertIn("DROP 8PM", caught.exception.text)

    def test_a_403_that_is_not_the_password_page_is_an_error_with_a_true_reason(self):
        r = self.r
        self._opener({("/products.json", None): (403, b"nope"), ("/collections/all/products.json", None): (403, b""),
                      ("/", None): (403, b"nope")})
        with self.assertRaises(r.FetchError) as caught:
            r.fetch_products()
        self.assertNotIsInstance(caught.exception, r.ShopLocked)
        self.assertIn("refusing my checks (403)", r.describe_failure(caught.exception))
        self.assertEqual(r.describe_failure(Exception("timed out")), "the shop isn't answering")

    def test_a_wrong_password_answered_with_401_is_reported_as_rejected(self):
        import io, urllib.error
        r = self.r
        page = b'<form><input type="password" name="password"></form>'
        class Opener:
            def open(self, request, timeout=20):
                raise urllib.error.HTTPError(r.SITE + "/password", 401, "Unauthorized", {}, io.BytesIO(page))
        r.OPENER = Opener()
        r.META["shop_password"] = "wrong"
        r.shop_password = lambda: "wrong"
        self.assertFalse(r.try_unlock())
        self.assertEqual(r.PW["bad"], "wrong")

    def test_a_password_written_on_the_password_page_is_found(self):
        f = self.r.find_page_password
        self.assertEqual(f("DROP TONIGHT · Password: knives13"), "knives13")
        self.assertEqual(f('the password is "FRIDAY"'), "FRIDAY")
        self.assertEqual(f("pw - Stolly99!"), "Stolly99")
        self.assertEqual(f("PASSWORD → crows"), "crows")
        self.assertIsNone(f("DROP TONIGHT 8PM. Password is in our IG story."))
        self.assertIsNone(f("Password coming soon"))
        self.assertIsNone(f("This shop will be powered by · Enter password"))
        self.assertIsNone(f(""))

    def test_a_password_on_the_page_is_shared_and_tried_once(self):
        r = self.r
        tried = []
        r.try_unlock = lambda quiet=False: tried.append(r.shop_password()) or False
        r.note_locked("Password: knives13 · drop at 8pm")
        self.assertEqual(tried, ["knives13"])
        found = [p for m, p in self.sent if "password" in p.get("text", "").lower() and "<code>" in p.get("text", "")]
        self.assertEqual(len(found), 1)
        self.assertIn("<code>knives13</code>", found[0]["text"])
        r.note_locked("Password: knives13 · drop at 8pm")         # same page again: not repeated
        self.assertEqual(tried, ["knives13"])
        self.assertTrue(r.fast_now())                              # found something: fast checks

    def test_the_first_password_that_works_is_used_and_shared(self):
        r = self.r
        tried = []
        def unlock(quiet=False):
            tried.append(r.shop_password())
            return r.shop_password() == "crows"
        r.try_unlock = unlock
        r.GATE["cands"] = ["crows"]
        r.note_locked("pw: Stolly99")
        self.assertEqual(tried, ["Stolly99", "crows"])
        self.assertEqual(r.shop_password(), "crows")
        msg = [p for m, p in self.sent if "Password found" in p.get("text", "")]
        self.assertIn("<code>crows</code>", msg[0]["text"])
        self.assertIn("It works", msg[0]["text"])

    def test_passwords_hidden_in_the_page_code_are_found(self):
        c = self.r.page_password_candidates
        self.assertEqual(c(b'<meta name="description" content="Password: hypno77">'), ["hypno77"])
        self.assertEqual(c(b'<p>The password is <strong>BLOODBATH</strong></p>'), ["BLOODBATH"])
        self.assertEqual(c(b'<!-- pw: jaymask --><p>Enter password</p>'), ["jaymask"])
        self.assertEqual(c(b'<img alt="password = tabloid" src="x.png">'), ["tabloid"])
        self.assertEqual(c(b'<p>Password is in our IG story</p><label>Enter password</label>'), [])

    def test_while_locked_only_the_front_page_is_read_each_check(self):
        r = self.r
        page = b'<form action="/password"><input type="password"></form><p>DROP 8PM</p>'
        calls = self._opener({("/products.json", None): (401, b""), ("/collections/all/products.json", None): (401, b""),
                              ("/", True): (401, page)})
        with self.assertRaises(r.ShopLocked):
            r.fetch_products()
        calls.clear()
        with self.assertRaises(r.ShopLocked):
            r.fetch_products()
        self.assertEqual([urllib.parse.urlparse(u).path for u, _ in calls], ["/"])

    def test_drop_times_on_the_page_are_read_in_us_time(self):
        r = self.r
        from datetime import datetime, timezone
        now = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc).timestamp()      # Tue 06:00 in Chicago
        def sg(ts):
            return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M")
        self.assertEqual(sg(r.find_drop_time("DROP TONIGHT 8PM · CURRENT CHICAGO TIME", now)), "2026-11-18 02:00")
        self.assertEqual(sg(r.find_drop_time("Friday 7:30pm ET", now)), "2026-11-21 00:30")
        self.assertEqual(sg(r.find_drop_time("11/21 6 PM PT", now)), "2026-11-22 02:00")
        self.assertEqual(sg(r.find_drop_time("Restock 20:00 CST", now)), "2026-11-18 02:00")
        self.assertEqual(sg(r.find_drop_time("drop at 5am", now)), "2026-11-18 11:00")      # 5am already gone: tomorrow
        summer = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(sg(r.find_drop_time("8PM CST", summer)), "2026-07-02 01:00")      # summer time: UTC-5
        self.assertIsNone(r.find_drop_time("32mm KH1 · 34mm KB1 · money back friends", now))
        self.assertIsNone(r.find_drop_time("Enter password", now))
        self.assertIsNone(r.find_drop_time("CURRENT CHICAGO TIME 6:01 AM", now))   # the page's clock
        self.assertIsNone(r.find_drop_time("", now))

    def test_a_drop_time_is_announced_once_and_rings_5_minutes_before(self):
        r = self.r
        r.ALARM_ASYNC = False
        r.BARK_KEY = "k"
        pushes = []
        r.bark_push = lambda title, body, url="": pushes.append(title) or "success"
        r.META["alarm"] = {"on": True, "hours": ""}
        r.find_drop_time = lambda text, now=None: time.time() + 200
        self.assertTrue(r.note_drop_time("DROP 8PM"))
        self.assertIn("Drop time spotted", self.sent[-1][1]["text"])
        n = len(self.sent)
        self.assertFalse(r.note_drop_time("DROP 8PM"))                     # same time: not repeated
        self.assertEqual(len(self.sent), n)
        r.maybe_drop_countdown()
        self.assertEqual(pushes, ["Knife Mfg Co: DROP IN 5 MIN"])
        r.maybe_drop_countdown()
        self.assertEqual(len(pushes), 1)                                   # rings once
        self.assertIn("⏰ drop", r.drop_countdown_line())

    def test_wake_ring_only_at_night_when_away_and_not_too_often(self):
        r = self.r
        r.ALARM_ASYNC = False
        r.BARK_KEY = "k"
        pushes = []
        r.bark_push = lambda title, body, url="": pushes.append(title) or "success"
        r.META["alarm"] = {"on": False, "hours": ""}
        self.assertFalse(r.wake_ring("A", "b"))                            # alarm off
        r.META["alarm"] = {"on": True, "hours": ""}
        r.AWAKE["t"] = time.time()
        self.assertFalse(r.wake_ring("A", "b"))                            # you're awake
        r.AWAKE["t"] = 0
        self.assertTrue(r.wake_ring("A", "b"))
        self.assertFalse(r.wake_ring("B", "b"))                            # not twice in 20 minutes
        self.assertEqual(pushes, ["A"])

    def test_a_password_found_at_night_rings_bark(self):
        r = self.r
        r.ALARM_ASYNC = False
        r.BARK_KEY = "k"
        pushes = []
        r.bark_push = lambda title, body, url="": pushes.append((title, body)) or "success"
        r.META["alarm"] = {"on": True, "hours": ""}
        r.try_unlock = lambda quiet=False: True
        r.note_locked("Password: knives13")
        self.assertEqual(pushes[0][0], "Knife Mfg Co: PASSWORD")
        self.assertIn("knives13", pushes[0][1])

    def test_gzip_responses_are_read(self):
        class Resp:
            headers = {"Content-Encoding": "gzip"}
            def read(self): return gzip.compress(b'{"ok": 1}')
        self.assertEqual(self.r._read(Resp()), b'{"ok": 1}')

    def test_prices_show_as_sgd(self):
        self.assertEqual(self.r.money("48.00"), "~S$62")
        self.assertEqual(self.r.money(""), "")

    def test_a_busy_shop_gets_one_quick_second_try(self):
        import urllib.error
        replies = [503, "ok"]
        def page(url, timeout=20):
            reply = replies.pop(0)
            if reply == "ok":
                return b'{"products": []}', url
            raise urllib.error.HTTPError(url, reply, "busy", {}, None)
        self.r.fetch_page = page
        self.assertEqual(self.r.fetch_page_retry("https://x/products.json")[0], b'{"products": []}')
        self.assertEqual(replies, [])

    def test_a_real_error_is_not_retried(self):
        import urllib.error
        tries = []
        def page(url, timeout=20):
            tries.append(1)
            raise urllib.error.HTTPError(url, 404, "missing", {}, None)
        self.r.fetch_page = page
        with self.assertRaises(urllib.error.HTTPError):
            self.r.fetch_page_retry("https://x/products.json")
        self.assertEqual(len(tries), 1)

    def test_a_shop_that_stays_down_gives_up_after_two_tries(self):
        import urllib.error
        tries = []
        def page(url, timeout=20):
            tries.append(1)
            raise urllib.error.HTTPError(url, 503, "busy", {}, None)
        self.r.fetch_page = page
        with self.assertRaises(urllib.error.HTTPError):
            self.r.fetch_page_retry("https://x/products.json")
        self.assertEqual(len(tries), 2)

    def test_too_many_requests_is_never_retried_straight_away(self):
        import urllib.error
        tries = []
        def page(url, timeout=20):
            tries.append(1)
            raise urllib.error.HTTPError(url, 429, "slow down", {"Retry-After": "30"}, None)
        self.r.fetch_page = page
        with self.assertRaises(self.r.RateLimited) as caught:
            self.r.fetch_page_retry("https://x/products.json")
        self.assertEqual(len(tries), 1)
        self.assertEqual(caught.exception.retry_after, 30.0)

    def test_a_429_makes_the_radar_slow_down_and_later_recover(self):
        base = self.r.CHECK_INTERVAL
        self.assertEqual(self.r.current_interval(), base)
        self.r.note_rate_limited()
        self.r.note_rate_limited()
        self.assertEqual(self.r.current_interval(), base + 8.0)
        self.assertFalse(self.r.PACE["buster"])
        self.r.PACE["last_limit"] = time.time() - 1900       # 30 calm minutes later
        for _ in range(5):
            self.r.calm_down()
            self.r.PACE["last_limit"] = time.time() - 1900
        self.assertEqual(self.r.current_interval(), self.r.CHECK_INTERVAL)
        self.assertTrue(self.r.PACE["buster"])

    def test_the_slow_down_is_capped(self):
        for _ in range(20):
            self.r.note_rate_limited()
        self.assertEqual(self.r.current_interval(), self.r.CHECK_INTERVAL + 40.0)

    # ---- alerts ----
    def test_restock_sends_one_alert_per_deck_with_buttons(self):
        self.r.check_stock()                                    # baseline
        self.world["products"] = shop(True, False, True, False)
        self.r.check_stock()
        alerts = self.alerts()
        self.assertEqual(len(alerts), 1)
        sizes, _ = self.size_buttons(alerts[0]["reply_markup"])
        self.assertEqual([b["text"] for b in sizes],
                         ["⚡ KH1 · 32mm", "✖ KL2 · 32.5mm", "⚡ KL1 · 34mm", "✖ KB1 · 34mm"])

    def test_buttons_are_always_the_same_four_in_the_same_order(self):
        self.r.check_stock()
        self.world["products"] = shop(False, False, False, True)
        self.r.check_stock()
        sizes, links = self.size_buttons(self.alerts()[0]["reply_markup"])
        self.assertEqual([b["text"] for b in sizes],
                         ["✖ KH1 · 32mm", "✖ KL2 · 32.5mm", "✖ KL1 · 34mm", "⚡ KB1 · 34mm"])
        self.assertIn("/cart/13:1", sizes[3]["url"])                 # KB1 is variant 13
        self.assertEqual(sizes[0]["callback_data"], "so:KH1")        # sold out: tap shows a note
        self.assertEqual([b["text"] for b in links], ["🔗 Open page", "🛒 Cart page"])
        self.assertIn("storefront=true", links[1]["url"])

    def test_a_size_that_stayed_in_stock_still_shows_as_available(self):
        self.r.check_stock()
        self.world["products"] = shop(True, False, False, False)
        self.r.check_stock()
        self.world["products"] = shop(True, False, True, False)      # KL1 restocks, KH1 still there
        self.sent.clear()
        self.r.check_stock()
        sizes, _ = self.size_buttons(self.alerts()[0]["reply_markup"])
        self.assertEqual([b["text"][:1] for b in sizes], ["⚡", "✖", "⚡", "✖"])

    def test_tapping_a_sold_out_button_explains_itself(self):
        answers = []
        self.r.telegram_api = lambda m, p=None, _retry=True: (answers.append((m, p)) or {"ok": True, "result": []})
        self.r.handle_callback({"id": "q1", "data": "so:KL2", "message": {"chat": {"id": 42}, "message_id": 5}})
        self.assertEqual(answers[0][0], "answerCallbackQuery")
        self.assertIn("KL2 is sold out", answers[0][1]["text"])

    def test_no_repeat_alerts(self):
        self.r.check_stock()
        self.world["products"] = shop(True, False, False, False)
        self.r.check_stock()
        self.r.check_stock()
        self.assertEqual(len(self.alerts()), 1)

    def test_untracked_size_never_alerts(self):
        self.r.check_stock()
        self.r.check_stock()
        self.assertEqual(self.alerts(), [])

    def test_an_alert_is_not_lost_when_telegram_fails(self):
        self.r.check_stock()
        self.world["products"] = shop(True, False, False, False)
        self.fail_sends = 99                                     # Telegram is down
        self.r.check_stock()
        self.assertEqual(self.alerts(), [])
        self.assertFalse(self.r.STATE.get("10", False))          # NOT marked as seen
        self.fail_sends = 0                                      # Telegram is back
        self.r.check_stock()
        self.assertEqual(len(self.alerts()), 1)
        self.assertTrue(self.r.STATE["10"])

    def test_a_short_telegram_hiccup_is_retried_within_one_check(self):
        self.r.check_stock()
        self.world["products"] = shop(True, False, False, False)
        self.fail_sends = 2
        self.r.check_stock()
        self.assertEqual(len(self.alerts()), 1)

    def test_decks_already_in_stock_at_start_are_alerted_when_memory_is_empty(self):
        self.world["products"] = shop(True, False, False, False)
        self.r.check_stock()
        self.assertEqual(len(self.alerts()), 1)

    def test_alert_on_start_can_be_turned_off(self):
        self.r.ALERT_ON_START = False
        self.world["products"] = shop(True, False, False, False)
        self.r.check_stock()
        self.assertEqual(self.alerts(), [])

    def test_a_failed_check_keeps_the_old_data(self):
        self.r.check_stock()
        before = dict(self.r.STATE)
        def boom(): raise self.r.FetchError("503")
        self.r.fetch_products = boom
        with self.assertRaises(self.r.FetchError):
            self.r.check_stock()
        self.assertEqual(self.r.STATE, before)

    def test_test_command_sends_a_sample_when_nothing_is_in_stock(self):
        self.r.check_stock()
        self.sent.clear()
        self.r.send_test_alert()
        photos = [p for m, p in self.sent if m == "sendPhoto"]
        self.assertEqual(len(photos), 1)
        sizes, _ = self.size_buttons(photos[0]["reply_markup"])
        self.assertEqual([b["text"] for b in sizes],
                         ["⚡ KH1 · 32mm", "⚡ KL2 · 32.5mm", "✖ KL1 · 34mm", "✖ KB1 · 34mm"])

    def test_refresh_button_when_nothing_changed_is_not_an_error(self):
        calls = []
        def api(method, params=None, _retry=True):
            calls.append((method, params or {}))
            if method == "editMessageText":
                return {"ok": False, "error": "HTTP 400",
                        "description": "Bad Request: message is not modified"}
            return {"ok": True, "result": []}
        self.r.telegram_api = api
        self.r.handle_callback({"id": "q9", "data": "refresh", "message": {"chat": {"id": 42}, "message_id": 5}})
        toast = [p for m, p in calls if m == "answerCallbackQuery"][0]["text"]
        self.assertIn("Already up to date", toast)

    def test_refresh_button_updates_the_message(self):
        calls = []
        self.r.telegram_api = lambda m, p=None, _retry=True: (calls.append((m, p or {})) or {"ok": True, "result": []})
        self.r.check_stock()
        self.r.handle_callback({"id": "q8", "data": "refresh", "message": {"chat": {"id": 42}, "message_id": 5}})
        self.assertEqual([m for m, p in calls][-2:], ["editMessageText", "answerCallbackQuery"])
        self.assertIn("Refreshed", calls[-1][1]["text"])

    def test_status_explains_why_when_the_shop_cannot_be_read(self):
        self.r.HEALTH["error"] = "the shop is limiting my checks (429)"
        text = self.r.status_text()
        self.assertIn("can't read the shop yet", text)
        self.assertIn("429", text)
        self.assertNotIn("warming up", text)

    def test_status_says_warming_up_only_when_nothing_is_wrong(self):
        self.r.HEALTH["error"] = ""
        self.assertIn("warming up", self.r.status_text())

    def test_status_shows_the_real_reason_when_the_data_is_old(self):
        self.world["products"] = shop(False, False, False, False)
        self.r.check_stock()
        self.r.LAST_CHECK = time.time() - 240
        self.r.HEALTH["error"] = "the shop is limiting my checks (429)"
        text = self.r.status_text()
        self.assertIn("limiting my checks (429) · data is 4m old", text)
        self.assertNotIn("store unreachable", text)

    def test_headings_are_plain_bold_by_default(self):
        self.assertEqual(self.r.render("<h>DECK IN STOCK</h> x"), "<b>DECK IN STOCK</b> x")

    def test_sans_style_changes_only_headings(self):
        self.r.FONT_STYLE = "sans"
        out = self.r.render("<h>Deck 1</h> and <b>Receiver - Black</b> &amp; more")
        self.assertIn("<b>𝗗𝗲𝗰𝗸 𝟭</b>", out)
        self.assertIn("<b>Receiver - Black</b> &amp; more", out)      # names stay normal text

    def test_mono_style_and_untouched_symbols(self):
        self.r.FONT_STYLE = "mono"
        self.assertEqual(self.r.fancy("KH1 · 32mm!"), "𝙺𝙷𝟷 · 𝟹𝟸𝚖𝚖!")

    def test_unknown_style_falls_back_to_plain(self):
        self.r.FONT_STYLE = "wobbly"
        self.assertEqual(self.r.fancy("Hello"), "Hello")

    def test_alert_heading_uses_the_chosen_style_when_sent(self):
        self.r.FONT_STYLE = "sans"
        self.r.check_stock()
        self.world["products"] = shop(True, False, False, False)
        self.r.check_stock()
        caption = [p for m, p in self.sent if m == "sendPhoto"][0]["caption"]
        self.assertIn("<b>𝗗𝗘𝗖𝗞 𝗜𝗡 𝗦𝗧𝗢𝗖𝗞</b>", caption)
        self.assertIn("Test Deck", caption)                            # deck name untouched

    def test_every_font_style_works_and_keeps_symbols(self):
        styles = ["sans", "sanslight", "sansitalic", "serif", "serifitalic", "script", "gothic",
                  "double", "mono", "wide", "smallcaps", "smallcapsall", "spaced"]
        for style in styles:
            self.r.FONT_STYLE = style
            out = self.r.fancy("Deck 7 · IN-STOCK!")
            self.assertNotEqual(out, "Deck 7 · IN-STOCK!", style)
            for symbol in ("·", "-", "!"):
                self.assertIn(symbol, out, style)
            self.assertIn("<b>", self.r.render("<h>Deck</h>"), style)

    def test_small_caps_and_spaced(self):
        self.r.FONT_STYLE = "smallcaps"
        self.assertEqual(self.r.fancy("Deck in stock"), "Dᴇᴄᴋ ɪɴ ꜱᴛᴏᴄᴋ")
        self.assertEqual(self.r.fancy("KNIFE MFG CO · deck radar"), "KNIFE MFG CO · ᴅᴇᴄᴋ ʀᴀᴅᴀʀ")
        self.r.FONT_STYLE = "smallcapsall"
        self.assertEqual(self.r.fancy("Deck in stock"), "ᴅᴇᴄᴋ ɪɴ ꜱᴛᴏᴄᴋ")
        self.r.FONT_STYLE = "spaced"
        self.assertEqual(self.r.fancy("KH1 ok"), "K\u200aH\u200a1 \u00a0o\u200ak")

    def test_double_struck_and_serif_italic_exceptions(self):
        self.r.FONT_STYLE = "double"
        self.assertEqual(self.r.fancy("CHN"), "ℂℍℕ")
        self.r.FONT_STYLE = "serifitalic"
        self.assertEqual(self.r.fancy("h"), "ℎ")

    def test_buttons_follow_the_font_style(self):
        self.r.FONT_STYLE = "sans"
        self.r.check_stock()
        self.world["products"] = shop(True, False, False, False)
        self.r.check_stock()
        markup = json.loads([p for m, p in self.sent if m == "sendPhoto"][0]["reply_markup"])["inline_keyboard"]
        self.assertEqual(markup[0][0]["text"], "⚡ 𝗞𝗛𝟭 · 𝟯𝟮𝗺𝗺")
        self.assertEqual(markup[2][0]["text"], "🔗 𝗢𝗽𝗲𝗻 𝗽𝗮𝗴𝗲")
        self.assertEqual(self.r.status_markup()["inline_keyboard"][0][0]["text"], "🔄 𝗥𝗲𝗳𝗿𝗲𝘀𝗵")

    def test_buttons_can_have_their_own_style(self):
        self.r.FONT_STYLE = "sans"
        self.r.BUTTON_STYLE = "smallcaps"
        self.assertEqual(self.r.btn("Open shop"), "Oᴘᴇɴ ꜱʜᴏᴘ")
        self.assertEqual(self.r.render("<h>Deck</h>"), "<b>𝗗𝗲𝗰𝗸</b>")

    def test_buttons_stay_plain_by_default(self):
        self.assertEqual(self.r.btn("🛒 Open shop"), "🛒 Open shop")

    def test_the_shop_is_called_knife_mfg_co(self):
        self.assertIn("KNIFE MFG CO", self.r.status_text())
        self.assertIn("KNIFE MFG CO", self.r.help_text())

    def test_not_modified_is_recognised_from_the_real_long_telegram_reply(self):
        import io, contextlib, urllib.error, urllib.request
        body = (b'{"ok":false,"error_code":400,"description":"Bad Request: message is not modified: '
                b'specified new message content and reply markup are exactly the same as a current '
                b'content and reply markup of the message"}')
        assert len(body) > 200                     # longer than the old cut-off that broke the check
        def fake_open(request, timeout=0):
            raise urllib.error.HTTPError("u", 400, "Bad Request", {}, io.BytesIO(body))
        real = urllib.request.urlopen
        urllib.request.urlopen = fake_open
        out = io.StringIO()
        try:
            self.r.telegram_api = self.original_api
            with contextlib.redirect_stdout(out):
                result = self.r.telegram_api("editMessageText", {"chat_id": "42"})
        finally:
            urllib.request.urlopen = real
        self.assertIn("not modified", result["description"])
        self.assertEqual(out.getvalue(), "")        # nothing logged: it is harmless

    # ---- calm mode and drop mode ----
    def test_drop_mode_is_fast_and_calm_mode_is_slow(self):
        self.assertFalse(self.r.fast_now())
        self.assertEqual(self.r.base_interval(), self.r.CHECK_INTERVAL)
        self.r.start_drop(10)
        self.assertTrue(self.r.fast_now())
        self.assertEqual(self.r.base_interval(), self.r.DROP_INTERVAL)
        self.r.end_drop()
        self.assertFalse(self.r.fast_now())

    def test_drop_mode_ends_by_itself(self):
        self.r.start_drop(10)
        self.r.DROP["until"] = time.time() - 1
        self.assertFalse(self.r.fast_now())

    def test_drop_mode_only_extends_never_shortens(self):
        self.r.start_drop(60)
        long_end = self.r.DROP["until"]
        self.r.start_drop(5)
        self.assertGreaterEqual(self.r.DROP["until"], long_end)

    def test_a_restock_alert_switches_drop_mode_on(self):
        self.r.check_stock()
        self.assertFalse(self.r.fast_now())
        self.world["products"] = shop(True, False, False, False)
        self.r.check_stock()
        self.assertTrue(self.r.fast_now())

    def test_cache_buster_is_only_used_in_drop_mode(self):
        seen = []
        def page(url, timeout=20):
            seen.append(url)
            return b'{"products": [{"title": "x", "handle": "x", "variants": []}]}', url
        self.r.fetch_page = page
        self.r.fetch_products_once()
        self.r.start_drop(10)
        self.r.fetch_products_once()
        self.assertNotIn("&_=", seen[0])
        self.assertIn("&_=", seen[1])

    def test_drop_command_variants(self):
        self.r.handle_drop("")
        self.assertTrue(self.r.fast_now())
        self.r.handle_drop("off")
        self.assertFalse(self.r.fast_now())
        self.r.handle_drop("45")
        self.assertAlmostEqual(self.r.DROP["until"] - self.r.DROP["from"], 45 * 60, delta=2)
        self.r.handle_drop("off")
        self.sent.clear()
        self.r.handle_drop("banana")
        self.assertIn("Try", self.sent[0][1]["text"])
        self.assertFalse(self.r.fast_now())
        self.sent.clear()
        self.r.handle_drop("25:99")
        self.assertIn("couldn't read", self.sent[0][1]["text"])

    def test_scheduled_drop_window_surrounds_the_time(self):
        begin, end = self.r.schedule_for("20:00")
        self.assertEqual(end - begin, 70 * 60)
        self.assertIsNone(self.r.schedule_for("nonsense"))

    def test_status_and_info_show_the_mode(self):
        self.world["products"] = shop(False, False, False, False)
        self.r.check_stock()
        self.assertIn("calm mode", self.r.status_text())
        self.r.start_drop(10)
        self.assertIn("drop mode", self.r.status_text())
        self.assertIn("version", self.r.info_text())

    # ---- drop mode goes back to calm by itself ----
    def test_the_quiet_time_before_calm_is_an_hour_by_default(self):
        self.assertEqual(self.r.DROP_IDLE_MINUTES, 60.0)

    def test_a_sticky_drop_mode_stays_on_while_things_happen(self):
        self.r.start_drop(self.r.DROP_IDLE_MINUTES, sticky=True)
        self.r.DROP["until"] = time.time() + 30                    # about to run out
        self.r.touch_activity()
        self.assertGreater(self.r.DROP["until"], time.time() + 3000)
        self.assertIn("if quiet", self.r.mode_line())

    def test_a_fixed_drop_window_is_not_stretched_by_activity(self):
        self.r.start_drop(10)                                      # /drop 10: exactly ten minutes
        end = self.r.DROP["until"]
        self.r.touch_activity()
        self.assertEqual(self.r.DROP["until"], end)

    def test_after_an_hour_of_quiet_it_goes_back_to_calm_and_tells_you_once(self):
        self.r.start_drop(self.r.DROP_IDLE_MINUTES, sticky=True)
        self.r.check_drop_end()                                    # notices it is on
        self.sent.clear()
        self.r.DROP["until"] = time.time() - 1                     # an hour with nothing happening
        self.assertFalse(self.r.fast_now())
        self.r.check_drop_end()
        self.r.check_drop_end()
        calm = [x for x in self.texts() if "Back to calm mode" in x]
        self.assertEqual(len(calm), 1)
        self.assertIn("No activity for 60 min", calm[0])

    def test_switching_it_off_yourself_does_not_send_the_ended_message(self):
        self.r.start_drop(self.r.DROP_IDLE_MINUTES, sticky=True)
        self.r.check_drop_end()
        self.sent.clear()
        self.r.handle_drop("off")
        self.r.check_drop_end()
        self.assertFalse(any("No activity" in x for x in self.texts()))

    def test_using_the_bot_counts_as_activity(self):
        self.r.start_drop(self.r.DROP_IDLE_MINUTES, sticky=True)
        self.r.DROP["until"] = time.time() + 30
        self.r.telegram_api = lambda m, p=None, _retry=True: {"ok": True, "result": []}
        self.r.handle_callback({"id": "q", "data": "so:KL2", "message": {"chat": {"id": 42}, "message_id": 1}})
        self.assertGreater(self.r.DROP["until"], time.time() + 3000)

    def test_stock_moving_counts_as_activity(self):
        self.r.check_stock()
        self.world["products"] = shop(True, False, False, False)
        self.r.check_stock()                                       # restock: drop mode on
        self.r.DROP["until"] = time.time() + 5
        self.world["products"] = shop(False, False, False, False)
        self.r.check_stock()                                       # it sold out again
        self.assertGreater(self.r.DROP["until"], time.time() + 3000)

    def test_a_restart_remembers_a_running_drop_mode(self):
        self.r.start_drop(self.r.DROP_IDLE_MINUTES, sticky=True)
        saved = json.load(open(self.r.META_FILE))
        self.assertTrue(saved["drop_sticky"])
        self.assertGreater(saved["drop_until"], time.time())

    # ---- drop intelligence ----
    def test_durations_bars_and_heat(self):
        self.assertEqual(self.r.human(41), "41s")
        self.assertEqual(self.r.human(130), "2m 10s")
        self.assertEqual(self.r.human(3900), "1h 05m")
        self.assertEqual(self.r.speed_bar(10), "▇▇▇▇▇▇▇▇")
        self.assertEqual(self.r.speed_bar(None), "▁▁▁▁▁▁▁▁")
        self.assertEqual(self.r.speed_bar(200), "▇▇▇▇▇▁▁▁")
        self.assertIn("INSANE", self.r.heat_label(41))
        self.assertIn("HOT", self.r.heat_label(120))
        self.assertIn("WARM", self.r.heat_label(900))
        self.assertIn("CHILL", self.r.heat_label(None))

    def test_a_restock_alert_is_remembered_as_a_drop(self):
        self.r.check_stock()
        self.world["products"] = shop(True, False, True, False)
        self.r.check_stock()
        event = self.r.META["events"][0]
        self.assertEqual(event["product"], "Test Deck")
        self.assertEqual({e["shape"] for e in event["items"].values()}, {"KH1", "KL1"})
        self.assertTrue(all(e["gone"] is None for e in event["items"].values()))

    def test_test_alerts_are_never_remembered_as_drops(self):
        self.world["products"] = shop(True, False, False, False)
        self.r.check_stock()
        self.sent.clear()
        before = len(self.r.META["events"])
        self.r.send_test_alert()
        self.assertEqual(len(self.r.META["events"]), before)

    def test_how_fast_each_size_sold_out_is_measured_and_reported_once(self):
        self.r.check_stock()
        self.world["products"] = shop(True, False, True, False)
        self.r.check_stock()
        event = self.r.META["events"][0]
        event["t"] -= 41                                              # the drop started 41 seconds ago
        self.world["products"] = shop(False, False, True, False)      # KH1 sold out
        self.r.check_stock()
        gone = {e["shape"]: e["gone"] for e in event["items"].values()}
        self.assertAlmostEqual(gone["KH1"], 41, delta=3)
        self.assertIsNone(gone["KL1"])
        self.sent.clear()
        self.r.maybe_drop_reports()
        self.assertEqual(self.texts(), [])                            # KL1 is still live: no report yet
        event["t"] -= 100
        self.world["products"] = shop(False, False, False, False)     # KL1 sold out too
        self.r.check_stock()
        self.r.maybe_drop_reports()
        self.r.maybe_drop_reports()
        reports = [x for x in self.texts() if "DROP REPORT" in x]
        self.assertEqual(len(reports), 1)
        self.assertIn("gone in 41s", reports[0])
        self.assertIn("Test Deck", reports[0])
        self.assertIn("INSANE", reports[0])                           # under a minute

    def test_a_drop_with_something_still_live_is_reported_after_an_hour(self):
        self.r.check_stock()
        self.world["products"] = shop(True, False, False, False)
        self.r.check_stock()
        self.sent.clear()
        self.r.META["events"][0]["t"] -= 3700
        self.r.maybe_drop_reports()
        report = [x for x in self.texts() if "DROP REPORT" in x][0]
        self.assertIn("still live", report)
        self.assertIn("CHILL", report)

    def test_history_lists_past_drops(self):
        self.assertIn("No drops yet", self.r.history_text())
        self.r.check_stock()
        self.world["products"] = shop(True, False, False, False)
        self.r.check_stock()
        text = self.r.history_text()
        self.assertIn("Test Deck", text)
        self.assertIn("1 drop remembered", text)

    def repeat_drops(self, count, days_apart=7):
        """Drops that all happened at this same weekday and hour, a week apart."""
        now = time.time()
        self.r.META["events"] = [
            {"t": now - 3600 * 24 * days_apart * k - 5, "product": "Deck", "handle": "d", "reported": True,
             "items": {str(k): {"shape": "KH1", "size": "32mm", "gone": 30.0}}}
            for k in range(1, count + 1)
        ]

    def test_the_forecast_learns_when_drops_usually_happen(self):
        self.assertIn("Still learning", self.r.forecast_text())
        self.repeat_drops(4)
        text = self.r.forecast_text()
        self.assertIn("learned from 4 drops", text)
        self.assertIn("4 drops (100%)", text)
        self.assertIn("next likely", text)
        self.assertEqual(len(self.r.smart_slots()), 1)

    def test_smart_drop_wakes_up_by_itself_inside_the_usual_window_and_only_once(self):
        self.repeat_drops(4)
        self.assertFalse(self.r.fast_now())
        self.r.maybe_smart_drop()
        self.assertTrue(self.r.fast_now())
        self.assertEqual(sum("SMART DROP" in x for x in self.texts()), 1)
        self.r.end_drop()
        self.sent.clear()
        self.r.maybe_smart_drop()                                    # same window: does not repeat
        self.assertEqual(self.texts(), [])

    def test_smart_drop_needs_enough_history_and_can_be_switched_off(self):
        self.repeat_drops(2)
        self.r.maybe_smart_drop()
        self.assertFalse(self.r.fast_now())
        self.repeat_drops(4)
        self.r.handle_smart("off")
        self.r.maybe_smart_drop()
        self.assertFalse(self.r.fast_now())
        self.r.handle_smart("on")
        self.r.maybe_smart_drop()
        self.assertTrue(self.r.fast_now())

    def test_only_the_latest_drops_are_kept(self):
        for i in range(self.r.EVENT_LIMIT + 20):
            self.r.record_restock([{"id": str(i), "product": "P", "handle": "h", "shape": "KH1", "size": "32mm"}])
        self.assertEqual(len(self.r.META["events"]), self.r.EVENT_LIMIT)

    # ---- live radar page ----
    def init_data(self, user_id=42, age=0, token="t", tamper=False):
        import hashlib, hmac, urllib.parse
        fields = {"auth_date": str(int(time.time()) - age), "query_id": "AAH123",
                  "user": json.dumps({"id": user_id, "first_name": "E"})}
        check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if tamper:
            fields["user"] = json.dumps({"id": 999, "first_name": "Evil"})
        return urllib.parse.urlencode(fields)

    def serve(self):
        import threading
        from http.server import ThreadingHTTPServer
        server = ThreadingHTTPServer(("127.0.0.1", 0), self.r.RadarHandler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def http(self, base, path, init=None, method="GET", body=None):
        import urllib.request, urllib.error
        headers = {}
        if init is not None:
            headers["X-Init-Data"] = init
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8")

    def test_only_genuine_telegram_signatures_are_accepted(self):
        good = self.r.verify_init_data(self.init_data())
        self.assertEqual(good["id"], 42)
        self.assertIsNone(self.r.verify_init_data(self.init_data(tamper=True)))      # changed after signing
        self.assertIsNone(self.r.verify_init_data(self.init_data(token="other")))    # signed with another bot's token
        self.assertIsNone(self.r.verify_init_data(self.init_data(age=3 * 86400)))    # too old
        self.assertIsNone(self.r.verify_init_data(""))
        self.assertIsNone(self.r.verify_init_data("hash=abc"))

    def test_the_page_itself_needs_no_login_but_holds_no_data(self):
        base = self.serve()
        code, page = self.http(base, "/")
        self.assertEqual(code, 200)
        self.assertIn("KNIFE MFG CO", page)
        self.assertIn("telegram-web-app.js", page)
        self.assertNotIn("innerHTML", page)                 # everything is inserted as plain text
        self.assertEqual(self.http(base, "/health")[0], 200)
        self.assertEqual(self.http(base, "/nothing")[0], 404)

    def test_the_data_is_only_for_you(self):
        base = self.serve()
        self.assertEqual(self.http(base, "/api/state")[0], 401)                              # no signature
        self.assertEqual(self.http(base, "/api/state", self.init_data(tamper=True))[0], 401)
        self.assertEqual(self.http(base, "/api/state", self.init_data(user_id=7))[0], 403)   # genuine, but someone else
        code, body = self.http(base, "/api/state", self.init_data())
        self.assertEqual(code, 200)
        self.assertEqual([t["shape"] for t in json.loads(body)["tiles"]], ["KH1", "KL2", "KL1", "KB1"])

    def test_the_page_data_shows_stock_buy_links_and_history(self):
        base = self.serve()
        self.r.check_stock()
        self.world["products"] = shop(True, False, True, False)
        self.r.check_stock()
        state = json.loads(self.http(base, "/api/state", self.init_data())[1])
        kh1, kl2 = state["tiles"][0], state["tiles"][1]
        self.assertEqual((kh1["count"], kl2["count"]), (1, 0))
        self.assertIn("/cart/10:1", kh1["live"][0]["buy"])
        self.assertEqual(kh1["live"][0]["sgd"], "~S$62")
        self.assertTrue(state["mode"]["fast"])                        # the restock switched drop mode on
        self.assertEqual(state["last_event"]["product"], "Test Deck")
        self.r.handle_bought_command("All Over 2")
        self.r.TRACK17_KEY = ""
        self.r.META["parcels"] = [{"number": "X1234567890", "label": "All Over 2", "status": "InTransit",
                                   "event": "Departed", "delivered": False}]
        state = json.loads(self.http(base, "/api/state", self.init_data())[1])
        self.assertEqual(state["pickups"], 1)
        self.assertEqual(state["parcels"][0]["label"], "All Over 2")

    def test_the_page_can_switch_drop_mode_on_and_off(self):
        base = self.serve()
        self.assertFalse(self.r.fast_now())
        code, body = self.http(base, "/api/drop", self.init_data(), "POST", {"on": True})
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["mode"]["fast"])
        self.assertTrue(self.r.fast_now())
        code, body = self.http(base, "/api/drop", self.init_data(), "POST", {"on": False})
        self.assertFalse(json.loads(body)["mode"]["fast"])
        self.assertFalse(self.r.fast_now())

    def test_switching_modes_needs_your_signature_and_valid_data(self):
        base = self.serve()
        self.assertEqual(self.http(base, "/api/drop", None, "POST", {"on": True})[0], 401)
        self.assertEqual(self.http(base, "/api/drop", self.init_data(user_id=7), "POST", {"on": True})[0], 403)
        self.assertEqual(self.http(base, "/api/drop", self.init_data(), "POST", b"{nope")[0], 400)
        self.assertEqual(self.http(base, "/other", self.init_data(), "POST", {})[0], 404)
        self.assertFalse(self.r.fast_now())

    def test_the_page_script_is_valid_javascript(self):
        import re, shutil, subprocess, tempfile, unittest
        if not shutil.which("node"):
            raise unittest.SkipTest("node is not installed")
        script = re.findall(r"<script>(.*?)</script>", self.r.DASHBOARD_HTML, re.S)[0]
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(script)
        result = subprocess.run(["node", "--check", f.name], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_radar_address_comes_from_railway_and_must_be_https(self):
        import os
        for name in ("PUBLIC_URL", "RAILWAY_PUBLIC_DOMAIN"):
            os.environ.pop(name, None)
        self.assertEqual(self.r.public_url(), "")
        os.environ["RAILWAY_PUBLIC_DOMAIN"] = "knife.up.railway.app"
        try:
            self.assertEqual(self.r.public_url(), "https://knife.up.railway.app")
            os.environ["PUBLIC_URL"] = "http://insecure.example"
            self.assertEqual(self.r.public_url(), "")
            os.environ["PUBLIC_URL"] = "https://mine.example/"
            self.assertEqual(self.r.public_url(), "https://mine.example")
        finally:
            os.environ.pop("PUBLIC_URL", None)
            os.environ.pop("RAILWAY_PUBLIC_DOMAIN", None)

    def test_radar_command_opens_the_page_inside_telegram_or_explains_the_setup(self):
        import os
        os.environ.pop("PUBLIC_URL", None)
        os.environ.pop("RAILWAY_PUBLIC_DOMAIN", None)
        self.r.handle_radar()
        self.assertIn("Generate Domain", self.texts()[-1])
        os.environ["RAILWAY_PUBLIC_DOMAIN"] = "knife.up.railway.app"
        try:
            self.sent.clear()
            self.r.handle_radar()
            markup = json.loads([p for m, p in self.sent if m == "sendMessage"][-1]["reply_markup"])
            button = markup["inline_keyboard"][0][0]
            self.assertEqual(button["web_app"]["url"], "https://knife.up.railway.app")
            status = self.r.status_markup()["inline_keyboard"]
            self.assertEqual(len(status), 2)                              # the status message gets the button too
            self.assertIn("web_app", status[1][0])
        finally:
            os.environ.pop("RAILWAY_PUBLIC_DOMAIN", None)

    def test_the_radar_page_can_be_switched_off(self):
        import os
        os.environ["RAILWAY_PUBLIC_DOMAIN"] = "knife.up.railway.app"
        try:
            self.r.LIVE_RADAR = False
            self.assertIsNone(self.r.radar_markup())
            self.assertEqual(len(self.r.status_markup()["inline_keyboard"]), 1)
        finally:
            os.environ.pop("RAILWAY_PUBLIC_DOMAIN", None)

    def test_starting_the_page_server_works_and_a_busy_port_does_not_crash(self):
        server = self.r.start_dashboard(port=0)
        self.assertIsNotNone(server)
        self.addCleanup(server.shutdown)
        busy = server.server_address[1]
        self.assertIsNone(self.r.start_dashboard(port=busy))

    # ---- /drop lasts 3 hours ----
    def test_drop_gives_three_hours_by_default(self):
        self.assertEqual(self.r.DROP_MANUAL_MINUTES, 180.0)
        self.r.handle_drop("")
        self.assertAlmostEqual(self.r.DROP["until"] - time.time(), 180 * 60, delta=5)
        self.assertTrue(self.r.DROP["sticky"])
        self.assertIn("3 h", self.texts()[-1])
        self.assertIn("calm by myself", self.texts()[-1])

    def test_after_three_quiet_hours_it_goes_back_to_calm_and_says_so(self):
        self.r.handle_drop("")
        self.r.check_drop_end()
        self.sent.clear()
        self.r.DROP["until"] = time.time() - 1
        self.r.check_drop_end()
        calm = [x for x in self.texts() if "Back to calm mode" in x]
        self.assertEqual(len(calm), 1)
        self.assertIn("No activity for 3 h", calm[0])

    def test_activity_late_in_the_three_hours_keeps_it_going_a_while_longer(self):
        self.r.handle_drop("")
        self.r.DROP["until"] = time.time() + 120                         # two minutes left
        self.r.touch_activity()
        self.assertGreater(self.r.DROP["until"], time.time() + 3000)     # about an hour more
        self.assertEqual(self.r.DROP["span"], self.r.DROP_IDLE_MINUTES)

    def test_automatic_drop_mode_keeps_its_shorter_quiet_time(self):
        self.r.check_stock()
        self.world["products"] = shop(True, False, False, False)
        self.r.check_stock()                                              # a restock switches it on
        self.assertAlmostEqual(self.r.DROP["until"] - time.time(), 60 * 60, delta=5)
        self.assertEqual(self.r.span_text(60), "60 min")
        self.assertEqual(self.r.span_text(180), "3 h")

    def test_a_new_window_does_not_inherit_the_old_length(self):
        self.r.handle_drop("")
        self.r.end_drop()
        self.r.start_drop(self.r.DROP_IDLE_MINUTES, sticky=True)
        self.assertEqual(self.r.DROP["span"], self.r.DROP_IDLE_MINUTES)

    def test_the_page_button_gives_the_same_three_hours(self):
        base = self.serve()
        state = json.loads(self.http(base, "/api/drop", self.init_data(), "POST", {"on": True})[1])
        self.assertEqual(state["mode"]["span"], 180.0)

    # ---- sell-out clock and practice drill ----
    def history(self, kh1=(40.0, 60.0), kl2=()):
        now = time.time()
        events = []
        for i, gone in enumerate(kh1):
            events.append({"t": now - 86400 * (i + 1), "product": "Deck", "handle": "d", "reported": True,
                           "items": {f"k{i}": {"shape": "KH1", "size": "32mm", "gone": gone}}})
        for i, gone in enumerate(kl2):
            events.append({"t": now - 86400 * (i + 9), "product": "Deck", "handle": "d", "reported": True,
                           "items": {f"l{i}": {"shape": "KL2", "size": "32.5mm", "gone": gone}}})
        self.r.META["events"] = events

    def test_the_usual_sell_out_time_is_the_median_and_needs_two_samples(self):
        self.assertIsNone(self.r.typical_gone())
        self.history(kh1=(40.0,))
        self.assertIsNone(self.r.typical_gone("KH1"))
        self.history(kh1=(40.0, 60.0, 200.0), kl2=(600.0, 900.0))
        self.assertEqual(self.r.typical_gone("KH1"), 60.0)
        self.assertEqual(self.r.typical_gone("KL2"), 750.0)
        self.assertEqual(self.r.typical_gone(), 200.0)

    def test_alerts_say_how_long_the_size_usually_lasts_once_there_is_history(self):
        self.r.check_stock()
        self.world["products"] = shop(True, False, False, False)
        self.r.check_stock()
        first = [p for m, p in self.sent if m == "sendPhoto"][0]["caption"]
        self.assertNotIn("usually gone", first)                       # nothing learned yet
        self.history(kh1=(40.0, 60.0))
        self.sent.clear()
        self.r.check_stock()
        self.world["products"] = shop(False, False, False, False)
        self.r.check_stock()
        self.world["products"] = shop(True, False, False, False)
        self.r.check_stock()
        caption = [p for m, p in self.sent if m == "sendPhoto"][0]["caption"]
        self.assertIn("⏱ usually gone in ~50s", caption)

    def test_test_alerts_never_claim_a_usual_time(self):
        self.history(kh1=(40.0, 60.0))
        self.r.check_stock()
        self.sent.clear()
        self.r.send_test_alert()
        self.assertNotIn("usually gone", [p for m, p in self.sent if m == "sendPhoto"][0]["caption"])

    def test_the_page_data_has_the_clock_numbers(self):
        base = self.serve()
        self.history(kh1=(40.0, 60.0))
        self.r.check_stock()
        self.world["products"] = shop(True, False, False, False)
        self.r.check_stock()                                           # a new drop is recorded now
        state = json.loads(self.http(base, "/api/state", self.init_data())[1])
        kh1 = state["tiles"][0]
        self.assertEqual(kh1["typical"], 50.0)
        self.assertIsNone(state["tiles"][1]["typical"])
        self.assertLess(kh1["live"][0]["since"], 5)
        self.assertEqual(state["drill"]["limit"], 50.0)

    def test_drill_scores_are_saved_and_summarised(self):
        base = self.serve()
        self.assertEqual(json.loads(self.http(base, "/api/state", self.init_data())[1])["drill"]["limit"], 45.0)
        for ms, won in ((6400, True), (8800, True), (45000, False)):
            code, body = self.http(base, "/api/drill", self.init_data(), "POST", {"ms": ms, "won": won})
            self.assertEqual(code, 200)
        drill = json.loads(body)["drill"]
        self.assertEqual((drill["count"], drill["wins"], drill["best"], drill["avg"]), (3, 2, 6400, 7600))

    def test_bad_drill_scores_and_strangers_are_refused(self):
        base = self.serve()
        for bad in ({"ms": 5}, {"ms": "fast"}, {"ms": True}, {"ms": 99999999}, {}):
            self.assertEqual(self.http(base, "/api/drill", self.init_data(), "POST", bad)[0], 400, bad)
        self.assertEqual(self.http(base, "/api/drill", None, "POST", {"ms": 5000, "won": True})[0], 401)
        self.assertEqual(self.http(base, "/api/drill", self.init_data(user_id=7), "POST", {"ms": 5000, "won": True})[0], 403)
        self.assertEqual(self.r.META.get("drills") or [], [])

    def test_only_the_last_hundred_drills_are_kept(self):
        for i in range(120):
            self.r.record_drill(5000 + i, True)
        self.assertEqual(len(self.r.META["drills"]), 100)

    def test_the_radar_page_theme_follows_telegram_unless_set(self):
        self.assertIn('data-theme="auto"', self.r.page_html())
        self.r.RADAR_THEME = "light"
        self.assertIn('data-theme="light"', self.r.page_html())
        self.r.RADAR_THEME = "dark"
        self.assertIn('data-theme="dark"', self.r.page_html())
        self.r.RADAR_THEME = "neon"                              # anything else falls back to auto
        self.assertIn('data-theme="auto"', self.r.page_html())

    def test_the_served_page_uses_the_chosen_theme_and_has_no_skateboard_or_sweep(self):
        base = self.serve()
        self.r.RADAR_THEME = "light"
        page = self.http(base, "/")[1]
        self.assertIn('data-theme="light"', page)
        for gone in ('id="board"', 'id="ground"', "conic-gradient", "@keyframes sweep", "skateboard", "roll"):
            self.assertNotIn(gone, page)

    def test_the_practice_drill_pays_at_the_shop_not_with_a_wallet(self):
        page = self.r.page_html()
        for gone in ("Apple Pay", "Face ID", "Shop Pay", "Google Pay"):
            self.assertNotIn(gone, page)
        self.assertIn("pay at the shop", page)
        self.assertIn("Pay now", page)

    # ---- remembering the shop's pause across restarts ----
    def test_calm_checking_is_every_20_seconds_by_default(self):
        self.assertEqual(self.r.CHECK_INTERVAL, 20.0)

    def test_the_gentler_pace_is_saved_and_restored_after_a_restart(self):
        self.r.note_rate_limited()
        self.r.note_rate_limited()
        saved = json.load(open(self.r.META_FILE))
        self.assertEqual(saved["pace_extra"], 8.0)
        fresh = load()                                                   # the bot after a restart
        fresh.META.update(saved)
        fresh.load_pace_state()
        self.assertEqual(fresh.PACE["extra"], 8.0)
        self.assertFalse(fresh.PACE["buster"])
        self.assertEqual(fresh.current_interval(), fresh.CHECK_INTERVAL + 8.0)

    def test_the_pace_relaxes_slowly_and_that_is_saved_too(self):
        self.r.note_rate_limited()
        self.r.note_rate_limited()
        self.r.PACE["last_limit"] = time.time() - 1900
        self.r.calm_down()
        self.assertEqual(self.r.PACE["extra"], 6.0)
        self.assertEqual(json.load(open(self.r.META_FILE))["pace_extra"], 6.0)

    def test_a_pause_the_shop_asked_for_survives_a_restart(self):
        self.r.set_block(240)
        self.assertGreater(self.r.cooldown_remaining(), 230)
        saved = json.load(open(self.r.META_FILE))
        fresh = load()
        fresh.META.update(saved)
        fresh.load_pace_state()
        self.assertGreater(fresh.cooldown_remaining(), 230)
        self.r.BLOCK["until"] = 0.0
        self.assertEqual(self.r.cooldown_remaining(), 0.0)

    def test_status_says_when_the_next_try_is_while_the_shop_is_blocking(self):
        self.r.HEALTH["error"] = "the shop is limiting my checks (429)"
        self.r.set_block(200)
        self.assertIn("next try in", self.r.status_text())
        self.r.BLOCK["until"] = 0.0
        self.assertIn("still trying by myself", self.r.status_text())

    def test_after_half_an_hour_of_refusals_it_suggests_another_region_once(self):
        self.r.streak_note()
        self.assertEqual(self.texts(), [])                                # the first refusal is not news
        self.r.STREAK["since"] = time.time() - 1900
        self.r.streak_note()
        self.r.streak_note()
        hints = [x for x in self.texts() if "Still blocked after 30 minutes" in x]
        self.assertEqual(len(hints), 1)
        self.assertIn("Region", hints[0])
        self.r.streak_clear()
        self.assertEqual(self.r.STREAK["since"], 0.0)
        self.assertFalse(self.r.STREAK["hinted"])

    # ---- /watch ----
    def watch_world(self, other_live=False, deck_live=False):
        """The usual deck plus a watched deck with a size that is not one of the four shapes."""
        extra = [
            {"title": "All Over 2", "handle": "all-over-2", "images": [{"src": "https://img/ao.jpg"}], "variants": [
                {"id": 300, "title": "29mm K01", "price": "48.00", "available": deck_live},
                {"id": 301, "title": "34mm KL1", "price": "48.00", "available": False}]},
            {"title": "Grip Tape", "handle": "grip", "images": [], "variants": [
                {"id": 400, "title": "Default", "price": "9.00", "available": other_live}]},
        ]
        return shop(False, False, False, False, extra=extra)

    def test_watch_adds_lists_and_removes_names(self):
        self.r.handle_watch("")
        self.assertIn("Nothing yet", self.texts()[-1])
        self.r.handle_watch("  All   Over ")
        self.assertEqual(self.r.META["watches"], ["All Over"])
        self.assertIn("Watching All Over", self.texts()[-1])
        self.r.handle_watch("all over")
        self.assertIn("already watching", self.texts()[-1])
        self.r.handle_watch("x")
        self.assertIn("2 to 40", self.texts()[-1])
        self.r.handle_unwatch("ALL OVER")
        self.assertEqual(self.r.META["watches"], [])
        self.r.handle_unwatch("nope")
        self.assertIn("not watching", self.texts()[-1])

    def test_watch_has_a_limit_and_is_saved(self):
        for i in range(self.r.WATCH_LIMIT):
            self.r.handle_watch(f"Name {i}")
        self.r.handle_watch("One too many")
        self.assertIn("names already", self.texts()[-1])
        self.assertEqual(len(json.load(open(self.r.META_FILE))["watches"]), self.r.WATCH_LIMIT)

    def test_a_watched_deck_is_tracked_in_every_size_and_other_products_are_not(self):
        products = self.watch_world(other_live=True, deck_live=True)
        self.assertNotIn("300", self.r.build_snapshot(products))                 # not watched yet
        self.r.META["watches"] = ["all over"]
        snap = self.r.build_snapshot(products)
        self.assertTrue(snap["300"]["watch"])
        self.assertEqual(snap["300"]["shape"], "29mm K01")                       # labelled by its own name
        self.assertFalse(snap["301"]["watch"])                                    # KL1 is a normal tracked shape
        self.assertEqual(snap["301"]["shape"], "KL1")
        self.assertNotIn("400", snap)                                             # Grip Tape is not watched

    def test_a_watched_size_coming_back_sends_an_alert_with_its_own_button(self):
        self.r.META["watches"] = ["All Over"]
        self.world["products"] = self.watch_world()
        self.r.check_stock()
        self.world["products"] = self.watch_world(deck_live=True)
        self.sent.clear()
        self.r.check_stock()
        photo = [p for m, p in self.sent if m == "sendPhoto"][0]
        self.assertIn("All Over 2", photo["caption"])
        self.assertIn("29mm K01", photo["caption"])
        rows = json.loads(photo["reply_markup"])["inline_keyboard"]
        texts = [b["text"] for row in rows for b in row]
        self.assertIn("⚡ 29mm K01", texts)
        self.assertNotIn("✖ KH1", " ".join(texts))                                 # no four shape buttons here
        self.assertNotIn("✅ I bought it", texts)
        self.assertIn("/cart/300:1", [b["url"] for row in rows for b in row if "url" in b][0])

    def test_status_shows_what_you_watch(self):
        self.r.META["watches"] = ["All Over", "Nothing Like This"]
        self.world["products"] = self.watch_world(deck_live=True)
        self.r.check_stock()
        text = self.r.status_text()
        self.assertIn("All Over</b> · 1 in stock", text)
        self.assertIn("Nothing Like This</b> · no match yet", text)

    # ---- gentler on the shop ----
    class FakeResponse:
        def __init__(self, body, etag=None, url="https://x/products.json"):
            self.body, self.headers, self.url = body, ({"ETag": etag} if etag else {}), url
        def read(self): return self.body
        def geturl(self): return self.url
        def __enter__(self): return self
        def __exit__(self, *a): pass

    class FakeOpener:
        def __init__(self, script):
            self.script, self.seen = list(script), []
        def open(self, request, timeout=0):
            import urllib.error
            self.seen.append({k.lower(): v for k, v in request.header_items()})
            item = self.script.pop(0)
            if item == 304:
                raise urllib.error.HTTPError(request.full_url, 304, "Not Modified", {}, None)
            return item

    def test_an_unchanged_page_is_asked_about_first_and_reused(self):
        opener = self.FakeOpener([self.FakeResponse(b'{"products": [1]}', '"abc"'), 304, 304])
        self.r.OPENER = opener
        self.r.CONDITIONAL.clear()
        url = "https://x/products.json?limit=250&page=1"
        self.assertEqual(self.r.fetch_page(url)[0], b'{"products": [1]}')
        self.assertNotIn("if-none-match", opener.seen[0])
        self.assertEqual(self.r.fetch_page(url)[0], b'{"products": [1]}')               # served from memory
        self.assertEqual(opener.seen[1]["if-none-match"], '"abc"')
        self.assertEqual(self.r.fetch_page(url)[0], b'{"products": [1]}')
        self.assertEqual((self.r.REQ["sent"], self.r.REQ["unchanged"]), (3, 2))

    def test_a_changed_page_replaces_the_remembered_one(self):
        opener = self.FakeOpener([self.FakeResponse(b"one", '"1"'), self.FakeResponse(b"two", '"2"'), 304])
        self.r.OPENER = opener
        self.r.CONDITIONAL.clear()
        url = "https://x/products.json?limit=250&page=1"
        self.r.fetch_page(url)
        self.assertEqual(self.r.fetch_page(url)[0], b"two")
        self.assertEqual(self.r.fetch_page(url)[0], b"two")
        self.assertEqual(opener.seen[2]["if-none-match"], '"2"')

    def test_fresh_copy_requests_and_password_pages_are_never_remembered(self):
        opener = self.FakeOpener([self.FakeResponse(b"a", '"1"'), self.FakeResponse(b"b", '"1"', url="https://x/password")])
        self.r.OPENER = opener
        self.r.CONDITIONAL.clear()
        self.r.fetch_page("https://x/products.json?limit=250&page=1&_=123")             # drop mode: unique address
        self.assertEqual(self.r.CONDITIONAL, {})
        self.r.fetch_page("https://x/products.json?limit=250&page=1")                   # redirected to the password page
        self.assertEqual(self.r.CONDITIONAL, {})

    def test_a_real_error_still_raises_when_nothing_is_remembered(self):
        import urllib.error
        class Boom:
            def open(self, request, timeout=0):
                raise urllib.error.HTTPError(request.full_url, 429, "slow", {}, None)
        self.r.OPENER = Boom()
        self.r.CONDITIONAL.clear()
        with self.assertRaises(urllib.error.HTTPError):
            self.r.fetch_page("https://x/products.json?limit=250&page=1")

    def test_info_shows_how_many_requests_were_saved(self):
        self.r.REQ.update(sent=120, unchanged=80)
        self.assertIn("shop requests: 120", self.r.info_text())
        self.assertIn("unchanged", self.r.info_text())

    def test_every_new_command_is_in_the_menu_and_the_help(self):
        names = [n for n, _ in self.r.MENU_COMMANDS]
        for needed in ("watch", "unwatch"):
            self.assertIn(needed, names)
            self.assertIn(f"/{needed}", self.r.help_text())

    # ---- the alarm: one loud Bark alert, only while you sleep ----
    def alarm_setup(self, on=True, hours="", max_minutes=0.0, domain=True, bark=True):
        import os
        self.r.ALARM_ASYNC = False                              # run the alarm right here in the test
        self.r.ALARM_RINGS = 4
        self.r.META["alarm"] = {"on": on, "hours": hours}
        self.r.BARK_KEY = "key123" if bark else ""
        self.r.AWAKE["t"] = 0.0                                 # nobody has used the bot for ages: you are asleep
        self.r.ALARM.update(acked=False, running=False, token="", token_t=0.0)
        if domain:
            os.environ["PUBLIC_URL"] = "https://radar.example"
        else:
            os.environ.pop("PUBLIC_URL", None)
            os.environ.pop("RAILWAY_PUBLIC_DOMAIN", None)
        self.addCleanup(lambda: os.environ.pop("PUBLIC_URL", None))
        self.pushes = []
        self.bark_script = []                                   # answers for the next pushes, then "success"
        def fake(title, body, url=""):
            self.pushes.append((title, body, url))
            answer = self.bark_script.pop(0) if self.bark_script else "success"
            if isinstance(answer, Exception):
                raise answer
            return answer
        self.r.bark_push = fake

    def drop_happens(self):
        self.r.check_stock()
        self.world["products"] = shop(True, False, False, False)
        self.sent.clear()
        self.r.check_stock()

    def tap_the_alert(self):
        """Taps the Bark alert on the phone: the link opens in Safari. Returns where Safari ends up."""
        return self.r.awake_target(self.r.ALARM["token"])

    def tapped_after(self, count):
        """Pretends time passes, and taps the alert once this many alerts have been sent."""
        def sleeper(sec):
            if len(self.pushes) >= count and not self.r.ALARM["acked"]:
                self.tap_the_alert()
        self.r.time.sleep = sleeper

    def tap(self, data, message_id=9):
        calls = []
        real = self.r.telegram_api
        self.r.telegram_api = lambda m, p=None, _retry=True: (calls.append((m, p or {})) or {"ok": True, "result": []})
        try:
            self.r.handle_callback({"id": "q", "data": data, "message": {"chat": {"id": 42}, "message_id": message_id}})
        finally:
            self.r.telegram_api = real
        return calls

    def labels(self, markup_json_or_dict):
        markup = json.loads(markup_json_or_dict) if isinstance(markup_json_or_dict, str) else markup_json_or_dict
        return [b["text"] for row in markup["inline_keyboard"] for b in row]

    def test_pushover_and_callmebot_are_gone_for_good(self):
        import re
        source = open(self.r.__file__).read()
        self.assertEqual(re.findall(r"(?i)pushover|callmebot", source), [])
        self.assertNotIn("WAKE UP", source)

    def test_the_alarm_is_off_until_you_switch_it_on(self):
        self.alarm_setup(on=False)
        self.drop_happens()
        self.assertEqual(self.pushes, [])
        self.assertEqual(len([m for m, p in self.sent if m == "sendPhoto"]), 1)      # the normal alert still comes

    def test_a_drop_sends_one_loud_alert_that_stops_and_opens_the_checkout_when_tapped(self):
        self.alarm_setup()
        self.tapped_after(1)
        self.drop_happens()
        self.assertEqual(len(self.pushes), 1)
        title, body, url = self.pushes[0]
        self.assertEqual(title, "Knife Mfg Co: IN STOCK")
        self.assertIn("KH1 Test Deck", body)
        self.assertIn("Tap to stop and buy", body)
        self.assertTrue(url.startswith("https://radar.example/awake/"))
        self.assertEqual(url.rsplit("/", 1)[1], self.r.ALARM["token"])
        self.assertIn("/cart/10:1", self.r.awake_target(self.r.ALARM["token"]))        # a tap ends up at the checkout
        self.assertFalse(self.r.ALARM["running"])

    def test_nothing_is_spammed_in_telegram_and_a_tap_needs_no_message(self):
        self.alarm_setup()
        self.tapped_after(3)
        self.drop_happens()
        extra = [x for x in self.texts() if "WAKE" in x or "awake" in x.lower() or "Alarm off" in x]
        self.assertEqual(extra, [])
        self.assertEqual(len([m for m, p in self.sent if m == "sendMessage"]), 0)    # only the normal photo alert




    def test_it_stays_quiet_while_you_are_active(self):
        self.alarm_setup()
        self.r.AWAKE["t"] = time.time() - 5 * 60                                    # you used the bot 5 minutes ago
        self.drop_happens()
        self.assertEqual(self.pushes, [])
        self.assertEqual(len([m for m, p in self.sent if m == "sendPhoto"]), 1)      # the normal alert still came

    def test_it_rings_once_you_have_been_away_for_half_an_hour(self):
        self.alarm_setup()
        self.r.AWAKE["t"] = time.time() - 31 * 60                                   # half an hour of quiet: asleep
        self.tapped_after(1)
        self.drop_happens()
        self.assertEqual(len(self.pushes), 1)

    def test_entering_the_shop_password_counts_as_being_active(self):
        self.alarm_setup()
        self.assertFalse(self.r.user_awake())
        self.r.mark_awake()                                                          # what any command or button does
        self.assertTrue(self.r.user_awake())
        self.assertFalse(self.r.alarm_active() and self.r.user_awake() is False)

    def test_any_button_or_command_from_you_marks_you_awake(self):
        self.alarm_setup()
        self.tap("pn:status")
        self.assertTrue(self.r.user_awake())

    def test_having_the_live_radar_open_marks_you_awake(self):
        self.alarm_setup()
        base = self.serve()
        self.assertFalse(self.r.user_awake())
        self.http(base, "/api/state", self.init_data())
        self.assertTrue(self.r.user_awake())

    def test_a_stranger_opening_the_radar_does_not_mark_you_awake(self):
        self.alarm_setup()
        base = self.serve()
        self.http(base, "/api/state", self.init_data(user_id=7))
        self.http(base, "/api/state", None)
        self.assertFalse(self.r.user_awake())

    def test_using_the_bot_while_it_rings_stops_it(self):
        self.alarm_setup()
        def sleeper(sec):
            if len(self.pushes) >= 1 and not self.r.ALARM["acked"]:
                self.tap("pn:status")                                                # you picked up your phone
        self.r.time.sleep = sleeper
        self.drop_happens()
        self.assertEqual(len(self.pushes), 1)
        self.assertFalse(self.r.ALARM["running"])


    def test_a_failing_bark_is_explained_once_and_given_up_after_three_tries(self):
        self.alarm_setup()
        self.bark_script = ["error 400: device key not found"] * 5
        self.drop_happens()
        problems = [x for x in self.texts() if "Bark alert didn't go through" in x]
        self.assertEqual(len(problems), 1)
        self.assertIn("device key not found", problems[0])
        self.assertIn("BARK_KEY", problems[0])
        self.assertEqual(len(self.pushes), 3)
        self.assertTrue(any("Bark kept failing" in x for x in self.texts()))

    def test_a_hiccup_does_not_stop_the_alarm(self):
        self.alarm_setup()
        self.bark_script = [OSError("no internet"), "success"]
        self.tapped_after(3)
        self.drop_happens()
        self.assertEqual(len(self.pushes), 3)                                       # failed, then ringing normally
        self.assertEqual(len([x for x in self.texts() if "Bark kept failing" in x]), 0)

    def test_the_test_alarm_rings_even_while_you_are_active_and_shows_that_it_works(self):
        self.alarm_setup(on=False)
        self.r.mark_awake()                                                          # you are using the bot right now
        self.tapped_after(1)
        calls = self.tap("al:test")
        self.assertEqual(len(self.pushes), 1)
        self.assertEqual(self.pushes[0][0], "Knife Mfg Co: TEST")
        self.assertIn("It works", " ".join(p["text"] for m, p in calls if m == "sendMessage"))
        self.assertEqual(self.r.awake_target(self.r.ALARM["token"]), self.r.SITE)    # a test opens the shop, not a deck


    def test_the_tap_link_stops_the_alarm_and_redirects_to_the_checkout_over_the_web(self):
        import http.client
        self.alarm_setup()
        base = self.serve()
        host, port = base.replace("http://", "").split(":")
        def get(path):
            conn = http.client.HTTPConnection(host, int(port), timeout=10)
            conn.request("GET", path)
            reply = conn.getresponse()
            reply.read()
            return reply.status, reply.getheader("Location")
        self.r.ALARM.update(token="tok123", token_t=time.time(), buy="https://knifemfg.co/cart/10:1", running=True, acked=False)
        self.assertEqual(get("/awake/wrong"), (302, self.r.SITE))                   # a wrong token goes to the shop
        self.assertFalse(self.r.ALARM["acked"])
        self.assertEqual(get("/awake/tok123"), (302, "https://knifemfg.co/cart/10:1"))
        self.assertTrue(self.r.ALARM["acked"])
        self.assertEqual(get("/awake/tok123"), (302, "https://knifemfg.co/cart/10:1"))    # a second tap still reaches checkout
        self.r.ALARM["token_t"] = time.time() - 3700                                 # but not after an hour
        self.assertEqual(get("/awake/tok123"), (302, self.r.SITE))
        self.assertEqual(get("/awake/"), (302, self.r.SITE))

    def test_the_tap_link_can_only_ever_lead_to_the_shop_or_the_checkout(self):
        self.alarm_setup()
        self.r.ALARM.update(token="abc", token_t=time.time(), buy="", running=False)
        self.assertEqual(self.r.awake_target("abc"), self.r.SITE)                    # nothing to open: the shop
        self.assertEqual(self.r.awake_target("https://evil.example"), self.r.SITE)
        self.assertEqual(self.r.awake_target(""), self.r.SITE)
        self.assertEqual(self.r.awake_target(None), self.r.SITE)

    def test_the_alarm_hours_are_in_your_own_time(self):
        import datetime
        sgt = datetime.timezone(datetime.timedelta(hours=8))
        at = lambda h: datetime.datetime(2026, 10, 12, h, 30, tzinfo=sgt).timestamp()
        self.r.META["alarm"] = {"on": True, "hours": "23-9"}
        self.assertTrue(self.r.in_alarm_hours(at(23)))
        self.assertTrue(self.r.in_alarm_hours(at(2)))
        self.assertTrue(self.r.in_alarm_hours(at(8)))
        self.assertFalse(self.r.in_alarm_hours(at(9)))
        self.assertFalse(self.r.in_alarm_hours(at(14)))
        self.r.META["alarm"]["hours"] = ""
        self.assertTrue(self.r.in_alarm_hours(at(14)))
        for bad in ("9-9", "25-3", "abc", "9"):
            with self.assertRaises(Exception):
                self.r.parse_hours(bad)

    def test_outside_the_hours_it_stays_quiet(self):
        import datetime
        self.alarm_setup(hours="23-9")
        now_hour = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).hour
        self.r.META["alarm"]["hours"] = f"{(now_hour + 2) % 24}-{(now_hour + 3) % 24}"       # an hour that is not now
        self.drop_happens()
        self.assertEqual(self.pushes, [])

    def test_the_first_read_after_a_restart_never_rings(self):
        self.alarm_setup()
        self.world["products"] = shop(True, False, False, False)                  # already in stock when the bot starts
        self.r.check_stock()
        self.assertEqual(self.pushes, [])

    def test_without_bark_nothing_rings_and_the_alarm_cannot_be_switched_on(self):
        self.alarm_setup(on=True, bark=False)
        self.drop_happens()
        self.assertEqual(self.pushes, [])
        self.r.META["alarm"]["on"] = False
        calls = self.tap("al:toggle")
        self.assertFalse(self.r.META["alarm"]["on"])
        self.assertEqual([p["text"] for m, p in calls if m == "answerCallbackQuery"], ["Set up Bark first (BARK_KEY)"])
        self.r.handle_alarm("on")
        self.assertIn("Bark not set up", self.texts()[-1])
        self.assertFalse(self.r.META["alarm"]["on"])


    # ---- the alarm screen ----

    def test_the_screen_warns_about_a_missing_bark_key_or_web_address(self):
        self.alarm_setup(bark=False)
        self.assertIn("Bark not set up", self.r.alarm_text())
        self.alarm_setup(domain=False)
        self.assertIn("No web address yet", self.r.alarm_text())
        self.alarm_setup()
        self.assertNotIn("not set up", self.r.alarm_text())
        self.assertNotIn("No web address", self.r.alarm_text())

    def test_the_alarm_switch_turns_it_on_and_off_and_redraws(self):
        self.alarm_setup(on=False, hours="23-9")
        calls = self.tap("al:toggle")
        self.assertTrue(self.r.META["alarm"]["on"])
        edit = [p for m, p in calls if m == "editMessageText"][0]
        self.assertEqual(self.labels(edit["reply_markup"])[0], "📲 Alarm: On")
        self.assertIn("Night alarm on", edit["text"])
        self.assertEqual([p["text"] for m, p in calls if m == "answerCallbackQuery"], ["Alarm on 📲"])
        self.tap("al:toggle")
        self.assertFalse(self.r.META["alarm"]["on"])
        self.assertTrue(json.load(open(self.r.META_FILE))["alarm"]["on"] is False)

    def test_the_night_only_switch_flips_between_23_to_09_and_any_hour(self):
        self.alarm_setup(on=True, hours="23-9")
        self.tap("al:hours")
        self.assertEqual(self.r.META["alarm"]["hours"], "")
        self.assertIn("🌙 Only 23:00-09:00: Off", self.labels(self.r.alarm_markup()))
        self.assertEqual(self.r.alarm_mode(), "always")
        self.tap("al:hours")
        self.assertEqual(self.r.META["alarm"]["hours"], "23-9")
        self.assertEqual(self.r.alarm_mode(), "night")

    def test_buttons_from_older_versions_are_harmlessly_ignored(self):
        self.alarm_setup(on=False)
        for old in ("al:night", "al:always", "al:on", "al:off", "al:h:22-8", "al:ack", "al:call", "al:bark"):
            calls = self.tap(old)
            self.assertEqual([p["text"] for m, p in calls if m == "answerCallbackQuery"], ["✓"], old)
        self.assertEqual(self.r.META["alarm"], {"on": False, "hours": ""})

    def test_typed_alarm_commands_still_work(self):
        self.alarm_setup(on=False)
        self.r.handle_alarm("on")
        self.assertTrue(self.r.META["alarm"]["on"])
        self.r.handle_alarm("hours 22-8")
        self.assertEqual(self.r.META["alarm"]["hours"], "22-8")
        self.assertIn("22:00 to 08:00", self.texts()[-1])
        self.r.handle_alarm("off")
        self.assertFalse(self.r.META["alarm"]["on"])
        self.r.handle_alarm("hours 99-3")
        self.assertIn("24-hour clock", self.texts()[-1])
        self.r.handle_alarm("hours all")
        self.assertEqual(self.r.META["alarm"]["hours"], "")
        for gone in ("night", "bark", "banana"):                                     # removed shortcuts and nonsense
            self.r.handle_alarm(gone)
            self.assertIn("buttons", self.texts()[-1])
        self.tapped_after(1)
        self.r.handle_alarm("test")
        self.assertEqual(len(self.pushes), 1)

    def test_the_panel_shows_the_state_on_its_buttons(self):
        self.alarm_setup(on=False)
        labels = self.labels(self.r.panel_markup())
        for needed in ("📡 Status", "⚡ Drop mode: Off", "🚨 Alarm: Off", "🔮 Smart drop: On", "⚙ Alarm settings",
                       "⚡ Auto drop: On", "👀 Watching", "🏆 Pickups", "📦 Parcels", "📜 History", "🔮 Forecast", "❔ Help"):
            self.assertIn(needed, labels)
        self.r.start_drop(10, sticky=True)
        self.r.META["alarm"]["on"] = True
        self.r.META["smart"] = False
        labels = self.labels(self.r.panel_markup())
        self.assertIn("⚡ Drop mode: On", labels)
        self.assertIn("🚨 Alarm: On", labels)
        self.assertIn("🔮 Smart drop: Off", labels)
        text = self.r.panel_text()
        self.assertIn("Drop mode on", text)
        self.assertIn("alarm on", text.lower())
        self.assertIn("Smart drop off", text)
        self.r.META["alarm"] = {"on": True, "hours": "23-9"}
        self.assertIn("Night alarm on · 23:00 to 09:00", self.r.panel_text())

    def test_panel_and_start_and_alarm_commands_show_buttons(self):
        for command in ("/panel", "/start", "/alarm"):
            self.sent.clear()
            self.r.handle_command(command)
            message = [p for m, p in self.sent if m == "sendMessage"][-1]
            self.assertIn("reply_markup", message, command)
            self.assertGreaterEqual(len(self.labels(message["reply_markup"])), 4, command)

    def test_the_panel_switches_flip_the_real_settings_and_redraw_the_panel(self):
        self.alarm_setup(on=False)
        calls = self.tap("pn:drop")
        self.assertTrue(self.r.fast_now())
        self.assertIn("⚡ Drop mode: On", self.labels([p for m, p in calls if m == "editMessageText"][0]["reply_markup"]))
        self.tap("pn:drop")
        self.assertFalse(self.r.fast_now())
        self.tap("pn:alarm")
        self.assertTrue(self.r.META["alarm"]["on"])
        self.tap("pn:alarm")
        self.assertFalse(self.r.META["alarm"]["on"])
        self.tap("pn:smart")
        self.assertFalse(self.r.META["smart"])
        self.tap("pn:smart")
        self.assertTrue(self.r.META["smart"])

    def test_the_panel_can_open_the_alarm_screen_and_come_back(self):
        self.alarm_setup(on=False)
        calls = self.tap("pn:alarmset")
        edit = [p for m, p in calls if m == "editMessageText"][0]
        self.assertIn("Alarm", edit["text"])
        self.assertIn("◀ Panel", self.labels(edit["reply_markup"]))
        calls = self.tap("pn:home")
        edit = [p for m, p in calls if m == "editMessageText"][0]
        self.assertIn("Control panel", edit["text"])

    def test_the_other_panel_buttons_open_their_own_messages(self):
        expect = {"pn:status": "deck radar", "pn:watch": "Watching",
                  "pn:stats": "pickups", "pn:parcels": "parcels", "pn:history": "history",
                  "pn:forecast": "Forecast", "pn:help": "Tap /panel"}
        for data, word in expect.items():
            self.sent.clear()
            self.r.telegram_api = self.original_api
            api_calls = []
            def api(method, params=None, _retry=True, store=api_calls):
                store.append((method, params or {}))
                return {"ok": True, "result": []}
            self.r.telegram_api = api
            self.r.handle_callback({"id": "q", "data": data, "message": {"chat": {"id": 42}, "message_id": 5}})
            sent = [p["text"] for m, p in api_calls if m == "sendMessage"]
            self.assertTrue(any(word.lower() in x.lower() for x in sent), (data, sent))

    def test_a_strangers_chat_cannot_use_the_panel(self):
        self.alarm_setup(on=False)
        self.r.handle_callback({"id": "q", "data": "pn:alarm", "message": {"chat": {"id": 999}, "message_id": 1}})
        self.assertFalse(self.r.META["alarm"]["on"])

    def test_the_help_is_compact_and_lists_every_command(self):
        text = self.r.help_text()
        for name, _ in self.r.MENU_COMMANDS:
            self.assertIn(f"/{name}", text)
        self.assertLess(len(text.splitlines()), 32)
        self.assertEqual(self.labels(self.r.help_markup())[0], "🎛 Control panel")

    def test_panel_and_alarm_are_in_the_menu(self):
        names = [n for n, _ in self.r.MENU_COMMANDS]
        self.assertEqual(names[0], "panel")
        self.assertIn("alarm", names)

    # ---- no ready checks, and auto drop mode ----
    def test_the_ready_checks_are_gone(self):
        import re
        source = open(self.r.__file__).read()
        for gone in ("ready_text", "ready_markup", "READY_STEPS", "handle_ready_tap", "pn:ready"):
            self.assertNotIn(gone, source)
        self.assertEqual(re.findall(r"[\"']rd:", source), [])             # no checklist buttons any more
        self.assertNotIn("ready", [n for n, _ in self.r.MENU_COMMANDS])
        self.assertNotIn("/ready", self.r.help_text())
        self.assertNotIn("Ready check", " ".join(self.labels(self.r.panel_markup())))
        self.r.handle_command("/ready")
        self.assertIn("Unknown command", self.texts()[-1])

    def test_drop_mode_switches_on_by_itself_when_something_is_in_stock(self):
        self.r.ALERT_ON_START = False                       # no alert at start-up: only the stock itself matters
        self.world["products"] = shop(True, False, False, False)
        self.assertFalse(self.r.fast_now())
        self.r.check_stock()
        self.assertTrue(self.r.fast_now())
        self.assertTrue(self.r.DROP["sticky"])

    def test_drop_mode_stays_calm_when_nothing_is_in_stock(self):
        self.world["products"] = shop(False, False, False, False)
        self.r.check_stock()
        self.r.check_stock()
        self.assertFalse(self.r.fast_now())
        self.assertEqual(self.r.AUTO["since"], 0.0)

    def test_it_stays_fast_while_the_stock_lasts_and_relaxes_after_it_sells_out(self):
        self.r.ALERT_ON_START = False
        self.world["products"] = shop(True, False, False, False)
        self.r.check_stock()
        self.r.DROP["until"] = time.time() + 30                        # about to run out
        self.r.check_stock()                                           # still in stock: it is extended
        self.assertGreater(self.r.DROP["until"], time.time() + 3000)
        self.world["products"] = shop(False, False, False, False)
        self.r.check_stock()                                           # sold out: no more extending
        before = self.r.DROP["until"]
        self.r.check_stock()
        self.assertEqual(self.r.DROP["until"], before)
        self.assertEqual(self.r.AUTO["since"], 0.0)

    def test_a_size_that_is_simply_always_there_does_not_keep_it_fast_for_days(self):
        self.r.ALERT_ON_START = False
        self.world["products"] = shop(True, False, False, False)
        self.r.check_stock()
        self.r.AUTO["since"] = time.time() - (self.r.AUTO_DROP_MAX_MINUTES + 1) * 60
        self.r.end_drop()
        self.r.check_stock()                                           # still the same stock, long past the limit
        self.assertFalse(self.r.fast_now())
        self.world["products"] = shop(False, False, False, False)
        self.r.check_stock()
        self.world["products"] = shop(True, False, False, False)       # a fresh restock starts it again
        self.r.check_stock()
        self.assertTrue(self.r.fast_now())

    def test_the_auto_drop_switch_turns_it_off_everywhere(self):
        self.r.ALERT_ON_START = False
        self.r.META["auto_drop"] = False
        self.world["products"] = shop(True, False, False, False)
        self.r.check_stock()
        self.assertFalse(self.r.fast_now())                            # in stock, but the switch is off
        self.r.check_stock()
        self.world["products"] = shop(False, False, False, False)
        self.r.check_stock()
        self.world["products"] = shop(True, True, False, False)        # a restock alert does not start it either
        self.r.check_stock()
        self.assertFalse(self.r.fast_now())

    def test_the_panel_button_and_the_command_flip_auto_drop(self):
        self.assertTrue(self.r.auto_drop_on())
        self.assertIn("⚡ Auto drop: On", self.labels(self.r.panel_markup()))
        self.assertIn("Auto drop on", self.r.panel_text())
        calls = self.tap("pn:auto")
        self.assertFalse(self.r.META["auto_drop"])
        edit = [p for m, p in calls if m == "editMessageText"][0]
        self.assertIn("⚡ Auto drop: Off", self.labels(edit["reply_markup"]))
        self.tap("pn:auto")
        self.assertTrue(self.r.META["auto_drop"])
        self.r.handle_switch("autodrop", "off")
        self.assertFalse(self.r.META["auto_drop"])
        self.assertIn("<b>off</b>", self.texts()[-1])
        self.r.handle_switch("autodrop", "on")
        self.assertTrue(self.r.META["auto_drop"])
        self.r.handle_switch("autodrop", "maybe")
        self.assertIn("Try", self.texts()[-1])

    def test_autodrop_is_in_the_menu_and_the_help(self):
        self.assertIn("autodrop", [n for n, _ in self.r.MENU_COMMANDS])
        self.assertIn("/autodrop", self.r.help_text())

    def test_a_watched_deck_in_stock_also_starts_drop_mode(self):
        self.r.ALERT_ON_START = False
        self.r.META["watches"] = ["All Over"]
        self.world["products"] = self.watch_world(deck_live=True)
        self.r.check_stock()
        self.assertTrue(self.r.fast_now())

    # ---- the drop board: one tidy message ----
    def board_setup(self, board=True):
        self.r.META["board"] = board
        self.calls = []
        self.next_id = [100]
        self.api_fail = {"sends": 0, "edits": 0}
        def api(method, params=None, _retry=True):
            params = params or {}
            if method in ("sendMessage", "sendPhoto"):
                if self.api_fail["sends"] > 0:
                    self.api_fail["sends"] -= 1
                    return {"ok": False}
            if method in ("editMessageCaption", "editMessageText") and self.api_fail["edits"] > 0:
                self.api_fail["edits"] -= 1
                return {"ok": False, "description": "message to edit not found"}
            self.calls.append((method, params))
            if method in ("sendMessage", "sendPhoto"):
                self.next_id[0] += 1
                return {"ok": True, "result": {"message_id": self.next_id[0]}}
            return {"ok": True, "result": []}
        self.r.telegram_api = api
        self.r.board_reset()

    def posts(self):
        return [(m, p) for m, p in self.calls if m in ("sendMessage", "sendPhoto")]

    def edits(self):
        return [(m, p) for m, p in self.calls if m in ("editMessageCaption", "editMessageText")]

    def deletes(self):
        return [p for m, p in self.calls if m == "deleteMessage"]

    def text_of(self, call):
        import re
        return re.sub(r"<[^>]+>", "", call[1].get("caption") or call[1].get("text") or "")

    def last_text(self):
        """The last message the bot sent, with the formatting tags removed."""
        import re
        sent = [p for m, p in self.calls if m in ("sendMessage", "sendPhoto")]
        return re.sub(r"<[^>]+>", "", sent[-1].get("caption") or sent[-1].get("text") or "")

    def second_deck(self, *states):
        names = ["32mm KH1", "32.5mm KL2", "34mm KL1", "34mm KB1"]
        variants = [{"id": 500 + i, "title": n, "price": "60.00", "available": s} for i, (n, s) in enumerate(zip(names, states))]
        return {"title": "Second Deck", "handle": "second-deck", "images": [{"src": "https://img/y.jpg"}], "variants": variants}













    # ---- shop news: one message for anything new ----
    def news_setup(self):
        self.r.META["news"] = True
        self.calls = []
        def api(method, params=None, _retry=True):
            self.calls.append((method, params or {}))
            return {"ok": True, "result": {"message_id": len(self.calls)}}
        self.r.telegram_api = api

    def news_posts(self):
        return [(m, p) for m, p in self.calls if m in ("sendMessage", "sendPhoto") and "SHOP UPDATE" in (p.get("caption") or p.get("text", ""))]

    def news_text(self, call):
        return self.text_of(call)

    def test_the_first_read_only_learns_the_shop_and_never_announces(self):
        self.news_setup()
        self.r.check_stock()
        self.assertEqual(self.news_posts(), [])
        self.assertIn("test-deck", self.r.META["seen"])

    def test_a_new_product_is_announced_once_even_when_sold_out(self):
        self.news_setup()
        self.r.check_stock()
        extra = [{"title": "Brand New Deck", "handle": "brand-new", "images": [{"src": "https://img/n.jpg"}], "variants": [
            {"id": 900, "title": "32mm KH1", "price": "70.00", "available": False},
            {"id": 901, "title": "34mm KL1", "price": "70.00", "available": False}]}]
        self.world["products"] = shop(False, False, False, False, extra=extra)
        self.r.check_stock()
        news = self.news_posts()
        self.assertEqual(len(news), 1)
        self.assertEqual(news[0][0], "sendPhoto")
        text = self.text_of(news[0])
        self.assertIn("Brand New Deck", text)
        self.assertIn("32mm KH1 (sold out)", text)
        self.assertIn("~S$", text)
        self.assertEqual(self.labels(news[0][1]["reply_markup"]), ["🔗 Open page", "🛒 Open shop"])
        self.r.check_stock()
        self.assertEqual(len(self.news_posts()), 1)                                # not again

    def test_a_new_size_on_a_known_product_is_announced(self):
        self.news_setup()
        self.r.check_stock()
        products = shop(False, False, False, False)
        products[0]["variants"].append({"id": 77, "title": "36mm KL3", "price": "48.00", "available": False})
        self.world["products"] = products
        self.r.check_stock()
        text = self.text_of(self.news_posts()[0])
        self.assertIn("new size 36mm KL3 (sold out)", text)
        self.assertIn("Test Deck", text)

    def test_a_price_change_on_a_tracked_size_is_announced(self):
        self.news_setup()
        self.r.check_stock()
        products = shop(False, False, False, False)
        products[0]["variants"][0]["price"] = "55.00"
        self.world["products"] = products
        self.r.check_stock()
        text = self.text_of(self.news_posts()[0])
        self.assertIn("KH1", text)
        self.assertIn("~S$62 → ~S$71", text)

    def test_several_news_items_in_one_check_make_one_message(self):
        self.news_setup()
        self.r.check_stock()
        extra = [{"title": "Deck A", "handle": "a", "images": [], "variants": [{"id": 910, "title": "Default", "price": "10.00", "available": True}]},
                 {"title": "Deck B", "handle": "b", "images": [], "variants": [{"id": 920, "title": "Default", "price": "20.00", "available": False}]}]
        products = shop(False, False, False, False, extra=extra)
        products[0]["variants"][1]["price"] = "49.00"
        self.world["products"] = products
        self.r.check_stock()
        news = self.news_posts()
        self.assertEqual(len(news), 1)
        text = self.text_of(news[0])
        for name in ("Deck A", "Deck B", "KL2"):
            self.assertIn(name, text)

    def test_a_new_product_that_is_in_stock_is_covered_by_the_drop_alert_not_the_news(self):
        self.news_setup()
        self.r.META["board"] = False
        self.r.check_stock()
        deck = self.second_deck(True, False, False, False)
        self.world["products"] = shop(False, False, False, False, extra=[deck])
        self.r.check_stock()
        self.assertEqual(self.news_posts(), [])
        self.assertEqual(len([m for m, p in self.calls if m == "sendPhoto"]), 1)      # just the restock alert

    def test_with_news_off_nothing_is_sent_and_switching_on_later_does_not_flood(self):
        self.news_setup()
        self.r.META["news"] = False
        self.r.check_stock()
        extra = [{"title": "Quiet Deck", "handle": "quiet", "images": [], "variants": [{"id": 930, "title": "Default", "price": "10.00", "available": False}]}]
        self.world["products"] = shop(False, False, False, False, extra=extra)
        self.r.check_stock()
        self.assertEqual(self.news_posts(), [])
        self.r.META["news"] = True
        self.r.check_stock()
        self.assertEqual(self.news_posts(), [])                                    # already known

    def test_a_news_message_that_fails_is_tried_again(self):
        self.news_setup()
        self.r.check_stock()
        fail = {"n": 99}
        base = self.r.telegram_api
        def flaky(method, params=None, _retry=True):
            if method in ("sendMessage", "sendPhoto") and fail["n"] > 0:
                fail["n"] -= 1
                return {"ok": False}
            return base(method, params, _retry)
        self.r.telegram_api = flaky
        extra = [{"title": "Retry Deck", "handle": "retry", "images": [], "variants": [{"id": 940, "title": "Default", "price": "10.00", "available": False}]}]
        self.world["products"] = shop(False, False, False, False, extra=extra)
        self.r.check_stock()
        self.assertEqual(self.news_posts(), [])
        self.assertNotIn("retry", self.r.META["seen"])
        fail["n"] = 0
        self.r.check_stock()
        self.assertEqual(len(self.news_posts()), 1)

    def test_a_crash_in_the_news_check_never_stops_the_stock_check(self):
        self.news_setup()
        self.r.detect_news = lambda *a, **k: (_ for _ in ()).throw(ValueError("boom"))
        self.r.check_stock()
        self.world["products"] = shop(True, False, False, False)
        self.r.check_stock()
        self.assertTrue(self.r.STATE.get("10"))

    def test_the_news_switch_is_on_the_panel_and_typed(self):
        self.news_setup()
        self.assertIn("🆕 New products: On", self.labels(self.r.panel_markup()))
        self.tap("pn:news")
        self.assertFalse(self.r.META["news"])
        self.r.handle_switch("news", "on")
        self.assertTrue(self.r.META["news"])
        self.r.handle_switch("news", "maybe")
        self.assertIn("Try", self.last_text())

    # ---- the alarm: which sizes may wake you ----



    # ---- the clean-up: one switch system, a board that survives restarts, solid fallbacks ----
    def test_every_setting_the_script_reads_is_explained_at_the_top(self):
        import re
        source = open(self.r.__file__).read()
        top = source.split('"""', 2)[1]
        used = set(re.findall(r'os\.getenv\("([A-Z0-9_]+)"', source))
        self.assertEqual(sorted(v for v in used if v not in top), [])

    def test_every_section_named_at_the_top_exists(self):
        import re
        source = open(self.r.__file__).read()
        listed = [x.strip() for x in source.split("Sections in this file", 1)[1].split('"""', 1)[0].replace("\n", " ").split("·")]
        found = re.findall(r"^# ={20,}\n# (.+)", source, re.M)
        self.assertEqual(listed, found)

    def test_the_switch_commands_show_and_change_their_setting(self):
        for name, key in (("autodrop", "auto_drop"), ("board", "board"), ("news", "news"), ("smart", "smart")):
            self.r.META[key] = True
            self.r.handle_switch(name, "off")
            self.assertFalse(self.r.META[key], name)
            self.assertIn("<b>off</b>", self.texts()[-1])
            self.assertIn(f"/{name} on", self.texts()[-1])
            self.r.handle_switch(name, "on")
            self.assertTrue(self.r.META[key], name)
            self.r.handle_switch(name, "")
            self.assertIn("<b>on</b>", self.texts()[-1])
            self.r.handle_switch(name, "maybe")
            self.assertIn("Try", self.texts()[-1])

    def test_the_panel_switches_all_go_through_the_same_place(self):
        self.r.telegram_api = lambda m, p=None, _retry=True: {"ok": True, "result": []}
        for button, key in (("pn:auto", "auto_drop"), ("pn:board", "board"), ("pn:news", "news"), ("pn:smart", "smart")):
            before = self.r.META.get(key, True)
            toast = self.r.handle_panel_tap({"message": {"chat": {"id": 42}, "message_id": 1}}, button)
            self.assertEqual(self.r.META[key], not before, button)
            self.assertIn(": on" if not before else ": off", toast)





    def test_a_surprise_inside_the_alarm_is_logged_and_the_alarm_is_freed(self):
        self.alarm_setup()
        def boom(*a, **k):
            raise RuntimeError("telegram exploded")
        self.r.send_text = boom
        self.r.bark_script = []
        self.tapped_after(99)
        self.r.ALARM_MAX_MINUTES = 1.0
        self.r.alarm_run([{"id": "10", "product": "Test Deck", "shape": "KH1", "handle": "test-deck", "cart_url": "https://x"}])
        self.assertFalse(self.r.ALARM["running"])

    # ---- v6.1: lessons from the first real drop ----
    def test_drop_mode_always_reads_a_fresh_copy_even_with_the_old_setting_off(self):
        import os
        os.environ["CACHE_BUSTER"] = "0"                                       # the leftover that caused 30-second steps
        try:
            fresh = load()
            seen = []
            body = json.dumps({"products": shop(False, False, False, False)}).encode()
            fresh.fetch_page_retry = lambda url: (seen.append(url) or (body, url))
            fresh.start_drop(10, sticky=True)
            fresh.fetch_products_once()
            self.assertIn("&_=", seen[-1])
            fresh.end_drop()
            fresh.fetch_products_once()
            self.assertNotIn("&_=", seen[-1])                                    # calm mode stays gentle
            fresh.start_drop(10, sticky=True)
            fresh.note_rate_limited()                                            # the shop is pushing back: be gentle
            fresh.fetch_products_once()
            self.assertNotIn("&_=", seen[-1])
        finally:
            os.environ.pop("CACHE_BUSTER", None)









    # ---- buy buttons stop the alarm ----


    def test_tapping_a_buy_button_stops_a_ringing_alarm(self):
        self.alarm_setup()
        self.r.go_token()
        def sleeper(sec):
            if len(self.pushes) >= 1 and not self.r.ALARM["acked"]:
                self.r.go_target(self.r.GO["token"], "10")                       # you tapped ⚡ KH1
        self.r.time.sleep = sleeper
        self.drop_happens()
        self.assertEqual(len(self.pushes), 1)
        self.assertTrue(self.r.user_awake())

    def test_a_buy_link_with_a_wrong_token_still_buys_but_does_not_count_as_you(self):
        self.alarm_setup()
        self.r.go_token()
        self.r.check_stock()
        self.assertIn("/cart/10:1", self.r.go_target("nope", "10"))
        self.assertFalse(self.r.user_awake())
        self.assertEqual(self.r.go_target(self.r.GO["token"], "424242"), self.r.SITE)     # unknown size: the shop
        self.assertEqual(self.r.go_target(self.r.GO["token"], ""), self.r.SITE)

    def test_buy_links_redirect_over_the_web(self):
        import http.client
        self.alarm_setup()
        base = self.serve()
        self.r.check_stock()
        host, port = base.replace("http://", "").split(":")
        conn = http.client.HTTPConnection(host, int(port), timeout=10)
        conn.request("GET", f"/go/{self.r.go_token()}/10")
        reply = conn.getresponse(); reply.read()
        self.assertEqual(reply.status, 302)
        self.assertIn("/cart/10:1", reply.getheader("Location"))
        self.assertTrue(self.r.user_awake())

    def test_the_buy_token_is_kept(self):
        token = self.r.go_token()
        self.assertEqual(json.load(open(self.r.META_FILE))["go_token"], token)
        self.assertGreaterEqual(len(token), 10)

    # ---- the calmer alarm ----
    def test_the_alarm_defaults(self):
        fresh = load()
        self.assertEqual((fresh.ALARM_RINGS, fresh.BARK_EVERY, fresh.ALARM_AWAKE_MINUTES), (4, 60, 30.0))

    def test_it_rings_once_a_minute_and_at_most_four_times(self):
        self.alarm_setup()
        sleeps = []
        self.r.time.sleep = lambda sec: sleeps.append(sec)
        self.drop_happens()
        self.assertEqual(len(self.pushes), 4)
        self.assertEqual(len(sleeps), 4 * 60)
        self.assertFalse(self.r.ALARM["running"])
        self.assertEqual([x for x in self.texts() if "Alarm stopped" in x], [])     # no extra message

    def test_it_stops_ringing_when_everything_has_sold_out(self):
        self.alarm_setup()
        state = {"n": 0}
        def sleeper(sec):
            state["n"] += 1
            if state["n"] == 10:
                self.world["products"] = shop(False, False, False, False)
                self.r.check_stock()
        self.r.time.sleep = sleeper
        self.drop_happens()
        self.assertEqual(len(self.pushes), 1)
        self.assertLess(state["n"], 20)                                             # stopped within a second

    def test_a_second_deck_while_ringing_does_not_start_a_second_alarm(self):
        self.alarm_setup()
        state = {"n": 0}
        def sleeper(sec):
            state["n"] += 1
            if state["n"] == 10:
                self.world["products"] = shop(True, False, True, False)
                self.r.check_stock()
            if len(self.pushes) >= 2 and not self.r.ALARM["acked"]:
                self.tap_the_alert()
        self.r.time.sleep = sleeper
        self.drop_happens()
        self.assertEqual(len(self.pushes), 2)
        self.assertIn("12", self.r.ALARM["ids"])

    def test_tapping_the_alert_with_one_deck_opens_its_checkout_for_your_size(self):
        self.alarm_setup()
        self.r.META["my_sizes"] = ["KL1"]
        self.tapped_after(1)
        self.r.check_stock()
        self.world["products"] = shop(True, False, True, False)
        self.r.check_stock()
        self.assertIn("/cart/12:1", self.r.ALARM["buy"])

    def test_tapping_the_alert_with_several_decks_opens_the_telegram_chat(self):
        self.alarm_setup()
        self.r.BOT["username"] = "knife_radar_bot"
        self.tapped_after(1)
        self.world["products"] = shop(False, False, False, False, extra=[self.second_deck(False, False, False, False)])
        self.r.check_stock()
        self.world["products"] = shop(True, False, False, False, extra=[self.second_deck(True, False, False, False)])
        self.r.check_stock()
        self.assertEqual(self.r.ALARM["buy"], "https://t.me/knife_radar_bot")

    def test_a_test_alarm_rings_twice_at_most(self):
        self.alarm_setup(on=False)
        calls = self.tap("al:test")
        self.assertEqual(len(self.pushes), 2)
        self.assertTrue(any("Test finished" in p["text"] for m, p in calls if m == "sendMessage"))

    def test_the_alarm_screen_buttons(self):
        self.alarm_setup(on=False, hours="23-9")
        self.r.handle_alarm("")
        message = [p for m, p in self.sent if m == "sendMessage"][-1]
        self.assertEqual(self.labels(message["reply_markup"]),
                         ["📲 Alarm: Off", "🌙 Only 23:00-09:00: On", "🎯 My sizes", "🧪 Test", "◀ Panel"])
        self.assertIn("at most 4", message["text"])
        self.assertIn("when the drop is over", message["text"])

    # ---- my sizes ----
    def test_my_sizes_start_strictly_on_kh1_and_kl2(self):
        for k in ("sizes_v67", "my_sizes"):
            self.r.META.pop(k, None)
        self.assertEqual(self.r.my_sizes(), ["KH1", "KL2"])
        self.r.META["my_sizes"] = ["KH1", "KL2", "KL1", "KB1"]                      # something older was saved
        self.r.META.pop("sizes_v67")
        self.assertEqual(self.r.my_sizes(), ["KH1", "KL2"])                         # still starts on KH1 and KL2
        self.r.handle_sizes("KH1 KL2 KL1")                                           # until you switch more on
        self.assertEqual(self.r.my_sizes(), ["KH1", "KL2", "KL1"])
        self.assertEqual(json.load(open(self.r.META_FILE))["my_sizes"], ["KH1", "KL2", "KL1"])

    def test_kl1_and_kb1_send_nothing_on_a_fresh_start(self):
        for k in ("sizes_v67", "my_sizes"):
            self.r.META.pop(k, None)
        self.r.META["board"] = True
        self.r.check_stock()
        self.world["products"] = shop(False, False, True, True)
        self.sent.clear()
        self.r.check_stock()
        self.assertEqual([m for m, p in self.sent if m in ("sendPhoto", "sendMessage")], [])

    def test_no_card_has_an_i_bought_it_button(self):
        self.assertEqual(open(self.r.__file__).read().count('btn("✅ I bought it")'), 0)

    def test_sizes_buttons_flip_and_keep_at_least_one(self):
        calls = self.tap("sz:KL2")
        self.assertEqual(self.r.my_sizes(), ["KH1", "KL1", "KB1"])
        self.assertTrue(any(m == "editMessageText" for m, p in calls))
        for shape in ("KH1", "KL1"):
            self.tap(f"sz:{shape}")
        calls = self.tap("sz:KB1")
        self.assertEqual(self.r.my_sizes(), ["KB1"])
        self.assertEqual([p["text"] for m, p in calls if m == "answerCallbackQuery"], ["Keep at least one size"])
        self.tap("sz:banana")
        self.assertEqual(self.r.my_sizes(), ["KB1"])

    def test_the_sizes_command(self):
        self.r.handle_sizes("kh1, kl2")
        self.assertEqual(self.r.my_sizes(), ["KH1", "KL2"])
        self.assertIn("KH1 · KL2", self.texts()[-1])
        self.r.handle_sizes("nonsense")
        self.assertIn("Try", self.texts()[-1])
        self.assertEqual(self.r.my_sizes(), ["KH1", "KL2"])
        self.assertIn("sizes", [n for n, _ in self.r.MENU_COMMANDS])

    def test_only_your_sizes_can_wake_you(self):
        self.alarm_setup()
        self.r.META["my_sizes"] = ["KL2"]
        self.drop_happens()                                                          # KH1 restocks
        self.assertEqual(self.pushes, [])
        self.world["products"] = shop(False, True, False, False)                     # KL2 restocks
        self.tapped_after(1)
        self.r.check_stock()
        self.assertEqual(len(self.pushes), 1)

    def test_a_watched_deck_always_wakes_you_and_always_gets_buttons(self):
        self.alarm_setup()
        self.r.META["my_sizes"] = ["KB1"]
        self.r.META["watches"] = ["All Over"]
        self.world["products"] = self.watch_world()
        self.r.check_stock()
        self.world["products"] = self.watch_world(deck_live=True)
        self.tapped_after(1)
        self.r.check_stock()
        self.assertEqual(len(self.pushes), 1)

    def test_the_panel_has_my_sizes_and_warns_when_checkout_is_not_prefilled(self):
        import os
        os.environ.pop("CHECKOUT_EMAIL", None)
        labels = self.labels(self.r.panel_markup())
        self.assertIn("🎯 My sizes", labels)
        self.assertIn("Checkout is not pre-filled", self.r.panel_text())
        os.environ["CHECKOUT_EMAIL"] = "me@example.com"
        try:
            self.assertNotIn("not pre-filled", self.r.panel_text())
        finally:
            os.environ.pop("CHECKOUT_EMAIL", None)


    # ---- tidy drops: one card per deck, gone when sold out ----
    def chat_now(self):
        """What is left in the chat: messages sent minus messages deleted."""
        alive, ids = {}, 100
        for method, params in self.calls:
            if method in ("sendMessage", "sendPhoto"):
                ids += 1
                alive[ids] = params
            elif method == "deleteMessage":
                alive.pop(params["message_id"], None)
        return alive

    def deck(self, handle, title, states, base, image=True):
        names = ["32mm KH1", "32.5mm KL2", "34mm KL1", "34mm KB1"]
        return {"title": title, "handle": handle, "images": [{"src": f"https://img/{handle}.jpg"}] if image else [],
                "variants": [{"id": base + i, "title": n, "price": "60.00", "available": s}
                             for i, (n, s) in enumerate(zip(names, states))]}

    def test_each_deck_gets_its_own_card_with_its_photo_and_sizes(self):
        self.board_setup()
        self.world["products"] = shop(False, False, False, False, extra=[self.second_deck(False, False, False, False)])
        self.r.check_stock()
        self.world["products"] = shop(True, False, True, False, extra=[self.second_deck(True, True, False, False)])
        self.r.check_stock()
        posts = self.posts()
        self.assertEqual([m for m, p in posts], ["sendPhoto", "sendPhoto"])
        self.assertEqual([p["photo"] for m, p in posts], ["https://img/x.jpg", "https://img/y.jpg"])
        first = self.text_of(posts[0])
        self.assertIn("IN STOCK · Test Deck", first)
        self.assertIn("🟢 KH1 · 32mm", first)
        self.assertIn("🟢 KL1 · 34mm", first)
        self.assertNotIn("KL2", first)                                             # sold-out sizes are not listed
        self.assertEqual(self.labels(posts[0][1]["reply_markup"]),
                         ["⚡ KH1 · 32mm", "⚡ KL1 · 34mm", "🛒 Cart page"])
        self.assertIn("Second Deck", self.text_of(posts[1]))

    def test_only_your_sizes_appear_on_a_card(self):
        self.board_setup()
        self.r.META["my_sizes"] = ["KH1", "KL2"]
        self.r.check_stock()
        self.world["products"] = shop(True, True, True, True)
        self.r.check_stock()
        text = self.text_of(self.posts()[0])
        self.assertIn("KH1", text)
        self.assertIn("KL2", text)
        self.assertNotIn("KL1", text)
        self.assertNotIn("KB1", text)
        self.assertEqual(self.labels(self.posts()[0][1]["reply_markup"]),
                         ["⚡ KH1 · 32mm", "⚡ KL2 · 32.5mm", "🛒 Cart page"])

    def test_a_drop_of_only_other_sizes_sends_nothing_at_all(self):
        self.board_setup()
        self.r.META["my_sizes"] = ["KH1", "KL2"]
        self.r.check_stock()
        self.world["products"] = shop(False, False, True, True)                     # only KL1 and KB1 drop
        self.r.check_stock()
        self.r.check_stock()
        self.assertEqual(self.posts(), [])                                           # no card, no ping
        self.assertFalse(self.r.fast_now())                                          # no drop mode for them either
        self.assertTrue(self.r.STATE.get("12"))                                      # noted, so never alerted later

    def test_your_card_vanishes_when_your_sizes_are_gone_even_if_others_remain(self):
        self.board_setup()
        self.r.META["my_sizes"] = ["KH1"]
        self.r.check_stock()
        self.world["products"] = shop(True, False, True, False)
        self.r.check_stock()
        self.world["products"] = shop(False, False, True, False)                     # KH1 gone, KL1 still there
        self.r.check_stock()
        self.assertEqual(len(self.chat_now()), 1)
        self.assertIn("DROP OVER", self.text_of(("sendMessage", list(self.chat_now().values())[0])))

    def test_classic_alerts_also_only_show_your_sizes(self):
        self.board_setup(board=False)
        self.r.META["my_sizes"] = ["KL2"]
        self.r.check_stock()
        self.world["products"] = shop(True, False, False, False)                     # only KH1
        self.r.check_stock()
        self.assertEqual(self.posts(), [])
        self.world["products"] = shop(True, True, False, False)                      # KL2 lands
        self.r.check_stock()
        self.assertEqual(len(self.posts()), 1)
        self.assertIn("KL2", self.text_of(self.posts()[0]))


    def test_a_size_that_sells_out_disappears_from_its_card_quietly(self):
        self.board_setup()
        self.r.check_stock()
        self.world["products"] = shop(True, True, False, False)
        self.r.check_stock()
        self.world["products"] = shop(False, True, False, False)
        self.r.check_stock()
        self.assertEqual(len(self.posts()), 1)                                     # no new message, no ping
        edit = self.edits()[-1]
        self.assertEqual(edit[0], "editMessageCaption")
        self.assertNotIn("KH1", self.text_of(edit))
        self.assertIn("🟢 KL2", self.text_of(edit))
        self.assertEqual(self.labels(edit[1]["reply_markup"])[0], "⚡ KL2 · 32.5mm")

    def test_a_deck_that_sells_out_disappears_from_the_chat(self):
        self.board_setup()
        self.world["products"] = shop(False, False, False, False, extra=[self.second_deck(False, False, False, False)])
        self.r.check_stock()
        self.world["products"] = shop(True, False, False, False, extra=[self.second_deck(True, False, False, False)])
        self.r.check_stock()
        self.world["products"] = shop(False, False, False, False, extra=[self.second_deck(True, False, False, False)])
        self.r.check_stock()
        self.assertEqual([d["message_id"] for d in self.deletes()], [101])        # Test Deck's card is gone
        self.assertEqual(len(self.chat_now()), 1)                                  # only Second Deck is left
        self.assertNotIn("DROP OVER", " ".join(self.text_of(c) for c in self.posts()))

    def test_when_everything_is_gone_one_summary_is_left(self):
        self.board_setup()
        self.world["products"] = shop(False, False, False, False, extra=[self.second_deck(False, False, False, False)])
        self.r.check_stock()
        self.world["products"] = shop(True, True, False, False, extra=[self.second_deck(True, False, False, False)])
        self.r.check_stock()
        self.world["products"] = shop(False, False, False, False, extra=[self.second_deck(False, False, False, False)])
        self.r.check_stock()
        self.r.maybe_drop_reports()
        alive = self.chat_now()
        self.assertEqual(len(alive), 1)
        summary = list(alive.values())[0]
        text = summary.get("text", "")
        self.assertIn("DROP OVER", text)
        self.assertIn("Test Deck", text)
        self.assertIn("Second Deck", text)
        self.assertTrue(any(w in text for w in ("INSANE", "HOT", "WARM", "CHILL")))
        self.assertEqual(self.r.BOARD["cards"], {})
        self.assertTrue(self.r.META["events"][-1]["reported"])                    # history still counts it

    def test_more_sizes_of_a_deck_on_the_screen_are_added_to_its_card(self):
        self.board_setup()
        self.r.check_stock()
        self.world["products"] = shop(True, False, False, False)
        self.r.check_stock()
        self.world["products"] = shop(True, False, True, False)
        self.r.check_stock()
        self.assertEqual(len(self.posts()), 1)
        self.assertIn("🟢 KL1", self.text_of(self.edits()[-1]))

    def test_a_new_deck_mid_drop_gets_its_own_card_and_ping(self):
        self.board_setup()
        self.world["products"] = shop(False, False, False, False, extra=[self.second_deck(False, False, False, False)])
        self.r.check_stock()
        self.world["products"] = shop(True, False, False, False, extra=[self.second_deck(False, False, False, False)])
        self.r.check_stock()
        self.world["products"] = shop(True, False, False, False, extra=[self.second_deck(False, True, False, False)])
        self.r.check_stock()
        self.assertEqual(len(self.posts()), 2)
        self.assertEqual(self.deletes(), [])

    def test_nothing_changing_sends_nothing(self):
        self.board_setup()
        self.r.check_stock()
        self.world["products"] = shop(True, False, False, False)
        self.r.check_stock()
        for _ in range(5):
            self.r.check_stock()
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.edits(), [])

    def test_a_card_that_fails_to_send_is_tried_again(self):
        self.board_setup()
        self.r.check_stock()
        self.world["products"] = shop(True, False, False, False)
        self.api_fail["sends"] = 20
        self.r.check_stock()
        self.assertEqual(self.posts(), [])
        self.assertFalse(self.r.STATE.get("10"))
        self.api_fail["sends"] = 0
        self.r.check_stock()
        self.assertEqual(len(self.posts()), 1)

    def test_a_card_that_cannot_be_edited_is_sent_again(self):
        self.board_setup()
        self.r.check_stock()
        self.world["products"] = shop(True, True, False, False)
        self.r.check_stock()
        self.api_fail["edits"] = 1
        self.world["products"] = shop(False, True, False, False)
        self.r.check_stock()
        self.assertEqual(len(self.posts()), 2)
        self.assertNotIn("KH1", self.text_of(self.posts()[1]))

    def test_a_deck_without_a_photo_gets_a_text_card(self):
        self.board_setup()
        self.world["products"] = [self.deck("plain", "Plain Deck", (False,) * 4, 800, image=False)]
        self.r.check_stock()
        self.world["products"] = [self.deck("plain", "Plain Deck", (True, True, False, False), 800, image=False)]
        self.r.check_stock()
        self.assertEqual(self.posts()[0][0], "sendMessage")
        self.world["products"] = [self.deck("plain", "Plain Deck", (False, True, False, False), 800, image=False)]
        self.r.check_stock()
        self.assertEqual(self.edits()[0][0], "editMessageText")



    def test_with_tidy_drops_off_it_is_classic_alerts_and_a_separate_report(self):
        self.board_setup(board=False)
        self.world["products"] = shop(False, False, False, False, extra=[self.second_deck(False, False, False, False)])
        self.r.check_stock()
        self.world["products"] = shop(True, False, False, False, extra=[self.second_deck(True, False, False, False)])
        self.r.check_stock()
        self.assertEqual(len(self.posts()), 2)
        self.world["products"] = shop(False, False, False, False, extra=[self.second_deck(False, False, False, False)])
        self.r.check_stock()
        self.assertEqual(self.deletes(), [])                                        # classic alerts stay
        self.r.maybe_drop_reports()
        self.assertTrue(any("DROP REPORT" in (p.get("text") or "") for m, p in self.posts()))

    def test_the_tidy_switch_is_on_the_panel_and_typed(self):
        self.board_setup()
        self.assertIn("🧹 Tidy drops: On", self.labels(self.r.panel_markup()))
        self.tap("pn:board")
        self.assertFalse(self.r.META["board"])
        self.r.handle_switch("board", "on")
        self.assertTrue(self.r.META["board"])

    def test_cards_carry_on_after_a_restart(self):
        self.board_setup()
        self.r.check_stock()
        self.world["products"] = shop(True, True, False, False)
        self.r.check_stock()
        saved = json.load(open(self.r.META_FILE))["board_state"]
        self.assertEqual(saved["cards"]["test-deck"]["msg"]["id"], 101)
        fresh = load()
        fresh.META["board_state"] = saved
        fresh.board_load()
        self.assertEqual(sorted(fresh.BOARD["cards"]["test-deck"]["items"]), ["10", "11"])

    def test_a_broken_card_never_stands_between_you_and_an_alert(self):
        self.board_setup()
        self.r.check_stock()
        def broken(items):
            raise RuntimeError("card bug")
        self.r.board_arrive = broken
        self.world["products"] = shop(True, False, False, False)
        self.r.check_stock()
        self.assertEqual(len(self.posts()), 1)
        self.assertIn("DECK IN STOCK", self.text_of(self.posts()[0]))

    def test_a_broken_card_update_never_stops_the_stock_check(self):
        self.board_setup()
        self.r.board_tick = lambda snapshot: (_ for _ in ()).throw(RuntimeError("tick bug"))
        self.r.check_stock()
        self.world["products"] = shop(True, False, False, False)
        self.r.check_stock()
        self.assertTrue(self.r.STATE.get("10"))

    def test_the_test_command_shows_one_sample_card(self):
        self.board_setup()
        self.r.check_stock()
        self.calls.clear()
        self.r.send_test_alerts(list(self.r.CURRENT_SNAPSHOT.values()))
        posts = self.posts()
        self.assertEqual(len(posts), 1)
        text = self.text_of(posts[0])
        self.assertIn("TEST · Test Deck", text)
        self.assertIn("A sample. Nothing just restocked.", text)
        self.assertEqual(self.r.BOARD["cards"], {})                                # nothing remembered

    def test_buy_buttons_go_through_the_radar_when_it_has_an_address(self):
        import os
        self.board_setup()
        os.environ["PUBLIC_URL"] = "https://radar.example"
        self.addCleanup(lambda: os.environ.pop("PUBLIC_URL", None))
        self.r.check_stock()
        self.world["products"] = shop(True, False, False, False)
        self.r.check_stock()
        rows = json.loads(self.posts()[0][1]["reply_markup"])["inline_keyboard"]
        buy = [b for row in rows for b in row if b["text"] == "⚡ KH1 · 32mm"][0]
        self.assertEqual(buy["url"], f"https://radar.example/go/{self.r.GO['token']}/10")
        self.assertIn("/cart/10:1", self.r.go_target(self.r.GO["token"], "10"))

    def test_without_an_address_buy_buttons_go_straight_to_checkout(self):
        import os
        os.environ.pop("PUBLIC_URL", None)
        os.environ.pop("RAILWAY_PUBLIC_DOMAIN", None)
        self.board_setup()
        self.r.check_stock()
        self.world["products"] = shop(True, False, False, False)
        self.r.check_stock()
        rows = json.loads(self.posts()[0][1]["reply_markup"])["inline_keyboard"]
        self.assertIn("/cart/10:1", rows[0][0]["url"])

    def test_the_real_drop_replayed(self):
        """20 Oct: 6 decks, 24 sizes, sold out in 5 minutes. Now: 6 cards with photos, each gone as it sells out,
        and one summary left at the end."""
        self.board_setup()
        names = ["Bloodbath Logo - Slime/Gray", "Poltergeist", "True Crime - Red", "Best Friend - Black", "Tabloid",
                 "John's Nightmare 2"]
        decks = [self.deck(f"d{i}", n, (False,) * 4, 600 + i * 10) for i, n in enumerate(names)]
        self.world["products"] = decks[:4]
        self.r.check_stock()
        def live(n):
            for d in decks[:n]:
                for v in d["variants"]:
                    v["available"] = True
            self.world["products"] = decks[:n]
        live(4); self.r.check_stock()
        self.assertEqual(len(self.chat_now()), 4)
        live(6); self.r.check_stock()
        self.assertEqual(len(self.chat_now()), 6)                                 # one card per deck, each with its photo
        self.assertEqual(len({p["photo"] for m, p in self.posts()}), 6)
        for d in decks:
            d["variants"][0]["available"] = False                                  # KH1 goes everywhere
        self.r.check_stock()
        self.assertTrue(all("KH1" not in self.text_of(e) for e in self.edits()[-6:]))
        for d in decks[:3]:
            for v in d["variants"]:
                v["available"] = False
        self.r.check_stock()
        self.assertEqual(len(self.chat_now()), 3)                                 # three decks gone from the chat
        for d in decks:
            for v in d["variants"]:
                v["available"] = False
        self.r.check_stock()
        alive = list(self.chat_now().values())
        self.assertEqual(len(alive), 1)                                           # just the summary
        self.assertIn("DROP OVER · 6 decks", self.text_of(("sendMessage", alive[0])))

    # ---- Bark sound and loop ----
    def bark_payload(self, **env):
        import io, os, urllib.request
        for k, v in env.items():
            os.environ[k] = v
        try:
            fresh = load()
        finally:
            for k in env:
                os.environ.pop(k, None)
        seen = {}
        class Reply(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): pass
        real = urllib.request.urlopen
        urllib.request.urlopen = lambda request, timeout=0: (seen.update(body=json.loads(request.data.decode())) or Reply(b'{"code": 200}'))
        try:
            fresh.BARK_KEY = "k"
            fresh.bark_push("T", "B")
        finally:
            urllib.request.urlopen = real
        return seen["body"]

    def test_bark_sound_and_loop_have_safe_defaults(self):
        body = self.bark_payload()
        self.assertEqual((body["sound"], body["call"], body["level"]), ("alarm", "1", "critical"))

    def test_bark_sound_can_be_changed(self):
        self.assertEqual(self.bark_payload(BARK_SOUND="minuet")["sound"], "minuet")
        self.assertEqual(self.bark_payload(BARK_SOUND="birdsong.caf")["sound"], "birdsong")
        self.assertEqual(self.bark_payload(BARK_SOUND="  ")["sound"], "alarm")

    def test_bark_can_play_once_instead_of_looping(self):
        self.assertNotIn("call", self.bark_payload(BARK_LOOP="0"))
        self.assertEqual(self.bark_payload(BARK_LOOP="1")["call"], "1")

    def test_one_ring_only(self):
        import os
        os.environ["ALARM_RINGS"] = "1"
        try:
            self.assertEqual(load().ALARM_RINGS, 1)
        finally:
            os.environ.pop("ALARM_RINGS", None)

    # ---- more lettering, picked with a tap ----
    def test_every_font_changes_letters_and_keeps_everything_else(self):
        for name in self.r.FONT_NAMES:
            out = self.r.fancy("Knife Mfg Co 123 · KH1!", name)
            self.assertIn("·", out, name)
            self.assertIn("!", out, name)
            if name != "plain":
                self.assertNotEqual(out, "Knife Mfg Co 123 · KH1!", name)

    def test_the_new_styles_look_right(self):
        f = self.r.fancy
        self.assertEqual(f("Ab", "sansbolditalic"), "𝘼𝙗")
        self.assertEqual(f("Ab", "serifbolditalic"), "𝑨𝒃")
        self.assertEqual(f("Heg", "scriptlight"), "ℋℯℊ")
        self.assertEqual(f("CHz", "gothiclight"), "ℭℌ𝔷")
        self.assertEqual(f("Ab 0 9", "circled"), "Ⓐⓑ ⓪ ⑨")
        self.assertEqual(f("ab 1", "circledblack"), "🅐🅑 ❶")
        self.assertEqual(f("ab", "squared"), "🄰🄱")
        self.assertEqual(f("Tiny q 1", "tiny"), "ᵗⁱⁿʸ q ¹")

    def test_fonts_command_shows_every_style_with_a_button(self):
        self.r.handle_command("/fonts")
        message = [p for m, p in self.sent if m == "sendMessage"][-1]
        for name in self.r.FONT_NAMES:
            self.assertIn(name, message["text"])
        rows = json.loads(message["reply_markup"])["inline_keyboard"]
        data = [b["callback_data"] for row in rows for b in row]
        self.assertEqual(len([d for d in data if d.startswith("ft:") and d != "ft:buttons"]), len(self.r.FONT_NAMES))
        self.assertIn("ft:buttons", data)
        self.assertLess(len(message["text"]), 4000)

    def test_tapping_a_font_changes_the_lettering_straight_away_and_is_kept(self):
        self.r.FONT_STYLE = "plain"
        calls = self.tap("ft:gothic")
        self.assertEqual(self.r.font_style(), "gothic")
        self.assertEqual(self.r.fancy("Ab"), "𝕬𝖇")
        self.assertIn("𝕬𝖇", self.r.render("<h>Ab</h>"))
        self.assertTrue(any(m == "editMessageText" for m, p in calls))
        self.assertEqual(json.load(open(self.r.META_FILE))["font_style"], "gothic")
        self.tap("ft:nonsense")
        self.assertEqual(self.r.font_style(), "gothic")

    def test_buttons_can_stay_plain(self):
        self.tap("ft:script")
        self.assertEqual(self.r.btn("Ab"), "𝓐𝓫")
        self.tap("ft:buttons")
        self.assertEqual(self.r.button_style(), "plain")
        self.assertEqual(self.r.btn("Ab"), "Ab")
        self.tap("ft:buttons")
        self.assertEqual(self.r.btn("Ab"), "𝓐𝓫")

    def test_the_variable_still_works_when_nothing_was_tapped(self):
        self.r.META.pop("font_style", None)
        self.r.FONT_STYLE = "mono"
        self.assertEqual(self.r.font_style(), "mono")
        self.r.FONT_STYLE = "plain"

    def test_fonts_are_in_the_menu_the_help_and_the_panel(self):
        self.assertIn("fonts", [n for n, _ in self.r.MENU_COMMANDS])
        self.assertIn("/fonts", self.r.help_text())
        self.assertIn("🔤 Fonts", self.labels(self.r.panel_markup()))

    # ---- parcels when the tracking service can't be reached ----
    def test_connections_prefer_ipv4_when_a_site_has_it(self):
        import socket
        fake = [(socket.AF_INET6, 1, 6, "", ("2606::1", 443, 0, 0)), (socket.AF_INET, 1, 6, "", ("1.2.3.4", 443))]
        real = self.r._GETADDRINFO
        self.r._GETADDRINFO = lambda *a, **k: fake
        try:
            self.assertEqual([r[0] for r in self.r._ipv4_first("x", 443)], [socket.AF_INET])
            only6 = fake[:1]
            self.r._GETADDRINFO = lambda *a, **k: only6
            self.assertEqual(self.r._ipv4_first("x", 443), only6)                 # no IPv4: keep what there is
            self.r.PREFER_IPV4 = False
            self.r._GETADDRINFO = lambda *a, **k: fake
            self.assertEqual(len(self.r._ipv4_first("x", 443)), 2)
        finally:
            self.r._GETADDRINFO = real
            self.r.PREFER_IPV4 = True

    def parcels_down(self):
        import urllib.error
        self.r.PARCELS_KEY = "key"
        self.r.live_tracking = lambda: True
        self.r.META["parcels"] = [
            {"number": "9400111899223344556677", "label": "a deck", "added": time.time(), "status": "", "event": "",
             "where": "", "when": "", "eta": "", "delivered": False}]
        def down(number):
            self.r.TRACK_HEALTH["service"] = "Parcels App"
            raise urllib.error.URLError("[Errno 101] Network is unreachable")
        self.r.parcel_info = down

    def test_an_unreachable_service_is_not_called_waiting_for_the_first_scan(self):
        self.parcels_down()
        self.r.poll_parcels()
        text = self.r.parcels_text()
        self.assertIn("couldn't check yet", text)
        self.assertNotIn("waiting for the first scan", text)

    def test_after_three_failures_it_says_so_once_with_the_parcels_own_page(self):
        self.parcels_down()
        for _ in range(5):
            self.r.poll_parcels()
        notes = [p for m, p in self.sent if m == "sendMessage" and "can't reach the tracking service" in p["text"]]
        self.assertEqual(len(notes), 1)
        rows = json.loads(notes[0]["reply_markup"])["inline_keyboard"]
        self.assertIn("tools.usps.com", rows[0][0]["url"])
        self.assertIn("Network is unreachable", notes[0]["text"])

    def test_once_it_works_again_the_status_is_real_and_a_new_outage_is_reported_again(self):
        self.parcels_down()
        for _ in range(3):
            self.r.poll_parcels()
        self.r.parcel_info = lambda number: {"status": "InTransit", "event": "Arrived at USPS Facility",
                                             "where": "CHICAGO IL", "when": "", "eta": ""}
        self.r.poll_parcels()
        self.assertIn("on the way", self.r.parcels_text())
        self.assertEqual(self.r.TRACK_HEALTH["net_fails"], 0)
        self.parcels_down()
        self.r.META["parcels"][0]["checked"] = time.time()
        self.sent.clear()
        for _ in range(3):
            self.r.poll_parcels()
        self.assertEqual(len([p for m, p in self.sent if "can't reach the tracking service" in p.get("text", "")]), 1)

    # ---- USPS: say exactly why it refuses ----
    def usps_world(self, replies):
        """replies: list of (status, body) for each USPS request in order."""
        import io, urllib.error, urllib.request
        seen = []
        script = list(replies)
        class Reply(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): pass
        def fake(request, timeout=0):
            seen.append((request.full_url, request.data))
            status, body = script.pop(0)
            if status == 200:
                return Reply(json.dumps(body).encode())
            raise urllib.error.HTTPError(request.full_url, status, "nope", {}, io.BytesIO(json.dumps(body).encode()))
        self.real_urlopen = urllib.request.urlopen
        urllib.request.urlopen = fake
        self.addCleanup(lambda: setattr(urllib.request, "urlopen", self.real_urlopen))
        self.r.USPS_CLIENT_ID, self.r.USPS_CLIENT_SECRET = "ID123", "SECRET456"
        self.r.USPS_TOKEN.update(value="", until=0)
        logs = []
        self.r.log = lambda msg: logs.append(msg)
        return seen, logs

    def test_a_refused_sign_in_is_logged_with_uspss_reason_and_no_secret(self):
        refusal = (401, {"error": "invalid_client", "error_description": "bad SECRET456"})
        seen, logs = self.usps_world([refusal] * 4)
        with self.assertRaises(Exception):
            self.r.usps_token()
        self.assertEqual(len(seen), 4)                                              # 2 formats x with/without scope
        self.assertTrue(any("invalid_client" in x for x in logs))
        self.assertFalse(any("SECRET456" in x for x in logs))
        self.assertNotIn(b"scope", seen[-1][1])                                      # the last try has no scope

    def test_sign_in_works_without_the_tracking_scope_if_needed(self):
        refusal = (401, {"error": "invalid_scope"})
        seen, logs = self.usps_world([refusal, refusal, (200, {"access_token": "T", "expires_in": 3600})])
        self.assertEqual(self.r.usps_token(), "T")

    def test_a_refused_tracking_call_is_logged_with_the_reason(self):
        token = (200, {"access_token": "T", "expires_in": 3600})
        refused = (403, {"error": {"code": "403", "message": "App not authorized for Tracking"}})
        seen, logs = self.usps_world([token, refused])
        with self.assertRaises(Exception):
            self.r.usps_call("LB487586596US")
        self.assertTrue(any("tracking refused (403)" in x and "not authorized" in x for x in logs))

    # ---- USPS Tracking 3.2 ----
    def test_tracking_uses_usps_3_2_as_usps_asked(self):
        token = (200, {"access_token": "T", "expires_in": 3600})
        reply = (200, [{"trackingNumber": "LB487586596US", "statusCategory": "In Transit",
                        "statusSummary": "Your item arrived at our CHICAGO IL DISTRIBUTION CENTER",
                        "trackingEvents": [{"eventType": "Arrived at USPS Facility", "eventTimestamp": "2026-10-28T20:06:00Z",
                                            "eventCity": "CHICAGO", "eventState": "IL"}]}])
        seen, logs = self.usps_world([token, reply])
        data = self.r.usps_call("LB487586596US")
        url, body = seen[-1]
        self.assertEqual(url, "https://apis.usps.com/tracking/v3r2/tracking")
        self.assertEqual(json.loads(body.decode()), [{"trackingNumber": "LB487586596US"}])
        info = self.r.parse_usps_reply(data)
        self.assertEqual(info["status"], "InTransit")
        self.assertIn("Arrived at USPS Facility", info["event"])
        self.assertEqual(info["where"], "CHICAGO, IL")

    def test_usps_3_2_answers_in_any_shape_are_understood(self):
        one = {"trackingNumber": "LB1US", "statusCategory": "Delivered", "trackingEvents": []}
        two = {"trackingNumber": "LB2US", "statusCategory": "In Transit", "trackingEvents": []}
        self.assertEqual(self.r.usps_pick(one, "LB1US")["statusCategory"], "Delivered")
        self.assertEqual(self.r.usps_pick([two, one], "lb1us")["statusCategory"], "Delivered")
        self.assertEqual(self.r.usps_pick({"trackingResults": [two, one]}, "LB2US")["statusCategory"], "In Transit")
        self.assertIsNone(self.r.usps_pick([], "LB1US"))
        self.assertIsNone(self.r.usps_pick(None, "LB1US"))
        self.r.log = lambda m: None
        self.assertIsNone(self.r.usps_pick([{"trackingNumber": "LB1US", "error": {"message": "not found"}}], "LB1US"))

    # ---- parcels as tap-to-track buttons (USPS charges US$599+/month to track others' parcels) ----
    def test_parcels_are_buttons_only_by_default(self):
        fresh = load()
        self.assertFalse(fresh.PARCEL_AUTO)
        fresh.USPS_CLIENT_ID, fresh.USPS_CLIENT_SECRET, fresh.PARCELS_KEY = "a", "b", "c"
        self.assertFalse(fresh.live_tracking())                        # keys alone don't switch it on

    def test_the_parcel_list_is_clean_with_buttons_and_copyable_numbers(self):
        self.r.PARCEL_AUTO = False
        self.r.META["parcels"] = [
            {"number": "LB487586596US", "label": "a deck", "added": time.time(), "status": "", "event": "",
             "where": "", "when": "", "eta": "", "delivered": False},
            {"number": "LB487586605US", "label": "a deck", "added": time.time(), "status": "", "event": "",
             "where": "", "when": "", "eta": "", "delivered": False}]
        text = self.r.parcels_text()
        self.assertIn("<code>LB487586596US</code>", text)
        for gone in ("couldn't check", "waiting for the first scan", "keys"):
            self.assertNotIn(gone, text)
        rows = self.r.parcels_markup()["inline_keyboard"]
        self.assertEqual(len(rows), 3)
        self.assertIn("tools.usps.com", rows[0][0]["url"])
        self.assertIn("LB487586596US", rows[0][0]["url"])
        self.assertIn("parcelsapp.com", rows[0][1]["url"])
        self.assertEqual(rows[-1][0]["url"], "https://www.singpost.com/track-items")

    def test_track_saves_and_offers_buttons_without_any_checking(self):
        self.r.PARCEL_AUTO = False
        called = []
        self.r.parcel_info = lambda n: called.append(n)
        self.r.handle_track("LB487586596US a deck")
        message = [p for m, p in self.sent if m == "sendMessage"][-1]
        self.assertIn("Parcel saved", message["text"])
        self.assertNotIn("USPS_CLIENT", message["text"])
        labels = self.labels(message["reply_markup"])
        self.assertIn("🔎 USPS page", labels)
        self.assertIn("🇸🇬 SingPost", labels)
        self.assertEqual(called, [])

    def test_nothing_is_polled_and_no_daily_summary_when_buttons_only(self):
        self.r.PARCEL_AUTO = False
        called = []
        self.r.parcel_info = lambda n: called.append(n)
        self.r.META["parcels"] = [{"number": "LB487586596US", "label": "a deck", "added": time.time(), "status": "",
                                   "event": "", "where": "", "when": "", "eta": "", "delivered": False}]
        self.r.poll_parcels()
        self.r.PARCEL_DAILY_HOUR = 9
        self.r.maybe_parcel_digest()
        self.assertEqual(called, [])
        self.assertEqual([p for m, p in self.sent if "Parcel update" in p.get("text", "")], [])

    # ---- my pickups ----
    def alert_for(self, *states):
        self.r.check_stock()
        self.world["products"] = shop(*states)
        self.r.check_stock()
        photo = [p for m, p in self.sent if m == "sendPhoto"][0]
        self.sent.clear()
        return json.loads(photo["reply_markup"])["inline_keyboard"]

    def texts(self):
        return [p.get("text", "") for m, p in self.sent if m == "sendMessage"]

    def test_alerts_no_longer_have_an_i_bought_it_button(self):
        rows = self.alert_for(True, False, False, False)
        labels = [b["text"] for row in rows for b in row]
        self.assertNotIn("✅ I bought it", labels)
        self.assertFalse(any(b.get("callback_data", "").startswith("b:") for row in rows for b in row))

    def test_one_size_in_stock_counts_straight_away_and_congratulates(self):
        self.alert_for(True, False, False, False)
        toast = self.r.handle_bought_tap("b:10")                         # an old button still in the chat
        self.assertIn("Counted", toast)
        self.assertEqual(len(self.r.META["purchases"]), 1)
        text = self.texts()[-1]
        self.assertIn("CONGRATS", text)
        self.assertIn("Pickup #1", text)
        self.assertIn("Milestone: 1 deck", text)

    def test_two_sizes_in_stock_asks_which_size_first(self):
        self.alert_for(True, False, True, False)
        toast = self.r.handle_bought_tap("b:10,12")
        self.assertIn("Pick your size", toast)
        self.assertEqual(len(self.r.META["purchases"]), 0)
        question = [p for m, p in self.sent if m == "sendMessage"][-1]
        choices = [b for row in json.loads(question["reply_markup"])["inline_keyboard"] for b in row]
        self.assertEqual([b["text"] for b in choices], ["KH1 · 32mm", "KL1 · 34mm", "🤷 Not sure"])
        self.r.handle_bought_tap(choices[1]["callback_data"])
        entry = self.r.META["purchases"][0]
        self.assertEqual((entry["shape"], entry["size"]), ("KL1", "34mm"))
        self.assertGreater(entry["sgd"], 0)

    def test_a_double_tap_is_counted_once(self):
        self.alert_for(True, False, False, False)
        data = "b:10"
        self.r.handle_bought_tap(data)
        self.assertIn("Already counted", self.r.handle_bought_tap(data))
        self.assertEqual(len(self.r.META["purchases"]), 1)

    def test_a_test_alert_never_counts(self):
        self.r.check_stock()
        self.sent.clear()
        self.r.send_test_alert()
        self.assertIn("only a test", self.r.handle_bought_tap("b:test"))
        self.assertEqual(self.r.META["purchases"], [])

    def test_bought_command_with_a_note_and_stats_and_undo(self):
        self.r.handle_bought_command("All Over 2 KL1")
        self.r.handle_bought_command("Receiver - Black")
        self.assertEqual(len(self.r.META["purchases"]), 2)
        stats = self.r.stats_text()
        self.assertIn("<b>2</b> decks bought", stats)
        self.assertIn("this month · 2", stats)
        self.sent.clear()
        self.r.handle_undo()                                            # asks first
        self.assertEqual(len(self.r.META["purchases"]), 2)
        self.assertIn("Remove your last pickup?", self.texts()[-1])
        self.r.handle_callback({"id": "q", "data": "undo:yes", "message": {"chat": {"id": 42}, "message_id": 1}})
        self.assertEqual(len(self.r.META["purchases"]), 1)
        self.assertIn("You now have <b>1</b>", self.texts()[-1])

    def test_keeping_the_last_pickup_changes_nothing(self):
        self.r.handle_bought_command("All Over 2 KL1")
        self.r.handle_callback({"id": "q", "data": "x:0", "message": {"chat": {"id": 42}, "message_id": 1}})
        self.assertEqual(len(self.r.META["purchases"]), 1)

    def test_bought_with_nothing_after_it_asks_which_size_of_the_latest_drop(self):
        self.r.check_stock()
        self.world["products"] = shop(True, False, True, False)
        self.r.check_stock()
        self.sent.clear()
        self.r.handle_bought_command("")
        self.assertEqual(self.r.META["purchases"], [])                  # a menu tap never counts by itself
        question = [p for m, p in self.sent if m == "sendMessage"][-1]
        buttons = [b for row in json.loads(question["reply_markup"])["inline_keyboard"] for b in row]
        self.assertEqual([b["text"] for b in buttons], ["KH1 · 32mm", "KL1 · 34mm", "🤷 Something else", "✖ Cancel"])
        self.r.handle_bought_tap(buttons[1]["callback_data"])
        self.assertEqual(self.r.META["purchases"][0]["shape"], "KL1")

    def test_a_size_chosen_from_an_old_drop_still_counts_after_the_shop_changed(self):
        self.r.check_stock()
        self.world["products"] = shop(True, False, False, False)
        self.r.check_stock()
        vid = next(iter(self.r.META["events"][-1]["items"]))
        self.r.CURRENT_SNAPSHOT = {}                                    # the shop list has moved on
        self.r.handle_bought_tap(f"c:{vid}")
        self.assertEqual(self.r.META["purchases"][0]["shape"], "KH1")

    def test_bought_with_no_recent_drop_explains_how(self):
        self.r.handle_bought_command("")
        self.assertEqual(self.r.META["purchases"], [])
        self.assertIn("/bought", self.texts()[-1])

    def test_the_menu_has_every_command(self):
        got = {}
        def api(method, params=None, _retry=True):
            got[method] = params
            return {"ok": True, "result": {"username": "bot"} if method == "getMe" else []}
        self.r.telegram_api = api
        self.r.setup_telegram()
        names = [c["command"] for c in json.loads(got["setMyCommands"]["commands"])]
        self.assertEqual(names, [n for n, _ in self.r.MENU_COMMANDS])
        for needed in ("status", "drop", "calm", "forecast", "history", "smart", "stats", "bought", "undo",
                       "parcels", "track", "untrack", "password", "test", "info", "help"):
            self.assertIn(needed, names)

    def test_help_lists_every_menu_command(self):
        text = self.r.help_text()
        for name, _ in self.r.MENU_COMMANDS:
            self.assertIn(f"/{name}", text)

    def test_stats_when_there_are_no_pickups_yet(self):
        self.assertIn("No pickups yet", self.r.stats_text())
        self.sent.clear()
        self.r.handle_undo()
        self.assertIn("Nothing to undo", self.texts()[-1])

    def test_pickups_are_saved_to_the_memory_file(self):
        self.r.handle_bought_command("Receiver - Black")
        saved = json.load(open(self.r.META_FILE))
        self.assertEqual(saved["purchases"][0]["product"], "Receiver - Black")

    def test_milestones_get_a_special_line(self):
        for _ in range(4):
            self.r.record_purchase(None, "x")
        count, entry = self.r.record_purchase(None, "x")
        self.assertIn("Milestone: 5 decks", self.r.congrats_text(count, entry))

    # ---- parcels (Parcels App and USPS) ----
    def parcels_reply(self, status="in_transit", state="Arrived at sorting centre", location="Singapore",
                      date="2026-10-12T07:42:00.000Z", done=True, number="SG1234567890"):
        result = {"tracking_number": number, "state": "completed" if done else "processing", "from_cache": False}
        if done:
            result["shipment"] = {"trackingId": number, "status": status, "states": [
                {"state": "Accepted at origin", "location": "Portland, OR, United States",
                 "date": "2026-10-10T20:00:00.000Z", "carrier": 0},
                {"state": state, "location": location, "date": date, "carrier": 0},
            ]}
        return {"request_id": "req1", "status": "completed" if done else "processing",
                "done": done, "expires_at": "2026-10-12T08:12:00.000Z", "results": [result]}

    def with_parcels(self, *replies):
        """Switches Parcels App on with a fake that answers with the given replies, one per call."""
        calls = []
        queue = list(replies)
        def fake(method, path, payload=None, timeout=25):
            calls.append((method, path, payload))
            return queue.pop(0) if len(queue) > 1 else queue[0]
        self.r.PARCELS_KEY = "key"
        self.r.parcels_call = fake
        self.r.ASYNC_FIRST_LOOKUP = False
        return calls

    def test_parcels_reply_is_understood_and_shown_in_your_time_zone(self):
        info = self.r.parse_parcels_reply(self.parcels_reply(), "SG1234567890")
        self.assertEqual(info["status"], "InTransit")
        self.assertEqual(info["event"], "Arrived at sorting centre")          # newest scan, whatever the order
        self.assertEqual(info["where"], "Singapore")
        self.assertEqual(self.r.fmt_iso("2026-10-12T07:42:00Z"), "12 Oct, 15:42")     # UTC+8
        self.assertIsNone(self.r.parse_parcels_reply({"results": []}, "X"))
        self.assertIsNone(self.r.parse_parcels_reply({"results": [{"tracking_number": "X", "state": "processing"}]}, "X"))

    def test_parcels_status_words(self):
        status = self.r.parcels_status
        self.assertEqual(status("delivered", "Delivered, In/At Mailbox"), "Delivered")
        self.assertEqual(status("in_transit", "Out for Delivery"), "OutForDelivery")
        self.assertEqual(status("pickup", ""), "AvailableForPickup")
        self.assertEqual(status("exception", ""), "Exception")
        self.assertEqual(status("expired", ""), "Expired")
        self.assertEqual(status("in_transit", "Arrived"), "InTransit")
        self.assertEqual(status("", ""), "")

    def test_a_lookup_with_an_error_is_ignored(self):
        reply = self.parcels_reply()
        reply["results"][0]["shipment"] = {"trackingId": "SG1234567890", "error": "ADDITIONAL_FIELDS_REQUIRED", "states": []}
        self.assertIsNone(self.r.parse_parcels_reply(reply, "SG1234567890"))

    def test_the_request_asks_for_the_right_country_and_waits_for_the_answer(self):
        calls = self.with_parcels(self.parcels_reply(done=False), self.parcels_reply())
        info = self.r.parcels_info("SG1234567890")
        self.assertEqual(info["status"], "InTransit")
        method, path, payload = calls[0]
        self.assertEqual((method, path), ("POST", "/trackings"))
        self.assertEqual(payload["shipments"][0], {"tracking_number": "SG1234567890", "destination_country": "SG"})
        self.assertEqual(calls[1][:2], ("GET", "/trackings/req1"))

    def test_track_command_starts_following_and_shows_the_first_update(self):
        self.with_parcels(self.parcels_reply())
        self.r.record_purchase(None, "Receiver - Black")
        self.sent.clear()
        self.r.handle_track("SG1234567890")
        texts = self.texts()
        self.assertTrue(any("Tracking started" in x for x in texts))
        self.assertTrue(any("FIRST UPDATE" in x and "on the way" in x for x in texts))
        self.assertEqual(self.r.META["parcels"][0]["label"], "Receiver - Black")

    def test_you_are_told_when_something_changes_and_not_when_nothing_does(self):
        self.with_parcels(self.parcels_reply(), self.parcels_reply(), self.parcels_reply(state="Out with the courier"))
        self.r.handle_track("SG1234567890 All Over 2")
        self.sent.clear()
        self.r.poll_parcels()
        self.assertEqual(self.texts(), [])                         # same as before: silent
        self.r.poll_parcels()
        self.assertTrue(any("PARCEL UPDATE" in x and "Out with the courier" in x for x in self.texts()))

    def test_delivered_parcels_get_a_cheer_and_stop_being_checked(self):
        calls = self.with_parcels(self.parcels_reply(), self.parcels_reply(status="delivered", state="Delivered"))
        self.r.handle_track("SG1234567890 All Over 2")
        self.sent.clear()
        self.r.poll_parcels()
        self.assertTrue(any("DELIVERED" in x and "All Over 2" in x for x in self.texts()))
        self.assertTrue(self.r.META["parcels"][0]["delivered"])
        before = len(calls)
        self.r.poll_parcels()
        self.assertEqual(len(calls), before)                      # no more checking

    def test_without_any_key_the_parcel_is_saved_with_a_link_only(self):
        self.r.PARCELS_KEY = ""
        self.r.USPS_CLIENT_ID = self.r.USPS_CLIENT_SECRET = ""
        called = []
        self.r.parcels_call = lambda *a, **k: called.append(1)
        self.r.handle_track("SG1234567890 All Over 2")
        self.assertEqual(called, [])
        self.assertIn("Tap a button to see where it is", self.texts()[-1])
        button = [p for m, p in self.sent if m == "sendMessage"][-1]["reply_markup"]
        self.assertIn("parcelsapp.com/en/tracking/SG1234567890", button)
        self.assertEqual(len(self.r.META["parcels"]), 1)

    def test_bad_and_duplicate_tracking_numbers(self):
        self.r.PARCELS_KEY = ""
        self.r.handle_track("12")
        self.assertIn("doesn't look like", self.texts()[-1])
        self.r.handle_track("")
        self.assertIn("/track", self.texts()[-1])
        self.r.handle_track("SG1234567890")
        self.r.handle_track("sg1234567890")
        self.assertIn("already following", self.texts()[-1])
        self.assertEqual(len(self.r.META["parcels"]), 1)

    def test_untrack_and_the_parcel_list(self):
        self.r.PARCELS_KEY = ""
        self.assertIn("Nothing on its way", self.r.parcels_text())
        self.r.handle_track("SG1234567890 All Over 2")
        self.assertIn("All Over 2", self.r.parcels_text())
        self.r.handle_untrack("SG1234567890")
        self.assertEqual(self.r.META["parcels"], [])
        self.r.handle_untrack("nope")
        self.assertIn("couldn't find", self.texts()[-1])

    def test_a_tracking_service_outage_never_breaks_anything(self):
        self.with_parcels(self.parcels_reply())
        self.r.handle_track("SG1234567890 All Over 2")
        def boom(*a, **k): raise OSError("tracking service down")
        self.r.parcels_call = boom
        self.sent.clear()
        self.r.poll_parcels()                                       # must not raise
        self.assertEqual(self.texts(), [])

    def test_one_parcel_summary_a_day_only_while_something_is_on_its_way(self):
        self.with_parcels(self.parcels_reply())
        self.r.PARCEL_DAILY_HOUR = time.gmtime(time.time() + 8 * 3600).tm_hour
        self.r.maybe_parcel_digest()
        self.assertEqual(self.texts(), [])                          # nothing to report yet
        self.r.handle_track("SG1234567890 All Over 2")
        self.sent.clear()
        self.r.maybe_parcel_digest()
        self.r.maybe_parcel_digest()
        self.assertEqual(sum("Parcel update" in x for x in self.texts()), 1)

    def test_a_usps_number_gets_the_public_usps_page_button_without_any_account(self):
        self.r.USPS_CLIENT_ID = self.r.USPS_CLIENT_SECRET = ""
        for number in ("9400111899223033005282", "LZ123456789US", "8212345678"):
            self.assertTrue(self.r.looks_like_usps(number), number)
            row = self.r.parcel_markup(number)["inline_keyboard"][0]
            self.assertEqual([b["text"] for b in row], ["🔎 USPS page", "🌏 Parcels"])
            self.assertIn("tools.usps.com", row[0]["url"])

    def test_other_numbers_only_get_the_parcels_page_button(self):
        self.r.USPS_CLIENT_ID = self.r.USPS_CLIENT_SECRET = ""
        for number in ("1Z999AA10123456784", "SG1234567890", "12345678"):
            self.assertFalse(self.r.looks_like_usps(number), number)
            row = self.r.parcel_markup(number)["inline_keyboard"][0]
            self.assertEqual([b["text"] for b in row], ["🔎 Track page"])

    def raise_http(self, code, service="Parcels App"):
        import urllib.error
        def boom(*a, **k):
            self.r.TRACK_HEALTH["service"] = service            # the real calls set this before asking
            raise urllib.error.HTTPError("u", code, "x", {}, None)
        return boom

    def test_a_rejected_parcels_key_is_explained_once_with_the_variable_to_fix(self):
        self.with_parcels(self.parcels_reply())
        self.r.handle_track("SG1234567890 All Over 2")
        self.r.parcels_call = self.raise_http(401)
        self.sent.clear()
        self.r.poll_parcels()
        self.r.poll_parcels()
        notes = [x for x in self.texts() if "didn't accept the key" in x]
        self.assertEqual(len(notes), 1)
        self.assertIn("PARCELS_API_KEY", notes[0])
        self.assertIn("Parcels App", self.r.TRACK_HEALTH["error"])

    def test_rejected_usps_keys_are_explained_with_both_variable_names(self):
        self.r.USPS_CLIENT_ID, self.r.USPS_CLIENT_SECRET = "developers.usps.com", "x"
        self.r.PARCELS_KEY = ""
        self.r.META["parcels"] = [{"number": "LB487181429US", "label": "Deck", "delivered": False}]
        self.r.usps_call = self.raise_http(401, "USPS")
        self.sent.clear()
        self.r.poll_parcels()
        note = [x for x in self.texts() if "USPS didn't accept" in x][0]
        self.assertIn("USPS_CLIENT_ID", note)
        self.assertIn("USPS_CLIENT_SECRET", note)

    def test_after_a_good_check_the_problem_is_cleared_and_can_be_reported_again(self):
        calls = self.with_parcels(self.parcels_reply())
        self.r.handle_track("SG1234567890 All Over 2")
        good = self.r.parcels_call
        self.r.parcels_call = self.raise_http(401)
        self.r.poll_parcels()
        self.assertTrue(self.r.TRACK_HEALTH["told"])
        self.r.parcels_call = good
        self.r.poll_parcels()
        self.assertEqual(self.r.TRACK_HEALTH["error"], "")
        self.assertFalse(self.r.TRACK_HEALTH["told"])

    def test_a_used_up_plan_gets_its_own_message(self):
        self.with_parcels(self.parcels_reply())
        self.r.handle_track("SG1234567890 All Over 2")
        self.r.parcels_call = self.raise_http(402)
        self.sent.clear()
        self.r.poll_parcels()
        self.assertTrue(any("plan used up" in x or "Plan used up" in x for x in self.texts()))

    def test_info_shows_which_tracking_keys_are_set(self):
        self.r.PARCELS_KEY = ""
        self.r.USPS_CLIENT_ID = self.r.USPS_CLIENT_SECRET = ""
        self.assertIn("USPS keys no · Parcels key no", self.r.info_text())
        self.r.PARCELS_KEY = "k"
        self.r.TRACK_HEALTH["error"] = "Parcels App: HTTP Error 401"
        info = self.r.info_text()
        self.assertIn("Parcels key yes", info)
        self.assertIn("last tracking problem", info)

    # ---- USPS tracking ----
    def usps_reply(self, category="In Transit", summary="Your item arrived at a USPS facility.",
                   event="Arrived at USPS Regional Facility", ascending=False):
        events = [
            {"eventType": "Accepted at USPS Origin Facility", "eventTimestamp": "2026-10-11T14:00:00Z",
             "eventCity": "BROOKLYN", "eventState": "NY", "eventCountry": "US"},
            {"eventType": event, "eventTimestamp": "2026-10-12T09:30:00Z",
             "eventCity": "JAMAICA", "eventState": "NY", "eventCountry": "US"},
        ]
        return {"trackingNumber": "9400111899223033005282", "statusCategory": category,
                "statusSummary": summary, "expectedDeliveryDate": "2026-10-15",
                "trackingEvents": events if ascending else list(reversed(events))}

    def with_usps(self, *replies):
        self.r.USPS_CLIENT_ID, self.r.USPS_CLIENT_SECRET = "id", "secret"
        queue = list(replies)
        calls = []
        def fake(number, retry=True):
            calls.append(number)
            return queue.pop(0) if len(queue) > 1 else queue[0]
        self.r.usps_call = fake
        return calls

    def test_usps_reply_is_understood_and_the_newest_scan_wins(self):
        for ascending in (False, True):
            info = self.r.parse_usps_reply(self.usps_reply(ascending=ascending))
            self.assertEqual(info["status"], "InTransit")
            self.assertEqual(info["event"], "Arrived at USPS Regional Facility")
            self.assertEqual(info["where"], "JAMAICA, NY, US")
            self.assertEqual(info["eta"], "2026-10-15")
            self.assertFalse(info["handoff"])
        self.assertIsNone(self.r.parse_usps_reply({}))

    def test_usps_status_words(self):
        status = self.r.usps_status
        self.assertEqual(status("Delivered", ""), "Delivered")
        self.assertEqual(status("Out for Delivery", ""), "OutForDelivery")
        self.assertEqual(status("Pre-Shipment", "Label created"), "InfoReceived")
        self.assertEqual(status("Alert", "Delivery attempted"), "Exception")
        self.assertEqual(status("In Transit", "x"), "InTransit")
        self.assertEqual(status("", ""), "")

    def test_usps_handoff_to_the_local_post_is_noticed(self):
        info = self.r.parse_usps_reply(self.usps_reply(event="Arrived at Foreign Post"))
        self.assertTrue(info["handoff"])

    def test_usps_is_used_first_and_parcels_app_is_left_alone(self):
        self.with_usps(self.usps_reply())
        self.r.PARCELS_KEY = "key"
        asked = []
        self.r.parcels_call = lambda *a, **k: asked.append(1)
        info = self.r.parcel_info("9400111899223033005282")
        self.assertEqual(info["status"], "InTransit")
        self.assertEqual(asked, [])

    def test_after_the_handoff_parcels_app_covers_the_last_leg(self):
        self.with_usps(self.usps_reply(event="Departed USPS, arrived at destination country"))
        self.with_parcels(self.parcels_reply(state="Out with SingPost", number="9400111899223033005282"))
        info = self.r.parcel_info("9400111899223033005282")
        self.assertEqual(info["event"], "Out with SingPost")

    def test_after_the_handoff_without_parcels_app_you_are_pointed_to_the_page(self):
        self.with_usps(self.usps_reply(event="Arrived at Foreign Post"))
        self.r.PARCELS_KEY = ""
        info = self.r.parcel_info("9400111899223033005282")
        self.assertTrue(info["handoff"])
        parcel = {"label": "All Over 2", "number": "9400111899223033005282", "delivered": False, **info}
        self.assertIn("Handed to the local post. Tap Parcels", self.r.parcel_text(parcel))

    def test_usps_not_knowing_the_number_yet_is_not_an_error(self):
        self.with_usps(None)
        self.r.PARCELS_KEY = ""
        self.assertIsNone(self.r.parcel_info("9400111899223033005282"))

    def test_if_usps_fails_parcels_app_steps_in(self):
        self.r.USPS_CLIENT_ID, self.r.USPS_CLIENT_SECRET = "id", "secret"
        def boom(number, retry=True): raise OSError("usps down")
        self.r.usps_call = boom
        self.with_parcels(self.parcels_reply(number="9400111899223033005282"))
        self.assertEqual(self.r.parcel_info("9400111899223033005282")["status"], "InTransit")

    def test_track_command_with_usps_keys_follows_the_parcel(self):
        self.with_usps(self.usps_reply())
        self.r.PARCELS_KEY = ""
        self.r.ASYNC_FIRST_LOOKUP = False
        self.r.handle_track("9400111899223033005282 All Over 2")
        texts = self.texts()
        self.assertTrue(any("Tracking started" in x for x in texts))
        self.assertTrue(any("FIRST UPDATE" in x and "on the way" in x for x in texts))
        markup = [p for m, p in self.sent if m == "sendMessage"][-1]["reply_markup"]
        self.assertIn("USPS page", markup)
        self.assertIn("tools.usps.com", markup)
        self.assertIn("parcelsapp.com", markup)

    def test_usps_token_is_kept_between_calls(self):
        import io, urllib.request
        posts = []
        class Reply(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): pass
        def fake_open(request, timeout=0):
            posts.append(request.full_url)
            return Reply(b'{"access_token": "tok", "expires_in": 3600}')
        real = urllib.request.urlopen
        urllib.request.urlopen = fake_open
        try:
            self.r.USPS_CLIENT_ID, self.r.USPS_CLIENT_SECRET = "id", "secret"
            self.r.USPS_TOKEN.update(value="", until=0.0)
            self.assertEqual(self.r.usps_token(), "tok")
            self.assertEqual(self.r.usps_token(), "tok")
        finally:
            urllib.request.urlopen = real
        self.assertEqual(len(posts), 1)

    # ---- password / locked shop ----
    def test_locked_shop_then_password_then_everything_in_stock_is_alerted(self):
        self.r.check_stock()
        def locked(): raise self.r.ShopLocked("locked", "DROP 8PM")
        self.r.fetch_products = locked
        try: self.r.check_stock()
        except self.r.ShopLocked as e: self.r.note_locked(e.text)
        self.r.note_locked("DROP 8PM")                           # same message: stays quiet
        self.assertEqual(sum("behind a password" in p.get("text", "") for m, p in self.sent), 1)
        self.r.fetch_products = lambda: shop(True, False, False, True)
        self.r.PW["using"] = True
        self.r.check_stock()
        texts = " ".join(p.get("text", "") for m, p in self.sent)
        self.assertIn("Inside the shop", texts)
        self.assertEqual(len(self.alerts()), 1)

    def test_password_is_forgotten_after_its_time_limit(self):
        self.r.META["shop_password"] = "dropday"
        self.r.META["shop_password_t"] = time.time() - 25 * 3600
        self.r.expire_password()
        self.assertEqual(self.r.META["shop_password"], "")

    def test_password_is_kept_inside_its_time_limit(self):
        self.r.META["shop_password"] = "dropday"
        self.r.META["shop_password_t"] = time.time() - 3600
        self.r.expire_password()
        self.assertEqual(self.r.META["shop_password"], "dropday")

    def test_a_rejected_password_is_never_retried(self):
        self.r.META["shop_password"] = "wrong"
        calls = []
        self.r.post_password = lambda pw, timeout=20: (calls.append(pw) or (b'<input type="password">', "https://x/password"))
        self.assertFalse(self.r.try_unlock())
        self.r.PW["last_try"] = 0
        self.assertFalse(self.r.try_unlock())
        self.assertEqual(calls, ["wrong"])

    # ---- messages ----
    def test_status_lists_decks_in_stock(self):
        self.world["products"] = shop(True, False, False, False)
        self.r.check_stock()
        text = self.r.status_text()
        self.assertIn("KH1</b> · 1 in stock", text)
        self.assertIn("KL2</b> · sold out", text)

    def test_daily_ping_is_sent_once_a_day(self):
        self.r.DAILY_PING_HOUR = time.gmtime(time.time() + 8 * 3600).tm_hour
        self.r.TZ_OFFSET_HOURS = 8
        self.r.maybe_daily_ping()
        self.r.maybe_daily_ping()
        self.assertEqual(sum("Still watching" in p.get("text", "") for m, p in self.sent), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
