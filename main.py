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
from threading import Event, current_thread, main_thread
import mimetypes
import requests
from datetime import datetime
import configparser
from dataclasses import dataclass, field
from typing import Callable, Optional, List, Tuple

from utils import (
    normalize_host,
    get_filename_suffix,
    run_async,
    get_idle_seconds,
    decode_response_content,
)

# ============================
# 网络请求客户端（网络访问层）
# ============================
class RequestClient:
    def __init__(self, error_handler=None):
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
APP_NAME = "TaxCloudTransferClient"

def get_config_path(filename="config.ini"):
    if sys.platform == "win32":
        base_dir = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        if not base_dir:
            base_dir = os.path.expanduser("~")
        config_dir = os.path.join(base_dir, APP_NAME)

    elif sys.platform == "darwin":
        config_dir = os.path.join(
            os.path.expanduser("~/Library/Application Support"),
            APP_NAME
        )

    else:
        base_dir = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
        config_dir = os.path.join(base_dir, APP_NAME)

    os.makedirs(config_dir, exist_ok=True)
    return os.path.join(config_dir, filename)

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
            config_dir = os.path.dirname(self.config_path)
            if config_dir:
                os.makedirs(config_dir, exist_ok=True)

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
# 状态集中管理
# ============================
@dataclass
class AppState:
    locked: bool = True
    current_code: str = ""
    monitor_thread_started: bool = False
    stop_event: Event = field(default_factory=Event)
    idle_threshold: int = 60
    idle_logged: bool = False


# ============================
# 日志与调度抽象
# ============================
class LoggerInterface:
    def log(self, message: str):
        raise NotImplementedError


class SchedulerInterface:
    def run_background(self, func: Callable, *args, **kwargs):
        raise NotImplementedError

    def call_ui(self, func: Callable, *args, **kwargs):
        raise NotImplementedError


class UILogger(LoggerInterface):
    def __init__(self, writer: Callable[[str], None]):
        self.writer = writer

    def log(self, message: str):
        self.writer(message)


class TkScheduler(SchedulerInterface):
    def __init__(self, root: tk.Misc):
        self.root = root

    def run_background(self, func: Callable, *args, **kwargs):
        run_async(func, *args, **kwargs)

    def call_ui(self, func: Callable, *args, **kwargs):
        if current_thread() is main_thread():
            func(*args, **kwargs)
        else:
            self.root.after(0, lambda: func(*args, **kwargs))


# ============================
# 业务服务层
# ============================
class VerificationService:
    def __init__(self, client: RequestClient, state: AppState):
        self.client = client
        self.state = state

    def validate_code_input(self, new_value: str) -> bool:
        if len(new_value) > 6:
            return False
        if new_value == "":
            return True
        return new_value.isdigit()

    def check_code_once(self, code_value: str, locked_before_check: bool) -> dict:
        resp = self.client.resolve_code(code_value)
        if resp.status_code == 200:
            json_data = resp.json()
            if locked_before_check:
                if json_data.get("success"):
                    return {
                        "continue_monitor": True,
                        "became_unlocked": True,
                        "message": "[验证] 成功！文本输入框、上传和拖拽区域已启用。",
                    }
                return {
                    "continue_monitor": False,
                    "became_unlocked": False,
                    "message": f"[验证] 失败：{json_data.get('msg')}",
                    "need_clear_code": True,
                }
            else:
                if not json_data.get("success"):
                    return {
                        "continue_monitor": False,
                        "became_locked": True,
                        "message": "[验证] 失败！请重新输入验证码。",
                        "need_clear_code": True,
                    }
        else:
            return {
                "continue_monitor": True,
                "message": "[验证] 失败！服务器故障或服务器地址错误。",
            }

        return {"continue_monitor": True}

    def stop_monitor_state(self):
        self.state.stop_event.set()
        self.state.locked = True
        self.state.current_code = ""

    def prepare_monitor_start(self, code_value: str) -> dict:
        if not self.state.monitor_thread_started:
            self.state.monitor_thread_started = True
            self.state.stop_event.clear()
            self.state.current_code = code_value
            return {"started": True, "message": "[验证] 已开始轮询验证。"}
        return {"started": False, "message": "[验证] 已在轮询中。"}

    def monitor_loop(self, code_value: str, on_tick: Callable[[dict], None]):
        self.state.current_code = code_value
        try:
            while not self.state.stop_event.is_set():
                idle_seconds = get_idle_seconds()

                if idle_seconds is not None and idle_seconds > self.state.idle_threshold:
                    if not self.state.idle_logged:
                        self.state.idle_logged = True
                        on_tick(
                            {
                                "type": "idle_pause",
                                "message": f"[监控] 检测到用户已空闲超过 {self.state.idle_threshold} 秒，暂停验证码轮询。",
                            }
                        )
                    if self.state.stop_event.wait(1):
                        break
                    continue
                else:
                    if self.state.idle_logged:
                        self.state.idle_logged = False
                        on_tick(
                            {
                                "type": "idle_resume",
                                "message": "[监控] 检测到用户恢复活动，恢复验证码轮询。",
                            }
                        )

                result = self.check_code_once(code_value, self.state.locked)
                on_tick({"type": "check_result", "result": result})
                if not result.get("continue_monitor", True):
                    break

                self.state.stop_event.wait(60)
        finally:
            self.state.monitor_thread_started = False


