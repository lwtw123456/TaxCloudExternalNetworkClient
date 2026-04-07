# -*- coding: utf-8 -*-

import os
import sys
import mimetypes
import configparser
import tkinter as tk
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from threading import Event
from typing import Callable, List, Optional
from abc import ABC, abstractmethod
import requests
import ttkbootstrap as tb
from tkinter import filedialog, scrolledtext, messagebox
from tkinterdnd2 import DND_FILES, TkinterDnD
from ttkbootstrap.constants import *
from ttkbootstrap.icons import Icon

from utils import (
    normalize_host,
    get_filename_suffix,
    run_async,
    get_idle_seconds,
    decode_response_content,
)

@dataclass
class AppState:
    host: str = ""
    locked: bool = True
    current_code: str = ""
    monitor_thread_started: bool = False
    idle_threshold: int = 60
    idle_logged: bool = False

@dataclass
class VerificationResult:
    success: bool
    message: str
    valid: bool = False
    expired: bool = False

@dataclass
class UploadResult:
    success: bool
    message: str
    file_name: str = ""
    expired: bool = False

@dataclass
class FileItem:
    file_id: str
    file_name: str

@dataclass
class FileListResult:
    success: bool
    message: str
    files: List[FileItem] = field(default_factory=list)
    expired: bool = False

@dataclass
class DownloadBinaryResult:
    success: bool
    message: str
    content: bytes = b""

@dataclass
class LoadTextResult:
    success: bool
    message: str
    text: str = ""

@dataclass
class DownloadDialogSelection:
    action: str  # "download" / "load_text"
    file_ids: List[str] = field(default_factory=list)
    file_names: List[str] = field(default_factory=list)
    file_id: str = ""
    file_name: str = ""

@dataclass
class UIState:
    locked: bool
    current_code: str
    code_entry_readonly: bool
    unlock_button_mode: str  # "unlock" / "reset" / "resetting"

class Logger(ABC):
    @abstractmethod
    def log(self, message: str) -> None:
        pass


class Scheduler(ABC):
    @abstractmethod
    def run_background(self, func: Callable, *args, **kwargs) -> None:
        pass

    @abstractmethod
    def call_soon(self, func: Callable, *args, **kwargs) -> None:
        pass

    @abstractmethod
    def call_later(self, delay_ms: int, func: Callable, *args, **kwargs) -> None:
        pass

class TkScheduler(Scheduler):
    def __init__(self, root: tk.Misc):
        self.root = root

    def run_background(self, func: Callable, *args, **kwargs) -> None:
        run_async(func, *args, **kwargs)

    def call_soon(self, func: Callable, *args, **kwargs) -> None:
        self.root.after(0, lambda: func(*args, **kwargs))

    def call_later(self, delay_ms: int, func: Callable, *args, **kwargs) -> None:
        self.root.after(delay_ms, lambda: func(*args, **kwargs))

class RequestClient:
    def __init__(self, error_handler: Optional[Callable[[Exception, str], None]] = None):
        self.host = ""
        self.error_handler = error_handler
        self.session = requests.Session()

    def _build_base_urls(self):
        base = f"http://{self.host}/cloudcenter/conversionNew"
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
            "Origin": f"http://{self.host}",
            "Referer": f"http://{self.host}/cloudcenter/nj_home.html",
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
            resp = self.session.request(method, url, timeout=(3, 60), **kwargs)

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
                    "error": e,
                },
            )()

    def resolve_code(self, code_value: str):
        urls = self._build_base_urls()
        return self.safe_request(
            "post",
            urls["resolve"],
            data={"code": code_value},
            cookies=self._build_cookies(),
            headers=self._build_headers(x_requested=True, content_type=True),
        )

    def upload_file(self, code_value: str, file_name: str, file_size: int, file_obj):
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

    def get_file_list(self, code_value: str):
        urls = self._build_base_urls()
        params = {
            "code": code_value,
            "order": "ctime",
            "asc": "desc",
            "_": int(datetime.now().timestamp() * 1000),
        }
        return self.safe_request(
            "get",
            urls["file_list"],
            params=params,
            cookies=self._build_cookies(),
            headers=self._build_headers(x_requested=True),
        )

    def download_file(self, file_ids: str):
        urls = self._build_base_urls()
        return self.safe_request(
            "post",
            urls["download"],
            data={"fileIds": file_ids},
            cookies=self._build_cookies(),
            headers=self._build_headers(content_type=True),
            stream=True,
        )

