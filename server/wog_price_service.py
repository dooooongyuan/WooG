#!/usr/bin/env python3
"""Private Steam price cache service for the WoG assistant.

The process listens on localhost only. Nginx exposes one authenticated JSON
endpoint and never forwards upstream errors or server details to clients.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import secrets
import sqlite3
import subprocess
import threading
import time
from collections import deque
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_ID = "4891320"
HOST = "127.0.0.1"
PORT = int(os.environ.get("WOG_PRICE_PORT", "8788"))
DATA_DIR = Path(os.environ.get("WOG_PRICE_DATA_DIR", "./wog-price-data"))
TOKEN_FILE = Path(os.environ.get("WOG_PRICE_TOKEN_FILE", str(DATA_DIR / "token")))
DB_FILE = Path(os.environ.get("WOG_PRICE_DB", str(DATA_DIR / "prices.sqlite3")))
CATALOG_FILE = Path(os.environ.get("WOG_PRICE_CATALOG", str(DATA_DIR / "catalog.json")))
COOKIE_FILE = Path(os.environ.get("WOG_PRICE_COOKIE_FILE", str(DATA_DIR / "steam-cookie")))
# Steam 对同一出口的累计请求很敏感。成功报价保留更久，避免全目录每 20
# 分钟重新排队；普通错误也不应在短时间内反复探测。
REFRESH_INTERVAL = 6 * 60 * 60
ERROR_RETRY_INTERVAL = 30 * 60
RATE_LIMIT_BACKOFF = 3 * 60
RATE_LIMIT_MAX_BACKOFF = 60 * 60
# 单 worker 串行请求，间隔使用抖动而不是固定值，降低突发和规律性。
STEAM_REQUEST_MIN_INTERVAL = 45.0
STEAM_REQUEST_MAX_INTERVAL = 90.0
MAX_ITEMS = 128
MAX_BODY = 128 * 1024


def _clean(value: object) -> str:
    value = re.sub(r"<[^>]+>", "", str(value or ""))
    return re.sub(r"\s+", " ", value).strip().casefold()


def _market_name(item: dict) -> str:
    name = str(item.get("market_name") or item.get("name") or "").strip()
    tier = int(item.get("tier") or 0)
    if tier >= 3 and not re.search(r"\(\s*tier\s*\d+\s*\)", name, re.I):
        name = f"{name} (Tier {tier})"
    return name


def _key(item: dict) -> str:
    return _clean(_market_name(item))


def _read_token() -> str:
    try:
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    if len(token) < 32:
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(48)
        TOKEN_FILE.write_text(token + "\n", encoding="utf-8")
        try:
            os.chmod(TOKEN_FILE, 0o600)
        except OSError:
            pass
    return token


class PriceStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS prices (key TEXT PRIMARY KEY, name TEXT NOT NULL, price REAL, median REAL, volume TEXT, status TEXT NOT NULL, updated REAL NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS catalog (key TEXT PRIMARY KEY, name TEXT NOT NULL, market_name TEXT NOT NULL, tier INTEGER NOT NULL DEFAULT 0, added REAL NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS service_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        # Upgrade databases created before the catalog table existed. Existing
        # cached names become the initial background-refresh catalog.
        self.db.execute(
            "INSERT OR IGNORE INTO catalog(key,name,market_name,tier,added) "
            "SELECT key,name,name,0,updated FROM prices")
        for key, name in self.db.execute("SELECT key,name FROM catalog WHERE tier=0").fetchall():
            match = re.search(r"\(\s*tier\s*(\d+)\s*\)", str(name or ""), re.I)
            if match:
                self.db.execute("UPDATE catalog SET tier=? WHERE key=?", (int(match.group(1)), key))
        self.db.commit()

    @staticmethod
    def _ttl(status: str) -> float:
        if status == "rate_limited":
            return RATE_LIMIT_BACKOFF
        return ERROR_RETRY_INTERVAL if status == "error" else REFRESH_INTERVAL

    def get(self, key: str, fresh_only=True):
        with self.lock:
            row = self.db.execute("SELECT name, price, median, volume, status, updated FROM prices WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        name, price, median, volume, status, updated = row
        stale = time.time() - float(updated or 0) >= self._ttl(str(status or ""))
        if fresh_only and stale:
            return None
        return {"name": name, "price": price, "median": median, "volume": volume,
                "status": status, "updated_at": updated, "stale": stale}

    def remember(self, item: dict) -> str:
        key = _key(item)
        if not key:
            return ""
        name = str(item.get("name") or item.get("market_name") or key)
        market_name = _market_name(item)
        tier = int(item.get("tier") or 0)
        with self.lock:
            self.db.execute(
                "INSERT INTO catalog(key,name,market_name,tier,added) VALUES(?,?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET name=excluded.name, market_name=excluded.market_name, tier=excluded.tier",
                (key, name, market_name, tier, time.time()))
            self.db.commit()
        return key

    def catalog_items(self):
        with self.lock:
            rows = self.db.execute("SELECT key,name,market_name,tier FROM catalog ORDER BY key").fetchall()
        return [{"key": key, "name": name, "market_name": market_name, "tier": tier}
                for key, name, market_name, tier in rows]

    def due(self, key: str) -> bool:
        return self.get(key, fresh_only=True) is None

    def rate_limit_state(self):
        with self.lock:
            rows = dict(self.db.execute(
                "SELECT key,value FROM service_state WHERE key IN ('rate_limit_until','rate_limit_count')"
            ).fetchall())
            if "rate_limit_until" not in rows:
                last_hit = self.db.execute(
                    "SELECT MAX(updated) FROM prices WHERE status='rate_limited'"
                ).fetchone()[0]
                if last_hit:
                    return {"until": float(last_hit) + RATE_LIMIT_BACKOFF, "count": 1}
        return {"until": float(rows.get("rate_limit_until", 0) or 0),
                "count": int(rows.get("rate_limit_count", 0) or 0)}

    def mark_rate_limited(self):
        with self.lock:
            current = dict(self.db.execute(
                "SELECT key,value FROM service_state WHERE key IN ('rate_limit_until','rate_limit_count')"
            ).fetchall())
            count = int(current.get("rate_limit_count", 0) or 0) + 1
            backoff = min(RATE_LIMIT_BACKOFF * (2 ** (count - 1)), RATE_LIMIT_MAX_BACKOFF)
            until = time.time() + backoff
            self.db.executemany(
                "INSERT OR REPLACE INTO service_state(key,value) VALUES(?,?)",
                (("rate_limit_until", str(until)), ("rate_limit_count", str(count))))
            self.db.commit()
        return until

    def clear_rate_limit(self):
        with self.lock:
            self.db.execute(
                "DELETE FROM service_state WHERE key IN ('rate_limit_until','rate_limit_count')")
            self.db.commit()

    def put(self, key: str, row: dict):
        with self.lock:
            if row.get("status") in ("error", "rate_limited", "missing"):
                previous = self.db.execute(
                    "SELECT name,price,median,volume FROM prices WHERE key=?", (key,)
                ).fetchone()
                if previous and previous[1] is not None:
                    row = {**row, "name": row.get("name") or previous[0],
                           "price": previous[1], "median": previous[2],
                           "volume": previous[3]}
            self.db.execute("INSERT OR REPLACE INTO prices(key,name,price,median,volume,status,updated) VALUES(?,?,?,?,?,?,?)", (key, row.get("name", ""), row.get("price"), row.get("median"), row.get("volume"), row.get("status", "error"), time.time()))
            self.db.commit()

    def close(self):
        with self.lock:
            self.db.close()


def _money(value: object):
    if value is None:
        return None
    text = str(value).replace("\xa5", "").replace("￥", "").replace(",", "").strip()
    match = re.search(r"\d+(?:\.\d+)?", text)
    return round(float(match.group(0)), 2) if match else None


def _steam_cookie_header() -> str:
    """Read an optional, server-local Steam cookie without logging or exposing it."""
    try:
        if os.name == "posix" and COOKIE_FILE.stat().st_mode & 0o077:
            return ""
        cookie = COOKIE_FILE.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""
    if (not cookie or len(cookie) > 8192 or "\r" in cookie or "\n" in cookie
            or not re.search(r"(?:^|;)\s*steamLoginSecure=", cookie)
            or not re.search(r"(?:^|;)\s*sessionid=", cookie)):
        return ""
    return cookie


def _fetch(item: dict) -> dict:
    name = _market_name(item)
    if int(item.get("tier") or 0) < 3 or not re.search(r"\(\s*tier\s*\d+\s*\)", name, re.I):
        return {"name": name, "price": None, "median": None, "volume": None, "status": "missing"}
    query = urllib.parse.urlencode({"appid": APP_ID, "currency": "23", "market_hash_name": name})
    url = "https://steamcommunity.com/market/priceoverview/?" + query
    cookie = _steam_cookie_header()
    try:
        # Steam 对 urllib 的默认 TLS/指纹偶发返回 429；curl 的系统 TLS
        # 栈更稳定。参数使用列表传递，避免 shell 解释市场名称。
        command = ["/usr/bin/curl", "-sS", "--compressed", "--max-time", "12", "-A", "Mozilla/5.0",
                   "-H", "Accept: */*", "-H", "Referer: https://steamcommunity.com/market/",
                   "-w", "\n%{http_code}"]
        curl_config = ""
        if cookie:
            escaped_cookie = cookie.replace("\\", "\\\\").replace('"', '\\"')
            curl_config = f'header = "Cookie: {escaped_cookie}"\n'
            command.extend(("--config", "-"))
        completed = subprocess.run(command + [url], input=curl_config.encode("utf-8"),
                                   check=True, capture_output=True, timeout=15)
        raw = completed.stdout[:64 * 1024].decode("utf-8", "replace")
        body, separator, status_code = raw.rpartition("\n")
        if separator and status_code == "429":
            return {"name": name, "price": None, "median": None, "volume": None,
                    "status": "rate_limited"}
        if separator and status_code != "200":
            return {"name": name, "price": None, "median": None, "volume": None,
                    "status": "error"}
        payload = json.loads(body if separator else raw)
    except Exception:
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            if cookie:
                headers["Cookie"] = cookie
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=12) as response:
                payload = json.loads(response.read(64 * 1024).decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            status = "rate_limited" if exc.code == 429 else "error"
            return {"name": name, "price": None, "median": None, "volume": None,
                    "status": status}
        except Exception:
            return {"name": name, "price": None, "median": None, "volume": None, "status": "error"}
    price = _money(payload.get("lowest_price")) if isinstance(payload, dict) else None
    median = _money(payload.get("median_price")) if isinstance(payload, dict) else None
    return {"name": name, "price": price, "median": median, "volume": payload.get("volume") if isinstance(payload, dict) else None, "status": "ok" if price is not None else "missing"}


class PriceWorker:
    """按客户端实际请求补价；HTTP 请求本身永远不访问 Steam。

    目录只用于记住已经见过的市场名称，不再在启动或定时任务中全量预热。
    这样同一台服务器上的多个助手共享已查缓存，只有用户实际需要的物品
    才会进入 Steam 请求队列。
    """

    def __init__(self, store: PriceStore, catalog_file: Path):
        self.store = store
        self.catalog_file = catalog_file
        self.queue = deque()
        self.pending = set()
        self.lock = threading.Lock()
        self.stop = threading.Event()
        persisted = store.rate_limit_state()
        self.rate_limited_until = time.monotonic() + max(
            0.0, persisted["until"] - time.time())
        self.thread = threading.Thread(target=self._run, name="wog-price-worker", daemon=True)

    def start(self):
        # catalog 文件只保留兼容旧部署的名称映射；不把整个目录加入队列。
        self._load_catalog_file()
        self.thread.start()

    def _load_catalog_file(self):
        try:
            raw = json.loads(self.catalog_file.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError):
            return
        rows = raw.get("items") if isinstance(raw, dict) else raw
        if not isinstance(rows, list):
            return
        for item in rows:
            if isinstance(item, dict):
                self.store.remember(item)

    def enqueue(self, items, priority=False):
        prepared = []
        for item in items or []:
            if not isinstance(item, dict) or int(item.get("tier") or 0) < 3:
                continue
            key = self.store.remember(item)
            if not key:
                continue
            with self.lock:
                queued = next((row for row in self.queue if row.get("key") == key), None)
                if key in self.pending and (not priority or queued is None):
                    if priority and queued is None:
                        prepared.append({**item, "key": key})
                    else:
                        continue
                    continue
                self.pending.add(key)
                prepared.append({**item, "key": key})
        if prepared:
            unique = []
            seen = set()
            for row in prepared:
                if row["key"] in seen:
                    continue
                seen.add(row["key"])
                unique.append(row)
            with self.lock:
                if priority:
                    priority_keys = {row["key"] for row in unique}
                    self.queue = deque(row for row in self.queue
                                       if row.get("key") not in priority_keys)
                    self.queue.extendleft(reversed(unique))
                else:
                    self.queue.extend(unique)

    def _refresh(self, item):
        key = item.get("key") or _key(item)
        if not key or not self.store.due(key):
            return None
        row = _fetch(item)
        self.store.put(key, row)
        return row.get("status")

    def _run(self):
        while not self.stop.is_set():
            now = time.monotonic()
            cooldown = self.rate_limited_until - now
            if cooldown > 0:
                self.stop.wait(min(1.0, cooldown))
                continue
            with self.lock:
                item = self.queue.popleft() if self.queue else None
            if item is None:
                self.stop.wait(1.0)
                continue
            try:
                status = self._refresh(item)
                if status == "rate_limited":
                    until = self.store.mark_rate_limited()
                    self.rate_limited_until = time.monotonic() + max(0.0, until - time.time())
                elif status in ("ok", "missing"):
                    self.store.clear_rate_limit()
            except Exception:
                # 失败由 store 的 error 时间戳控制重试，不让 worker 线程退出。
                key = item.get("key") or _key(item)
                if key:
                    self.store.put(key, {"name": _market_name(item), "price": None,
                                         "median": None, "volume": None, "status": "error"})
            finally:
                with self.lock:
                    self.pending.discard(item.get("key") or _key(item))
                self.stop.wait(random.uniform(STEAM_REQUEST_MIN_INTERVAL,
                                              STEAM_REQUEST_MAX_INTERVAL))


class Handler(BaseHTTPRequestHandler):
    server_version = "WOGPrice"
    sys_version = ""

    def log_message(self, *_args):
        return

    def _reply(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._reply(HTTPStatus.OK, {"ok": True})
        else:
            self._reply(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})

    def do_POST(self):
        if self.path != "/prices":
            self._reply(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
            return
        supplied = self.headers.get("Authorization", "")
        expected = "Bearer " + TOKEN
        if not secrets.compare_digest(supplied, expected):
            self._reply(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY:
                raise ValueError
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            items = payload.get("items") if isinstance(payload, dict) else None
            prices = payload.get("prices") if isinstance(payload, dict) else None
            # refresh 仅保留协议兼容性；服务器不再根据 items 主动访问 Steam。
            if not isinstance(items, list) or len(items) > MAX_ITEMS:
                raise ValueError
            if prices is not None and (not isinstance(prices, list) or len(prices) > MAX_ITEMS):
                raise ValueError
        except (ValueError, TypeError, json.JSONDecodeError, UnicodeError):
            self._reply(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_request"})
            return
        results = []
        requested = []
        for item in items:
            if not isinstance(item, dict) or not _key(item):
                continue
            key = _key(item)
            STORE.remember(item)
            requested.append({**item, "key": key})
        # 客户端负责从自己的网络采集 Steam 价格后上报；服务器只验证并保存
        # 共享缓存，不把任何客户端请求转发到 Steam。
        for row in prices or []:
            if not isinstance(row, dict):
                continue
            try:
                tier = int(row.get("tier") or 0)
                price = float(row.get("price"))
            except (TypeError, ValueError, OverflowError):
                continue
            if tier < 3 or price < 0 or price > 1_000_000:
                continue
            item = {"name": row.get("name") or row.get("market_name") or "",
                    "market_name": row.get("market_name") or row.get("name") or "",
                    "tier": tier}
            key = STORE.remember(item)
            if not key:
                continue
            STORE.put(key, {"name": _market_name(item), "price": price,
                            "median": row.get("median"), "volume": row.get("volume"),
                            "status": "ok"})
            if not any(existing.get("key") == key for existing in requested):
                requested.append({**item, "key": key})
        for item in requested:
            key = item["key"]
            fresh = STORE.get(key, fresh_only=True)
            stale = STORE.get(key, fresh_only=False)
            row = fresh or stale
            if row is None:
                results.append({"key": key, "name": _market_name(item), "price": None,
                                "median": None, "volume": None, "status": "pending"})
                continue
            results.append({"key": key, "name": row.get("name", _market_name(item)),
                            "price": row.get("price"), "median": row.get("median"),
                            "volume": row.get("volume"),
                            "status": row.get("status", "error") if fresh else "stale",
                            "updated_at": row.get("updated_at")})
        self._reply(HTTPStatus.OK, {"ok": True, "items": results,
                                    "server_refresh_interval": REFRESH_INTERVAL})


TOKEN = _read_token()
STORE = PriceStore(DB_FILE)
WORKER = PriceWorker(STORE, CATALOG_FILE)


def main():
    Path(DB_FILE).parent.mkdir(parents=True, exist_ok=True)
    WORKER.start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