class TransferService:
    def __init__(self, client: RequestClient):
        self.client = client

    def upload_text(self, code_value: str, text_value: str, override_name: str = None):
        file_bytes = text_value.encode("utf-8")
        file_name = override_name or f"文本{get_filename_suffix()}.txt"
        file_size = len(file_bytes)
        file_obj = BytesIO(file_bytes)
        resp = self.client.upload_file(code_value, file_name, file_size, file_obj)
        return resp, file_name

    def upload_local_file(self, code_value: str, file_path: str, override_name: str = None):
        file_name = override_name or os.path.basename(file_path)
        file_size = os.path.getsize(file_path)
        with open(file_path, "rb") as f:
            resp = self.client.upload_file(code_value, file_name, file_size, f)
        return resp, file_name

    def upload_with_retry(self, code_value, text_value = None, file_path = None, text_file_name = None):
        def _next_name(name, n):
            base, ext = os.path.splitext(name)
            return f"{base}({n}){ext}"

        if file_path:
            origin_name = os.path.basename(file_path)
        else:
            origin_name = text_file_name or f"文本{get_filename_suffix()}.txt"

        logs = []
        attempt = 0

        while True:
            if attempt == 0:
                override_name = origin_name
            else:
                override_name = _next_name(origin_name, attempt)

            if file_path:
                resp, file_name = self.upload_local_file(
                    code_value,
                    file_path,
                    override_name=override_name,
                )
            else:
                resp, file_name = self.upload_text(
                    code_value,
                    text_value,
                    override_name=override_name,
                )

            if resp.status_code == 200:
                json_data = resp.json()

                if json_data.get("success"):
                    logs.append(f"[上传] 成功，文件名为「{file_name}」")
                    return {
                        "success": True,
                        "logs": logs,
                        "need_stop_monitor": False,
                    }

                msg = json_data.get("msg")

                if msg == "中转上传文件中已存在同名文件":
                    logs.append(f"[上传] 已存在同名文件{file_name}，自动更名后重试")
                    attempt += 1
                    continue

                if msg == "上传码已失效":
                    return {
                        "success": False,
                        "logs": logs,
                        "need_stop_monitor": True,
                    }

                logs.append(f"[上传] 失败，{msg}。")
                return {
                    "success": False,
                    "logs": logs,
                    "need_stop_monitor": False,
                }

            logs.append("[上传] 失败！服务器故障或服务器地址错误。")
            return {
                "success": False,
                "logs": logs,
                "need_stop_monitor": False,
            }