class ConfigManager:
    def __init__(self, config_path: str, logger: Optional[Logger] = None):
        self.config_path = config_path
        self._config = configparser.ConfigParser()
        self.logger = logger

    def _log(self, msg: str):
        if self.logger:
            self.logger.log(msg)

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

    def save(self, host: Optional[str] = None, code: Optional[str] = None):
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

class VerificationService:
    def __init__(self, client: RequestClient):
        self.client = client

    def verify_code(self, code_value: str) -> VerificationResult:
        resp = self.client.resolve_code(code_value)
        if resp.status_code != 200:
            return VerificationResult(
                success=False,
                message="[验证] 失败！服务器故障或服务器地址错误。",
                valid=False,
            )

        json_data = resp.json()
        ok = bool(json_data.get("success"))
        msg = json_data.get("msg") or ""

        if ok:
            return VerificationResult(
                success=True,
                message="[验证] 成功！文本输入框、上传和拖拽区域已启用。",
                valid=True,
            )

        expired = ("失效" in msg) or ("无效" in msg)
        return VerificationResult(
            success=False,
            message=f"[验证] 失败：{msg or '验证码无效'}",
            valid=False,
            expired=expired,
        )


class TransferService:
    def __init__(self, client: RequestClient):
        self.client = client

    @staticmethod
    def _next_name(name: str, n: int) -> str:
        base, ext = os.path.splitext(name)
        return f"{base}({n}){ext}"

    def _upload_binary(self, code_value: str, file_name: str, data: bytes) -> UploadResult:
        file_obj = BytesIO(data)
        resp = self.client.upload_file(code_value, file_name, len(data), file_obj)

        if resp.status_code != 200:
            return UploadResult(
                success=False,
                message="[上传] 失败！服务器故障或服务器地址错误。",
            )

        json_data = resp.json()
        if json_data.get("success"):
            return UploadResult(
                success=True,
                message=f"[上传] 成功，文件名为「{file_name}」",
                file_name=file_name,
            )

        msg = json_data.get("msg") or ""
        if msg == "上传码已失效":
            return UploadResult(
                success=False,
                message="[上传] 失败，上传码已失效。",
                expired=True,
            )

        if msg == "中转上传文件中已存在同名文件":
            return UploadResult(
                success=False,
                message=f"[上传] 已存在同名文件「{file_name}」，准备自动更名重试。",
            )

        return UploadResult(
            success=False,
            message=f"[上传] 失败，{msg}。",
        )

    def upload_text(self, code_value: str, text_value: str) -> UploadResult:
        origin_name = f"文本{get_filename_suffix()}.txt"
        data = text_value.encode("utf-8")

        attempt = 0
        while True:
            upload_name = origin_name if attempt == 0 else self._next_name(origin_name, attempt)
            result = self._upload_binary(code_value, upload_name, data)

            if result.success:
                return result

            if "准备自动更名重试" in result.message:
                attempt += 1
                continue

            return result

    def upload_file(self, code_value: str, file_path: str) -> UploadResult:
        if not os.path.exists(file_path):
            return UploadResult(
                success=False,
                message=f"[上传] 失败，文件不存在：{file_path}",
            )

        origin_name = os.path.basename(file_path)
        with open(file_path, "rb") as f:
            data = f.read()

        attempt = 0
        while True:
            upload_name = origin_name if attempt == 0 else self._next_name(origin_name, attempt)
            result = self._upload_binary(code_value, upload_name, data)

            if result.success:
                return result

            if "准备自动更名重试" in result.message:
                attempt += 1
                continue

            return result

