"""使用 GOST 的 Windows 代理客户端。"""

from collections import deque
import ctypes
from ctypes import wintypes
import ipaddress
import json
import math
import os
from pathlib import Path
import queue
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import quote

try:
    import customtkinter as ctk
    import psutil
except ImportError:
    message = "缺少界面或网速统计依赖。\n\n请在项目目录执行\npython -m pip install -r requirements.txt"
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, message, "请安装依赖", 0x10)
    else:
        print(message, file=sys.stderr)
    raise SystemExit(1)

if os.name == "nt":
    import winreg


BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
APP_DIR = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
           else Path(__file__).resolve().parent)
CONFIG_FILE = APP_DIR / "config.json"
GOST_EXE = BASE_DIR / "gost.exe"
LOCAL_PORT = 10808  # 如需自定义本地端口，只修改这里；远端端口仍由输入框设置。
LOCAL_ADDRESS = ("127.0.0.1", LOCAL_PORT)
PROXY_ADDRESS = f"127.0.0.1:{LOCAL_PORT}"
# 不指定协议时，GOST 自动识别 HTTP / SOCKS5；系统代理需要 HTTP 支持。
LISTEN_URL = PROXY_ADDRESS
BACKGROUND = "#F6F8FB"
SURFACE = "#FFFFFF"
INPUT = "#FFFFFF"
BORDER = "#E2E7EF"
TEXT = "#1E293B"
MUTED = "#64748B"
BLUE = "#3b82f6"
RED = "#ef4444"
GREEN = "#22c55e"
AMBER = "#B7791F"
DOWNLOAD = "#7CB7FA"
UPLOAD = "#7BDDB6"
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
LOG_FIELD = re.compile(r'"(msg|level)"\s*:\s*("(?:\\.|[^"\\])*")')
DEFAULT_CONFIG = {"vps_ip": "", "port": "", "username": "", "password": ""}


class ConfigStore:
    """配置留在程序旁边；先写临时文件，再替换，避免写到一半损坏原配置。"""

    def __init__(self, path=None):
        self.path = Path(path) if path is not None else CONFIG_FILE

    def load(self):
        result = DEFAULT_CONFIG.copy()
        try:
            with self.path.open("r", encoding="utf-8-sig") as stream:
                saved = json.load(stream)
        except FileNotFoundError:
            return result
        if not isinstance(saved, dict):
            raise ValueError("配置内容必须是 JSON 对象")
        for key in result:
            value = saved.get(key)
            if isinstance(value, str):
                result[key] = value
            elif key == "port" and isinstance(value, int) and not isinstance(value, bool):
                result[key] = str(value)
        return result

    def save(self, values):
        payload = {key: values.get(key, default) for key, default in DEFAULT_CONFIG.items()}
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.path.parent,
                prefix=".config-", suffix=".tmp", delete=False
            ) as stream:
                temporary = Path(stream.name)
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def matches_local_proxy(server, address=None):
    """只匹配本客户端的回环地址，兼容 Windows 的协议列表表示。"""
    if not isinstance(server, str):
        return False
    address = PROXY_ADDRESS if address is None else address
    endpoints = []
    for item in server.split(";"):
        item = item.strip()
        if not item:
            continue
        if "=" in item:
            protocol, item = item.split("=", 1)
            if protocol.strip().lower() not in {"http", "https", "socks", "ftp"}:
                return False
        endpoints.append(item.strip())
    return bool(endpoints) and all(endpoint == address for endpoint in endpoints)


def format_speed(byte_rate):
    """以 1024 换算 KB / MB，输入为字节每秒。"""
    byte_rate = max(0.0, byte_rate)
    if byte_rate >= 1024 * 1024:
        return f"{byte_rate / (1024 * 1024):.2f} MB/s"
    return f"{byte_rate / 1024:.1f} KB/s"


def blend_color(base, accent, amount):
    amount = min(1.0, max(0.0, amount))
    values = [round(int(base[i:i + 2], 16) * (1 - amount)
                    + int(accent[i:i + 2], 16) * amount) for i in (1, 3, 5)]
    return "#" + "".join(f"{value:02x}" for value in values)


