#!/usr/bin/env python3
"""受控开启 WoG Puerts Inspector 10998 端口。

默认只检查状态，不写游戏文件。``apply`` 会先创建带哈希的备份，再把当前
GameAssembly.dll 中唯一的 10998 端口常量从 -1 改为 10998。``restore``
只接受本工具创建、且内容未被篡改的备份。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


def find_game_dir() -> Path:
    """从 exe、当前目录和 Steam 库位置定位真正的游戏根目录。"""
    candidates = []
    configured = os.environ.get("WOG_GAME_DIR")
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend([Path.cwd(), Path(__file__).resolve().parent])
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent)
    # 双击放在桌面、下载目录等位置时，从 Steam 的库配置补充候选路径。
    steam_roots = [
        Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Steam",
        Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Steam",
    ]
    try:
        import winreg
        for hive, key in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                          (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam")):
            try:
                with winreg.OpenKey(hive, key) as handle:
                    value, _ = winreg.QueryValueEx(handle, "SteamPath")
                    steam_roots.append(Path(value))
            except OSError:
                pass
    except ImportError:
        pass
    for steam_root in steam_roots:
        libraries_file = steam_root / "steamapps" / "libraryfolders.vdf"
        candidates.append(steam_root / "steamapps" / "common" / "War of Genesis Idle Loot")
        if libraries_file.is_file():
            try:
                text = libraries_file.read_text(encoding="utf-8", errors="ignore")
                for raw in re.findall(r'"path"\s+"([^"]+)"', text):
                    library = Path(raw.replace("\\\\", "\\"))
                    candidates.append(library / "steamapps" / "common" / "War of Genesis Idle Loot")
            except OSError:
                pass
    for base in candidates:
        for directory in (base, *base.parents):
            if (directory / "GameAssembly.dll").is_file() and (directory / "Genesis_Data").is_dir():
                return directory
    raise RuntimeError("找不到游戏根目录，请把 exe 放在游戏目录或 docs/game-plugins/dist 中")


GAME_DIR: Path | None = None
TARGET: Path | None = None
BACKUP_DIR: Path | None = None


def ensure_paths() -> Path:
    global GAME_DIR, TARGET, BACKUP_DIR
    if TARGET is not None and TARGET.is_file():
        return GAME_DIR
    GAME_DIR = find_game_dir()
    TARGET = GAME_DIR / "GameAssembly.dll"
    BACKUP_DIR = GAME_DIR / "docs" / "game-plugins" / "backups" / "10998-port"
    return GAME_DIR
TARGET_OFFSET = 16_934_718
CURRENT_IMMEDIATE = bytes.fromhex("ffffffff")
PATCHED_IMMEDIATE = bytes.fromhex("f62a0000")
INSTRUCTION_PREFIX = bytes.fromhex("ba")
EXPECTED_CURRENT_SHA256 = "3f55a97877c6c7101dcd5cfcdff30b7aa78df553f8ba986375dd99aa2e33bdc9"
EXPECTED_PATCHED_SHA256 = None


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_target() -> bytes:
    ensure_paths()
    if not TARGET.is_file():
        raise RuntimeError(f"找不到游戏文件：{TARGET}")
    return TARGET.read_bytes()


def assert_game_stopped() -> None:
    """避免在 Unity 进程仍占用模块时替换二进制。"""
    tasklist = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "tasklist.exe"
    try:
        result = subprocess.run(
            [str(tasklist), "/FI", "IMAGENAME eq Genesis.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, encoding="mbcs", errors="replace",
            timeout=10, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(
            "无法通过 Windows 进程列表确认游戏是否已退出；请检查 tasklist.exe，拒绝修改"
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "未知错误").strip()[:180]
        raise RuntimeError("Windows 进程检测失败，拒绝修改：" + detail)
    running = []
    for row in csv.reader(io.StringIO(result.stdout)):
        if row and row[0].strip().casefold() == "genesis.exe":
            pid = row[1].strip() if len(row) > 1 else "进程运行中"
            running.append(f"Genesis.exe PID {pid}")
    if running:
        raise RuntimeError("请先退出游戏再操作；仍在运行：" + ", ".join(running))


def target_state(data: bytes) -> str:
    if len(data) <= TARGET_OFFSET + 5:
        return "文件过短"
    if data[TARGET_OFFSET:TARGET_OFFSET + 1] != INSTRUCTION_PREFIX:
        return f"目标指令不是 mov edx，实际字节：{data[TARGET_OFFSET:TARGET_OFFSET + 5].hex()}"
    immediate = data[TARGET_OFFSET + 1:TARGET_OFFSET + 5]
    if immediate == CURRENT_IMMEDIATE:
        return "当前版本：10998 未开启"
    if immediate == PATCHED_IMMEDIATE:
        return "已打补丁：10998 已开启"
    return f"未知状态：{data[TARGET_OFFSET:TARGET_OFFSET + 5].hex()}"


def assert_current(data: bytes) -> None:
    actual = hashlib.sha256(data).hexdigest()
    if actual != EXPECTED_CURRENT_SHA256:
        raise RuntimeError(
            "拒绝修改：GameAssembly.dll 不是已确认的当前版本。\n"
            f"期望 SHA-256：{EXPECTED_CURRENT_SHA256}\n实际 SHA-256：{actual}"
        )
    if data[TARGET_OFFSET:TARGET_OFFSET + 1] != INSTRUCTION_PREFIX or \
            data[TARGET_OFFSET + 1:TARGET_OFFSET + 5] != CURRENT_IMMEDIATE:
        raise RuntimeError(
            "拒绝修改：目标位置不是预期的 mov edx, 0xffffffff，"
            f"实际字节为 {data[TARGET_OFFSET:TARGET_OFFSET + 5].hex()}"
        )


def backup_path() -> Path:
    ensure_paths()
    return BACKUP_DIR / "GameAssembly.dll.original"


def manifest_path() -> Path:
    ensure_paths()
    return BACKUP_DIR / "manifest.json"


def write_backup(data: bytes) -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    path = backup_path()
    if path.exists():
        old_hash = sha256(path)
        new_hash = hashlib.sha256(data).hexdigest()
        if old_hash != new_hash:
            raise RuntimeError(f"已有备份与当前文件不一致：{path}")
    else:
        temp = path.with_suffix(".tmp")
        temp.write_bytes(data)
        os.replace(temp, path)
    manifest = {
        "tool": "修复宝玉助手连接.py",
        "target": str(TARGET),
        "backup": str(path),
        "offset": TARGET_OFFSET,
        "original_bytes": data[TARGET_OFFSET:TARGET_OFFSET + 5].hex(),
        "original_sha256": hashlib.sha256(data).hexdigest(),
        "backup_sha256": sha256(path),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
    }
    manifest_path().write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def atomic_write(data: bytes) -> None:
    temp = TARGET.with_suffix(TARGET.suffix + ".wogtmp")
    temp.write_bytes(data)
    os.replace(temp, TARGET)


def cmd_status() -> int:
    data = read_target()
    print(f"文件：{TARGET}")
    print(f"SHA-256：{hashlib.sha256(data).hexdigest()}")
    print(f"状态：{target_state(data)}")
    print(f"目标字节：{data[TARGET_OFFSET:TARGET_OFFSET + 5].hex()}")
    if backup_path().is_file():
        print(f"备份：{backup_path()} ({sha256(backup_path())})")
    else:
        print("备份：不存在")
    return 0


def cmd_apply() -> int:
    ensure_patched()
    data = read_target()
    print("已完成：只开启 10998，9229 保持关闭。")
    print(f"原文件备份：{backup_path()}")
    print(f"新 SHA-256：{hashlib.sha256(data).hexdigest()}")
    print(f"新字节：{data[TARGET_OFFSET:TARGET_OFFSET + 5].hex()}")
    return 0


def ensure_patched() -> str:
    """确保 10998 已开启；已开启时不重复写文件。"""
    data = read_target()
    if target_state(data) == "已打补丁：10998 已开启":
        return "already_patched"
    assert_game_stopped()
    assert_current(data)
    write_backup(data)
    patched = data[:TARGET_OFFSET + 1] + PATCHED_IMMEDIATE + data[TARGET_OFFSET + 5:]
    atomic_write(patched)
    return "patched"


def cmd_restore() -> int:
    assert_game_stopped()
    path = backup_path()
    manifest = manifest_path()
    if not path.is_file() or not manifest.is_file():
        raise RuntimeError("没有找到完整的补丁备份")
    info = json.loads(manifest.read_text(encoding="utf-8"))
    backup_data = path.read_bytes()
    backup_hash = hashlib.sha256(backup_data).hexdigest()
    if backup_hash != info.get("backup_sha256") or backup_hash != info.get("original_sha256"):
        raise RuntimeError("拒绝恢复：备份文件哈希与 manifest 不一致")
    current = read_target()
    if target_state(current) != "已打补丁：10998 已开启":
        raise RuntimeError(f"拒绝恢复：当前文件不是本工具生成的补丁状态（{target_state(current)}）")
    atomic_write(backup_data)
    print("已恢复原始 GameAssembly.dll。")
    print(f"SHA-256：{backup_hash}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("status", "apply", "restore"), default="status")
    args = parser.parse_args()
    try:
        return {"status": cmd_status, "apply": cmd_apply, "restore": cmd_restore}[args.command]()
    except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
