"""Offline regressions and idle CPU measurement for the panel.

测试不打开 Tk、不连游戏：把两个源文件里的类抠出来，用一堆桩件 exec 到同一个
作用域里跑。Game 现在住在 宝玉助手.py、App 住在 宝玉面板.py，所以两个文件都要读。
"""
import ast
from collections import deque
from datetime import datetime
import html
import json
import os
from pathlib import Path
import queue
import re
import sys
import threading
import time
import types
import urllib.parse
import urllib.request
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))
PANEL = SRC / "宝玉面板.py"
ASSIST = SRC / "宝玉助手.py"


_HARNESS_STUBS = ("load_config", "save_config")


def _harness_nodes(path, skip=()):
    """取出类定义 + 字面量常量 + 模块级函数。

    类体里的默认参数（比如 ScrollFrame 的 bg=BG）在定义时就要取值，所以颜色常量
    这类纯字面量必须一起带上；算不出来的（读文件、调函数）直接跳过。
    模块级函数只定义不调用，带上它们是为了让 _fmt_span / _as_dict 这些面板里的小
    工具在测试作用域里也解析得到；skip 是要保留成桩件的名字（读写配置的那两个）。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            nodes.append(node)
        elif isinstance(node, ast.FunctionDef):
            if node.name not in skip:
                nodes.append(node)
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            try:
                ast.literal_eval(node.value)
            except Exception:
                continue
            nodes.append(node)
    return nodes


def _tk_stub():
    """ScrollFrame(tk.Frame) 在类定义时就要用到 tk.Frame，给它一个空基类即可。"""
    return types.SimpleNamespace(
        Frame=type("Frame", (), {}),
        Canvas=object, Scrollbar=object, Text=object,
        BooleanVar=object, IntVar=object, StringVar=object,
        Label=object, Button=object, Checkbutton=object, LabelFrame=object,
        Spinbox=object, OptionMenu=object, Tk=object)


def load_runtime():
    """把两个模块里的类装到同一个作用域，行为和真实模块一致但不碰外部资源。"""
    scope = dict(json=json, threading=threading, queue=queue, time=time,
                 websocket=types.SimpleNamespace(),
                 WS_URL="ws://127.0.0.1:10998",
                 PRICE_SERVICE_URL="https://price-service.test/wog-prices",
                 urllib=types.SimpleNamespace(parse=urllib.parse, request=urllib.request),
                 tk=_tk_stub(), sys=sys, os=os, re=re, html=html,
                 datetime=datetime, Path=Path,
                 load_config=lambda: {}, save_config=lambda cfg: None)
    for path in (ASSIST, PANEL):
        body = _harness_nodes(path, skip=_HARNESS_STUBS)
        exec(compile(ast.Module(body=body, type_ignores=[]), str(path), "exec"), scope)
    # 面板里用到的 js_* 构建函数住在 宝玉助手.py，直接引真货，别在测试里再抄一份
    import 宝玉助手 as assist
    for name in dir(assist):
        if name.startswith("js_") or name.startswith("CONTENT_"):
            scope[name] = getattr(assist, name)
    assert "Game" in scope and "App" in scope
    return scope


class _FakeRoot:
    """够 after / configure 用的假 root。只记录不回调，否则 _poll_updates 会自己递归。"""

    def __init__(self):
        self.after_calls = []

    def after(self, delay, fn=None, *a):
        self.after_calls.append((delay, fn))
        return "id"

    def configure(self, **kw):
        pass

    def title(self, *a):
        pass

    def geometry(self, *a):
        pass

    def minsize(self, *a):
        pass


def make_app(scope):
    app = scope["App"].__new__(scope["App"])
    app.game = None
    app.busy = False
    app.busy_since = 0.0
    app.ui_state = dict(auto_jewel=False, auto_gear=False, auto_acc=False,
                        auto_dep=False, auto_org=False,
                        detected_pages=0, pages_auto=True, pages_manual=3)
    app.logq = queue.Queue()
    app.uiq = queue.Queue()
    app.root = _FakeRoot()
    app.buttons = []
    app.deposit_stopped_full = False
    app.storage_info = {}
    app._connect_lock = threading.Lock()
    app._connecting = False
    app._stop_event = threading.Event()
    app._runtime_ready = True
    app._data_ready = True
    app.conn_label = types.SimpleNamespace(configure=lambda **kw: None)
    return app


class StopMonitor(BaseException):
    pass


class RuntimeTests(unittest.TestCase):
    def test_gear_fusion_shortfall_adds_yellow_hint(self):
        scope = load_runtime()
        app = make_app(scope)

        app._log_fusion_results("gear", ["装备 T3/Lv30:2件＜6，跳过"])

        rows = []
        while not app.logq.empty():
            rows.append(app.logq.get_nowait())
        self.assertEqual(rows[-1], "提示：请将铁匠的设置品级调整为全部")
        self.assertEqual(app._log_tag(rows[-1]), "warning")
        self.assertEqual(app._log_tag(rows[0]), "warning")

    def test_connection_guidance_log_is_yellow(self):
        scope = load_runtime()

        self.assertEqual(scope["App"]._log_tag("请先打开助手再启动游戏并等待1分钟"), "warning")

    def test_transport_drop_starts_reconnect_and_closes_old_game(self):
        scope = load_runtime()
        app = make_app(scope)
        closed = []
        reconnects = []
        app.game = types.SimpleNamespace(close=lambda: closed.append(True))
        app._start_connect = lambda: reconnects.append(True)

        app._drop_connection(ConnectionError("connection reset"))

        self.assertIsNone(app.game)
        self.assertEqual(closed, [True])
        self.assertEqual(reconnects, [True])
        self.assertIn("自动重连", app.logq.get_nowait())

    def test_transport_error_classifier_recognizes_closed_websocket(self):
        scope = load_runtime()
        self.assertTrue(scope["App"]._is_transport_error(
            RuntimeError("WebSocket is already closed")))

    def test_fusion_materials_fall_back_to_previous_snapshot(self):
        scope = load_runtime()
        app = make_app(scope)
        app.economic_snapshot = [{"itemId": "out", "name": "产物", "category": "gear",
                                  "tier": 5, "quantity": 1}]
        app._economic_previous = {
            str(i): {"itemId": str(i), "name": f"原料{i}", "market_name": f"原料{i} (Tier 4)",
                     "category": "gear", "tier": 4, "quantity": 1, "location": 1}
            for i in range(6)
        }

        names = app._fusion_input_names("gear", 4)

        self.assertEqual([row["name"] for row in names], [f"原料{i}" for i in range(6)])
        self.assertTrue(all(row["tier"] == 4 for row in names))

    def test_startup_inventory_backfill_is_baselined_before_drop_tracking(self):
        scope = load_runtime()
        app = make_app(scope)
        app._economic_tracking_ready = False
        app._economic_previous = None
        app._economic_baseline_signature = None
        app._economic_baseline_stable = 0
        app.economic_snapshot = []
        app._economic_pending_outputs = {
            "jewel": [0] * 6, "gear": [0] * 6, "accessory": [0] * 6}
        app._economic_pending_manual_failures = []
        app._economic_auto_fusion_until = 0
        app._refresh_economic_view = lambda: None
        app._request_market_refresh = lambda: None
        app._notify_mail_event = lambda _event: None

        class Store:
            def __init__(self):
                self.rows = []

            def add_event(self, event):
                self.rows.append(dict(event))

        app.economic_store = Store()
        first = {"itemId": "a", "name": "已有装备", "category": "gear",
                 "tier": 4, "level": 30, "location": 1, "quantity": 1}
        late = {"itemId": "b", "name": "延迟加载装备", "category": "gear",
                "tier": 4, "level": 35, "location": 1, "quantity": 1}

        app._update_economic_snapshot([first])
        app._update_economic_snapshot([first, late])
        for _ in range(scope["ECONOMIC_BASELINE_STABLE_SNAPSHOTS"] - 1):
            app._update_economic_snapshot([first, late])

        self.assertTrue(app._economic_tracking_ready)
        self.assertEqual(app.economic_store.rows, [])

        new_drop = {"itemId": "c", "name": "新掉落", "category": "gear",
                    "tier": 4, "level": 40, "location": 1, "quantity": 1}
        app._update_economic_snapshot([first, late, new_drop])
        self.assertEqual(len(app.economic_store.rows), 1)
        self.assertEqual(app.economic_store.rows[0]["name"], "新掉落")

    def test_market_refresh_includes_missing_fusion_material_prices(self):
        scope = load_runtime()
        app = make_app(scope)
        app.economic_snapshot = [{"itemId": "out", "name": "产物", "category": "gear",
                                  "tier": 5, "market_name": "产物 (Tier 5)"}]
        app.economic_store = types.SimpleNamespace(events=lambda kind=None: [{
            "kind": "fusion", "timestamp": time.time(), "tier": 4,
            "target_tier": 5, "success": True, "name": "产物",
            "market_name": "产物 (Tier 5)",
            "input_items": [{"name": "原料", "market_name": "原料 (Tier 4)", "tier": 4}],
        }])
        app.market_cache = types.SimpleNamespace(
            key_for=scope["MarketPriceCache"].key_for, get=lambda _item: None)

        items = app._market_items_for_refresh()

        self.assertEqual({item["name"] for item in items}, {"产物", "原料"})
        self.assertIn(5, [item["tier"] for item in items if item["name"] == "产物"])

    def test_manual_market_refresh_skips_fresh_prices_and_requests_only_stale_items(self):
        scope = load_runtime()
        app = make_app(scope)
        app.economic_snapshot = [
            {"name": "已有价格", "market_name": "Already Priced", "tier": 4},
            {"name": "缺失价格", "market_name": "Missing Price", "tier": 5},
            {"name": "无市场结果", "market_name": "No Market Result", "tier": 3},
        ]
        app.economic_store = types.SimpleNamespace(events=lambda _kind=None: [])
        fresh_key = scope["MarketPriceCache"].key_for(app.economic_snapshot[0])
        no_result_key = scope["MarketPriceCache"].key_for(app.economic_snapshot[2])
        app.market_cache = types.SimpleNamespace(
            key_for=scope["MarketPriceCache"].key_for,
            get=lambda item: ({"price": 1.0} if scope["MarketPriceCache"].key_for(item) == fresh_key
                              else {"price": None, "status": "missing"}
                              if scope["MarketPriceCache"].key_for(item) == no_result_key
                              else None),
        )

        items = app._market_items_for_refresh(manual=True)

        self.assertEqual({item["name"] for item in items}, {"缺失价格", "无市场结果"})

    def test_market_refresh_retries_transient_error_items_once(self):
        scope = load_runtime()
        cache = scope["MarketPriceCache"].__new__(scope["MarketPriceCache"])
        cache.lock = threading.RLock()
        cache.data = {"version": 2, "items": {}, "meta": {}}
        cache._service_token = lambda: "token"
        cache._begin_refresh = lambda: True
        cache._finish_refresh = lambda error=False: None
        calls = []

        def remote(items, force=False):
            calls.append((tuple(item["name"] for item in items), force))
            for item in items:
                key = cache.key_for(item)
                cache.data["items"][key] = {
                    "name": cache.market_name(item),
                    "price": None if len(calls) == 1 else 1.25,
                    "status": "error" if len(calls) == 1 else "ok",
                    "updated_at": time.time(),
                }
            return {cache.key_for(item) for item in items}, len(calls) > 1

        cache._remote_refresh = remote
        item = {"name": "短暂失败物品", "market_name": "Transient (Tier 4)", "tier": 4}
        self.assertTrue(cache.refresh([item], force=True))
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][1], True)
        self.assertEqual(cache.data["items"][cache.key_for(item)]["price"], 1.25)

    def test_remote_missing_price_preserves_last_valid_cached_price(self):
        scope = load_runtime()
        cache = scope["MarketPriceCache"].__new__(scope["MarketPriceCache"])
        cache.lock = threading.RLock()
        cache.data = {"version": 2, "meta": {}, "items": {
            "item (tier 4)": {"name": "Item (Tier 4)", "price": 3.25,
                               "median": 3.0, "volume": "10", "status": "ok",
                               "missing": False, "updated_at": time.time() - 3600}
        }}
        cache._service_token = lambda: "test-token"
        cache._save = lambda: None

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return json.dumps({"ok": True, "items": [{
                    "key": "item (tier 4)", "name": "Item (Tier 4)",
                    "price": None, "status": "missing",
                }]}).encode()

        old_urlopen = urllib.request.urlopen
        urllib.request.urlopen = lambda *_args, **_kwargs: Response()
        try:
            returned, changed = cache._remote_refresh([{
                "name": "Item", "market_name": "Item", "tier": 4,
            }], force=True)
        finally:
            urllib.request.urlopen = old_urlopen

        self.assertIn("item (tier 4)", returned)
        self.assertFalse(changed)
        row = cache.data["items"]["item (tier 4)"]
        self.assertEqual(row["price"], 3.25)
        self.assertEqual(row["status"], "ok")
        self.assertIn("last_checked_at", row)

    def test_economic_events_keep_showing_stale_cached_estimates(self):
        scope = load_runtime()
        cache = scope["MarketPriceCache"].__new__(scope["MarketPriceCache"])
        cache.lock = threading.RLock()
        cache.max_age = 12 * 3600
        cache.data = {"version": 2, "meta": {}, "items": {
            "tri-color ammo (tier 5)": {
                "name": "Tri-Color Ammo (Tier 5)", "price": 1.35,
                "status": "ok", "updated_at": time.time() - 24 * 3600,
            }
        }}
        app = make_app(scope)
        app.market_cache = cache
        app.economic_snapshot = []

        self.assertIsNone(cache.get({"name": "Tri-Color Ammo", "tier": 5}))
        self.assertEqual(app._event_value({
            "kind": "fusion", "name": "Tri-Color Ammo", "market_name": "Tri-Color Ammo",
            "tier": 4, "target_tier": 5, "success": True,
        }), 1.35)

    def test_failed_fusion_estimates_and_refreshes_retained_input_tier_item(self):
        scope = load_runtime()
        cache = scope["MarketPriceCache"]
        key = cache.key_for({"name": "Elite Cloak", "tier": 3})
        app = make_app(scope)
        app.market_cache = types.SimpleNamespace(
            get=lambda item, **_kwargs: {"price": 0.36}
            if cache.key_for(item) == key else None,
            key_for=cache.key_for,
        )
        app.economic_snapshot = []
        event = {"kind": "fusion", "name": "精英斗篷", "market_name": "Elite Cloak",
                 "tier": 3, "target_tier": 4, "success": False, "category": "gear",
                 "timestamp": time.time()}

        self.assertEqual(app._event_item_tier(event), 3)
        self.assertEqual(app._event_value(event), 0.36)

        app.economic_store = types.SimpleNamespace(events=lambda _kind=None: [event])
        items = app._market_items_for_refresh()
        retained = [item for item in items if item["name"] == "精英斗篷"]
        self.assertEqual(len(retained), 0, "已有 T3 缓存时炸炉记录不应重复请求")

        app.market_cache.get = lambda _item: None
        items = app._market_items_for_refresh()
        retained = [item for item in items if item["name"] == "精英斗篷"]
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0]["tier"], 3)

    def test_economic_totals_and_priced_counts_use_stack_quantities(self):
        scope = load_runtime()
        app = make_app(scope)
        app._market_price = lambda item: item.get("unit_price")
        value, priced, eligible = app._economic_value([
            {"category": "gear", "tier": 4, "quantity": 3, "unit_price": 2.5},
            {"category": "accessory", "tier": 5, "quantity": 2, "unit_price": None},
            {"category": "jewel", "tier": 3, "quantity": 1, "unit_price": 4.0,
             "embedded": True},
            {"category": "gear", "tier": 2, "quantity": 8, "unit_price": 20.0},
        ])

        self.assertEqual(value, 11.5)
        self.assertEqual(priced, 4)
        self.assertEqual(eligible, 6)

    def test_failed_fusion_missing_market_name_hydrates_at_input_tier(self):
        scope = load_runtime()
        app = make_app(scope)
        app.economic_snapshot = [{"name": "精英斗篷", "market_name": "Elite Cloak",
                                  "category": "gear", "tier": 3}]
        app.economic_store = types.SimpleNamespace(
            events=lambda: [{"kind": "fusion", "name": "精英斗篷", "market_name": "",
                             "tier": 3, "target_tier": 4, "success": False,
                             "category": "gear"}],
            replace_events=lambda events: setattr(app, "hydrated_events", events),
        )

        app._hydrate_event_market_names()

        self.assertEqual(app.hydrated_events[0]["market_name"], "Elite Cloak")

    def test_failed_fusion_market_name_hydrates_from_historical_matching_output(self):
        scope = load_runtime()
        app = make_app(scope)
        app.economic_snapshot = []
        failed = {"kind": "fusion", "name": "精英斗篷", "market_name": "",
                  "tier": 3, "target_tier": 4, "success": False,
                  "category": "gear", "timestamp": time.time()}
        successful = {"kind": "fusion", "name": "精英斗篷", "market_name": "Elite Cloak",
                      "tier": 2, "target_tier": 3, "success": True,
                      "category": "gear", "timestamp": time.time() - 100}
        app.economic_store = types.SimpleNamespace(
            events=lambda: [failed, successful],
            replace_events=lambda events: setattr(app, "hydrated_events", events),
        )

        app._hydrate_event_market_names()

        self.assertEqual(app.hydrated_events[0]["market_name"], "Elite Cloak")
        self.assertEqual(app.hydrated_events[0]["tier"], 3)

    def test_today_income_is_net_of_fusion_inputs_and_failures(self):
        scope = load_runtime()
        app = make_app(scope)
        app._event_value = lambda event: event.get("value")
        app._input_value = lambda event: event.get("input")
        app._fusion_success = lambda event: event.get("success", False)
        today = datetime.now().strftime("%Y-%m-%d")
        stamp = time.time()
        events = [
            {"kind": "drop", "timestamp": stamp, "value": 10},
            {"kind": "fusion", "timestamp": stamp, "success": True,
             "input": 25, "value": 30},
            {"kind": "fusion", "timestamp": stamp, "success": False,
             "input": 7, "value": 2},
            {"kind": "drop", "timestamp": stamp - 86400, "value": 1000},
        ]

        self.assertEqual(app._today_net_income(events, today), 10)

    def test_failed_fusion_retained_item_reduces_material_loss_in_daily_income(self):
        scope = load_runtime()
        app = make_app(scope)
        app._event_value = lambda event: event.get("output")
        app._input_value = lambda event: event.get("input")
        app._fusion_success = lambda _event: False
        today = datetime.now().strftime("%Y-%m-%d")

        failed = {"kind": "fusion", "timestamp": time.time(), "input": 12,
                  "output": 3.5, "success": False}
        no_result = {"kind": "fusion", "timestamp": time.time(), "input": 8,
                     "output": None, "success": False}

        self.assertEqual(app._today_net_income([failed, no_result], today), -16.5)

    def test_mail_settings_are_sanitized_and_local_defaults_are_safe(self):
        scope = load_runtime()
        cfg = scope["_sanitize"]({
            "mail_push_enabled": "true",
            "mail_sender": " sender@qq.com ",
            "mail_recipient": " receiver@example.com ",
            "mail_auth_code": " local-secret ",
            "mail_fusion_targets": [4, 99, "2", 4],
        })

        self.assertFalse(scope["DEFAULT_CONFIG"]["mail_push_enabled"])
        self.assertTrue(cfg["mail_push_enabled"])
        self.assertEqual(cfg["mail_sender"], "sender@qq.com")
        self.assertEqual(cfg["mail_recipient"], "receiver@example.com")
        self.assertEqual(cfg["mail_auth_code"], "local-secret")
        self.assertEqual(cfg["mail_push_name"], "WoG宝玉助手")
        self.assertEqual(cfg["mail_fusion_targets"], [4, 2])

    def test_mail_subject_uses_custom_name_without_header_newlines(self):
        scope = load_runtime()
        subject = scope["App"]._mail_subject

        self.assertEqual(subject("夜班掉落", "材料掉落：Gold Bar"),
                         "夜班掉落 | 材料掉落：Gold Bar")
        self.assertNotIn("\r", subject("test\r\nBcc: attacker@example.com", "drop"))
        self.assertNotIn("\n", subject("test", "drop\r\nBcc: attacker@example.com"))

    def test_fusion_mail_html_contains_requested_card_fields(self):
        scope = load_runtime()
        app = make_app(scope)
        app._fusion_success = lambda _event: True
        app._input_value = lambda _event: 12.5
        app._event_value = lambda _event: 18.0
        event = {"tier": 3, "target_tier": 4, "category": "gear",
                 "name": "精英护肩", "timestamp": time.time()}

        card = app._fusion_mail_html(event)

        for label in ("品级", "目标级", "类型", "合成结果", "状态", "原料价值",
                      "产出价值", "此炉", "时间"):
            self.assertIn(label, card)
        self.assertIn("¥ 12.50", card)
        self.assertIn("¥ 18.00", card)
        self.assertIn("自动合成通知", card)
        self.assertIn("color:#ffffff", card)
        self.assertIn("background-color:#17212f", card)
        self.assertIn("color:#2463b4", card)
        self.assertIn("color:#c62828", card)

    def test_failed_fusion_mail_shows_retained_item_value(self):
        scope = load_runtime()
        app = make_app(scope)
        app._fusion_success = lambda _event: False
        app._input_value = lambda _event: 2.0
        app._event_value = lambda _event: 0.36
        app._fusion_verdict = lambda _event: "炸炉"
        card = app._fusion_mail_html({
            "tier": 3, "target_tier": 4, "category": "gear",
            "name": "精英斗篷", "timestamp": time.time(), "success": False,
        })

        self.assertIn("炸炉", card)
        self.assertIn("¥ 0.36", card)

    def test_mail_tier_palette_is_legible_on_light_card(self):
        scope = load_runtime()
        tier_color = scope["App"]._mail_tier_color
        self.assertEqual([tier_color(tier) for tier in range(1, 7)], [
            "#66717e", "#16803c", "#2463b4", "#c62828", "#7e42b5", "#8a6800",
        ])

    def test_drop_mail_html_contains_value_and_time(self):
        scope = load_runtime()
        app = make_app(scope)
        app._event_value = lambda _event: 5.25
        card = app._drop_mail_html([{
            "name": "Gold Bar", "timestamp": time.time(), "quantity": 2,
        }])

        self.assertIn("Gold Bar", card)
        self.assertIn("价值", card)
        self.assertIn("¥ 5.25", card)
        self.assertIn("时间", card)

    def test_mail_drop_names_only_accept_exact_steam_market_items(self):
        scope = load_runtime()
        match = scope["App"]._mail_drop_name

        self.assertEqual(match({"name": "金条", "market_name": "Gold Bar"}), "Gold Bar")
        self.assertEqual(match({"name": "Diamond Pouch"}), "Diamond Pouch")
        self.assertEqual(match({"market_name": "https://steamcommunity.com/market/listings/4891320/Gold%20Bar"}),
                         "Gold Bar")
        for event in ({"name": "金条"}, {"name": "钻石袋"}, {"name": "Diamond"},
                      {"name": "Large Gold Bar"}, {"market_name": "Diamond Shard"}):
            self.assertEqual(match(event), "", event)

    def test_mail_settings_require_complete_qq_sender_address(self):
        scope = load_runtime()
        validate = scope["App"]._mail_settings_error

        self.assertIn("@qq.com", validate("123456789", "to@example.com", "code", True))
        self.assertIn("@qq.com", validate("123456789@example.com", "to@example.com", "code", True))
        self.assertEqual(validate("123456789@qq.com", "to@example.com", "code", True), "")

    def test_mail_settings_only_change_config_after_valid_save(self):
        scope = load_runtime()
        app = make_app(scope)
        app.cfg = {
            "mail_push_enabled": False, "mail_sender": "old@qq.com",
            "mail_recipient": "old@example.com", "mail_auth_code": "old-code",
            "mail_fusion_targets": [4],
        }
        app.mail_push_var = _Var(True)
        app.mail_push_name_var = _Var("经济提醒")
        app.mail_sender_var = _Var("123456789")
        app.mail_recipient_var = _Var("new@example.com")
        app.mail_auth_var = _Var("new-code")
        app.mail_target_vars = {tier: _Var(tier in (4, 5)) for tier in range(1, 7)}
        app._log = lambda _message: None
        warnings = []
        scope["messagebox"] = types.SimpleNamespace(
            showwarning=lambda title, message: warnings.append((title, message)))
        saved = []
        scope["save_config"] = lambda cfg: saved.append(dict(cfg))

        self.assertFalse(app._save_mail_settings())
        self.assertEqual(app.cfg["mail_sender"], "old@qq.com")
        self.assertEqual(saved, [])
        self.assertTrue(warnings)

        app.mail_sender_var.set("123456789@qq.com")
        self.assertTrue(app._save_mail_settings())
        self.assertEqual(app.cfg["mail_sender"], "123456789@qq.com")
        self.assertEqual(app.cfg["mail_fusion_targets"], [4, 5])
        self.assertEqual(app.cfg["mail_push_name"], "经济提醒")
        self.assertEqual(saved[-1]["mail_auth_code"], "new-code")

    def test_mail_notifications_use_saved_settings_and_exact_drop_names(self):
        scope = load_runtime()
        app = make_app(scope)
        app.cfg = {
            "mail_push_enabled": True, "mail_push_name": "仓库提醒",
            "mail_sender": "sender@qq.com",
            "mail_recipient": "to@example.com", "mail_auth_code": "code",
            "mail_fusion_targets": [4],
        }
        app._mail_send_queue = queue.Queue()
        app._mail_worker_running = True
        app._mail_worker_lock = threading.Lock()
        app._call_soon = lambda fn: fn()
        app._fusion_mail_html = lambda _event: "fusion"
        app._drop_mail_html = lambda _events: "drop"

        app._notify_mail_event({"kind": "drop", "name": "钻石", "timestamp": time.time()})
        self.assertTrue(app._mail_send_queue.empty(), "模糊中文名称不能触发推送")
        app._notify_mail_event({"kind": "drop", "market_name": "Diamond Pouch",
                                "name": "钻石袋", "timestamp": time.time()})
        sender, recipient, auth, subject, body = app._mail_send_queue.get_nowait()
        self.assertEqual((sender, recipient, auth), ("sender@qq.com", "to@example.com", "code"))
        self.assertTrue(subject.startswith("仓库提醒 | "))
        self.assertIn("Diamond Pouch", subject)
        self.assertEqual(body, "drop")

    def test_general_ui_save_does_not_commit_unconfirmed_mail_edits(self):
        scope = load_runtime()
        app = make_app(scope)
        app.cfg = {
            "mail_push_enabled": False, "mail_sender": "saved@qq.com",
            "mail_recipient": "saved@example.com", "mail_auth_code": "saved-code",
            "mail_fusion_targets": [4],
        }
        _wire_ui_vars(app)
        app.mail_push_var = _Var(True)
        app.mail_sender_var = _Var("unsaved@qq.com")
        app.mail_recipient_var = _Var("unsaved@example.com")
        app.mail_auth_var = _Var("unsaved-code")

        app._save_all_from_ui()

        self.assertFalse(app.cfg["mail_push_enabled"])
        self.assertEqual(app.cfg["mail_sender"], "saved@qq.com")
        self.assertEqual(app.cfg["mail_auth_code"], "saved-code")

    def test_mail_tab_follows_help_and_is_not_inside_economic_tab(self):
        tree = ast.parse(PANEL.read_text(encoding="utf-8"))
        app_cls = next(n for n in tree.body
                       if isinstance(n, ast.ClassDef) and n.name == "App")
        tabs = next(n.value for n in app_cls.body
                    if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "TABS" for t in n.targets))
        tab_labels = [item.elts[0].value for item in tabs.elts]
        self.assertLess(tab_labels.index("说明"), tab_labels.index("邮箱推送"))
        self.assertEqual(tab_labels[tab_labels.index("邮箱推送") - 1], "说明")
        self.assertEqual(tab_labels[tab_labels.index("邮箱推送") + 1], "打赏")

        econ = next(n for n in app_cls.body if isinstance(n, ast.FunctionDef)
                    and n.name == "_build_economic_tab")
        econ_text = " ".join(n.value for n in ast.walk(econ)
                              if isinstance(n, ast.Constant) and isinstance(n.value, str))
        self.assertNotIn("邮件推送", econ_text)

    def test_fusion_output_is_recorded_in_its_category_and_output_tier(self):
        scope = load_runtime()
        app = make_app(scope)
        app.current_stage = "地狱2-10"
        app._stats_pending_fusion_outputs = {
            "jewel": [0] * 6, "gear": [0] * 6, "accessory": [0] * 6}
        app._stats_pending_fusion_returns = {
            category: [0] * 6 for category in ("jewel", "gear", "accessory")}

        class Store:
            def __init__(self):
                self.calls = []

            def add(self, *args, **kwargs):
                self.calls.append((args, kwargs))

        app.stats_store = Store()
        app._record_fusion_lines("gear", ["装备 T4/混级 #1 → 精英护肩",
                                           "装备 T4 共 1 次（出货 1 / 未出货 0）"])
        app._record_fusion_lines("accessory", ["饰品 T2/混级 #1 → 项链",
                                                "饰品 T2 共 1 次（出货 1 / 未出货 0）"])
        app._record_fusion_lines("jewel", ["Tier3/Lv1 合成出 → 宝玉"])

        self.assertEqual([call[0][1] for call in app.stats_store.calls],
                         ["gear", "accessory", "jewel"])
        self.assertEqual(app.stats_store.calls[0][0][0], "fusions")
        self.assertEqual(app.stats_store.calls[0][0][2], [0, 0, 0, 0, 1, 0])
        self.assertEqual(app.stats_store.calls[1][0][2], [0, 0, 1, 0, 0, 0])
        self.assertEqual(app.stats_store.calls[2][0][2], [0, 0, 0, 1, 0, 0])
        self.assertEqual(app._stats_pending_fusion_outputs["gear"][4], 1)
        self.assertEqual(app._stats_pending_fusion_outputs["accessory"][2], 1)
        self.assertEqual(app._stats_pending_fusion_outputs["jewel"][3], 1)

    def test_fusion_claims_just_observed_count_before_it_becomes_a_drop(self):
        scope = load_runtime()
        app = make_app(scope)
        app.current_stage = "地狱2-10"
        app._stats_previous = {"jewel": None, "gear": None, "accessory": None}
        app._stats_pending_fusion_outputs = {
            "jewel": [0] * 6, "gear": [0] * 6, "accessory": [0] * 6}
        app._stats_pending_fusion_returns = {
            category: [0] * 6 for category in ("jewel", "gear", "accessory")}
        app._stats_pending_drop_deltas = {
            category: [[] for _ in range(6)]
            for category in ("jewel", "gear", "accessory")}

        class Store:
            def __init__(self):
                self.calls = []

            def add(self, *args, **kwargs):
                self.calls.append((args, kwargs))

        app.stats_store = Store()
        counts_before = {f"tier{tier}": {"total": 0} for tier in range(1, 7)}
        counts_after = {f"tier{tier}": {"total": int(tier == 5)} for tier in range(1, 7)}
        app._record_count_delta("gear", counts_before)
        app._record_count_delta("gear", counts_after)

        self.assertEqual(app.stats_store.calls, [], "新增件数先等待合成事件归类")
        app._record_fusion_lines("gear", ["装备 T4/混级 #1 → 精英护肩"])
        app._flush_pending_stat_drops(force=True)

        self.assertEqual(app.stats_store.calls[0][0][0], "fusions")
        self.assertFalse(any(call[0][0] == "drops" for call in app.stats_store.calls))

    def test_count_probe_never_commits_inventory_increase_as_drop(self):
        scope = load_runtime()
        app = make_app(scope)
        app.current_stage = "噩梦1-5"
        app._stats_previous = {"jewel": None, "gear": None, "accessory": None}
        app._stats_pending_fusion_outputs = {
            "jewel": [0] * 6, "gear": [0] * 6, "accessory": [0] * 6}
        app._stats_pending_drop_deltas = {
            category: [[] for _ in range(6)]
            for category in ("jewel", "gear", "accessory")}

        class Store:
            def __init__(self):
                self.calls = []

            def add(self, *args, **kwargs):
                self.calls.append((args, kwargs))

        app.stats_store = Store()
        app._record_count_delta("gear", {
            f"tier{tier}": {"total": int(tier == 3)} for tier in range(1, 7)})
        app._record_count_delta("gear", {
            f"tier{tier}": {"total": int(tier in (3, 4))} for tier in range(1, 7)})
        self.assertEqual(app.stats_store.calls, [])

        app._queue_verified_drop({"kind": "drop", "timestamp": time.time(),
                                  "name": "真实掉落", "category": "gear",
                                  "tier": 4, "quantity": 1})
        app._stats_pending_verified_drops[0]["timestamp"] -= \
            scope["STATS_FUSION_RECONCILE_SECONDS"]
        app._flush_verified_drops()
        self.assertEqual(app.stats_store.calls[0][0][0], "drops")
        self.assertEqual(app.stats_store.calls[0][0][2], [0, 0, 0, 1, 0, 0])

    def test_failed_fusion_returned_material_is_not_counted_as_drop(self):
        scope = load_runtime()
        app = make_app(scope)
        app.current_stage = "地狱2-10"
        app._stats_previous = {"jewel": None, "gear": None, "accessory": None}
        app._stats_pending_fusion_outputs = {
            "jewel": [0] * 6, "gear": [0] * 6, "accessory": [0] * 6}
        app._stats_pending_fusion_returns = {
            category: [0] * 6 for category in ("jewel", "gear", "accessory")}
        app._stats_pending_drop_deltas = {
            category: [[] for _ in range(6)]
            for category in ("jewel", "gear", "accessory")}

        class Store:
            def __init__(self):
                self.calls = []

            def add(self, *args, **kwargs):
                self.calls.append((args, kwargs))

        app.stats_store = Store()
        app._record_count_delta("gear", {
            f"tier{tier}": {"total": 0} for tier in range(1, 7)})
        app._record_fusion_lines("gear", ["装备 T4/混级 #1 → 未出货"])
        app._record_count_delta("gear", {
            f"tier{tier}": {"total": int(tier == 4)} for tier in range(1, 7)})
        app._flush_pending_stat_drops(force=True)

        self.assertEqual(app.stats_store.calls[0][0][0], "fusion_failures")
        self.assertFalse(any(call[0][0] == "drops" for call in app.stats_store.calls))

    # ---------- 监听线程 ----------
    def test_monitor_waits_in_every_state(self):
        for state in ("disconnected", "busy", "disabled", "active", "error"):
            with self.subTest(state=state):
                scope = load_runtime()
                app = make_app(scope)
                calls, waits = [], []
                if state != "disconnected":
                    app.game = object()
                app.busy = state == "busy"
                if state in ("active", "error"):
                    def cycle():
                        calls.append(True)
                        if state == "error":
                            raise RuntimeError("offline test")
                    app._monitor_cycle = cycle

                def sleep(delay):
                    waits.append(delay)
                    if len(waits) == 3:
                        raise StopMonitor()

                scope["time"] = types.SimpleNamespace(sleep=sleep, time=time.time)
                # _monitor 里引用的 time 是模块全局，换掉作用域里的即可
                with self.assertRaises(StopMonitor):
                    app._monitor()
                self.assertEqual(waits, [scope["MONITOR_INTERVAL"]] * 3)
                if state in ("active", "error"):
                    self.assertEqual(len(calls), 3)
                if state == "error":
                    self.assertEqual(app.logq.qsize(), 3)

    def test_full_storage_disables_deposit_then_resumes(self):
        """满仓停入库 → 腾出空间自动恢复：两条都要真的走得到。"""
        scope = load_runtime()
        app = make_app(scope)
        app.ui_state.update(auto_dep=True, stop_when_full=True, auto_resume=True)

        app._check_deposit_full(app.ui_state, dict(storageTotal=126, storageUsed=126,
                                                  storageFree=0, pages=3))
        self.assertTrue(app.deposit_stopped_full)
        self.assertEqual(app.uiq.get_nowait()[0], "deposit_full")

        # 主线程处理器把 auto_dep 关掉
        app.auto_dep_var = types.SimpleNamespace(
            get=lambda: False, set=lambda v: None)
        app.cfg = {}
        app.root = _FakeRoot()
        app._on_deposit_full(dict(storageUsed=126, storageTotal=126, pages=3))
        app.ui_state["auto_dep"] = False

        # 关键：auto_dep 已经是 False，恢复仍然要能触发
        app._check_deposit_full(app.ui_state, dict(storageTotal=126, storageUsed=100,
                                                  storageFree=26, pages=3))
        self.assertFalse(app.deposit_stopped_full)
        self.assertEqual(app.uiq.get_nowait()[0], "deposit_resume")

    def test_no_resume_when_option_off(self):
        scope = load_runtime()
        app = make_app(scope)
        app.ui_state.update(auto_dep=True, stop_when_full=True, auto_resume=False)
        app._check_deposit_full(app.ui_state, dict(storageTotal=126, storageUsed=126,
                                                  storageFree=0, pages=3))
        app.uiq.get_nowait()  # deposit_full
        app.ui_state["auto_dep"] = False
        app._check_deposit_full(app.ui_state, dict(storageTotal=126, storageUsed=1,
                                                  storageFree=125, pages=3))
        self.assertTrue(app.uiq.empty(), "没勾自动恢复就不该投递 deposit_resume")
        # 标志留着，等主线程手动重开自动入库时再清（on_toggle_deposit 里）
        self.assertTrue(app.deposit_stopped_full)

    # ---------- 请求串行化 ----------
    def test_concurrent_requests_do_not_steal_responses(self):
        scope = load_runtime()
        entered = threading.Event()
        release = threading.Event()

        class Socket:
            def __init__(self):
                self.sent = []
                self.responses = queue.Queue()

            def send(self, data):
                msg = json.loads(data)
                self.sent.append(msg)
                self.responses.put(json.dumps({"id": msg["id"], "result": {
                    "result": {"value": msg["params"]["expression"]}}}))

            def recv(self):
                entered.set()
                if not release.wait(2):
                    raise TimeoutError("test response not released")
                return self.responses.get(timeout=2)

        socket = Socket()
        scope["websocket"].create_connection = lambda *a, **k: socket
        game = scope["Game"]()
        results, errors = {}, []

        def run(name):
            try:
                results[name] = game.eval(name)
            except Exception as exc:
                errors.append(exc)

        first = threading.Thread(target=run, args=("first",))
        second = threading.Thread(target=run, args=("second",))
        first.start()
        try:
            self.assertTrue(entered.wait(1))
            second.start()
            time.sleep(0.05)
            self.assertEqual(len(socket.sent), 1)
        finally:
            release.set()
            first.join(3)
            if second.ident is not None:
                second.join(3)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, {"first": "first", "second": "second"})

    def test_protocol_error_releases_request_lock(self):
        scope = load_runtime()
        socket = types.SimpleNamespace(
            send=lambda data: None,
            recv=lambda: json.dumps({"id": 1, "error": {"message": "failed"}}))
        scope["websocket"].create_connection = lambda *a, **k: socket
        game = scope["Game"]()
        with self.assertRaisesRegex(RuntimeError, "failed"):
            game.eval("test")
        self.assertFalse(game._request_lock.locked())

    # ---------- 界面轮询 ----------
    def test_poll_updates_survives_handler_exception(self):
        """一个 handler 炸了，after 链和后续消息都不能丢（否则按钮永远灰着）。"""
        scope = load_runtime()
        app = make_app(scope)
        app.gear_tier_labels = {}
        app.acc_tier_labels = {}
        app.tier_labels = {}
        app.log = types.SimpleNamespace(
            configure=lambda **k: None, insert=lambda *a: None,
            index=lambda *a: "1.0", see=lambda *a: None, delete=lambda *a: None)

        def boom(_data):
            raise RuntimeError("handler blew up")

        app._show_jewel_stats = boom
        seen = []
        app._handle_update = lambda kind, data: (
            boom(data) if kind == "jewel" else seen.append(kind))

        app.uiq.put(("jewel", {}))
        app.uiq.put(("storage", {}))
        app._poll_updates()

        self.assertTrue(app.uiq.empty(), "坏消息之后的消息仍要被处理")
        # after 链必须续上
        self.assertTrue(any(fn == app._poll_updates for _, fn in app.root.after_calls),
                        "after 链必须续上")
        self.assertIn("storage", seen)

    def test_call_soon_runs_on_the_queue(self):
        scope = load_runtime()
        app = make_app(scope)
        done = []
        app._call_soon(lambda: done.append(True))
        self.assertEqual(done, [])
        app._handle_update("call", lambda: done.append(True))
        self.assertEqual(done, [True])
    # ---------- 装备/饰品：批数要看当前模式 ----------
    def _auto_equip_calls(self, same_level, counts):
        """跑一次 _auto_equip，返回实际下发的表达式条数（1=只探测，2=探测+合成）。"""
        scope = load_runtime()
        app = make_app(scope)
        app.ui_state.update(auto_gear=True, auto_acc=True,
                            gear_tiers=[1], acc_tiers=[1],
                            gear_storage=True, acc_storage=True,
                            gear_same_level=same_level, acc_same_level=same_level)
        app._log = lambda _m: None
        sent = []

        class _Game:
            def eval(self, expr, await_promise=False):
                sent.append(expr)
                return json.dumps(counts if len(sent) == 1 else ["fused"])

        app.game = _Game()
        app._auto_equip(app.ui_state, 1)   # content type 1 = 装备
        return sent

    def test_mixed_mode_triggers_fusion(self):
        """仅同等级=off 时必须按 mixedBatches 判断。
        旧代码只看 batches，所以用户关掉「仅同等级」之后反而永远不合成了。"""
        counts = {"tier1": {"total": 6, "batches": 0, "mixedBatches": 1,
                            "need": 6, "topLevel": 50}}
        sent = self._auto_equip_calls(same_level=False, counts=counts)
        self.assertEqual(len(sent), 2, "混合模式有 1 批可合，应该下发合成请求")

    def test_same_level_mode_skips_when_no_group(self):
        counts = {"tier1": {"total": 6, "batches": 0, "mixedBatches": 1,
                            "need": 6, "topLevel": 50}}
        sent = self._auto_equip_calls(same_level=True, counts=counts)
        self.assertEqual(len(sent), 1, "同等级凑不满时不该下发合成")

    def test_same_level_mode_triggers_when_group_ready(self):
        counts = {"tier1": {"total": 8, "batches": 1, "mixedBatches": 1,
                            "need": 6, "topLevel": 50}}
        sent = self._auto_equip_calls(same_level=True, counts=counts)
        self.assertEqual(len(sent), 2)


    # ---------- 自动入库：日志不能重复刷 ----------
    def _auto_deposit_app(self, replies):
        """replies: 依次返回的 js_deposit 结果（dict）。"""
        scope = load_runtime()
        app = make_app(scope)
        app.ui_state.update(auto_dep=True, dep_types=[6], dep_tiers=[1, 2, 3],
                            exclude_locked=True, stop_when_full=True,
                            pages_auto=True, pages_manual=3)
        app.storage_info = {}
        box = list(replies)

        class _Game:
            def eval(self, expr, await_promise=False):
                return json.dumps(box.pop(0) if box else {"deposited": 0,
                                                          "matchedByKey": {}})

        app.game = _Game()
        return app

    def _notes(self, app):
        out = []
        while not app.logq.empty():
            line = app.logq.get_nowait()
            if "已就绪" in line or "暂时没有可入库" in line:
                out.append(line)
        return out

    def test_no_empty_note_after_successful_deposit(self):
        """成功入库之后紧接着的空轮询不该再打一条“没有可入库物品”——
        那看起来就像又触发了一次入库。"""
        app = self._auto_deposit_app([
            {"deposited": 2, "matchedByKey": {"6": 2}, "storageUsed": 34,
             "storageTotal": 126, "storageFree": 92, "full": False},
            {"deposited": 0, "matchedByKey": {}, "storageUsed": 34,
             "storageTotal": 126, "storageFree": 92, "full": False},
            {"deposited": 0, "matchedByKey": {}, "storageUsed": 34,
             "storageTotal": 126, "storageFree": 92, "full": False},
        ])
        for _ in range(3):
            app._auto_deposit(app.ui_state)
        self.assertEqual(self._notes(app), [],
                         "入库成功后不该刷“没有可入库物品”")

    def test_empty_note_reported_once_per_selection(self):
        empty = {"deposited": 0, "matchedByKey": {}, "storageUsed": 34,
                 "storageTotal": 126, "storageFree": 92, "full": False}
        app = self._auto_deposit_app([empty, empty, empty])
        for _ in range(3):
            app._auto_deposit(app.ui_state)
        self.assertEqual(len(self._notes(app)), 2,
                         "同一套筛选只说明一次（标题+明细各一行）")

    def test_changing_selection_reports_again(self):
        empty = {"deposited": 0, "matchedByKey": {}, "storageUsed": 34,
                 "storageTotal": 126, "storageFree": 92, "full": False}
        app = self._auto_deposit_app([empty, empty, empty])
        app._auto_deposit(app.ui_state)
        self._notes(app)
        app.ui_state["dep_types"] = [6, 4]          # 改了筛选
        app._auto_deposit(app.ui_state)
        self.assertGreater(len(self._notes(app)), 0,
                           "换了筛选应该重新说明一次")
    # ---------- 入库后按分类页归位 ----------
    def _deposit_arrange_app(self, deposited, arrange=True, cats=None, reply=None):
        """跑一次 _auto_deposit，收集真正下发到游戏的表达式。"""
        scope = load_runtime()
        app = make_app(scope)
        app.ui_state.update(auto_dep=True, dep_types=[4], dep_tiers=[1, 2, 3],
                            exclude_locked=True, stop_when_full=True,
                            arrange_pages=arrange, org_locked=False, org_sort=False,
                            pages_auto=False, pages_manual=3,
                            page_cats=cats if cats is not None else
                            {"0": "jewel", "1": "gear", "2": "any"})
        app.storage_info = {"pages": 3}
        sent = []

        class _Game:
            def eval(self, expr, await_promise=False):
                sent.append(expr)
                if len(sent) == 1:
                    return json.dumps({"deposited": deposited, "storageUsed": 30,
                                       "storageTotal": 126, "storageFree": 96,
                                       "matchedByKey": {"4": deposited}, "full": False})
                return json.dumps(reply if reply is not None else
                                  {"ok": True, "moved": deposited, "failed": 0,
                                   "distribution": {"page0": 1, "page1": deposited}})

        app.game = _Game()
        app._auto_deposit(app.ui_state)
        return app, sent

    def test_deposit_then_arranges_into_category_pages(self):
        """入库之后要立刻按「每页放一类」归位，不能全堆在第一页等下次整理。"""
        app, sent = self._deposit_arrange_app(deposited=3)
        self.assertEqual(len(sent), 2, "入库之后应该再下发一次整理（归位）")
        self.assertIn("reqStorageMoveInList", sent[0], "第一条是入库")
        self.assertIn("reqStorageMove", sent[1], "第二条是按格子归位")
        log = []
        while not app.logq.empty():
            log.append(app.logq.get_nowait())
        self.assertTrue(any("归位" in line and "3 件" in line for line in log),
                        f"归位要记一条日志，实际：{log}")

    def test_no_arrange_when_nothing_deposited(self):
        app, sent = self._deposit_arrange_app(deposited=0)
        self.assertEqual(len(sent), 1, "没入库就不该去搬仓库")

    def test_arrange_can_be_switched_off(self):
        app, sent = self._deposit_arrange_app(deposited=2, arrange=False)
        self.assertEqual(len(sent), 1, "关掉「按分类页入库」就只入库、不归位")

    def test_arrange_skipped_when_no_page_cats(self):
        """分页全是「不限」时没什么可归位的，不该白跑一趟整理。"""
        app, sent = self._deposit_arrange_app(
            deposited=2, cats={"0": "any", "1": "any", "2": "any"})
        self.assertEqual(len(sent), 1, "没配分页就不该下发归位")

    def test_arrange_failure_does_not_hide_the_deposit(self):
        """归位失败不能盖掉「已入库 N 件」：入库本身是成功的。"""
        app, sent = self._deposit_arrange_app(
            deposited=2, reply={"ok": False, "error": "放置方案未收敛（分类容量不够）"})
        log = []
        while not app.logq.empty():
            log.append(app.logq.get_nowait())
        self.assertTrue(any("自动入库 2 件" in line for line in log), log)
        self.assertTrue(any("归位跳过" in line for line in log), log)


    # ---------- 设置：小时+分钟的间隔 / 日志保留 / 存盘完整性 ----------
    def test_organize_interval_combines_hours_and_minutes(self):
        """自动整理间隔按「小时 + 分钟」填，存盘统一换成总分钟。"""
        scope = load_runtime()
        app = make_app(scope)
        app.org_h_var, app.org_m_var = _Var(1), _Var(30)
        self.assertEqual(app._interval_minutes(), 90, "1 小时 30 分钟 = 90 分钟")
        app.org_m_var = _Var(45)
        self.assertEqual(app._interval_minutes(), 105, "1 小时 45 分钟 = 105 分钟")
        app.org_h_var, app.org_m_var = _Var(0), _Var(45)
        self.assertEqual(app._interval_minutes(), 45, "只填分钟也可以")
        app.org_h_var, app.org_m_var = _Var(0), _Var(0)
        self.assertEqual(app._interval_minutes(), 1, "两个都填 0 按 1 分钟算，不能是 0")
        app.org_h_var = _Var("乱码")
        self.assertEqual(app._interval_minutes(), 1, "输入框里是乱码也得给个能用的值")

    def test_on_toggle_organize_saves_and_logs_only_on_change(self):
        scope = load_runtime()
        app = make_app(scope)
        app.cfg = {}
        app.auto_org_var, app.org_locked_var, app.org_sort_var = \
            _Var(True), _Var(False), _Var(True)
        app.org_h_var, app.org_m_var = _Var(0), _Var(45)
        logged = []
        app._log = logged.append
        scope["save_config"] = lambda cfg: None

        app.on_toggle_organize()
        self.assertEqual(app.cfg["organize_interval_min"], 45)
        self.assertTrue(app.cfg["auto_organize_enabled"])
        self.assertFalse(app.cfg["organize_include_locked"])
        self.assertTrue(app.cfg["organize_sort_after"])
        self.assertEqual(len(logged), 1, "开关真的变了才记一条")
        self.assertIn("45 分钟", logged[0])

        app.on_toggle_organize()          # 间隔框失焦也会走到这里，值没变就不该刷屏
        self.assertEqual(len(logged), 1)
        app.org_m_var = _Var(50)
        app.on_toggle_organize()
        self.assertEqual(len(logged), 2)
        self.assertIn("50 分钟", logged[1])

    def test_log_keep_saves_total_minutes(self):
        scope = load_runtime()
        app = make_app(scope)
        app.cfg = {}
        app._log_rows = deque()
        app.log = types.SimpleNamespace(configure=lambda **k: None,
                                        delete=lambda *a: None)
        app.log_keep_on_var, app.log_keep_h_var, app.log_keep_m_var = \
            _Var(True), _Var(1), _Var(30)
        logged = []
        app._log = logged.append
        scope["save_config"] = lambda cfg: None

        app.on_log_keep()
        self.assertEqual(app.cfg["log_keep_minutes"], 90, "1 小时 30 分钟 = 90 分钟")
        self.assertTrue(logged and "1 小时 30 分钟" in logged[0])
        app.log_keep_on_var = _Var(False)
        app.on_log_keep()
        self.assertEqual(app.cfg["log_keep_minutes"], 0, "关掉开关 = 不按时间清理")

    def test_save_all_from_ui_writes_every_setting(self):
        scope = load_runtime()
        app = make_app(scope)
        app.cfg = {}
        _wire_ui_vars(app)
        app._save_all_from_ui()
        expected = {
            "auto_fuse_enabled", "fuse_tiers", "include_storage",
            "auto_gear_fuse_enabled", "gear_fuse_tiers", "gear_same_level",
            "gear_include_storage", "auto_acc_fuse_enabled", "acc_fuse_tiers",
            "acc_same_level", "acc_include_storage", "auto_deposit_enabled",
            "deposit_item_types", "deposit_tiers", "deposit_exclude_locked",
            "deposit_stop_when_full", "deposit_auto_resume", "deposit_arrange_pages",
            "auto_organize_enabled", "organize_include_locked", "organize_sort_after",
            "organize_interval_min", "storage_pages_auto", "storage_pages_manual",
            "log_keep_minutes",
        }
        self.assertEqual(expected - set(app.cfg), set())
        self.assertEqual(app.cfg["fuse_tiers"], [1, 2])
        self.assertEqual(app.cfg["organize_interval_min"], 90)
        self.assertEqual(app.cfg["log_keep_minutes"], 135)

    def test_save_all_from_ui_survives_one_broken_setting(self):
        """有一项取不到值（控件还没建好之类）时，后面的设置也必须照常写进配置 ——
        以前这里是一长串赋值，中间断一次后面的就全丢，表现就是「下次启动还原不全」。"""
        scope = load_runtime()
        app = make_app(scope)
        app.cfg = {}
        _wire_ui_vars(app, broken="gear_storage_var")
        app._save_all_from_ui()
        self.assertNotIn("gear_include_storage", app.cfg, "坏掉的那项跳过就行")
        for key in ("auto_fuse_enabled", "deposit_auto_resume", "organize_interval_min",
                    "log_keep_minutes", "storage_pages_manual"):
            self.assertIn(key, app.cfg, f"{key} 不该因为前面一项出错而丢掉")

    def test_every_saved_key_is_read_back_at_startup(self):
        """写进配置的每一项都必须有人读回来，否则就是「存了但下次启动不还原」。"""
        tree = ast.parse(PANEL.read_text(encoding="utf-8"))
        app_cls = next(n for n in tree.body
                       if isinstance(n, ast.ClassDef) and n.name == "App")
        written = set()
        for fn in app_cls.body:
            if isinstance(fn, ast.FunctionDef) and fn.name == "_save_all_from_ui":
                for call in ast.walk(fn):
                    if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) \
                            and call.func.id == "put":
                        written.add(ast.literal_eval(call.args[0]))
        read = set()
        for node in ast.walk(app_cls):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "get" and node.args:
                # 取值时可能带条件表达式（"gear_fuse_tiers" if gear else …），
                # 所以把实参里出现的字符串常量都算作「被读回来」。
                for arg in node.args:
                    read.update(n.value for n in ast.walk(arg)
                                if isinstance(n, ast.Constant)
                                and isinstance(n.value, str))
        self.assertEqual(written - read, set(), "存了却没人读回来的设置 = 下次启动不还原")

    def test_help_tab_and_tooltips_explain_the_deposit_toggles(self):
        """满仓自动停 / 腾出后恢复：开关上要有悬浮解释，说明页签里也要写清楚。"""
        tree = ast.parse(PANEL.read_text(encoding="utf-8"))
        tips, help_lines = {}, []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr == "_toggle_row" and len(node.args) > 1 \
                    and isinstance(node.args[1], ast.Constant):
                tip = next((k.value for k in node.keywords if k.arg == "tip"), None)
                tips[node.args[1].value] = tip.value if isinstance(tip, ast.Constant) else ""
            elif node.func.attr == "_help_block":
                help_lines += [n.value for n in ast.walk(node)
                               if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        for label in ("满仓自动停", "腾出后恢复"):
            self.assertTrue(tips.get(label), f"{label} 开关必须有鼠标悬浮说明")
            self.assertIn("仓库", tips[label], f"{label} 的说明要讲清楚和仓库的关系")
            self.assertTrue(any(label in line for line in help_lines),
                            f"说明页签里也要写清楚{label}")

    def test_gauge_fills_and_tooltip_hide_is_safe(self):
        """占用条要真的画填充；提示关闭时不能引用不存在的变量。"""
        scope = load_runtime()
        gauge = scope["Gauge"].__new__(scope["Gauge"])
        gauge._gw, gauge._gh = 100, 7
        fills = []
        gauge.delete = lambda *a: None
        gauge.create_rectangle = lambda *a, **k: fills.append(k.get("fill"))
        gauge.set(50, 100)
        self.assertEqual(fills[-1], scope["BLUE"], "半仓画蓝色填充")
        gauge.set(100, 100)
        self.assertEqual(fills[-1], scope["RED"], "满仓画红色")
        gauge.set(0, 0)
        self.assertEqual(fills[-1], scope["CARD3"], "总数未知时只剩底色，不能除零")

        tip = scope["Tooltip"].__new__(scope["Tooltip"])
        tip.tip = types.SimpleNamespace(destroy=lambda: None)
        tip._hide()
        self.assertIsNone(tip.tip)


class _Var:
    """够用的假 tk 变量：get()/set() 就够被测的代码用。"""

    def __init__(self, value=False):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value

class _BrokenVar:
    """模拟还没建好的控件：一取值就炸。"""

    def get(self):
        raise RuntimeError("控件还没建好")

    def set(self, value):
        raise RuntimeError("控件还没建好")


def _wire_ui_vars(app, broken=None):
    """把 _save_all_from_ui 会读到的每个控件变量都装上，模拟界面已经建完。"""
    names = {
        "auto_jewel_var": _Var(True), "jewel_storage_var": _Var(True),
        "auto_gear_var": _Var(True), "gear_same_var": _Var(True),
        "gear_storage_var": _Var(True), "auto_acc_var": _Var(False),
        "acc_same_var": _Var(False), "acc_storage_var": _Var(True),
        "auto_dep_var": _Var(True), "lock_var": _Var(True),
        "stop_full_var": _Var(True), "resume_var": _Var(True),
        "arrange_pages_var": _Var(True),
        "auto_org_var": _Var(True), "org_locked_var": _Var(False),
        "org_sort_var": _Var(True), "org_h_var": _Var(1), "org_m_var": _Var(30),
        "pages_auto_var": _Var(True), "pages_var": _Var(3),
        "log_keep_on_var": _Var(True), "log_keep_h_var": _Var(2),
        "log_keep_m_var": _Var(15),
        "jewel_tier_vars": {t: _Var(t <= 2) for t in range(1, 7)},
        "gear_tier_vars": {t: _Var(t <= 3) for t in range(1, 7)},
        "acc_tier_vars": {t: _Var(t <= 3) for t in range(1, 7)},
        "dep_type_vars": {6: _Var(True), 4: _Var(True), 7: _Var(False), 1: _Var(False)},
        "dep_tier_vars": {t: _Var(t <= 4) for t in range(1, 7)},
        "page_vars": {},
    }
    if broken:
        names[broken] = _BrokenVar()
    for key, value in names.items():
        setattr(app, key, value)
    return names


def benchmark(path):
    scope = load_runtime()
    app = make_app(scope)
    start_cpu, start_wall = time.process_time(), time.perf_counter()
    threading.Thread(target=app._monitor, daemon=True).start()
    time.sleep(2)
    elapsed = time.perf_counter() - start_wall
    cpu = time.process_time() - start_cpu
    print(json.dumps(dict(source=str(path), wall_seconds=round(elapsed, 3),
                          cpu_seconds=round(cpu, 4),
                          one_core_percent=round(cpu / elapsed * 100, 2))))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--benchmark":
        benchmark(Path(sys.argv[2]) if len(sys.argv) > 2 else PANEL)
    else:
        unittest.main()