class DownloadService:
    TEXT_EXTENSIONS = {
        ".txt", ".js", ".html", ".htm", ".py", ".cpp", ".c", ".h", ".hpp",
        ".css", ".json", ".xml", ".md", ".yaml", ".yml", ".ini", ".cfg", ".sh", ".bat",
        ".java", ".cs", ".go", ".rs", ".php", ".rb", ".sql", ".log", ".csv"
    }

    def __init__(self, client: RequestClient):
        self.client = client

    def get_file_list(self, code_value: str) -> FileListResult:
        resp = self.client.get_file_list(code_value)
        if resp.status_code != 200:
            return FileListResult(
                success=False,
                message="[下载] 查询失败！服务器故障或服务器地址错误。",
            )

        json_data = resp.json()
        if not json_data.get("success"):
            msg = json_data.get("msg") or "当前验证码下没有可下载的文件。"
            expired = ("失效" in msg) or ("无效" in msg)
            return FileListResult(
                success=False,
                message=f"[下载] {msg}",
                expired=expired,
            )

        raw_files = json_data.get("data") or []
        if not raw_files:
            return FileListResult(
                success=False,
                message="[下载] 当前验证码下没有可下载的文件。",
            )

        files = [
            FileItem(
                file_id=str(item.get("id")),
                file_name=item.get("fileName") or str(item.get("id")),
            )
            for item in raw_files
        ]
        return FileListResult(
            success=True,
            message="[下载] 已查询到可下载文件列表。",
            files=files,
        )

    def download_binary(self, file_ids: List[str], display_name: str) -> DownloadBinaryResult:
        ids_str = ",".join(file_ids)
        resp = self.client.download_file(ids_str)

        if resp.status_code != 200:
            return DownloadBinaryResult(
                success=False,
                message=f"[下载] 失败！服务器返回状态码 {resp.status_code}。",
            )

        try:
            chunks = []
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    chunks.append(chunk)
            return DownloadBinaryResult(
                success=True,
                message=f"[下载] 文件「{display_name}」已下载完成。",
                content=b"".join(chunks),
            )
        except Exception as e:
            return DownloadBinaryResult(
                success=False,
                message=f"[下载] 读取下载内容失败：{e}",
            )

    def load_text_file(self, file_id: str, file_name: str) -> LoadTextResult:
        resp = self.client.download_file(file_id)
        if resp.status_code != 200:
            return LoadTextResult(
                success=False,
                message=f"[加载] 失败！服务器返回状态码 {resp.status_code}。",
            )

        try:
            text = decode_response_content(resp.content)
            if text is None:
                return LoadTextResult(
                    success=False,
                    message="[加载] 失败：无法解析文件编码。",
                )

            text = text.replace("\r\n", "\n").replace("\r", "\n")
            return LoadTextResult(
                success=True,
                message=f"[加载] 完成，文件「{file_name}」已加载到文本输入框。",
                text=text,
            )
        except Exception as e:
            return LoadTextResult(
                success=False,
                message=f"[加载] 失败：{e}",
            )

    def can_load_to_text(self, file_name: str) -> bool:
        ext = os.path.splitext(file_name)[1].lower()
        return ext in self.TEXT_EXTENSIONS

class AppView(ABC):
    @abstractmethod
    def apply_ui_state(self, ui_state: UIState) -> None:
        pass

    @abstractmethod
    def append_log(self, message: str) -> None:
        pass

    @abstractmethod
    def get_code_input(self) -> str:
        pass

    @abstractmethod
    def set_code_input(self, value: str) -> None:
        pass

    @abstractmethod
    def clear_code_input(self) -> None:
        pass

    @abstractmethod
    def get_main_text(self) -> str:
        pass

    @abstractmethod
    def set_main_text(self, value: str) -> None:
        pass

    @abstractmethod
    def show_warning(self, title: str, message: str) -> None:
        pass

    @abstractmethod
    def show_host_config_dialog(self, current_host: str, reason: str) -> Optional[str]:
        pass

    @abstractmethod
    def show_download_dialog(
        self,
        files: List[FileItem],
        can_load_checker: Callable[[str], bool]
    ) -> Optional[DownloadDialogSelection]:
        pass

    @abstractmethod
    def prompt_save_path(self, default_name: str) -> Optional[str]:
        pass

    @abstractmethod
    def choose_files(self) -> List[str]:
        pass

class UILogger(Logger):
    def __init__(self, view: AppView):
        self.view = view

    def log(self, message: str) -> None:
        self.view.append_log(message)