class DownloadService:
    TEXT_EXTENSIONS = {
        ".txt", ".js", ".html", ".htm", ".py", ".cpp", ".c", ".h", ".hpp",
        ".css", ".json", ".xml", ".md", ".yaml", ".yml", ".ini", ".cfg", ".sh", ".bat",
        ".java", ".cs", ".go", ".rs", ".php", ".rb", ".sql", ".log", ".csv"
    }
    IMAGE_EXTENSIONS = {
        ".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"
    }

    def __init__(self, client: RequestClient):
        self.client = client
        self._rapid_ocr = None

    def get_downloadable_files(self, code_value: str) -> dict:
        resp = self.client.get_file_list(code_value)
        if resp.status_code != 200:
            return {"success": False, "message": "[下载] 查询失败！服务器故障或服务器地址错误。"}
        json_data = resp.json()
        if not json_data.get("success"):
            return {"success": False, "message": "[下载] 当前验证码下没有可下载的文件。"}
        files = json_data.get("data") or []
        if not files:
            return {"success": False, "message": "[下载] 当前验证码下没有可下载的文件。"}
        return {"success": True, "files": files}

    def build_download_display_name(self, selected_names: List[str]) -> str:
        if len(selected_names) == 1:
            return selected_names[0]
        return f"选中文件打包_{get_filename_suffix()}.zip"

    def download_stream(self, file_ids: List[str]):
        ids_str = ",".join(file_ids)
        return self.client.download_file(ids_str)

    def load_text_content(self, file_id: str) -> dict:
        resp = self.client.download_file(file_id)
        if resp.status_code != 200:
            return {"success": False, "status_code": resp.status_code}

        try:
            content = resp.content
            text = decode_response_content(content)
            if text is None:
                return {"success": False, "decode_failed": True}
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            return {"success": True, "text": text}
        except Exception as e:
            return {"success": False, "exception": e}

    def can_load_to_text(self, file_name: str) -> bool:
        ext = os.path.splitext(file_name)[1].lower()
        return ext in self.TEXT_EXTENSIONS
        
    def can_ocr_to_text(self, file_name: str) -> bool:
        ext = os.path.splitext(file_name)[1].lower()
        return ext in self.IMAGE_EXTENSIONS

    def _get_rapid_ocr_engine(self):
        if self._rapid_ocr is None:
            from rapidocr import RapidOCR
            self._rapid_ocr = RapidOCR()
        return self._rapid_ocr

    def _extract_ocr_text(self, result) -> str:
        txts = getattr(result, "txts", None)
        if not txts:
            return ""
        return "\n".join(str(t).strip() for t in txts if str(t).strip())

    def ocr_image_content(self, file_id: str, file_name: str) -> dict:
        resp = self.client.download_file(file_id)
        if resp.status_code != 200:
            return {"success": False, "status_code": resp.status_code}

        try:
            engine = self._get_rapid_ocr_engine()

            result = engine(resp.content)

            text = self._extract_ocr_text(result)
            return {
                "success": True,
                "text": text,
            }

        except ImportError:
            return {
                "success": False,
                "missing_rapidocr": True,
            }
        except Exception as e:
            return {
                "success": False,
                "exception": e,
            }
            
    def ocr_local_image(self, file_path: str) -> dict:
        try:
            with open(file_path, "rb") as f:
                image_bytes = f.read()

            engine = self._get_rapid_ocr_engine()
            result = engine(image_bytes)

            text = self._extract_ocr_text(result)

            return {
                "success": True,
                "text": text,
            }

        except ImportError:
            return {
                "success": False,
                "missing_rapidocr": True,
            }

        except Exception as e:
            return {
                "success": False,
                "exception": e,
            }

