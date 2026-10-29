"""Project-scoped OpenCode server and session adapter for Motion Director."""

from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional


class OpenCodeBridgeError(RuntimeError):
    pass


class OpenCodeBridge:
    def __init__(self, project_root: Path, state_root: Path, executable: Optional[str] = None):
        self.project_root = Path(project_root).resolve()
        self.state_root = Path(state_root).resolve()
        self.executable = executable or os.environ.get("OPENCODE_BIN") or shutil.which("opencode") or "opencode"
        self.process: Optional[subprocess.Popen] = None
        self.base_url = ""
        self.username = "motion"
        self.password = ""
        self._log_handle = None

    def start(self) -> dict:
        if self.healthy():
            return {"running": True, "base_url": self.base_url}
        self.stop()
        self.state_root.mkdir(parents=True, exist_ok=True)
        sock = socket.socket()
        try:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        finally:
            sock.close()
        self.username = "motion-" + uuid.uuid4().hex[:8]
        self.password = secrets.token_urlsafe(32)
        self.base_url = "http://127.0.0.1:%d" % port
        env = os.environ.copy()
        env["OPENCODE_SERVER_USERNAME"] = self.username
        env["OPENCODE_SERVER_PASSWORD"] = self.password
        log_path = self.state_root / "opencode-server.log"
        self._log_handle = log_path.open("ab")
        try:
            self.process = subprocess.Popen(
                [self.executable, "serve", "--hostname", "127.0.0.1", "--port", str(port)],
                cwd=str(self.project_root), env=env, stdin=subprocess.DEVNULL,
                stdout=self._log_handle, stderr=self._log_handle,
                start_new_session=True,
            )
        except Exception:
            self._close_log()
            raise
        deadline = time.monotonic() + 20.0
        last_error = None
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise OpenCodeBridgeError("OpenCode server завершился при запуске")
            try:
                health = self._request("GET", "/global/health", timeout=1.0)
                return {"running": True, "base_url": self.base_url, "health": health}
            except Exception as exc:
                last_error = exc
                time.sleep(0.2)
        self.stop()
        raise OpenCodeBridgeError("OpenCode server не ответил за 20 секунд: %s" % (last_error or "timeout"))

    def healthy(self) -> bool:
        if not self.process or self.process.poll() is not None or not self.base_url:
            return False
        try:
            self._request("GET", "/global/health", timeout=1.5)
            return True
        except Exception:
            return False

    def stop(self) -> None:
        process = self.process
        self.process = None
        if process is not None and process.poll() is None:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    process.terminate()
                except Exception:
                    pass
            try:
                process.wait(timeout=5)
            except Exception:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except Exception:
                    try:
                        process.kill()
                    except Exception:
                        pass
                try:
                    process.wait(timeout=2)
                except Exception:
                    pass
        self.base_url = ""
        self.password = ""
        self._close_log()

    def create_session(self, title: str, model_id: str, agent: str = "motion-director",
                       directory: Optional[Path] = None) -> str:
        provider, model = _split_model(model_id)
        path = "/session"
        session_directory = Path(directory).resolve() if directory is not None else self.project_root
        path += "?" + urllib.parse.urlencode({"directory": str(session_directory)})
        response = self._request(
            "POST", path,
            body={
                "title": str(title)[:160],
                "agent": str(agent),
                "model": {"providerID": provider, "id": model},
            },
            timeout=30,
        )
        session_id = str((response or {}).get("id") or "")
        if not session_id.startswith("ses"):
            raise OpenCodeBridgeError("OpenCode не вернул id Motion-сессии")
        return session_id

    def providers(self) -> dict:
        result = self._request("GET", "/provider", timeout=20)
        return result if isinstance(result, dict) else {}

    def messages(self, session_id: str, limit: int = 50, directory: Optional[Path] = None) -> list:
        params = {"limit": max(1, min(100, int(limit)))}
        params["directory"] = str((Path(directory).resolve() if directory is not None else self.project_root))
        query = urllib.parse.urlencode(params)
        result = self._request("GET", "/session/%s/message?%s" % (urllib.parse.quote(session_id, safe=""), query), timeout=30)
        return result if isinstance(result, list) else []

    def prompt_async(self, session_id: str, model_id: str, parts: list,
                     format_schema: Optional[dict] = None, message_id: Optional[str] = None,
                     system: str = "", agent: str = "motion-director",
                     directory: Optional[Path] = None) -> None:
        provider, model = _split_model(model_id)
        body = {
            "messageID": message_id or ("msg_" + uuid.uuid4().hex),
            "agent": str(agent),
            "model": {"providerID": provider, "modelID": model},
            "parts": parts,
        }
        if system:
            body["system"] = str(system)[:20_000]
        if format_schema:
            body["format"] = {"type": "json_schema", "schema": format_schema}
        path = "/session/%s/prompt_async" % urllib.parse.quote(session_id, safe="")
        session_directory = Path(directory).resolve() if directory is not None else self.project_root
        path += "?" + urllib.parse.urlencode({"directory": str(session_directory)})
        self._request(
            "POST", path,
            body=body, timeout=60, allow_empty=True,
        )

    def session_status(self, session_id: str) -> dict:
        data = self._request("GET", "/session/status", timeout=10)
        status = data.get(session_id) if isinstance(data, dict) else None
        return status if isinstance(status, dict) else {"type": "idle"}

    def abort(self, session_id: str) -> None:
        self._request("POST", "/session/%s/abort" % urllib.parse.quote(session_id, safe=""), body={}, timeout=10)

    def _request(self, method: str, path: str, body: Optional[dict] = None,
                 timeout: float = 30, allow_empty: bool = False) -> Any:
        if not self.base_url or not self.password:
            raise OpenCodeBridgeError("OpenCode server ещё не запущен")
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        auth = base64.b64encode((self.username + ":" + self.password).encode("utf-8")).decode("ascii")
        headers = {"Authorization": "Basic " + auth, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read(8 * 1024 * 1024 + 1)
                if len(payload) > 8 * 1024 * 1024:
                    raise OpenCodeBridgeError("Ответ OpenCode превысил лимит размера")
                if not payload and allow_empty:
                    return None
                if not payload:
                    return None
                try:
                    return json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise OpenCodeBridgeError("OpenCode вернул некорректный JSON") from exc
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read(4096).decode("utf-8", errors="replace")
                try:
                    detail = str(json.loads(detail).get("message") or json.loads(detail).get("error") or detail)
                except Exception:
                    pass
            except Exception:
                pass
            raise OpenCodeBridgeError("OpenCode HTTP %d%s" % (exc.code, ": " + detail[:600] if detail else "")) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OpenCodeBridgeError("Не удалось связаться с локальным OpenCode server: %s" % exc) from exc

    def _close_log(self) -> None:
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except Exception:
                pass
            self._log_handle = None


def _split_model(model_id: str) -> tuple:
    value = str(model_id or "").strip()
    if value.count("/") != 1:
        raise OpenCodeBridgeError("Модель должна иметь формат provider/model-id")
    provider, model = value.split("/", 1)
    if not provider or not model or any(ch.isspace() for ch in value):
        raise OpenCodeBridgeError("Выбрана некорректная модель OpenCode")
    return provider, model


def assistant_text(message: dict) -> str:
    """Extract only user-visible assistant text, never tool/log internals."""
    info = message.get("info") if isinstance(message, dict) else None
    if not isinstance(info, dict) or info.get("role") != "assistant":
        return ""
    parts = message.get("parts") or []
    return "\n".join(
        str(part.get("text") or "") for part in parts
        if isinstance(part, dict) and part.get("type") == "text" and part.get("text")
    ).strip()
