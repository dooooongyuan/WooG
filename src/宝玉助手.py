#!/usr/bin/env python3
"""WoG 宝玉助手：游戏接口层（JS 流程库 + CDP 客户端 + 命令行入口）。

本模块是插件唯一的“游戏接口层”，所有在游戏主线程里跑的 JS 都定义在这里：

- 宝玉助手配置.json 的默认值、读取与保存（打包成 exe 后写在 exe 同目录）
- Game：通过游戏 Puerts 调试端口 127.0.0.1:10998 执行 JS
- 各 JS 流程：仓库信息、计数探测、宝玉/装备/饰品合成、入库、仓库整理
- js_* 构建函数：把参数拼成一条可直接 eval 的表达式，供面板与命令行共用

宝玉面板.py 只负责 UI 与调度，JS 一律从这里 import，杜绝两份实现各自漂移。

用法：
  python 宝玉助手.py stats            # 只读：宝玉各品级可合成数量
  python 宝玉助手.py gear 3           # 装备合成（contentType=1）
  python 宝玉助手.py acc 3            # 饰品合成（contentType=2）
  python 宝玉助手.py fuse 1,2,3       # 宝玉合成（contentType=20）
  python 宝玉助手.py deposit          # 按配置入库
  python 宝玉助手.py storage          # 只读：仓库页数/占用/分类分布
  python 宝玉助手.py organize         # 按配置整理仓库（先 dry-run 预览）
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

import websocket

if sys.stdout is not None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    except Exception:  # 打包成 windowed exe 时没有 stdout
        pass

WS_URL = "ws://127.0.0.1:10998"
WS_CONNECT_TIMEOUT = 5
WS_REQUEST_TIMEOUT = 180
FRIDA_BRIDGE_NAME = "bridge.js"
FRIDA_EVAL_TIMEOUT_MS = 5000
FRIDA_ASYNC_TIMEOUT_SECONDS = 120
FRIDA_UNDEFINED = "__wog_undefined__"

if getattr(sys, "frozen", False):
    RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
else:
    RESOURCE_DIR = Path(__file__).resolve().parent

# 游戏内枚举（nn.helper.Enums.table 里查得，见 README 的“游戏接口”一节）
CONTENT_GEAR = 1        # E_ContentType.Gear      装备
CONTENT_ACCESSORY = 2   # E_ContentType.Accessory 饰品
CONTENT_JEWEL = 20      # E_ContentType.Jewel     宝玉
ABILITY_INV_SLOT = 124  # E_AbilityType.Final_Inventory_Slot
ABILITY_STORAGE_PAGE = 125  # E_AbilityType.Final_Storage_Page

# 入库物品类型：与面板勾选项、配置文件里的 deposit_item_types 一一对应
DEPOSIT_MATERIAL = 1
DEPOSIT_GEAR = 4
DEPOSIT_JEWEL = 6
DEPOSIT_ACCESSORY = 7

# 仓库页分类：与面板每页的下拉框一一对应
PAGE_CATEGORIES = ("jewel", "accessory", "gear", "material", "other", "any")
CATEGORY_LABELS = {
    "jewel": "宝玉",
    "accessory": "饰品",
    "gear": "装备",
    "material": "材料",
    "other": "其他",
    "any": "不限",
}


def app_dir() -> Path:
    """exe 同目录（打包后）或脚本所在目录（源码运行）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


CONFIG_FILE = app_dir() / "宝玉助手配置.json"

DEFAULT_CONFIG = {
    # ---- 宝玉（contentType 20）----
    "fuse_tiers": [1, 2, 3],
    "include_storage": True,
    "auto_fuse_enabled": False,
    # ---- 装备（contentType 1）----
    "gear_fuse_tiers": [3],
    "gear_same_level": True,
    "gear_include_storage": True,
    "auto_gear_fuse_enabled": False,
    # ---- 饰品（contentType 2）：原生 3 件合 1 件，按融合表动态取件数 ----
    "acc_fuse_tiers": [1, 2, 3],
    "acc_same_level": True,
    "acc_include_storage": True,
    "auto_acc_fuse_enabled": False,
    # ---- 入库 ----
    "deposit_item_types": [6, 4],
    "deposit_tiers": [1, 2, 3],
    "deposit_exclude_locked": True,
    "auto_deposit_enabled": False,
    # 入库后把物品放到「每页放一类」对应的那页（复用整理的放置方案）。
    # 游戏自己的入库接口只往第一个空位塞，不做这一步就会全堆在第一页。
    "deposit_arrange_pages": True,
    # 仓库满时自动关闭入库；可选在腾出空间后自动恢复
    "deposit_stop_when_full": True,
    "deposit_auto_resume": False,
    # ---- 仓库整理 ----
    "storage_page_cats": {"0": "jewel", "1": "accessory", "2": "gear"},
    "organize_include_locked": False,
    "organize_sort_after": False,
    "auto_organize_enabled": False,
    # 每 N 分钟整理一次（面板上按 小时+分钟 填，这里存总分钟数）
    "organize_interval_min": 5,
    # 仓库页数：默认自动检测（Final_Storage_Page），关掉就用 storage_pages_manual
    "storage_pages_auto": True,
    "storage_pages_manual": 3,
    # ---- 杂项 ----
    # 日志只保留最近 N 分钟，0 = 不自动清理
    "log_keep_minutes": 60,
    # 窗口位置尺寸，关掉时记下来，下次原样还原
    "window_geometry": "",
    # 邮件通知：QQ SMTP SSL，授权码只保存在本机配置文件。
    "mail_push_enabled": False,
    "mail_push_name": "WoG宝玉助手",
    "mail_sender": "",
    "mail_recipient": "",
    "mail_auth_code": "",
    "mail_fusion_targets": [4],
}


CONFIG_HELP = {
    "fuse_tiers": "宝玉：要自动合成的品级（1=下级 … 6=最高级）",
    "include_storage": "宝玉：是否把仓库里的宝玉也算作材料",
    "auto_fuse_enabled": "宝玉：自动合成总开关",
    "gear_fuse_tiers": "装备：要自动合成的品级",
    "gear_same_level": "装备：只把同一穿戴等级的凑一批（关掉则按最高等级混合）",
    "gear_include_storage": "装备：是否把仓库里的装备也算作材料",
    "auto_gear_fuse_enabled": "装备：自动合成总开关",
    "acc_fuse_tiers": "饰品：要自动合成的品级",
    "acc_same_level": "饰品：只把同一穿戴等级的凑一批",
    "acc_include_storage": "饰品：是否把仓库里的饰品也算作材料",
    "auto_acc_fuse_enabled": "饰品：自动合成总开关",
    "deposit_item_types": "入库类型：1=材料(非堆叠) 4=装备 6=宝玉 7=饰品",
    "deposit_tiers": "入库品级：只收这些品级",
    "deposit_exclude_locked": "入库：跳过已锁定的物品",
    "auto_deposit_enabled": "自动入库总开关",
    "deposit_stop_when_full": "仓库满时自动关掉自动入库，避免空转",
    "deposit_auto_resume": "当初因满仓被停掉的，腾出空间后自动重开",
    "deposit_arrange_pages": "入库后按「每页放一类」把物品放到对应分区页",
    "storage_page_cats": "仓库每页放什么分类：jewel/accessory/gear/material/other/any",
    "organize_include_locked": "整理：连锁定物品一起搬（默认不动它们）",
    "organize_sort_after": "整理：搬完再调一次游戏自带的仓库排序",
    "auto_organize_enabled": "自动整理总开关",
    "organize_interval_min": "自动整理间隔，单位分钟（面板上填小时+分钟）",
    "storage_pages_auto": "仓库页数自动检测（关掉则用手填的页数）",
    "storage_pages_manual": "手填的仓库页数",
    "log_keep_minutes": "日志只保留最近 N 分钟，0 = 不自动清理",
    "window_geometry": "窗口位置尺寸，自动记录",
}

_BOOL_KEYS = (
    "include_storage", "auto_fuse_enabled", "gear_same_level", "gear_include_storage",
    "auto_gear_fuse_enabled", "acc_same_level", "acc_include_storage",
    "auto_acc_fuse_enabled", "deposit_exclude_locked", "auto_deposit_enabled",
    "deposit_stop_when_full", "deposit_auto_resume", "organize_include_locked",
    "organize_sort_after", "auto_organize_enabled", "storage_pages_auto",
    "deposit_arrange_pages", "mail_push_enabled",
)
_INT_KEYS = {
    "organize_interval_min": (5, 1, 10080),
    "storage_pages_manual": (3, 1, 20),
    "log_keep_minutes": (60, 0, 20160),
}
_TIER_LIST_KEYS = ("fuse_tiers", "gear_fuse_tiers", "acc_fuse_tiers", "deposit_tiers",
                   "mail_fusion_targets")
_TYPE_LIST_KEYS = ("deposit_item_types",)

# 最近一次读配置出的问题，面板启动后提示一次
LAST_LOAD_WARNING = ""


def _read_config(path):
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        raise ValueError("配置顶层必须是 JSON 对象")
    return raw