# ============================
# Presenter / Controller
# ============================
class AppPresenter:
    def __init__(
        self,
        state: AppState,
        client: RequestClient,
        config_manager: ConfigManager,
        verification_service: VerificationService,
        transfer_service: TransferService,
        download_service: DownloadService,
        scheduler: SchedulerInterface,
        logger: LoggerInterface,
    ):
        self.state = state
        self.client = client
        self.config_manager = config_manager
        self.verification_service = verification_service
        self.transfer_service = transfer_service
        self.download_service = download_service
        self.scheduler = scheduler
        self.logger = logger
        self.view = None

    def bind_view(self, view):
        self.view = view

    def log(self, message: str):
        self.logger.log(message)

    def on_request_error(self, exception, url):
        self.log(f"网络请求异常：{exception} - {url}")

    def is_host_configured(self):
        return bool(self.client.host.strip())

    def ensure_host_configured(self, auto_popup: bool):
        if self.is_host_configured():
            return True
        self.log("[配置] 未检测到服务器地址(HOST)，请先点击“配置地址”进行设置。")
        if auto_popup:
            self.scheduler.call_ui(self.view.show_host_config, "runtime")
        return False

    def init_from_config(self):
        config = self.config_manager.load_all()
        host = config.get("host", "")
        saved_code = config.get("code", "")

        if not host:
            self.log("[配置] 未检测到服务器地址(HOST)，请先点击“配置地址”进行设置。")
            self.scheduler.call_ui(lambda: self.view.after(200, lambda: self.view.show_host_config("startup")))
            return

        self.client.host = host
        self.log(f"[配置] 已从配置文件读取服务器地址：{host}")

        if saved_code and len(saved_code) == 6 and saved_code.isdigit():
            self.scheduler.call_ui(self.view.set_code_input, saved_code)
            self.log("[配置] 已从配置文件读取上次的验证码，正在自动验证...")
            self.scheduler.call_ui(lambda: self.view.after(100, self.on_unlock_clicked))

    def on_closing(self):
        if not self.state.locked and self.state.current_code:
            self.config_manager.save_code(self.state.current_code)
        else:
            self.config_manager.save_code("")
        self.state.stop_event.set()

    def save_host(self, host: str):
        self.client.host = host
        self.config_manager.save_host(host)
        self.log(f"[配置] 已设置服务器地址：{host}")

    def validate_code_input(self, new_value: str) -> bool:
        return self.verification_service.validate_code_input(new_value)

    def on_unlock_clicked(self):
        if not self.ensure_host_configured(auto_popup=True):
            return

        code_value = self.view.get_code_input().strip()
        if len(code_value) != 6:
            self.log("[验证] 验证码必须为6位数字，请检查。")
            return

        result = self.verification_service.prepare_monitor_start(code_value)
        self.log(result["message"])
        if result["started"]:
            self.scheduler.run_background(self._monitor_check_loop, code_value)

    def _monitor_check_loop(self, code_value: str):
        def on_tick(payload: dict):
            if payload["type"] in ("idle_pause", "idle_resume"):
                self.log(payload["message"])
                return

            if payload["type"] == "check_result":
                result = payload["result"]
                message = result.get("message")
                if message:
                    self.log(message)

                if result.get("became_unlocked"):
                    self.state.locked = False
                    self.scheduler.call_ui(self.view.apply_unlocked_ui, code_value)

                if result.get("became_locked"):
                    self.state.locked = True
                    self.scheduler.call_ui(self.view.apply_locked_ui)

                if result.get("need_clear_code"):
                    self.scheduler.call_ui(self.view.clear_code_input)

        self.verification_service.monitor_loop(code_value, on_tick)

    def stop_monitor(self):
        self.verification_service.stop_monitor_state()
        self.scheduler.call_ui(self.view.apply_stopped_monitor_ui)

    def on_reset_clicked(self):
        try:
            self.scheduler.call_ui(self.view.apply_resetting_ui)
            self.log("[验证] 正在重置验证码并锁定界面...")
            self.stop_monitor()
            self.scheduler.call_ui(self.view.clear_code_input_and_enable)
            self.scheduler.call_ui(lambda: self.view.after(300, self.view.set_unlock_button_default))
            self.log("[验证] 已重置，已恢复到待验证状态。")
        except Exception as e:
            self.log(f"[验证] 重置失败：{e}")
            self.scheduler.call_ui(self.view.set_unlock_button_default)

    def on_confirm_clicked(self):
        if self.state.locked:
            self.log("[上传] 功能尚未启用，请先输入验证码并确认。")
            return

        if not self.ensure_host_configured(auto_popup=True):
            return

        text_value = self.view.get_main_text()
        if text_value.strip():
            self.log("[上传] 正在上传文本内容...")
            self.scheduler.run_background(self._upload_text_worker, text_value)
        else:
            self.log("[上传] 失败，当前文本框为空。")

    def on_files_selected(self, paths: List[str], from_drop=False):
        if self.state.locked:
            prefix = "[拖拽]" if from_drop else "[上传]"
            self.log(f"{prefix} 功能尚未启用，请先输入验证码并确认。")
            return

        if not self.ensure_host_configured(auto_popup=True):
            return

        code_value = self.view.get_code_input()

        for p in paths:
            p = p.strip()
            if not p:
                continue

            file_name = os.path.basename(p)

            if self.download_service.can_ocr_to_text(file_name):
                use_ocr = self.view.ask_ocr_before_upload(file_name)

                if use_ocr:
                    self.log(f"[OCR] 已选择 OCR，正在识别图片：{file_name} ...")
                    self.scheduler.run_background(
                        self._ocr_and_upload_file_worker,
                        p,
                        code_value,
                    )
                    continue

            self.log(f"[上传] 正在上传文件：{file_name} ...")
            self.scheduler.run_background(self._upload_file_worker, p)

    def _upload_text_worker(self, text_value: str):
        self.scheduler.call_ui(self.view.set_transfer_buttons_enabled, False)
        result = self.transfer_service.upload_with_retry(
            code_value=self.view.get_code_input(),
            text_value=text_value,
            file_path=None,
        )

        for msg in result["logs"]:
            self.log(msg)

        if result["need_stop_monitor"]:
            self.scheduler.call_ui(self.stop_monitor)

        self.scheduler.call_ui(self.view.set_transfer_buttons_enabled, True)

    def _upload_file_worker(self, file_path: str):
        self.scheduler.call_ui(self.view.set_transfer_buttons_enabled, False)
        result = self.transfer_service.upload_with_retry(
            code_value=self.view.get_code_input(),
            file_path=file_path,
        )

        for msg in result["logs"]:
            self.log(msg)

        if result["need_stop_monitor"]:
            self.scheduler.call_ui(self.stop_monitor)

        self.scheduler.call_ui(self.view.set_transfer_buttons_enabled, True)
        
    def _ocr_and_upload_file_worker(self, file_path: str, code_value: str):
        self.scheduler.call_ui(
            self.view.set_transfer_buttons_enabled,
            False,
        )

        file_name = os.path.basename(file_path)

        try:
            ocr_result = self.download_service.ocr_local_image(file_path)

            if not ocr_result["success"]:
                if ocr_result.get("missing_rapidocr"):
                    self.log(
                        "[OCR] 失败：未安装 RapidOCR，"
                        "请先执行：pip install rapidocr onnxruntime"
                    )

                elif ocr_result.get("exception") is not None:
                    self.log(f"[OCR] 失败：{ocr_result['exception']}")

                else:
                    self.log(f"[OCR] 失败：无法识别图片「{file_name}」。")

                return

            text = ocr_result["text"]

            if not text.strip():
                self.log(
                    f"[OCR] 完成，但图片「{file_name}」"
                    "未识别到文字，已取消上传。"
                )
                return

            base_name = os.path.splitext(file_name)[0]
            text_file_name = f"{base_name}_OCR.txt"

            self.log(
                f"[OCR] 识别完成，正在上传识别结果："
                f"{text_file_name} ..."
            )

            upload_result = self.transfer_service.upload_with_retry(
                code_value=code_value,
                text_value=text,
                text_file_name=text_file_name,
            )

            for msg in upload_result["logs"]:
                self.log(msg)

            if upload_result["need_stop_monitor"]:
                self.scheduler.call_ui(self.stop_monitor)

        finally:
            self.scheduler.call_ui(
                self.view.set_transfer_buttons_enabled,
                True,
            )

    def on_download_clicked(self):
        if self.state.locked:
            self.log("[下载] 功能尚未启用，请先输入验证码并确认。")
            return

        if not self.ensure_host_configured(auto_popup=True):
            return

        code_value = self.view.get_code_input()
        if not code_value:
            self.log("[下载] 请先输入验证码。")
            return

        self.log("[下载] 正在查询可下载文件列表...")
        self.scheduler.run_background(self._download_list_worker, code_value)

    def _download_list_worker(self, code_value: str):
        result = self.download_service.get_downloadable_files(code_value)
        if not result["success"]:
            self.log(result["message"])
            return
        self.scheduler.call_ui(self.view.show_download_dialog, result["files"])

    def download_files_async(self, file_ids: List[str], display_name: str):
        self.scheduler.run_background(self._download_files_worker, file_ids, display_name)

    def _download_files_worker(self, file_ids: List[str], display_name: str):
        self.log(f"[下载] 开始下载文件：{display_name} ...")
        resp = self.download_service.download_stream(file_ids)

        def ui_after_resp():
            if resp.status_code != 200:
                self.log(f"[下载] 失败！服务器返回状态码 {resp.status_code}。")
                return

            save_path = self.view.ask_save_path(display_name)
            if not save_path:
                self.log(f"[下载] 已取消保存「{display_name}」。")
                return

            try:
                with open(save_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                self.log(f"[下载] 完成，文件「{display_name}」已保存到：{save_path}")
            except Exception as e:
                self.log(f"[下载] 保存失败：{e}")

        self.scheduler.call_ui(ui_after_resp)

    def load_file_to_text_async(self, file_id: str, file_name: str):
        self.scheduler.run_background(self._load_file_to_text_worker, file_id, file_name)

    def ocr_file_to_text_async(self, file_id: str, file_name: str):
        self.scheduler.run_background(self._ocr_file_to_text_worker, file_id, file_name)

    def _ocr_file_to_text_worker(self, file_id: str, file_name: str):
        self.log(f"[OCR] 正在识别图片：{file_name} ...")
        result = self.download_service.ocr_image_content(file_id, file_name)

        def ui_after_resp():
            if not result["success"]:
                if result.get("status_code") is not None:
                    self.log(f"[OCR] 失败！服务器返回状态码 {result['status_code']}。")
                    return
                if result.get("missing_rapidocr"):
                    self.log("[OCR] 失败：未安装 RapidOCR，请先执行：pip install rapidocr onnxruntime")
                    return
                if result.get("exception") is not None:
                    self.log(f"[OCR] 失败：{result['exception']}")
                    return
                self.log("[OCR] 失败。")
                return

            self.view.set_main_text(result["text"])

            if result["text"].strip():
                self.log(f"[OCR] 完成，图片「{file_name}」的识别结果已加载到文本输入框。")
            else:
                self.log(f"[OCR] 完成，但图片「{file_name}」未识别到文字。")

        self.scheduler.call_ui(ui_after_resp)

    def _load_file_to_text_worker(self, file_id: str, file_name: str):
        self.log(f"[加载] 正在加载文件：{file_name} ...")
        result = self.download_service.load_text_content(file_id)

        def ui_after_resp():
            if not result["success"]:
                if result.get("status_code") is not None:
                    self.log(f"[加载] 失败！服务器返回状态码 {result['status_code']}。")
                    return
                if result.get("decode_failed"):
                    self.log("[加载] 失败：无法解析文件编码。")
                    return
                if result.get("exception") is not None:
                    self.log(f"[加载] 失败：{result['exception']}")
                    return
                self.log("[加载] 失败。")
                return

            self.view.set_main_text(result["text"])
            self.log(f"[加载] 完成，文件「{file_name}」已加载到文本输入框。")

        self.scheduler.call_ui(ui_after_resp)

    def build_download_display_name(self, selected_names: List[str]) -> str:
        return self.download_service.build_download_display_name(selected_names)

    def can_load_to_text(self, file_name: str):
        return self.download_service.can_load_to_text(file_name)
        
    def can_ocr_to_text(self, file_name: str) -> bool:
        return self.download_service.can_ocr_to_text(file_name)

# ============================
# UI 视图层
# ============================
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

        self.state_obj = AppState()

        self.client = RequestClient()
        config_path = get_config_path()

        self.scheduler = TkScheduler(self)
        self.logger = UILogger(self.append_log)
        self.config_manager = ConfigManager(config_path, logger=self.logger.log)

        self.verification_service = VerificationService(self.client, self.state_obj)
        self.transfer_service = TransferService(self.client)
        self.download_service = DownloadService(self.client)

        self.presenter = AppPresenter(
            state=self.state_obj,
            client=self.client,
            config_manager=self.config_manager,
            verification_service=self.verification_service,
            transfer_service=self.transfer_service,
            download_service=self.download_service,
            scheduler=self.scheduler,
            logger=self.logger,
        )
        self.presenter.bind_view(self)
        self.client.error_handler = self.presenter.on_request_error

        self._build_ui()
        self.apply_locked_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_closing)
        self.presenter.init_from_config()

    # ----------------------------
    # UI 构建
    # ----------------------------
    def _build_ui(self):
        container = tb.Frame(self, padding=12)
        container.pack(fill=BOTH, expand=True)

        top_frame = tb.Frame(container)
        top_frame.pack(fill=X, pady=(0, 12))

        top_card = tb.Labelframe(top_frame, text="验证码验证", bootstyle=INFO)
        top_card.pack(fill=X, padx=10, pady=2, ipady=6)

        lbl = tb.Label(top_card, text="上传验证码：", anchor="w")
        lbl.pack(side=LEFT, padx=(10, 6))

        vcmd = (self.register(self.presenter.validate_code_input), "%P")
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
            command=self.presenter.on_unlock_clicked,
        )
        self.btn_unlock.pack(side=LEFT, padx=6)

        self.btn_confirm = tb.Button(
            top_card,
            text="上传文本",
            bootstyle=SUCCESS,
            command=self.presenter.on_confirm_clicked,
        )
        self.btn_confirm.pack(side=LEFT, padx=6)

        self.btn_download = tb.Button(
            top_card,
            text="下载文件",
            bootstyle=WARNING,
            command=self.presenter.on_download_clicked,
        )
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
            self.drop_area.dnd_bind("<<Drop>>", self.on_files_dropped)
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

        self.append_log("[启动] 界面加载完成，如首次使用请先点击“配置地址”设置 HOST，然后输入验证码并点击“确定”。")

    # ----------------------------
    # UI 基础能力
    # ----------------------------
    def append_log(self, message):
        def write_log(message):
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.text_log.configure(state="normal")
            self.text_log.insert("end", f"[{timestamp}] {message}\n")
            self.text_log.see("end")
            self.text_log.configure(state="disabled")

        if current_thread() is main_thread():
            write_log(message)
        else:
            self.after(0, lambda: write_log(message))

    def get_code_input(self) -> str:
        return self.entry_code.get()

    def set_code_input(self, value: str):
        self.entry_code.delete(0, "end")
        self.entry_code.insert(0, value)

    def clear_code_input(self):
        self.entry_code.delete(0, "end")

    def clear_code_input_and_enable(self):
        self.entry_code.config(state="normal")
        self.entry_code.delete(0, "end")

    def get_main_text(self) -> str:
        return self.text_main.get("1.0", "end-1c")

    def set_main_text(self, text: str):
        self.text_main.delete("1.0", "end")
        self.text_main.insert("1.0", text)

    def ask_save_path(self, default_name: str):
        return filedialog.asksaveasfilename(
            parent=self,
            title="选择文件保存位置",
            initialfile=default_name,
        )
        
    def ask_ocr_before_upload(self, file_name: str) -> bool:
        return messagebox.askyesno(
            "图片上传",
            f"检测到图片文件：{file_name}\n\n"
            "是否先进行 OCR？\n\n"
            "选择“是”：上传 OCR 识别结果（TXT）\n"
            "选择“否”：直接上传原图片",
            parent=self,
        )

    def set_transfer_buttons_enabled(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        self.btn_confirm.config(state=state)
        self.btn_download.config(state=state)

    def _update_title_status(self, code_value: str):
        if code_value:
            self.title(f"{self.BASE_TITLE} — 验证码: {code_value}（已启用）")
        else:
            self.title(self.BASE_TITLE)

    def set_unlock_button_default(self):
        self.btn_unlock.config(
            text="确定",
            bootstyle=PRIMARY,
            state="normal",
            command=self.presenter.on_unlock_clicked,
        )

    def set_unlock_button_enabled(self, code_value: str):
        self._update_title_status(code_value)
        self.btn_unlock.config(
            text="重置",
            bootstyle=INFO,
            state="normal",
            command=self.presenter.on_reset_clicked,
        )

    def apply_locked_ui(self):
        self.state_obj.locked = True
        self.entry_code.config(state="normal")
        self.btn_confirm.config(state="disabled")
        self.btn_download.config(state="disabled")
        self.drop_area.config(text="请先验证验证码以启用拖拽功能", bootstyle="secondary")
        self.set_unlock_button_default()
        self._update_title_status("")

    def apply_unlocked_ui(self, code_value: str):
        self.state_obj.locked = False
        self.btn_confirm.config(state="normal")
        self.btn_download.config(state="normal")
        self.drop_area.config(text="将文件拖拽到此处（支持多个）", bootstyle="info-subtle")
        self.set_unlock_button_enabled(code_value)
        self.entry_code.config(state="readonly")

    def apply_stopped_monitor_ui(self):
        self.apply_locked_ui()
        self.entry_code.config(state="normal")
        self.state_obj.current_code = ""

    def apply_resetting_ui(self):
        self.btn_unlock.config(text="重置中...", bootstyle=DANGER, state="disabled")

    # ----------------------------
    # 窗口与弹窗
    # ----------------------------
    def _on_closing(self):
        self.presenter.on_closing()
        self.destroy()

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

        current_host = self.client.host.strip()
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

            is_valid_host, host = normalize_host(host)
            if not is_valid_host:
                messagebox.showwarning("配置服务器地址", "服务器地址格式不正确，请重新输入。")
                return

            self.presenter.save_host(host)
            win.destroy()

        btn_save = tb.Button(btn_frame, text="保存", bootstyle=SUCCESS, command=on_save)
        btn_save.pack(side=LEFT, padx=(0, 6))

        def on_cancel():
            if not self.presenter.is_host_configured():
                self.append_log("[配置] 未完成服务器地址配置，客户端功能暂不可用。")
            win.destroy()

        btn_cancel = tb.Button(btn_frame, text="取消", bootstyle=SECONDARY, command=on_cancel)
        btn_cancel.pack(side=RIGHT)
        self.show_modal(win)

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
        win.geometry(f"{width}x{height}+{max(x, 0)}+{max(y, 0)}")

        lbl = tb.Label(win, text="请选择要下载的文件（可按 Ctrl/Shift 多选）：")
        lbl.pack(padx=10, pady=(10, 6), anchor="w")

        frame_list = tb.Frame(win)
        frame_list.pack(fill=BOTH, expand=True, padx=10, pady=(0, 10))

        scrollbar = tb.Scrollbar(frame_list, orient="vertical")
        listbox = tk.Listbox(frame_list, selectmode="extended", yscrollcommand=scrollbar.set)
        scrollbar.config(command=listbox.yview)
        listbox.pack(side="left", fill=BOTH, expand=True)
        scrollbar.pack(side="right", fill="y")

        id_name_list: List[Tuple[str, str]] = []
        for f in files:
            file_id = str(f.get("id"))
            file_name = f.get("fileName") or file_id
            id_name_list.append((file_id, file_name))
            listbox.insert("end", file_name)

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

            display_name = self.presenter.build_download_display_name(selected_names)
            self.presenter.download_files_async(selected_ids, display_name)
            win.destroy()

        def on_load_to_text():
            selection = listbox.curselection()
            if len(selection) != 1:
                return
            idx = selection[0]
            fid, fname = id_name_list[idx]
            self.presenter.load_file_to_text_async(fid, fname)
            win.destroy()

        def on_ocr_to_text():
            selection = listbox.curselection()
            if len(selection) != 1:
                return
            idx = selection[0]
            fid, fname = id_name_list[idx]
            self.presenter.ocr_file_to_text_async(fid, fname)
            win.destroy()

        def update_load_button_state(event=None):
            selection = listbox.curselection()

            btn_load_text.config(state="disabled")
            btn_ocr_text.config(state="disabled")

            if len(selection) != 1:
                return

            idx = selection[0]
            _, fname = id_name_list[idx]

            if self.presenter.can_load_to_text(fname):
                btn_load_text.config(state="normal")

            if self.presenter.can_ocr_to_text(fname):
                btn_ocr_text.config(state="normal")

        btn_download = tb.Button(btn_frame, text="下载选中文件", bootstyle=SUCCESS, command=on_download_selected)
        btn_download.pack(side=LEFT)

        btn_load_text = tb.Button(
            btn_frame,
            text="加载到文本框",
            bootstyle=PRIMARY,
            command=on_load_to_text,
            state="disabled",
        )
        btn_load_text.pack(side=LEFT, padx=(10, 0))
        btn_ocr_text = tb.Button(
            btn_frame,
            text="OCR到文本框",
            bootstyle=INFO,
            command=on_ocr_to_text,
            state="disabled",
        )
        btn_ocr_text.pack(side=LEFT, padx=(10, 0))

        btn_close = tb.Button(btn_frame, text="关闭", bootstyle=SECONDARY, command=win.destroy)
        btn_close.pack(side=RIGHT)

        listbox.bind("<<ListboxSelect>>", update_load_button_state)
        self.show_modal(win)

    def _choose_files(self):
        files = filedialog.askopenfilenames(parent=self, title="选择要上传的文件")
        if files:
            for f in files:
                self.append_log(f"[上传] 选择文件：{os.path.basename(f)}")
            self.presenter.on_files_selected(list(files), from_drop=False)

    def on_files_dropped(self, event):
        try:
            paths = self.tk.splitlist(event.data)
        except Exception:
            paths = [event.data]
        self.presenter.on_files_selected(list(paths), from_drop=True)


if __name__ == "__main__":
    app = App()
    app.mainloop()