class AppController:
    def __init__(
        self,
        view: AppView,
        state: AppState,
        scheduler: Scheduler,
        logger: Logger,
        config_manager: ConfigManager,
        request_client: RequestClient,
        verification_service: VerificationService,
        transfer_service: TransferService,
        download_service: DownloadService,
    ):
        self.view = view
        self.state = state
        self.scheduler = scheduler
        self.logger = logger
        self.config_manager = config_manager
        self.request_client = request_client
        self.verification_service = verification_service
        self.transfer_service = transfer_service
        self.download_service = download_service

        self.stop_event = Event()

    # ---------- 初始化与收尾 ----------

    def initialize(self):
        self.view.apply_ui_state(self._build_ui_state())
        self.logger.log("[启动] 界面加载完成，如首次使用请先点击“配置地址”设置 HOST，然后输入验证码并点击“确定”。")

        config = self.config_manager.load_all()
        host = config.get("host", "")
        saved_code = config.get("code", "")

        if not host:
            self.logger.log("[配置] 未检测到服务器地址(HOST)，请先点击“配置地址”进行设置。")
            self.scheduler.call_later(200, self.open_host_config, "startup")
            return

        self.set_host(host)
        self.logger.log(f"[配置] 已从配置文件读取服务器地址：{host}")

        if saved_code and len(saved_code) == 6 and saved_code.isdigit():
            self.view.set_code_input(saved_code)
            self.logger.log("[配置] 已从配置文件读取上次的验证码，正在自动验证...")
            self.scheduler.call_later(100, self.on_unlock_requested)

    def shutdown(self):
        if not self.state.locked and self.state.current_code:
            self.config_manager.save_code(self.state.current_code)
        else:
            self.config_manager.save_code("")

        self.stop_event.set()

    # ---------- 状态与渲染 ----------

    def _build_ui_state(self) -> UIState:
        if self.state.locked:
            return UIState(
                locked=True,
                current_code="",
                code_entry_readonly=False,
                unlock_button_mode="unlock",
            )
        return UIState(
            locked=False,
            current_code=self.state.current_code,
            code_entry_readonly=True,
            unlock_button_mode="reset",
        )

    def _refresh_view(self):
        self.view.apply_ui_state(self._build_ui_state())

    def set_host(self, host: str):
        host = (host or "").strip()
        self.state.host = host
        self.request_client.host = host

    def is_host_configured(self) -> bool:
        return bool(self.request_client.host.strip())

    def ensure_host_configured(self, auto_popup: bool) -> bool:
        if self.is_host_configured():
            return True

        self.logger.log("[配置] 未检测到服务器地址(HOST)，请先点击“配置地址”进行设置。")
        if auto_popup:
            self.scheduler.call_soon(self.open_host_config, "runtime")
        return False

    def open_host_config(self, reason: str = "manual"):
        current_host = self.request_client.host.strip()
        host = self.view.show_host_config_dialog(current_host, reason)
        if host is None:
            if not self.is_host_configured():
                self.logger.log("[配置] 未完成服务器地址配置，客户端功能暂不可用。")
            return

        is_valid_host, normalized = normalize_host(host)
        if not is_valid_host:
            self.view.show_warning("配置服务器地址", "服务器地址格式不正确，请重新输入。")
            return

        self.set_host(normalized)
        self.config_manager.save_host(normalized)
        self.logger.log(f"[配置] 已设置服务器地址：{normalized}")

    def on_unlock_requested(self):
        if not self.ensure_host_configured(auto_popup=True):
            return

        code_value = self.view.get_code_input().strip()
        if len(code_value) != 6 or not code_value.isdigit():
            self.logger.log("[验证] 验证码必须为6位数字，请检查。")
            return

        if self.state.monitor_thread_started:
            self.logger.log("[验证] 已在轮询中。")
            return

        self.state.current_code = code_value
        self.state.monitor_thread_started = True
        self.state.idle_logged = False
        self.stop_event.clear()

        self.logger.log("[验证] 已开始轮询验证。")
        self.scheduler.run_background(self._monitor_check_loop, code_value)

    def _monitor_check_loop(self, code_value: str):
        try:
            while not self.stop_event.is_set():
                idle_seconds = get_idle_seconds()

                if idle_seconds is not None and idle_seconds > self.state.idle_threshold:
                    if not self.state.idle_logged:
                        self.state.idle_logged = True
                        self.scheduler.call_soon(
                            self.logger.log,
                            f"[监控] 检测到用户已空闲超过 {self.state.idle_threshold} 秒，暂停验证码轮询。"
                        )
                    if self.stop_event.wait(1):
                        break
                    continue
                else:
                    if self.state.idle_logged:
                        self.state.idle_logged = False
                        self.scheduler.call_soon(
                            self.logger.log,
                            "[监控] 检测到用户恢复活动，恢复验证码轮询。"
                        )

                result = self.verification_service.verify_code(code_value)
                if not result.valid:
                    self.scheduler.call_soon(self._handle_verify_failed_after_started, result)
                    break

                self.scheduler.call_soon(self._handle_verify_success_if_needed, code_value, result)

                if self.stop_event.wait(60):
                    break
        finally:
            self.state.monitor_thread_started = False

    def _handle_verify_success_if_needed(self, code_value: str, result: VerificationResult):
        if self.state.locked:
            self.state.locked = False
            self.state.current_code = code_value
            self._refresh_view()
            self.logger.log(result.message)

    def _handle_verify_failed_after_started(self, result: VerificationResult):
        if self.state.locked:
            self.logger.log(result.message)
            self.view.clear_code_input()
            return

        self.logger.log("[验证] 失败！请重新输入验证码。")
        self._reset_locked_state(clear_code=True)

    def on_reset_requested(self):
        try:
            self.view.apply_ui_state(
                UIState(
                    locked=self.state.locked,
                    current_code=self.state.current_code,
                    code_entry_readonly=False,
                    unlock_button_mode="resetting",
                )
            )
            self.logger.log("[验证] 正在重置验证码并锁定界面...")
            self._reset_locked_state(clear_code=True)
            self.logger.log("[验证] 已重置，已恢复到待验证状态。")
        except Exception as e:
            self.logger.log(f"[验证] 重置失败：{e}")
            self._refresh_view()

    def _reset_locked_state(self, clear_code: bool):
        self.stop_event.set()
        self.state.locked = True
        self.state.current_code = ""
        self.state.idle_logged = False
        if clear_code:
            self.view.clear_code_input()
        self._refresh_view()

    def on_upload_text_requested(self):
        if self.state.locked:
            self.logger.log("[上传] 功能尚未启用，请先输入验证码并确认。")
            return

        if not self.ensure_host_configured(auto_popup=True):
            return

        text_value = self.view.get_main_text()
        if not text_value.strip():
            self.logger.log("[上传] 失败，当前文本框为空。")
            return

        code_value = self.state.current_code or self.view.get_code_input().strip()
        self.logger.log("[上传] 正在上传文本内容...")
        self.scheduler.run_background(self._upload_text_worker, code_value, text_value)

    def _upload_text_worker(self, code_value: str, text_value: str):
        result = self.transfer_service.upload_text(code_value, text_value)
        self.scheduler.call_soon(self._handle_upload_result, result)

    def on_files_selected(self, paths: List[str]):
        if self.state.locked:
            self.logger.log("[拖拽] 功能尚未启用，请先输入验证码并确认。")
            return

        if not self.ensure_host_configured(auto_popup=True):
            return

        code_value = self.state.current_code or self.view.get_code_input().strip()
        for path in paths:
            if not path:
                continue
            self.logger.log(f"[上传] 正在上传文件：{os.path.basename(path)} ...")
            self.scheduler.run_background(self._upload_file_worker, code_value, path)

    def _upload_file_worker(self, code_value: str, file_path: str):
        result = self.transfer_service.upload_file(code_value, file_path)
        self.scheduler.call_soon(self._handle_upload_result, result)

    def _handle_upload_result(self, result: UploadResult):
        self.logger.log(result.message)
        if result.expired:
            self._reset_locked_state(clear_code=True)

    def on_download_requested(self):
        if self.state.locked:
            self.logger.log("[下载] 功能尚未启用，请先输入验证码并确认。")
            return

        if not self.ensure_host_configured(auto_popup=True):
            return

        code_value = self.state.current_code or self.view.get_code_input().strip()
        if not code_value:
            self.logger.log("[下载] 请先输入验证码。")
            return

        self.logger.log("[下载] 正在查询可下载文件列表...")
        self.scheduler.run_background(self._query_file_list_worker, code_value)

    def _query_file_list_worker(self, code_value: str):
        result = self.download_service.get_file_list(code_value)
        self.scheduler.call_soon(self._handle_file_list_result, result)

    def _handle_file_list_result(self, result: FileListResult):
        if not result.success:
            self.logger.log(result.message)
            if result.expired:
                self._reset_locked_state(clear_code=True)
            return

        selection = self.view.show_download_dialog(
            result.files,
            self.download_service.can_load_to_text
        )
        if not selection:
            return

        if selection.action == "download":
            if len(selection.file_ids) == 1:
                display_name = selection.file_names[0]
            else:
                display_name = f"选中文件打包_{get_filename_suffix()}.zip"

            self.logger.log(f"[下载] 开始下载文件：{display_name} ...")
            self.scheduler.run_background(
                self._download_binary_worker,
                selection.file_ids,
                display_name,
            )
            return

        if selection.action == "load_text":
            self.logger.log(f"[加载] 正在加载文件：{selection.file_name} ...")
            self.scheduler.run_background(
                self._load_text_worker,
                selection.file_id,
                selection.file_name,
            )

    def _download_binary_worker(self, file_ids: List[str], display_name: str):
        result = self.download_service.download_binary(file_ids, display_name)
        self.scheduler.call_soon(self._handle_download_binary_result, result, display_name)

    def _handle_download_binary_result(self, result: DownloadBinaryResult, display_name: str):
        if not result.success:
            self.logger.log(result.message)
            return

        save_path = self.view.prompt_save_path(display_name)
        if not save_path:
            self.logger.log(f"[下载] 已取消保存「{display_name}」。")
            return

        try:
            with open(save_path, "wb") as f:
                f.write(result.content)
            self.logger.log(f"[下载] 完成，文件「{display_name}」已保存到：{save_path}")
        except Exception as e:
            self.logger.log(f"[下载] 保存失败：{e}")

    def _load_text_worker(self, file_id: str, file_name: str):
        result = self.download_service.load_text_file(file_id, file_name)
        self.scheduler.call_soon(self._handle_load_text_result, result)

    def _handle_load_text_result(self, result: LoadTextResult):
        if not result.success:
            self.logger.log(result.message)
            return

        self.view.set_main_text(result.text)
        self.logger.log(result.message)

