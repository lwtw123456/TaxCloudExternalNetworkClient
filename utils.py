import re
import ipaddress
from urllib.parse import urlsplit
import ctypes
from threading import Thread
from datetime import datetime

class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [
        ('cbSize', ctypes.c_uint),
        ('dwTime', ctypes.c_uint),
    ]

def get_idle_seconds():
    try:
        last_input_info = LASTINPUTINFO()
        last_input_info.cbSize = ctypes.sizeof(last_input_info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(last_input_info)):
            return None

        tick_count = ctypes.windll.kernel32.GetTickCount()
        idle_ms = tick_count - last_input_info.dwTime
        return idle_ms / 1000.0
    except Exception:
        return None

def normalize_host(raw):
    if raw is None:
        return False, ""

    s = raw.strip()
    if not s:
        return False, ""

    s = s.strip(" \t\r\n<>\"'")

    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", s):
        parts = urlsplit(s)
    else:
        parts = urlsplit("//" + s)

    netloc = parts.netloc.strip()
    if not netloc:
        netloc = parts.path.split("/", 1)[0].strip()

    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[-1].strip()

    if not netloc:
        return False, ""

    host, port = _split_host_port(netloc)
    if host is None:
        return False, ""

    host = host.strip()
    if not host:
        return False, ""

    if host.endswith("."):
        host = host[:-1]

    if port is not None and not (1 <= port <= 65535):
        return False, ""

    ok, norm_host = _validate_and_normalize_host(host)
    if not ok:
        return False, ""

    if port is None:
        return True, norm_host
    return True, "{}:{}".format(norm_host, port)


def _split_host_port(netloc):
    n = netloc.strip()

    if n.startswith("["):
        m = re.match(r"^\[([^\]]+)\](?::(\d+))?$", n)
        if not m:
            return None, None
        host = m.group(1)
        port_s = m.group(2)
        return host, int(port_s) if port_s else None

    if n.count(":") >= 2:
        return n, None

    if ":" in n:
        host, port_s = n.rsplit(":", 1)
        if not port_s.isdigit():
            return None, None
        return host, int(port_s)

    return n, None


def _validate_and_normalize_host(host):
    try:
        ip = ipaddress.ip_address(host)
        return True, ip.compressed
    except ValueError:
        pass

    h = host.lower()

    if any(c.isspace() for c in h):
        return False, ""
    if "/" in h or "\\" in h:
        return False, ""
    if len(h) > 253:
        return False, ""
    if not re.compile(r"^[A-Za-z0-9.-]+$").match(h):
        return False, ""

    labels = h.split(".")
    if any(lbl == "" for lbl in labels):
        return False, ""

    if len(labels) < 2:
        return False, ""

    for lbl in labels:
        if len(lbl) > 63:
            return False, ""
        if not re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$").match(lbl):
            return False, ""

    return True, h
    
def get_filename_suffix():
    return datetime.now().strftime("%m%d%H%M%S")
    
def run_async(func, *args):
    t = Thread(target=func, args=args, daemon=True)
    t.start()
    return t
    
def decode_response_content(content):
    encodings = ['utf-8', 'gbk', 'gb2312', 'utf-16', 'latin-1']
    for enc in encodings:
        try:
            return content.decode(enc)
        except:
            continue
    return None
