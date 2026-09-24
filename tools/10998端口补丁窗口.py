#!/usr/bin/env python3
"""WoG 10998 端口补丁的双击启动窗口。"""

from __future__ import annotations

import contextlib
import io
import threading
import tkinter as tk
from tkinter import messagebox, ttk

import 修复宝玉助手连接 as patch


class PatchWindow(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("WoG 宝玉助手连接补丁")
        self.geometry("720x480")
        self.minsize(620, 400)
        self.configure(padx=16, pady=14)

        ttk.Label(self, text="宝玉助手 10998 单端口补丁", font=("Microsoft YaHei", 15, "bold")).pack(anchor="w")
        ttk.Label(
            self,
            text="只修改 GameAssembly.dll 的 10998 端口常量；9229 和 data.unity3d 保持不变。",
        ).pack(anchor="w", pady=(4, 10))

        self.status_var = tk.StringVar(value="正在读取状态…")
        ttk.Label(self, textvariable=self.status_var).pack(anchor="w", pady=(0, 8))

        buttons = ttk.Frame(self)
        buttons.pack(fill="x", pady=(0, 10))
        self.refresh_button = ttk.Button(buttons, text="刷新状态", command=self.refresh)
        self.refresh_button.pack(side="left")
        self.apply_button = ttk.Button(buttons, text="开启 10998", command=lambda: self.run_action("apply"))
        self.apply_button.pack(side="left", padx=(8, 0))
        self.restore_button = ttk.Button(buttons, text="恢复原文件", command=lambda: self.run_action("restore"))
        self.restore_button.pack(side="left", padx=(8, 0))

        self.output = tk.Text(self, height=18, wrap="word", state="disabled", font=("Consolas", 10))
        self.output.pack(fill="both", expand=True)
        self.refresh()

    def write_output(self, text: str) -> None:
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.insert("end", text)
        self.output.configure(state="disabled")

    def run_capture(self, command: str) -> tuple[int, str]:
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                code = {"status": patch.cmd_status, "apply": patch.cmd_apply, "restore": patch.cmd_restore}[command]()
        except Exception as exc:
            code = 2
            print(f"错误：{exc}", file=buffer)
        return code, buffer.getvalue()

    def refresh(self) -> None:
        code, output = self.run_capture("status")
        self.write_output(output)
        self.status_var.set("状态读取完成" if code == 0 else "状态读取失败")

    def run_action(self, command: str) -> None:
        if command == "apply":
            if not messagebox.askyesno("确认开启端口", "请确认游戏已经完全退出。\n\n继续后会先备份 GameAssembly.dll，再开启 10998。"):
                return
        elif command == "restore":
            if not messagebox.askyesno("确认恢复", "将恢复补丁工具创建的原始 GameAssembly.dll。\n\n游戏必须已经完全退出。"):
                return
        for button in (self.refresh_button, self.apply_button, self.restore_button):
            button.configure(state="disabled")
        self.status_var.set("正在处理…")

        def work():
            code, output = self.run_capture(command)
            self.after(0, lambda: self.finish_action(code, output))

        threading.Thread(target=work, daemon=True).start()

    def finish_action(self, code: int, output: str) -> None:
        self.write_output(output)
        self.status_var.set("操作完成" if code == 0 else "操作未执行")
        for button in (self.refresh_button, self.apply_button, self.restore_button):
            button.configure(state="normal")
        if code == 0:
            messagebox.showinfo("完成", "操作已完成。")
        else:
            messagebox.showerror("未执行", "操作被拒绝或失败，请查看窗口中的详细信息。")


if __name__ == "__main__":
    PatchWindow().mainloop()