class NetworkSampler:
    """读取整机累计字节，用真实时间差计算速率，首个样本仅建立基线。"""

    def __init__(self, reader=None, clock=None):
        self.reader = reader if reader is not None else psutil.net_io_counters
        self.clock = clock if clock is not None else time.perf_counter
        self.previous = None

    def sample(self):
        counters = self.reader()
        now = self.clock()
        if counters is None:
            self.previous = None
            return None
        current = (now, counters.bytes_recv, counters.bytes_sent)
        previous, self.previous = self.previous, current
        if previous is None or now <= previous[0]:
            return 0.0, 0.0
        elapsed = now - previous[0]
        return (max(0, counters.bytes_recv - previous[1]) / elapsed,
                max(0, counters.bytes_sent - previous[2]) / elapsed)


class TrafficChart(ctk.CTkCanvas):
    """最近 60 个真实采样值；两条曲线使用平滑线段和过渡动画。"""

    def __init__(self, master, **kwargs):
        super().__init__(master, bg=SURFACE, highlightthickness=0, bd=0, **kwargs)
        self.download = deque([0.0] * 60, maxlen=60)
        self.upload = deque([0.0] * 60, maxlen=60)
        self.previous_download = list(self.download)
        self.previous_upload = list(self.upload)
        self.transition_started = time.perf_counter()
        self.create_polygon(0, 0, 0, 0, fill="#F0F7FF", outline="", tags="area")
        for tag, color in (("download", DOWNLOAD), ("upload", UPLOAD)):
            self.create_line(0, 0, 1, 0, fill=color, width=2.8, smooth=True,
                             splinesteps=20, capstyle="round", joinstyle="round", tags=tag)
        self.bind("<Configure>", lambda event: self.draw())

    def push(self, download, upload):
        self.previous_download = list(self.download)
        self.previous_upload = list(self.upload)
        self.download.append(max(0.0, download))
        self.upload.append(max(0.0, upload))
        self.transition_started = time.perf_counter()

    def draw(self):
        width, height = self.winfo_width(), self.winfo_height()
        if width < 16 or height < 4:
            return
        progress = min(1.0, max(0.0, (time.perf_counter() - self.transition_started) / 0.65))
        progress = 1 - (1 - progress) ** 3
        peak = max(1024.0, max(self.download), max(self.upload)) * 1.18
        margin = min(10, height / 4)
        for tag, previous, current in (
            ("download", self.previous_download, self.download),
            ("upload", self.previous_upload, self.upload),
        ):
            points = []
            for index, (old, new) in enumerate(zip(previous, current)):
                value = old + (new - old) * progress
                points.extend((6 + index * (width - 12) / 59,
                               height - margin - value / peak * (height - 2 * margin)))
            self.coords(tag, *points)
            if tag == "download":
                self.coords("area", points[0], height, *points, points[-2], height)


def parse_gost_log(line):
    """JSON 仅提取顶层 msg / level；破损 JSON 尝试提取完整字符串字段。"""
    text = ANSI_ESCAPE.sub("", line).strip()
    if not text:
        return None
    try:
        record = json.loads(text)
    except (ValueError, RecursionError):
        if "{" in text or text.startswith("[") and '"' in text:
            # 兼容带时间前缀的 JSON；失败时不把原始对象显示在界面。
            start = text.find("{")
            try:
                record, _ = json.JSONDecoder().raw_decode(text[start:])
            except (ValueError, RecursionError):
                record = {}
                for key, value in LOG_FIELD.findall(text):
                    try:
                        record[key] = json.loads(value)
                    except ValueError:
                        pass
        else:
            # 兼容旧版 GOST 或进程加载错误的纯文本输出。
            match = re.search(r"\b(error|fatal|panic|warn|warning)\b", text, re.I)
            return (match.group(1).lower() if match else "info", text)
    if not isinstance(record, dict) or not isinstance(record.get("msg"), str):
        return None
    message = record["msg"].strip()
    if not message:
        return None
    level = record.get("level", "info")
    return (level.lower().strip() if isinstance(level, str) else "info", message)


