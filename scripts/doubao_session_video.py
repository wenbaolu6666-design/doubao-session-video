#!/usr/bin/env python3
"""Portable Doubao web-session client for video generation.

The client intentionally uses only the user's existing signed-in Doubao web
session. It does not perform login, solve captchas, or accept an API key.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any


BASE_URL = "https://www.doubao.com"
CHAT_AID = "582478"
CHROME_VERSION = "148.0.0.0"
CHROMIUM_VERSION = "148.0.7816.0"
APP_VERSION = "2.1.7"
RUNTIME_VERSION = "3.5.4"
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{CHROME_VERSION} Safari/537.36"
)
DEFAULT_RATIO = "16:9"
SUPPORTED_RATIOS = {"1:1", "16:9", "9:16"}
DEFAULT_TIMEOUT_SECONDS = 600
VIDEO_AGENT_BOT_ID = "7338286299411103781"
VIDEO_AGENT_MODEL_ID = "9"
VIDEO_AGENT_MODE_ID = "3"
POLL_REQUESTS = 3
POLL_RETRY_SECONDS = 5
MAX_IMAGE_BYTES = 20 * 1024 * 1024
SUPPORTED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_MODELS = (
    "doubao-video",
    "seedance-2.5",
    "seedance-2.0",
    "seedance-2.0-fast",
    "seedance-2.0-mini",
)
ACTIVE_TASK_LOG: "TaskLog | None" = None


class ClientError(RuntimeError):
    """A user-facing Doubao client error."""


def config_path() -> Path:
    if os.name == "nt":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "doubao-session-video" / "config.json"


def private_mode(path: Path, mode: int) -> None:
    if os.name != "nt":
        os.chmod(path, mode)


def fail(message: str) -> None:
    if ACTIVE_TASK_LOG is not None:
        ACTIVE_TASK_LOG.event("error", message=message)
    raise ClientError(message)


class TaskLog:
    """Persist non-sensitive task state so a terminal can be reopened safely."""

    def __init__(self, output_dir: str | os.PathLike[str], action: str, task_id: str = "") -> None:
        directory = Path(output_dir).expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        name = f"doubao-session-video-task-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(4)}.jsonl"
        self.path = directory / name
        self.event("started", action=action, task_id=task_id)

    def event(self, event: str, **details: Any) -> None:
        payload = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event": event,
            **details,
        }
        try:
            with open(self.path, "a", encoding="utf-8") as handle:
                private_mode(self.path, 0o600)
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            pass


def parse_cookie_header(value: str) -> dict[str, str]:
    text = value.strip()
    if text.lower().startswith("cookie:"):
        text = text[7:].strip()
    if not text:
        return {}
    if "=" not in text and ";" not in text and "\n" not in text:
        return {"sessionid": text}
    cookies: dict[str, str] = {}
    for item in text.replace("\r", ";").replace("\n", ";").split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        name, cookie_value = item.split("=", 1)
        name = name.strip()
        if name:
            cookies[name] = cookie_value.strip()
    return cookies


def _coerce_session(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            fail("The session input is empty.")
        if text.startswith("{"):
            try:
                raw = json.loads(text)
            except json.JSONDecodeError as exc:
                fail(f"The session JSON is invalid: {exc}")
        else:
            raw = {"cookies": parse_cookie_header(text)}
    if not isinstance(raw, dict):
        fail("The session must be a JSON object or a Cookie header string.")

    candidate = raw.get("cookies")
    if candidate is None and isinstance(raw.get("Cookie"), str):
        candidate = parse_cookie_header(raw["Cookie"])
    if candidate is None and isinstance(raw.get("cookie"), str):
        candidate = parse_cookie_header(raw["cookie"])
    if candidate is None and isinstance(raw.get("sessionid"), str):
        candidate = {"sessionid": raw["sessionid"]}
    if candidate is None and all(isinstance(value, str) for value in raw.values()):
        candidate = raw
    if not isinstance(candidate, dict):
        fail("The session JSON must contain a cookies object.")

    cookies = {str(name).strip(): str(value).strip() for name, value in candidate.items() if str(name).strip()}
    if not cookies.get("sessionid"):
        fail("The session does not contain a sessionid cookie.")

    session: dict[str, Any] = {
        "cookies": cookies,
        "label": str(raw.get("label") or raw.get("email") or "Doubao web session"),
    }
    for key in ("device_id", "web_id", "fp"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            session[key] = value.strip()
    return session


def _random_digits(length: int, first: str) -> str:
    return first + "".join(secrets.choice("0123456789") for _ in range(length - 1))


def new_identity() -> dict[str, str]:
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    part = lambda size: "".join(secrets.choice(alphabet) for _ in range(size))
    return {
        "device_id": _random_digits(16, "7"),
        "web_id": _random_digits(19, "7"),
        "fp": f"verify_{part(8)}_{part(4)}_{part(4)}_{part(4)}_{part(12)}",
    }


def load_session(*, required: bool = True) -> dict[str, Any] | None:
    path = config_path()
    if not path.is_file():
        if required:
            fail(
                "No Doubao session is saved. On Windows run "
                "`config --import-desktop`, or provide an authenticated Doubao "
                "session with `config --set-session-stdin`."
            )
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        session = _coerce_session(raw)
    except (OSError, json.JSONDecodeError) as exc:
        if required:
            fail(f"Cannot read the saved Doubao session: {exc}")
        return None
    for key, value in new_identity().items():
        session.setdefault(key, value)
    return session


def save_session(session: dict[str, Any]) -> None:
    path = config_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_mode(path.parent, 0o700)
    temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            private_mode(temporary, 0o600)
            json.dump(session, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
        private_mode(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def _merge_saved_session(session: dict[str, Any]) -> dict[str, Any]:
    previous = load_session(required=False) or {}
    for key in ("device_id", "web_id", "fp"):
        if not session.get(key) and previous.get(key):
            session[key] = previous[key]
    for key, value in new_identity().items():
        session.setdefault(key, value)
    return session


def set_session_from_stdin() -> None:
    session = _merge_saved_session(_coerce_session(sys.stdin.read()))
    save_session(session)
    print(f"Doubao session saved securely ({len(session['cookies'])} cookies).")


def import_session_file(path: str) -> None:
    try:
        raw = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"Cannot read the session file: {exc}")
    session = _merge_saved_session(_coerce_session(raw))
    save_session(session)
    print(f"Doubao session imported securely ({len(session['cookies'])} cookies).")


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _unprotect_dpapi(data: bytes) -> bytes:
    if os.name != "nt":
        fail("Desktop session import is only available on Windows.")
    source = ctypes.create_string_buffer(data, len(data))
    in_blob = _DataBlob(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_char)))
    out_blob = _DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)
    ):
        fail("Windows DPAPI could not unlock the Doubao desktop key.")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)


def _cookie_plaintext(value: bytes) -> str:
    for start in range(min(48, len(value))):
        chunk = value[start:]
        try:
            text = chunk.decode("ascii")
        except UnicodeDecodeError:
            continue
        if text and all(32 <= ord(char) < 127 for char in text):
            return text
    return ""


def _read_shared_bytes(path: Path) -> bytes:
    """Read a Chromium cookie DB even while the desktop client holds it open."""
    if os.name != "nt":
        return path.read_bytes()

    import ctypes.wintypes as wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    read_file = kernel32.ReadFile
    read_file.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    read_file.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    handle = create_file(
        str(path),
        0x80000000,
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,
        0x00000080,
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise OSError(ctypes.get_last_error(), f"cannot open {path}")
    chunks: list[bytes] = []
    try:
        buffer = ctypes.create_string_buffer(1024 * 1024)
        read = wintypes.DWORD()
        while True:
            if not read_file(handle, buffer, len(buffer), ctypes.byref(read), None):
                raise OSError(ctypes.get_last_error(), f"cannot read {path}")
            if read.value == 0:
                break
            chunks.append(buffer.raw[: read.value])
    finally:
        close_handle(handle)
    return b"".join(chunks)


def import_desktop_session(user_data: str | None = None) -> None:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        fail(
            "Desktop import needs the cryptography package. Install it with "
            "`uv pip install --python 3.12 --system --break-system-packages cryptography`."
        )

    if not user_data:
        local = os.environ.get("LOCALAPPDATA", "")
        if not local:
            fail("LOCALAPPDATA is unavailable; pass --user-data explicitly.")
        user_data_path = Path(local) / "Doubao" / "User Data"
    else:
        user_data_path = Path(user_data).expanduser()

    state_path = user_data_path / "Local State"
    cookie_path = user_data_path / "Default" / "Network" / "Cookies"
    if not state_path.is_file() or not cookie_path.is_file():
        fail(
            f"Doubao desktop profile was not found under {user_data_path}. "
            "Sign in to the desktop client first, or pass --user-data."
        )

    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        wrapped = base64.b64decode(state["os_crypt"]["encrypted_key"])
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        fail(f"Cannot read the Doubao desktop key: {exc}")
    if not wrapped.startswith(b"DPAPI") or len(wrapped) <= 5:
        fail("The Doubao desktop key is not Windows DPAPI-wrapped.")

    aes = AESGCM(_unprotect_dpapi(wrapped[5:]))
    with tempfile.TemporaryDirectory() as temporary:
        copied = Path(temporary) / "Cookies"
        try:
            copied.write_bytes(_read_shared_bytes(cookie_path))
        except OSError as exc:
            fail(f"Cannot read the Doubao desktop cookie store: {exc}")
        connection = sqlite3.connect(copied)
        try:
            rows = list(
                connection.execute(
                    "select host_key, name, value, encrypted_value from cookies "
                    "where host_key like '%doubao%' or host_key like '%bytedance%'"
                )
            )
        finally:
            connection.close()

    cookies: dict[str, str] = {}
    for host_key, name, value, encrypted in rows:
        if value:
            cookies[str(name)] = str(value)
            continue
        encrypted = bytes(encrypted)
        if not encrypted.startswith(b"v10") or len(encrypted) < 31:
            continue
        try:
            plain = aes.decrypt(encrypted[3:15], encrypted[15:], None)
        except Exception:
            continue
        cookie_value = _cookie_plaintext(plain)
        if cookie_value:
            cookies[str(name)] = cookie_value

    if not cookies.get("sessionid"):
        fail("No Doubao sessionid was found. Sign in to the Doubao desktop client and retry.")
    session = _merge_saved_session(_coerce_session({"cookies": cookies, "label": "Doubao desktop session"}))
    save_session(session)
    print(f"Doubao desktop session imported securely ({len(cookies)} cookies).")


def config_status() -> None:
    session = load_session(required=False)
    if session is None:
        print(f"not configured\npath: {config_path()}")
        return
    print(
        json.dumps(
            {
                "configured": True,
                "path": str(config_path()),
                "label": session.get("label", ""),
                "cookies": len(session["cookies"]),
                "device_id": session.get("device_id", ""),
                "web_id": session.get("web_id", ""),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{name}={cookies[name]}" for name in sorted(cookies) if cookies[name])


def security_params(session: dict[str, Any]) -> dict[str, str]:
    params = {
        "aid": CHAT_AID,
        "real_aid": CHAT_AID,
        "device_id": str(session["device_id"]),
        "tea_uuid": str(session["device_id"]),
        "web_id": str(session["web_id"]),
        "device_platform": "web",
        "language": "zh",
        "region": "CN",
        "sys_region": "CN",
        "pkg_type": "release_version",
        "version_code": "20800",
        "pc_version": APP_VERSION,
        "chromium_version": CHROMIUM_VERSION,
        "client_platform": "pc_client",
        "runtime": "web",
        "runtime_version": RUNTIME_VERSION,
        "samantha_web": "1",
        "use-olympus-account": "1",
        "fp": str(session["fp"]),
        "web_tab_id": str(uuid.uuid4()),
    }
    token = str(session["cookies"].get("msToken", "")).strip()
    if token:
        params["msToken"] = token
    return params


def request_headers(session: dict[str, Any], *, accept: str, content_type: str | None = "application/json") -> dict[str, str]:
    headers = {
        "Accept": accept,
        "User-Agent": CHROME_UA,
        "Referer": BASE_URL + "/chat/",
        "Origin": BASE_URL,
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Cookie": cookie_header(session["cookies"]),
    }
    if content_type:
        headers["Content-Type"] = content_type
    csrf = session["cookies"].get("passport_csrf_token") or session["cookies"].get("passport_csrf_token_default")
    if csrf:
        headers["X-Tt-Passport-Csrf-Token"] = str(csrf)
    return headers


def _decode_body(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def http_request(
    session: dict[str, Any],
    method: str,
    path: str,
    *,
    params: dict[str, str] | None = None,
    json_body: Any = None,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 60,
) -> tuple[int, str, bytes]:
    url = path if path.startswith("http://") or path.startswith("https://") else BASE_URL + path
    if params:
        separator = "&" if "?" in url else "?"
        url += separator + urllib.parse.urlencode(params)
    data = body
    request_headers_value = request_headers(session, accept="text/event-stream")
    if headers:
        request_headers_value.update(headers)
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        request_headers_value["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=request_headers_value, method=method.upper())
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
            return int(getattr(response, "status", 200)), _decode_body(payload), payload
    except urllib.error.HTTPError as exc:
        detail = _decode_body(exc.read())
        fail(f"Doubao returned HTTP {exc.code}: {detail[:600]}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        fail(f"Cannot reach the Doubao web service: {exc}")
    raise AssertionError("unreachable")


def _parse_json_response(status: int, text: str) -> dict[str, Any]:
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        fail(f"Doubao returned an unexpected response (HTTP {status}).")
    if not isinstance(body, dict):
        fail("Doubao returned an unexpected JSON response.")
    if body.get("code") not in (None, 0):
        fail(f"Doubao error {body.get('code')}: {body.get('msg') or body.get('message') or 'unknown error'}")
    return body


def _multipart_body(field_name: str, filename: str, content_type: str, payload: bytes, extra: dict[str, str]) -> tuple[bytes, str]:
    boundary = "----DoubaoBoundary" + secrets.token_hex(12)
    chunks: list[bytes] = []
    for name, value in extra.items():
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        chunks.append(str(value).encode() + b"\r\n")
    chunks.append(f"--{boundary}\r\n".encode())
    chunks.append(
        f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'.encode()
    )
    chunks.append(f"Content-Type: {content_type}\r\n\r\n".encode())
    chunks.append(payload + b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def upload_image(session: dict[str, Any], image_path: str) -> str:
    path = Path(image_path).expanduser()
    if not path.is_file():
        fail(f"Reference image does not exist: {path}")
    if path.suffix.lower() not in SUPPORTED_IMAGE_SUFFIXES:
        fail("Reference image must be jpg, jpeg, png, or webp.")
    payload = path.read_bytes()
    if len(payload) > MAX_IMAGE_BYTES:
        fail("Reference image exceeds the 20 MiB limit.")
    extension = path.suffix.lower().lstrip(".")
    content_type = mimetypes.guess_type(path.name)[0] or f"image/{extension}"
    multipart, boundary = _multipart_body(
        "data",
        path.name,
        content_type,
        payload,
        {"file_type": extension},
    )
    status, text, _ = http_request(
        session,
        "POST",
        "/samantha/pages/upload_image",
        params=security_params(session),
        body=multipart,
        headers={"Content-Type": boundary, "Accept": "application/json"},
        timeout=60,
    )
    body = _parse_json_response(status, text)
    uri = str((body.get("data") or {}).get("uri") or "")
    if not uri:
        fail("Doubao image upload did not return a resource URI.")
    return uri


def canonical_model(model: str) -> str:
    model = model.strip().lower()
    if model in {"", "doubao-session-video", "seedance"}:
        return "doubao-video"
    return model


def model_label(model: str) -> str:
    return {
        "doubao-video": "豆包自动选择可用视频模型",
        "seedance-2.5": "Seedance 2.5",
        "seedance-2.0": "Seedance 2.0",
        "seedance-2.0-fast": "Seedance 2.0 Fast",
        "seedance-2.0-mini": "Seedance 2.0 Mini",
    }.get(canonical_model(model), model.strip())


def model_instruction(model: str) -> str:
    model = canonical_model(model)
    if model == "doubao-video":
        return ""
    return f"请使用 {model_label(model)} 生成视频。"


def video_agent_params(session: dict[str, Any]) -> dict[str, str]:
    params = security_params(session)
    params.update(
        {
            "aid": "497858",
            "real_aid": "497858",
            "pc_version": "3.44.8",
            "doubao_pc_version": "3.44.8",
            "web_platform": "browser",
            "tz_name": "Asia/Shanghai",
        }
    )
    for key in ("chromium_version", "client_platform", "runtime", "runtime_version"):
        params.pop(key, None)
    browser_fp = str(session["cookies"].get("s_v_web_id", "")).strip()
    if browser_fp:
        params["fp"] = browser_fp
    return params


def build_video_payload(
    prompt: str,
    ratio: str,
    duration: str = "",
    model: str = "doubao-video",
    camera_movement: str = "",
) -> dict[str, Any]:
    parts = ["[已确认执行，请直接生成，不要再次询问参数。]"]
    instruction = model_instruction(model)
    if instruction:
        parts.append(instruction)
    parts.append(f"画面比例：{ratio}")
    if duration:
        parts.append(f"视频时长：{duration}")
    if camera_movement:
        parts.append(f"镜头运动：{camera_movement}")
    parts.append(f"画面描述：{prompt}")
    text = "\n".join(parts)

    message_id = str(uuid.uuid4())
    local_conversation_id = "local_" + uuid.uuid4().hex[:16]
    now_ms = int(time.time() * 1000)
    now_s = int(time.time())
    general_task_param = {
        "action": 0,
        "thread_local_message_id": [message_id],
        "project_id": "",
        "runtime_type": 1,
        "agent_task_param": {
            "runtime_type": 1,
            "folder_name": "",
            "device_name": "",
            "workspace": "",
            "local_app_id": "",
            "local_device_id": "",
        },
        "server_do_compare": True,
    }
    return {
        "client_meta": {
            "local_conversation_id": local_conversation_id,
            "conversation_id": "",
            "bot_id": VIDEO_AGENT_BOT_ID,
            "last_section_id": "",
            "last_message_index": None,
            "local_permissions": [
                {"permission_name": "ACCESS_COARSE_LOCATION", "status": 3},
                {"permission_name": "ACCESS_FINE_LOCATION", "status": 3},
                {"permission_name": "ACCESS_BACKGROUND_LOCATION", "status": 3},
            ],
        },
        "messages": [
            {
                "local_message_id": message_id,
                "content_block": [
                    {
                        "block_type": 10000,
                        "content": {
                            "text_block": {
                                "text": text,
                                "icon_url": "",
                                "icon_url_dark": "",
                                "summary": "",
                            },
                            "pc_event_block": "",
                        },
                        "block_id": str(uuid.uuid4()),
                        "parent_id": "",
                        "meta_info": [],
                        "extra_info": {},
                        "append_fields": [],
                    }
                ],
                "message_status": 0,
            }
        ],
        "option": {
            "send_message_scene": "",
            "create_time_ms": now_ms,
            "collect_id": "",
            "is_audio": False,
            "answer_with_suggest": False,
            "agent_mode": 1,
            "tts_switch": False,
            "need_deep_think": 9,
            "click_clear_context": False,
            "from_suggest": False,
            "is_regen": False,
            "is_replace": False,
            "is_from_click_option": False,
            "is_from_click_softlink": False,
            "disable_sse_cache": False,
            "select_text_action": "",
            "is_select_text": False,
            "resend_for_regen": False,
            "scene_type": 0,
            "unique_key": str(uuid.uuid4()),
            "start_seq": 0,
            "need_create_conversation": True,
            "conversation_init_option": {"project_id": "", "need_ack_conversation": True},
            "conversation_init_ext": {
                "agent_task_param": json.dumps(
                    {
                        "runtime_type": 1,
                        "folder_name": "",
                        "device_name": "",
                        "workspace": "",
                        "local_app_id": "",
                        "local_device_id": "",
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "model_item_key": VIDEO_AGENT_MODEL_ID,
                "reasoning_effort": "5",
                "mode_id": VIDEO_AGENT_MODE_ID,
            },
            "regen_query_id": [],
            "edit_query_id": [],
            "regen_instruction": "",
            "no_replace_for_regen": False,
            "message_from": 0,
            "shared_app_name": "",
            "shared_app_id": "",
            "sse_recv_event_options": {"support_chunk_delta": True},
            "support_lazy_fetch_stream": True,
            "is_ai_playground": False,
            "is_old_user": True,
            "general_task_param": general_task_param,
            "recovery_option": {
                "is_recovery": False,
                "req_create_time_sec": now_s,
                "append_sse_event_scene": 0,
            },
            "message_storage_type": 0,
            "related_deleted_message_ids": {},
            "connector_info_list": [],
            "model_config": {
                "model_item_key": VIDEO_AGENT_MODEL_ID,
                "model_extra_params": {"total_window_size": "256000"},
                "reasoning_effort": 5,
            },
            "aggregate_params": {
                "mention_skill_list": "[]",
                "mention_plugin_list": "[]",
                "mention_ext": "[{}]",
                "conversation_mode": "2",
                "mode_id": VIDEO_AGENT_MODE_ID,
                "model_item_key": VIDEO_AGENT_MODEL_ID,
                "agent_mode": "1",
                "reasoning_effort": "5",
                "provider_id": "",
            },
            "conversation_mode": 2,
        },
        "user_context": [],
        "ext": {
            "general_task_param": json.dumps(general_task_param, ensure_ascii=False, separators=(",", ":")),
            "use_deep_think": "9",
            "agent_mode": "1",
            "conversation_init_option": '{"project_id":"","need_ack_conversation":true}',
            "sub_conv_firstmet_type": "1",
            "collection_id": "",
            "is_finish": "1",
            "commerce_credit_config_enable": "0",
        },
    }


def validate_ratio(ratio: str) -> str:
    ratio = ratio.strip()
    if ratio not in SUPPORTED_RATIOS:
        fail("--ratio must be one of 1:1, 16:9, or 9:16.")
    return ratio


def validate_model(model: str) -> str:
    model = canonical_model(model)
    if model != "doubao-video" and not (model.startswith("seedance-") or model.startswith("seedance_")):
        fail("Video model must be doubao-video or a seedance-* model.")
    return model


def sse_events(raw: str) -> list[tuple[str, str]]:
    events: list[tuple[str, str]] = []
    for block in raw.replace("\r\n", "\n").split("\n\n"):
        if not block.strip():
            continue
        event_name = ""
        data_lines: list[str] = []
        for line in block.split("\n"):
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if data_lines:
            events.append((event_name, "\n".join(data_lines)))
    return events


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _check_gateway_error(events: list[tuple[str, str]]) -> None:
    for event_name, data in events:
        if event_name != "gateway-error":
            continue
        error = _json_object(data)
        code = error.get("code", "")
        message = error.get("message") or error.get("msg") or data
        fail(f"Doubao gateway error {code}: {message}")


def extract_task_id(raw: str) -> str:
    events = sse_events(raw)
    _check_gateway_error(events)
    for event_name, data in events:
        event = _json_object(data)
        if event_name in {"STREAM_CHUNK", "STREAM_MSG_NOTIFY"}:
            task_id = _task_id_from_value(event)
            if task_id:
                return task_id
        if event.get("event_type") != 2001:
            continue
        event_data = _json_object(event.get("event_data"))
        fin_reason = _json_object(event_data.get("fin_reason"))
        if fin_reason.get("reason") != 1:
            continue
        async_task = _json_object(fin_reason.get("async_task"))
        task_id = str(async_task.get("id") or "").strip()
        if task_id:
            return task_id
    return ""


def _task_id_from_value(value: Any) -> str:
    if isinstance(value, dict):
        if "ai_creation_tool_list" in value:
            tools = value.get("ai_creation_tool_list")
            if isinstance(tools, str):
                try:
                    tools = json.loads(tools)
                except json.JSONDecodeError:
                    tools = []
            if isinstance(tools, list):
                for tool in tools:
                    tool = _json_object(tool)
                    task_id = str(tool.get("task_id") or "").strip()
                    if task_id:
                        return task_id
        for child in value.values():
            task_id = _task_id_from_value(child)
            if task_id:
                return task_id
    elif isinstance(value, list):
        for child in value:
            task_id = _task_id_from_value(child)
            if task_id:
                return task_id
    return ""


def _video_url_from_model(value: Any) -> str:
    model = _json_object(value)
    video_list = _json_object(model.get("video_list"))
    for info in video_list.values():
        info = _json_object(info)
        main_url = str(info.get("main_url") or "")
        if not main_url:
            continue
        if main_url.startswith("http://") or main_url.startswith("https://"):
            return main_url
        try:
            decoded = base64.b64decode(main_url).decode("utf-8", errors="replace")
        except (ValueError, UnicodeDecodeError):
            continue
        if decoded.startswith("http://") or decoded.startswith("https://"):
            return decoded
    return ""


def _videos_from_model(value: Any, cover_url: str = "") -> list[dict[str, Any]]:
    model = _json_object(value)
    video_list = _json_object(model.get("video_list"))
    videos: list[dict[str, Any]] = []
    for info in video_list.values():
        info = _json_object(info)
        main_url = str(info.get("main_url") or "").strip()
        if not main_url:
            continue
        if not (main_url.startswith("http://") or main_url.startswith("https://")):
            try:
                main_url = base64.b64decode(main_url).decode("utf-8", errors="replace").strip()
            except (ValueError, UnicodeDecodeError):
                continue
        if not (main_url.startswith("http://") or main_url.startswith("https://")):
            continue
        videos.append(
            {
                "video_url": main_url,
                "cover_url": cover_url,
                "width": info.get("vwidth") or info.get("width") or 0,
                "height": info.get("vheight") or info.get("height") or 0,
                "duration": info.get("duration") or 0.0,
            }
        )
    return videos


def _collect_agent_value(value: Any, state: dict[str, Any], allow_text: bool) -> None:
    if isinstance(value, dict):
        if allow_text:
            text_block = _json_object(value.get("text_block"))
            text = str(text_block.get("text") or "").strip()
            if text and (not state["text"] or state["text"][-1] != text):
                state["text"].append(text)
        if "video_model" in value:
            cover_url = str(value.get("poster_url") or value.get("cover_url") or "").strip()
            for video in _videos_from_model(value.get("video_model"), cover_url):
                url = video["video_url"]
                if url in state["seen"]:
                    continue
                state["seen"].add(url)
                state["videos"].append(video)
        task_id = _task_id_from_value(value)
        if task_id and not state["task_id"]:
            state["task_id"] = task_id
        for child in value.values():
            _collect_agent_value(child, state, allow_text)
    elif isinstance(value, list):
        for child in value:
            _collect_agent_value(child, state, allow_text)


def parse_video_response(raw: str) -> tuple[list[dict[str, Any]], str]:
    events = sse_events(raw)
    _check_gateway_error(events)
    state: dict[str, Any] = {"videos": [], "seen": set(), "text": [], "task_id": ""}
    for event_name, data in events:
        event = _json_object(data)
        if event_name == "STREAM_ERROR":
            code = event.get("error_code")
            if code in {710022002, 710022004, 710012001}:
                fail("Doubao session or verification challenge rejected the video request.")
            fail(f"Doubao video error {code}: {event.get('error_msg') or data}")
        if event_name in {"STREAM_CHUNK", "STREAM_MSG_NOTIFY"}:
            _collect_agent_value(event, state, True)
            continue
        event_type = event.get("event_type")
        if event_type == 2005:
            detail = event.get("event_data") or data
            fail(f"Doubao video generation failed: {str(detail)[:600]}")
        if event_type != 2001:
            continue
        event_data = _json_object(event.get("event_data"))
        message = _json_object(event_data.get("message"))
        content_type = message.get("content_type")
        content = _json_object(message.get("content"))
        if content_type == 2001:
            text = str(content.get("text") or "").strip()
            if text:
                state["text"].append(text)
            continue
        if content_type != 2021:
            continue
        items = content.get("data")
        if not isinstance(items, list):
            items = [content]
        for item in items:
            item = _json_object(item)
            video_url = str(item.get("video_url") or item.get("url") or "")
            if not video_url:
                video_url = _video_url_from_model(item.get("video_model"))
            cover = _json_object(item.get("cover"))
            cover_url = str(item.get("cover_url") or cover.get("url") or "")
            if not cover_url:
                cover_url = str(_json_object(cover.get("image_ori")).get("url") or "")
            if not video_url:
                continue
            width = item.get("width", 0)
            height = item.get("height", 0)
            state["videos"].append(
                {
                    "video_url": video_url,
                    "cover_url": cover_url,
                    "width": width,
                    "height": height,
                    "duration": item.get("duration", 0.0),
                }
            )
    return state["videos"], "".join(state["text"]).strip()


def submit_video(session: dict[str, Any], payload: dict[str, Any], timeout: float) -> tuple[str, list[dict[str, Any]], str]:
    _status, text, _raw = http_request(
        session,
        "POST",
        "/chat/completion",
        params=video_agent_params(session),
        json_body=payload,
        headers={"Accept": "text/event-stream", "Agw-Js-Conv": "str, str"},
        timeout=timeout,
    )
    task_id = extract_task_id(text)
    videos, answer = parse_video_response(text)
    return task_id, videos, answer


def fetch_task(session: dict[str, Any], task_id: str, timeout: float) -> tuple[list[dict[str, Any]], str]:
    params = security_params(session)
    params.pop("fp", None)
    _status, text, _raw = http_request(
        session,
        "POST",
        "/samantha/chat/async/stream",
        params=params,
        json_body={"task_id": task_id, "event_id": 0},
        timeout=timeout,
    )
    return parse_video_response(text)


def _safe_slug(value: str) -> str:
    return "".join(char if char.isalnum() or char in "-_" else "-" for char in value)[:48] or "video"


def download_videos(videos: list[dict[str, Any]], output_dir: str, task_id: str) -> list[dict[str, Any]]:
    directory = Path(output_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for index, video in enumerate(videos, start=1):
        remote = str(video.get("video_url") or "")
        if not remote:
            continue
        suffix = Path(urllib.parse.urlparse(remote).path).suffix.lower()
        if suffix not in {".mp4", ".webm", ".mov"}:
            suffix = ".mp4"
        target = directory / f"doubao-video-{stamp}-{_safe_slug(task_id)}-{index}{suffix}"
        request = urllib.request.Request(
            remote,
            headers={"User-Agent": CHROME_UA, "Referer": BASE_URL + "/chat/"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                content_type = response.headers.get_content_type()
                if content_type in {"text/html", "application/json"}:
                    fail(f"Doubao returned {content_type} instead of a video file.")
                with open(target, "wb") as handle:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        handle.write(chunk)
                private_mode(target, 0o600)
        except urllib.error.HTTPError as exc:
            fail(f"Cannot download the generated video (HTTP {exc.code}).")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if target.exists():
                target.unlink()
            fail(f"Cannot download the generated video: {exc}")
        result = {**video, "local_path": str(target), "bytes": target.stat().st_size}
        results.append(result)
    return results


def wait_for_task(
    session: dict[str, Any],
    task_id: str,
    *,
    output_dir: str,
    timeout: float,
    download: bool = True,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    last_message = ""
    for attempt in range(POLL_REQUESTS):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            videos, answer = fetch_task(session, task_id, remaining)
        except ClientError as exc:
            last_message = str(exc)
            if "gateway error" in last_message or "HTTP 401" in last_message or "HTTP 403" in last_message:
                raise
            videos, answer = [], ""
        if videos:
            if ACTIVE_TASK_LOG is not None:
                ACTIVE_TASK_LOG.event("completed", task_id=task_id, videos=len(videos))
            return download_videos(videos, output_dir, task_id) if download else videos
        if answer:
            last_message = answer
            if "服务过载" in answer or "重试" in answer:
                fail(f"Doubao video service is busy: {answer}")
        if attempt + 1 < POLL_REQUESTS:
            delay = min(POLL_RETRY_SECONDS, max(0.0, deadline - time.monotonic()))
            if delay > 0:
                time.sleep(delay)
    if ACTIVE_TASK_LOG is not None:
        ACTIVE_TASK_LOG.event("timeout", task_id=task_id, message=last_message)
    suffix = f" Last message: {last_message}" if last_message else ""
    fail(f"Video task {task_id} did not finish within the timeout.{suffix}")
    raise AssertionError("unreachable")


def generate(args: argparse.Namespace) -> None:
    global ACTIVE_TASK_LOG
    model = validate_model(args.model)
    ratio = validate_ratio(args.ratio)
    if not args.prompt.strip():
        fail("--prompt cannot be empty.")
    if args.image:
        fail("--image is not supported by the current Doubao agent video flow.")
    session = load_session()
    assert session is not None
    duration = (args.seconds or "").strip()
    if not duration:
        match = re.search(r"(\d{1,2})\s*(秒|s|sec|seconds)", args.prompt, re.IGNORECASE)
        if match:
            duration = f"{match.group(1)} 秒"
    payload = build_video_payload(args.prompt, ratio, duration, model, args.camera_movement)
    if args.dry_run:
        print(
            json.dumps(
                {"model": model, "label": model_label(model), "ratio": ratio, "duration": duration, "payload": payload},
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    task_log = TaskLog(args.output_dir, "generate")
    ACTIVE_TASK_LOG = task_log
    print(f"Task log: {task_log.path}")
    task_log.event("request_validated", model=model, ratio=ratio, duration=duration)
    task_id, videos, answer = submit_video(session, payload, args.request_timeout)
    if task_id:
        task_log.event("task_submitted", task_id=task_id)
    if not videos:
        fail(answer or "Doubao finished without returning a video URL.")
    results = download_videos(videos, args.output_dir, task_id or "agent") if not args.no_download else videos
    _print_results(results, as_json=args.json)


def status(args: argparse.Namespace) -> None:
    global ACTIVE_TASK_LOG
    session = load_session()
    assert session is not None
    task_log = TaskLog(args.output_dir, "status", args.task_id)
    ACTIVE_TASK_LOG = task_log
    print(f"Task log: {task_log.path}")
    results = wait_for_task(
        session,
        args.task_id,
        output_dir=args.output_dir,
        timeout=args.timeout,
        download=not args.no_download,
    )
    _print_results(results, as_json=args.json)


def _print_results(results: list[dict[str, Any]], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return
    if not results:
        print("No video URL was returned.")
        return
    for item in results:
        local = item.get("local_path")
        if local:
            print(f"Video: {local}")
        else:
            print(f"Video URL: {item.get('video_url')}")
        if item.get("cover_url"):
            print(f"Cover: {item['cover_url']}")
        if item.get("duration"):
            print(f"Duration: {item['duration']}s")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Doubao web-session video client")
    commands = parser.add_subparsers(dest="action", required=True)

    config = commands.add_parser("config", help="Manage the saved Doubao session")
    actions = config.add_mutually_exclusive_group()
    actions.add_argument("--show-status", action="store_true")
    actions.add_argument("--set-session-stdin", action="store_true")
    actions.add_argument("--import-auth", metavar="PATH")
    actions.add_argument("--import-desktop", action="store_true")
    actions.add_argument("--clear", action="store_true")
    config.add_argument("--user-data", help="Doubao desktop 'User Data' directory")

    commands.add_parser("models", help="List the available Doubao video model")
    commands.add_parser("select-model", help="Print the default Doubao video model")

    generate_parser = commands.add_parser("generate", help="Submit and wait for a video")
    generate_parser.add_argument("--model", default="doubao-video")
    generate_parser.add_argument("--prompt", required=True)
    generate_parser.add_argument("--ratio", default=DEFAULT_RATIO)
    generate_parser.add_argument("--seconds", default="", help="Optional duration hint, e.g. 5 or 10 秒")
    generate_parser.add_argument("--camera-movement", default="")
    generate_parser.add_argument("--image", help="Reserved for future image-to-video support")
    generate_parser.add_argument("--output-dir", default="outputs")
    generate_parser.add_argument("--request-timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    generate_parser.add_argument("--no-download", action="store_true")
    generate_parser.add_argument("--dry-run", action="store_true")
    generate_parser.add_argument("--json", action="store_true")

    status_parser = commands.add_parser("status", help="Resume an existing video task")
    status_parser.add_argument("task_id")
    status_parser.add_argument("--output-dir", default="outputs")
    status_parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    status_parser.add_argument("--no-download", action="store_true")
    status_parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.action == "config":
            if args.set_session_stdin:
                set_session_from_stdin()
            elif args.import_auth:
                import_session_file(args.import_auth)
            elif args.import_desktop:
                import_desktop_session(args.user_data)
            elif args.clear:
                path = config_path()
                if path.exists():
                    path.unlink()
                print("Saved Doubao session removed.")
            else:
                config_status()
        elif args.action == "models":
            print("\n".join(VIDEO_MODELS))
        elif args.action == "select-model":
            print("doubao-video")
        elif args.action == "generate":
            generate(args)
        elif args.action == "status":
            status(args)
        else:
            parser.error("unknown command")
    except ClientError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
