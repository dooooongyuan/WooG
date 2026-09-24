#!/usr/bin/env python3
"""WoG 助手面板：实时监听库存，自动合成宝玉/装备/饰品，自动入库，自动整理仓库。

界面分为合成、仓库、经济、统计、日志、说明、邮箱推送和打赏页签，底部常驻状态栏。

线程模型：
- 主线程：tkinter UI + 每秒把勾选状态同步进 self.ui_state（普通字典）
- 监听线程：只读 self.ui_state（普通数据），绝不直接访问 tkinter；
  需要动 UI 的一律通过 self.uiq 投递，由主线程执行

所有 JS 流程都在 宝玉助手.py 里，这里只负责界面与调度。
"""

import json
import html
import os
import queue
import re
import smtplib
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import deque
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import tkinter as tk
from tkinter import messagebox, ttk
if sys.stdout is not None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    except Exception:
        pass

HERE = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parent)
sys.path.insert(0, str(HERE))

import 修复宝玉助手连接 as port_patch  # noqa: E402

from 宝玉助手 import (  # noqa: E402
    CATEGORY_LABELS, CONTENT_ACCESSORY, CONTENT_GEAR,
    Game, js_count, js_deposit, js_equip_count, js_equip_fuse,
    js_jewel_fuse, js_organize, js_runtime_ready, js_current_stage, js_storage_info,
    js_economic_snapshot, load_config, save_config, WS_REQUEST_TIMEOUT,
)

# =====================================================================
# 主题
# =====================================================================
BG = "#0e1015"
CARD = "#191c24"
CARD2 = "#21252f"
CARD3 = "#272c37"
LINE = "#2b303b"
FG = "#e9eef7"
DIM = "#8892a6"
FAINT = "#5c6474"
INK = "#0e1015"

GOLD = "#ffd75e"      # 宝玉
VIOLET = "#a78bfa"    # 装备
CYAN = "#4cc9f0"      # 饰品
BLUE = "#4a94ec"      # 入库
GREEN = "#51cf66"     # 整理
RED = "#ff7a7a"
OKC = "#4ecb71"
LOG_WARNING = "#f0c419"

FONT = "Microsoft YaHei"
MONO = "Consolas"

MONITOR_INTERVAL = 2
STATS_FUSION_RECONCILE_SECONDS = 12.0
STATS_BASELINE_STABLE_SNAPSHOTS = 3
RECONNECT_RETRY_SECONDS = 2
RECONNECT_MAX_RETRY_SECONDS = 30
ECONOMIC_BASELINE_STABLE_SNAPSHOTS = 10
BUSY_TIMEOUT = 300
# 连接端口可用不等于游戏 UI / Puerts / 融合数据已经完成初始化。历史版本
# 不会在端口刚开时反复执行探针；当前版本也固定让游戏先独立完成启动，避免
# Runtime.evaluate 与铁匠窗口首轮绑定交错。
GAME_BOOT_WAIT_SECONDS = 60.0
RUNTIME_PROBE_INTERVAL = 5.0
GAME_SETTLE_SECONDS = 3.0
STATS_FILE = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
              else Path(__file__).resolve().parent) / "宝玉助手统计.json"
MARKET_CACHE_FILE = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
                     else Path(__file__).resolve().parent) / "宝玉助手价格缓存.json"
ECONOMIC_FILE = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
                 else Path(__file__).resolve().parent) / "宝玉助手经济记录.json"
MARKET_CACHE_HOURS = 12
# 手动合成时，产物可能比原料扣除晚几个监听周期；在这段时间内保留待确认批次。
MANUAL_FUSION_RESULT_GRACE_SECONDS = 12.0
# 价格服务全局节流，跨进程/重启持久化在价格缓存中，避免重复点击或轮询触发限流。
MARKET_REQUEST_MIN_INTERVAL_SECONDS = 120.0
MARKET_REQUEST_ERROR_BACKOFF_SECONDS = 600.0
# 每次同步只由本机补少量服务器缺口；不并发、不扫全库存，避免单个用户
# 的 Steam 出口再次形成突发请求。
LOCAL_FETCH_MAX_ITEMS = 8
LOCAL_STEAM_REQUEST_MIN_INTERVAL_SECONDS = 5.0
LOCAL_STEAM_REQUEST_MAX_INTERVAL_SECONDS = 8.0
LOCAL_STEAM_ERROR_BACKOFF_SECONDS = 600.0
# Optional shared cache endpoint.  The public source intentionally ships
# without the author's deployment URL; distributors can set this at build or
# runtime through ``WOG_PRICE_SERVICE_URL``.
PRICE_SERVICE_URL = os.environ.get("WOG_PRICE_SERVICE_URL", "").strip()
PRICE_SERVICE_TOKEN_FILE = ((Path(sys.executable).resolve().parent
                             if getattr(sys, "frozen", False)
                             else Path(__file__).resolve().parent)
                            / "宝玉助手价格服务.token")
PRICE_SERVICE_TOKEN_ENV = "WOG_PRICE_SERVICE_TOKEN"

TIER_NAMES = {1: "下级", 2: "中级", 3: "上级", 4: "高级", 5: "特级", 6: "最高级"}
CAT_ORDER = ("jewel", "accessory", "gear", "material", "other", "any")
LABEL_TO_CAT = {CATEGORY_LABELS[c]: c for c in CAT_ORDER}
STATS_CATEGORIES = ("jewel", "gear", "accessory")
STATS_CATEGORY_LABELS = {"jewel": "宝玉", "gear": "装备", "accessory": "饰品"}
TIER_COLORS = ("#9aa5b1", "#4ecb71", "#4a94ec", "#ff6262", "#a78bfa", "#f0c419")


class StatsStore:
    """按分钟保存本地统计；只记录助手能确认的物品变化和合成结果。"""

    def __init__(self, path=None):
        if path is None:
            # BAT 可以从任意工作目录启动；统计文件应和脚本/exe 放在一起，
            # 避免同一份助手因为启动位置不同而产生多份统计。
            path = globals().get("STATS_FILE")
            if path is None:
                path = ((Path(sys.executable).resolve().parent
                          if getattr(sys, "frozen", False)
                          else Path(__file__).resolve().parent)
                        / "宝玉助手统计.json")
        self.path = Path(path)
        self.lock = threading.RLock()
        self.data = {"version": 2, "buckets": {}}
        self.load()

    def load(self):
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict) and isinstance(raw.get("buckets"), dict):
                version = int(raw.get("version", 1) or 1)
                buckets = raw["buckets"]
                if version < 2:
                    # v1 mixed fusion/count-probe increases into drops.  There
                    # is no reliable source marker to repair those rows, so
                    # discard only the contaminated drop buckets and preserve
                    # confirmed fusion and failure history.
                    buckets = {
                        key: dict(value) for key, value in buckets.items()
                        if isinstance(value, dict)
                    }
                    for bucket in buckets.values():
                        bucket["drops"] = {
                            c: [0, 0, 0, 0, 0, 0] for c in STATS_CATEGORIES}
                self.data = {"version": 2, "buckets": buckets}
        except (OSError, ValueError, TypeError):
            pass

    @staticmethod
    def _key(ts, stage):
        dt = datetime.fromtimestamp(ts)
        return f"{dt:%Y-%m-%d}|{dt:%H:%M}|{stage or '当前关卡'}"

    @staticmethod
    def _blank(stage, ts):
        dt = datetime.fromtimestamp(ts)
        return {
            "date": dt.strftime("%Y-%m-%d"), "hour": dt.hour, "minute": dt.minute,
            "stage": stage or "当前关卡",
            "drops": {c: [0, 0, 0, 0, 0, 0] for c in STATS_CATEGORIES},
            "fusions": {c: [0, 0, 0, 0, 0, 0] for c in STATS_CATEGORIES},
            "fusion_failures": {c: [0, 0, 0, 0, 0, 0] for c in STATS_CATEGORIES},
        }

    def _bucket(self, ts, stage):
        key = self._key(ts, stage)
        bucket = self.data["buckets"].get(key)
        if not isinstance(bucket, dict):
            bucket = self._blank(stage, ts)
            self.data["buckets"][key] = bucket
        for section in ("drops", "fusions", "fusion_failures"):
            bucket.setdefault(section, {})
            for category in STATS_CATEGORIES:
                values = bucket[section].get(category)
                if not isinstance(values, list) or len(values) != 6:
                    bucket[section][category] = [0, 0, 0, 0, 0, 0]
        return bucket

    def add(self, section, category, values, ts=None, stage="当前关卡"):
        if section not in ("drops", "fusions", "fusion_failures") or category not in STATS_CATEGORIES:
            return
        ts = time.time() if ts is None else ts
        with self.lock:
            arr = self._bucket(ts, stage)[section][category]
            for i, value in enumerate(values[:6]):
                try:
                    arr[i] += max(0, int(value))
                except (TypeError, ValueError):
                    pass
            self._save_locked()

    def rows(self, stage="全部关卡", date="全部日期", hours="全部时间"):
        now = datetime.now()
        cutoff = None
        if hours != "全部时间":
            try:
                cutoff = now - timedelta(hours=int(str(hours).rstrip("hH")))
            except (TypeError, ValueError):
                cutoff = None
        with self.lock:
            rows = []
            for row in self.data["buckets"].values():
                if not isinstance(row, dict):
                    continue
                if stage != "全部关卡" and row.get("stage") != stage:
                    continue
                if date != "全部日期" and row.get("date") != date:
                    continue
                try:
                    stamp = datetime.strptime(
                        f"{row.get('date')} {int(row.get('hour', 0)):02d}:"
                        f"{int(row.get('minute', 0)):02d}", "%Y-%m-%d %H:%M")
                except (TypeError, ValueError):
                    continue
                if cutoff and stamp < cutoff.replace(second=0, microsecond=0):
                    continue
                rows.append(row)
            return sorted(rows, key=lambda r: (
                r.get("date", ""), int(r.get("hour", 0)), int(r.get("minute", 0))))

    def choices(self):
        with self.lock:
            stages = sorted({str(r.get("stage")) for r in self.data["buckets"].values()
                             if isinstance(r, dict) and r.get("stage")})
            dates = sorted({str(r.get("date")) for r in self.data["buckets"].values()
                            if isinstance(r, dict) and r.get("date")}, reverse=True)
            return stages, dates

    def reset(self):
        """清空统计桶并原子保存；不影响日志、配置或游戏状态。"""
        with self.lock:
            self.data = {"version": 2, "buckets": {}}
            self._save_locked()

    def _save_locked(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass


class EconomicStore:
    """保存经济页事件和每日估值；与日志、统计文件分开。"""

    def __init__(self, path=None):
        self.path = Path(path or ECONOMIC_FILE)
        self.lock = threading.RLock()
        self.data = {"version": 1, "events": [], "daily": {}}
        self.load()

    def load(self):
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict):
                self.data["events"] = raw.get("events", []) if isinstance(raw.get("events", []), list) else []
                self.data["daily"] = raw.get("daily", {}) if isinstance(raw.get("daily", {}), dict) else {}
        except (OSError, ValueError, TypeError):
            pass

    def add_event(self, event):
        if not isinstance(event, dict):
            return
        with self.lock:
            self.data["events"].append(dict(event))
            self.data["events"] = self.data["events"][-5000:]
            self._save_locked()

    def events(self, kind=None):
        with self.lock:
            rows = list(self.data.get("events", []))
        if kind and kind != "全部":
            rows = [row for row in rows if row.get("kind") == kind]
        return sorted(rows, key=lambda row: float(row.get("timestamp", 0) or 0), reverse=True)

    def replace_events(self, events):
        with self.lock:
            self.data["events"] = list(events)[-5000:]
            self._save_locked()

    def update_daily(self, personal, warehouse, ts=None):
        ts = time.time() if ts is None else ts
        today = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        with self.lock:
            value = {
                "personal": round(float(personal), 2),
                "warehouse": round(float(warehouse), 2),
                "updated_at": ts,
            }
            previous = self.data.setdefault("daily", {}).get(today)
            if not isinstance(previous, dict) or any(
                    abs(float(previous.get(key, 0) or 0) - float(value[key])) > 0.005
                    for key in ("personal", "warehouse")):
                self.data["daily"][today] = value
                self._save_locked()
            return dict(self.data["daily"].get(today, {})), dict(
                self.data["daily"].get(
                    (datetime.fromtimestamp(ts) - timedelta(days=1)).strftime("%Y-%m-%d"), {}))

    def _save_locked(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
            temp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temp.replace(self.path)
        except OSError:
            pass


class MarketPriceCache:
    """Steam 市场价格缓存，只接受名称中明确带 (Tier X) 的结果。"""

    SEARCH_URL = "https://steamcommunity.com/market/search/"

    def __init__(self, path=None, max_age_hours=MARKET_CACHE_HOURS):
        self.path = Path(path or MARKET_CACHE_FILE)
        self.max_age = max(1, int(max_age_hours)) * 3600
        self.lock = threading.RLock()
        self.data = {"version": 2, "items": {}, "meta": {}}
        self.last_refresh_warning = ""
        self.last_local_error = ""
        self.load()

    def load(self):
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict) and isinstance(raw.get("items"), dict):
                meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
                self.data = {"version": 2, "items": raw["items"], "meta": meta}
        except (OSError, ValueError, TypeError):
            pass

    @staticmethod
    def _clean(value):
        value = html.unescape(str(value or ""))
        value = re.sub(r"<[^>]+>", "", value)
        return re.sub(r"\s+", " ", value).strip().casefold()

    @classmethod
    def market_name(cls, item):
        name = str(item.get("market_name") or item.get("name") or "").strip()
        tier = int(item.get("tier") or 0)
        if tier > 0 and not re.search(r"\(\s*tier\s*\d+\s*\)", name, re.I):
            name = f"{name} (Tier {tier})"
        return name

    @classmethod
    def key_for(cls, item):
        return cls._clean(cls.market_name(item))

    @staticmethod
    def _service_token():
        token = os.environ.get(PRICE_SERVICE_TOKEN_ENV, "").strip()
        if token:
            return token
        try:
            token = PRICE_SERVICE_TOKEN_FILE.read_text(encoding="utf-8-sig").strip()
            if token:
                return token
        except (OSError, UnicodeError):
            pass
        return ""

    def refresh_allowed(self):
        """检查全局价格请求冷却；不暴露服务地址或令牌状态。"""
        with self.lock:
            meta = self.data.setdefault("meta", {})
            return time.time() >= float(meta.get("next_request_at", 0) or 0)

    def refresh_wait_seconds(self):
        with self.lock:
            meta = self.data.setdefault("meta", {})
            return max(0.0, float(meta.get("next_request_at", 0) or 0) - time.time())

    def _begin_refresh(self):
        now = time.time()
        with self.lock:
            meta = self.data.setdefault("meta", {})
            if now < float(meta.get("next_request_at", 0) or 0):
                return False
            meta["last_request_at"] = now
            meta["next_request_at"] = now + MARKET_REQUEST_MIN_INTERVAL_SECONDS
            self._save()
        return True

    def _finish_refresh(self, error=False):
        if not error:
            return
        with self.lock:
            meta = self.data.setdefault("meta", {})
            meta["next_request_at"] = max(
                float(meta.get("next_request_at", 0) or 0),
                time.time() + MARKET_REQUEST_ERROR_BACKOFF_SECONDS)
            self._save()

    def _remote_refresh(self, items, force=False, prices=None):
        token = self._service_token()
        if not token:
            return set(), False
        payload = {"items": [{"name": item.get("name", ""),
                              "market_name": item.get("market_name", ""),
                              "tier": int(item.get("tier") or 0)}
                             for item in items],
                   "refresh": bool(force)}
        if prices:
            payload["prices"] = [dict(row) for row in prices if isinstance(row, dict)]
        request = urllib.request.Request(
            PRICE_SERVICE_URL,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": "Bearer " + token,
                     "Content-Type": "application/json",
                     "User-Agent": "WoGAssistant/1.0"},
            method="POST")
        with urllib.request.urlopen(request, timeout=25) as response:
            result = json.loads(response.read(512 * 1024).decode("utf-8", "replace"))
        if not isinstance(result, dict) or result.get("ok") is not True:
            return set(), False
        changed = False
        touched = False
        returned = set()
        for row in result.get("items", []):
            if not isinstance(row, dict):
                continue
            key = self._clean(row.get("key") or row.get("name"))
            if not key:
                continue
            returned.add(key)
            status = str(row.get("status") or "error")
            price = row.get("price")
            record = {"name": row.get("name") or key,
                      "price": float(price) if price is not None else None,
                      "median": row.get("median"),
                      "volume": row.get("volume"),
                      "source": "WoG price service / Steam Community Market",
                      "updated_at": time.time(),
                      "missing": status not in ("ok", "stale"),
                      "status": status}
            with self.lock:
                old = self.data["items"].get(key)
                # 市场服务偶发返回 error/missing 时，不能把已经有价格的缓存
                # 覆盖成 None。保留上次有效报价并记录检查时间，后续仍可重试。
                if (status != "ok" or record["price"] is None) and isinstance(old, dict) \
                        and old.get("price") is not None:
                    old["last_checked_at"] = time.time()
                    touched = True
                    continue
                same_value = isinstance(old, dict) and all(
                    old.get(field) == record.get(field)
                    for field in ("price", "median", "volume", "status", "name"))
                # 服务器即使确认“仍无价格”，也要刷新检查时间；否则旧的 missing
                # 记录会在每轮经济快照后再次触发同步，造成界面持续闪烁。
                record["updated_at"] = float(row.get("updated_at") or time.time())
                self.data["items"][key] = {**(old or {}), **record}
                if not same_value:
                    changed = True
                else:
                    touched = True
        if changed or touched:
            self._save()
        return returned, changed

    def get(self, item, allow_stale=False):
        if int(item.get("tier") or 0) < 3:
            return None
        key = self.key_for(item)
        with self.lock:
            row = self.data["items"].get(key)
            status = str(row.get("status") or ("missing" if row.get("missing") else "ok")) if isinstance(row, dict) else ""
            age_limit = (0 if status == "pending" else
                         10 * 60 if status == "error" else
                         getattr(self, "max_age", MARKET_CACHE_HOURS * 3600))
            if isinstance(row, dict):
                if float(row.get("updated_at", 0) or 0) > time.time() - age_limit:
                    return row
                if allow_stale and row.get("price") is not None:
                    return row
        return None

    def _begin_local_fetch(self):
        now = time.time()
        with self.lock:
            meta = self.data.setdefault("meta", {})
            if now < float(meta.get("next_local_request_at", 0) or 0):
                return False
            meta["last_local_request_at"] = now
            meta["next_local_request_at"] = now + LOCAL_STEAM_REQUEST_MIN_INTERVAL_SECONDS
            self._save()
        return True

    def _record_local_price(self, key, item, price):
        record = price or {
            "name": self.market_name(item), "price": None,
            "source": "Steam Community Market · 本机采集",
            "updated_at": time.time(), "missing": True, "status": "missing",
        }
        record = {**record, "status": "ok" if record.get("price") is not None else "missing",
                  "source": record.get("source") or "Steam Community Market · 本机采集",
                  "updated_at": time.time()}
        with self.lock:
            self.data["items"][key] = record
        return record

    def _local_refresh(self, pairs):
        """只在服务器缺少价格时由本机顺序补价，并返回可上报的结果。"""
        self.last_local_error = ""
        if not pairs:
            return False, []
        if not self._begin_local_fetch():
            self.last_local_error = "cooldown"
            return False, []
        changed = False
        observations = []
        for index, (key, item) in enumerate(pairs[:LOCAL_FETCH_MAX_ITEMS]):
            try:
                price = self._fetch(item)
            except Exception as exc:
                if not self.last_local_error:
                    self.last_local_error = type(exc).__name__
                price = None
            record = self._record_local_price(key, item, price)
            changed = True
            if record.get("price") is not None:
                observations.append({
                    "name": record.get("name") or self.market_name(item),
                    "market_name": self.market_name(item),
                    "tier": int(item.get("tier") or 0),
                    "price": record.get("price"),
                    "median": record.get("median"),
                    "volume": record.get("volume"),
                    "status": "ok",
                })
            if index + 1 < min(len(pairs), LOCAL_FETCH_MAX_ITEMS):
                time.sleep(random.uniform(LOCAL_STEAM_REQUEST_MIN_INTERVAL_SECONDS,
                                           LOCAL_STEAM_REQUEST_MAX_INTERVAL_SECONDS))
        self._save()
        return changed, observations

    def refresh(self, items, force=False):
        self.last_refresh_warning = ""
        changed = False
        unique = {}
        for item in items or []:
            if int(item.get("tier") or 0) >= 3:
                unique[self.key_for(item)] = item
        if not force:
            unique = {key: item for key, item in unique.items()
                      if self.get(item) is None
                      and not self._cooldown_active(key)}
            if not unique:
                return False
        remote_error = None
        if self._service_token():
            pairs = list(unique.items())
            try:
                for offset in range(0, len(pairs), 20):
                    batch = pairs[offset:offset + 20]
                    returned, remote_changed = self._remote_refresh(
                        [item for _, item in batch], force=force)
                    changed = changed or remote_changed
                    if len(returned) < len(batch):
                        raise RuntimeError("价格服务返回数据不完整")
                    # 价格服务的 error 通常是 Steam 短暂限流或网络抖动；
                    # 同一批内只补请求一次 error 项，missing 结果不重复轰炸。
                    transient = []
                    for key, item in batch:
                        row = self.get(item, allow_stale=True)
                        if (isinstance(row, dict)
                                and str(row.get("status") or "").casefold() == "error"):
                            transient.append(item)
                    if transient:
                        _, retry_changed = self._remote_refresh(transient, force=True)
                        changed = changed or retry_changed
            except Exception as exc:
                self._finish_refresh(error=True)
                # Shared cache is an optimization, not a hard dependency.
                # Continue with this machine's limited Steam requests when the
                # service is temporarily unreachable or returns malformed data.
                remote_error = exc
                self.last_refresh_warning = "共享缓存暂时不可用"
            pending = [(key, item) for key, item in pairs if self.get(item) is None]
            local_changed, observations = self._local_refresh(pending)
            changed = changed or local_changed
            if observations:
                try:
                    self._remote_refresh([], prices=observations)
                except Exception:
                    # A failed upload must not discard prices already collected
                    # locally; they remain in the local cache for this session.
                    pass
            if pending and not observations and not self.last_local_error:
                self.last_local_error = "no_listing"
            if self.last_local_error == "HTTPError":
                self.last_refresh_warning = "Steam 请求被限流"
            elif self.last_local_error == "no_listing":
                self.last_refresh_warning = "Steam 搜索页未返回可识别报价"
            elif self.last_local_error == "cooldown":
                self.last_refresh_warning = "本机 Steam 请求冷却中，稍后可再同步"
            elif self.last_local_error:
                self.last_refresh_warning = "本机 Steam 查询失败"
            elif remote_error is not None and not observations:
                self.last_refresh_warning = "共享缓存异常，本机查询未取得新报价"
            return changed

        pending = list(unique.items())
        local_changed, _observations = self._local_refresh(pending)
        changed = changed or local_changed
        if pending and not _observations:
            self.last_refresh_warning = (
                "Steam 请求被限流" if self.last_local_error == "HTTPError"
                else "Steam 搜索页未返回可识别报价" if self.last_local_error == "no_listing"
                else "本机 Steam 请求冷却中，稍后可再同步"
                if self.last_local_error == "cooldown" else "本机 Steam 查询失败")
        return changed

    def _cooldown_active(self, key):
        with self.lock:
            row = self.data["items"].get(key)
        return (isinstance(row, dict)
                and str(row.get("status") or "") == "error"
                and float(row.get("updated_at", 0) or 0) > time.time() - 600)

    def _fetch(self, item):
        market_name = self.market_name(item)
        # Steam's current market UI is React and no longer emits the legacy
        # ``market_listing_item_name`` HTML.  The render endpoint is the
        # stable JSON source used by that page; keep the old parser below as a
        # fallback for older regional responses.
        render_query = urllib.parse.urlencode({
            "query": market_name, "start": 0, "count": 20,
            "search_descriptions": 0, "sort_column": "popular",
            "sort_dir": "desc", "appid": "4891320", "norender": 1,
            "currency": 23,
        })
        render_request = urllib.request.Request(
            "https://steamcommunity.com/market/search/render/?" + render_query,
            headers={"User-Agent": "Mozilla/5.0 WoGAssistant/1.0"})
        try:
            with urllib.request.urlopen(render_request, timeout=8) as response:
                payload = json.loads(response.read(512 * 1024).decode("utf-8", "replace"))
        except Exception:
            payload = {}
        wanted = self._clean(market_name)
        for row in (payload.get("results", []) if isinstance(payload, dict) else []):
            if not isinstance(row, dict):
                continue
            candidate = self._clean(row.get("hash_name") or row.get("name"))
            if candidate != wanted or not re.search(r"\(\s*tier\s*\d+\s*\)", candidate, re.I):
                continue
            cents = row.get("sell_price")
            try:
                price = float(cents) / 100.0
            except (TypeError, ValueError):
                price = None
            if price is not None and price > 0:
                return {"name": market_name, "price": price, "currency": "CNY",
                        "source": "Steam Community Market · 最低卖价",
                        "updated_at": time.time()}

        query = urllib.parse.urlencode({"appid": "4891320", "q": market_name,
                                        "l": "english", "cc": "cn", "norender": "1"})
        request = urllib.request.Request(
            self.SEARCH_URL + "?" + query,
            headers={"User-Agent": "Mozilla/5.0 WoGAssistant/1.0"})
        with urllib.request.urlopen(request, timeout=8) as response:
            raw = response.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw)
            raw = payload.get("results_html", "") if isinstance(payload, dict) else raw
        except ValueError:
            pass
        wanted = self._clean(market_name)
        matches = []
        names = list(re.finditer(
            r"market_listing_item_name[^>]*>\s*(.*?)\s*</span>", raw, re.I | re.S))
        for index, match in enumerate(names):
            candidate = self._clean(match.group(1))
            if candidate != wanted or not re.search(r"\(\s*tier\s*\d+\s*\)", candidate, re.I):
                continue
            tail = raw[match.end(): names[index + 1].start() if index + 1 < len(names) else match.end() + 4000]
            for value in re.findall(r"(?:¥|￥|\$|€|£)\s*([0-9]+(?:[.,][0-9]{1,2})?)", tail):
                try:
                    matches.append(float(value.replace(",", "")))
                except ValueError:
                    pass
        if not matches:
            return None
        return {"name": market_name, "price": min(matches), "currency": "CNY",
                "source": "Steam Community Market · 最低卖价", "updated_at": time.time()}

    def _save(self):
        with self.lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temp = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
                temp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                temp.replace(self.path)
            except OSError:
                pass