class LogTextbox(ctk.CTkTextbox):
    """CTkText 的标签接口不接受字体；在内部文本组件配置并同步 DPI 缩放。"""

    def _style_tags(self):
        self.tag_config("info", foreground=MUTED)
        self.tag_config("warning", foreground=AMBER)
        self._textbox.tag_configure(
            "error", foreground=RED,
            font=self._apply_font_scaling(("Microsoft YaHei UI", 13, "bold"))
        )

    def _set_scaling(self, *args, **kwargs):
        super()._set_scaling(*args, **kwargs)
        self._style_tags()

    def _update_font(self):
        super()._update_font()
        self._style_tags()


def build_forward_url(host, port, username, password):
    try:
        ip = ipaddress.ip_address(host.strip().removeprefix("[").removesuffix("]"))
    except ValueError as exc:
        raise ValueError("请填写有效的 VPS IPv4 或 IPv6 地址。") from exc
    try:
        number = int(port)
    except (ValueError, TypeError) as exc:
        raise ValueError("端口必须是 1 到 65535 之间的整数。") from exc
    if not 1 <= number <= 65535:
        raise ValueError("端口必须是 1 到 65535 之间的整数。")
    if not username.strip() or not password:
        raise ValueError("请填写用户名和密码。")
    address = f"[{ip}]" if ip.version == 6 else str(ip)
    # 特殊字符必须编码，避免 @、:、/ 等字符破坏认证 URL。
    return (
        f"socks5://{quote(username, safe='')}:{quote(password, safe='')}"
        f"@{address}:{number}"
    )


def notify_proxy_changed():
    library = ctypes.WinDLL("wininet", use_last_error=True)
    set_option = library.InternetSetOptionW
    set_option.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    set_option.restype = wintypes.BOOL
    for option in (39, 37):  # SETTINGS_CHANGED、REFRESH
        if not set_option(None, option, None, 0):
            raise ctypes.WinError(ctypes.get_last_error())


class SystemProxy:
    """管理当前用户的 Windows 手动代理，不修改机器级 WinHTTP 设置。"""

    REGISTRY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"

    def __init__(self):
        self.managed = False
        self.original_values = {}

    def clear_stale_proxy(self, address=None):
        """初始化时立即执行；残留代理关闭后，后续停止不能再恢复为开启。"""
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, self.REGISTRY_PATH, 0,
            winreg.KEY_QUERY_VALUE
        ) as key:
            try:
                enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
                server, _ = winreg.QueryValueEx(key, "ProxyServer")
            except FileNotFoundError:
                return False
            if enabled != 1 or not matches_local_proxy(server, address):
                return False
        # 若写入或网络通知失败，保留一个“恢复到关闭”的清理状态以便重试。
        self.original_values = {"ProxyEnable": (0, winreg.REG_DWORD)}
        self.managed = True
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, self.REGISTRY_PATH, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
        notify_proxy_changed()
        self.managed = False
        self.original_values.clear()
        return True

    def enable(self):
        if self.managed:
            return
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, self.REGISTRY_PATH, 0,
            winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE
        ) as key:
            for name in ("ProxyServer", "ProxyOverride", "ProxyEnable"):
                try:
                    self.original_values[name] = winreg.QueryValueEx(key, name)
                except FileNotFoundError:
                    self.original_values[name] = None
            # 先标记接管，以便任何写入或通知失败后仍能回滚。
            self.managed = True
            winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, PROXY_ADDRESS)
            winreg.SetValueEx(key, "ProxyOverride", 0, winreg.REG_SZ,
                              "<local>;localhost;127.*;[::1]")
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
        notify_proxy_changed()

    def disable(self):
        if not self.managed:
            return
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, self.REGISTRY_PATH, 0,
            winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE
        ) as key:
            # 先断开本程序的代理，再恢复原来的地址、绕过列表及开关。
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
            for name, original in self.original_values.items():
                if original is None:
                    try:
                        winreg.DeleteValue(key, name)
                    except FileNotFoundError:
                        pass
                else:
                    value, value_type = original
                    winreg.SetValueEx(key, name, 0, value_type, value)
        notify_proxy_changed()
        self.managed = False
        self.original_values.clear()