class App(TkinterDnD.Tk):
    BASE_TITLE = "税务云文件中转客户端"

    def __init__(self):
        super().__init__()
        self.style = tb.Style("flatly")
        self.title(self.BASE_TITLE)
        self.iconphoto(True, tk.PhotoImage(data=Icon.icon))

        width, height = 1000, 750
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
        x, y = (screen_w - width) // 2, (screen_h - height) // 2
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.resizable(False, False)

        self._build_ui()

        self.scheduler = TkScheduler(self)
        self.request_client = RequestClient(self._on_request_error)

        state = AppState()
        logger = UILogger(self)

        config_path = os.path.join(sys.path[0], "config.ini")
        config_manager = ConfigManager(config_path, logger=logger)

        verification_service = VerificationService(self.request_client)
        transfer_service = TransferService(self.request_client)
        download_service = DownloadService(self.request_client)

        self.controller = AppController(
            view=self,
            state=state,
            scheduler=self.scheduler,
            logger=logger,
            config_manager=config_manager,
            request_client=self.request_client,
            verification_service=verification_service,
            transfer_service=transfer_service,
            download_service=download_service,
        )

        self.protocol("WM_DELETE_WINDOW", self._on_closing)
        self.controller.initialize()

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
        self.entry_code = tb.Entry(
            top_card,
            width=10,
            justify="center",
            validate="key",
            validatecommand=vcmd,
        )
        self.entry_code.pack(side=LEFT, padx=(0, 10))

        self.btn_unlock = tb.Button(
            top_card,
            text="确定",
            bootstyle=PRIMARY,
            command=self._on_unlock_clicked,
        )
        self.btn_unlock.pack(side=LEFT, padx=6)

        self.btn_confirm = tb.Button(
            top_card,
            text="上传文本",
            bootstyle=SUCCESS,
            command=self._on_confirm_clicked,
        )
        self.btn_confirm.pack(side=LEFT, padx=6)

        self.btn_download = tb.Button(
            top_card,
            text="下载文件",
            bootstyle=WARNING,
            command=self._on_download_clicked,
        )
        self.btn_download.pack(side=LEFT, padx=6)

        self.btn_host_config = tb.Button(
            top_card,
            text="配置地址",
            bootstyle=SECONDARY,
            command=lambda: self.controller.open_host_config("manual"),
        )
        self.btn_host_config.pack(side=RIGHT, padx=(0, 10))

        main_pane = tb.Frame(container)
        main_pane.pack(fill=BOTH, expand=True, padx=10, pady=(6, 0))

        text_card = tb.Labelframe(main_pane, text="文本输入区域", bootstyle=PRIMARY)
        text_card.pack(side=LEFT, fill=BOTH, expand=True, padx=(0, 8), pady=4)

        self.text_main = scrolledtext.ScrolledText(
            text_card,
            wrap="word",
            font=("Microsoft YaHei", 10),
            undo=True,
        )
        self.text_main.pack(fill=BOTH, expand=True, padx=8, pady=8)

        right_card = tb.Labelframe(main_pane, text="拖拽上传文件", bootstyle=PRIMARY)
        right_card.pack(side=LEFT, fill=BOTH, expand=False, ipadx=8, ipady=8, pady=4)

        self.drop_area = tb.Label(
            right_card,
            text="将文件拖拽到此处（支持多个）",
            anchor=CENTER,
            justify=CENTER,
            bootstyle="info-subtle",
            padding=20,
        )
        self.drop_area.pack(fill=BOTH, expand=True, padx=8, pady=8)

        try:
            self.drop_area.drop_target_register(DND_FILES)
            self.drop_area.dnd_bind("<<Drop>>", self._on_files_dropped)
        except Exception:
            btn_choose = tb.Button(
                right_card,
                text="选择文件上传",
                bootstyle=INFO,
                command=self._choose_files,
            )
            btn_choose.pack(pady=6)
            self.append_log("[提示] 系统未检测到拖拽支持，已启用文件选择按钮作为替代。")

        log_card = tb.Labelframe(container, text="日志输出", bootstyle=PRIMARY)
        log_card.pack(fill=BOTH, expand=True, padx=10, pady=(12, 0))

        self.text_log = scrolledtext.ScrolledText(
            log_card,
            wrap="word",
            font=("Consolas", 9),
            state="disabled",
            height=8,
        )
        self.text_log.pack(fill=BOTH, expand=True, padx=8, pady=8)

    def apply_ui_state(self, ui_state: UIState) -> None:
        state = "disabled" if ui_state.locked else "normal"
        self.btn_confirm.config(state=state)
        self.btn_download.config(state=state)

        if ui_state.locked:
            self.drop_area.config(text="请先验证验证码以启用拖拽功能", bootstyle="secondary")
        else:
            self.drop_area.config(text="将文件拖拽到此处（支持多个）", bootstyle="info-subtle")

        self.entry_code.config(state="readonly" if ui_state.code_entry_readonly else "normal")

        if ui_state.unlock_button_mode == "unlock":
            self.btn_unlock.config(
                text="确定",
                bootstyle=PRIMARY,
                state="normal",
                command=self._on_unlock_clicked,
            )
        elif ui_state.unlock_button_mode == "reset":
            self.btn_unlock.config(
                text="重置",
                bootstyle=INFO,
                state="normal",
                command=self._on_reset_clicked,
            )
        elif ui_state.unlock_button_mode == "resetting":
            self.btn_unlock.config(
                text="重置中...",
                bootstyle=DANGER,
                state="disabled",
            )

        if ui_state.current_code:
            self.title(f"{self.BASE_TITLE} — 验证码: {ui_state.current_code}（已启用）")
        else:
            self.title(self.BASE_TITLE)

    def append_log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.text_log.configure(state="normal")
        self.text_log.insert("end", f"[{timestamp}] {message}\n")
        self.text_log.see("end")
        self.text_log.configure(state="disabled")

    def get_code_input(self) -> str:
        return self.entry_code.get()

    def set_code_input(self, value: str) -> None:
        self.entry_code.config(state="normal")
        self.entry_code.delete(0, "end")
        self.entry_code.insert(0, value)

    def clear_code_input(self) -> None:
        self.entry_code.config(state="normal")
        self.entry_code.delete(0, "end")

    def get_main_text(self) -> str:
        return self.text_main.get("1.0", "end-1c")

    def set_main_text(self, value: str) -> None:
        self.text_main.delete("1.0", "end")
        self.text_main.insert("1.0", value)

    def show_warning(self, title: str, message: str) -> None:
        messagebox.showwarning(title, message, parent=self)

    def choose_files(self) -> List[str]:
        return list(filedialog.askopenfilenames(parent=self, title="选择要上传的文件"))

    def prompt_save_path(self, default_name: str) -> Optional[str]:
        return filedialog.asksaveasfilename(
            parent=self,
            title="选择文件保存位置",
            initialfile=default_name,
        )

    def show_host_config_dialog(self, current_host: str, reason: str) -> Optional[str]:
        result = {"host": None}

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

        tb.Label(frame, text=tip, anchor="w", justify="left").pack(fill=X, pady=(0, 10))
        tb.Label(frame, text="服务器地址（HOST）：", anchor="w").pack(fill=X)

        entry_host = tb.Entry(frame)
        entry_host.pack(fill=X, pady=(4, 8))
        if current_host:
            entry_host.insert(0, current_host)

        tb.Label(
            frame,
            text="示例：192.168.1.1 或 example.com",
            bootstyle="secondary",
            anchor="w",
            justify="left",
        ).pack(fill=X, pady=(0, 12))

        btn_frame = tb.Frame(frame)
        btn_frame.pack(fill=X, pady=(4, 0))

        def on_save():
            host = entry_host.get().strip()
            if not host:
                messagebox.showwarning("配置服务器地址", "服务器地址不能为空，请输入一个有效的 HOST。", parent=win)
                return
            result["host"] = host
            win.destroy()

        def on_cancel():
            win.destroy()

        tb.Button(btn_frame, text="保存", bootstyle=SUCCESS, command=on_save).pack(side=LEFT, padx=(0, 6))
        tb.Button(btn_frame, text="取消", bootstyle=SECONDARY, command=on_cancel).pack(side=RIGHT)

        self.show_modal(win)
        return result["host"]

    def show_download_dialog(
        self,
        files: List[FileItem],
        can_load_checker: Callable[[str], bool],
    ) -> Optional[DownloadDialogSelection]:
        result = {"selection": None}

        win = tb.Toplevel(self)
        win.title("选择要下载的文件")
        self.update_idletasks()

        width, height = 560, 420
        parent_x, parent_y = self.winfo_x(), self.winfo_y()
        parent_w, parent_h = self.winfo_width(), self.winfo_height()
        x = parent_x + (parent_w - width) // 2
        y = parent_y + (parent_h - height) // 2
        win.geometry(f"{width}x{height}+{max(x, 0)}+{max(y, 0)}")

        tb.Label(win, text="请选择要下载的文件（可按 Ctrl/Shift 多选）：").pack(
            padx=10, pady=(10, 6), anchor="w"
        )

        frame_list = tb.Frame(win)
        frame_list.pack(fill=BOTH, expand=True, padx=10, pady=(0, 10))

        scrollbar = tb.Scrollbar(frame_list, orient="vertical")
        listbox = tk.Listbox(frame_list, selectmode="extended", yscrollcommand=scrollbar.set)
        scrollbar.config(command=listbox.yview)
        listbox.pack(side="left", fill=BOTH, expand=True)
        scrollbar.pack(side="right", fill="y")

        id_name_list = []
        for item in files:
            id_name_list.append((item.file_id, item.file_name))
            listbox.insert("end", item.file_name)

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

            result["selection"] = DownloadDialogSelection(
                action="download",
                file_ids=selected_ids,
                file_names=selected_names,
            )
            win.destroy()

        def on_load_to_text():
            selection = listbox.curselection()
            if len(selection) != 1:
                return

            idx = selection[0]
            fid, fname = id_name_list[idx]
            result["selection"] = DownloadDialogSelection(
                action="load_text",
                file_id=fid,
                file_name=fname,
            )
            win.destroy()

        def update_load_button_state(event=None):
            selection = listbox.curselection()
            if len(selection) == 1:
                idx = selection[0]
                _, fname = id_name_list[idx]
                if can_load_checker(fname):
                    btn_load_text.config(state="normal")
                    return
            btn_load_text.config(state="disabled")

        tb.Button(
            btn_frame,
            text="下载选中文件",
            bootstyle=SUCCESS,
            command=on_download_selected,
        ).pack(side=LEFT)

        btn_load_text = tb.Button(
            btn_frame,
            text="加载到文本框",
            bootstyle=PRIMARY,
            command=on_load_to_text,
            state="disabled",
        )
        btn_load_text.pack(side=LEFT, padx=(10, 0))

        tb.Button(
            btn_frame,
            text="关闭",
            bootstyle=SECONDARY,
            command=win.destroy,
        ).pack(side=RIGHT)

        listbox.bind("<<ListboxSelect>>", update_load_button_state)
        self.show_modal(win)
        return result["selection"]

    def _on_unlock_clicked(self):
        self.controller.on_unlock_requested()

    def _on_reset_clicked(self):
        self.controller.on_reset_requested()

    def _on_confirm_clicked(self):
        self.controller.on_upload_text_requested()

    def _on_download_clicked(self):
        self.controller.on_download_requested()

    def _on_files_dropped(self, event):
        try:
            paths = self.tk.splitlist(event.data)
        except Exception:
            paths = [event.data]

        normalized = []
        for p in paths:
            p = p.strip()
            if p:
                normalized.append(p)

        self.controller.on_files_selected(normalized)

    def _choose_files(self):
        files = self.choose_files()
        if not files:
            return

        for f in files:
            self.append_log(f"[上传] 选择文件：{os.path.basename(f)}")
        self.controller.on_files_selected(files)

    def _validate_code(self, new_value):
        if len(new_value) > 6:
            return False
        if new_value == "":
            return True
        return new_value.isdigit()

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

    def _on_request_error(self, exception, url):
        self.append_log(f"网络请求异常：{exception} - {url}")

    def _on_closing(self):
        self.controller.shutdown()
        self.destroy()


if __name__ == "__main__":
    app = App()
    app.mainloop()
