#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
V680-CHUD 串口 读写 工具（简化版）
================================

仅保留：
1) COM 串口选择、刷新、连接/断开
2) 读取：RDA1 + 地址(4hex) + 长度(2位十进制) + 终结符
3) 写入：WTA1 + 地址(4hex) + 数据(ASCII) + 终结符

需要依赖：
    pip install pyserial
"""

from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
from typing import Optional, Union

import serial
import serial.tools.list_ports


# ---------------- 协议/常量 ----------------

TAG_USER_SIZE = 8192  # V680-D8KF67 用户区 8KB

CMD_TERM_STAR = "*"
CMD_TERM_STAR_CR = "*\r"
CMD_TERM_CR = "\r"

RDA1_READ_MAX = 99  # RDA1 单包最大长度：两位十进制 01~99
READ_TAG_TIMEOUT = 2.5


def find_v680_ports() -> list[serial.tools.list_ports.ListPortInfo]:
    """列举可用 COM 口（V680-CHUD 插上后会出现新 COM 口）"""
    return list(serial.tools.list_ports.comports())


def to_hex4(val: int) -> str:
    """4 位十六进制字符串（地址）"""
    return f"{val & 0xFFFF:04X}"


def bytes_to_ascii_display(data: bytes) -> str:
    """将字节转为可显示的 ASCII（不可见字符显示为 '.'）"""
    return "".join(chr(b) if 32 <= b < 127 else "." for b in data)


def hex_dump(data: bytes, start_addr: int = 0, bytes_per_line: int = 16) -> str:
    """生成十六进制 + ASCII 对照显示"""
    lines: list[str] = []
    for i in range(0, len(data), bytes_per_line):
        chunk = data[i : i + bytes_per_line]
        addr = start_addr + i
        hex_part = " ".join(f"{b:02X}" for b in chunk)
        ascii_part = bytes_to_ascii_display(chunk)
        lines.append(f"{addr:04X}: {hex_part:<48} | {ascii_part}")
    return "\n".join(lines)


class V680Protocol:
    """
    基于 RDA1 / WTA1 命令的简单协议封装。

    - 读：RDA1 + AAAA(4位十六进制地址) + NN(2位十进制长度) + term
    - 写：WTA1 + AAAA(4位十六进制地址) + DATA(ASCII) + term
    - 读响应：RD00 + DATA + '*'（有些设备可能在 '*' 前附加结束码，这里尽量兼容）
    """

    def __init__(self, ser: serial.Serial, term: str = CMD_TERM_STAR_CR) -> None:
        self.ser = ser
        self._lock = threading.Lock()
        self.term = term
        self.last_sent: str = ""
        self.last_recv: str = ""

    def set_term(self, term: str) -> None:
        self.term = term

    def _send_cmd(self, cmd: str, read_extra_timeout: float = 0.2) -> str:
        full_cmd = cmd + self.term
        with self._lock:
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
            raw = full_cmd.encode("ascii", errors="replace")
            self.ser.write(raw)
            self.ser.flush()
            self.last_sent = full_cmd.replace("\r", "\\r").replace("\n", "\\n")

            buf = bytearray()
            while True:
                b = self.ser.read(1)
                if not b:
                    # 已收到部分数据 -> 再额外等一小段看是否补齐
                    if read_extra_timeout > 0 and buf:
                        old_timeout = self.ser.timeout
                        self.ser.timeout = read_extra_timeout
                        more = self.ser.read(2048)
                        if more:
                            buf.extend(more)
                        self.ser.timeout = old_timeout
                    break

                buf.extend(b)
                if b == b"*":
                    if read_extra_timeout > 0:
                        old_timeout = self.ser.timeout
                        self.ser.timeout = read_extra_timeout
                        more = self.ser.read(2048)
                        if more:
                            buf.extend(more)
                        self.ser.timeout = old_timeout
                    break

                if len(buf) > 8192:
                    break

            resp = buf.decode("ascii", errors="replace")
            self.last_recv = resp.replace("\r", "\\r").replace("\n", "\\n") if resp else ""
            if not resp and buf:
                self.last_recv = "hex: " + buf.hex()
            return resp

    def _parse_rd_response(self, resp: str) -> tuple[Optional[bytes], Optional[str]]:
        if not resp.startswith("RD"):
            return None, None

        star = resp.find("*")
        if star < 0:
            return None, None

        # 兼容：RD00 + DATA + '*'（没有单独结束码字段）
        if resp.startswith("RD00") and len(resp) > 4:
            payload = resp[4:star]
            return payload.encode("ascii", errors="replace"), "00"

        # 其他情况：尝试用 '*' 前两位当结束码（兼容性处理）
        if star < 2:
            return None, None

        end_code = resp[star - 2 : star]
        if end_code != "00":
            return None, end_code

        # 兼容少数前缀：RD100 / RD10 等（尽量提取 DATA）
        for prefix_len in (5, 4, 2):
            if len(resp) <= prefix_len + 2:
                continue
            payload = resp[prefix_len : star - 2]
            try:
                return payload.encode("ascii", errors="replace"), "00"
            except Exception:
                continue

        return None, end_code

    def read_block(self, start_byte: int, length: int, extra_timeout: Optional[float] = None) -> bytes:
        if extra_timeout is None:
            extra_timeout = READ_TAG_TIMEOUT

        result: list[bytes] = []
        offset = 0
        while offset < length:
            chunk = min(length - offset, RDA1_READ_MAX)
            addr = start_byte + offset
            length_str = f"{chunk:02d}" if 0 < chunk < 100 else "99"
            cmd = "RDA1" + to_hex4(addr) + length_str
            resp = self._send_cmd(cmd, read_extra_timeout=extra_timeout)
            data, ec = self._parse_rd_response(resp)
            if data is None:
                raise RuntimeError(
                    f"读取失败 结束码={ec or '?'} 原始响应: {self.last_recv[:200] or '(空)'}"
                )
            result.append(data)
            offset += chunk
        return b"".join(result)

    def write_block(self, start_byte: int, data: Union[bytes, str]) -> None:
        if isinstance(data, str):
            data_bytes = data.encode("ascii", errors="replace")
        else:
            data_bytes = data

        # 写入 DATA 在协议中以 ASCII 文本形式拼接：
        # 非可打印字符用 '.' 占位，避免破坏帧结构
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in data_bytes)
        cmd = "WTA1" + to_hex4(start_byte) + text
        resp = self._send_cmd(cmd, read_extra_timeout=1.0)

        star = resp.find("*")
        if star >= 2:
            end_code = resp[star - 2 : star]
            if end_code not in ("00", "90"):
                raise RuntimeError(
                    f"写失败 结束码={end_code} 原始响应: {self.last_recv[:200]}"
                )


# ---------------- GUI ----------------


class V680SerialSimpleApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("V680-CHUD 串口 读写工具（简化版）")
        # 允许窗口在最小时尽量贴近控件高度，避免留下大面积空白
        self.root.minsize(720, 190)

        self.ser: Optional[serial.Serial] = None
        self.protocol: Optional[V680Protocol] = None

        self._build_ui()
        self._refresh_ports()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=10)
        # 只在水平方向自适应，避免高度被拉得过大
        main.pack(fill=tk.X, expand=False)

        # 串口连接区
        f_conn = ttk.LabelFrame(main, text="串口连接", padding=8)
        f_conn.pack(fill=tk.X, pady=(0, 4))

        row0 = ttk.Frame(f_conn)
        row0.pack(fill=tk.X)

        ttk.Label(row0, text="端口:").pack(side=tk.LEFT)
        self.port_var = tk.StringVar()
        self.port_combo = ttk.Combobox(row0, textvariable=self.port_var, width=18, state="readonly")
        self.port_combo.pack(side=tk.LEFT, padx=(6, 8))

        ttk.Button(row0, text="刷新", command=self._refresh_ports).pack(side=tk.LEFT, padx=4)

        ttk.Label(row0, text="波特率:").pack(side=tk.LEFT, padx=(14, 4))
        self.baud_var = tk.StringVar(value="9600")
        self.baud_combo = ttk.Combobox(
            row0,
            textvariable=self.baud_var,
            values=["2400", "4800", "9600", "19200", "38400", "115200"],
            width=10,
            state="disabled",
        )
        self.baud_combo.pack(side=tk.LEFT)

        self.btn_connect = ttk.Button(row0, text="连接", command=self._toggle_connect)
        self.btn_connect.pack(side=tk.LEFT, padx=(14, 0))

        # 操作区
        f_op = ttk.LabelFrame(main, text="读写功能", padding=8)
        f_op.pack(fill=tk.X, pady=(0, 4))

        # 只保留你红框部分（长度/读取、写入内容/写入）。
        # 起始地址固定为 0；结束符固定为 *CR（不再显示对应控件）。
        self.term_var = tk.StringVar(value="*CR")

        body = ttk.Frame(f_op)
        body.pack(fill=tk.X)

        right = ttk.Frame(body)
        right.pack(fill=tk.X, expand=False)
        # 结果显示不需要拉伸占满高度（避免显示框太大）
        right.grid_rowconfigure(1, weight=0)
        # 输入框宽度与“串口连接”区对齐，不随窗口横向拉伸
        right.grid_columnconfigure(1, weight=0)

        ui_font = ("Consolas", 18)

        # 写入行（放在最上面）
        ttk.Label(right, text="写入内容(ASCII):").grid(row=0, column=0, sticky=tk.W)
        self.txt_write = tk.Text(
            right,
            height=1,
            width=48,
            wrap=tk.NONE,
            font=ui_font,
            foreground="red",
            insertbackground="red",
        )
        self.txt_write.grid(row=0, column=1, sticky=tk.W, padx=(6, 0))

        # 结果显示（布局与写入内容一致：Label + Text，无边框容器）
        ttk.Label(right, text="结果显示:").grid(row=1, column=0, sticky=tk.W, pady=(5, 0))
        self.txt_display = tk.Text(
            right,
            height=1,
            width=48,
            wrap=tk.NONE,
            font=ui_font,
            foreground="green",
        )
        self.txt_display.grid(row=1, column=1, sticky=tk.W, padx=(6, 0), pady=(5, 0))
        self.txt_display.config(state=tk.DISABLED)

        # 读取参数行（放在结果显示下面）
        ttk.Label(right, text="长度(字节):").grid(row=2, column=0, sticky=tk.W, pady=(5, 0))
        self.read_len_var = tk.StringVar(value="26")
        ttk.Entry(right, textvariable=self.read_len_var, width=8).grid(
            row=2, column=1, sticky=tk.W, padx=(6, 0), pady=(5, 0)
        )

        # 底部按钮区：把“写入/读取”放到最后（最底一行）
        btn_row = ttk.Frame(right)
        btn_row.grid(row=3, column=0, columnspan=3, sticky=tk.EW, pady=(5, 0))
        btn_row.columnconfigure(0, weight=1)
        ttk.Button(btn_row, text="写入", command=self._do_write).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Button(btn_row, text="读取", command=self._do_read).pack(side=tk.LEFT)

    def _show_ascii_only(self, data: bytes) -> None:
        """结果显示区只显示 ASCII 内容，其它信息不输出。"""
        ascii_text = bytes_to_ascii_display(data)
        self.txt_display.config(state=tk.NORMAL)
        self.txt_display.delete(1.0, tk.END)
        if ascii_text:
            self.txt_display.insert(tk.END, ascii_text)
        self.txt_display.config(state=tk.DISABLED)

    def _set_connected_ui(self, connected: bool) -> None:
        # 简化：只控制按钮
        self.btn_connect.config(text="断开" if connected else "连接")

    def _refresh_ports(self) -> None:
        ports = find_v680_ports()
        values = [p.device for p in ports]
        self.port_combo["values"] = values
        if values and not self.port_var.get():
            self.port_var.set(values[0])

    def _toggle_connect(self) -> None:
        if self.ser and self.ser.is_open:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None
            self.protocol = None
            self._set_connected_ui(False)
            return

        port = self.port_var.get().strip()
        if not port:
            messagebox.showwarning("警告", "请选择 COM 端口")
            return
        try:
            baud = int(self.baud_var.get())
        except ValueError:
            baud = 9600

        try:
            self.ser = serial.Serial(
                port=port,
                baudrate=baud,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.5,
                write_timeout=2,
            )
            self.protocol = V680Protocol(self.ser, term=CMD_TERM_STAR_CR)
            self._apply_term()
            self._set_connected_ui(True)
        except Exception as e:
            self.ser = None
            self.protocol = None
            messagebox.showerror("连接失败", str(e))

    def _apply_term(self) -> None:
        if not self.protocol:
            return
        t = self.term_var.get()
        if t == "CR":
            self.protocol.set_term(CMD_TERM_CR)
        elif t == "*CR":
            self.protocol.set_term(CMD_TERM_STAR_CR)
        else:
            self.protocol.set_term(CMD_TERM_STAR)

    def _ensure_connected(self) -> bool:
        if not self.ser or not self.ser.is_open or not self.protocol:
            messagebox.showwarning("警告", "请先连接串口。")
            return False
        return True

    def _do_read(self) -> None:
        if not self._ensure_connected():
            return
        start = 0
        try:
            length = int(self.read_len_var.get())
        except ValueError:
            messagebox.showwarning("警告", "长度请输入数字")
            return

        if length <= 0 or start + length > TAG_USER_SIZE:
            messagebox.showwarning("警告", f"长度范围：1~{TAG_USER_SIZE-start}")
            return

        self._apply_term()

        def run() -> None:
            try:
                assert self.protocol is not None
                data = self.protocol.read_block(start, length, extra_timeout=READ_TAG_TIMEOUT)
                self.root.after(
                    0, lambda: self._on_read_done(start, data, length=length)
                )
            except Exception as e:
                msg = str(e)
                if "结束码=72" in msg or "end_code=72" in msg:
                    friendly = "读取失败：未检测到芯片。请确认标签已靠近天线，或检查芯片状态/通信线缆。"
                    self.root.after(0, lambda: messagebox.showerror("读取失败", friendly))
                else:
                    self.root.after(0, lambda: messagebox.showerror("读取失败", msg))

        threading.Thread(target=run, daemon=True).start()

    def _on_read_done(self, start_addr: int, data: bytes, *, length: int) -> None:
        if not data:
            return
        display_len = min(len(data), length)
        data = data[:display_len]
        self._show_ascii_only(data)

    def _do_write(self) -> None:
        if not self._ensure_connected():
            return
        start = 0
        if start < 0 or start >= TAG_USER_SIZE:
            messagebox.showwarning("警告", f"写入起始地址范围：0~{TAG_USER_SIZE-1}")
            return

        content = self.txt_write.get("1.0", tk.END).rstrip("\n").rstrip("\r")
        content = content.strip("\x00")
        if not content:
            messagebox.showwarning("警告", "请在输入框中填写要写入的 ASCII 内容")
            return

        data_bytes = content.encode("ascii", errors="replace")
        if start + len(data_bytes) > TAG_USER_SIZE:
            messagebox.showwarning("警告", "写入范围越界（超出 8KB 用户区）")
            return

        def run() -> None:
            try:
                assert self.protocol is not None
                self.protocol.write_block(start, data_bytes)
                self.root.after(0, lambda: messagebox.showinfo("写入完成", "写入完成。"))
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("写入失败", str(e)))

        threading.Thread(target=run, daemon=True).start()

    def _on_close(self) -> None:
        try:
            if self.ser and self.ser.is_open:
                self.ser.close()
        except Exception:
            pass
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    V680SerialSimpleApp().run()