def ensure_listener_available(address=None):
    address = LOCAL_ADDRESS if address is None else address
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind(address)


def wait_for_listener(process, cancelled, address=None, timeout=8):
    """确认本地 SOCKS5 握手成功；不连接 VPS，不代表远端认证已通过。"""
    address = LOCAL_ADDRESS if address is None else address
    deadline = time.monotonic() + timeout
    while not cancelled.is_set() and time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("GOST 在本地服务就绪前退出，请查看日志。")
        try:
            with socket.create_connection(address, timeout=0.25) as connection:
                connection.settimeout(0.25)
                connection.sendall(b"\x05\x01\x00")
                response = b""
                while len(response) < 2:
                    chunk = connection.recv(2 - len(response))
                    if not chunk:
                        break
                    response += chunk
                if response == b"\x05\x00":
                    if cancelled.wait(0.1):
                        return False
                    if process.poll() is None:
                        return True
        except OSError:
            pass
        cancelled.wait(0.1)
    if cancelled.is_set():
        return False
    raise TimeoutError("等待本地代理就绪超时，未开启系统代理。")


def terminate_and_wait(process, timeout=3):
    """只终止本程序启动的进程，并等待回收；超时则强制结束。"""
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except OSError:
        # 进程可能刚好自行退出；也可能 terminate 失败，仍继续等待并尝试 kill。
        pass
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            if process.poll() is None:
                raise
        process.wait(timeout=timeout)


