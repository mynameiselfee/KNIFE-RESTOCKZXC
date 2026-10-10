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

HERE = os.path.dirname(os.path.abspath(__file__))
FILE = os.path.join(HERE, os.getenv("RADAR_FILE", "restock_monitor.py"))
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

        self.r.telegram_api = api
        self.world = {"products": shop(False, False, False, False)}
        self.r.fetch_products = lambda: self.world["products"]

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
        self.assertEqual(self.r.current_interval(), 5.0)
        self.r.note_rate_limited()
        self.r.note_rate_limited()
        self.assertEqual(self.r.current_interval(), 9.0)
        self.assertFalse(self.r.PACE["buster"])
        self.r.PACE["last_limit"] = time.time() - 1900       # 30 calm minutes later
        for _ in range(5):
            self.r.calm_down()
            self.r.PACE["last_limit"] = time.time() - 1900
        self.assertEqual(self.r.current_interval(), 5.0)
        self.assertTrue(self.r.PACE["buster"])

    def test_the_slow_down_is_capped(self):
        for _ in range(20):
            self.r.note_rate_limited()
        self.assertEqual(self.r.current_interval(), 15.0)

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