# =====================================================================
# 基础控件
# =====================================================================
class ScrollFrame(tk.Frame):
    """无可见滚动条的竖向滚动容器，滚轮只在指针进入时接管。"""

    def __init__(self, master, bg=BG):
        super().__init__(master, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.body = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", self._on_body)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.canvas.bind("<Enter>", self._grab)
        self.canvas.bind("<Leave>", self._release)

    def _on_body(self, _e=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, e):
        self.canvas.itemconfigure(self._win, width=e.width)

    def _grab(self, _e=None):
        self.canvas.bind_all("<MouseWheel>", self._wheel)

    def _release(self, _e=None):
        self.canvas.unbind_all("<MouseWheel>")

    def _wheel(self, e):
        self.canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")


class EconomicGrid(tk.Frame):
    """经济页专用表格：独立单元格着色，滚轮滚动且不显示滚动条。"""

    def __init__(self, parent, columns, height=5):
        super().__init__(parent, bg=CARD)
        self.columns = list(columns)
        self.column_widths = {key: width for key, _heading, width in columns}
        self.headings = {key: heading for key, heading, _width in columns}
        self.rows = []
        self._next_id = 1
        self.colors = {"default": FG, "success": OKC, "failure": RED}
        self.canvas = tk.Canvas(self, bg=CARD2, highlightthickness=1,
                                highlightbackground=LINE, bd=0,
                                height=max(105, height * 25 + 28))
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.canvas.bind("<Shift-MouseWheel>", self._wheel_horizontal)
        self.canvas.bind("<Button-4>", lambda _event: self.canvas.yview_scroll(-1, "units"))
        self.canvas.bind("<Button-5>", lambda _event: self.canvas.yview_scroll(1, "units"))
        self.canvas.bind("<Shift-Button-4>", lambda _event: self.canvas.xview_scroll(-1, "units"))
        self.canvas.bind("<Shift-Button-5>", lambda _event: self.canvas.xview_scroll(1, "units"))
        self.canvas.bind("<Configure>", lambda _event: self._draw())
        self._draw()

    def _wheel(self, event):
        step = int(-event.delta / 120) if event.delta else 0
        if step:
            self.canvas.yview_scroll(step, "units")
        return "break"

    def _wheel_horizontal(self, event):
        step = int(-event.delta / 120) if event.delta else 0
        if step:
            self.canvas.xview_scroll(step, "units")
        return "break"

    def heading(self, key, text=None):
        if text is not None:
            self.headings[key] = text
            self._draw()

    def column(self, key, width=None, **_kwargs):
        if width is not None:
            self.column_widths[key] = int(width)
            self._draw()

    def tag_configure(self, tag, foreground=None, **_kwargs):
        if foreground:
            self.colors[tag] = foreground

    def insert(self, _parent, _index, values=(), tags=(), cell_tags=None):
        row_id = f"row{self._next_id}"
        self._next_id += 1
        self.rows.append({"id": row_id, "values": tuple(values),
                          "tags": tuple(tags or ()), "cell_tags": tuple(cell_tags or ())})
        self._draw()
        return row_id

    def get_children(self):
        return tuple(row["id"] for row in self.rows)

    def delete(self, *children):
        if not children:
            self.rows.clear()
        else:
            wanted = set(children)
            self.rows = [row for row in self.rows if row["id"] not in wanted]
        self._draw()

    def _draw(self):
        if not hasattr(self, "canvas"):
            return
        self.canvas.delete("all")
        x_positions = [0]
        for key, _heading, _width in self.columns:
            x_positions.append(x_positions[-1] + self.column_widths.get(key, 60))
        total_width = x_positions[-1]
        heading_height = 27
        row_height = 25
        self.canvas.create_rectangle(0, 0, total_width, heading_height,
                                     fill=CARD3, outline=LINE)
        for index, (key, heading, _width) in enumerate(self.columns):
            left, right = x_positions[index], x_positions[index + 1]
            self.canvas.create_text((left + right) / 2, heading_height / 2,
                                    text=heading, fill=FG, font=(FONT, 8, "bold"))
            self.canvas.create_line(right, 0, right, heading_height + row_height * len(self.rows),
                                    fill=LINE)
        for row_index, row in enumerate(self.rows):
            top = heading_height + row_index * row_height
            if row_index % 2:
                self.canvas.create_rectangle(0, top, total_width, top + row_height,
                                             fill=CARD, outline="")
            cell_tags = row.get("cell_tags", ())
            for index, value in enumerate(row["values"]):
                if index >= len(self.columns):
                    break
                key = self.columns[index][0]
                left, right = x_positions[index], x_positions[index + 1]
                tag = cell_tags[index] if index < len(cell_tags) else "default"
                color = self.colors.get(tag, self.colors.get("default", FG))
                self.canvas.create_text((left + right) / 2, top + row_height / 2,
                                        text=str(value), fill=color, font=(FONT, 8))
            self.canvas.create_line(0, top + row_height, total_width, top + row_height,
                                    fill=LINE)
        self.canvas.configure(scrollregion=(0, 0, total_width,
                                            heading_height + row_height * len(self.rows)))


class Card(tk.Frame):
    """带标题和左侧色条的卡片。内容放进 self.body。"""

    def __init__(self, parent, title, accent, subtitle=None):
        super().__init__(parent, bg=LINE)
        inner = tk.Frame(self, bg=CARD)
        inner.pack(fill="both", expand=True, padx=1, pady=1)

        head = tk.Frame(inner, bg=CARD)
        head.pack(fill="x", padx=12, pady=(10, 0))
        tk.Frame(head, bg=accent, width=3, height=15).pack(side="left", pady=1)
        tk.Label(head, text=title, bg=CARD, fg=FG,
                 font=(FONT, 10, "bold")).pack(side="left", padx=(8, 0))
        self.head_right = tk.Frame(head, bg=CARD)
        self.head_right.pack(side="right")
        if subtitle:
            tk.Label(head, text=subtitle, bg=CARD, fg=FAINT,
                     font=(FONT, 8)).pack(side="left", padx=(8, 0))

        self.body = tk.Frame(inner, bg=CARD)
        self.body.pack(fill="x", padx=12, pady=(9, 12))


class Toggle(tk.Canvas):
    """开关：比 Checkbutton 清楚得多。"""

    W, H = 38, 20

    def __init__(self, parent, var, command=None, on_color=GREEN, bg=CARD):
        super().__init__(parent, width=self.W, height=self.H, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.var = var
        self.command = command
        self.on_color = on_color
        self.bind("<Button-1>", self._click)
        self._draw()

    def _click(self, _e=None):
        self.var.set(not bool(self.var.get()))
        self._draw()
        if self.command:
            self.command()

    def sync(self):
        self._draw()

    def _draw(self):
        self.delete("all")
        on = bool(self.var.get())
        track = self.on_color if on else "#3a4150"
        # 胶囊轨道：两个圆 + 中间矩形
        self.create_oval(0, 1, 18, 19, fill=track, outline="")
        self.create_oval(self.W - 19, 1, self.W - 1, 19, fill=track, outline="")
        self.create_rectangle(9, 1, self.W - 10, 19, fill=track, outline="")
        kx = self.W - 19 if on else 1
        self.create_oval(kx + 1, 3, kx + 16, 17,
                         fill="#ffffff" if on else "#98a2b3", outline="")


class Chip(tk.Label):
    """可点的小胶囊，用于多选。"""

    def __init__(self, parent, text, var, command=None, color=BLUE, bg=CARD):
        super().__init__(parent, text=text, bg=bg, fg=DIM, font=(FONT, 9),
                         padx=11, pady=4, cursor="hand2")
        self.var = var
        self.command = command
        self.color = color
        self.bind("<Button-1>", self._click)
        self._draw()

    def _click(self, _e=None):
        self.var.set(not bool(self.var.get()))
        self._draw()
        if self.command:
            self.command()

    def sync(self):
        self._draw()

    def _draw(self):
        on = bool(self.var.get())
        self.configure(bg=self.color if on else CARD2,
                       fg=INK if on else DIM)


class TierTile(tk.Frame):
    """品级方块：既是显示也是选择。显示件数与可合成批数。"""

    def __init__(self, parent, name, var, command=None, accent=GOLD, bg=CARD):
        super().__init__(parent, bg=CARD2, highlightthickness=1,
                         highlightbackground=LINE, cursor="hand2")
        self.var = var
        self.command = command
        self.accent = accent
        self.bg = bg
        self.name_l = tk.Label(self, text=name, bg=CARD2, fg=FAINT, font=(FONT, 8))
        self.name_l.pack(pady=(6, 0))
        self.value_l = tk.Label(self, text="—", bg=CARD2, fg=FG, font=(FONT, 13, "bold"))
        self.value_l.pack()
        self.sub_l = tk.Label(self, text="", bg=CARD2, fg=FAINT, font=(FONT, 8))
        self.sub_l.pack(pady=(0, 6))
        for w in (self, self.name_l, self.value_l, self.sub_l):
            w.bind("<Button-1>", self._click)

    def _click(self, _e=None):
        self.var.set(not bool(self.var.get()))
        self.command and self.command()
        self.sync()

    def set_data(self, value, sub, color, ready=False):
        self.value_l.configure(text=value, fg=color)
        self.sub_l.configure(text=sub, fg=(self.accent if ready else FAINT))
        self.sync()

    def sync(self):
        on = bool(self.var.get())
        border = self.accent if on else LINE
        self.configure(highlightbackground=border, highlightcolor=border)
        self.name_l.configure(fg=self.accent if on else FAINT)


class Gauge(tk.Canvas):
    """仓库占用条。"""

    def __init__(self, parent, width=300, height=7, bg=CARD):
        super().__init__(parent, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0)
        # 不能叫 self._w / self._h：_w 是 tkinter 内部存控件路径名的属性，
        # 一旦覆盖，连 pack() 都会报 "must be name of window"
        self._gw = width
        self._gh = height

    def set(self, used, total):
        self.delete("all")
        w, h = self._gw, self._gh
        self.create_rectangle(0, 0, w, h, fill=CARD3, outline="")
        fr = 0.0 if not total else max(0.0, min(1.0, used / float(total)))
        if fr > 0:
            # 满仓涂红，省得只靠状态栏那行小字提醒
            self.create_rectangle(0, 0, max(2, int(w * fr)), h,
                                  fill=(RED if fr >= 1.0 else BLUE), outline="")


class Tooltip:
    """悬浮提示：鼠标停在控件上时在下方弹一小块说明。"""

    def __init__(self, widgets, text, wrap=320):
        if not text:
            return
        self.text = text
        self.wrap = wrap
        self.tip = None
        for w in widgets:
            w.bind("<Enter>", self._show, add="+")
            w.bind("<Leave>", self._hide, add="+")
            w.bind("<ButtonPress>", self._hide, add="+")

    def _show(self, event=None):
        if self.tip is not None:
            return
        w = event.widget if event else None
        if w is None:
            return
        try:
            x = w.winfo_rootx() + 10
            y = w.winfo_rooty() + w.winfo_height() + 6
        except Exception:
            return
        self.tip = tk.Toplevel(w)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        self.tip.attributes("-topmost", True)
        tk.Label(self.tip, text=self.text, bg="#2b303b", fg=FG, font=(FONT, 8),
                 justify="left", padx=9, pady=6, wraplength=self.wrap,
                 highlightthickness=1, highlightbackground=LINE).pack()

    def _hide(self, _event=None):
        if self.tip is not None:
            try:
                self.tip.destroy()
            except Exception:
                pass
            self.tip = None


def hint_label(parent, bg=CARD):
    lb = tk.Label(parent, text="", bg=bg, fg=FAINT, font=(FONT, 8),
                  anchor="w", justify="left", wraplength=420)
    return lb


# =====================================================================
# 主界面
# =====================================================================
class App:
    # 类级默认值：即使对象没走完 __init__（测试里用 __new__ 造），也不会 AttributeError
    busy = False
    busy_since = 0.0
    _dep_note_key = None
    _game_op_lock = threading.RLock()

    DEPOSIT_LABELS = {6: "宝玉", 7: "饰品", 4: "装备", 1: "材料"}

    TABS = (("合成", 0), ("仓库", 1), ("经济", 2), ("统计", 3), ("日志", 4),
            ("说明", 5), ("邮箱推送", 6), ("打赏", 7))

    def __init__(self, root):
        self.root = root
        root.title("WoG 助手")
        root.configure(bg=BG)
        root.geometry("560x950")
        root.minsize(530, 680)

        self.cfg = load_config()
        self.game = None
        self.logq = queue.Queue()
        self.uiq = queue.Queue()
        self.stats = {}
        self.storage_info = {}
        self.stats_store = StatsStore()
        self.economic_store = EconomicStore()
        self.market_cache = MarketPriceCache()
        self.economic_snapshot = []
        self._economic_previous = None
        self._economic_tracking_ready = False
        self._economic_baseline_signature = None
        self._economic_baseline_stable = 0
        self._economic_pending_outputs = {"jewel": [0] * 6, "gear": [0] * 6, "accessory": [0] * 6}
        self._economic_auto_fusion_until = 0.0
        self._economic_pending_manual_failures = []
        self._market_refreshing = False
        self._mail_send_queue = queue.Queue()
        self._mail_worker_running = False
        self._mail_worker_lock = threading.Lock()
        self._economic_fusion_page = 0
        self._economic_drop_page = 0
        self._stats_previous = {"jewel": None, "gear": None, "accessory": None}
        # Count probes are display-only.  Drop statistics are sourced from the
        # item-level economic snapshot, while these per-category baselines keep
        # reconnects and material-range changes from looking like drops.
        self._stats_tracking_ready = {category: False for category in STATS_CATEGORIES}
        self._stats_baseline_signature = {category: None for category in STATS_CATEGORIES}
        self._stats_baseline_stable = {category: 0 for category in STATS_CATEGORIES}
        self._stats_count_modes = {category: None for category in STATS_CATEGORIES}
        # 合成成功产物会先出现在库存快照里；先记账，下一次快照扣除，避免把产物算成掉落。
        self._stats_pending_fusion_outputs = {"jewel": [0] * 6,
                                              "gear": [0] * 6,
                                              "accessory": [0] * 6}
        self._stats_pending_fusion_returns = {category: [0] * 6
                                             for category in STATS_CATEGORIES}
        # Inventory counters arrive before economic fusion reconciliation. Hold
        # positive deltas briefly so fusion outputs can claim them before they
        # are committed to the drop chart.
        self._stats_pending_drop_deltas = {
            category: [[] for _ in range(6)] for category in STATS_CATEGORIES}
        self._stats_pending_verified_drops = []
        self.current_stage = "当前关卡"
        self._migrate_legacy_manual_fusion_names()

        self.gear_tiles = {}
        self.acc_tiles = {}
        self.jewel_tiles = {}
        self.page_rows = []
        self.page_vars = {}
        self.detected_pages = 0
        self._page_rows_built_for = None
        self.buttons = []
        self.toggles = []
        self.chips = []
        self.deposit_stopped_full = False
        self.ui_state = {}
        self._tab = 0
        self._tab_frames = {}
        self._tab_buttons = {}
        self._log_rows = deque()          # (时间戳, 文本)，用于按时间清理日志
        self._stop_event = threading.Event()
        self._connect_lock = threading.Lock()
        self._connecting = False
        # Game.eval() 只保证单次请求的收发不串包；合成、入库、整理等流程
        # 会连续改动游戏状态，必须把整段流程锁住，不能让监听/刷新插进中间。
        self._game_op_lock = threading.RLock()
        self._game_ready_at = 0.0
        # WebSocket 已连接和游戏脚本已就绪是两个阶段。连接阶段禁止任何业务
        # 查询，避免首次刷新/自动入库抢在铁匠窗口完成初始化之前执行。
        self._runtime_ready = False
        self._data_ready = False

        self._init_vars()
        self._build_ui()
        self._apply_geometry()
        self._push_state()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._start_connect()
        self._poll_log()
        self._poll_updates()
        self._sync_ui_state()
        threading.Thread(target=self._monitor, daemon=True).start()

    def _init_vars(self):
        cfg = self.cfg
        # 自动合成/自动入库每次启动强制全关，不读配置里的旧值——
        # 避免「一开 exe 就开始合成」。想开就点合成页顶部的「一键开启」，
        # 本次会话内状态正常记忆，重启后回到全关。
        # 这里仍读取旧值，保证配置字段有明确的启动读取点；为安全起见不直接
        # 把它们用于控件初始值，用户必须在游戏稳定后主动开启自动操作。
        self._saved_auto_flags = (
            bool(cfg.get("auto_fuse_enabled", False)),
            bool(cfg.get("auto_gear_fuse_enabled", False)),
            bool(cfg.get("auto_acc_fuse_enabled", False)),
            bool(cfg.get("auto_deposit_enabled", False)),
        )
        self.auto_jewel_var = tk.BooleanVar(value=False)
        self.auto_gear_var = tk.BooleanVar(value=False)
        self.auto_acc_var = tk.BooleanVar(value=False)
        self.auto_dep_var = tk.BooleanVar(value=False)
        self.auto_org_var = tk.BooleanVar(value=cfg.get("auto_organize_enabled", False))
        self.log_keep_on_var = tk.BooleanVar(
            value=int(cfg.get("log_keep_minutes", 60) or 0) > 0)
        self.mail_push_var = tk.BooleanVar(value=bool(cfg.get("mail_push_enabled", False)))
        self.mail_push_name_var = tk.StringVar(
            value=cfg.get("mail_push_name", "WoG宝玉助手"))
        self.mail_sender_var = tk.StringVar(value=cfg.get("mail_sender", ""))
        self.mail_recipient_var = tk.StringVar(value=cfg.get("mail_recipient", ""))
        self.mail_auth_var = tk.StringVar(value=cfg.get("mail_auth_code", ""))
        selected = set(cfg.get("mail_fusion_targets", [4]))
        self.mail_target_vars = {tier: tk.BooleanVar(value=tier in selected)
                                 for tier in range(1, 7)}

    # ------------------------------------------------------------------
    # 骨架
    def _apply_geometry(self):
        """还原上次关窗时的位置尺寸；值不合理就忽略。"""
        geom = (self.cfg.get("window_geometry") or "").strip()
        if not re.fullmatch(r"\d+x\d+([+-]\d+[+-]\d+)?", geom):
            return
        try:
            self.root.geometry(geom)
        except Exception:
            pass

    def _on_close(self):
        """关窗时把窗口位置和当前设置一起存下来，下次原样还原。"""
        try:
            self._stop_event.set()
            if self.game is not None:
                self.game.close()
                self.game = None
        except Exception:
            pass
        try:
            geom = self.root.winfo_geometry()
            if re.fullmatch(r"\d+x\d+([+-]\d+[+-]\d+)?", geom or ""):
                self.cfg["window_geometry"] = geom
        except Exception:
            pass
        try:
            self._push_state()
            self._save_all_from_ui()
        except Exception:
            pass
        try:
            save_config(self.cfg)
        except Exception as exc:
            # 存盘失败必须说出来：否则用户以为设置记住了，下次启动还是旧的
            messagebox.showwarning(
                "设置没存上",
                "配置写入失败，这次的选择下次启动不会还原：\n" + str(exc)[:200])
        try:
            self.root.destroy()
        except Exception:
            pass

    def _cfg_put(self, key, getter):
        """逐项写配置：这一项取值失败就跳过，不影响后面的项。"""
        try:
            self.cfg[key] = getter()
        except Exception:
            pass

    def _save_all_from_ui(self):
        """把界面上所有选择都落盘一次，不用靠每个控件各自触发。

        每项单独取、单独兜异常：以前这里是一长串赋值，中间任何一项取不到值
        （界面还没建好、输入框里是乱码…）就会中断，后面的设置全都写不进去，
        表现出来就是「下次启动还原不全」。现在一项失败不影响其余项。
        """
        put = self._cfg_put
        put("auto_fuse_enabled", lambda: self.auto_jewel_var.get())
        put("fuse_tiers",
            lambda: [t for t in range(1, 7) if self.jewel_tier_vars[t].get()])
        put("include_storage", lambda: self.jewel_storage_var.get())
        put("auto_gear_fuse_enabled", lambda: self.auto_gear_var.get())
        put("gear_fuse_tiers",
            lambda: [t for t in range(1, 7) if self.gear_tier_vars[t].get()])
        put("gear_same_level", lambda: self.gear_same_var.get())
        put("gear_include_storage", lambda: self.gear_storage_var.get())
        put("auto_acc_fuse_enabled", lambda: self.auto_acc_var.get())
        put("acc_fuse_tiers",
            lambda: [t for t in range(1, 7) if self.acc_tier_vars[t].get()])
        put("acc_same_level", lambda: self.acc_same_var.get())
        put("acc_include_storage", lambda: self.acc_storage_var.get())
        put("auto_deposit_enabled", lambda: self.auto_dep_var.get())
        put("deposit_item_types",
            lambda: [k for k, v in self.dep_type_vars.items() if v.get()])
        put("deposit_tiers",
            lambda: [t for t in range(1, 7) if self.dep_tier_vars[t].get()])
        put("deposit_exclude_locked", lambda: self.lock_var.get())
        put("deposit_stop_when_full", lambda: self.stop_full_var.get())
        put("deposit_auto_resume", lambda: self.resume_var.get())
        put("deposit_arrange_pages", lambda: self.arrange_pages_var.get())
        put("auto_organize_enabled", lambda: self.auto_org_var.get())
        put("organize_include_locked", lambda: self.org_locked_var.get())
        put("organize_sort_after", lambda: self.org_sort_var.get())
        put("organize_interval_min", lambda: self._interval_minutes())
        put("storage_pages_auto", lambda: self.pages_auto_var.get())
        put("storage_pages_manual", lambda: self._int_of(self.pages_var, 3, 1, 20))
        # 页数行还没建出来的时候保持原值，不能写成空字典把之前的分页方案抹掉
        put("storage_page_cats",
            lambda: (self._page_cats_for(
                self._effective_pages(self.ui_state, self.storage_info), self.ui_state)
                if self.page_vars else (self.cfg.get("storage_page_cats") or {})))
        put("log_keep_minutes",
            lambda: self._keep_minutes() if self.log_keep_on_var.get() else 0)
        # 邮件设置只由“保存设置”按钮写入。关窗时不能把尚未确认的输入顺手保存，
        # 否则保存按钮无法防止误点、误输入。

    # ------------------------------------------------------------------
    def _build_ui(self):
        head = tk.Frame(self.root, bg=BG)
        head.pack(fill="x", padx=14, pady=(12, 8))
        left = tk.Frame(head, bg=BG)
        left.pack(side="left")
        tk.Label(left, text="WoG 助手", bg=BG, fg=FG,
                 font=(FONT, 15, "bold")).pack(anchor="w")
        tk.Label(left, text="自动合成 · 自动入库 · 仓库整理", bg=BG, fg=FAINT,
                 font=(FONT, 8)).pack(anchor="w")
        self.conn_label = tk.Label(head, text="连接中…", bg=BG, fg=DIM, font=(FONT, 9))
        self.conn_label.pack(side="right", pady=(6, 0))

        self._build_tabbar()
        # 状态栏必须先 pack（side=bottom），否则下面 fill+expand 的内容区会把它挤没
        self._build_statusbar()

        wrap = tk.Frame(self.root, bg=BG)
        wrap.pack(fill="both", expand=True, padx=0, pady=(2, 0))

        self._tab_frames[0] = ScrollFrame(wrap, bg=BG)
        self._tab_frames[1] = ScrollFrame(wrap, bg=BG)
        # 日志和说明不需要滚动体，直接用普通 Frame
        self._tab_frames[2] = ScrollFrame(wrap, bg=BG)
        self._tab_frames[3] = ScrollFrame(wrap, bg=BG)
        self._tab_frames[4] = tk.Frame(wrap, bg=BG)
        self._tab_frames[5] = ScrollFrame(wrap, bg=BG)
        self._tab_frames[6] = ScrollFrame(wrap, bg=BG)
        self._tab_frames[7] = ScrollFrame(wrap, bg=BG)

        self._build_fuse_tab(self._tab_frames[0].body)
        self._build_store_tab(self._tab_frames[1].body)
        self._build_economic_tab(self._tab_frames[2].body)
        self._build_stats_tab(self._tab_frames[3].body)
        self._build_log_tab(self._tab_frames[4])
        self._build_help_tab(self._tab_frames[5].body)
        self._build_mail_tab(self._tab_frames[6].body)
        self._build_donation_tab(self._tab_frames[7].body)

        self._select_tab(0)
        self._select_tab(0)

    def _build_tabbar(self):
        bar = tk.Frame(self.root, bg=BG)
        bar.pack(fill="x", padx=14)
        for name, idx in self.TABS:
            cell = tk.Frame(bar, bg=BG)
            cell.pack(side="left", padx=(0, 6))
            lb = tk.Label(cell, text=name, bg=BG, fg=FAINT, font=(FONT, 10),
                          padx=14, pady=6, cursor="hand2")
            lb.pack()
            underline = tk.Frame(cell, bg=BG, height=2)
            underline.pack(fill="x")
            lb.bind("<Button-1>", lambda _e, i=idx: self._select_tab(i))
            self._tab_buttons[idx] = (lb, underline)

    def _select_tab(self, idx):
        self._tab = idx
        for i, (lb, ul) in self._tab_buttons.items():
            active = (i == idx)
            lb.configure(fg=FG if active else FAINT)
            ul.configure(bg=BLUE if active else BG)
        for i, frame in self._tab_frames.items():
            if i == idx:
                frame.pack(fill="both", expand=True, padx=14, pady=(8, 0))
            else:
                frame.pack_forget()

    def _build_statusbar(self):
        bar = tk.Frame(self.root, bg=CARD)
        bar.pack(fill="x", side="bottom")
        inner = tk.Frame(bar, bg=CARD)
        inner.pack(fill="x", padx=14, pady=7)
        self.status_label = tk.Label(inner, text="启动中…", bg=CARD, fg=DIM,
                                     font=(FONT, 8), anchor="w")
        self.status_label.pack(side="left", fill="x", expand=True)
        self.storage_mini = tk.Label(inner, text="仓库 —", bg=CARD, fg=DIM,
                                     font=(FONT, 8))
        self.storage_mini.pack(side="right")

    # ------------------------------------------------------------------
    # 页签一：合成
    # ------------------------------------------------------------------
    def _build_fuse_tab(self, body):
        self._build_auto_quick_card(body)
        self._build_jewel_card(body)
        self._build_fuse_card(body, role="gear")
        self._build_fuse_card(body, role="acc")

    # ---------- 一键自动开关 ----------
    def _build_auto_quick_card(self, parent):
        card = Card(parent, "自动快捷", GREEN,
                    "宝玉 / 装备 / 饰品 自动合成 + 自动入库")
        card.pack(fill="x", pady=(0, 10))
        row = tk.Frame(card.body, bg=CARD)
        row.pack(fill="x")
        self._button(row, "一键开启", self.enable_all_auto, GREEN, INK,
                     side="left", expand=True)
        self._button(row, "一键关闭", self.disable_all_auto, RED, INK,
                     side="left", expand=True)

    # ---------- 宝玉 ----------
    def _build_jewel_card(self, parent):
        card = Card(parent, "宝玉合成", GOLD, "6 颗同品级 → 1 颗下一品级")
        card.pack(fill="x", pady=(0, 10))
        row = tk.Frame(card.body, bg=CARD)
        row.pack(fill="x")
        self.jewel_tier_vars = {}
        for tier in range(1, 7):
            var = tk.BooleanVar(value=tier in self.cfg.get("fuse_tiers", [1, 2, 3]))
            self.jewel_tier_vars[tier] = var
            tile = TierTile(row, f"T{tier}", var, command=self.on_jewel_tier_change,
                            accent=GOLD)
            tile.pack(side="left", fill="x", expand=True, padx=2)
            self.jewel_tiles[tier] = tile
        self.jewel_hint = hint_label(card.body)
        self.jewel_hint.pack(fill="x", pady=(8, 0))

        opts = tk.Frame(card.body, bg=CARD)
        opts.pack(fill="x", pady=(8, 0))
        self.jewel_storage_var = tk.BooleanVar(value=self.cfg.get("include_storage", True))
        self._toggle_row(opts, "含仓库物品", self.jewel_storage_var,
                         self.on_jewel_storage, GOLD)
        self._spacer(opts)
        self._toggle_row(opts, "自动合成", self.auto_jewel_var,
                         self.on_toggle_jewel, GOLD)

        self._button(card.body, "立即合成一次", self.manual_jewel, GOLD, INK)

    # ---------- 装备 / 饰品 ----------
    def _build_fuse_card(self, parent, role):
        gear = (role == "gear")
        accent = VIOLET if gear else CYAN
        title = "装备合成" if gear else "饰品合成"
        sub = "6 件同等级 → 1 件" if gear else "3 件同等级 → 1 件（概率出货）"
        card = Card(parent, title, accent, sub)
        card.pack(fill="x", pady=(0, 10))

        row = tk.Frame(card.body, bg=CARD)
        row.pack(fill="x")
        tier_vars = {}
        tiles = {}
        for tier in range(1, 7):
            default = self.cfg.get("gear_fuse_tiers" if gear else "acc_fuse_tiers",
                                   [3] if gear else [1, 2, 3])
            var = tk.BooleanVar(value=tier in default)
            tier_vars[tier] = var
            tile = TierTile(row, f"T{tier}", var,
                            command=(self.on_gear_option if gear else self.on_acc_option),
                            accent=accent)
            tile.pack(side="left", fill="x", expand=True, padx=2)
            tiles[tier] = tile
        hint = hint_label(card.body)
        hint.pack(fill="x", pady=(8, 0))

        opts = tk.Frame(card.body, bg=CARD)
        opts.pack(fill="x", pady=(8, 0))
        storage_var = tk.BooleanVar(
            value=self.cfg.get("gear_include_storage" if gear else "acc_include_storage", True))
        same_var = tk.BooleanVar(
            value=self.cfg.get("gear_same_level" if gear else "acc_same_level", True))
        opt_cmd = self.on_gear_option if gear else self.on_acc_option
        self._toggle_row(opts, "含仓库物品", storage_var, opt_cmd, accent,
                         tip=("打开后，仓库里的" + ("装备" if gear else "饰品")
                              + "也算作合成材料。\n"
                              "材料基本都放在仓库的话，这个必须打开，否则会显示「没有材料」。"))
        self._spacer(opts)
        self._toggle_row(opts, "仅同等级", same_var, opt_cmd, accent,
                         tip="打开：只把同一穿戴等级的凑成一批（更稳，但材料分散时凑不满）。\n"
                             "关闭：允许按最高等级混合出一批。\n"
                             "例如 6 件里有 Lv40 和 Lv50，用 Lv50 的融合表可以一起合掉。")
        self._spacer(opts)
        self._toggle_row(opts, "自动合成", self.auto_gear_var if gear else self.auto_acc_var,
                         (self.on_toggle_gear if gear else self.on_toggle_acc), accent,
                         tip="打开后每 2 秒检查一次，凑够一批就自动合。")

        self._button(card.body, "立即合成一次",
                     self.manual_gear if gear else self.manual_acc, accent, INK)

        if gear:
            self.gear_tier_vars, self.gear_tiles = tier_vars, tiles
            self.gear_storage_var, self.gear_same_var, self.gear_hint = \
                storage_var, same_var, hint
        else:
            self.acc_tier_vars, self.acc_tiles = tier_vars, tiles
            self.acc_storage_var, self.acc_same_var, self.acc_hint = \
                storage_var, same_var, hint

    # ------------------------------------------------------------------
    # 页签二：仓库
    # ------------------------------------------------------------------
    def _build_store_tab(self, body):
        self._build_deposit_card(body)
        self._build_organize_card(body)

    def _build_deposit_card(self, parent):
        card = Card(parent, "自动入库", BLUE, "把背包里的物品收进仓库")
        card.pack(fill="x", pady=(0, 10))

        self.storage_label = tk.Label(card.body, text="仓库 —/—", bg=CARD, fg=FG,
                                      font=(FONT, 10, "bold"), anchor="w")
        self.storage_label.pack(fill="x")
        self.gauge = Gauge(card.body, width=430)
        self.gauge.pack(fill="x", pady=(5, 2))
        self.storage_sub = tk.Label(card.body, text="", bg=CARD, fg=FAINT,
                                    font=(FONT, 8), anchor="w")
        self.storage_sub.pack(fill="x")

        tk.Label(card.body, text="入库类型", bg=CARD, fg=FAINT,
                 font=(FONT, 8)).pack(anchor="w", pady=(10, 4))
        trow = tk.Frame(card.body, bg=CARD)
        trow.pack(fill="x")
        dep_types = set(self.cfg.get("deposit_item_types", [6, 4]))
        self.dep_type_vars = {}
        for label, key in (("宝玉", 6), ("饰品", 7), ("装备", 4), ("材料(非堆叠)", 1)):
            var = tk.BooleanVar(value=key in dep_types)
            self.dep_type_vars[key] = var
            Chip(trow, label, var, command=self.on_toggle_deposit,
                 color=BLUE).pack(side="left", padx=(0, 6))

        tk.Label(card.body, text="品级", bg=CARD, fg=FAINT,
                 font=(FONT, 8)).pack(anchor="w", pady=(10, 4))
        prow = tk.Frame(card.body, bg=CARD)
        prow.pack(fill="x")
        dep_tiers = set(self.cfg.get("deposit_tiers", [1, 2, 3]))
        self.dep_tier_vars = {}
        for tier in range(1, 7):
            var = tk.BooleanVar(value=tier in dep_tiers)
            self.dep_tier_vars[tier] = var
            Chip(prow, f"T{tier}", var, command=self.on_toggle_deposit,
                 color=BLUE).pack(side="left", padx=(0, 6))

        opts = tk.Frame(card.body, bg=CARD)
        opts.pack(fill="x", pady=(10, 0))
        self.lock_var = tk.BooleanVar(value=self.cfg.get("deposit_exclude_locked", True))
        self._toggle_row(opts, "排除锁定", self.lock_var, self.on_toggle_deposit, BLUE,
                         tip="打开后，被锁定的物品不会被收进仓库。\n"
                             "在游戏里点物品上的小锁就能锁定。")
        self._spacer(opts)
        self.stop_full_var = tk.BooleanVar(value=self.cfg.get("deposit_stop_when_full", True))
        self._toggle_row(opts, "满仓自动停", self.stop_full_var,
                         self.on_toggle_deposit, BLUE,
                         tip="仓库装满时，自动把「自动入库」关掉。\n\n"
                             "为什么要这样：仓库一格都放不下的时候，程序每 2 秒还是会去问一次游戏，\n"
                             "既没用又白跑，日志也会一直被刷。关掉之后就安静了。\n\n"
                             "关掉这个开关 = 即使仓库满了也继续每 2 秒尝试一次。")
        self._spacer(opts)
        self.resume_var = tk.BooleanVar(value=self.cfg.get("deposit_auto_resume", False))
        self._toggle_row(opts, "腾出后恢复", self.resume_var,
                         self.on_toggle_deposit, BLUE,
                         tip="接在「满仓自动停」后面用的：\n"
                             "当初是被「仓库满」自动停掉的那次，等你清出空间后自动把自动入库重新打开。\n\n"
                             "注意：你自己手动关掉的那种，不会自动打开。\n"
                             "不勾也行 —— 腾出空间后手动点一下「自动入库」开关即可。")

        on = tk.Frame(card.body, bg=CARD)
        on.pack(fill="x", pady=(10, 0))
        self.arrange_pages_var = tk.BooleanVar(
            value=self.cfg.get("deposit_arrange_pages", True))
        self._toggle_row(on, "按分类页入库", self.arrange_pages_var,
                         self.on_toggle_deposit, BLUE,
                         tip="入库后自动把物品放到「每页放一类」对应的那页（用下面\n"
                             "「自动整理仓库」里配的分页方案）：装备进装备页、宝玉进宝玉页。\n\n"
                             "打开之前：游戏的入库接口只会往第一个空位塞，所以新收的东西\n"
                             "全堆在第一页，得等下一次整理才会散开。\n\n"
                             "本来就放对的物品不会移动，所以只有刚入库的那几件会被搬；\n"
                             "下面没配分页（全是「不限」）时这一步自动跳过。")
        self._spacer(on)
        self._toggle_row(on, "自动入库", self.auto_dep_var, self.on_toggle_deposit, BLUE,
                         tip="打开后每 2 秒检查一次背包，符合上面筛选的物品自动收进仓库。")
        self._button(card.body, "立即入库一次", self.manual_deposit, BLUE, "#ffffff")

    def _build_organize_card(self, parent):
        card = Card(parent, "自动整理仓库", GREEN, "每页固定放一类")
        card.pack(fill="x", pady=(0, 4))

        top = tk.Frame(card.body, bg=CARD)
        top.pack(fill="x")
        self.pages_label = tk.Label(top, text="检测中…", bg=CARD, fg=GREEN,
                                    font=(FONT, 10, "bold"))
        self.pages_label.pack(side="left")
        self.pages_auto_var = tk.BooleanVar(value=self.cfg.get("storage_pages_auto", True))
        self._toggle_row(top, "自动检测", self.pages_auto_var,
                         self.on_pages_mode, GREEN)
        tk.Label(top, text="手填", bg=CARD, fg=FAINT,
                 font=(FONT, 8)).pack(side="right", padx=(0, 6))
        self.pages_var = tk.IntVar(
            value=int(self.cfg.get("storage_pages_manual", 3) or 3))
        self.pages_spin = tk.Spinbox(top, from_=1, to=20, width=3,
                                     textvariable=self.pages_var,
                                     bg=CARD2, fg=FG, buttonbackground=CARD3,
                                     relief="flat", font=(FONT, 9), justify="center",
                                     command=self.on_pages_mode,
                                     insertbackground=FG, disabledbackground=CARD2,
                                     disabledforeground=FAINT)
        self.pages_spin.pack(side="right")
        self.pages_spin.configure(
            state="normal" if not self.cfg.get("storage_pages_auto", True) else "disabled")

        self.page_frame = tk.Frame(card.body, bg=CARD)
        self.page_frame.pack(fill="x", pady=(8, 0))
        tk.Label(self.page_frame, text="连接游戏后自动列出各页…", bg=CARD, fg=FAINT,
                 font=(FONT, 8)).pack(anchor="w")

        opts = tk.Frame(card.body, bg=CARD)
        opts.pack(fill="x", pady=(10, 0))
        self.org_locked_var = tk.BooleanVar(value=self.cfg.get("organize_include_locked", False))
        self._toggle_row(opts, "连锁定一起搬", self.org_locked_var,
                         self.on_toggle_organize, GREEN,
                         tip="打开后，被锁定的物品也会一起参与整理。\n"
                             "默认关闭：锁定的物品原地不动，占的格子也不会被拿去当目标。")
        self._spacer(opts)
        self.org_sort_var = tk.BooleanVar(value=self.cfg.get("organize_sort_after", False))
        self._toggle_row(opts, "整理后调用游戏排序", self.org_sort_var,
                         self.on_toggle_organize, GREEN,
                         tip="整理完再调一次游戏自带的仓库排序，把页内空隙压实。\n"
                             "它只排当前页内部，不会打乱「每页放一类」的分工。")

        every = tk.Frame(card.body, bg=CARD)
        every.pack(fill="x", pady=(8, 0))
        self._toggle_row(every, "自动整理", self.auto_org_var,
                         self.on_toggle_organize, GREEN,
                         tip="按下面的间隔在后台自动整理仓库。\n"
                             "只移动仓库内的物品，不会买卖或销毁任何东西；\n"
                             "本来就放对的物品不会动。")
        tk.Label(every, text="　　每", bg=CARD, fg=FAINT,
                 font=(FONT, 9)).pack(side="left")
        total_min = int(self.cfg.get("organize_interval_min", 5) or 5)
        self.org_h_var = tk.IntVar(value=total_min // 60)
        self.org_m_var = tk.IntVar(value=total_min % 60)
        self._spin(every, self.org_h_var, 0, 99, command=self.on_toggle_organize
                   ).pack(side="left", padx=(4, 2))
        tk.Label(every, text="小时", bg=CARD, fg=FAINT,
                 font=(FONT, 9)).pack(side="left")
        self._spin(every, self.org_m_var, 0, 59, command=self.on_toggle_organize
                   ).pack(side="left", padx=(6, 2))
        tk.Label(every, text="分钟", bg=CARD, fg=FAINT,
                 font=(FONT, 9)).pack(side="left")
        iv_tip = ("多久整理一次。小时和分钟可以一起用，例如 1 小时 30 分钟。\n"
                  "两个都填 0 会按 1 分钟算。")
        Tooltip([every], iv_tip)

        brow = tk.Frame(card.body, bg=CARD)
        brow.pack(fill="x", pady=(10, 0))
        self._button(brow, "预览计划", self.manual_organize_preview, CARD3, FG,
                     side="left", expand=True)
        self._button(brow, "立即整理", self.manual_organize, GREEN, INK,
                     side="left", expand=True)

    # ------------------------------------------------------------------
    # 页签三：日志
    # ------------------------------------------------------------------
    def _build_log_tab(self, parent):
        head = tk.Frame(parent, bg=BG)
        head.pack(fill="x", pady=(0, 6))
        tk.Label(head, text="运行日志", bg=BG, fg=FG,
                 font=(FONT, 10, "bold")).pack(side="left")
        tk.Button(head, text="清空", command=self._clear_log, bg=CARD2, fg=DIM,
                  activebackground=CARD3, activeforeground=FG, relief="flat",
                  font=(FONT, 8), padx=10, pady=2).pack(side="right")
        keep = tk.Frame(parent, bg=BG)
        keep.pack(fill="x", pady=(0, 6))
        self._toggle_row(keep, "自动清理日志", self.log_keep_on_var,
                         self.on_log_keep, GREEN,
                         tip="只保留最近一段时间的日志，避免挂一整天以后日志越堆越多。\n"
                             "超时的行会自动从上面删掉。")
        tk.Label(keep, text="　　只保留最近", bg=BG, fg=FAINT,
                 font=(FONT, 8)).pack(side="left")
        total_keep = self._cfg_keep_minutes()
        self.log_keep_h_var = tk.IntVar(value=total_keep // 60)
        self.log_keep_m_var = tk.IntVar(value=total_keep % 60)
        self._spin(keep, self.log_keep_h_var, 0, 168, command=self.on_log_keep
                   ).pack(side="left", padx=(4, 2))
        tk.Label(keep, text="小时", bg=BG, fg=FAINT,
                 font=(FONT, 8)).pack(side="left")
        self._spin(keep, self.log_keep_m_var, 0, 59, command=self.on_log_keep
                   ).pack(side="left", padx=(6, 2))
        tk.Label(keep, text="分钟", bg=BG, fg=FAINT,
                 font=(FONT, 8)).pack(side="left")
        Tooltip([keep], "想留多久就填多久，小时和分钟可以一起用。\n"
                        "例如 0 小时 30 分钟 = 只看最近半小时的日志；\n"
                        "1 小时 = 超过一小时的日志行自动删掉。\n"
                        "两个都填 0 按 1 分钟算；关掉左边的开关就完全不按时间清理"
                        "（仍有 2000 行硬上限）。")
        wrap = tk.Frame(parent, bg=LINE)
        wrap.pack(fill="both", expand=True, pady=(0, 8))
        self.log = tk.Text(wrap, bg="#0b0d11", fg="#b9c2d0", relief="flat",
                           font=(MONO, 9), state="disabled", wrap="word",
                           insertbackground=FG, padx=10, pady=8)
        self.log.tag_configure("warning", foreground=LOG_WARNING)
        self.log.pack(fill="both", expand=True, padx=1, pady=1)

    def _build_help_tab(self, body):
        intro = Card(body, "说明", BLUE, "鼠标停在开关上也会弹出对应解释")
        intro.pack(fill="x", pady=(0, 10))
        self._help_block(intro.body, [
            "本工具通过游戏自带的调试端口调用游戏自己的接口，等价于你手点一遍。",
            "合成会消耗材料，入库和整理会移动物品，但不会买卖或销毁任何东西。",
            "所有设置改完会保存进 宝玉助手配置.json，下次启动会还原。",
        ])
        fuse = Card(body, "合成", GOLD, "宝玉 / 装备 / 饰品")
        fuse.pack(fill="x", pady=(0, 10))
        self._help_block(fuse.body, [
            "点品级方块 = 选中/取消该品级；方块中间显示件数，下面显示批数或还需几件。",
            "含仓库物品：把仓库里的也算作材料。材料大多在仓库时必须打开。",
            "仅同等级：只把同一穿戴等级凑一批；关掉后允许按最高等级混合。",
            "自动合成：每 2 秒检查一次，凑够一批就自动合。",
            "宝玉 6 颗合 1 颗；装备 6 件合 1 件；饰品 3 件合 1 件（有成功率）。",
        ])
        dep = Card(body, "入库", BLUE, "把背包里的物品收进仓库")
        dep.pack(fill="x", pady=(0, 10))
        self._help_block(dep.body, [
            "排除锁定：锁定的物品不收。",
            "",
            "满仓自动停 —— 仓库一格格满了以后，自动把「自动入库」关掉：",
            "  仓库放不下东西时，程序每 2 秒还是会去问一次游戏，既没用又白跑，",
            "  日志也会被一直刷；停下来之后就安静等你清出空间。",
            "  想让它满仓了也照旧每 2 秒试一次，把这个开关关掉即可。",
            "",
            "腾出后恢复 —— 接着上面用的：当初是被「仓库满」自动停掉的那次，",
            "  等你在游戏里清出空间（卖掉 / 合成 / 丢掉几件），自动把「自动入库」重新打开。",
            "  你自己手动关掉的那种不会自动开；不勾也行，清完空间手动点一下开关即可。",
            "",
            "按分类页入库 —— 入库后自动把物品放到「每页放一类」对应的那页：",
            "  装备进装备页、宝玉进宝玉页。用的是下面「自动整理仓库」里配的分页方案。",
            "  为什么要这一步：游戏的入库接口只会往第一个空位塞，所以不做的话新收的东西",
            "  全堆在第 1 页，得等下一次整理才散开。本来就放对的物品不会被移动。",
            "  下面没配分页（每页都是「不限」）时这一步会自动跳过。",
            "",
            "金币/经验/钻石等可堆叠材料是全局数量，不在仓库容器里，无法入库。",
        ])
        org = Card(body, "整理仓库", GREEN, "每页固定放一类")
        org.pack(fill="x", pady=(0, 10))
        self._help_block(org.body, [
            "每页一个下拉框选分类；预览计划只看不动；立即整理才真正执行。",
            "自动整理：按下面填的「小时 + 分钟」在后台定期维持，本来就放对的物品不会移动。",
            "  例：填 1 小时 30 分钟 = 每 90 分钟整理一次；两个都填 0 按 1 分钟算。",
            "连锁定一起搬默认关闭；整理后调用游戏排序只整理页内空隙。",
        ])
        misc = Card(body, "日志与配置", DIM)
        misc.pack(fill="x", pady=(0, 10))
        self._help_block(misc.body, [
            "自动清理日志：只保留最近一段时间的日志，同样按「小时 + 分钟」填。",
            "  例：填 1 小时 = 超过一小时的日志行会被自动删掉，挂一整天也不会越堆越多。",
            "  关掉这个开关就不按时间清理（仍有 2000 行硬上限，不会无限涨）。",
            "配置文件写入采用临时文件替换，异常中断不会把 JSON 写成半截。",
            "窗口位置尺寸和所有开关选择都会在关闭面板时保存，下次启动还原。",
        ])

    # ------------------------------------------------------------------
    # 页签三：经济
    # ------------------------------------------------------------------
    def _build_economic_tab(self, body):
        head = tk.Frame(body, bg=BG)
        head.pack(fill="x", pady=(0, 8))
        tk.Label(head, text="经济", bg=BG, fg=FG,
                 font=(FONT, 13, "bold")).pack(side="left")
        self.econ_market_status = tk.Label(head, text="Steam 市场待同步", bg=BG, fg=FAINT,
                                           font=(FONT, 8))
        self.econ_market_status.pack(side="left", padx=(10, 0), pady=(4, 0))
        tk.Button(head, text="同步价格", command=lambda: self._request_market_refresh(manual=True),
                  bg=CARD2, fg=CYAN, activebackground=CARD3, activeforeground=CYAN,
                  relief="flat", highlightthickness=0, font=(FONT, 8),
                  padx=8, pady=3).pack(side="right")

        summary = tk.Frame(body, bg=BG)
        summary.pack(fill="x", pady=(0, 10))
        self.econ_personal = self._build_economic_summary(summary, "个人身价", VIOLET)
        self.econ_personal["frame"].pack(side="left", fill="both", expand=True, padx=(0, 4))
        self.econ_warehouse = self._build_economic_summary(summary, "仓总价", CYAN)
        self.econ_warehouse["frame"].pack(side="left", fill="both", expand=True, padx=(4, 0))

        filters = tk.Frame(body, bg=CARD)
        filters.pack(fill="x", pady=(0, 8))
        tk.Label(filters, text="查询", bg=CARD, fg=FAINT, font=(FONT, 8)).pack(side="left", padx=(10, 4), pady=8)
        self.econ_query_var = tk.StringVar()
        query = tk.Entry(filters, textvariable=self.econ_query_var, bg=CARD2, fg=FG,
                         insertbackground=FG, relief="flat", font=(FONT, 8), width=16)
        query.pack(side="left", pady=6)
        query.bind("<KeyRelease>", lambda _e: self._economic_filter_changed())
        tk.Label(filters, text="类型", bg=CARD, fg=FAINT, font=(FONT, 8)).pack(side="left", padx=(10, 4))
        self.econ_kind_var = tk.StringVar(value="全部")
        kind_menu = tk.OptionMenu(filters, self.econ_kind_var, "全部", "合成", "掉落",
                                  command=lambda *_: self._economic_filter_changed())
        self._style_stats_menu(kind_menu)
        kind_menu.pack(side="left")
        tk.Label(filters, text="目标级", bg=CARD, fg=FAINT, font=(FONT, 8)).pack(side="left", padx=(10, 4))
        self.econ_target_var = tk.StringVar(value="全部")
        target_menu = tk.OptionMenu(filters, self.econ_target_var, "全部", *(f"T{i}" for i in range(1, 7)),
                                    command=lambda *_: self._economic_filter_changed())
        self._style_stats_menu(target_menu)
        target_menu.pack(side="left")
        tk.Label(filters, text="状态", bg=CARD, fg=FAINT, font=(FONT, 8)).pack(side="left", padx=(8, 4))
        self.econ_status_var = tk.StringVar(value="全部")
        status_menu = tk.OptionMenu(filters, self.econ_status_var, "全部", "成功", "失败",
                                    command=lambda *_: self._refresh_economic_view())
        self._style_stats_menu(status_menu)
        status_menu.pack(side="left")
        tk.Label(filters, text="此炉", bg=CARD, fg=FAINT, font=(FONT, 8)).pack(side="left", padx=(8, 4))
        self.econ_verdict_var = tk.StringVar(value="全部")
        verdict_menu = tk.OptionMenu(filters, self.econ_verdict_var, "全部", "得吃", "亏了", "炸炉",
                                     command=lambda *_: self._economic_filter_changed())
        self._style_stats_menu(verdict_menu)
        verdict_menu.pack(side="left")
        tk.Label(filters, text="时间", bg=CARD, fg=FAINT, font=(FONT, 8)).pack(side="left", padx=(8, 4))
        self.econ_time_var = tk.StringVar(value="全部时间")
        time_menu = tk.OptionMenu(filters, self.econ_time_var, "全部时间", "1h", "6h", "12h", "24h", "72h",
                                  command=lambda *_: self._economic_filter_changed())
        self._style_stats_menu(time_menu)
        time_menu.pack(side="left")
        self.econ_total_label = tk.Label(filters, text="共 0 条", bg=CARD, fg=FAINT, font=(FONT, 8))
        self.econ_total_label.pack(side="right", padx=(0, 8))

        self._economic_fusion_page = 0
        self._economic_drop_page = 0

        self._econ_style_tree()
        fusion_card = Card(body, "合成信息", VIOLET, "价格估算 · 炉况判断")
        fusion_card.pack(fill="x", pady=(0, 8))
        self.econ_fusion_tree = self._build_economic_tree(
            fusion_card.body,
            (("tier", "品级", 40), ("target", "目标级", 46), ("type", "类型", 46),
             ("result", "合成结果", 82), ("status", "状态", 48),
             ("input", "原料价值(估)", 58),
             ("output", "产出价值(估)", 58), ("verdict", "此炉", 44), ("time", "时间", 70)))
        self._build_econ_pager(fusion_card.body, "fusion")
        drop_card = Card(body, "掉落信息", GOLD, "库存新增 · 市场估值")
        drop_card.pack(fill="x", pady=(0, 8))
        self.econ_drop_tree = self._build_economic_tree(
            drop_card.body,
             (("name", "名称", 128), ("tier", "品级", 44), ("level", "等级", 52),
             ("quantity", "数量", 44), ("value", "价值(估)", 78), ("time", "时间", 90)))
        self._build_econ_pager(drop_card.body, "drop")
        self._refresh_economic_view()

    def _build_mail_tab(self, body):
        head = tk.Frame(body, bg=BG)
        head.pack(fill="x", pady=(0, 8))
        tk.Label(head, text="邮箱推送", bg=BG, fg=FG,
                 font=(FONT, 13, "bold")).pack(side="left")

        mail = Card(body, "QQ 邮箱", CYAN, "自动合成与指定材料掉落通知")
        mail.pack(fill="x", pady=(0, 8))
        mail_head = tk.Frame(mail.body, bg=CARD)
        mail_head.pack(fill="x", pady=(0, 9))
        self._toggle_row(mail_head, "启用邮件推送", self.mail_push_var,
                         self._mail_settings_changed, CYAN)

        self._mail_entry(mail.body, "推送名称", self.mail_push_name_var)
        self._mail_entry(mail.body, "发送 QQ 邮箱", self.mail_sender_var)
        self._mail_entry(mail.body, "收件邮箱", self.mail_recipient_var)
        self._mail_entry(mail.body, "QQ 授权码", self.mail_auth_var, show="*")
        tk.Label(
            mail.body,
            text="本机配置：仅用于 QQ 邮箱 SSL 验证，不会上传到价格服务或其他服务器。",
            bg=CARD, fg=GOLD, font=(FONT, 8), anchor="w",
        ).pack(fill="x", padx=(94, 0), pady=(0, 9))

        target_row = tk.Frame(mail.body, bg=CARD)
        target_row.pack(fill="x", pady=(2, 8))
        tk.Label(target_row, text="合成目标级", width=12, anchor="w", bg=CARD, fg=FAINT,
                 font=(FONT, 8)).pack(side="left")
        for tier, var in self.mail_target_vars.items():
            tk.Checkbutton(target_row, text=f"T{tier}", variable=var,
                           command=self._mail_settings_changed,
                           bg=CARD, fg=TIER_COLORS[tier - 1], selectcolor=CARD2,
                           activebackground=CARD, activeforeground=TIER_COLORS[tier - 1],
                           font=(FONT, 8), relief="flat",
                           highlightthickness=0).pack(side="left", padx=2)

        tk.Label(
            mail.body,
            text=("仅自动合成会按目标级推送，手动合成不推送。材料掉落只识别 Steam "
                  "市场的 Gold Bar 与 Diamond Pouch。"),
            bg=CARD, fg=DIM, font=(FONT, 8), anchor="w", wraplength=440,
            justify="left",
        ).pack(fill="x", pady=(1, 10))

        actions = tk.Frame(mail.body, bg=CARD)
        actions.pack(fill="x")
        self.mail_save_status = tk.Label(actions, text="修改后请点击保存",
                                         bg=CARD, fg=FAINT, font=(FONT, 8))
        self.mail_save_status.pack(side="left")
        tk.Button(actions, text="保存设置", command=self._save_mail_settings,
                  bg=CYAN, fg=INK, activebackground="#74d7f3", activeforeground=INK,
                  relief="flat", highlightthickness=0, font=(FONT, 8, "bold"),
                  padx=12, pady=5, cursor="hand2").pack(side="right")

    def _mail_entry(self, parent, label, variable, width=20, show=None):
        row = tk.Frame(parent, bg=CARD)
        row.pack(fill="x", pady=(0, 7))
        tk.Label(row, text=label, width=12, anchor="w", bg=CARD, fg=FAINT,
                 font=(FONT, 8)).pack(side="left")
        entry = tk.Entry(row, textvariable=variable, bg=CARD2, fg=FG,
                         insertbackground=FG, relief="flat", font=(FONT, 8),
                         width=width, show=show or "")
        entry.pack(side="left", fill="x", expand=True, ipady=4)
        entry.bind("<KeyRelease>", lambda _event: self._mail_settings_changed())
        return entry

    def _mail_settings_changed(self):
        label = getattr(self, "mail_save_status", None)
        if label is not None:
            label.configure(text="有未保存的修改", fg=GOLD)

    @staticmethod
    def _mail_settings_error(sender, recipient, auth_code, enabled):
        sender = str(sender or "").strip()
        recipient = str(recipient or "").strip()
        auth_code = str(auth_code or "").strip()
        if sender and not re.fullmatch(r"[A-Za-z0-9._%+-]+@qq\.com", sender, re.I):
            return "发送 QQ 邮箱必须填写完整地址，并以 @qq.com 结尾。"
        if recipient and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", recipient):
            return "收件邮箱格式不正确，请填写完整邮箱地址。"
        if enabled and not sender:
            return "启用邮件推送前，请填写发送 QQ 邮箱。"
        if enabled and not recipient:
            return "启用邮件推送前，请填写收件邮箱。"
        if enabled and not auth_code:
            return "启用邮件推送前，请填写 QQ 邮箱授权码。"
        return ""

    @staticmethod
    def _mail_subject(push_name, detail):
        name = re.sub(r"[\r\n]+", " ", str(push_name or "")).strip()[:40]
        detail = re.sub(r"[\r\n]+", " ", str(detail or "")).strip()[:120]
        return f"{name or 'WoG宝玉助手'} | {detail}"

    def _save_mail_settings(self):
        enabled = bool(self.mail_push_var.get())
        sender = self.mail_sender_var.get().strip()
        recipient = self.mail_recipient_var.get().strip()
        auth_code = self.mail_auth_var.get().strip()
        push_name = self.mail_push_name_var.get().strip()
        if not push_name or len(push_name) > 40 or "\r" in push_name or "\n" in push_name:
            messagebox.showwarning("邮件设置未保存", "推送名称不能为空、不能换行，且最多 40 个字符。")
            return False
        error = self._mail_settings_error(sender, recipient, auth_code, enabled)
        if error:
            messagebox.showwarning("邮件设置未保存", error)
            return False
        values = {
            "mail_push_enabled": enabled,
            "mail_push_name": push_name,
            "mail_sender": sender,
            "mail_recipient": recipient,
            "mail_auth_code": auth_code,
            "mail_fusion_targets": [tier for tier, var in self.mail_target_vars.items()
                                    if var.get()],
        }
        old_values = {key: self.cfg.get(key) for key in values}
        self.cfg.update(values)
        try:
            save_config(self.cfg)
        except Exception as exc:
            self.cfg.update(old_values)
            messagebox.showwarning("邮件设置未保存", "配置写入失败：\n" + str(exc)[:200])
            return False
        label = getattr(self, "mail_save_status", None)
        if label is not None:
            label.configure(text="设置已保存", fg=OKC)
        self._log("邮箱推送设置已保存")
        return True

    @staticmethod
    def _mail_drop_name(event):
        """只接受两个准确的 Steam 市场名称，避免“钻石”模糊匹配到其他材料。"""
        allowed = {"gold bar": "Gold Bar", "diamond pouch": "Diamond Pouch"}
        for key in ("market_name", "name"):
            raw = urllib.parse.unquote(str(event.get(key) or "")).strip()
            # 有些库存字段会保存市场 listing URL；只取最后一段再做同样的精确匹配。
            if "/listings/4891320/" in raw.casefold():
                raw = raw.split("/listings/4891320/", 1)[1].split("?", 1)[0].split("#", 1)[0]
            name = re.sub(r"\s+", " ", raw).strip().casefold()
            if name in allowed:
                return allowed[name]
        return ""

    def _notify_mail_event(self, event):
        if threading.current_thread() is not threading.main_thread():
            self._call_soon(lambda event=dict(event): self._notify_mail_event(event))
            return
        if not bool(self.cfg.get("mail_push_enabled", False)):
            return
        if event.get("kind") == "fusion":
            if event.get("manual"):
                return
            try:
                target_tier = int(event.get("target_tier", 0) or 0)
            except (TypeError, ValueError):
                return
            if target_tier not in self.cfg.get("mail_fusion_targets", []):
                return
            detail = f"自动合成 T{event.get('tier')} → T{target_tier}"
            body = self._fusion_mail_html(event)
        elif event.get("kind") == "drop":
            name = self._mail_drop_name(event)
            if not name:
                return
            event = {**event, "name": name}
            detail = f"材料掉落：{name}"
            body = self._drop_mail_html([event])
        else:
            return
        subject = self._mail_subject(self.cfg.get("mail_push_name"), detail)
        sender = str(self.cfg.get("mail_sender", "")).strip()
        recipient = str(self.cfg.get("mail_recipient", "")).strip()
        auth_code = str(self.cfg.get("mail_auth_code", "")).strip()
        if self._mail_settings_error(sender, recipient, auth_code, True):
            return
        self._mail_send_queue.put((sender, recipient, auth_code, subject, body))
        with self._mail_worker_lock:
            if not self._mail_worker_running:
                self._mail_worker_running = True
                threading.Thread(target=self._mail_worker, daemon=True).start()

    def _build_donation_tab(self, body):
        card = Card(body, "支持作者", GREEN, "官方收款码")
        card.pack(fill="x", pady=(0, 8))
        tk.Label(
            card.body,
            text="如果本助手对你有帮助，欢迎打赏支持作者，你的鼓励是持续更新的动力。",
            bg=CARD, fg=FG, font=(FONT, 9), wraplength=440, justify="left", anchor="w",
        ).pack(fill="x", padx=4, pady=(2, 10))
        tk.Label(card.body, text="收款人（**元）", bg=CARD, fg=GOLD,
                 font=(FONT, 9, "bold"), anchor="w").pack(fill="x", padx=4, pady=(0, 8))

        resource_root = Path(getattr(sys, "_MEIPASS", HERE))
        candidates = (resource_root / "打赏收款码.png",
                      HERE / "发行资源" / "打赏收款码.png")
        image_path = next((path for path in candidates if path.is_file()), None)
        if image_path is None:
            tk.Label(card.body, text="收款码图片未找到。", bg=CARD, fg=RED,
                     font=(FONT, 9)).pack(anchor="w", padx=4, pady=8)
            return
        try:
            image = tk.PhotoImage(master=self.root, file=str(image_path))
            factor = max(1, (image.width() + 419) // 420)
            if factor > 1:
                image = image.subsample(factor, factor)
            self._donation_image = image
            tk.Label(card.body, image=image, bg=CARD, bd=0).pack(anchor="center", pady=(0, 4))
        except tk.TclError:
            tk.Label(card.body, text="收款码图片无法加载。", bg=CARD, fg=RED,
                     font=(FONT, 9)).pack(anchor="w", padx=4, pady=8)

    def _mail_base_html(self, title, subtitle, rows):
        rows_html = []
        for index, row in enumerate(rows):
            label, value = row[:2]
            value_color = row[2] if len(row) > 2 else "#17212f"
            background = "#ffffff" if index % 2 == 0 else "#f5f7fa"
            rows_html.append(
                "<tr>"
                f"<td style='padding:12px 14px;background:{background};color:#657286;"
                "font-family:Arial,Microsoft YaHei,sans-serif;font-size:13px;line-height:20px;"
                "border-bottom:1px solid #e7ebf0'>"
                f"{html.escape(str(label))}</td>"
                f"<td style='padding:12px 14px;background:{background};color:{value_color};"
                "font-family:Arial,Microsoft YaHei,sans-serif;font-size:14px;font-weight:600;"
                "line-height:20px;text-align:right;border-bottom:1px solid #e7ebf0'>"
                f"{html.escape(str(value))}</td></tr>")
        return (
            "<!doctype html><html><head><meta charset='utf-8'></head>"
            "<body style='margin:0;padding:24px 12px;background-color:#f1f4f8;"
            "font-family:Arial,Microsoft YaHei,sans-serif;color:#17212f'>"
            "<table role='presentation' width='100%' cellpadding='0' cellspacing='0' border='0' "
            "style='width:100%;border-collapse:collapse;background-color:#f1f4f8'>"
            "<tr><td align='center' style='padding:4px'>"
            "<table role='presentation' width='560' cellpadding='0' cellspacing='0' border='0' "
            "style='width:100%;max-width:560px;border-collapse:separate;border-spacing:0;"
            "background-color:#ffffff;border:1px solid #dce3eb;border-radius:10px;overflow:hidden'>"
            "<tr><td style='padding:20px 22px;background-color:#17212f;"
            "border-bottom:3px solid #4cc9f0'>"
            f"<div style='margin:0;color:#ffffff;font-family:Arial,Microsoft YaHei,sans-serif;"
            f"font-size:20px;font-weight:700;line-height:28px'>{html.escape(title)}</div>"
            f"<div style='margin-top:5px;color:#b9c6d5;font-family:Arial,Microsoft YaHei,sans-serif;"
            f"font-size:12px;line-height:18px'>{html.escape(subtitle)}</div>"
            "</td></tr><tr><td style='padding:12px;background-color:#ffffff'>"
            "<table role='presentation' width='100%' cellpadding='0' cellspacing='0' border='0' "
            "style='width:100%;border-collapse:collapse'>"
            f"{''.join(rows_html)}</table></td></tr>"
            "<tr><td style='padding:12px 18px;background-color:#f7f9fb;color:#7b8796;"
            "font-family:Arial,Microsoft YaHei,sans-serif;font-size:11px;line-height:16px;"
            "border-top:1px solid #e7ebf0'>WoG 宝玉助手 · 本地事件通知</td></tr>"
            "</table></td></tr></table></body></html>")

    @staticmethod
    def _mail_tier_color(tier):
        try:
            tier = int(tier)
        except (TypeError, ValueError):
            return "#17212f"
        # 邮件使用浅色背景，沿用统计页的灰、绿、蓝、红、紫、金映射并提高对比度。
        colors = ("#66717e", "#16803c", "#2463b4", "#c62828", "#7e42b5", "#8a6800")
        return colors[tier - 1] if 1 <= tier <= 6 else "#17212f"

    def _fusion_mail_html(self, event):
        category = STATS_CATEGORY_LABELS.get(event.get("category"), event.get("category", "—"))
        success = self._fusion_success(event)
        input_value = self._input_value(event)
        output_value = self._event_value(event)
        verdict = self._fusion_verdict(event)
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S",
                                  time.localtime(float(event.get("timestamp", 0) or 0)))
        tier = int(event.get("tier", 0) or 0)
        target_tier = int(event.get("target_tier", 0) or 0)
        status_color = OKC if success else RED
        verdict_color = {"得吃": OKC, "亏了": "#c47b00", "炸炉": RED}.get(
            verdict, "#17212f")
        rows = [
            ("品级", f"T{tier}", self._mail_tier_color(tier)),
            ("目标级", f"T{target_tier}", self._mail_tier_color(target_tier)),
            ("类型", category),
            ("合成结果", event.get("name", "—"),
             self._mail_tier_color(target_tier if success else tier)),
            ("状态", "成功" if success else "失败", status_color),
            ("原料价值", self._money(input_value)),
            ("产出价值", self._money(output_value)),
            ("此炉", verdict, verdict_color),
            ("时间", timestamp),
        ]
        return self._mail_base_html("自动合成通知", "材料消耗与市场估值", rows)

    def _drop_mail_html(self, events):
        rows = []
        for event in events:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S",
                                  time.localtime(float(event.get("timestamp", 0) or 0)))
            rows.extend([
                ("材料", event.get("name", "—")),
                ("数量", f"×{max(1, int(event.get('quantity', 1) or 1))}"),
                ("价值", self._money(self._event_value(event))),
                ("时间", stamp),
            ])
        return self._mail_base_html("材料掉落通知", "Gold Bar / Diamond Pouch", rows)

    def _send_mail(self, sender, recipient, auth_code, subject, html_body):
        if not (sender and recipient and auth_code):
            self.logq.put("邮件推送未发送：请填写发送 QQ 邮箱、收件邮箱和授权码")
            return
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = sender
        message["To"] = recipient
        message.set_content("此邮件包含 HTML 格式的 WoG 助手事件卡片。")
        message.add_alternative(html_body, subtype="html")
        try:
            with smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=20) as smtp:
                smtp.login(sender, auth_code)
                smtp.send_message(message)
            self.logq.put(f"邮件推送成功：{subject}")
        except Exception as exc:
            # 不记录 SMTP 原始异常，避免服务器响应或认证细节进入日志。
            self.logq.put(f"邮件推送失败：{subject}（请检查网络和 QQ 邮箱授权码）")

    def _mail_worker(self):
        while True:
            sender, recipient, auth_code, subject, body = self._mail_send_queue.get()
            try:
                self._send_mail(sender, recipient, auth_code, subject, body)
            finally:
                self._mail_send_queue.task_done()
                time.sleep(1.0)

    def _economic_filter_changed(self):
        self._economic_fusion_page = 0
        self._economic_drop_page = 0
        self._refresh_economic_view()

    def _build_econ_pager(self, parent, kind):
        frame = tk.Frame(parent, bg=CARD)
        frame.pack(fill="x", pady=(5, 7))
        label = tk.Label(frame, text="第 1/1 页", bg=CARD, fg=DIM, font=(FONT, 8))
        label.pack(side="right", padx=(5, 8))
        tk.Button(frame, text="下一页", command=lambda: self._economic_turn_page(1, kind),
                  bg=CARD2, fg=FG, relief="flat", highlightthickness=0,
                  font=(FONT, 8), padx=6, pady=2).pack(side="right", padx=2)
        tk.Button(frame, text="上一页", command=lambda: self._economic_turn_page(-1, kind),
                  bg=CARD2, fg=FG, relief="flat", highlightthickness=0,
                  font=(FONT, 8), padx=6, pady=2).pack(side="right", padx=2)
        setattr(self, f"econ_{kind}_page_label", label)

    def _build_economic_summary(self, parent, title, accent):
        frame = tk.Frame(parent, bg=LINE)
        inner = tk.Frame(frame, bg=CARD)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Label(inner, text=title, bg=CARD, fg=FG, font=(FONT, 9, "bold"),
                 anchor="w").pack(fill="x", padx=10, pady=(8, 0))
        value = tk.Label(inner, text="¥ 0.00", bg=CARD, fg=FG,
                         font=(FONT, 15, "bold"), anchor="w")
        value.pack(fill="x", padx=10, pady=(4, 0))
        delta = tk.Label(inner, text="较昨日 —", bg=CARD, fg=FAINT,
                         font=(FONT, 8), anchor="w")
        delta.pack(fill="x", padx=10)
        meta = tk.Label(inner, text="估值同步中", bg=CARD, fg=DIM,
                        font=(FONT, 8), anchor="w")
        meta.pack(fill="x", padx=10, pady=(5, 9))
        return {"frame": frame, "value": value, "delta": delta, "meta": meta,
                "accent": accent}

    @staticmethod
    def _econ_style_tree():
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Economic.Treeview", background=CARD2, fieldbackground=CARD2,
                        foreground=FG, rowheight=25, borderwidth=0,
                        font=(FONT, 8))
        style.configure("Economic.Treeview.Heading", background=CARD3, foreground=FG,
                        relief="flat", font=(FONT, 8, "bold"))
        style.map("Economic.Treeview", background=[("selected", BLUE)],
                  foreground=[("selected", INK)])

    @staticmethod
    def _build_economic_tree(parent, columns):
        tree = EconomicGrid(parent, columns, height=5)
        tree.pack(fill="both", expand=True)
        for tier, color in enumerate(TIER_COLORS, 1):
            tree.tag_configure(f"tier{tier}", foreground=color)
        tree.tag_configure("success", foreground=OKC)
        tree.tag_configure("failure", foreground=RED)
        tree.tag_configure("warning", foreground=GOLD)
        return tree

    @staticmethod
    def _tier_tag(tier):
        try:
            tier = int(tier)
        except (TypeError, ValueError):
            tier = 0
        return f"tier{tier}" if 1 <= tier <= 6 else ""

    def _economic_turn_page(self, step, kind):
        attr = "_economic_fusion_page" if kind == "fusion" else "_economic_drop_page"
        setattr(self, attr, max(0, getattr(self, attr, 0) + step))
        self._refresh_economic_view()

    @staticmethod
    def _money(value):
        return "-" if value is None else f"¥ {float(value):,.2f}"

    def _market_price(self, item):
        if int(item.get("tier") or 0) < 3:
            return None
        # 过期报价继续显示用于估值；缓存的正常新鲜度判断仍会触发后台同步。
        row = self.market_cache.get(item, allow_stale=True)
        return float(row["price"]) if row and row.get("price") is not None else None

    def _fusion_input_names(self, category, tier):
        need = 3 if category == "accessory" else 6
        current = {str(item.get("itemId")): item for item in self.economic_snapshot
                   if item.get("category") == category
                   and int(item.get("tier", 0) or 0) == tier
                   and not item.get("embedded") and not item.get("equipped")}
        previous = getattr(self, "_economic_previous", None) or {}

        # The polling snapshot may already be post-fusion when its result log is
        # delivered. Prefer items that disappeared or had their stack reduced.
        consumed = []
        for item_id, item in previous.items():
            if (item.get("category") != category
                    or int(item.get("tier", 0) or 0) != tier
                    or item.get("embedded") or item.get("equipped")
                    or int(item.get("location", 0) or 0) not in (1, 2)):
                continue
            before = max(1, int(item.get("quantity", 1) or 1))
            after_item = current.get(str(item_id))
            after = max(0, int(after_item.get("quantity", 1) or 1)) if after_item else 0
            consumed.extend([item] * min(need - len(consumed), max(0, before - after)))
            if len(consumed) >= need:
                break

        candidates = list(current.values()) + [
            item for item_id, item in previous.items()
            if str(item_id) not in current
        ]
        names = []
        for item in (consumed + candidates):
            if len(names) >= need:
                break
            names.append({"name": item.get("name", ""),
                          "market_name": item.get("market_name", ""),
                          "tier": int(item.get("tier", tier) or tier)})
        return names

    def _snapshot_item_for_event(self, name, category, tier, target_tier=None):
        wanted = str(name or "").strip().casefold()
        wanted_tier = int(target_tier or tier or 0)
        candidates = [item for item in self.economic_snapshot
                      if item.get("category") == category
                      and int(item.get("tier", 0) or 0) == wanted_tier]
        for item in candidates:
            if str(item.get("name") or "").strip().casefold() == wanted:
                return item
        return None

    def _event_item_tier(self, event):
        """成功合成估产出目标级；失败时估值留在投入级的原物品。"""
        try:
            if event.get("kind") == "fusion" and not self._fusion_success(event):
                return int(event.get("tier", 0) or 0)
            return int(event.get("target_tier", event.get("tier", 0)) or 0)
        except (AttributeError, TypeError, ValueError):
            return 0

    def _hydrate_event_market_names(self):
        events = self.economic_store.events()
        known_names = {}
        for item in self.economic_snapshot:
            market_name = str(item.get("market_name") or "").strip()
            if market_name:
                key = (str(item.get("name") or "").strip().casefold(),
                       item.get("category"), int(item.get("tier", 0) or 0))
                known_names.setdefault(key, market_name)
        for event in events:
            market_name = str(event.get("market_name") or "").strip()
            if not market_name or event.get("kind") not in ("drop", "fusion"):
                continue
            tier = self._event_item_tier(event)
            key = (str(event.get("name") or "").strip().casefold(),
                   event.get("category"), tier)
            known_names.setdefault(key, market_name)

        changed = False
        for event in events:
            if event.get("market_name") or event.get("kind") not in ("drop", "fusion"):
                continue
            item_tier = self._event_item_tier(event)
            lookup_key = (str(event.get("name") or "").strip().casefold(),
                          event.get("category"), item_tier)
            market_name = known_names.get(lookup_key, "")
            item = self._snapshot_item_for_event(
                event.get("name"), event.get("category"), event.get("tier", 0),
                item_tier)
            if not market_name and item:
                market_name = str(item.get("market_name") or "").strip()
            if market_name:
                event["market_name"] = market_name
                known_names.setdefault(lookup_key, market_name)
                changed = True
        if changed:
            self.economic_store.replace_events(events)

    def _input_value(self, event):
        names = event.get("input_items")
        if not isinstance(names, list) or not names:
            return None
        tier = int(event.get("tier", 0) or 0)
        total = 0.0
        for name in names:
            if isinstance(name, dict):
                item = dict(name)
                item.setdefault("tier", tier)
            else:
                item = {"name": name, "tier": tier,
                        "market_name": ""}
            if not item.get("market_name"):
                match = self._snapshot_item_for_event(
                    item.get("name"), event.get("category"), tier, tier)
                if match:
                    item["market_name"] = match.get("market_name", "")
            price = self._market_price(item)
            if price is None:
                return None
            total += price
        return total

    def _event_value(self, event):
        item = {"name": event.get("name", ""),
                "market_name": event.get("market_name", ""),
                "tier": self._event_item_tier(event)}
        if not item["market_name"]:
            match = self._snapshot_item_for_event(
                item["name"], event.get("category"), item["tier"], item["tier"])
            if match:
                item["market_name"] = match.get("market_name", "")
        price = self._market_price(item)
        return price * max(1, int(event.get("quantity", 1) or 1)) if price is not None else None

    def _fusion_success(self, event):
        """合成成功必须同时有成功标记，并且产出品级高于投入品级。"""
        try:
            return bool(event.get("success")) and int(event.get("target_tier", 0) or 0) > int(event.get("tier", 0) or 0)
        except (AttributeError, TypeError, ValueError):
            return False

    def _fusion_verdict(self, event):
        """失败是炸炉；成功默认得吃，成功但产出估值较低才标记亏了。"""
        if not self._fusion_success(event):
            return "炸炉"
        input_value = self._input_value(event)
        output_value = self._event_value(event)
        if input_value is not None and output_value is not None and output_value < input_value:
            return "亏了"
        return "得吃"

    def _migrate_legacy_manual_fusion_names(self):
        """修复旧版本留下的「Tn 合成失败」占位名。

        旧版手动检测会先写失败事件，随后同一监听批次才发现产物。若历史中
        存在同类、同一时间附近的掉落，就用掉落物名替换占位名；是否成功仍按
        产物品级判断，避免把普通同品级掉落强行改成成功。
        """
        try:
            events = self.economic_store.events()
        except Exception:
            return
        changed = False
        for event in events:
            if (event.get("kind") != "fusion" or event.get("success")
                    or not re.fullmatch(r"T\d+ 合成失败", str(event.get("name", "")))):
                continue
            try:
                stamp = float(event.get("timestamp", 0) or 0)
                tier = int(event.get("tier", 0) or 0)
                target = int(event.get("target_tier", tier + 1) or tier + 1)
            except (TypeError, ValueError):
                continue
            candidates = []
            for row in events:
                if row.get("kind") != "drop" or row.get("category") != event.get("category"):
                    continue
                try:
                    diff = abs(float(row.get("timestamp", 0) or 0) - stamp)
                    row_tier = int(row.get("tier", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if diff <= 2.0 and row_tier in (tier, target):
                    candidates.append((diff, row, row_tier))
            if not candidates:
                continue
            _, row, row_tier = min(candidates, key=lambda value: value[0])
            event["name"] = row.get("name") or "未出货"
            event["market_name"] = row.get("market_name", "")
            if row_tier > tier:
                event["success"] = True
            changed = True
        if changed:
            try:
                self.economic_store.replace_events(events)
            except Exception:
                pass

    def _economic_value(self, items):
        total = 0.0
        priced_quantity = 0
        eligible_quantity = 0
        for item in items:
            quantity = max(1, int(item.get("quantity", 1) or 1))
            # The market intentionally excludes T1-T2. Keep them out of the
            # pricing-progress denominator while allowing every category at T3+.
            if int(item.get("tier", 0) or 0) < 3:
                continue
            eligible_quantity += quantity
            price = self._market_price(item)
            if price is not None:
                total += price * quantity
                priced_quantity += quantity
        return total, priced_quantity, eligible_quantity

    def _economic_delta_text(self, current, previous, key):
        if not previous:
            return "较昨日 —", FAINT
        old = float(previous.get(key, 0) or 0)
        if old <= 0:
            return ("较昨日 ▲ 新增", OKC) if current > 0 else ("较昨日 —", FAINT)
        change = (current - old) / old * 100.0
        if abs(change) < 0.05:
            return "较昨日 → 0.0%", FAINT
        return f"较昨日 {'▲' if change > 0 else '▼'} {abs(change):.1f}%", OKC if change > 0 else RED

    def _today_net_income(self, events, today_key):
        """Drops add value; fusion records contribute output minus consumed inputs."""
        income = 0.0
        for event in events:
            try:
                event_day = datetime.fromtimestamp(
                    float(event.get("timestamp", 0) or 0)).strftime("%Y-%m-%d")
            except (TypeError, ValueError, OSError):
                continue
            if event_day != today_key:
                continue
            kind = event.get("kind")
            if kind == "drop":
                value = self._event_value(event)
                if value is not None:
                    income += value
            elif kind == "fusion":
                input_value = self._input_value(event)
                if input_value is None:
                    continue
                income -= input_value
                output_value = self._event_value(event)
                if output_value is not None:
                    # 炸炉记录仍可能对应一件同品级保留物品，净收益应计入它的估值。
                    income += output_value
        return income

    def _economic_event_in_time(self, event, choice):
        if choice in ("全部时间", "全部", ""):
            return True
        try:
            hours = int(str(choice).rstrip("hH"))
            return float(event.get("timestamp", 0) or 0) >= time.time() - hours * 3600
        except (TypeError, ValueError):
            return True

    def _refresh_economic_view(self):
        if not hasattr(self, "econ_fusion_tree"):
            return
        items = list(self.economic_snapshot)
        self._hydrate_event_market_names()
        personal_items = [item for item in items if item.get("equipped")]
        warehouse_items = [item for item in items if not item.get("equipped")
                           and int(item.get("location", 0)) in (1, 2)]
        personal, personal_priced, personal_eligible = self._economic_value(personal_items)
        warehouse, warehouse_priced, warehouse_eligible = self._economic_value(warehouse_items)
        today, yesterday = self.economic_store.update_daily(personal, warehouse)
        self.econ_personal["value"].configure(text=self._money(personal) if personal_priced else "¥ —")
        self.econ_warehouse["value"].configure(text=self._money(warehouse) if warehouse_priced else "¥ —")
        p_delta, p_color = self._economic_delta_text(personal, yesterday, "personal")
        w_delta, w_color = self._economic_delta_text(warehouse, yesterday, "warehouse")
        self.econ_personal["delta"].configure(text=p_delta, fg=p_color)
        self.econ_warehouse["delta"].configure(text=w_delta, fg=w_color)
        events = self.economic_store.events()
        today_key = datetime.now().strftime("%Y-%m-%d")
        income = self._today_net_income(events, today_key)
        fusions = [event for event in events if event.get("kind") == "fusion"]
        attempts = len(fusions)
        success = sum(1 for event in fusions if self._fusion_success(event))
        ratio = f"合成率 {success / attempts * 100:.0f}%" if attempts else "合成率 —"
        self.econ_personal["meta"].configure(
            text=f"T3+已定价 {personal_priced}/{personal_eligible} 件 · {ratio}")
        income_text = (f"+¥ {income:,.2f}" if income > 0 else
                       f"-¥ {abs(income):,.2f}" if income < 0 else "¥ 0.00")
        income_color = OKC if income > 0 else RED if income < 0 else FAINT
        self.econ_warehouse["meta"].configure(
            text=(f"T3+已定价 {warehouse_priced}/{warehouse_eligible} 件 · "
                  f"今日收益 {income_text}"),
            fg=income_color)

        query = self.econ_query_var.get().strip().casefold()
        kind = self.econ_kind_var.get()
        target = self.econ_target_var.get()
        status_filter = self.econ_status_var.get()
        verdict_filter = self.econ_verdict_var.get()
        time_filter = self.econ_time_var.get()

        def common_match(event):
            if kind not in ("全部", "") and not ((kind == "合成" and event.get("kind") == "fusion")
                                                   or (kind == "掉落" and event.get("kind") == "drop")):
                return False
            if query and query not in str(event.get("name", "")).casefold() \
                    and query not in str(event.get("category", "")).casefold():
                return False
            return self._economic_event_in_time(event, time_filter)

        fusion_rows = []
        drop_rows = []
        for event in events:
            if not common_match(event):
                continue
            if event.get("kind") == "fusion":
                success = self._fusion_success(event)
                verdict = self._fusion_verdict(event)
                if target != "全部" and f"T{event.get('target_tier', 0)}" != target:
                    continue
                if status_filter != "全部" and status_filter != ("成功" if success else "失败"):
                    continue
                if verdict_filter != "全部" and verdict_filter != verdict:
                    continue
                fusion_rows.append(event)
            elif event.get("kind") == "drop":
                drop_rows.append(event)
        page_size = 5
        fusion_pages = max(1, (len(fusion_rows) + page_size - 1) // page_size)
        drop_pages = max(1, (len(drop_rows) + page_size - 1) // page_size)
        self._economic_fusion_page = min(getattr(self, "_economic_fusion_page", 0), fusion_pages - 1)
        self._economic_drop_page = min(getattr(self, "_economic_drop_page", 0), drop_pages - 1)
        f_start = self._economic_fusion_page * page_size
        d_start = self._economic_drop_page * page_size
        selected_fusions = fusion_rows[f_start:f_start + page_size]
        selected_drops = drop_rows[d_start:d_start + page_size]
        self.econ_total_label.configure(text=f"合成 {len(fusion_rows)} 条 · 掉落 {len(drop_rows)} 条")
        self.econ_fusion_page_label.configure(text=f"第 {self._economic_fusion_page + 1}/{fusion_pages} 页")
        self.econ_drop_page_label.configure(text=f"第 {self._economic_drop_page + 1}/{drop_pages} 页")
        for tree in (self.econ_fusion_tree, self.econ_drop_tree):
            for child in tree.get_children():
                tree.delete(child)
        for event in selected_fusions:
            stamp = time.strftime("%m/%d %H:%M", time.localtime(float(event.get("timestamp", 0) or 0)))
            input_value = self._input_value(event)
            success = self._fusion_success(event)
            output_value = self._event_value(event)
            verdict = self._fusion_verdict(event)
            status = "成功" if success else "失败"
            self.econ_fusion_tree.insert("", "end", values=(
                f"T{event.get('tier', 0)}", f"T{event.get('target_tier', 0)}",
                STATS_CATEGORY_LABELS.get(event.get("category"), event.get("category", "")),
                event.get("name", "—"), status, self._money(input_value), self._money(output_value),
                verdict, stamp), cell_tags=(
                    self._tier_tag(event.get("tier")),
                    self._tier_tag(event.get("target_tier")),
                    "default",
                    self._tier_tag(event.get("target_tier")) if success
                    else self._tier_tag(event.get("tier")),
                    "success" if success else "failure", "default", "default",
                    "success" if verdict == "\u5f97\u5403" else "failure" if verdict == "\u70b8\u7089" else "warning", "default"))
        for event in selected_drops:
            stamp = time.strftime("%m/%d %H:%M", time.localtime(float(event.get("timestamp", 0) or 0)))
            value = self._event_value(event)
            level = int(event.get("level", 0) or 0)
            # 宝玉没有装备等级，按需求统一显示 1 级。
            if event.get("category") == "jewel" and level <= 0:
                level = 1
            self.econ_drop_tree.insert("", "end", values=(
                event.get("name", "—"), f"T{event.get('tier', 0)}",
                f"Lv.{level}" if level else "—", f"×{event.get('quantity', 1)}",
                self._money(value), stamp), cell_tags=(
                    "default", self._tier_tag(event.get("tier")), "default", "default",
                    "default", "default"))

    def _request_market_refresh(self, manual=False):
        if self._market_refreshing or not self.economic_snapshot:
            return
        # 请求冷却在缓存中持久化，避免重启或重复点击绕过限流保护。
        if not self.market_cache.refresh_allowed():
            if manual:
                wait = max(1, int(self.market_cache.refresh_wait_seconds() + 0.999))
                self.econ_market_status.configure(
                    text=f"价格请求冷却中 · {wait} 秒后可同步", fg=GOLD)
            else:
                self._schedule_market_refresh_retry()
            return
        if not manual and time.time() < getattr(self, "_market_retry_after", 0):
            return
        items = self._market_items_for_refresh(manual)
        if not items:
            if manual:
                self.econ_market_status.configure(text="价格缓存已是最新", fg=OKC)
            return
        self._market_refreshing = True
        self.econ_market_status.configure(text="Steam 市场同步中…", fg=GOLD)
        # 先用本地已有报价重绘；网络请求只负责补缺失或过期的价格。
        self._refresh_economic_view()

        def work():
            try:
                # 手动同步只携带缺价/过期项目，因此可对负缓存强制重试，
                # 但不会再次请求那些已有有效报价的物品。
                changed = self.market_cache.refresh(items, force=manual)
                self._call_soon(lambda: self._market_refresh_done(changed))
            except Exception as exc:
                error_text = (type(exc).__name__ + ": " + str(exc))[:200]
                self.market_cache.last_refresh_warning = "同步异常：" + error_text[:80]
                self._call_soon(lambda error_text=error_text:
                                self._market_refresh_done(False, error_text))
        threading.Thread(target=work, daemon=True).start()

    def _schedule_market_refresh_retry(self):
        if getattr(self, "_market_retry_scheduled", False):
            return
        self._market_retry_scheduled = True
        delay = max(1.0, self.market_cache.refresh_wait_seconds())

        def retry():
            self._market_retry_scheduled = False
            self._request_market_refresh()

        self.root.after(int(delay * 1000), retry)

    def _market_items_for_refresh(self, manual=False):
        """Include current inventory and recent fusion materials so consumed inputs get priced."""
        self._hydrate_event_market_names()
        candidates = {}

        def add(item):
            if not isinstance(item, dict) or int(item.get("tier") or 0) < 3:
                return
            key = self.market_cache.key_for(item)
            if key:
                candidates[key] = item

        for item in self.economic_snapshot:
            add(item)
        cutoff = time.time() - 72 * 3600
        for event in self.economic_store.events("fusion"):
            if float(event.get("timestamp", 0) or 0) < cutoff:
                break
            event_tier = self._event_item_tier(event)
            event_item = {"name": event.get("name", ""),
                          "market_name": event.get("market_name", ""),
                          "tier": event_tier}
            if not event_item["market_name"] and hasattr(self, "_snapshot_item_for_event"):
                match = self._snapshot_item_for_event(
                    event_item["name"], event.get("category"), event_tier, event_tier)
                if match:
                    event_item["market_name"] = match.get("market_name", "")
            # 失败记录代表投入品级的物品仍在，因此也要同步该级价格。
            add(event_item)
            for material in event.get("input_items") or []:
                if isinstance(material, dict):
                    add(material)
                else:
                    add({"name": material, "tier": event.get("tier", 0)})

        pending = []
        for item in candidates.values():
            cached = self.market_cache.get(item)
            if cached is None or (manual and cached.get("price") is None):
                pending.append(item)
        return pending

    def _market_refresh_done(self, changed, error=None):
        self._market_refreshing = False
        self._market_retry_after = time.time() + (60 if error else 0)
        warning = getattr(self.market_cache, "last_refresh_warning", "")
        self.econ_market_status.configure(
            text=(warning or ("Steam 市场暂不可用" if error else
                  "价格缓存已更新" if changed else "价格缓存已是最新")),
            fg=(OKC if changed else GOLD if warning or error else FAINT))
        if error:
            self.logq.put("价格同步失败：" + str(error)[:160])
        elif warning:
            self.logq.put("价格同步提示：" + warning)
        self._refresh_economic_view()

    # ------------------------------------------------------------------
    # 页签三：统计
    # ------------------------------------------------------------------
    def _build_stats_tab(self, body):
        head = tk.Frame(body, bg=BG)
        head.pack(fill="x", pady=(0, 8))
        tk.Label(head, text="掉落与合成统计", bg=BG, fg=FG,
                 font=(FONT, 13, "bold")).pack(side="left")
        tk.Label(head, text="本地记录 · 分钟趋势", bg=BG, fg=FAINT,
                 font=(FONT, 8)).pack(side="left", padx=(10, 0), pady=(4, 0))
        tk.Button(head, text="重置统计", command=self._reset_stats,
                  bg=CARD2, fg=RED, activebackground=CARD3, activeforeground=RED,
                  relief="flat", highlightthickness=0, font=(FONT, 8),
                  padx=8, pady=3).pack(side="right")

        filters = tk.Frame(body, bg=CARD)
        filters.pack(fill="x", pady=(0, 10))
        inner = tk.Frame(filters, bg=CARD)
        inner.pack(fill="x", padx=12, pady=10)
        tk.Label(inner, text="关卡", bg=CARD, fg=FAINT, font=(FONT, 8)).pack(side="left")
        self.stats_stage_var = tk.StringVar(value="当前关卡")
        self.stats_stage_menu = tk.OptionMenu(inner, self.stats_stage_var, "当前关卡",
                                              command=lambda *_: self._refresh_stats_view())
        self._style_stats_menu(self.stats_stage_menu)
        self.stats_stage_menu.pack(side="left", padx=(5, 10))
        tk.Label(inner, text="日期", bg=CARD, fg=FAINT, font=(FONT, 8)).pack(side="left")
        self.stats_date_var = tk.StringVar(value="全部日期")
        self.stats_date_menu = tk.OptionMenu(inner, self.stats_date_var, "全部日期",
                                             command=lambda *_: self._refresh_stats_view())
        self._style_stats_menu(self.stats_date_menu)
        self.stats_date_menu.pack(side="left", padx=(5, 10))
        tk.Label(inner, text="时间", bg=CARD, fg=FAINT, font=(FONT, 8)).pack(side="left")
        self.stats_hours_var = tk.StringVar(value="全部时间")
        self.stats_hours_menu = tk.OptionMenu(
            inner, self.stats_hours_var, "全部时间", "1h", "3h", "6h", "12h", "24h", "72h",
            command=lambda *_: self._refresh_stats_view())
        self._style_stats_menu(self.stats_hours_menu)
        self.stats_hours_menu.pack(side="left", padx=(5, 0))

        self.stats_drop_card = self._build_stats_section(body, "掉落", GOLD, "drop")
        self.stats_fusion_card = self._build_stats_section(body, "合成", VIOLET, "fusion")
        self._refresh_stats_choices()
        self._refresh_stats_view()

    def _reset_stats(self):
        if not messagebox.askyesno(
                "确认重置统计",
                "将清空所有掉落、合成、趋势和日期记录。\n日志、配置和游戏数据不会改变。\n\n确定继续吗？"):
            return
        self.stats_store.reset()
        self._reset_stats_tracking()
        self._stats_pending_fusion_outputs = {
            "jewel": [0] * 6, "gear": [0] * 6, "accessory": [0] * 6}
        self._stats_pending_fusion_returns = {category: [0] * 6
                                             for category in STATS_CATEGORIES}
        self._stats_pending_drop_deltas = {
            category: [[] for _ in range(6)] for category in STATS_CATEGORIES}
        self._stats_pending_verified_drops = []
        self._refresh_stats_choices()
        self._refresh_stats_view()

    def _reset_stats_tracking(self, category=None):
        """Discard count-probe baselines without creating a drop event.

        The count probes are eligible-material counters used by the合成页. They
        can jump when the game finishes loading or when ``含仓库`` changes, so
        they must never be treated as an item acquisition source.
        """
        previous_map = getattr(self, "_stats_previous", None)
        if previous_map is None:
            previous_map = self._stats_previous = {
                name: None for name in STATS_CATEGORIES}
        tracking_ready = getattr(self, "_stats_tracking_ready", None)
        if tracking_ready is None:
            tracking_ready = self._stats_tracking_ready = {
                name: False for name in STATS_CATEGORIES}
        baseline_signature = getattr(self, "_stats_baseline_signature", None)
        if baseline_signature is None:
            baseline_signature = self._stats_baseline_signature = {
                name: None for name in STATS_CATEGORIES}
        baseline_stable = getattr(self, "_stats_baseline_stable", None)
        if baseline_stable is None:
            baseline_stable = self._stats_baseline_stable = {
                name: 0 for name in STATS_CATEGORIES}
        categories = (category,) if category in STATS_CATEGORIES else STATS_CATEGORIES
        for name in categories:
            previous_map[name] = None
            tracking_ready[name] = False
            baseline_signature[name] = None
            baseline_stable[name] = 0
            queued = getattr(self, "_stats_pending_drop_deltas", {}).get(name)
            if queued is not None:
                for entries in queued:
                    entries.clear()
        pending = getattr(self, "_stats_pending_verified_drops", None)
        if pending is not None:
            pending[:] = [event for event in pending
                          if event.get("category") not in categories]

    @staticmethod
    def _style_stats_menu(menu):
        menu.configure(bg=CARD2, fg=FG, activebackground=BLUE,
                       activeforeground=INK, relief="flat", highlightthickness=0,
                       font=(FONT, 8), padx=5, pady=2)
        menu["menu"].configure(bg=CARD2, fg=FG, activebackground=BLUE,
                                activeforeground=INK, font=(FONT, 8))

    def _build_stats_section(self, parent, title, accent, kind):
        card = Card(parent, title, accent, "按分钟趋势 · 速率与趋势")
        card.pack(fill="x", pady=(0, 8))
        top = tk.Frame(card.body, bg=CARD)
        top.pack(fill="x")
        tk.Label(top, text="类型", bg=CARD, fg=FAINT, font=(FONT, 8)).pack(side="left")
        var = tk.StringVar(value="宝玉")
        setattr(self, f"stats_{kind}_category_var", var)
        menu = tk.OptionMenu(top, var, "宝玉", "装备", "饰品",
                             command=lambda *_: self._refresh_stats_view())
        self._style_stats_menu(menu)
        menu.pack(side="left", padx=(5, 0))
        tier_row = tk.Frame(card.body, bg=CARD)
        tier_row.pack(fill="x", pady=(10, 3))
        tiers = {}
        for tier in range(1, 7):
            tile = tk.Canvas(tier_row, width=58, height=58, bg=CARD,
                             highlightthickness=0, bd=0)
            tile.pack(side="left", padx=(0 if tier == 1 else 3, 0))
            tiers[tier] = tile
        total = tk.Canvas(tier_row, width=58, height=58, bg=CARD,
                          highlightthickness=0, bd=0)
        total.pack(side="left", padx=(3, 0))
        graph = tk.Canvas(card.body, height=116, bg=CARD, highlightthickness=0)
        graph.pack(fill="x", pady=(6, 0))
        rate = tk.Label(card.body, text=("掉落/h 0" if kind == "drop" else "合成/h 0"),
                        bg=CARD, fg=DIM, font=(FONT, 8), anchor="w")
        rate.pack(fill="x", pady=(4, 0))
        result = tk.Label(card.body, text="", bg=CARD, fg=DIM, font=(FONT, 8), anchor="w")
        result.pack(fill="x", pady=(3, 0))
        obj = {"card": card, "tiers": tiers, "total": total, "graph": graph,
               "rate": rate, "result": result,
               "kind": kind, "accent": accent}
        setattr(self, f"stats_{kind}_widgets", obj)
        return obj

    def _refresh_stats_choices(self):
        stages, dates = self.stats_store.choices()
        current = self.current_stage or "当前关卡"
        self._replace_menu(self.stats_stage_menu, self.stats_stage_var,
                           [current, "全部关卡"] + [s for s in stages if s not in (current, "全部关卡")])
        self._replace_menu(self.stats_date_menu, self.stats_date_var,
                           ["全部日期"] + dates)

    def _replace_menu(self, widget, var, values):
        current = var.get()
        menu = widget["menu"]
        menu.delete(0, "end")
        for value in values:
            menu.add_command(label=value, command=lambda v=value: self._set_stats_filter(var, v))
        if current not in values:
            var.set(values[0])

    def _set_stats_filter(self, var, value):
        var.set(value)
        self._refresh_stats_view()

    def _stats_rows_for(self, kind):
        category = {"宝玉": "jewel", "装备": "gear", "饰品": "accessory"}.get(
            getattr(self, f"stats_{kind}_category_var").get(), "jewel")
        rows = self.stats_store.rows(self.stats_stage_var.get(), self.stats_date_var.get(),
                                     self.stats_hours_var.get())
        values = [r.get("drops" if kind == "drop" else "fusions", {}).get(category,
                 [0] * 6) for r in rows]
        return category, rows, values

    def _stats_failure_rows_for(self):
        category = {"宝玉": "jewel", "装备": "gear", "饰品": "accessory"}.get(
            self.stats_fusion_category_var.get(), "jewel")
        rows = self.stats_store.rows(self.stats_stage_var.get(), self.stats_date_var.get(),
                                     self.stats_hours_var.get())
        values = [r.get("fusion_failures", {}).get(category, [0] * 6) for r in rows]
        return category, rows, values

    @staticmethod
    def _draw_stat_tile(canvas, title, value, color):
        canvas.delete("all")
        canvas.create_rectangle(1, 1, 57, 57, outline=color, width=1, fill=CARD2)
        canvas.create_text(29, 17, text=title, fill=color, font=(FONT, 8, "bold"))
        canvas.create_text(29, 39, text=f"{value:,}", fill=FG, font=(FONT, 10, "bold"))

    def _refresh_stats_view(self):
        if not hasattr(self, "stats_drop_widgets"):
            return
        self._refresh_stats_choices()
        for kind in ("drop", "fusion"):
            category, rows, values = self._stats_rows_for(kind)
            totals = [sum(row[t] for row in values) for t in range(6)]
            widgets = getattr(self, f"stats_{kind}_widgets")
            for tier, label in widgets["tiers"].items():
                self._draw_stat_tile(label, f"T{tier}", totals[tier - 1], TIER_COLORS[tier - 1])
            self._draw_stat_tile(widgets["total"], "总", sum(totals), CYAN)
            if kind == "fusion":
                _, _, failure_values = self._stats_failure_rows_for()
                failures = [sum(row[t] for row in failure_values) for t in range(6)]
                widgets["result"].configure(
                    text=f"成功 {sum(totals):,}　·　未出货 {sum(failures):,}　·　合计尝试 {sum(totals) + sum(failures):,}",
                    fg=OKC if sum(failures) == 0 else GOLD)
            else:
                failure_values = []
                failures = []
                widgets["result"].configure(text="库存新增快照 · 仅统计可确认的增长", fg=DIM)
            self._draw_stats_graph(widgets["graph"], values, widgets["accent"],
                                   failure_values, rows)
            rate_hours = self._stats_rate_hours(rows)
            per_hour = sum(totals) / rate_hours
            label = "掉落" if kind == "drop" else "合成"
            widgets["rate"].configure(text=f"{label}/h {per_hour:.1f}")

    def _stats_rate_hours(self, rows):
        selected = self.stats_hours_var.get()
        if selected != "全部时间":
            try:
                return max(1.0 / 60.0, float(str(selected).rstrip("hH")))
            except (TypeError, ValueError):
                pass
        if len(rows) >= 2:
            try:
                first = datetime.strptime(
                    f"{rows[0].get('date')} {int(rows[0].get('hour', 0)):02d}:"
                    f"{int(rows[0].get('minute', 0)):02d}", "%Y-%m-%d %H:%M")
                last = datetime.strptime(
                    f"{rows[-1].get('date')} {int(rows[-1].get('hour', 0)):02d}:"
                    f"{int(rows[-1].get('minute', 0)):02d}", "%Y-%m-%d %H:%M")
                return max(1.0 / 60.0,
                           (last - first).total_seconds() / 3600.0 + 1.0 / 60.0)
            except (TypeError, ValueError):
                pass
        return 1.0

    @staticmethod
    def _draw_stats_graph(canvas, values, accent, failure_values=None, rows=None):
        """绘制以中线为基准的分钟趋势：正值向上，未出货向下，零值贴中线。"""
        canvas.delete("all")
        width = max(260, canvas.winfo_width() or 430)
        height = 116
        left, right = 12, width - 12
        top, bottom = 9, 91
        baseline = (top + bottom) / 2
        canvas.create_line(left, baseline, right, baseline, fill=FAINT, dash=(3, 3))
        canvas.create_line(left, top, right, top, fill=LINE)
        canvas.create_line(left, bottom, right, bottom, fill=LINE)
        canvas.create_text(left + 2, baseline - 7, text="基准", anchor="w", fill=FAINT,
                           font=(FONT, 7))
        if not values:
            canvas.create_text(width / 2, baseline, text="暂无该时间段数据", fill=FAINT,
                               font=(FONT, 8))
            return
        failures = failure_values or []
        series = []
        for i, value in enumerate(values):
            success = sum(value)
            failed = sum(failures[i]) if i < len(failures) else 0
            series.append(success - failed)
        scale = max(1, max(abs(v) for v in series))
        points = []
        for i, value in enumerate(series):
            x = left + (right - left) * (i / max(1, len(series) - 1))
            y = baseline - value / scale * ((bottom - top) / 2 - 4)
            points.extend((x, y))
        if len(points) >= 4:
            canvas.create_line(*points, fill=accent, width=2, smooth=True)
            # 未出货只用一个红点标记位置，不覆盖主曲线，避免失败段出现突兀折线。
            for i, failed_row in enumerate(failures):
                if sum(failed_row) > 0 and i * 2 + 1 < len(points):
                    x, y = points[i * 2], points[i * 2 + 1]
                    canvas.create_oval(x - 3, y - 3, x + 3, y + 3,
                                       fill=RED, outline=CARD2, width=1)
        if rows:
            labels = []
            for idx in (0, len(rows) // 2, len(rows) - 1):
                if idx not in labels:
                    labels.append(idx)
            for idx in labels:
                row = rows[idx]
                stamp = f"{int(row.get('hour', 0)):02d}:{int(row.get('minute', 0)):02d}"
                x = left + (right - left) * (idx / max(1, len(rows) - 1))
                anchor = "w" if idx == 0 else "e" if idx == len(rows) - 1 else "center"
                canvas.create_text(x, height - 8, text=stamp, anchor=anchor,
                                   fill=FAINT, font=(FONT, 7))

    def _record_count_delta(self, category, counts):
        """Maintain the合成页 count baseline without recording a drop.

        These probes count items eligible for合成, rather than item-level
        acquisitions.  Storage scope, database loading, and fusion staging can
        all change the number without a drop.  The economic snapshot is the
        only source allowed to write ``drops`` statistics.
        """
        if category not in STATS_CATEGORIES or not isinstance(counts, dict):
            return
        if category == "jewel":
            current = [max(0, int(counts.get(f"tier{tier}", 0) or 0)) for tier in range(1, 7)]
        else:
            current = [max(0, int((counts.get(f"tier{tier}") or {}).get("total", 0) or 0))
                       for tier in range(1, 7)]
        tracking_ready = getattr(self, "_stats_tracking_ready", None)
        if tracking_ready is None:
            tracking_ready = self._stats_tracking_ready = {
                name: False for name in STATS_CATEGORIES}
        baseline_signature = getattr(self, "_stats_baseline_signature", None)
        if baseline_signature is None:
            baseline_signature = self._stats_baseline_signature = {
                name: None for name in STATS_CATEGORIES}
        baseline_stable = getattr(self, "_stats_baseline_stable", None)
        if baseline_stable is None:
            baseline_stable = self._stats_baseline_stable = {
                name: 0 for name in STATS_CATEGORIES}
        previous_map = getattr(self, "_stats_previous", None)
        if previous_map is None:
            previous_map = self._stats_previous = {
                name: None for name in STATS_CATEGORIES}
        tracking_ready.setdefault(category, False)
        baseline_signature.setdefault(category, None)
        baseline_stable.setdefault(category, 0)
        previous = previous_map.get(category)
        signature = tuple(current)
        if not tracking_ready.get(category, False):
            if previous is None:
                stable = 1
            elif signature == baseline_signature.get(category):
                stable = baseline_stable.get(category, 0) + 1
            else:
                stable = 1
            baseline_signature[category] = signature
            baseline_stable[category] = stable
            previous_map[category] = current
            if stable >= STATS_BASELINE_STABLE_SNAPSHOTS:
                tracking_ready[category] = True
            return
        previous_map[category] = current

    def _flush_pending_stat_drops(self, now=None, force=False):
        """Discard the retired count-probe queue.

        Kept as a compatibility shim for older in-memory App instances; new
        code never queues count deltas and therefore can never flush them into
        the drop chart.
        """
        for tiers in getattr(self, "_stats_pending_drop_deltas", {}).values():
            for entries in tiers:
                entries.clear()

    def _claim_pending_drop(self, category, tier, quantity=1):
        """Compatibility shim; fusion output reconciliation is item-level now."""
        return 0

    def _record_fusion_lines(self, category, lines, automatic=False):
        for line in lines or []:
            text = str(line)
            marker = "Tier" if "Tier" in text else ("T" if "T" in text else "")
            if not marker:
                continue
            try:
                tail = text.split(marker, 1)[1]
                digits = []
                for ch in tail:
                    if ch.isdigit():
                        digits.append(ch)
                    elif digits:
                        break
                tier = int("".join(digits)) if digits else 0
            except (TypeError, ValueError, AttributeError):
                continue
            # 只统计每次尝试的明细行；“共 X 次（出货 Y / 未出货 Z）”是汇总行，
            # 避免把汇总再次算作一次。装备/饰品的未出货明细仍算一次失败。
            is_attempt = ("→" in text or "合成出" in text) and "共 " not in text
            if 1 <= tier <= 6 and is_attempt:
                values = [0] * 6
                # 明细中的 Tier 是投入品级；成功产物是下一品级。
                output_tier = min(6, tier + 1)
                if "未出货" in text:
                    values[tier - 1] = 1
                    section = "fusion_failures"
                else:
                    values[output_tier - 1] = 1
                    section = "fusions"
                self.stats_store.add(section, category, values, stage=self.current_stage)
                if section == "fusions":
                    pending_map = getattr(self, "_stats_pending_fusion_outputs", None)
                    if pending_map is None:
                        pending_map = self._stats_pending_fusion_outputs = {
                            "jewel": [0] * 6, "gear": [0] * 6, "accessory": [0] * 6}
                    claimed_event = self._claim_pending_verified_drop(category, output_tier)
                    if claimed_event is None:
                        pending = pending_map.setdefault(category, [0] * 6)
                        pending[output_tier - 1] += 1
                else:
                    claimed_event = self._claim_pending_verified_drop(category, tier)
                    if claimed_event is None:
                        returns_map = getattr(self, "_stats_pending_fusion_returns", None)
                        if returns_map is None:
                            returns_map = self._stats_pending_fusion_returns = {
                                name: [0] * 6 for name in STATS_CATEGORIES}
                        returns_map.setdefault(category, [0] * 6)[tier - 1] += 1
                economic_store = getattr(self, "economic_store", None)
                if economic_store is not None:
                    result_name = text.split("→", 1)[1].strip() if "→" in text else "未知产出"
                    result_name = result_name.replace("(未出货)", "").strip() or "未知产出"
                    result_item = self._snapshot_item_for_event(
                        result_name, category, tier, output_tier)
                    econ_pending = getattr(self, "_economic_pending_outputs", {}).setdefault(
                        category, [0] * 6)
                    if section == "fusions":
                        econ_pending[output_tier - 1] += 1
                    event = {
                        "kind": "fusion", "timestamp": time.time(),
                        "name": result_name, "category": category,
                        "tier": tier, "target_tier": output_tier,
                        "quantity": 1, "success": section == "fusions",
                        "input_items": self._fusion_input_names(category, tier),
                        "market_name": (result_item or {}).get("market_name", ""),
                        "manual": not automatic,
                    }
                    if claimed_event is not None:
                        # The inventory increase belongs to this fusion, so it
                        # must never be emitted as a drop notification/event.
                        pass
                    economic_store.add_event(event)
                    if automatic:
                        self._notify_mail_event(event)
                self._economic_auto_fusion_until = time.time() + 8.0

    def _log_fusion_results(self, category, lines):
        lines = list(lines or [])
        for line in lines:
            self.logq.put("  " + str(line))
        if category == "gear" and any(
                "跳过" in str(line) and re.search(r"件\s*[<＜]\s*\d+", str(line))
                for line in lines):
            self.logq.put("提示：请将铁匠的设置品级调整为全部")

    def _cfg_keep_minutes(self):
        """配置里的日志保留时长（总分钟），下限 1 分钟。"""
        try:
            return max(1, int(self.cfg.get("log_keep_minutes", 60) or 60))
        except (TypeError, ValueError):
            return 60

    def _keep_minutes(self):
        """界面上那两个框（小时 + 分钟）合成总分钟；都填 0 按 1 分钟算。"""
        hours = self._int_of(self.log_keep_h_var, 0, 0, 168)
        mins = self._int_of(self.log_keep_m_var, 0, 0, 59)
        return max(1, hours * 60 + mins)

    def on_log_keep(self):
        on = bool(self.log_keep_on_var.get())
        minutes = self._keep_minutes()
        self.cfg["log_keep_minutes"] = minutes if on else 0
        save_config(self.cfg)
        self._log("日志保留：" + (f"最近 {_fmt_span(minutes)}" if on else "不自动清理"))
        self._prune_log()          # 立刻生效，不用等下一次轮询

    def _help_block(self, parent, lines):
        """说明页签里的一段文字；空串当一条间距用。"""
        for line in lines:
            if not line:
                tk.Frame(parent, bg=CARD, height=5).pack(fill="x")
                continue
            tk.Label(parent, text=line, bg=CARD,
                     fg=(FG if line[:1] in ("·", "1", "2", "3") else DIM),
                     font=(FONT, 9), anchor="w", justify="left",
                     wraplength=430).pack(fill="x", anchor="w")

    def _clear_log(self):
        self._log_rows.clear()
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
    # ------------------------------------------------------------------
    # 小部件工厂
    # ------------------------------------------------------------------
    def _spacer(self, parent, width=14):
        tk.Frame(parent, bg=parent.cget("bg"), width=width).pack(side="left")

    def _toggle_row(self, parent, text, var, command, color, tip=None):
        """一行「开关 + 文字」，tip 是鼠标悬浮的说明。"""
        holder = tk.Frame(parent, bg=parent.cget("bg"))
        holder.pack(side="left")
        tg = Toggle(holder, var, command=command, on_color=color,
                    bg=parent.cget("bg"))
        tg.pack(side="left")
        lb = tk.Label(holder, text=text, bg=parent.cget("bg"), fg=FG,
                      font=(FONT, 9), cursor="hand2")
        lb.pack(side="left", padx=(6, 0))
        # 点文字等于点开关，省得非要去点那个小圆点
        lb.bind("<Button-1>", lambda _e: tg._click())
        if tip:
            Tooltip([holder, tg, lb], tip)
            lb.configure(fg=FG)
        self.toggles.append(tg)
        return tg

    def _spin(self, parent, var, lo, hi, width=3, command=None):
        sp = tk.Spinbox(parent, from_=lo, to=hi, width=width, textvariable=var,
                        bg=CARD2, fg=FG, buttonbackground=CARD3, relief="flat",
                        font=(FONT, 9), justify="center", insertbackground=FG,
                        disabledbackground=CARD2, disabledforeground=FAINT,
                        command=command, bd=0, highlightthickness=1,
                        highlightbackground=LINE, highlightcolor=BLUE)
        # Spinbox 的 command 只在点箭头时触发；手打数字要另外存
        if command:
            sp.bind("<FocusOut>", lambda _e: command())
            sp.bind("<Return>", lambda _e: command())
        return sp

    def _int_of(self, var, default, lo=1, hi=100000):
        """安全取整：输入框里是空/乱码时不能抛异常（否则状态同步整条中断）。"""
        try:
            return max(lo, min(hi, int(var.get())))
        except Exception:
            return default
    def _interval_minutes(self):
        """自动整理间隔：面板上填「小时 + 分钟」，存盘统一用总分钟。

        两个都填 0 时按 1 分钟算 —— 间隔 0 会退化成每轮监听都整理（每 2 秒搬一次
        仓库），不是想要的行为。
        """
        hours = self._int_of(self.org_h_var, 0, 0, 99)
        mins = self._int_of(self.org_m_var, 0, 0, 59)
        return max(1, hours * 60 + mins)


    def _button(self, parent, text, cmd, bg, fg, side=None, expand=False):
        b = tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg,
                      activebackground=bg, activeforeground=fg, relief="flat",
                      font=(FONT, 9, "bold"), pady=5, cursor="hand2",
                      bd=0, highlightthickness=0)
        if side:
            b.pack(side=side, fill="x" if expand else None, expand=expand, padx=3)
        else:
            b.pack(fill="x", pady=(10, 0))
        self.buttons.append(b)
        return b

    # ------------------------------------------------------------------
    # 状态同步（主线程）
    # ------------------------------------------------------------------
    def _push_state(self):
        st = self.ui_state
        st["auto_jewel"] = self.auto_jewel_var.get()
        st["auto_gear"] = self.auto_gear_var.get()
        st["auto_acc"] = self.auto_acc_var.get()
        st["auto_dep"] = self.auto_dep_var.get()
        st["auto_org"] = self.auto_org_var.get()
        st["jewel_tiers"] = [t for t in range(1, 7) if self.jewel_tier_vars[t].get()]
        st["gear_tiers"] = [t for t in range(1, 7) if self.gear_tier_vars[t].get()]
        st["acc_tiers"] = [t for t in range(1, 7) if self.acc_tier_vars[t].get()]
        st["dep_types"] = [k for k, v in self.dep_type_vars.items() if v.get()]
        st["dep_tiers"] = [t for t in range(1, 7) if self.dep_tier_vars[t].get()]
        st["jewel_storage"] = self.jewel_storage_var.get()
        st["gear_storage"] = self.gear_storage_var.get()
        st["acc_storage"] = self.acc_storage_var.get()
        st["gear_same_level"] = self.gear_same_var.get()
        st["acc_same_level"] = self.acc_same_var.get()
        st["exclude_locked"] = self.lock_var.get()
        st["stop_when_full"] = self.stop_full_var.get()
        st["auto_resume"] = self.resume_var.get()
        st["arrange_pages"] = self.arrange_pages_var.get()
        st["org_locked"] = self.org_locked_var.get()
        st["org_sort"] = self.org_sort_var.get()
        st["org_every_min"] = self._interval_minutes()
        st["pages_auto"] = self.pages_auto_var.get()
        st["pages_manual"] = self._int_of(self.pages_var, 3, 1, 20)
        st["detected_pages"] = self.detected_pages
        st["page_cats"] = {str(p): v.get() and LABEL_TO_CAT.get(v.get(), "any")
                           for p, v in self.page_vars.items()}

    def _sync_ui_state(self):
        try:
            self._push_state()
            for t in self.toggles:
                t.sync()
        except Exception:
            pass
        self.root.after(1000, self._sync_ui_state)

    # ------------------------------------------------------------------
    # 日志 / 状态栏
    # ------------------------------------------------------------------
    def _poll_log(self):
        try:
            while True:
                self._append_log(self.logq.get_nowait())
        except queue.Empty:
            pass
        except Exception:
            pass
        finally:
            try:
                self._prune_log()
            except Exception:
                pass
            self.root.after(300, self._poll_log)

    def _append_log(self, raw):
        """所有日志都在这里统一加时间戳，方便按时间清理。"""
        ts = time.time()
        text = f"{time.strftime('%H:%M:%S', time.localtime(ts))}  {raw}"
        tag = self._log_tag(raw)
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", tag)
        self.log.configure(state="disabled")
        self.log.see("end")
        self._log_rows.append((ts, text))
        self.status_label.configure(text=str(raw).strip()[:90], fg=LOG_WARNING if tag else DIM)

    @staticmethod
    def _log_tag(raw):
        """Return a visual tag for actionable user guidance in the log."""
        text = str(raw or "")
        if ("请先打开助手再启动游戏并等待1分钟" in text
                or "请将铁匠的设置品级调整为全部" in text):
            return "warning"
        if ("跳过" in text and re.search(r"件\s*[<＜]\s*\d+", text)):
            return "warning"
        return ""

    def _prune_log(self):
        """按时间丢弃旧日志；另外留一个行数硬上限，防止关掉清理后无限涨。"""
        drop = 0
        keep = self.cfg.get("log_keep_minutes", 60) or 0
        if keep > 0:
            cutoff = time.time() - keep * 60
            while self._log_rows and self._log_rows[0][0] < cutoff:
                self._log_rows.popleft()
                drop += 1
        hard = 2000
        while len(self._log_rows) > hard:
            self._log_rows.popleft()
            drop += 1
        if drop:
            self.log.configure(state="normal")
            self.log.delete("1.0", f"{drop + 1}.0")
            self.log.configure(state="disabled")

    def _log(self, msg):
        self.logq.put(msg)

    # ------------------------------------------------------------------
    # 界面更新
    # ------------------------------------------------------------------
    def _poll_updates(self):
        try:
            while True:
                try:
                    kind, data = self.uiq.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle_update(kind, data)
                except Exception as e:
                    self.logq.put("界面更新失败(" + str(kind) + "): " + str(e)[:120])
        except Exception:
            pass
        finally:
            self.root.after(200, self._poll_updates)

    def _handle_update(self, kind, data):
        if kind == "jewel":
            self._show_jewel_stats(data)
            self._record_count_delta("jewel", data)
            self._stats_seen = True
        elif kind == "gear":
            self._show_equip_stats("gear", data)
            self._record_count_delta("gear", data)
            self._stats_seen = True
        elif kind == "acc":
            self._show_equip_stats("acc", data)
            self._record_count_delta("accessory", data)
            self._stats_seen = True
        elif kind == "storage":
            self._show_storage(data)
        elif kind == "economic":
            self._update_economic_snapshot(data)
        elif kind == "stage":
            value = str(data or "").strip()
            if value and value not in ("当前关卡", "null", "None"):
                self.current_stage = value
                if getattr(self, "stats_stage_var", None) is not None:
                    self._refresh_stats_choices()
                    self._refresh_stats_view()
        elif kind == "deposit_full":
            self._on_deposit_full(data)
        elif kind == "deposit_resume":
            self._on_deposit_resume()
        elif kind == "call":
            data()

    def _update_economic_snapshot(self, snapshot):
        if not isinstance(snapshot, list):
            return
        normalized = []
        seen_ids = set()
        for item in snapshot:
            if not isinstance(item, dict) or not item.get("itemId"):
                continue
            item = dict(item)
            item_id = str(item["itemId"])
            if item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            item["quantity"] = max(1, int(item.get("quantity", 1) or 1))
            normalized.append(item)
        current = {str(item["itemId"]): item for item in normalized}
        previous = self._economic_previous
        signature = self._economic_inventory_signature(current)
        if not getattr(self, "_economic_tracking_ready", False):
            if previous is None:
                self._economic_baseline_signature = signature
                self._economic_baseline_stable = 1
            elif signature == getattr(self, "_economic_baseline_signature", None):
                self._economic_baseline_stable = getattr(
                    self, "_economic_baseline_stable", 1) + 1
            else:
                self._economic_baseline_signature = signature
                self._economic_baseline_stable = 1
            self._economic_previous = current
            if self._economic_baseline_stable >= ECONOMIC_BASELINE_STABLE_SNAPSHOTS:
                self._economic_tracking_ready = True
            self.economic_snapshot = normalized
            self._refresh_economic_view()
            self._request_market_refresh()
            return
        removed_by_group = {}
        removed_items = {}
        new_drop_events = []
        new_pending_failures = []
        now = time.time()
        # 只把可用于合成的背包/仓库物品纳入差分。除了 itemId 消失，堆叠物品
        # 的数量减少也必须视为原料消耗；否则宝玉手动合成不会被记录。
        manual_detection_enabled = (
            previous is not None
            and time.time() >= getattr(self, "_economic_auto_fusion_until", 0)
        )

        def collect_removed(item, count):
            if not manual_detection_enabled or not isinstance(item, dict):
                return
            if item.get("embedded") or item.get("equipped"):
                return
            if int(item.get("location", 0) or 0) not in (1, 2):
                return
            category = item.get("category")
            if category not in STATS_CATEGORIES:
                return
            tier = max(0, min(6, int(item.get("tier", 0) or 0)))
            count = max(0, int(count or 0))
            if not tier or not count:
                return
            key = (category, tier)
            removed_by_group[key] = removed_by_group.get(key, 0) + count
            # 事件只需保留一个配方批次的原料明细，避免大堆叠造成无意义的内存增长。
            bucket = removed_items.setdefault(key, [])
            for _ in range(min(count, 6 * 20 - len(bucket))):
                copy = dict(item)
                copy["quantity"] = 1
                bucket.append(copy)

        if manual_detection_enabled:
            for item_id, old in previous.items():
                old_quantity = max(1, int(old.get("quantity", 1) or 1))
                current_item = current.get(item_id)
                if current_item is None:
                    collect_removed(old, old_quantity)
                    continue
                current_quantity = max(1, int(current_item.get("quantity", 1) or 1))
                if old_quantity > current_quantity:
                    collect_removed(old, old_quantity - current_quantity)
        if previous is not None:
            for item_id, item in current.items():
                old = previous.get(item_id)
                old_quantity = int(old.get("quantity", 0) or 0) if old else 0
                added = max(0, int(item.get("quantity", 1) or 1) - old_quantity)
                if not added or item.get("embedded") or item.get("equipped"):
                    continue
                category = item.get("category") if item.get("category") in STATS_CATEGORIES else "other"
                tier = max(0, min(6, int(item.get("tier", 0) or 0)))
                need = 3 if category == "accessory" else 6
                input_tier = tier - 1
                input_key = (category, input_tier)
                while input_tier >= 1 and added and removed_by_group.get(input_key, 0) >= need:
                    inputs = removed_items.get(input_key, [])[:need]
                    if len(inputs) < need:
                        break
                    del removed_items[input_key][:need]
                    removed_by_group[input_key] -= need
                    self.economic_store.add_event({
                        "kind": "fusion", "timestamp": time.time(),
                        "name": item.get("name", "未知物品"), "market_name": item.get("market_name", ""),
                        "category": category, "tier": input_tier, "target_tier": tier,
                        "quantity": 1, "success": True,
                        "input_items": [{"name": x.get("name", ""),
                                         "market_name": x.get("market_name", ""),
                                         "tier": input_tier} for x in inputs],
                        "manual": True,
                    })
                    added -= 1
                pending = self._economic_pending_outputs.setdefault(category, [0] * 6)
                consumed = min(added, pending[tier - 1] if tier else 0)
                if tier:
                    pending[tier - 1] -= consumed
                added -= consumed
                if added:
                    new_drop_events.append((item_id, {
                        "kind": "drop", "timestamp": time.time(),
                        "name": item.get("name", "未知物品"), "category": category,
                        "tier": tier, "level": int(item.get("level", 0) or 0),
                        "quantity": added, "location": int(item.get("location", 0) or 0),
                        "market_name": item.get("market_name", ""),
                    }, item))
            # 没有产物的完整原料批次才按手动失败记录；不完整的消失批次可能是
            # 出售、丢弃或其他操作，宁可不记录也不要伪造“炸炉”。
            for (category, tier), count in list(removed_by_group.items()):
                need = 3 if category == "accessory" else 6
                if count < need or count % need:
                    continue
                while count >= need:
                    inputs = removed_items.get((category, tier), [])[:need]
                    if len(inputs) < need:
                        break
                    del removed_items[(category, tier)][:need]
                    new_pending_failures.append({
                        "created_at": now,
                        "name": f"T{tier} 合成失败",
                        "market_name": "",
                        "category": category, "tier": tier, "target_tier": min(6, tier + 1),
                        "quantity": 1, "success": False,
                        "input_items": [{"name": x.get("name", ""),
                                         "market_name": x.get("market_name", ""),
                                         "tier": tier} for x in inputs],
                        "manual": True,
                    })
                    count -= need

        # 失败批次先进入短暂待确认窗口。产物可能在下一次快照才出现，届时回填
        # 真实物品名；窗口结束仍无产物才落成失败记录，并用“未出货”作结果。
        pending = list(getattr(self, "_economic_pending_manual_failures", []))
        pending.extend(new_pending_failures)
        used_drop_indexes = set()
        unresolved = []
        for failure in pending:
            created_at = float(failure.get("created_at", now) or now)
            tier = int(failure.get("tier", 0) or 0)
            target_tier = int(failure.get("target_tier", tier + 1) or tier + 1)
            # An item can arrive one snapshot after its materials disappear.
            # Claim the still-pending item-level addition before considering a
            # real-time candidate, so it cannot later become a drop.
            pending_result = self._claim_pending_verified_drop(
                failure.get("category"), target_tier)
            if pending_result is None:
                pending_result = self._claim_pending_verified_drop(
                    failure.get("category"), tier)
            if pending_result is not None:
                output_tier = int(pending_result.get("tier", 0) or 0)
                success = output_tier > tier
                event = {
                    "kind": "fusion", "timestamp": created_at,
                    "name": pending_result.get("name", "未出货"),
                    "market_name": pending_result.get("market_name", ""),
                    "category": failure.get("category"), "tier": tier,
                    "target_tier": target_tier, "quantity": 1,
                    "success": success,
                    "input_items": failure.get("input_items", []), "manual": True,
                }
                stat_values = [0] * 6
                stat_section = "fusions" if success else "fusion_failures"
                stat_values[(output_tier if success else tier) - 1] = 1
                self.stats_store.add(stat_section, failure.get("category"), stat_values,
                                     ts=created_at, stage=self.current_stage)
                self.economic_store.add_event(event)
                continue
            candidates = []
            for index, (item_id, drop_event, item) in enumerate(new_drop_events):
                if index in used_drop_indexes or item.get("category") != failure.get("category"):
                    continue
                item_tier = int(item.get("tier", 0) or 0)
                if item_tier not in (tier, target_tier):
                    continue
                age = abs(float(drop_event.get("timestamp", now) or now) - created_at)
                if age <= MANUAL_FUSION_RESULT_GRACE_SECONDS:
                    # 优先目标级产物；同级物品只能修正结果名，不能改成成功。
                    candidates.append((0 if item_tier == target_tier else 1, age,
                                       index, item_id, drop_event, item))
            if candidates:
                _, _, index, _, drop_event, item = min(candidates, key=lambda value: (value[0], value[1]))
                used_drop_indexes.add(index)
                event = {
                    "kind": "fusion", "timestamp": created_at,
                    "name": item.get("name", "未出货"),
                    "market_name": item.get("market_name", ""),
                    "category": failure.get("category"), "tier": tier,
                    "target_tier": target_tier, "quantity": 1,
                    "success": int(item.get("tier", 0) or 0) > tier,
                    "input_items": failure.get("input_items", []), "manual": True,
                }
                category = failure.get("category")
                output_tier = int(item.get("tier", 0) or 0)
                event["success"] = output_tier > tier
                stat_values = [0] * 6
                stat_section = "fusions" if event["success"] else "fusion_failures"
                stat_values[(output_tier if event["success"] else tier) - 1] = 1
                self.stats_store.add(stat_section, category, stat_values,
                                     ts=created_at, stage=self.current_stage)
                if event["success"]:
                    pending_outputs = getattr(self, "_stats_pending_fusion_outputs", {})
                    pending_outputs.setdefault(category, [0] * 6)[
                        target_tier - 1] += 1
                self.economic_store.add_event(event)
            elif now - created_at >= MANUAL_FUSION_RESULT_GRACE_SECONDS:
                event = dict(failure)
                event.pop("created_at", None)
                event["name"] = "未出货"
                stat_values = [0] * 6
                stat_values[tier - 1] = 1
                self.stats_store.add("fusion_failures", failure.get("category"),
                                     stat_values, ts=created_at, stage=self.current_stage)
                returns_map = getattr(self, "_stats_pending_fusion_returns", None)
                if returns_map is None:
                    returns_map = self._stats_pending_fusion_returns = {
                        name: [0] * 6 for name in STATS_CATEGORIES}
                returns_map.setdefault(failure.get("category"), [0] * 6)[tier - 1] += 1
                self.economic_store.add_event(event)
            else:
                unresolved.append(failure)
        for index, (_, drop_event, _) in enumerate(new_drop_events):
            if index not in used_drop_indexes:
                self._queue_verified_drop(drop_event)
        self._flush_verified_drops()
        self._economic_pending_manual_failures = unresolved
        self._economic_previous = current
        self.economic_snapshot = normalized
        self._refresh_economic_view()
        self._request_market_refresh()

    def _record_verified_drop(self, event):
        """Write only unclaimed item additions to the drop statistics.

        Fusion results are removed from ``new_drop_events`` before this method
        is called.  This makes the source boundary explicit: count probes and
        fusion logs can populate the合成 buckets, while only an item-level
        inventory increase can populate the掉落 bucket.
        """
        if not isinstance(event, dict) or event.get("kind") != "drop":
            return
        category = event.get("category")
        try:
            tier = int(event.get("tier", 0) or 0)
            quantity = max(0, int(event.get("quantity", 0) or 0))
        except (TypeError, ValueError):
            return
        if category not in STATS_CATEGORIES or not 1 <= tier <= 6 or not quantity:
            return
        store = getattr(self, "stats_store", None)
        if store is None:
            return
        values = [0] * 6
        values[tier - 1] = quantity
        timestamp = float(event.get("timestamp", time.time()) or time.time())
        minute = int(timestamp // 60) * 60
        store.add("drops", category, values, ts=minute,
                  stage=self.current_stage)

    def _queue_verified_drop(self, event):
        """Hold an item increase briefly so a late fusion result can claim it."""
        if not isinstance(event, dict) or event.get("kind") != "drop":
            return
        pending = getattr(self, "_stats_pending_verified_drops", None)
        if pending is None:
            pending = self._stats_pending_verified_drops = []
        queued = dict(event)
        queued["_pending_drop"] = True
        pending.append(queued)
        store = getattr(self, "economic_store", None)
        if store is not None and hasattr(store, "add_event"):
            store.add_event(queued)

    def _claim_pending_verified_drop(self, category, tier):
        pending = getattr(self, "_stats_pending_verified_drops", [])
        for index, event in enumerate(pending):
            if (event.get("category") == category
                    and int(event.get("tier", 0) or 0) == int(tier)):
                event = dict(event)
                quantity = max(1, int(event.get("quantity", 1) or 1))
                event["quantity"] = 1
                source = pending[index]
                if quantity > 1:
                    source["quantity"] = quantity - 1
                else:
                    pending.pop(index)
                store = getattr(self, "economic_store", None)
                if store is not None and hasattr(store, "replace_events") and hasattr(store, "events"):
                    target_ts = float(event.get("timestamp", 0) or 0)
                    rows = [row for row in store.events() if not (
                        row.get("_pending_drop")
                        and row.get("category") == category
                        and int(row.get("tier", 0) or 0) == int(tier)
                        and abs(float(row.get("timestamp", 0) or 0) - target_ts) < 0.5)]
                    if quantity > 1:
                        remainder = dict(event)
                        remainder["quantity"] = quantity - 1
                        remainder["_pending_drop"] = True
                        rows.append(remainder)
                    store.replace_events(rows)
                return event
        return None

    def _flush_verified_drops(self, now=None, force=False):
        now = time.time() if now is None else now
        pending = getattr(self, "_stats_pending_verified_drops", None)
        if not pending:
            return
        retained = []
        for event in pending:
            stamp = float(event.get("timestamp", now) or now)
            if not force and now - stamp < STATS_FUSION_RECONCILE_SECONDS:
                retained.append(event)
                continue
            store = getattr(self, "economic_store", None)
            if store is not None:
                event.pop("_pending_drop", None)
                if hasattr(store, "replace_events") and hasattr(store, "events"):
                    stamp = float(event.get("timestamp", 0) or 0)
                    store.replace_events([row for row in store.events() if not (
                        row.get("_pending_drop")
                        and row.get("category") == event.get("category")
                        and int(row.get("tier", 0) or 0) == int(event.get("tier", 0) or 0)
                        and abs(float(row.get("timestamp", 0) or 0) - stamp) < 0.5)])
                if hasattr(store, "add_event"):
                    store.add_event(event)
            self._record_verified_drop(event)
            if hasattr(self, "cfg"):
                self._notify_mail_event(event)
        self._stats_pending_verified_drops = retained

    @staticmethod
    def _economic_inventory_signature(items):
        """Only trackable inventory affects bootstrap stability; wallet noise does not."""
        rows = []
        for item_id, item in items.items():
            if (item.get("category") not in STATS_CATEGORIES
                    or item.get("embedded") or item.get("equipped")):
                continue
            rows.append((
                str(item_id), int(item.get("quantity", 1) or 1),
                int(item.get("tier", 0) or 0), int(item.get("level", 0) or 0),
                int(item.get("location", 0) or 0),
            ))
        return tuple(sorted(rows))

    def _call_soon(self, fn):
        """从任意线程请求在主线程执行 fn。"""
        self.uiq.put(("call", fn))

    def set_busy(self, busy):
        self.busy = busy
        self.busy_since = time.time() if busy else 0.0
        state = "disabled" if busy else "normal"
        for b in self.buttons:
            b.configure(state=state)

    # ------------------------------------------------------------------
    # 连接
    # ------------------------------------------------------------------
    def _start_connect(self):
        with self._connect_lock:
            if self._connecting or self._stop_event.is_set():
                return
            self._connecting = True
        threading.Thread(target=self._connect, daemon=True).start()

    @staticmethod
    def _is_transport_error(error):
        text = str(error).casefold()
        return isinstance(error, (OSError, TimeoutError, ConnectionError)) or any(
            marker in text for marker in (
                "timed out", "connection reset", "connection closed",
                "socket is closed", "socket is already closed",
                "websocket is closed", "broken pipe", "10054", "10061"))

    def _drop_connection(self, reason, expected_game=None):
        """统一处理传输层断开，并确保旧线程不会关闭新连接。"""
        with self._connect_lock:
            if expected_game is not None and self.game is not expected_game:
                return
            if self.game is None and self._connecting:
                return
        game = self.game
        self.game = None
        # 连接重建后先重新建立经济快照基线，避免把断线期间的临时空快照
        # 当成手动合成原料消耗。
        self._economic_previous = None
        self._economic_tracking_ready = False
        self._economic_baseline_signature = None
        self._economic_baseline_stable = 0
        self._reset_stats_tracking()
        self._game_ready_at = 0.0
        self._runtime_ready = False
        self._data_ready = False
        if game is not None:
            try:
                game.close()
            except Exception:
                pass
        self.logq.put("游戏连接中断，正在自动重连：" + str(reason)[:100])
        self._call_soon(lambda: self.conn_label.configure(text="○ 等待重连", fg=DIM))
        self._start_connect()

    def _connect(self):
        try:
            try:
                patch_state = port_patch.ensure_patched()
                self.logq.put("已确保 10998 端口补丁：" + ("刚刚开启" if patch_state == "patched" else "已存在"))
            except Exception as e:
                self.logq.put("端口补丁失败：" + str(e)[:180])
                self._call_soon(lambda: self.conn_label.configure(text="✕ 补丁失败", fg=RED))
                return

            self.logq.put("请打开游戏，助手正在等待游戏启动并监听 10998 端口…")
            # 初始提示已经代表 port 状态，第一次连接被拒绝时不要重复刷同一行。
            wait_state = "port"
            connection_hint_logged = False
            retry_seconds = RECONNECT_RETRY_SECONDS
            while not self._stop_event.is_set():
                game = None
                try:
                    # 就绪探测使用短请求超时，避免游戏主线程加载时把连接线程
                    # 卡住数分钟；探测仍复用这条 WebSocket，不会因此重连。
                    game = Game(timeout=5, allow_frida=False, request_timeout=5)
                    # 先登记 WebSocket 连接；脚本初始化在同一连接上异步等待。
                    # 登记后 _runtime_ready 仍为 False，监听/统计/自动操作全部暂停。
                    self.game = game
                    self._runtime_ready = False
                    self._data_ready = False
                    self._game_ready_at = float("inf")
                    self.logq.put("已连接游戏（" + game.connection_label + "），等待游戏脚本初始化…")
                    self._call_soon(lambda: self.conn_label.configure(text="● 已连接，初始化中", fg=GOLD))

                    # 连接后先完全不执行 JS，给游戏自己的 Puerts、登录数据和
                    # 铁匠页留下完整初始化时间。期间监听/统计/自动操作都被门控。
                    if self._stop_event.wait(GAME_BOOT_WAIT_SECONDS):
                        if self.game is game:
                            self.game = None
                            self._runtime_ready = False
                            self._data_ready = False
                            self._game_ready_at = 0.0
                        try:
                            game.close()
                        except Exception:
                            pass
                        return

                    # 只在等待窗口结束后低频探测一次运行时入口；不再每 0.5 秒
                    # 在游戏主线程上轮询，避免探针本身干扰铁匠页初始化。
                    ready_logged = False
                    while not self._stop_event.is_set():
                        try:
                            if game.eval(js_runtime_ready()):
                                break
                        except Exception as ready_error:
                            ws = getattr(game, "ws", None)
                            error_text = str(ready_error).casefold()
                            # Runtime.evaluate 在游戏主线程繁忙时可能短暂超时；
                            # 只有明确断开的连接才需要重新建立 WebSocket。
                            closed = ws is not None and not getattr(ws, "connected", True)
                            closed = closed or any(marker in error_text for marker in (
                                "connection is already closed", "websocket is closed",
                                "socket is closed", "connection reset", "broken pipe",
                                "10054", "10061"))
                            if closed:
                                raise ready_error
                        if not ready_logged:
                            self.logq.put("游戏脚本初始化中，等待物品和合成数据加载…")
                            ready_logged = True
                        self._stop_event.wait(RUNTIME_PROBE_INTERVAL)
                    else:
                        if self.game is game:
                            self.game = None
                            self._runtime_ready = False
                            self._data_ready = False
                            self._game_ready_at = 0.0
                        try:
                            game.close()
                        except Exception:
                            pass
                        return
                    game.request_timeout = WS_REQUEST_TIMEOUT
                    if game.ws is not None:
                        game.ws.settimeout(WS_REQUEST_TIMEOUT)
                    # 业务操作从这里才允许；再留出一个短稳定窗口，让铁匠页先完成
                    # 自己的首次绑定和重绘。
                    self._game_ready_at = time.monotonic() + GAME_SETTLE_SECONDS
                    self._runtime_ready = True
                    self.logq.put("游戏脚本已就绪，开始刷新数量和仓库信息")
                    self._call_soon(lambda: self.conn_label.configure(text="● 已连接", fg=OKC))
                    self._call_soon(lambda: self.root.after(100, self.refresh_all))
                    return
                except Exception as e:
                    if game is not None:
                        if self.game is game:
                            self.game = None
                            self._runtime_ready = False
                            self._data_ready = False
                            self._game_ready_at = 0.0
                        try:
                            game.close()
                        except Exception:
                            pass
                    raw_error = str(e)
                    if "10061" in raw_error or "无法连接" in raw_error:
                        state = "port"
                        message = "请打开游戏，助手正在等待游戏启动并监听 10998 端口…"
                    elif ("nn is not defined" in raw_error or "JS错误" in raw_error
                          or "脚本尚未完成初始化" in raw_error):
                        state = "runtime"
                        message = "游戏已启动，正在等待游戏脚本初始化…"
                    else:
                        state = "other"
                        message = "游戏连接暂未就绪，助手将继续等待…"
                    if not connection_hint_logged:
                        self.logq.put("请先打开助手再启动游戏并等待1分钟")
                        connection_hint_logged = True
                    if state != wait_state:
                        self.logq.put(message)
                        wait_state = state
                    self._call_soon(lambda: self.conn_label.configure(text="○ 等待游戏", fg=DIM))
                    if self._stop_event.wait(retry_seconds):
                        return
                    retry_seconds = min(RECONNECT_MAX_RETRY_SECONDS,
                                        max(RECONNECT_RETRY_SECONDS, retry_seconds * 2))
        finally:
            with self._connect_lock:
                self._connecting = False

    # ------------------------------------------------------------------
    # 监听线程
    # ------------------------------------------------------------------
    def _monitor(self):
        while True:
            try:
                self._monitor_cycle()
            except Exception as e:
                try:
                    if self._is_transport_error(e):
                        self._drop_connection(e)
                    else:
                        self.logq.put("监听异常（已恢复）: " + str(e)[:120])
                except Exception:
                    pass
            finally:
                time.sleep(MONITOR_INTERVAL)

    def _monitor_cycle(self):
        # 监听周期是一个完整的业务事务。非阻塞获取避免监听线程排队，
        # 由手动操作或统计刷新占用时直接等下一周期。
        if not self._game_op_lock.acquire(blocking=False):
            return
        try:
            if (self.game is None or not getattr(self, "_runtime_ready", True)
                    or not getattr(self, "_data_ready", True)):
                return
            if time.monotonic() < self._game_ready_at:
                return
            self._clear_stuck_busy()
            if self.busy:
                return
            st = self.ui_state
            cycle = getattr(self, "_mon_cycle", 0) + 1
            self._mon_cycle = cycle

            info = self._read_storage_info()
            if info is None or self.game is None:
                return
            self.uiq.put(("storage", info))
            self._check_deposit_full(st, info)
            try:
                self.uiq.put(("economic", json.loads(self.game.eval(js_economic_snapshot()))))
            except Exception:
                pass

            if st.get("auto_jewel"):
                self._auto_jewel(st)
            if st.get("auto_gear"):
                self._auto_equip(st, CONTENT_GEAR)
            if st.get("auto_acc"):
                self._auto_equip(st, CONTENT_ACCESSORY)
            if st.get("auto_dep") and not self.deposit_stopped_full:
                self._auto_deposit(st)
            if st.get("auto_org"):
                self._auto_organize(st, cycle)
        finally:
            self._game_op_lock.release()

    def _clear_stuck_busy(self):
        """兜底：万一某个工作线程没回来，别让界面永远灰着。"""
        if not self.busy:
            self.busy_since = 0.0
            return
        if not getattr(self, "busy_since", 0.0):
            self.busy_since = time.time()
            return
        if time.time() - self.busy_since > BUSY_TIMEOUT:
            self.busy = False
            self.busy_since = 0.0
            self._call_soon(lambda: self.set_busy(False))
            self.logq.put("上一个操作超时未返回，已解除界面锁定")

    def _read_storage_info(self):
        try:
            return json.loads(self.game.eval(js_storage_info()))
        except Exception as e:
            if self._is_transport_error(e):
                self._drop_connection(e)
            return None

    def _effective_pages(self, st, info):
        detected = (info or {}).get("pages") or st.get("detected_pages") or 0
        if st.get("pages_auto", True):
            return detected or st.get("pages_manual", 1)
        return st.get("pages_manual", 1)

    def _pages_override(self, st, info):
        return 0 if st.get("pages_auto", True) else self._effective_pages(st, info)

    def _check_deposit_full(self, st, info):
        total = info.get("storageTotal") or 0
        free = info.get("storageFree")
        if free is None:
            free = max(0, total - (info.get("storageUsed") or 0))
        if st.get("auto_dep") and free <= 0 and total > 0 \
                and st.get("stop_when_full", True):
            self.deposit_stopped_full = True
            self.uiq.put(("deposit_full", info))
            return
        if self.deposit_stopped_full and free > 0 and st.get("auto_resume", False):
            self.deposit_stopped_full = False
            self.uiq.put(("deposit_resume", None))

    # ---------- 自动：宝玉 ----------
    def _auto_jewel(self, st):
        include = st["jewel_storage"]
        counts = json.loads(self.game.eval(js_count(include)))
        self.uiq.put(("jewel", counts))
        ready = [t for t in st["jewel_tiers"]
                 if isinstance(counts.get(f"tier{t}"), int) and counts[f"tier{t}"] >= 6]
        if not ready:
            return
        self._log(f"检测到宝玉可合成：Tier {ready} → 自动执行")
        lines = self.game.eval(js_jewel_fuse(ready, include), await_promise=True) or []
        self._record_fusion_lines("jewel", lines, automatic=True)
        self._log_fusion_results("jewel", lines)
        self._call_soon(self._refresh_stats_view)

    # ---------- 自动：装备 / 饰品 ----------
    def _auto_equip(self, st, content_type):
        is_acc = content_type == CONTENT_ACCESSORY
        label = "饰品" if is_acc else "装备"
        include = st["acc_storage"] if is_acc else st["gear_storage"]
        same = st["acc_same_level"] if is_acc else st["gear_same_level"]
        tiers = st["acc_tiers"] if is_acc else st["gear_tiers"]
        if not tiers:
            return
        counts = json.loads(self.game.eval(js_equip_count(content_type, include)))
        self.uiq.put(("acc" if is_acc else "gear", counts))
        # 关键：批数取决于当前模式——勾了“仅同等级”看 batches，否则看 mixedBatches
        key = "batches" if same else "mixedBatches"
        ready = [t for t in tiers
                 if isinstance(counts.get(f"tier{t}"), dict)
                 and counts[f"tier{t}"].get(key, 0) > 0]
        if not ready:
            return
        self._log(f"检测到{label}可合成：Tier {ready} → 自动执行")
        lines = self.game.eval(
                js_equip_fuse(content_type, ready, include, same), await_promise=True) or []
        self._record_fusion_lines("accessory" if is_acc else "gear", lines, automatic=True)
        self._log_fusion_results("accessory" if is_acc else "gear", lines)
        self._call_soon(self._refresh_stats_view)

    # ---------- 自动：入库 ----------
    def _auto_deposit(self, st):
        types, tiers = st.get("dep_types") or [], st.get("dep_tiers") or []
        if not types or not tiers:
            return
        r = json.loads(self.game.eval(
            js_deposit(types, tiers, st.get("exclude_locked", True), 20,
                       self._pages_override(st, self.storage_info)),
            await_promise=True) or "{}")
        if r.get("deposited", 0) > 0:
            self._log(f"自动入库 {r['deposited']} 件"
                      f"（仓库 {r['storageUsed']}/{r['storageTotal']}）")
            # 新入库的物品只会落在第一个空位（也就是第一页），
            # 所以搬完立刻按「每页放一类」把它们挪到各自的分区页。
            self._arrange_after_deposit(st, "自动")
            # 记下这套筛选“确实有货”：刚搬完背包当然是空的，
            # 这时候再提示一句“没有可入库物品”只会让人以为又触发了一次入库
            self._dep_note_key = tuple(sorted(types))
        elif not (r.get("matchedByKey") or {}):
            # 同一套筛选只提醒一次，之后安静待命，不再每 2 秒刷一条
            key = tuple(sorted(types))
            if getattr(self, "_dep_note_key", None) != key:
                self._dep_note_key = key
                picked = "、".join(self.DEPOSIT_LABELS.get(k, str(k)) for k in types)
                self._log(f"自动入库已就绪：筛选（{picked}）")
                self._log("  背包里暂时没有可入库的物品，会自动等下一件")
                if 1 in types:
                    self._log("  提示：可堆叠材料（金币/经验/钻石这类）不在仓库容器里，无法入库")
        if r.get("full") and st.get("stop_when_full", True):
            self.deposit_stopped_full = True
            self.uiq.put(("deposit_full", r))

    # ---------- 自动：整理 ----------
    def _auto_organize(self, st, cycle):
        pages = self._effective_pages(st, self.storage_info)
        cats = self._page_cats_for(pages, st)
        if not cats:
            return
        every = max(1, int(st.get("org_every_min", 5) * 60 / MONITOR_INTERVAL))
        if cycle % every != 0:
            return
        r = self._run_organize(cats, dry_run=False,
                               include_locked=st.get("org_locked", False),
                               sort_after=st.get("org_sort", False),
                               pages_override=self._pages_override(st, self.storage_info))
        if r and r.get("ok"):
            self._log(f"自动整理：搬 {r['moved']} 件"
                      + (f"，失败 {r['failed']} 件" if r.get("failed") else "")
                      + f"（分布 {r.get('distribution')}）")
        elif r and r.get("error"):
            self._log("自动整理跳过：" + str(r["error"])[:80])

    def _arrange_after_deposit(self, st, source):
        """入库后按「每页放一类」把物品放到各自的分区页。

        游戏自己的入库接口（reqStorageMoveInList）只往第一个空位塞，所以刚入库的
        东西一定堆在第一页；这里复用整理那套放置方案把它们挪到对应页，
        装备进装备页、宝玉进宝玉页，不用等下一次定时整理。

        与「自动整理」独立：开关是单独一项，定时整理关着也照样归位。
        本来就放对的物品不会移动，所以这个调用是幂等的、只有新入库的那几件会动。
        """
        if not st.get("arrange_pages", True):
            return
        pages = self._effective_pages(st, self.storage_info)
        cats = self._page_cats_for(pages, st)
        if not cats or all(c == "any" for c in cats.values()):
            return          # 没配「每页放一类」就没什么可归位的
        r = self._run_organize(cats, dry_run=False,
                               include_locked=st.get("org_locked", False),
                               sort_after=st.get("org_sort", False),
                               pages_override=self._pages_override(st, self.storage_info))
        if not r:
            return
        if r.get("ok"):
            if r.get("moved"):
                self._log(f"  {source}入库后归位：搬 {r['moved']} 件到对应分类页"
                          + (f"，{r['failed']} 件没到位" if r.get("failed") else "")
                          + f"（分布 {r.get('distribution')}）")
            return
        # 归位失败不该盖掉“已入库 N 件”这条：入库本身是成功的，说明清楚即可
        self._log("  入库后归位跳过：" + str(r.get("error"))[:80])


    def _page_cats_for(self, pages, st):
        if pages <= 0:
            return {}
        cats = dict(st.get("page_cats") or {})
        return {p: cats.get(str(p), "any") for p in range(pages)}

    def _run_organize(self, cats, dry_run, include_locked, sort_after,
                      pages_override=0):
        try:
            return json.loads(self.game.eval(
                js_organize(cats, dry_run=dry_run, include_locked=include_locked,
                            sort_after=sort_after, pages_override=pages_override),
                await_promise=True))
        except Exception as e:
            if self._is_transport_error(e):
                self._drop_connection(e)
            else:
                self._log("整理失败: " + str(e)[:140])
            return None

    # ------------------------------------------------------------------
    # 主线程：满仓停入库 / 恢复
    # ------------------------------------------------------------------
    def _on_deposit_full(self, info):
        used = info.get("storageUsed", "?")
        total = info.get("storageTotal", "?")
        pages = info.get("pages", "?")
        if self.auto_dep_var.get():
            self.auto_dep_var.set(False)
            for tg in self.toggles:
                tg.sync()      # 程序化改值后重画开关，别让界面停在「开」的样子
        self.cfg["auto_deposit_enabled"] = False
        save_config(self.cfg)
        self._log(f"⚠ 仓库已满（{used}/{total}，{pages} 页）→ 已自动关闭自动入库")
        self._log("  腾出空间后可手动再勾选；勾上「腾出后恢复」就会自动重开")

    def _on_deposit_resume(self):
        if self.auto_dep_var.get():
            return
        self.auto_dep_var.set(True)
        self.cfg["auto_deposit_enabled"] = True
        save_config(self.cfg)
        self._log("仓库腾出空间 → 已自动恢复自动入库")

    # ------------------------------------------------------------------
    # 一键：自动合成 ×3 + 自动入库
    # ------------------------------------------------------------------
    def enable_all_auto(self):
        self._set_all_auto(True)

    def disable_all_auto(self):
        self._set_all_auto(False)

    def _set_all_auto(self, on):
        """一键开关四个自动项，行为与逐个拨动勾选框保持一致：存盘 + 日志 + 立即重算。"""
        self.auto_jewel_var.set(on)
        self.auto_gear_var.set(on)
        self.auto_acc_var.set(on)
        self.auto_dep_var.set(on)
        for tg in self.toggles:
            tg.sync()          # Toggle 是手绘的，程序化改值后必须重画
        self.cfg["auto_fuse_enabled"] = on
        self.cfg["auto_gear_fuse_enabled"] = on
        self.cfg["auto_acc_fuse_enabled"] = on
        self.cfg["auto_deposit_enabled"] = on
        save_config(self.cfg)
        if on:
            # 与单独勾「自动入库」同语义：别带着上一次的满仓停状态空转
            self.deposit_stopped_full = False
        self._log("一键" + ("开启" if on else "关闭")
                  + "：宝玉/装备/饰品 自动合成 + 自动入库")
        self._refresh_if_idle()

    # ------------------------------------------------------------------
    # 统计刷新
    # ------------------------------------------------------------------
    def refresh_all(self):
        if (self.game is None or not getattr(self, "_runtime_ready", True)
                or self.busy):
            return
        remaining = self._game_ready_at - time.monotonic()
        if remaining > 0:
            # 手动点选项发生在稳定窗口内时也顺延，不提前触碰工作台。
            self._call_soon(lambda: self.root.after(
                max(100, int(remaining * 1000)), self.refresh_all))
            return
        self.set_busy(True)
        # tk 变量只能在主线程读，所以先取快照再开线程
        jewel_inc = self.jewel_storage_var.get()
        gear_inc = self.gear_storage_var.get()
        acc_inc = self.acc_storage_var.get()
        count_modes = {"jewel": bool(jewel_inc), "gear": bool(gear_inc),
                       "accessory": bool(acc_inc)}
        old_modes = getattr(self, "_stats_count_modes", {})
        for category, mode in count_modes.items():
            if old_modes.get(category) is not None and old_modes.get(category) != mode:
                self._reset_stats_tracking(category)
        self._stats_count_modes = count_modes

        def work():
            try:
                with self._game_op_lock:
                    try:
                        stage_raw = self.game.eval(js_current_stage())
                        stage = json.loads(stage_raw) if isinstance(stage_raw, str) else stage_raw
                        if isinstance(stage, str) and stage:
                            self.uiq.put(("stage", stage))
                    except Exception:
                        pass
                    self.uiq.put(("jewel", json.loads(self.game.eval(js_count(jewel_inc)))))
                    self.uiq.put(("gear", json.loads(
                        self.game.eval(js_equip_count(CONTENT_GEAR, gear_inc)))))
                    self.uiq.put(("acc", json.loads(
                        self.game.eval(js_equip_count(CONTENT_ACCESSORY, acc_inc)))))
                    info = self._read_storage_info()
                    if info is None:
                        raise RuntimeError("仓库数据尚未准备好")
                    self.uiq.put(("storage", info))
                    try:
                        economic = json.loads(self.game.eval(js_economic_snapshot()))
                        self.uiq.put(("economic", economic))
                    except Exception:
                        # 经济页是只读增强功能，市场/物品快照失败不能阻断数量刷新。
                        pass
                    # 首次四项只读统计全部成功后，才允许监听线程执行
                    # 自动入库/整理/合成；避免“连接成功但数据仍是空壳”时改动游戏。
                    self._data_ready = True
            except Exception as e:
                if self._is_transport_error(e):
                    self._drop_connection(e)
                else:
                    self.logq.put("统计失败: " + str(e)[:140])
                    # 连接刚建立时数据库/融合表可能仍在注册；统计失败不应要求
                    # 用户点击“一键开启”才能再次刷新，短暂等待后自动重试。
                    self._call_soon(lambda: self.root.after(1500, self.refresh_all))
            finally:
                self._call_soon(lambda: self.set_busy(False))
                self._call_soon(self._refresh_stats_view)

        threading.Thread(target=work, daemon=True).start()

    # ---------- 显示：宝玉 ----------
    def _show_jewel_stats(self, counts):
        self.stats = counts
        total_all = 0
        for tier in range(1, 7):
            v = counts.get(f"tier{tier}", 0)
            if not isinstance(v, int) or v < 0:
                v = 0
            total_all += v
            can = v // 6
            sub = f"{can} 次" if can > 0 else "需 6 颗"
            self.jewel_tiles[tier].set_data(
                str(v), sub, GOLD if can > 0 else (FG if v > 0 else FAINT),
                ready=can > 0)
        if total_all == 0:
            if not self.jewel_storage_var.get():
                self.jewel_hint.configure(text="没有可用材料 —— 材料可能都在仓库，试试打开「含仓库物品」", fg=GOLD)
            else:
                self.jewel_hint.configure(text="没有可用材料", fg=FAINT)
        else:
            self.jewel_hint.configure(
                text="点方块选品级；标黄表示已够 6 颗，开启「自动合成」后满即合", fg=FAINT)

    # ---------- 显示：装备 / 饰品 ----------
    def _show_equip_stats(self, role, counts):
        gear = (role == "gear")
        tiles = self.gear_tiles if gear else self.acc_tiles
        hint = self.gear_hint if gear else self.acc_hint
        include = (self.gear_storage_var if gear else self.acc_storage_var).get()
        same = (self.gear_same_var if gear else self.acc_same_var).get()
        accent = VIOLET if gear else CYAN
        label = "装备" if gear else "饰品"

        total_all = 0
        ready_tiers = 0
        mixed_avail = 0
        best_level = best_have = 0
        need_shown = 6 if gear else 3
        for tier in range(1, 7):
            d = counts.get(f"tier{tier}") or {}
            total = d.get("total")
            if not isinstance(total, int) or total < 0:
                total = None
            batches_same = d.get("batches", 0) or 0
            batches_mixed = d.get("mixedBatches", 0) or 0
            batches = batches_same if same else batches_mixed
            need = d.get("need", 0) or need_shown
            if need <= 0:
                need = need_shown
            if total:
                total_all += total
            mixed_avail += batches_mixed
            if batches > 0:
                ready_tiers += 1
            bh = d.get("bestHave", 0) or 0
            if bh > best_have:
                best_have, best_level = bh, (d.get("bestLevel", 0) or 0)

            if total is None:
                tiles[tier].set_data("—", "", FAINT)
            elif total == 0:
                tiles[tier].set_data("0", "无材料", FAINT)
            elif batches > 0:
                tiles[tier].set_data(f"{total}", f"{batches} 批", accent, ready=True)
            else:
                tiles[tier].set_data(f"{total}", f"需 {need}", FG)

        if not total_all:
            if not include:
                hint.configure(text=f"{label}材料都在仓库里 —— 打开「含仓库物品」就能看到", fg=GOLD)
            else:
                hint.configure(text=f"没有可合成的{label}材料", fg=FAINT)
        elif ready_tiers == 0:
            unit = "件"
            if same and mixed_avail > 0:
                hint.configure(
                    text=f"同等级凑不满（{need_shown} {unit}/批）—— 关掉「仅同等级」即可合成 "
                         f"{mixed_avail} 批（按最高等级混着合）",
                    fg=GOLD)
            elif same:
                where = f"（最多的一档 Lv{best_level} 只有 {best_have} {unit}）" if best_level else ""
                hint.configure(
                    text=f"同等级至少要 {need_shown} {unit}才能合{where}；"
                         "想用低等级的凑数就关掉「仅同等级」",
                    fg=FAINT)
            else:
                hint.configure(
                    text=f"材料还不够，每批要 {need_shown} {unit}；继续攒",
                    fg=FAINT)
        else:
            hint.configure(text=f"{ready_tiers} 个品级可合成"
                                + ("　·　混合等级模式" if not same else ""), fg=FAINT)

    # ---------- 显示：仓库 ----------
    def _show_storage(self, info):
        self.storage_info = info
        self.detected_pages = info.get("pages", 0) or 0
        used = info.get("storageUsed", 0)
        total = info.get("storageTotal", 0)
        free = info.get("storageFree", 0)
        pages = info.get("pages", "?")
        slot = info.get("slotCnt", "?")
        full = isinstance(free, int) and free <= 0
        pct = f"{used / total * 100:.0f}%" if total else "—"
        self.storage_label.configure(text=f"{used} / {total}　({pct})")
        self.gauge.set(used, total)
        self.storage_sub.configure(
            text=f"{pages} 页 × {slot} 格　剩余 {free}"
                 + ("　【已满】" if full else ""),
            fg=RED if full else FAINT)
        self.storage_mini.configure(text=f"仓库 {used}/{total}",
                                    fg=RED if full else DIM)
        self.pages_label.configure(text=f"{pages} 页")
        if info.get("ok"):
            self._rebuild_page_rows(self._effective_pages(self.ui_state, info))

    def _rebuild_page_rows(self, pages):
        if pages <= 0:
            return
        cats = dict(self.cfg.get("storage_page_cats") or {})
        # 已经建好的行用界面上当前选中的分类覆盖配置：配置里存的是分类 id，
        # 下拉框里显示的是中文标签，所以必须过一遍 LABEL_TO_CAT，
        # 否则重建行时会把标签当成未知分类、跳回上一次存盘的值。
        cats.update({str(p): LABEL_TO_CAT.get(v.get(), "any")
                     for p, v in self.page_vars.items() if v.get()})
        if getattr(self, "_page_rows_built_for", None) == pages \
                and len(self.page_rows) == pages:
            return
        for child in self.page_frame.winfo_children():
            child.destroy()
        self.page_rows = []
        self.page_vars = {}
        labels = [CATEGORY_LABELS[c] for c in CAT_ORDER]
        per_row = 3 if pages > 3 else pages
        for start in range(0, pages, per_row):
            line = tk.Frame(self.page_frame, bg=CARD)
            line.pack(fill="x", pady=2)
            for p in range(start, min(start + per_row, pages)):
                cell = tk.Frame(line, bg=CARD)
                cell.pack(side="left", expand=True, fill="x", padx=(0, 6))
                tk.Label(cell, text=f"第 {p + 1} 页", bg=CARD, fg=FAINT,
                         font=(FONT, 8)).pack(anchor="w")
                key = cats.get(str(p), "any")
                if key not in CATEGORY_LABELS:
                    key = "any"
                var = tk.StringVar(value=CATEGORY_LABELS[key])
                self.page_vars[p] = var
                om = tk.OptionMenu(cell, var, *labels,
                                   command=lambda *_a: self.on_page_cat_change())
                om.configure(bg=CARD2, fg=FG, activebackground=CARD3,
                             activeforeground=FG, relief="flat",
                             highlightthickness=0, font=(FONT, 9), anchor="w",
                             bd=0, padx=8, pady=3)
                om["menu"].configure(bg=CARD2, fg=FG, font=(FONT, 9),
                                     activebackground=BLUE, activeforeground=INK)
                om.pack(fill="x")
                self.page_rows.append(p)
        self._page_rows_built_for = pages
        self._push_state()

    # ------------------------------------------------------------------
    # 开关回调
    # ------------------------------------------------------------------
    def on_toggle_jewel(self):
        self.cfg["auto_fuse_enabled"] = self.auto_jewel_var.get()
        self.cfg["fuse_tiers"] = [t for t in range(1, 7) if self.jewel_tier_vars[t].get()]
        save_config(self.cfg)
        self._log("宝玉自动合成：" + ("开启" if self.auto_jewel_var.get() else "关闭"))

    def on_jewel_tier_change(self):
        self.cfg["fuse_tiers"] = [t for t in range(1, 7) if self.jewel_tier_vars[t].get()]
        save_config(self.cfg)

    def on_jewel_storage(self):
        self.cfg["include_storage"] = self.jewel_storage_var.get()
        save_config(self.cfg)
        self._log("宝玉材料范围：" + ("背包 + 仓库" if self.jewel_storage_var.get() else "仅背包"))
        self.refresh_all()

    def on_toggle_gear(self):
        self._save_gear()
        self._log("装备自动合成：" + ("开启" if self.auto_gear_var.get() else "关闭"))
        self._refresh_if_idle()

    def on_gear_option(self):
        """品级 / 含仓库 / 仅同等级 变动：只存配置并重算，不刷总开关的日志。"""
        self._save_gear()
        self._refresh_if_idle()

    def _save_gear(self):
        self.cfg["auto_gear_fuse_enabled"] = self.auto_gear_var.get()
        self.cfg["gear_fuse_tiers"] = [t for t in range(1, 7) if self.gear_tier_vars[t].get()]
        self.cfg["gear_same_level"] = self.gear_same_var.get()
        self.cfg["gear_include_storage"] = self.gear_storage_var.get()
        save_config(self.cfg)

    def on_toggle_acc(self):
        self._save_acc()
        self._log("饰品自动合成：" + ("开启" if self.auto_acc_var.get() else "关闭"))
        self._refresh_if_idle()

    def on_acc_option(self):
        self._save_acc()
        self._refresh_if_idle()

    def _save_acc(self):
        self.cfg["auto_acc_fuse_enabled"] = self.auto_acc_var.get()
        self.cfg["acc_fuse_tiers"] = [t for t in range(1, 7) if self.acc_tier_vars[t].get()]
        self.cfg["acc_same_level"] = self.acc_same_var.get()
        self.cfg["acc_include_storage"] = self.acc_storage_var.get()
        save_config(self.cfg)

    def _refresh_if_idle(self):
        """改选项后立刻重算件数/批数，别等下一个监听周期。"""
        if self.game is None or self.busy:
            return
        self.uiq.put(("call", self.refresh_all))

    def on_toggle_deposit(self):
        self.cfg["auto_deposit_enabled"] = self.auto_dep_var.get()
        self.cfg["deposit_item_types"] = [k for k, v in self.dep_type_vars.items() if v.get()]
        self.cfg["deposit_tiers"] = [t for t in range(1, 7) if self.dep_tier_vars[t].get()]
        self.cfg["deposit_exclude_locked"] = self.lock_var.get()
        self.cfg["deposit_stop_when_full"] = self.stop_full_var.get()
        self.cfg["deposit_auto_resume"] = self.resume_var.get()
        self.cfg["deposit_arrange_pages"] = self.arrange_pages_var.get()
        if self.auto_dep_var.get():
            self.deposit_stopped_full = False
            # 刻意不动 _dep_note_key：筛选没变就不该重播那条“暂时没有可入库物品”，
            # 否则每点一下开关/类型都会多刷一条
        save_config(self.cfg)
        self._log("自动入库：" + ("开启" if self.auto_dep_var.get() else "关闭"))

    def on_toggle_organize(self):
        """整理相关的开关与间隔共用这个入口：存盘，并且只在值真的变了时才报一句。"""
        before_on = bool(self.cfg.get("auto_organize_enabled", False))
        before_min = int(self.cfg.get("organize_interval_min", 5) or 5)
        minutes = self._interval_minutes()
        on = bool(self.auto_org_var.get())
        self.cfg["auto_organize_enabled"] = on
        self.cfg["organize_include_locked"] = self.org_locked_var.get()
        self.cfg["organize_sort_after"] = self.org_sort_var.get()
        self.cfg["organize_interval_min"] = minutes
        save_config(self.cfg)
        # 间隔框失焦/回车也会走到这里，所以只在开关或间隔真的变了的时候记一条
        if on != before_on:
            self._log("自动整理：" + (f"开启，每 {_fmt_span(minutes)} 一次" if on else "关闭"))
        elif on and minutes != before_min:
            self._log(f"整理间隔：每 {_fmt_span(minutes)} 一次")

    def on_pages_mode(self):
        auto = self.pages_auto_var.get()
        self.cfg["storage_pages_auto"] = auto
        self.cfg["storage_pages_manual"] = self._int_of(self.pages_var, 3, 1, 20)
        save_config(self.cfg)
        self.pages_spin.configure(state="disabled" if auto else "normal")
        if self.detected_pages:
            self._rebuild_page_rows(self._effective_pages(self.ui_state, self.storage_info))

    def on_page_cat_change(self):
        # 先把最新的下拉框值同步进 ui_state，再存配置。
        # 否则 ui_state 还是上一秒的快照，写进去的永远是上一次的选择。
        self._push_state()
        pages = self._effective_pages(self.ui_state, self.storage_info)
        self.cfg["storage_page_cats"] = self._page_cats_for(pages, self.ui_state)
        self.cfg["storage_pages_manual"] = self._int_of(self.pages_var, 3, 1, 20)
        save_config(self.cfg)
        if self.detected_pages:
            self._rebuild_page_rows(pages)

    # ------------------------------------------------------------------
    # 手动动作
    # ------------------------------------------------------------------


    def _require(self):
        if self.game is None:
            messagebox.showerror("未连接", "游戏未连接，请先启动游戏并重启本面板。")
            return False
        if self.busy:
            return False
        return True

    def _run(self, expr, await_promise=True, on_done=None):
        self.set_busy(True)

        def work():
            try:
                with self._game_op_lock:
                    game = self.game
                    if game is None or not self._runtime_ready:
                        raise RuntimeError("游戏连接已断开，正在自动重连")
                    result = game.eval(expr, await_promise=await_promise)
                    if on_done:
                        on_done(result)
            except Exception as e:
                if self._is_transport_error(e) or "连接已断开" in str(e):
                    self._drop_connection(e, expected_game=locals().get("game"))
                else:
                    self._log("执行失败: " + str(e)[:160])
            finally:
                self._call_soon(lambda: self.set_busy(False))

        threading.Thread(target=work, daemon=True).start()

    def manual_jewel(self):
        if not self._require():
            return
        tiers = [t for t in range(1, 7) if self.jewel_tier_vars[t].get()]
        if not tiers:
            messagebox.showinfo("提示", "请至少点选一个品级。")
            return
        include = self.jewel_storage_var.get()
        if not messagebox.askyesno(
                "确认", f"宝玉合成品级 {tiers}（含仓库={include}）？\n"
                        "6 颗 → 1 颗下一品级，不可逆。"):
            return
        self._log(f"手动宝玉合成 {tiers}…")
        self._run(js_jewel_fuse(tiers, include),
                  on_done=lambda result: self._report_lines("jewel", result))

    def manual_gear(self):
        self._manual_equip(CONTENT_GEAR)

    def manual_acc(self):
        self._manual_equip(CONTENT_ACCESSORY)

    def _manual_equip(self, content_type):
        if not self._require():
            return
        is_acc = content_type == CONTENT_ACCESSORY
        label = "饰品" if is_acc else "装备"
        vars_ = self.acc_tier_vars if is_acc else self.gear_tier_vars
        tiers = [t for t in range(1, 7) if vars_[t].get()]
        if not tiers:
            messagebox.showinfo("提示", "请至少点选一个品级。")
            return
        same = self.acc_same_var.get() if is_acc else self.gear_same_var.get()
        include = self.acc_storage_var.get() if is_acc else self.gear_storage_var.get()
        need = 3 if is_acc else 6
        mode = "仅同等级" if same else "可混合等级"
        if not messagebox.askyesno(
                "确认", f"{label}合成品级 {tiers}\n"
                        f"{mode}（{need} 件/批）　含仓库={include}\n"
                        "概率出货，材料不可逆。"):
            return
        self._log(f"手动{label}合成 {tiers}…")
        self._run(js_equip_fuse(content_type, tiers, include, same),
                  on_done=lambda result: self._report_lines(
                      "accessory" if is_acc else "gear", result))

    def manual_deposit(self):
        if not self._require():
            return
        types = [k for k, v in self.dep_type_vars.items() if v.get()]
        tiers = [t for t in range(1, 7) if self.dep_tier_vars[t].get()]
        if not types or not tiers:
            messagebox.showinfo("提示", "请至少选一个入库类型和品级。")
            return
        self._log(f"手动入库：类型 {types}，品级 {_fmt_tiers(tiers)}…")
        # 归位要用到的开关必须在主线程先取好快照：工作线程不能读 tk 变量
        self._push_state()
        st = dict(self.ui_state)
        self._run(js_deposit(types, tiers, self.lock_var.get(), 20,
                             self._pages_override(self.ui_state, self.storage_info)),
                  on_done=lambda res: self._report_deposit(res, st))

    def manual_organize_preview(self):
        if not self._require():
            return
        pages = self._effective_pages(self.ui_state, self.storage_info)
        cats = self._page_cats_for(pages, self.ui_state)
        self._log(f"预览整理方案（{pages} 页）…")
        self._run(js_organize(cats, dry_run=True,
                              include_locked=self.org_locked_var.get(),
                              pages_override=self._pages_override(self.ui_state,
                                                                  self.storage_info)),
                  on_done=self._report_preview)

    def manual_organize(self):
        if not self._require():
            return
        pages = self._effective_pages(self.ui_state, self.storage_info)
        cats = self._page_cats_for(pages, self.ui_state)
        plan = "、".join(f"第{p + 1}页={CATEGORY_LABELS.get(c, c)}"
                        for p, c in sorted(cats.items()))
        if not messagebox.askyesno(
                "确认整理", f"按下面方案重排仓库里的物品？\n\n{plan}\n\n"
                            "只移动仓库内的物品，不会买卖或销毁任何东西。"):
            return
        self._log(f"整理仓库：{plan}")
        self._run(js_organize(cats, dry_run=False,
                              include_locked=self.org_locked_var.get(),
                              sort_after=self.org_sort_var.get(),
                              pages_override=self._pages_override(self.ui_state,
                                                                  self.storage_info)),
                  on_done=self._report_organize)

    # ------------------------------------------------------------------
    # 结果回报
    # ------------------------------------------------------------------
    def _report_lines(self, category, result):
        self._record_fusion_lines(category, result)
        self._log_fusion_results(category, result)
        self._call_soon(self._refresh_stats_view)
        self.logq.put("完成 ✔")
        self._call_soon(lambda: self.root.after(400, self.refresh_all))

    def _report_deposit(self, result, st=None):
        try:
            r = result if isinstance(result, dict) else json.loads(result or "{}")
        except Exception:
            self.logq.put("入库结果: " + str(result)[:160])
            return
        self.logq.put(f"入库 {r.get('deposited', 0)} 件"
                      f"（仓库 {r.get('storageUsed')}/{r.get('storageTotal')}，"
                      f"剩余 {r.get('storageFree')}）")
        if r.get("blocked"):
            self.logq.put(f"  {r['blocked']} 件因为仓库没位置没进去")
        self._explain_matched(r)
        if r.get("deposited", 0) > 0 and st:
            self._arrange_after_deposit(st, "手动")
        if r.get("full"):
            self.uiq.put(("deposit_full", r))
        self._call_soon(lambda: self.root.after(300, self.refresh_all))

    def _explain_matched(self, r):
        matched = r.get("matchedByKey")
        if not isinstance(matched, dict):
            return
        if r.get("deposited", 0) > 0:
            detail = "、".join(f"{self.DEPOSIT_LABELS.get(int(k), k)} {v} 件"
                              for k, v in sorted(matched.items(), key=lambda kv: int(kv[0])))
            if detail:
                self.logq.put(f"  本次匹配：{detail}")
            return
        if not matched:
            picked = [self.DEPOSIT_LABELS.get(k, str(k))
                      for k, v in self.dep_type_vars.items() if v.get()]
            self.logq.put(f"  背包里没有可入库的 {'、'.join(picked)} 物品，什么都没做")
            if self.dep_type_vars.get(1) and self.dep_type_vars[1].get():
                kinds = r.get("stackMaterialCount") or 0
                self.logq.put(f"  注意：可堆叠材料（金币/经验/钻石这类，当前 {kinds} 种）"
                              "是全局数量，不在仓库容器里，无法入库")
            if r.get("full"):
                self.logq.put("  仓库已满，先清点空间")

    def _report_preview(self, result):
        r = _as_dict(result)
        if not r or not r.get("ok"):
            self.logq.put("预览失败: " + str(r)[:160])
            return
        self.logq.put(f"  {r['items']} 件 / {r['pages']} 页 × {r['slotCnt']}，"
                      f"需搬 {r['planned']} 件（{r['steps']} 步，含中转），"
                      f"溢出留原地 {r['overflow']} 件")
        for line in r.get("plan", [])[:40]:
            self.logq.put("   " + str(line))

    def _report_organize(self, result):
        r = _as_dict(result)
        if not r or not r.get("ok"):
            self.logq.put("整理失败: " + str(r.get("error") if r else result)[:160])
            return
        self.logq.put(f"整理完成：搬 {r['moved']} 件"
                      + (f"，{r['failed']} 件没到位" if r.get("failed") else "")
                      + f"，分布 {r.get('distribution')}")
        self._call_soon(lambda: self.root.after(400, self.refresh_all))


def _fmt_span(minutes):
    """把总分钟数说成「1 小时 30 分钟」，日志和提示里都好读。"""
    try:
        minutes = max(0, int(minutes))
    except (TypeError, ValueError):
        minutes = 0
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours} 小时 {mins} 分钟"
    if hours:
        return f"{hours} 小时"
    return f"{mins} 分钟"


def _as_dict(value):
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value or "{}")
    except Exception:
        return None


def _fmt_tiers(lst):
    return "、".join("T" + str(t) for t in lst)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