class ProxyApp:
    def __init__(self, root, system_proxy=None, network_sampler=None, config_store=None):
        # 第一步先清理注册表残留，早于读取配置和创建任何界面控件。
        self.system_proxy = system_proxy if system_proxy is not None else SystemProxy()
        startup_cleaned, startup_error = False, None
        try:
            startup_cleaned = self.system_proxy.clear_stale_proxy()
        except OSError as exc:
            startup_error = str(exc)
        self.startup_cleanup_pending = startup_error is not None
        self.root = root
        self.config_store = config_store if config_store is not None else ConfigStore()
        config_error = None
        try:
            saved_config = self.config_store.load()
        except (OSError, ValueError):
            saved_config = DEFAULT_CONFIG.copy()
            config_error = "无法读取 config.json，已使用默认配置。"
        self.network_sampler = network_sampler if network_sampler is not None else NetworkSampler()
        self.traffic_problem = False
        self.breathing = False
        self.process = None
        self.starting = False
        self.stopping = False
        self.closing = False
        self.proxy_error = startup_error is not None
        self.failed = startup_error is not None
        self.cancel_start = threading.Event()
        self.secret_values = set()
        if saved_config["password"]:
            self.secret_values.update((saved_config["password"], quote(saved_config["password"], safe="")))
        self.events = queue.Queue()
        self.log_events = queue.Queue(maxsize=2000)
        self.after_id = None
        self.traffic_after_id = None
        self.animation_after_id = None

        ctk.set_appearance_mode("light")
        root.title("极简代理")
        root.geometry("740x820")
        root.minsize(640, 720)
        root.configure(fg_color=BACKGROUND)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(4, weight=1)

        header = ctk.CTkFrame(root, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=32, pady=(24, 16))
        ctk.CTkLabel(
            header, text="极简代理", text_color=TEXT,
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=27, weight="bold")
        ).pack(side="left")
        self.status_halo = ctk.CTkFrame(
            header, width=24, height=24, corner_radius=12, fg_color=BACKGROUND
        )
        self.status_halo.pack(side="left", padx=(12, 5))
        self.status_halo.pack_propagate(False)
        self.status_dot = ctk.CTkFrame(
            self.status_halo, width=10, height=10, corner_radius=5, fg_color=RED
        )
        self.status_dot.place(relx=0.5, rely=0.5, anchor="center")
        self.status = ctk.StringVar(value="未启动")
        self.status_label = ctk.CTkLabel(
            header, textvariable=self.status, text_color=MUTED,
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=13)
        )
        self.status_label.pack(side="left")

        config = ctk.CTkFrame(root, fg_color=SURFACE, corner_radius=20,
                              border_width=1, border_color="#EDF0F5")
        config.grid(row=1, column=0, sticky="ew", padx=32)
        config.columnconfigure(0, weight=3, uniform="fields")
        config.columnconfigure(1, weight=1)
        ctk.CTkLabel(
            config, text="节点配置", text_color=TEXT, anchor="w", height=22,
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=14, weight="bold")
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=22, pady=(15, 2))
        self.host = ctk.StringVar(value=saved_config["vps_ip"])
        self.port = ctk.StringVar(value=saved_config["port"])
        self.username = ctk.StringVar(value=saved_config["username"])
        self.password = ctk.StringVar(value=saved_config["password"])
        self.config_entries = []
        for label, variable, row, column, masked in (
            ("VPS IP", self.host, 1, 0, False),
            ("端口", self.port, 1, 1, False),
            ("用户名", self.username, 3, 0, False),
            ("密码", self.password, 3, 1, True),
        ):
            padding = (22, 8) if column == 0 else (8, 22)
            ctk.CTkLabel(
                config, text=label, text_color=MUTED, anchor="w", height=18,
                font=ctk.CTkFont(family="Microsoft YaHei UI", size=12)
            ).grid(row=row, column=column, sticky="w", padx=padding, pady=(8, 5))
            entry = ctk.CTkEntry(
                config, textvariable=variable,
                height=42, corner_radius=11, fg_color=INPUT,
                border_width=1, border_color=BORDER, text_color=TEXT,
                show="*" if masked else "",
                font=ctk.CTkFont(family="Microsoft YaHei UI", size=13)
            )
            entry.grid(row=row + 1, column=column, sticky="ew", padx=padding,
                       pady=(0, 18) if row == 3 else 0)
            entry.bind("<FocusIn>", lambda event, field=entry: field.configure(border_color=BLUE))
            entry.bind("<FocusOut>", lambda event, field=entry: field.configure(border_color=BORDER))
            self.config_entries.append(entry)

        self.control_button = ctk.CTkButton(
            root, text="启动代理", height=50, corner_radius=13,
            fg_color=BLUE, hover_color="#2563EB", command=self.toggle_proxy,
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=15, weight="bold")
        )
        self.control_button.grid(row=2, column=0, sticky="ew", padx=32, pady=16)

        traffic = ctk.CTkFrame(root, fg_color=SURFACE, corner_radius=20,
                               border_width=1, border_color="#EDF0F5")
        traffic.grid(row=3, column=0, sticky="ew", padx=32, pady=(0, 16))
        traffic.grid_columnconfigure(0, weight=1)
        traffic_header = ctk.CTkFrame(traffic, fg_color="transparent")
        traffic_header.grid(row=0, column=0, sticky="ew", padx=22, pady=(14, 0))
        ctk.CTkLabel(
            traffic_header, text="实时流量", text_color=TEXT, height=22,
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=14, weight="bold")
        ).pack(side="left")
        self.upload_speed = ctk.StringVar(value="↑ 0.0 KB/s")
        self.download_speed = ctk.StringVar(value="↓ 0.0 KB/s")
        for variable, color in ((self.upload_speed, "#239F79"), (self.download_speed, BLUE)):
            ctk.CTkLabel(
                traffic_header, textvariable=variable, text_color=color, height=22,
                font=ctk.CTkFont(family="Microsoft YaHei UI", size=12)
            ).pack(side="right", padx=(16, 0))
        self.traffic_chart = TrafficChart(traffic, height=100)
        self.traffic_chart.grid(row=1, column=0, sticky="ew", padx=16, pady=(6, 0))
        ctk.CTkLabel(
            traffic, text="整机网速 · 最近 60 秒", text_color="#94A3B8", height=16,
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=10)
        ).grid(row=2, column=0, sticky="w", padx=22, pady=(0, 12))

        logs = ctk.CTkFrame(root, fg_color=SURFACE, corner_radius=20,
                            border_width=1, border_color="#EDF0F5")
        logs.grid(row=4, column=0, sticky="nsew", padx=32, pady=(0, 24))
        logs.grid_columnconfigure(0, weight=1)
        logs.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(
            logs, text="运行日志", text_color=TEXT, anchor="w", height=22,
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=14, weight="bold")
        ).grid(row=0, column=0, sticky="w", padx=22, pady=(15, 8))
        self.log_box = LogTextbox(
            logs, wrap="word", state="disabled", fg_color="#F8FAFC",
            corner_radius=12, border_width=0, text_color=MUTED,
            scrollbar_button_color="#D6DEE9", scrollbar_button_hover_color="#B6C4D7",
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=13),
            spacing1=3, spacing3=4, height=96, activate_scrollbars=True
        )
        self.log_box.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 16))
        self.log_box._style_tags()
        self._append_log("填写节点信息后启动代理。")
        if startup_cleaned:
            self._append_log("已关闭上次异常退出遗留的系统代理。")
        if startup_error:
            self._append_log(f"启动时清理系统代理失败：{startup_error}，请点击停止重试。", "error")
        if config_error:
            self._append_log(config_error, "warning")
        self._refresh_controls()
        self.after_id = root.after(50, self._poll_events)
        self._sample_traffic()
        self._animate_ui()

    def _sample_traffic(self):
        self.traffic_after_id = None
        try:
            rates = self.network_sampler.sample()
            if rates is None:
                raise OSError("未找到可用的网卡计数器")
        except (OSError, psutil.Error) as exc:
            self.network_sampler.previous = None
            self.download_speed.set("↓ —")
            self.upload_speed.set("↑ —")
            if not self.traffic_problem:
                self._append_log(f"暂时无法读取网速：{exc}", "warning")
            self.traffic_problem = True
        else:
            self.traffic_problem = False
            download, upload = rates
            self.download_speed.set("↓ " + format_speed(download))
            self.upload_speed.set("↑ " + format_speed(upload))
            self.traffic_chart.push(download, upload)
        self.traffic_after_id = self.root.after(1000, self._sample_traffic)

    def _animate_ui(self):
        self.animation_after_id = None
        if self.breathing:
            phase = (math.sin(time.perf_counter() * math.tau / 2.4) + 1) / 2
            self.status_halo.configure(fg_color=blend_color(BACKGROUND, GREEN, 0.10 + 0.22 * phase))
        else:
            self.status_halo.configure(fg_color=BACKGROUND)
        self.traffic_chart.draw()
        self.animation_after_id = self.root.after(50, self._animate_ui)

    def _append_log(self, text, level="info"):
        self._write_logs([(level, text)])

    def _write_logs(self, records):
        """只在主线程操作控件；保留最近约 1000 行。"""
        follow = self.log_box.yview()[1] >= 0.98
        self.log_box.configure(state="normal")
        for level, text in records:
            text = ANSI_ESCAPE.sub("", text).strip()
            for secret in sorted(self.secret_values, key=len, reverse=True):
                text = text.replace(secret, "******")
            tag, label = "info", "信息"
            if level in {"error", "fatal", "panic"}:
                tag, label = "error", "错误"
            elif level in {"warn", "warning"}:
                tag, label = "warning", "警告"
            for line in text.splitlines():
                self.log_box.insert("end", f"[{label}] {line}\n", (tag,))
        extra_lines = int(self.log_box.index("end-1c").split(".")[0]) - 1000
        if extra_lines > 0:
            self.log_box.delete("1.0", f"{extra_lines + 1}.0")
        if follow:
            self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _refresh_controls(self):
        active = self.process is not None
        managed = self.system_proxy.managed
        locked = (active or managed or self.startup_cleanup_pending
                  or self.starting or self.stopping or self.closing)
        for entry in self.config_entries:
            entry.configure(state="disabled" if locked else "normal")
        stop = active or managed or self.startup_cleanup_pending
        button_text = "停止代理" if stop else "启动代理"
        button_color, hover_color = (RED, "#DC2626") if stop else (BLUE, "#2563EB")
        if self.proxy_error:
            status, dot = "设置恢复失败", RED
        elif self.stopping:
            status, dot, button_text = "正在停止", AMBER, "正在停止…"
        elif self.starting:
            status, dot, button_text = "正在启动", AMBER, "取消启动"
        elif self.failed:
            status, dot = "连接已停止 · 出现错误", RED
        elif active:
            status, dot = ("运行中", GREEN) if managed else ("系统代理已恢复", AMBER)
        else:
            status, dot = "未启动", RED
        self.status.set(status)
        self.breathing = dot == GREEN
        self.status_dot.configure(fg_color=dot)
        self.status_label.configure(text_color=dot if dot != RED else MUTED)
        self.control_button.configure(
            text=button_text, fg_color=button_color, hover_color=hover_color,
            state="disabled" if self.stopping or self.closing else "normal"
        )

    def toggle_proxy(self):
        if self.process is not None or self.system_proxy.managed or self.startup_cleanup_pending:
            self.stop_proxy()
        else:
            self.start_proxy()

    def start_proxy(self):
        if (self.process is not None or self.system_proxy.managed or self.startup_cleanup_pending
                or self.starting or self.stopping or self.closing):
            return
        values = {
            "vps_ip": self.host.get(), "port": self.port.get(),
            "username": self.username.get(), "password": self.password.get(),
        }
        if values["password"]:
            self.secret_values.update((values["password"], quote(values["password"], safe="")))
        try:
            self.config_store.save(values)
        except (OSError, ValueError):
            self._append_log("配置保存失败，请检查程序目录的写入权限；本次仍尝试启动。", "warning")
        try:
            password = values["password"]
            forward_url = build_forward_url(
                values["vps_ip"], values["port"], values["username"], password
            )
        except ValueError as exc:
            self.failed = True
            self._append_log(f"配置错误：{exc}", "error")
            self._refresh_controls()
            return
        self.secret_values.update((password, quote(password, safe="")))
        if not GOST_EXE.is_file():
            self.failed = True
            self._append_log(f"启动失败：找不到 {GOST_EXE}", "error")
            self._refresh_controls()
            return
        try:
            ensure_listener_available()
            process = subprocess.Popen(
                [str(GOST_EXE), f"-L={LISTEN_URL}", f"-F={forward_url}"],
                cwd=str(BASE_DIR),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                shell=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except OSError as exc:
            self.failed = True
            self._append_log(f"启动失败，请确认本地 {LOCAL_ADDRESS[1]} 端口可用：{exc}", "error")
            self._refresh_controls()
            return

        self.process = process
        self.starting = True
        self.failed = False
        self.proxy_error = False
        self.cancel_start = threading.Event()
        self._append_log("正在启动本地代理…")
        self._refresh_controls()
        # 捕获本次进程对象，避免重启时读取到另一次启动的输出。
        threading.Thread(
            target=self._read_output, args=(process,), daemon=True,
            name="gost-output"
        ).start()
        threading.Thread(
            target=self._startup_worker, args=(process, self.cancel_start),
            daemon=True, name="gost-startup"
        ).start()

    def _startup_worker(self, process, cancelled):
        try:
            if wait_for_listener(process, cancelled):
                self.events.put(("ready", process, None))
        except (OSError, RuntimeError) as exc:
            self.events.put(("startup_error", process, str(exc)))

    def _disable_system_proxy(self):
        managed = self.system_proxy.managed
        try:
            if self.startup_cleanup_pending:
                self.system_proxy.clear_stale_proxy()
            self.system_proxy.disable()
        except OSError as exc:
            self.proxy_error = True
            self.closing = False
            self._append_log(f"恢复系统代理失败，请再次点击停止：{exc}", "error")
            return False
        self.proxy_error = False
        self.startup_cleanup_pending = False
        if managed:
            self._append_log("系统代理已恢复。")
        return True

    def _read_output(self, process):
        """工作线程只写队列，绝不直接操作 Tk 控件。"""
        try:
            with process.stdout as stream:
                for line in stream:
                    record = parse_gost_log(line)
                    if record is not None:
                        # 日志和控制事件分开，输出过多也不会拖延停止/退出事件。
                        try:
                            self.log_events.put_nowait(record)
                        except queue.Full:
                            try:
                                self.log_events.get_nowait()
                            except queue.Empty:
                                pass
                            self.log_events.put_nowait(record)
        except (OSError, ValueError) as exc:
            self.events.put(("reader_error", process, f"读取日志失败：{exc}"))
        finally:
            code = process.wait()
            self.events.put(("exit", process, code))

    def stop_proxy(self):
        if self.stopping:
            return
        self.cancel_start.set()
        self.starting = False
        # 恢复代理与终止进程分别尝试，不能因注册表失败而跳过杀进程。
        self._disable_system_proxy()
        if self.process is None:
            self._refresh_controls()
            return
        self.stopping = True
        self._append_log("正在停止代理…")
        self._refresh_controls()
        # 等待进程退出不能放在按钮回调中，否则界面会卡住。
        threading.Thread(
            target=self._stop_worker, args=(self.process,),
            daemon=False, name="gost-stop"
        ).start()

    def _stop_worker(self, process):
        error = None
        try:
            terminate_and_wait(process)
        except (OSError, subprocess.TimeoutExpired) as exc:
            error = str(exc)
        finally:
            self.events.put(("stop_done", process, error))

    def _poll_events(self):
        self.after_id = None
        # 分批处理，持续输出日志时仍能响应按钮和关闭操作。
        for _ in range(40):
            try:
                kind, process, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "reader_error":
                self._append_log(value, "error")
            elif (kind == "ready" and process is self.process and self.starting
                  and not self.stopping and not self.closing):
                if process.poll() is None:
                    try:
                        self.system_proxy.enable()
                    except OSError as exc:
                        self.failed = True
                        self._append_log(f"开启系统代理失败：{exc}", "error")
                        self.stop_proxy()
                    else:
                        self.starting = False
                        self._append_log("代理已就绪，系统代理已开启。")
            elif (kind == "startup_error" and process is self.process
                  and self.starting):
                self.failed = True
                self._append_log(f"启动失败：{value}", "error")
                self.stop_proxy()
            elif kind == "exit" and process is self.process:
                unexpected = not self.cancel_start.is_set()
                self.cancel_start.set()
                self.process = None
                self.starting = False
                self._disable_system_proxy()
                if unexpected:
                    self.failed = True
                    self._append_log(f"代理进程意外退出，退出码：{value}。请检查节点或网络。", "error")
                else:
                    self._append_log("代理已停止。")
            elif kind == "stop_done":
                self.stopping = False
                if value:
                    # 清理失败时保留窗口和进程引用，允许再次停止。
                    self.closing = False
                    self.failed = True
                    self._append_log(f"停止失败：{value}", "error")

        records = []
        for _ in range(100):
            try:
                records.append(self.log_events.get_nowait())
            except queue.Empty:
                break
        if records:
            self._write_logs(records)
        self._refresh_controls()
        if (self.closing and self.process is None and not self.stopping
                and not self.system_proxy.managed and not self.startup_cleanup_pending):
            self._destroy_window()
            return
        self.after_id = self.root.after(50, self._poll_events)

    def _destroy_window(self):
        # CTk 还会安排缩放和滚动条检查；退出前一并取消这些定时回调。
        for callback in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            # Tcl 命令仍交给各自控件销毁，避免跨控件 after_cancel 重复删除。
            self.root.tk.call("after", "cancel", callback)
        self.after_id = None
        self.traffic_after_id = None
        self.animation_after_id = None
        self.root.destroy()

    def on_close(self):
        if self.closing:
            return
        self.closing = True
        self.stop_proxy()
        if not self.closing:
            return
        if (self.process is None and not self.stopping
                and not self.system_proxy.managed and not self.startup_cleanup_pending):
            self._destroy_window()
            return
        self._append_log("正在清理，完成后关闭窗口…")
        self._refresh_controls()


def main():
    if os.name != "nt":
        print("本程序仅支持 Windows。", file=sys.stderr)
        return
    ctk.set_appearance_mode("light")
    ctk.set_default_color_theme("blue")
    root = ctk.CTk(fg_color=BACKGROUND)
    app = ProxyApp(root)
    try:
        root.mainloop()
    finally:
        # 为主循环异常结束提供最后一道清理；顺序仍是先关闭系统代理。
        app.cancel_start.set()
        try:
            app.system_proxy.disable()
        finally:
            if app.process is not None:
                terminate_and_wait(app.process)


if __name__ == "__main__":
    main()
