# -*- coding: utf-8 -*-

import os
import sys
import ttkbootstrap as tb
from ttkbootstrap.constants import *
from tkinterdnd2 import DND_FILES, TkinterDnD
from ttkbootstrap.icons import Icon
import tkinter as tk
from tkinter import filedialog, scrolledtext, messagebox
from io import BytesIO
from threading import Event
import mimetypes
import requests
from datetime import datetime
import configparser
from utils import is_valid_host, get_filename_suffix, run_async, get_idle_seconds, decode_response_content

# ============================
# 网络请求客户端（网络访问层）
# ============================
class RequestClient:
    HOST = ""    # 由配置窗口设置
    
    def __init__(self, error_handler=None):
        self.error_handler = error_handler
        self.session = requests.Session()

    def _build_base_urls(self):
        base = f"http://{self.HOST}/cloudcenter/conversionNew"
        return {
            "resolve": f"{base}/resolveCode",
            "upload": f"{base}/uploadFile",
            "file_list": f"{base}/getFileListForDownCode",
            "download": f"{base}/downLoadFile",
        }

    def _build_cookies(self):
        return {"_systemType_": "_NANJING_"}

    def _build_headers(self, *, x_requested=False, content_type=False):
        headers = {
            "Origin": f"http://{self.HOST}",
            "Referer": f"http://{self.HOST}/cloudcenter/nj_home.html",
            "accept-language": "zh-CN,zh;q=0.9",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/142.0.0.0 Safari/537.36"
            ),
        }
        if x_requested:
            headers["x-requested-with"] = "XMLHttpRequest"
        if content_type:
            headers["content-type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        return headers
            
    def safe_request(self, method, url, **kwargs):
        try:
            resp = self.session.request(method, url, timeout=5, **kwargs)
            
            _raw_json = resp.json

            def _safe_json():
                try:
                    return _raw_json()
                except Exception:
                    return {}
            resp.json = _safe_json

            return resp
        except Exception as e:
            if self.error_handler:
                self.error_handler(e, url)
            return type(
                "Resp",
                (object,),
                {
                    "status_code": 500,
                    "json": staticmethod(lambda: {}),
                    "content": b"",
                    "iter_content": staticmethod(lambda chunk_size=8192: []),
                },
            )()

    def resolve_code(self, code_value):
        urls = self._build_base_urls()
        return self.safe_request(
            "post",
            urls["resolve"],
            data={"code": code_value},
            cookies=self._build_cookies(),
            headers=self._build_headers(x_requested=True, content_type=True),
        )

    def upload_file(self, code_value, file_name, file_size, file_obj):
        urls = self._build_base_urls()
        mime = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
        data = {
            "name": file_name,
            "code": code_value,
            "hash": "",
            "size": file_size,
            "fileName": file_name,
        }
        files = {"Filedata": (file_name, file_obj, mime)}
        return self.safe_request(
            "post",
            urls["upload"],
            data=data,
            files=files,
            cookies=self._build_cookies(),
            headers=self._build_headers(),
        )

    def get_file_list(self, code_value):
        urls = self._build_base_urls()
        params = {"code": code_value, "order": "ctime", "asc": "desc", "_": int(datetime.now().timestamp() * 1000)}
        return self.safe_request(
            "get", urls["file_list"], params=params, cookies=self._build_cookies(), headers=self._build_headers(x_requested=True)
        )

    def download_file(self, file_ids):
        urls = self._build_base_urls()
        return self.safe_request(
            "post",
            urls["download"],
            data={"fileIds": file_ids},
            cookies=self._build_cookies(),
            headers=self._build_headers(content_type=True),
            stream=True,
        )


# ============================
# 配置与持久化
# ============================
class ConfigManager:
    def __init__(self, config_path: str, logger=None):
        self.config_path = config_path
        self._config = configparser.ConfigParser()
        self.logger = logger

    def _log(self, msg: str):
        if self.logger:
            self.logger(msg)

    def _ensure_sections(self):
        if not self._config.has_section("server"):
            self._config.add_section("server")
        if not self._config.has_section("session"):
            self._config.add_section("session")

    def load_all(self) -> dict:
        result = {"host": "", "code": ""}
        if not os.path.exists(self.config_path):
            return result

        try:
            self._config.read(self.config_path, encoding="utf-8")
            result["host"] = self._config.get("server", "host", fallback="").strip()
            result["code"] = self._config.get("session", "code", fallback="").strip()
        except Exception as e:
            self._log(f"[配置] 读取配置文件时发生错误：{e}")
        return result

    def save(self, host: str = None, code: str = None):
        if os.path.exists(self.config_path) and not self._config.sections():
            try:
                self._config.read(self.config_path, encoding="utf-8")
            except Exception:

                self._config = configparser.ConfigParser()

        self._ensure_sections()

        if host is not None:
            self._config.set("server", "host", host)
        if code is not None:
            self._config.set("session", "code", code)

        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                self._config.write(f)
            if host is not None:
                self._log(f"[配置] 已保存服务器地址到配置文件：{self.config_path}")
            if code is not None:
                if code:
                    self._log("[配置] 已保存验证码到配置文件")
                else:
                    self._log("[配置] 已清空已保存的验证码")
        except Exception as e:
            self._log(f"[配置] 保存配置文件失败：{e}")

    def save_host(self, host: str):
        self.save(host=host)

    def save_code(self, code: str):
        self.save(code=code)

# ============================
# 主窗口类（界面与业务逻辑层）
# ============================
class App(TkinterDnD.Tk):
    BASE_TITLE = "税务云文件中转客户端"

    def __init__(self):
        super().__init__()
        self.style = tb.Style("flatly")
        self.title(self.BASE_TITLE)
        self.iconphoto(True, tk.PhotoImage(data=Icon.icon))
        width, height = 1100, 750
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
        x, y = (screen_w - width) // 2, (screen_h - height) // 2
        self.geometry(f"{width}x{height}+{x}+{y}")

        self.client = RequestClient(self._on_request_error)
        config_path = os.path.join(sys.path[0], "config.ini")
        self.config_manager = ConfigManager(config_path, logger=self.append_log)
        
        self.locked = True
        self.monitor_thread_started = False
        self.stop_event = Event()
        self.current_code = ""

        self.idle_threshold = 60
        self._idle_logged = False

        self._build_ui()
        self.set_locked(True)
        self.protocol("WM_DELETE_WINDOW", self._on_closing)
        self._init_from_config()

    def _build_ui(self):
        container = tb.Frame(self, padding=12)
        container.pack(fill=BOTH, expand=True)

        top_frame = tb.Frame(container)
        top_frame.pack(fill=X, pady=(0, 12))

        top_card = tb.Labelframe(top_frame, text="验证码验证", bootstyle=INFO)
        top_card.pack(fill=X, padx=10, pady=2, ipady=6)

        lbl = tb.Label(top_card, text="上传验证码：", anchor="w")
        lbl.pack(side=LEFT, padx=(10, 6))

        vcmd = (self.register(self._validate_code), "%P")
        self.entry_code = tb.Entry(top_card, width=10, justify="center", validate="key", validatecommand=vcmd)
        self.entry_code.pack(side=LEFT, padx=(0, 10))

        self.btn_unlock = tb.Button(top_card, text="确定", bootstyle=PRIMARY, command=self.on_unlock_clicked)
        self.btn_unlock.pack(side=LEFT, padx=6)

        self.btn_confirm = tb.Button(top_card, text="上传文本", bootstyle=SUCCESS, command=self.on_confirm_clicked)
        self.btn_confirm.pack(side=LEFT, padx=6)

        self.btn_download = tb.Button(top_card, text="下载文件", bootstyle=WARNING, command=self.on_download_clicked)
        self.btn_download.pack(side=LEFT, padx=6)

        self.btn_host_config = tb.Button(
            top_card,
            text="配置地址",
            bootstyle=SECONDARY,
            command=lambda: self.show_host_config(reason="manual"),
        )
        self.btn_host_config.pack(side=RIGHT, padx=(0, 10))

        main_pane = tb.Frame(container)
        main_pane.pack(fill=BOTH, expand=True, padx=10, pady=(6, 0))

        text_card = tb.Labelframe(main_pane, text="文本输入区域", bootstyle=PRIMARY)
        text_card.pack(side=LEFT, fill=BOTH, expand=True, padx=(0, 8), pady=4)

        self.text_main = scrolledtext.ScrolledText(text_card, wrap="word", font=("Microsoft YaHei", 10), undo=True)
        self.text_main.pack(fill=BOTH, expand=True, padx=8, pady=8)

        right_card = tb.Labelframe(main_pane, text="拖拽上传文件", bootstyle=SECONDARY)
        right_card.pack(side=LEFT, fill=BOTH, expand=False, ipadx=8, ipady=8, pady=4)

        self.drop_area = tb.Label(right_card, text="将文件拖拽到此处（支持多个）", anchor=CENTER, justify=CENTER, bootstyle="info-subtle", padding=20)
        self.drop_area.pack(fill=BOTH, expand=True, padx=8, pady=8)
        try:
            self.drop_area.drop_target_register(DND_FILES)
            self.drop_area.dnd_bind("<<Drop>>", self.on_files_dropped)
        except Exception:
            btn_choose = tb.Button(right_card, text="选择文件上传", bootstyle=INFO, command=self._choose_files)
            btn_choose.pack(pady=6)
            self.append_log("[提示] 系统未检测到拖拽支持，已启用文件选择按钮作为替代。")

        log_card = tb.Labelframe(container, text="日志输出", bootstyle=LIGHT)
        log_card.pack(fill=BOTH, expand=True, padx=10, pady=(12, 0))

        self.text_log = scrolledtext.ScrolledText(log_card, wrap="word", font=("Consolas", 9), state="disabled", height=8)
        self.text_log.pack(fill=BOTH, expand=True, padx=8, pady=8)

        status_frame = tb.Frame(self)
        status_frame.pack(fill=X, side=BOTTOM)
        self.lbl_status = tb.Label(status_frame, text="就绪", anchor="w", bootstyle="secondary")
        self.lbl_status.pack(fill=X, padx=8, pady=4)

        self.append_log("[启动] 界面加载完成，如首次使用请先点击“配置地址”设置 HOST，然后输入验证码并点击“确定”。")

    def _on_closing(self):
        if not self.locked and self.current_code:
            self.config_manager.save_code(self.current_code)
        else:
            self.config_manager.save_code("")

        self.stop_event.set()
        self.destroy()

    def _is_host_configured(self) -> bool:
        return bool(getattr(self.client, "HOST", "").strip())

    def _init_from_config(self):
        config = self.config_manager.load_all()
        host = config.get("host", "")
        saved_code = config.get("code", "")

        if not host:
            self.append_log("[配置] 未检测到服务器地址(HOST)，请先点击“配置地址”进行设置。")
            self.after(200, lambda: self.show_host_config(reason="startup"))
            return

        self.client.HOST = host
        self.append_log(f"[配置] 已从配置文件读取服务器地址：{host}")

        if saved_code and len(saved_code) == 6 and saved_code.isdigit():
            self.entry_code.delete(0, 'end')
            self.entry_code.insert(0, saved_code)
            self.append_log("[配置] 已从配置文件读取上次的验证码，正在自动验证...")
            self.after(100, self.on_unlock_clicked)

    def ensure_host_configured(self, auto_popup ):
        if self._is_host_configured():
            return True
        self.append_log("[配置] 未检测到服务器地址(HOST)，请先点击“配置地址”进行设置。")
        if auto_popup:
            self.after(0, lambda: self.show_host_config(reason="runtime"))
        return False

    def show_host_config(self, reason: str = "manual"):
        win = tb.Toplevel(self)
        win.withdraw()
        win.title("配置服务器地址 (HOST)")
        self.update_idletasks()
        width, height = 420, 220
        parent_x, parent_y = self.winfo_x(), self.winfo_y()
        parent_w, parent_h = self.winfo_width(), self.winfo_height()
        x = parent_x + (parent_w - width) // 2
        y = parent_y + (parent_h - height) // 2
        win.geometry(f"{width}x{height}+{max(x, 0)}+{max(y, 0)}")

        frame = tb.Frame(win, padding=15)
        frame.pack(fill=BOTH, expand=True)

        if reason == "startup":
            tip = (
                "未检测到配置文件或其中未配置服务器地址(HOST)。\n"
                "请先配置服务器地址后再使用客户端功能。"
            )
        elif reason == "runtime":
            tip = (
                "当前尚未配置服务器地址(HOST)，或配置无效。\n"
                "请先完成以下配置。"
            )
        else:
            tip = (
                "当前已配置服务器地址(HOST)。\n"
                "如需修改完成以下配置。"
            )

        lbl_tip = tb.Label(frame, text=tip, anchor="w", justify="left")
        lbl_tip.pack(fill=X, pady=(0, 10))

        lbl_host = tb.Label(frame, text="服务器地址（HOST）：", anchor="w")
        lbl_host.pack(fill=X)

        entry_host = tb.Entry(frame)
        entry_host.pack(fill=X, pady=(4, 8))

        current_host = getattr(self.client, "HOST", "").strip()
        if current_host:
            entry_host.insert(0, current_host)

        lbl_example = tb.Label(
            frame,
            text="示例：192.168.1.1 或 example.com",
            bootstyle="secondary",
            anchor="w",
            justify="left",
        )
        lbl_example.pack(fill=X, pady=(0, 12))

        btn_frame = tb.Frame(frame)
        btn_frame.pack(fill=X, pady=(4, 0))

        def on_save():
            host = entry_host.get().strip()
            if not host:
                messagebox.showwarning("配置服务器地址", "服务器地址不能为空，请输入一个有效的 HOST。")
                return

            if not is_valid_host(host):
                messagebox.showwarning("配置服务器地址", "服务器地址格式不正确，请重新输入。")
                return

            self.client.HOST = host
            self.config_manager.save_host(host)
            self.append_log(f"[配置] 已设置服务器地址：{host}")
            win.destroy()

        btn_save = tb.Button(btn_frame, text="保存", bootstyle=SUCCESS, command=on_save)
        btn_save.pack(side=LEFT, padx=(0, 6))

        def on_cancel():
            if not self._is_host_configured():
                self.append_log("[配置] 未完成服务器地址配置，客户端功能暂不可用。")
            win.destroy()

        btn_cancel = tb.Button(btn_frame, text="取消", bootstyle=SECONDARY, command=on_cancel)
        btn_cancel.pack(side=RIGHT)
        self.show_modal(win)

    def _validate_code(self, new_value: str) -> bool:
        if len(new_value) > 6:
            return False
        if new_value == "":
            return True
        return new_value.isdigit()

    def _choose_files(self):
        files = filedialog.askopenfilenames(parent=self, title="选择要上传的文件")
        if files:
            for f in files:
                self.append_log(f"[上传] 选择文件：{os.path.basename(f)}")
                self.upload_async(f)

    def set_locked(self, locked: bool):
        self.locked = locked
        state = "disabled" if locked else "normal"
        self.btn_confirm.config(state=state)
        self.btn_download.config(state=state)
        if locked:
            self.drop_area.config(text="请先验证验证码以启用拖拽功能", bootstyle="secondary")
            self._set_unlock_button_default()
            self._update_title_status("")  # clear code display
        else:
            self.drop_area.config(text="将文件拖拽到此处（支持多个）", bootstyle="info-subtle")

    def append_log(self, message: str):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.text_log.configure(state="normal")
        self.text_log.insert("end", f"[{timestamp}] {message}\n")
        self.text_log.see("end")
        self.text_log.configure(state="disabled")
        short = message if len(message) < 80 else message[:77] + "..."
        self.lbl_status.config(text=short)

    def _on_request_error(self, exception, url):
        msg = f"网络请求异常：{exception} - {url}"
        self.append_log(msg)

    def on_unlock_clicked(self):
        if not self.ensure_host_configured(auto_popup=True):
            return

        code_value = self.entry_code.get().strip()
        if len(code_value) != 6:
            self.append_log("[验证] 验证码必须为6位数字，请检查。")
            return
        if not self.monitor_thread_started:
            self.monitor_thread_started = True
            self.stop_event.clear()
            run_async(self._monitor_check_loop, code_value)
            self.append_log("[验证] 已开始轮询验证。")
        else:
            self.append_log("[验证] 已在轮询中。")

    def _monitor_check_loop(self, code_value):
        self.current_code = code_value
        try:
            while not self.stop_event.is_set():
                idle_seconds = get_idle_seconds()

                if idle_seconds is not None and idle_seconds > self.idle_threshold:
                    if not self._idle_logged:
                        self._idle_logged = True
                        self.after(0, lambda: self.append_log(f"[监控] 检测到用户已空闲超过 {self.idle_threshold} 秒，暂停验证码轮询。"))
                    if self.stop_event.wait(1):
                        break
                    continue
                else:
                    if self._idle_logged:
                        self._idle_logged = False
                        self.after(0, lambda: self.append_log("[监控] 检测到用户恢复活动，恢复验证码轮询。"))

                result = self.check_code(code_value)
                if not result:
                    break

                self.stop_event.wait(60)
        finally:
            self.monitor_thread_started = False


    def check_code(self, code_value):
        resp = self.client.resolve_code(code_value)
        if resp.status_code == 200:
            json_data = resp.json()
            if self.locked:
                if json_data.get('success'):
                    self.after(0, lambda: self.set_locked(False))
                    self.after(0, lambda: self.append_log("[验证] 成功！文本输入框、上传和拖拽区域已启用。"))
                    self.after(0, lambda: self._set_unlock_button_enabled(code_value))
                    self.after(0, lambda: self.entry_code.config(state="readonly"))
                else:
                    self.after(0, lambda: self.append_log(f"[验证] 失败：{json_data.get('msg')}"))
                    self.after(0, lambda: self.entry_code.delete(0, 'end'))
                    return False
            else:
                if not json_data.get('success'):
                    self.after(0, lambda: self.append_log("[验证] 失败！请重新输入验证码。"))
                    self.after(0, lambda: self.set_locked(True))
                    self.after(0, lambda: self._set_unlock_button_default())
                    self.after(0, lambda: self.entry_code.config(state="normal"))
                    self.after(0, lambda: self.entry_code.delete(0, 'end'))
                    return False
        else:
            self.after(0, lambda: self.append_log("[验证] 失败！服务器故障或服务器地址错误。"))
        return True

    def stop_monitor(self):
        self.stop_event.set()
        self.set_locked(True)
        self.btn_unlock.config(text="确定", state="normal", bootstyle=PRIMARY, command=self.on_unlock_clicked)
        self.entry_code.config(state="normal")
        self.current_code = ""
        self._update_title_status("")

    def upload_file(self, file_path=None, override_name=None):
        code_value = self.entry_code.get()
        if file_path:
            file_name = override_name or os.path.basename(file_path)
            file_size = os.path.getsize(file_path)
            with open(file_path, "rb") as f:
                resp = self.client.upload_file(code_value, file_name, file_size, f)
        else:
            text_value = self.text_main.get("1.0", "end-1c")
            file_bytes = text_value.encode("utf-8")
            file_name = override_name or f'文本{get_filename_suffix()}.txt'
            file_size = len(file_bytes)
            file_obj = BytesIO(file_bytes)
            resp = self.client.upload_file(code_value, file_name, file_size, file_obj)
        return resp, file_name

    def _upload_async_core(self, file_path=None):
        self.after(0, lambda: self.btn_confirm.config(state="disabled"))
        self.after(0, lambda: self.btn_download.config(state="disabled"))

        def _next_name(name, n):
            base, ext = os.path.splitext(name)
            return f"{base}({n}){ext}"

        if file_path:
            origin_name = os.path.basename(file_path)
        else:
            origin_name = f'文本{get_filename_suffix()}.txt'

        attempt = 0
        while True:
            if attempt == 0:
                override_name = None
            else:
                override_name = _next_name(origin_name, attempt)

            resp, file_name = self.upload_file(file_path, override_name=override_name)

            need_retry = False
            msg = None
            if resp.status_code == 200:
                json_data = resp.json()
                if json_data.get('success'):
                    self.append_log(f"[上传] 成功，文件名为「{file_name}」")
                else:
                    msg = json_data.get("msg")
                    if msg == "中转上传文件中已存在同名文件":
                        attempt += 1
                        need_retry = True
                    else:
                        self.append_log(f"[上传] 失败，{msg}。")
            else:
                self.append_log(f"[上传] 失败！服务器故障或服务器地址错误。")

            if need_retry:
                self.after(0, lambda fn=file_name: self.append_log(f"[上传] 检测到同名，自动更名后重试：{fn}"))
                continue

            break

        self.after(0, lambda: self.btn_confirm.config(state="normal"))
        self.after(0, lambda: self.btn_download.config(state="normal"))

    def upload_async(self, file_path=None):
        run_async(self._upload_async_core, file_path)

    def on_confirm_clicked(self):
        if self.locked:
            self.append_log("[上传] 功能尚未启用，请先输入验证码并确认。")
            return

        if not self.ensure_host_configured(auto_popup=True):
            return

        text_value = self.text_main.get("1.0", "end-1c")
        if text_value.strip():
            self.append_log("[上传] 正在上传文本内容...")
            self.upload_async(None)
        else:
            self.append_log("[上传] 失败，当前文本框为空。")

    def on_files_dropped(self, event):
        if self.locked:
            self.append_log("[拖拽] 功能尚未启用，请先输入验证码并确认。")
            return

        if not self.ensure_host_configured(auto_popup=True):
            return

        try:
            paths = self.tk.splitlist(event.data)
        except Exception:
            paths = [event.data]
        for p in paths:
            p = p.strip()
            if not p:
                continue
            self.append_log(f"[上传] 正在上传文件：{os.path.basename(p)} ...")
            self.upload_async(p)

    def on_download_clicked(self):
        if self.locked:
            self.append_log("[下载] 功能尚未启用，请先输入验证码并确认。")
            return

        if not self.ensure_host_configured(auto_popup=True):
            return

        code_value = self.entry_code.get()
        if not code_value:
            self.append_log("[下载] 请先输入验证码。")
            return
        self.append_log("[下载] 正在查询可下载文件列表...")

        run_async(self._download_list_async, code_value)

    def _download_list_async(self, code_value):
        resp = self.client.get_file_list(code_value)
        if resp.status_code != 200:
            self.after(0, lambda: self.append_log("[下载] 查询失败！服务器故障或服务器地址错误。"))
            return
        json_data = resp.json()
        if not json_data.get("success"):
            self.after(0, lambda: self.append_log("[下载] 当前验证码下没有可下载的文件。"))
            return
        files = json_data.get("data") or []
        if not files:
            self.after(0, lambda: self.append_log("[下载] 当前验证码下没有可下载的文件。"))
            return

        self.after(0, lambda: self.show_download_dialog(files))
        
    def show_modal(self, win):
        win.transient(self)

        def _release_grab_if_needed():
            try:
                cur = win.grab_current()
                if cur == win:
                    win.grab_release()
            except tk.TclError:
                pass

        def on_modal_unmap(event=None):
            _release_grab_if_needed()

        def on_modal_map(event=None):
            try:
                win.after(0, lambda: (win.grab_set(), win.lift(), win.focus_force()))
            except tk.TclError:
                pass

        win.bind("<Unmap>", on_modal_unmap)
        win.bind("<Map>", on_modal_map)

        win.deiconify()
        win.lift()
        win.focus_force()
        win.grab_set()

        def on_close():
            _release_grab_if_needed()
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", on_close)
        self.wait_window(win)

    def show_download_dialog(self, files):
        win = tb.Toplevel(self)
        win.title("选择要下载的文件")
        self.update_idletasks()
        width, height = 560, 420
        parent_x, parent_y = self.winfo_x(), self.winfo_y()
        parent_w, parent_h = self.winfo_width(), self.winfo_height()
        x, y = parent_x + (parent_w - width) // 2, parent_y + (parent_h - height) // 2
        win.geometry(f"{width}x{height}+{max(x,0)}+{max(y,0)}")

        lbl = tb.Label(win, text="请选择要下载的文件（可按 Ctrl/Shift 多选）：")
        lbl.pack(padx=10, pady=(10, 6), anchor="w")

        frame_list = tb.Frame(win)
        frame_list.pack(fill=BOTH, expand=True, padx=10, pady=(0, 10))

        scrollbar = tb.Scrollbar(frame_list, orient="vertical")
        listbox = tk.Listbox(frame_list, selectmode="extended", yscrollcommand=scrollbar.set)
        scrollbar.config(command=listbox.yview)
        listbox.pack(side="left", fill=BOTH, expand=True)
        scrollbar.pack(side="right", fill="y")

        id_name_list = []
        for f in files:
            file_id = str(f.get("id"))
            file_name = f.get("fileName") or file_id
            display_text = file_name
            id_name_list.append((file_id, file_name))
            listbox.insert("end", display_text)

        TEXT_EXTENSIONS = {
            '.txt', '.js', '.html', '.htm', '.py', '.cpp', '.c', '.h', '.hpp',
            '.css', '.json', '.xml', '.md', '.yaml', '.yml', '.ini', '.cfg', '.sh', '.bat',
            '.java', '.cs', '.go', '.rs', '.php', '.rb', '.sql', '.log', '.csv'
        }

        btn_frame = tb.Frame(win)
        btn_frame.pack(fill="x", padx=10, pady=(0, 10))

        def on_download_selected():
            selection = listbox.curselection()
            if not selection:
                self.append_log("[下载] 请先在列表中选择至少一个文件。")
                return
            selected_ids = []
            selected_names = []
            for idx in selection:
                fid, fname = id_name_list[idx]
                selected_ids.append(fid)
                selected_names.append(fname)
            if len(selected_ids) == 1:
                display_name = selected_names[0]
            else:
                display_name = f"选中文件打包_{get_filename_suffix()}.zip"

            self.download_files_async(selected_ids, display_name)
            win.destroy()

        def on_load_to_text():
            selection = listbox.curselection()
            if len(selection) != 1:
                return
            idx = selection[0]
            fid, fname = id_name_list[idx]
            self.load_file_to_text_async(fid, fname)
            win.destroy()

        def update_load_button_state(event=None):
            selection = listbox.curselection()
            if len(selection) == 1:
                idx = selection[0]
                _, fname = id_name_list[idx]
                ext = os.path.splitext(fname)[1].lower()
                if ext in TEXT_EXTENSIONS:
                    btn_load_text.config(state="normal")
                    return
            btn_load_text.config(state="disabled")

        btn_download = tb.Button(btn_frame, text="下载选中文件", bootstyle=SUCCESS, command=on_download_selected)
        btn_download.pack(side=LEFT)

        btn_load_text = tb.Button(btn_frame, text="加载到文本框", bootstyle=PRIMARY, command=on_load_to_text, state="disabled")
        btn_load_text.pack(side=LEFT, padx=(10, 0))

        btn_close = tb.Button(btn_frame, text="关闭", bootstyle=SECONDARY, command=win.destroy)
        btn_close.pack(side=RIGHT)

        listbox.bind("<<ListboxSelect>>", update_load_button_state)
        self.show_modal(win)

    def download_files_async(self, file_ids, display_name: str):

        def worker():
            ids_str = ",".join(file_ids)
            self.after(0, lambda: self.append_log(f"[下载] 开始下载文件：{display_name} ..."))
            resp = self.client.download_file(ids_str)

            def ui_after_resp():
                if resp.status_code != 200:
                    self.append_log(f"[下载] 失败！服务器返回状态码 {resp.status_code}。")
                    return

                default_name = display_name
                save_path = filedialog.asksaveasfilename(parent=self, title="选择文件保存位置", initialfile=default_name)
                if not save_path:
                    self.append_log(f"[下载] 已取消保存「{display_name}」。")
                    return
                try:
                    with open(save_path, "wb") as f:
                        for chunk in resp.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                    self.append_log(f"[下载] 完成，文件「{display_name}」已保存到：{save_path}")
                except Exception as e:
                    self.append_log(f"[下载] 保存失败：{e}")

            self.after(0, ui_after_resp)

        run_async(worker)

    def load_file_to_text_async(self, file_id: str, file_name: str):

        def worker():
            self.after(0, lambda: self.append_log(f"[加载] 正在加载文件：{file_name} ..."))
            resp = self.client.download_file(file_id)

            def ui_after_resp():
                if resp.status_code != 200:
                    self.append_log(f"[加载] 失败！服务器返回状态码 {resp.status_code}。")
                    return

                try:
                    content = resp.content
                    text = decode_response_content(content)
                    
                    if text is None:
                        self.append_log(f"[加载] 失败：无法解析文件编码。")
                        return

                    self.text_main.delete("1.0", "end")
                    self.text_main.insert("1.0", text)
                    self.append_log(f"[加载] 完成，文件「{file_name}」已加载到文本输入框。")

                except Exception as e:
                    self.append_log(f"[加载] 失败：{e}")

            self.after(0, ui_after_resp)

        run_async(worker)

    def _set_unlock_button_enabled(self, code_value: str):
        self._update_title_status(code_value)
        self.btn_unlock.config(text=f"重置", bootstyle=INFO, state="normal", command=self.on_reset_clicked)

    def _set_unlock_button_default(self):
        self.btn_unlock.config(text="确定", bootstyle=PRIMARY, state="normal", command=self.on_unlock_clicked)

    def on_reset_clicked(self):
        try:
            self.btn_unlock.config(text="重置中...", bootstyle=DANGER, state="disabled")
            self.append_log("[验证] 正在重置验证码并锁定界面...")
            self.stop_monitor()
            self.entry_code.config(state="normal")
            self.entry_code.delete(0, 'end')
            self.after(300, lambda: self._set_unlock_button_default())
            self.append_log("[验证] 已重置，已恢复到待验证状态。")
        except Exception as e:
            self.append_log(f"[验证] 重置失败：{e}")
            self._set_unlock_button_default()

    def _update_title_status(self, code_value: str):
        if code_value:
            self.title(f"{self.BASE_TITLE} — 验证码: {code_value}（已启用）")
        else:
            self.title(self.BASE_TITLE)

if __name__ == "__main__":
    app = App()
    app.mainloop()