def load_config() -> dict:
    """读取设置；文件损坏时尝试上一次有效备份。"""
    global LAST_LOAD_WARNING
    LAST_LOAD_WARNING = ""
    cfg = dict(DEFAULT_CONFIG)
    errors = []
    for path in _config_candidates():
        if not path.exists():
            continue
        try:
            raw = _read_config(path)
        except (OSError, ValueError) as exc:
            errors.append(f"{path.name}: {exc}")
            if path == CONFIG_FILE:
                try:
                    shutil.copy2(path, path.with_suffix(path.suffix + ".bad"))
                except OSError:
                    pass
            continue
        cfg.update({k: v for k, v in raw.items() if not str(k).startswith("_")})
        if errors or path != CONFIG_FILE:
            LAST_LOAD_WARNING = f"已从 {path} 恢复配置。" + "；".join(errors)
        return _sanitize(cfg)
    if errors:
        LAST_LOAD_WARNING = "配置及备份读取失败，使用默认设置：" + "；".join(errors)
    return _sanitize(cfg)


def _sanitize(cfg: dict) -> dict:
    """逐项校验，保留合法设置，避免字符串 false 被误判为开启。"""
    cfg = dict(cfg)
    for key in _BOOL_KEYS:
        value = cfg.get(key, DEFAULT_CONFIG[key])
        if isinstance(value, str):
            value = {"true": True, "false": False, "1": True, "0": False}.get(
                value.strip().lower(), DEFAULT_CONFIG[key])
        elif not isinstance(value, bool):
            value = bool(value) if value in (0, 1) else DEFAULT_CONFIG[key]
        cfg[key] = value
    for key, (dflt, lo, hi) in _INT_KEYS.items():
        try:
            cfg[key] = max(lo, min(hi, int(cfg.get(key, dflt))))
        except (ValueError, TypeError, OverflowError):
            cfg[key] = dflt
    for key in _TIER_LIST_KEYS:
        cfg[key] = _int_list(cfg.get(key), DEFAULT_CONFIG[key], 1, 6)
    cfg["deposit_item_types"] = [n for n in _int_list(
        cfg.get("deposit_item_types"), DEFAULT_CONFIG["deposit_item_types"], 1, 7)
        if n in (1, 4, 6, 7)]
    cats = cfg.get("storage_page_cats")
    if not isinstance(cats, dict):
        cats = DEFAULT_CONFIG["storage_page_cats"]
    normalized = {}
    for key, value in cats.items():
        try:
            page = int(key)
        except (ValueError, TypeError):
            continue
        if 0 <= page < 20 and isinstance(value, str):
            category = next((k for k, label in CATEGORY_LABELS.items() if label == value), value)
            if category in PAGE_CATEGORIES:
                normalized[str(page)] = category
    cfg["storage_page_cats"] = normalized
    geom = cfg.get("window_geometry")
    cfg["window_geometry"] = geom if isinstance(geom, str) else ""
    for key in ("mail_push_name", "mail_sender", "mail_recipient", "mail_auth_code"):
        value = cfg.get(key, "")
        cfg[key] = str(value).strip() if isinstance(value, str) else ""
    if not cfg["mail_push_name"] or len(cfg["mail_push_name"]) > 40:
        cfg["mail_push_name"] = DEFAULT_CONFIG["mail_push_name"]
    return cfg


def _int_list(value, fallback, lo, hi) -> list:
    if not isinstance(value, (list, tuple)):
        value = fallback
    out = []
    for x in value:
        try:
            n = int(x)
        except (ValueError, TypeError, OverflowError):
            continue
        if lo <= n <= hi and n not in out:
            out.append(n)
    return out


def _config_candidates():
    yield CONFIG_FILE
    yield CONFIG_FILE.with_suffix(CONFIG_FILE.suffix + ".bak")
    # 源码首次启动可以沿用 dist 中的设置；已有源码配置时始终优先用它。
    if not getattr(sys, "frozen", False):
        yield CONFIG_FILE.parent / "dist" / CONFIG_FILE.name
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        yield Path(meipass) / CONFIG_FILE.name


_CONFIG_LOCK = threading.Lock()


def _atomic_config_write(path, data):
    # 每次写入独立临时文件，避免并行实例抢同一个 .tmp 文件。
    tmp = path.with_name(path.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def save_config(cfg: dict) -> None:
    """原子保存完整配置与有效备份；失败抛出异常，由界面提示。"""
    with _CONFIG_LOCK:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        settings = _sanitize({**DEFAULT_CONFIG, **cfg})
        payload = {key: settings[key] for key in DEFAULT_CONFIG}
        payload.update({k: v for k, v in settings.items()
                        if k not in payload and not str(k).startswith("_")})
        payload["_说明"] = CONFIG_HELP
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        if CONFIG_FILE.exists():
            try:
                previous = _read_config(CONFIG_FILE)
            except (OSError, ValueError):
                previous = None
            if previous is not None:
                _atomic_config_write(CONFIG_FILE.with_suffix(CONFIG_FILE.suffix + ".bak"),
                                     json.dumps(previous, ensure_ascii=False, indent=2) + "\n")
        _atomic_config_write(CONFIG_FILE, text)
        backup = CONFIG_FILE.with_suffix(CONFIG_FILE.suffix + ".bak")
        if not backup.exists():
            _atomic_config_write(backup, text)


def _find_game_pid() -> int:
    """找唯一的 Genesis.exe，供 WebSocket 不可用时的 Frida 回退连接使用。"""
    import psutil

    matches = []
    for process in psutil.process_iter(["name"]):
        try:
            if (process.info.get("name") or "").casefold() == "genesis.exe":
                matches.append(process.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if not matches:
        raise RuntimeError("未找到 Genesis.exe")
    if len(matches) > 1:
        raise RuntimeError("检测到多个 Genesis.exe，无法自动选择")
    return matches[0]


def _frida_bridge_source() -> str:
    candidates = [
        RESOURCE_DIR / FRIDA_BRIDGE_NAME,
        Path(__file__).resolve().parents[2] / "scripts" / "drop_interval_mod" / FRIDA_BRIDGE_NAME,
    ]
    for path in candidates:
        if path.is_file():
            return path.read_text(encoding="utf-8")
    raise FileNotFoundError("缺少 Frida 桥接脚本 bridge.js")


class Game:
    """在游戏主线程执行 JS：优先 Puerts CDP，端口失效时直连 Frida。"""

    def __init__(self, url: str = WS_URL, pid: int | None = None, allow_frida: bool = False,
                 timeout: float = WS_CONNECT_TIMEOUT,
                 request_timeout: float = WS_REQUEST_TIMEOUT):
        self.ws = None
        self.session = None
        self.script = None
        self.seq = 0
        self.mode = "websocket"
        self.pid = None
        self._frida_detached = threading.Event()
        self._frida_detach_reason = None
        self._frida_script_errors = []
        self.allow_frida = allow_frida
        self.request_timeout = request_timeout
        # 一条请求从 send 到收到自己的回复之前独占 recv()；否则两个线程并行时
        # 会互相把对方的回复帧吃掉，先发的那条要一直等到 socket 超时。
        self._request_lock = threading.Lock()
        try:
            self.ws = websocket.create_connection(url, timeout=timeout)
            # 建立连接可以快速失败，但游戏内 JS/Promise 可能运行几十秒；
            # 不能把连接探测用的 5 秒超时继续带到业务请求上。
            if hasattr(self.ws, "settimeout"):
                self.ws.settimeout(request_timeout)
        except Exception as ws_error:
            if not allow_frida:
                raise RuntimeError(f"无法连接 {url}：{ws_error}") from ws_error
            try:
                self._connect_frida(pid)
            except Exception as frida_error:
                raise RuntimeError(
                    f"调试端口连接失败（{url}）：{ws_error}；Frida 直连失败：{frida_error}"
                ) from frida_error

    @property
    def connection_label(self) -> str:
        if self.mode == "frida":
            return f"Frida 直连（Genesis.exe PID {self.pid}）"
        return "WebSocket 127.0.0.1:10998"

    def _connect_frida(self, pid: int | None = None) -> None:
        import frida

        self.pid = int(pid) if pid is not None else _find_game_pid()
        self.session = frida.attach(self.pid)
        self.session.on("detached", self._on_frida_detached)
        self.script = self.session.create_script(_frida_bridge_source())
        self.script.on("message", self._on_frida_message)
        try:
            self.script.load()
            if self._frida_script_errors:
                raise RuntimeError(self._frida_script_errors[-1])
            self.script.exports_sync.capabilities()
        except BaseException:
            self.close()
            raise
        self.mode = "frida"

    def _on_frida_detached(self, reason, crash=None):
        self._frida_detach_reason = reason
        self._frida_detached.set()

    def _on_frida_message(self, message, data):
        if message.get("type") == "error":
            self._frida_script_errors.append(message.get("description", "Frida script error"))

    def eval(self, expr: str, await_promise: bool = False):
        with self._request_lock:
            if self.mode == "frida":
                return self._eval_frida(expr, await_promise)
            return self._eval_websocket(expr, await_promise)

    def _eval_websocket(self, expr: str, await_promise: bool = False):
        self.seq += 1
        mid = self.seq
        self.ws.send(json.dumps({
            "id": mid,
            "method": "Runtime.evaluate",
            "params": {"expression": expr, "returnByValue": True,
                       "awaitPromise": await_promise},
        }))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") != mid:
                continue
            if "error" in msg:
                raise RuntimeError("CDP错误: " + str(msg["error"])[:300])
            r = msg.get("result", {})
            if "exceptionDetails" in r:
                d = r["exceptionDetails"]
                desc = ((d.get("exception") or {}).get("description")
                        or d.get("text") or "?")
                raise RuntimeError("JS错误: " + desc[:300])
            return r.get("result", {}).get("value")

    def _frida_call(self, source: str, timeout_ms: int = FRIDA_EVAL_TIMEOUT_MS) -> str:
        if self._frida_detached.is_set() or self.script is None:
            raise RuntimeError(f"游戏连接已结束：{self._frida_detach_reason or 'detached'}")
        try:
            return self.script.exports_sync.evaluate(source, int(timeout_ms))
        except Exception as exc:
            raise RuntimeError("Frida 执行失败：" + str(exc)[:300]) from exc

    @staticmethod
    def _frida_decode(raw):
        if not isinstance(raw, str):
            return raw
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return raw
        if isinstance(value, dict) and value.get(FRIDA_UNDEFINED):
            return None
        return value

    @staticmethod
    def _frida_encode_expr(expr: str) -> str:
        return """(() => {
          const encode = (value) => {
            if (typeof value === 'undefined') return JSON.stringify({__wog_undefined__: true});
            return JSON.stringify(value, (_key, item) =>
              typeof item === 'bigint' ? String(item) : item);
          };
          return encode((%s));
        })()""" % expr

    def _eval_frida(self, expr: str, await_promise: bool = False):
        if not await_promise:
            return self._frida_decode(self._frida_call(self._frida_encode_expr(expr)))

        self.seq += 1
        key = f"__wog_assistant_pending_{self.pid}_{self.seq}"
        encoded_key = json.dumps(key)
        setup = """(() => {
          const key = %s;
          const encode = (value) => {
            if (typeof value === 'undefined') return JSON.stringify({__wog_undefined__: true});
            return JSON.stringify(value, (_key, item) =>
              typeof item === 'bigint' ? String(item) : item);
          };
          globalThis[key] = {state: 'pending'};
          const finish = (state) => { globalThis[key] = state; };
          try {
            const value = (%s);
            Promise.resolve(value).then(
              (result) => finish({state: 'done', value: encode(result)}),
              (error) => finish({state: 'error', error: String(error && (error.stack || error.message) || error)})
            );
          } catch (error) {
            finish({state: 'error', error: String(error && (error.stack || error.message) || error)});
          }
          return 'pending';
        })()""" % (encoded_key, expr)
        self._frida_call(setup)
        poll = "JSON.stringify(globalThis[%s] || null)" % encoded_key
        deadline = time.monotonic() + FRIDA_ASYNC_TIMEOUT_SECONDS
        try:
            while time.monotonic() < deadline:
                state_raw = self._frida_call(poll)
                try:
                    state = json.loads(state_raw)
                except (TypeError, ValueError):
                    state = None
                if isinstance(state, dict):
                    if state.get("state") == "done":
                        return self._frida_decode(state.get("value"))
                    if state.get("state") == "error":
                        raise RuntimeError("JS错误: " + str(state.get("error", "未知错误"))[:300])
                time.sleep(0.03)
            raise TimeoutError("异步 JS 执行超过 120 秒")
        finally:
            try:
                self._frida_call("delete globalThis[%s]" % encoded_key)
            except Exception:
                pass

    def close(self) -> None:
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None
        if self.script is not None and not self._frida_detached.is_set():
            try:
                self.script.exports_sync.dispose()
            except Exception:
                pass
        if self.session is not None:
            try:
                self.session.detach()
            except Exception:
                pass
        self.script = None
        self.session = None


# =====================================================================
# JS 共享片段
# =====================================================================

JS_HELPERS = """
  const svcOf = (name) => {
    for (const [k, v] of nn.services._mapService.entries()) {
      if ((k.name || k.toString()) === name) return v;
    }
    return null;
  };
  // 统计、仓库分类和入库筛选只读物品定义，不访问 ServiceWorkshop。
  // 这条路径会在助手启动后自动执行；即使 getTableInfo 在某个客户端
  // 被实现成了带 UI 刷新的服务方法，也不能让它触碰铁匠页面状态。
  const dbInfoOf = (tid) => {
    try {
      const e = nn.db.equip.get(tid);
      const ct = Number(e && e.EquipType), rating = Number(e && e.RatingType);
      if (e && Number.isFinite(ct) && Number.isFinite(rating)) {
        const level = Number(e.BaseLimitLevel ?? e.LimitLevel ?? e.ReqUserLevel
                             ?? e.EquipLevel ?? e.LevelLimit) || 0;
        return {contentType: ct, ratingType: rating,
                baseLimitLevel: level};
      }
      const i = nn.db.item.get(tid);
      const itemRating = Number(i && i.RatingType);
      if (i && Number.isFinite(itemRating)) return {
        contentType: Number(i.ItemType) === 6 ? 20 : 0,
        ratingType: itemRating,
        baseLimitLevel: Number(i.BaseLimitLevel ?? i.LimitLevel
                               ?? i.ReqUserLevel ?? i.LevelLimit) || 0
      };
    } catch (e) {}
    return null;
  };
  const ctOf = (tid) => {
    const info = dbInfoOf(tid);
    return info ? info.contentType : 0;
  };
  const itemTypeOf = (tid) => {
    try { const d = nn.db.item.get(tid); return d ? Number(d.ItemType) : -1; } catch (e) { return -1; }
  };
  const catOf = (tid) => {
    const ct = ctOf(tid);
    if (ct === 20) return 'jewel';
    if (ct === 2) return 'accessory';
    if (ct === 1) return 'gear';
    if (itemTypeOf(tid) === 6) return 'jewel';
    if (itemTypeOf(tid) >= 1) return 'material';
    return 'other';
  };
  const depositKeyOf = (tid) => {
    const ct = ctOf(tid);
    if (ct === 20) return 6;
    if (ct === 2) return 7;
    if (ct === 1) return 4;
    return 1;
  };
  const tableInfoOf = (tid) => {
    return dbInfoOf(tid);
  };
  const ratingOf = (tid) => { const t = tableInfoOf(tid); return t ? Number(t.ratingType) : 0; };
  const limitLevelOf = (tid) => { const t = tableInfoOf(tid); return t ? Number(t.baseLimitLevel) : 0; };
  const nameOf = (tid) => {
    try {
      const info = nn.db.equip.get(tid) || nn.db.item.get(tid);
      const loc = nn.db.locale.get(info.ItemName);
      return (loc && (loc.CN || loc.CN_TW || loc.EN)) || info.ItemName || ('TID ' + tid);
    } catch (e) { return 'TID ' + tid; }
  };
  const marketNameOf = (tid) => {
    try {
      const info = nn.db.equip.get(tid) || nn.db.item.get(tid);
      const loc = nn.db.locale.get(info.ItemName);
      return (loc && (loc.EN || loc.CN || loc.CN_TW)) || info.ItemName || ('TID ' + tid);
    } catch (e) { return nameOf(tid); }
  };
  const embeddedItemsOf = (it) => {
    const out = [];
    const seen = new Set();
    const add = (value, index, hintedTid) => {
      if (!value && !hintedTid) return;
      const tid = (value && (value.itemTid ?? value.ItemTid ?? value.itemTID ?? value.tid ?? value.Tid))
                  ?? hintedTid;
      const iid = value && (value.itemId ?? value.ItemId ?? value.id ?? value.Id)
                  || (String(it.itemId) + ':gem:' + index);
      if (tid === undefined || tid === null || seen.has(String(iid))) return;
      const info = dbInfoOf(tid);
      if (!info || catOf(tid) !== 'jewel') return;
      seen.add(String(iid));
      out.push({itemId: String(iid), itemTid: String(tid), parentId: String(it.itemId)});
    };
    const visited = new Set();
    let index = 0;
    const walk = (value, depth, gemHint) => {
      if (value === null || value === undefined || depth > 4) return;
      if (typeof value === 'number' || typeof value === 'string') {
        if (gemHint) add(null, index++, value);
        return;
      }
      if (typeof value !== 'object' || visited.has(value)) return;
      visited.add(value);
      if (gemHint && !Array.isArray(value)) add(value, index++);
      for (const [key, child] of Object.entries(value)) {
        const hint = gemHint || /gem|jewel|socket|inlay/i.test(key);
        walk(child, depth + 1, hint);
      }
    };
    for (const [key, value] of Object.entries(it)) {
      try {
        walk(value, 0, /gem|jewel|socket|inlay/i.test(key));
      } catch (e) {}
    }
    return out;
  };
  const storageSlotCnt = () => Number(nn.db.config.gen.Storage_Slot_Cnt) || 0;
  const storagePages = () => {
    try { return Math.max(1, Number(nn.services.contentState.calcValue(125))); } catch (e) { return 1; }
  };
  const invSlots = () => {
    try { return Math.max(0, Number(nn.services.contentState.calcValue(124))); } catch (e) { return 0; }
  };
"""

# =====================================================================
# 安装：把 ServiceWorkshop 挂到 window 上（只读句柄）
# =====================================================================

INSTALL = """
(() => {
  let svc = null;
  for (const [k, v] of nn.services._mapService.entries()) {
    const name = k.name || k.toString();
    if (name === 'ServiceWorkshop') svc = v;
  }
  return svc ? 'ok' : 'nf';
})()
"""

# 端口开放后，游戏的 Puerts 全局和物品服务仍可能尚未建立。
# 探针只确认启动统计所需的物品缓存和静态数据库已经出现；不读取
# ServiceWorkshop，不读取融合页，也不调用任何会广播铁匠状态的接口。
RUNTIME_READY = """
(() => {
  try {
    if (typeof nn === 'undefined' || !nn || !nn.net || !nn.net.data) return false;
    const item = nn.net.data.item;
    if (!item || typeof item.getAllItemNotStack !== 'function') return false;
    if (!nn.db || !nn.db.item || typeof nn.db.item.get !== 'function') return false;
    if (!nn.db.equip || typeof nn.db.equip.get !== 'function') return false;
    if (!nn.db || !nn.db.fusion || typeof nn.db.fusion.getListByContentType !== 'function') return false;
    return true;
  } catch (e) {
    return false;
  }
})()
"""

# 只读当前关卡探针。不同游戏版本把关卡状态挂在不同管理器上，先尝试
# 常见字段，再从当前会话对象的浅层字段中寻找 stage/area/chapter 组合。
# 不调用切关卡、战斗或 ServiceWorkshop 方法。
CURRENT_STAGE = """
(() => {
  try {
    const roots = [
      nn.net && nn.net.manager && nn.net.manager.gameSession,
      nn.net && nn.net.data,
      nn.userData,
      nn.gameData
    ].filter(Boolean);
    const pick = (o, names) => {
      for (const n of names) {
        try { if (o && o[n] !== undefined && o[n] !== null) return o[n]; } catch (e) {}
      }
      return null;
    };
    const text = (v) => {
      if (v === undefined || v === null) return '';
      if (typeof v === 'string') return v.trim();
      if (typeof v === 'number' && isFinite(v)) return String(v);
      return '';
    };
    const nameKeys = ['currentStageName','playingStageName','stageName','stageTitle','currentStage','playingStage'];
    const idKeys = ['playingStageTid','currentStageTid','stageTid','currentStageId','stageId'];
    for (const root of roots) {
      const direct = text(pick(root, nameKeys));
      if (direct && direct.length < 80) return JSON.stringify(direct);
      for (const key of idKeys) {
        const value = pick(root, [key]);
        if (value && typeof value === 'object') {
          const nested = text(pick(value, nameKeys.concat(['name','title','displayName'])));
          if (nested && nested.length < 80) return JSON.stringify(nested);
        }
      }
    }
    for (const root of roots) {
      const area = pick(root, ['areaType','currentAreaType','playingAreaType','areaId']);
      const type = pick(root, ['stageType','currentStageType','playingStageType','chapter']);
      const step = pick(root, ['stageStep','currentStageStep','playingStageStep','stageNo','step']);
      if (Number(area) > 0 && Number(type) > 0 && Number(step) > 0) {
        const areaName = ({1:'普通',2:'地狱',3:'噩梦',4:'深渊',5:'炼狱'})[Number(area)] || ('区域' + Number(area));
        return JSON.stringify(areaName + Number(type) + '-' + Number(step));
      }
    }
    return JSON.stringify('当前关卡');
  } catch (e) { return JSON.stringify('当前关卡'); }
})()
"""
# =====================================================================
# JS 流程：仓库信息 / 计数探测
# =====================================================================

STORAGE_INFO = """
(() => {
""" + JS_HELPERS + """
  const all = nn.net.data.item.getAllItemNotStack();
  const stored = all.filter(i => i.location === 2);
  const inv = all.filter(i => i.location === 1);
  const slotCnt = storageSlotCnt();
  const pages = storagePages();
  const byCat = {};
  for (const it of stored) {
    const c = catOf(it.itemTid);
    byCat[c] = (byCat[c] || 0) + 1;
  }
  let maxDiaPages = 0;
  try { maxDiaPages = Number(nn.db.config.gen.Max_Dia_Storage) || 0; } catch (e) {}
  const lockInStorage = stored.filter(i => i.isLock).length;
  return JSON.stringify({
    slotCnt: slotCnt,
    pages: pages,
    invSlots: invSlots(),
    storageUsed: stored.length,
    storageTotal: pages * slotCnt,
    storageFree: Math.max(0, pages * slotCnt - stored.length),
    inventoryUsed: inv.length,
    maxDiaPages: maxDiaPages,
    lockedInStorage: lockInStorage,
    byCategory: byCat,
    ok: slotCnt > 0
  });
})()
"""

# ---------------------------------------------------------------------
# 只读合成探测共享片段：复刻 collectAutoRegisterCandidates 的候选池规则和
# canStageFusionItem 的有效性判定，但完全不碰 workshop 全局状态——
# ServiceWorkshop 维护当前合成页/暂存区状态，后台统计不能依赖它。
# 这里使用数据库融合表和物品缓存重建候选池：池子=背包(location 1)必收，
# 仓库(location 2)按需；只有真正执行合成时才访问工作台服务。
JS_FUSE_PROBE_HELPERS = """
  const fusionTablesOf = (contentType) => {
    try { return nn.db.fusion.getListByContentType(contentType) || []; }
    catch (e) { return []; }
  };
  const findPureFusionTable = (contentType, rating, level) => {
    let best = null;
    for (const t of fusionTablesOf(contentType)) {
      if (Number(t.MaterialRating) !== Number(rating)) continue;
      if (Number(t.MaterialLevelLimit) < Number(level)) continue;
      if (!best || Number(t.MaterialLevelLimit) < Number(best.MaterialLevelLimit)) best = t;
    }
    return best;
  };
  const fuseProbeCollect = (contentType, includeStorage, tier) => {
    const itemMove = nn.services && nn.services.itemMove;
    let market = null;
    try { market = nn.services.steamMarket && nn.services.steamMarket.staging; } catch (e) {}
    const found = [];
    for (const it of nn.net.data.item.getAllItemNotStack()) {
      try {
        if (it.location !== 1 && !(includeStorage && it.location === 2)) continue;
        // 等价于 isValidFusionMaterial 的检查：未锁定、未穿戴、未上架、未暂存
        if (it.isLock) continue;
        // itemMove 在登录早期或部分版本中可能尚未注册；缺少这个可选
        // 判断时保留物品，后续合成流程仍会由工作台再次校验。
        if (itemMove && typeof itemMove.isEquippedItemId === 'function'
            && itemMove.isEquippedItemId(it.itemId)) continue;
        try { if (market && market.isStaged(it.itemId)) continue; } catch (e) {}
        const info = dbInfoOf(it.itemTid);
        if (!info || info.contentType !== contentType || info.ratingType !== tier) continue;
        if (!findPureFusionTable(contentType, tier, info.baseLimitLevel)) continue;
        found.push(info);
      } catch (e) {}
    }
    return found;
  };
"""

# 只读经济快照：不访问 ServiceWorkshop，不切换页面，不修改游戏状态。
# itemMove 只用于判断物品是否已装备，Steam 价格匹配在 Python 侧完成。
ECONOMIC_SNAPSHOT = """
(() => {
""" + JS_HELPERS + """
  const all = nn.net.data.item.getAllItemNotStack();
  const mv = nn.services && nn.services.itemMove;
  const out = [];
  for (const it of all) {
    try {
      const info = dbInfoOf(it.itemTid);
      const category = catOf(it.itemTid);
      let equipped = false;
      try { equipped = !!(mv && mv.isEquippedItemId && mv.isEquippedItemId(it.itemId)); } catch (e) {}
      const quantity = Number(it.count ?? it.num ?? it.amount ?? it.stackCount ?? 1) || 1;
      out.push({
        itemId: String(it.itemId), itemTid: String(it.itemTid),
        name: nameOf(it.itemTid), market_name: marketNameOf(it.itemTid), category: category,
        tier: info ? Number(info.ratingType) || 0 : 0,
        level: info ? Number(info.baseLimitLevel) || 0 : 0,
        location: Number(it.location) || 0, slot: Number(it.slotIdx) || 0,
        equipped: equipped, quantity: Math.max(1, quantity)
      });
      for (const gem of embeddedItemsOf(it)) {
        const gInfo = dbInfoOf(gem.itemTid);
        out.push({
          itemId: gem.itemId, itemTid: gem.itemTid, parentId: gem.parentId,
          name: nameOf(gem.itemTid), market_name: marketNameOf(gem.itemTid),
          category: 'jewel', tier: gInfo ? Number(gInfo.ratingType) || 0 : 0,
          level: gInfo ? Number(gInfo.baseLimitLevel) || 0 : 0,
          location: Number(it.location) || 0, slot: Number(it.slotIdx) || 0,
          equipped: equipped, embedded: true, quantity: 1
        });
      }
    } catch (e) {}
  }
  try {
    for (const stack of nn.net.data.item.getAllItemStack()) {
      try {
        const tid = stack.itemTid ?? stack.ItemTid ?? stack.itemTID ?? stack.tid;
        if (tid === undefined || tid === null) continue;
        const location = Number(stack.location) || 0;
        if (location !== 1 && location !== 2) continue;
        const info = dbInfoOf(tid);
        const count = Number(stack.count ?? stack.num ?? stack.amount ?? stack.stackCount ?? 1) || 1;
        out.push({
          itemId: 'stack:' + String(stack.itemId ?? tid) + ':' + String(stack.location || 0)
                  + ':' + String(stack.slotIdx || 0),
          itemTid: String(tid), name: nameOf(tid), market_name: marketNameOf(tid),
          category: catOf(tid), tier: info ? Number(info.ratingType) || 0 : 0,
          level: info ? Number(info.baseLimitLevel) || 0 : 0,
          location: Number(stack.location) || 0, slot: Number(stack.slotIdx) || 0,
          equipped: false, embedded: false, quantity: Math.max(1, count)
        });
      } catch (e) {}
    }
  } catch (e) {}
  return JSON.stringify(out);
})()
"""

COUNT_PROBE = """
((includeStorage) => {
""" + JS_HELPERS + JS_FUSE_PROBE_HELPERS + """
  const out = {};
  for (const tier of [1, 2, 3, 4, 5, 6]) {
    try {
      out['tier' + tier] = fuseProbeCollect(20, includeStorage, tier).length;
    } catch (e) {
      out['tier' + tier] = -1;
    }
  }
  return JSON.stringify(out);
})
"""

# 装备/饰品计数：按 content type 参数化，件数取自融合表（装备 6、饰品 3）
EQUIP_COUNT_PROBE = """
((contentType, includeStorage) => {
""" + JS_HELPERS + JS_FUSE_PROBE_HELPERS + """
  const out = {};
  for (const tier of [1, 2, 3, 4, 5, 6]) {
    try {
      const cands = fuseProbeCollect(contentType, includeStorage, tier);
      const byLevel = {};
      for (const c of cands) {
        const lv = Number(c.baseLimitLevel) || 0;
        byLevel[lv] = (byLevel[lv] || 0) + 1;
      }
      const plan = {};
      for (const lv of Object.keys(byLevel)) {
        let need = 0, rate = 0;
        const t = findPureFusionTable(contentType, tier, Number(lv));
        if (t) { need = Number(t.MaterialRatingCnt) || 0; rate = Number(t.FusionRate) || 0; }
        plan[lv] = {
          have: byLevel[lv],
          need: need,
          rate: rate,
          batches: need > 0 ? Math.floor(byLevel[lv] / need) : 0
        };
      }
      let batches = 0;
      let bestLevel = 0, bestHave = 0;
      for (const lv of Object.keys(plan)) {
        batches += plan[lv].batches;
        if (plan[lv].have > bestHave) { bestHave = plan[lv].have; bestLevel = Number(lv); }
      }
      // 允许混级时的批数：用最高等级的表（该表接受所有等级 <= 它的材料）
      let topLevel = 0;
      for (const lv of Object.keys(byLevel)) topLevel = Math.max(topLevel, Number(lv));
      const topTable = findPureFusionTable(contentType, tier, topLevel);
      const topNeed = topTable ? (Number(topTable.MaterialRatingCnt) || 0) : 0;
      const topRate = topTable ? (Number(topTable.FusionRate) || 0) : 0;
      const mixedBatches = topNeed > 0 ? Math.floor(cands.length / topNeed) : 0;
      // 每批要几件（同等级/混合都是同一套表的 MaterialRatingCnt）
      let need = topNeed;
      if (need <= 0) {
        for (const lv of Object.keys(plan)) { if (plan[lv].need > 0) { need = plan[lv].need; break; } }
      }
      out['tier' + tier] = {
        total: cands.length,
        byLevel: byLevel,
        plan: plan,
        batches: batches,
        mixedBatches: mixedBatches,
        topLevel: topLevel,
        topRate: topRate,
        need: need,
        bestLevel: bestLevel,
        bestHave: bestHave
      };
    } catch (e) {
      out['tier' + tier] = { total: -1, byLevel: {}, plan: {}, batches: 0,
                             mixedBatches: 0, need: 0, topLevel: 0, topRate: 0,
                             err: String(e.message || e).slice(0, 80) };
    }
  }
  return JSON.stringify(out);
})
"""

# =====================================================================
# JS 流程：合成
# =====================================================================

# 宝玉合成：6 颗同品级 → 1 颗下一品级（融合表 MaterialRatingCnt = 6）
FUSE_FLOW = """
(async (tiers, includeStorage) => {
""" + JS_HELPERS + """
  const ws = nn.services.workshop;
  const report = [];
  // 只有真正要合成才允许动全局状态；记住用户停在哪个合成页，收尾恢复，
  // 别让人家的页面停在宝玉类型上、暂存区里还留着上一批材料
  // 不同登录时机下 fusionContentType 可能尚未由 UI 初始化。
  // 不能把 undefined/非法值写回，否则 ServiceWorkshop 会刷新成空白合成页。
  let savedCT = null;
  try {
    const v = Number(ws.fusionContentType);
    if ([1, 2, 20].includes(v)) savedCT = v;
  } catch (e) {}
  const setFusionTypeIfNeeded = (ct) => {
    let current = null;
    try { current = Number(ws.fusionContentType); } catch (e) {}
    if (current !== ct) ws.setFusionContentType(ct);
  };
  try {
    for (const tier of tiers) {
      try {
        setFusionTypeIfNeeded(20);
      const cands = ws.collectAutoRegisterCandidates(
        'Fusion',
        (itemTid, itemId) => !ws.isFusionStaged(itemId) && ws.canStageFusionItem(itemTid, itemId),
        includeStorage,
        { rating: tier }
      ).filter(c => c.ratingType === tier);
      if (cands.length < 6) {
        report.push('Tier' + tier + ': ' + cands.length + ' 颗，不足 6，跳过');
        continue;
      }
      // 按等级分组：融合表是按 baseLimitLevel 挑的，混等级的 6 颗会填不满槽，
      // 而候选顺序每轮都一样，一旦卡住就会无限重复“填槽失败”。
      const byLevel = new Map();
      for (const c of cands) {
        const lv = Number(c.baseLimitLevel) || 0;
        if (!byLevel.has(lv)) byLevel.set(lv, []);
        byLevel.get(lv).push(c);
      }
      let fused = 0;
      for (const [lv, pool] of byLevel.entries()) {
        if (pool.length < 6) {
          report.push('Tier' + tier + '/Lv' + lv + ': ' + pool.length + ' 颗，不足 6，跳过');
          continue;
        }
        let done = 0, guard = 0;
        while (pool.length >= 6 && guard < 60) {
          guard += 1;
          const batch = pool.splice(0, 6);
          const table = ws.findFusionTable(20, tier, Number(batch[0].baseLimitLevel) || 0)
                     || ws.resolveFusionTableByItem(batch[0].itemTid, batch[0].itemId);
          if (!table) {
            report.push('Tier' + tier + '/Lv' + lv + ': 未找到融合表，停止');
            break;
          }
          ws.clearFusionStaging();
          ws.lockFusionTable(table);
          ws.fillFusionSlots(table, batch);
          if (!ws.isFusionStagingFull()) {
            report.push('Tier' + tier + '/Lv' + lv + ': 填槽失败，停止');
            ws.clearFusionStaging();
            break;
          }
          const res = await ws.reqFusionStagedAsync();
          if (!res || res.length === 0) {
            report.push('Tier' + tier + '/Lv' + lv + ': 合成请求失败，停止');
            // 游戏的 reqFusionStagedAsync 失败时不清暂存（锁表+满槽会一直留着），
            // 后续候选收集会被这张卡死的表限制住，必须立刻清掉
            ws.clearFusionStaging();
            break;
          }
          fused += 1;
          let name = 'TID ' + (res[0] && res[0].tid);
          try { if (res[0] && res[0].tid) name = nameOf(res[0].tid); } catch (e) {}
          report.push('Tier' + tier + '/Lv' + lv + ' 合成出 → ' + name);
        }
      }
      report.push('Tier' + tier + ' 共合成 ' + fused + ' 次');
      } catch (e) {
        report.push('Tier' + tier + ' 错误: ' + String(e.message || e).slice(0, 120));
      }
    }
  } finally {
    try { ws.clearFusionStaging(); } catch (e) {}
    if (savedCT !== null) {
      try { setFusionTypeIfNeeded(savedCT); } catch (e) {}
    }
  }
  return report;
})
"""

# 装备/饰品合成：按融合表驱动
#   - 件数取 table.MaterialRatingCnt（装备 6、饰品 3），不再写死
#   - 等级取候选自带的 baseLimitLevel（游戏自己的 getTableInfo 结果），
#     不再依赖名字已经改掉的 ServiceEquip
#   - 概率合成：reqFusionStagedAsync 成功但可能“没出货”，用 _isLastFusionSuccess 区分
EQUIP_FUSE_FLOW = """
(async (contentType, tiers, includeStorage, sameLevel, maxBatches) => {
""" + JS_HELPERS + """
  const ws = nn.services.workshop;
  const report = [];
  const label = contentType === 2 ? '饰品' : '装备';
  // 与宝玉合成同理：完事把用户的合成页恢复原状（见 FUSE_FLOW 注释）
  // 不同登录时机下 fusionContentType 可能尚未由 UI 初始化。
  // 不能把 undefined/非法值写回，否则 ServiceWorkshop 会刷新成空白合成页。
  let savedCT = null;
  try {
    const v = Number(ws.fusionContentType);
    if ([1, 2, 20].includes(v)) savedCT = v;
  } catch (e) {}
  const setFusionTypeIfNeeded = (ct) => {
    let current = null;
    try { current = Number(ws.fusionContentType); } catch (e) {}
    if (current !== ct) ws.setFusionContentType(ct);
  };
  try {
    for (const tier of tiers) {
      try {
        setFusionTypeIfNeeded(contentType);
      const cands = ws.collectAutoRegisterCandidates(
        'Fusion',
        (itemTid, itemId) => !ws.isFusionStaged(itemId) && ws.canStageFusionItem(itemTid, itemId),
        includeStorage,
        { rating: tier }
      ).filter(c => c.ratingType === tier);
      if (cands.length === 0) {
        report.push(label + ' T' + tier + ': 无可用材料');
        continue;
      }
      const groups = new Map();
      if (sameLevel) {
        for (const c of cands) {
          const lv = Number(c.baseLimitLevel) || 0;
          if (!groups.has(lv)) groups.set(lv, []);
          groups.get(lv).push(c);
        }
      } else {
        groups.set(-1, cands);
      }
      let fused = 0, okCnt = 0, failCnt = 0;
      for (const [lv, arr] of groups.entries()) {
        const tag = (lv >= 0 ? 'Lv' + lv : '混级');
        let batches = [];
        if (sameLevel) {
          let table = null;
          for (const c of arr) {
            table = ws.findFusionTable(contentType, tier, Number(c.baseLimitLevel) || 0);
            if (table) break;
          }
          if (!table) { report.push(label + ' T' + tier + '/' + tag + ': 未找到融合表，跳过'); continue; }
          const need = Number(table.MaterialRatingCnt) || 0;
          if (need <= 0) { report.push(label + ' T' + tier + '/' + tag + ': 融合表件数为 0，跳过'); continue; }
          if (arr.length < need) {
            report.push(label + ' T' + tier + '/' + tag + ': ' + arr.length + ' 件 < ' + need + '，跳过');
            continue;
          }
          let n = Math.floor(arr.length / need);
          if (maxBatches > 0 && n > maxBatches) n = maxBatches;
          for (let i = 0; i < n; i++) batches.push(arr.slice(i * need, i * need + need));
        } else {
          // 混级：每批用这批里最高的等级去挑表，填不满就跳过该批
          let cursor = 0;
          while (cursor < arr.length) {
            const rest = arr.slice(cursor);
            let top = 0;
            for (const c of rest) top = Math.max(top, Number(c.baseLimitLevel) || 0);
            const table = ws.findFusionTable(contentType, tier, top);
            const need = table ? (Number(table.MaterialRatingCnt) || 0) : 0;
            if (need <= 0 || rest.length < need) break;
            batches.push(rest.slice(0, need));
            cursor += need;
            if (maxBatches > 0 && batches.length >= maxBatches) break;
          }
        }
        for (let i = 0; i < batches.length; i++) {
          const batch = batches[i];
          let table = null;
          let top = 0;
          for (const c of batch) top = Math.max(top, Number(c.baseLimitLevel) || 0);
          table = ws.findFusionTable(contentType, tier, top);
          if (!table) { report.push(label + ' T' + tier + '/' + tag + ': 未找到融合表，停止'); break; }
          ws.clearFusionStaging();
          ws.lockFusionTable(table);
          ws.fillFusionSlots(table, batch);
          if (!ws.isFusionStagingFull()) {
            report.push(label + ' T' + tier + '/' + tag + ': 填槽失败，停止');
            ws.clearFusionStaging();
            break;
          }
          const res = await ws.reqFusionStagedAsync();
          if (!res || res.length === 0) {
            report.push(label + ' T' + tier + '/' + tag + ': 合成请求失败，停止');
            // 失败时游戏不清暂存（见 FUSE_FLOW 同款注释），立刻清掉再停
            ws.clearFusionStaging();
            break;
          }
          fused += 1;
          let ok = true;
          try { ok = !!ws._isLastFusionSuccess; } catch (e) {}
          if (ok) okCnt += 1; else failCnt += 1;
          let name = 'TID ' + (res[0] && res[0].tid);
          try { if (res[0] && res[0].tid) name = nameOf(res[0].tid); } catch (e) {}
          report.push(label + ' T' + tier + '/' + tag + ' #' + (i + 1) + ' → ' + name
                      + (ok ? '' : ' (未出货)'));
        }
      }
      report.push(label + ' T' + tier + ' 共 ' + fused + ' 次（出货 ' + okCnt + ' / 未出货 ' + failCnt + '）');
      } catch (e) {
        report.push(label + ' T' + tier + ' 错误: ' + String(e.message || e).slice(0, 120));
      }
    }
  } finally {
    try { ws.clearFusionStaging(); } catch (e) {}
    if (savedCT !== null) {
      try { setFusionTypeIfNeeded(savedCT); } catch (e) {}
    }
  }
  return report;
})
"""

# =====================================================================
# JS 流程：入库
# =====================================================================

# 入库：直接按物品自身类型分类（getContentTypeByTid + ItemType），
# 容量用 Final_Storage_Page × Storage_Slot_Cnt 算出来——
# 旧实现调用的 nn.net.data.item.getStorageTotal() 在游戏里根本不存在，
# 所以它一直悄悄退回硬编码 300。
DEPOSIT_FLOW = """
(async (typeKeys, allowedTiers, excludeLocked, roundLimit, pagesOverride) => {
""" + JS_HELPERS + """
  const itemData = nn.net.data.item;
  const gs = nn.net.manager.gameSession;
  const mv = nn.services.itemMove;
  const slotCnt = storageSlotCnt();
  const pages = (Number(pagesOverride) > 0) ? Number(pagesOverride) : storagePages();
  const capacity = pages * slotCnt;

  const want = new Set(typeKeys.map(Number));
  const tiers = new Set(allowedTiers.map(Number));
  const rounds = [];
  const usableIds = () => new Set(
    itemData.getAllItemNotStack().filter(i => i.location === 2).map(i => String(i.itemId)));

  // 这件物品要不要入库。挑选和最后的“到底匹配到几件”共用同一条判断。
  const wants = (it) => {
    if (it.location !== 1) return false;
    if (excludeLocked && it.isLock) return false;
    try { if (mv.isEquippedItemId(it.itemId)) return false; } catch (e) {}
    const key = depositKeyOf(it.itemTid);
    if (!want.has(key)) return false;
    // 材料类不按品级筛（沿用旧行为：材料勾选后全收）
    if (key !== 1) {
      const rating = ratingOf(it.itemTid);
      if (tiers.size > 0 && !tiers.has(rating)) return false;
    }
    return true;
  };

  let blocked = 0;

  // 每种勾选类型在开始前匹配到几件。可堆叠材料（金币/经验/钻石之类）压根没有
  // location，不在仓库容器里，所以永远匹配不到——面板靠这个数字把
  // “勾了材料却什么都没发生”讲清楚，而不是静默无视。
  const matchedByKey = {};
  for (const it of itemData.getAllItemNotStack()) {
    if (!wants(it)) continue;
    const k = depositKeyOf(it.itemTid);
    matchedByKey[k] = (matchedByKey[k] || 0) + 1;
  }

  for (let round = 0; round < roundLimit; round++) {
    const all = itemData.getAllItemNotStack();
    const stored = all.filter(i => i.location === 2).length;
    const free = Math.max(0, capacity - stored);
    if (free <= 0) break;
    const picked = [];
    for (const it of all) {
      if (!wants(it)) continue;
      picked.push(it.itemId);
      if (picked.length >= 50) break;
    }
    if (picked.length === 0) break;
    const chunk = picked.slice(0, free);
    blocked += picked.length - chunk.length;
    if (chunk.length === 0) break;
    const before = usableIds();
    await gs.reqStorageMoveInList(chunk);
    // 不信返回码，按“这件是不是真的从背包里挪走了”核对
    const after = usableIds();
    let movedNow = 0;
    for (const id of new Set(chunk.map(String))) {
      // 入库是“背包 → 仓库”，所以判据是：入库前不在仓库、入库后在仓库
      if (!before.has(id) && after.has(id)) movedNow += 1;
    }
    rounds.push(movedNow);
    if (movedNow === 0) break;   // 一件都没动，再循环也是白跑
  }

  const finalAll = itemData.getAllItemNotStack();
  const used = finalAll.filter(i => i.location === 2).length;
  const deposited = rounds.reduce((a, b) => a + b, 0);
  let stackMaterialCount = 0;
  try { stackMaterialCount = nn.net.data.item.getAllItemStack().length; } catch (e) {}

  return JSON.stringify({
    deposited: deposited,
    storageUsed: used,
    storageTotal: capacity,
    storageFree: Math.max(0, capacity - used),
    pages: pages,
    slotCnt: slotCnt,
    blocked: blocked,
    full: capacity > 0 && used >= capacity,
    rounds: rounds,
    matchedByKey: matchedByKey,
    stackMaterialCount: stackMaterialCount
  });
})
"""

# =====================================================================
# JS 流程：仓库整理
# =====================================================================

# 按“每页一个分类”重排仓库。
# 槽位是扁平的：第 p 页 = [p*slotCnt, (p+1)*slotCnt)。
# 页内移动走 gameSession.reqStorageMove(itemId, toSlotIdx)。
#
# 先做一次纯模拟算出安全的执行顺序（只往“当前空着”的格子搬，搬不动就
# 把一件先挪到空位当缓冲），模拟过了才真正下发，避免把物品搬进别人的格子里。
ORGANIZE_FLOW = """
(async (pageCats, opts) => {
""" + JS_HELPERS + """
  const gs = nn.net.manager.gameSession;
  const report = [];
  const dryRun = !!(opts && opts.dryRun);
  const includeLocked = !!(opts && opts.includeLocked);
  const sortAfter = !!(opts && opts.sortAfter);

  const slotCnt = storageSlotCnt();
  if (slotCnt <= 0) return JSON.stringify({ ok: false, error: 'Storage_Slot_Cnt 读取失败' });
  const pages = (opts && Number(opts.pagesOverride) > 0) ? Number(opts.pagesOverride) : storagePages();
  const capacity = pages * slotCnt;

  // 页面 -> 分类；未指定的页当“不限”，用来放没配页的分类
  const pagesCat = [];
  const ownerPage = {};
  for (let p = 0; p < pages; p++) {
    const raw = (pageCats && (pageCats[String(p)] || pageCats[p])) || 'any';
    const c = raw === 'any' ? 'any' : raw;
    pagesCat[p] = c;
    if (c !== 'any' && ownerPage[c] === undefined) ownerPage[c] = p;
  }
  const anyPages = [];
  for (let p = 0; p < pages; p++) if (pagesCat[p] === 'any') anyPages.push(p);

  const itemList = nn.net.data.item.getAllItemNotStack().filter(i => i.location === 2);
  const slotOf = {};
  for (const it of itemList) slotOf[String(it.itemId)] = it.slotIdx;
  const items = itemList.slice().sort((a, b) => a.slotIdx - b.slotIdx);

  // 锁定物品：includeLocked=false 时原地不动，占住的格子也不能当目标
  const pinnedSlots = new Set();
  const work = [];
  for (const it of items) {
    if (!includeLocked && it.isLock) pinnedSlots.add(it.slotIdx);
    else work.push(it);
  }

  const byCat = {};
  for (const it of work) {
    const c = catOf(it.itemTid);
    if (!byCat[c]) byCat[c] = [];
    byCat[c].push(it);
  }

  // —— 放置：本来就待在正确页面的物品保留原格子，只给真正要搬的分配空位 ——
  // 这样整理是“最小移动”，跑第二遍不会再动任何东西。
  const pageOfSlot = (s) => Math.floor(s / slotCnt);
  let target = null, overflowIds = [];
  let converged = false;
  for (let pass = 0; pass < 12; pass++) {
    const reserved = new Set(pinnedSlots);
    for (const id of overflowIds) {
      const s = slotOf[id];
      if (s !== undefined) reserved.add(s);
    }
    // 每页的成员：有指定分类的按分类归属，没配页的分类依次填“不限”页
    const members = new Array(pages);
    for (let p = 0; p < pages; p++) members[p] = [];
    const floating = [];
    for (const cat of Object.keys(byCat)) {
      const p = ownerPage[cat];
      if (p === undefined) { for (const it of byCat[cat]) floating.push(it); }
      else { for (const it of byCat[cat]) members[p].push(it); }
    }
    for (const it of floating) {
      let done = false;
      for (const p of anyPages) {
        if (members[p].length < slotCnt) { members[p].push(it); done = true; break; }
      }
      if (!done) members[0].push(it);
    }

    const place = {};
    const overflow = [];
    for (let p = 0; p < pages; p++) {
      const stay = [], move = [];
      for (const it of members[p]) {
        if (pageOfSlot(it.slotIdx) === p && !reserved.has(it.slotIdx)) stay.push(it);
        else move.push(it);
      }
      const taken = new Set();
      for (const it of stay) { place[it.itemId] = it.slotIdx; taken.add(it.slotIdx); }
      for (let k = 0; k < slotCnt && move.length > 0; k++) {
        const s = p * slotCnt + k;
        if (taken.has(s) || reserved.has(s)) continue;
        const it = move.shift();
        place[it.itemId] = s;
        taken.add(s);
      }
      for (const it of move) overflow.push(it.itemId);
    }
    target = place;
    // 用集合比较判断收敛（不是比数量）：reserved 只增不减，
    // 所以溢出集合一旦不再变化，这一趟算出来的 place 就是自洽的。
    const prev = overflowIds.slice().sort().join(',');
    overflowIds = overflow.slice();
    if (overflowIds.slice().sort().join(',') === prev) { converged = true; break; }
  }

  // 没收敛说明“某页装不下自己的分类”这类矛盾没解开：宁可不整理，
  // 也不要拿一份自相矛盾的计划去搬东西。
  if (!converged) {
    return JSON.stringify({ ok: false, error: '放置方案未收敛（分类容量不够），已放弃整理',
                            pages: pages, capacity: capacity, items: items.length });
  }

  const moves = [];
  for (const it of items) {
    const to = target[it.itemId];
    if (to === undefined || to === it.slotIdx) continue;
    moves.push({ itemId: it.itemId, from: it.slotIdx, to: to });
  }

  // —— 排序：只往当前空的格子搬；卡住时拿一个空位当缓冲区 ——
  const occupancy = new Map();
  for (const it of items) occupancy.set(it.slotIdx, it.itemId);
  const isFree = (s) => {
    const cur = occupancy.get(s);
    return cur === undefined || cur === null;
  };
  const plannedTargets = new Set(moves.map(m => m.to));
  // 拷贝一份，后面的缓冲区调度会改写 from，不能污染真正的计划
  const queue = moves.map(m => ({ itemId: m.itemId, from: m.from, to: m.to }));
  const order = [];
  // 缓冲区调度最多允许多搬 4 倍件数：正常换位只会多几步，
  // 一旦超出说明有件挪不动，宁可整份作废也不要在仓库里反复弹同一个物品。
  const stepBudget = moves.length * 4 + 32;
  let aborted = '';
  while (queue.length > 0) {
    if (order.length > stepBudget) {
      aborted = '需要的中转步数异常（>' + stepBudget + '），已放弃整理以免反复搬动';
      break;
    }
    let progressed = false;
    for (let i = 0; i < queue.length; i++) {
      const m = queue[i];
      if (m.to === m.from || isFree(m.to)) {
        occupancy.delete(m.from);
        occupancy.set(m.to, m.itemId);
        order.push(m);
        queue.splice(i, 1);
        i -= 1;
        progressed = true;
      }
    }
    if (progressed) continue;
    // 互相占着对方的格子：把队首那件先挪到一个“谁都不需要”的空位当缓冲
    let scratch = -1;
    for (let s = 0; s < capacity; s++) {
      if (isFree(s) && !plannedTargets.has(s)) { scratch = s; break; }
    }
    if (scratch < 0) {
      for (let s = 0; s < capacity; s++) {
        if (isFree(s) && s !== queue[0].to) { scratch = s; break; }
      }
    }
    if (scratch < 0) {
      aborted = '仓库没有空位，剩余 ' + queue.length + ' 件无法整理（先腾点空间）';
      break;
    }
    const m = queue[0];
    occupancy.delete(m.from);
    occupancy.set(scratch, m.itemId);
    order.push({ itemId: m.itemId, from: m.from, to: scratch });
    m.from = scratch;
  }

  // 有件没排上（或被放弃）就整份作废：计划照搬下去只会搬一半。
  if (aborted || queue.length > 0) {
    return JSON.stringify({ ok: false,
                            error: aborted || ('还有 ' + queue.length + ' 件安排不下，已放弃整理'),
                            pages: pages, capacity: capacity, items: items.length,
                            unplaced: queue.length });
  }

  const slotCat = {};
  for (const it of items) slotCat[it.slotIdx] = catOf(it.itemTid);

  const summary = {
    ok: true,
    dryRun: dryRun,
    pages: pages,
    slotCnt: slotCnt,
    capacity: capacity,
    items: items.length,
    planned: moves.length,
    steps: order.length,
    overflow: overflowIds.length,
    moved: 0,
    failed: 0,
    pageCats: pagesCat,
    distribution: {}
  };

  if (dryRun) {
    summary.preview = order.slice(0, 60).map(m => m.from + ' → ' + m.to);
    summary.plan = moves.map(m => m.from + '→' + m.to + '(' + (slotCat[m.from] || '?') + ')');
    summary.layout = slotCat;
    return JSON.stringify(summary);
  }

  // 下发搬迁。reqStorageMove 返回码的含义随版本会变，所以不拿返回码判定成败，
  // 最后按“物品是否真的落在目标格”核对；顺带把返回码原样带出来便于排查。
  let errors = 0;
  for (const m of order) {
    try {
      const res = await gs.reqStorageMove(m.itemId, m.to);
      if (res && res.NetResult !== undefined) summary.lastNetResult = String(res.NetResult);
    } catch (e) {
      errors += 1;
    }
  }
  summary.errors = errors;

  // 先核对“有没有搬到位”，再调游戏排序。
  // 反过来的话排序会把所有位置打乱，核对就全成了“没到位”。
  let posOf = {};
  let dist = {};
  const snapshot = () => {
    posOf = {};
    dist = {};
    for (let p = 0; p < pages; p++) dist['page' + p] = 0;
    for (const it of nn.net.data.item.getAllItemNotStack()) {
      if (it.location !== 2) continue;
      posOf[String(it.itemId)] = it.slotIdx;
      const p = Math.floor(it.slotIdx / slotCnt);
      dist['page' + p] = (dist['page' + p] || 0) + 1;
    }
  };
  snapshot();
  const verifiedPos = Object.assign({}, posOf);
  for (const m of moves) {
    if (verifiedPos[String(m.itemId)] === m.to) summary.moved += 1;
    else summary.failed += 1;
  }
  summary.distribution = dist;

  // 排序放在核对之后，排完再取一次真实分布
  if (sortAfter) {
    for (let p = 0; p < pages; p++) {
      try { await gs.reqStorageSort(p); } catch (e) {}
    }
    snapshot();
    summary.distribution = dist;
    summary.sorted = true;
  }
  return JSON.stringify(summary);
})
"""


# =====================================================================
# 表达式构建：把参数拼成一条可直接 eval 的字符串
# =====================================================================

def _call(flow: str, *args) -> str:
    """把 (flow)(args...) 包成一条表达式；flow 形如 "\\n(async (...) => {...})\\n"。"""
    payload = ", ".join(json.dumps(a) if not isinstance(a, str) else a for a in args)
    return "(" + flow.strip() + ")(" + payload + ")"


def _js(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def js_install() -> str:
    return INSTALL


def js_runtime_ready() -> str:
    return RUNTIME_READY


def js_current_stage() -> str:
    return CURRENT_STAGE.strip()


def js_storage_info() -> str:
    return STORAGE_INFO.strip()


def js_economic_snapshot() -> str:
    return ECONOMIC_SNAPSHOT.strip()


def js_count(include_storage: bool) -> str:
    return _call(COUNT_PROBE, "true" if include_storage else "false")


def js_equip_count(content_type: int, include_storage: bool) -> str:
    return _call(EQUIP_COUNT_PROBE, str(int(content_type)),
                 "true" if include_storage else "false")


def js_jewel_fuse(tiers, include_storage: bool) -> str:
    return _call(FUSE_FLOW, _js(list(tiers)), "true" if include_storage else "false")


def js_equip_fuse(content_type: int, tiers, include_storage: bool,
                  same_level: bool, max_batches: int = 0) -> str:
    return _call(EQUIP_FUSE_FLOW, str(int(content_type)), _js(list(tiers)),
                 "true" if include_storage else "false",
                 "true" if same_level else "false", str(int(max_batches)))


def js_deposit(type_keys, allowed_tiers, exclude_locked: bool,
               round_limit: int = 20, pages_override: int = 0) -> str:
    return _call(DEPOSIT_FLOW, _js([int(t) for t in type_keys]),
                 _js([int(t) for t in allowed_tiers]),
                 "true" if exclude_locked else "false", str(int(round_limit)),
                 str(int(pages_override)))


def js_organize(page_cats: dict, dry_run: bool = False, include_locked: bool = False,
                sort_after: bool = False, pages_override: int = 0) -> str:
    opts = {"dryRun": bool(dry_run), "includeLocked": bool(include_locked),
            "sortAfter": bool(sort_after), "pagesOverride": int(pages_override)}
    return _call(ORGANIZE_FLOW, _js(dict(page_cats)), _js(opts))


def install(g: Game) -> None:
    if g.eval(INSTALL) != "ok":
        raise RuntimeError("ServiceWorkshop 未找到——请确认游戏已登录并停留在可操作界面")


# =====================================================================
# 命令行入口
# =====================================================================

def _print_report(lines) -> None:
    for r in lines or []:
        print(" ", r)


def cmd_stats(g: Game, cfg: dict) -> None:
    include = cfg["include_storage"]
    counts = json.loads(g.eval(js_count(include)))
    print(f"宝玉各品级可合成（含仓库={include}）：")
    for tier in range(1, 7):
        v = counts.get(f"tier{tier}", "?")
        if isinstance(v, int) and v >= 0:
            print(f"  T{tier}: {v} 颗 → 可合成 {v // 6} 次")
        else:
            print(f"  T{tier}: {v}")


def cmd_equip_stats(g: Game, cfg: dict, content_type: int) -> None:
    label = "饰品" if content_type == 2 else "装备"
    include = cfg["acc_include_storage"] if content_type == 2 else cfg["gear_include_storage"]
    data = json.loads(g.eval(js_equip_count(content_type, include)))
    print(f"{label}各品级可合成（含仓库={include}）：")
    for tier in range(1, 7):
        d = data.get(f"tier{tier}") or {}
        total = d.get("total", "?")
        print(f"  T{tier}: {total} 件，可合成 {d.get('batches', 0)} 批")
        for lv, p in sorted((d.get("plan") or {}).items(), key=lambda kv: int(kv[0])):
            print(f"      Lv{lv}: {p['have']} 件 / 每批 {p['need']} 件"
                  f"（成功率 {p['rate'] / 10000:.1f}%）→ {p['batches']} 批")


def cmd_fuse(g: Game, cfg: dict, tiers, content_type: int) -> None:
    if content_type == 20:
        include = cfg["include_storage"]
        print(f"宝玉合成品级 {tiers}（含仓库={include}）——执行中…")
        _print_report(g.eval(js_jewel_fuse(tiers, include), await_promise=True))
        return
    label = "饰品" if content_type == 2 else "装备"
    if content_type == 2:
        include, same = cfg["acc_include_storage"], cfg["acc_same_level"]
    else:
        include, same = cfg["gear_include_storage"], cfg["gear_same_level"]
    print(f"{label}合成品级 {tiers}（含仓库={include}, 仅同等级={same}）——执行中…")
    _print_report(g.eval(js_equip_fuse(content_type, tiers, include, same),
                         await_promise=True))


def cmd_deposit(g: Game, cfg: dict) -> None:
    types = cfg.get("deposit_item_types", [6, 4])
    tiers = cfg.get("deposit_tiers", [1, 2, 3])
    excl = cfg.get("deposit_exclude_locked", True)
    print(f"入库筛选：类型 {types}，品级 {tiers}，排除锁定={excl}")
    r = json.loads(g.eval(js_deposit(types, tiers, excl), await_promise=True))
    print(f"入库 {r['deposited']} 件（仓库 {r['storageUsed']}/{r['storageTotal']}，"
          f"{r['pages']} 页 × {r['slotCnt']}）"
          + ("　【仓库已满】" if r.get("full") else ""))


def cmd_storage(g: Game) -> None:
    info = json.loads(g.eval(js_storage_info()))
    print(f"仓库：{info['storageUsed']}/{info['storageTotal']}"
          f"（{info['pages']} 页 × {info['slotCnt']}，剩余 {info['storageFree']}）")
    print(f"背包：{info['inventoryUsed']}/{info['invSlots']}"
          f"，仓库内锁定 {info['lockedInStorage']} 件（最多可买 {info['maxDiaPages']} 页）")
    print("分类分布：", info["byCategory"])


def cmd_organize(g: Game, cfg: dict, dry_run: bool) -> None:
    cats = cfg.get("storage_page_cats", {})
    print(f"整理仓库（dry-run={dry_run}）：每页分类 {cats}")
    r = json.loads(g.eval(js_organize(
        cats, dry_run=dry_run,
        include_locked=cfg.get("organize_include_locked", False),
        sort_after=cfg.get("organize_sort_after", False)), await_promise=True))
    if not r.get("ok"):
        print("失败:", r.get("error"))
        return
    print(f"  {r['items']} 件 / {r['pages']} 页 × {r['slotCnt']}，"
          f"需搬 {r['planned']} 件（{r['steps']} 步，含中转），溢出留原地 {r['overflow']} 件")
    for line in r.get("preview", []):
        print("   ", line)
    if not dry_run:
        print(f"  实际移动 {r['moved']} 件，失败 {r['failed']} 件，分布 {r.get('distribution')}")


def main(argv=None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    cfg = load_config()
    cmd = argv[0] if argv else "stats"
    g = Game()
    try:
        install(g)
        if cmd == "stats":
            cmd_stats(g, cfg)
        elif cmd == "storage":
            cmd_storage(g)
        elif cmd == "fuse":
            tiers = [int(x) for x in argv[1].split(",")] if len(argv) > 1 else cfg["fuse_tiers"]
            cmd_fuse(g, cfg, tiers, CONTENT_JEWEL)
        elif cmd == "gear":
            tiers = ([int(x) for x in argv[1].split(",")] if len(argv) > 1
                     else cfg["gear_fuse_tiers"])
            cmd_fuse(g, cfg, tiers, CONTENT_GEAR)
        elif cmd == "acc":
            tiers = ([int(x) for x in argv[1].split(",")] if len(argv) > 1
                     else cfg["acc_fuse_tiers"])
            cmd_fuse(g, cfg, tiers, CONTENT_ACCESSORY)
        elif cmd == "gearstats":
            cmd_equip_stats(g, cfg, CONTENT_GEAR)
        elif cmd == "accstats":
            cmd_equip_stats(g, cfg, CONTENT_ACCESSORY)
        elif cmd == "deposit":
            cmd_deposit(g, cfg)
        elif cmd == "organize":
            cmd_organize(g, cfg, dry_run="--dry" in argv)
        else:
            print(__doc__)
    finally:
        g.close()


if __name__ == "__main__":
    main()
