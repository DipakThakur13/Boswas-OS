"""Typed access to boswas-winapp, the WinCompat backend.

Every Windows-application operation of the agent, the session agent and the
Compatibility Manager goes through these calls: boswas-winapp with a fixed
path, an argument list (never a shell), validated application IDs and
installer paths, and --json output. Policy, manifests, prefixes, bubblewrap
and AppArmor stay entirely inside boswas-winapp; nothing here re-implements
or bypasses them.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path

from boswas_compat.executor import sanitize
from boswas_compat.manifest import valid_id

from .errors import BackendError

WINAPP = "/usr/bin/boswas-winapp"
DEFAULT_TIMEOUT = 300
LONG_TIMEOUT = 4 * 3600
MAX_OUTPUT = 8 * 1024 * 1024
# Environment passed to boswas-winapp: what it needs to find the user's
# session (display, audio socket, locale), nothing else.
PASSED_ENV = ("HOME", "USER", "LOGNAME", "LANG", "TZ", "DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY",
              "XDG_RUNTIME_DIR")


def check_id(app_id: object) -> str:
    if not valid_id(app_id):
        raise BackendError(f"invalid application ID {str(app_id)[:100]!r}", reason="usage", exit_code=2)
    return app_id


def check_installer(path: object) -> str:
    if not isinstance(path, str) or not path.startswith("/") or "\0" in path or len(path) > 4096:
        raise BackendError("the installer must be given as an absolute path", reason="usage", exit_code=2)
    try:
        if not Path(path).is_file():
            raise BackendError(f"installer not found: {sanitize(path)}", reason="not-found", exit_code=3)
    except OSError as exc:
        raise BackendError(f"installer not readable: {exc.strerror}", reason="not-found", exit_code=3) from exc
    return path


def clean(value):
    """Strings from the backend reach a GUI or the Control Plane: no control characters."""
    if isinstance(value, str):
        return sanitize(value)
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    return value


class Winapp:
    def __init__(self, binary: str = WINAPP, run=subprocess.run, popen=subprocess.Popen, env: dict | None = None):
        self.binary, self._run, self._popen = binary, run, popen
        self.base_env = env if env is not None else os.environ

    def environment(self, extra: dict | None = None) -> dict:
        env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"}
        env.update({k: v for k, v in self.base_env.items() if k in PASSED_ENV and isinstance(v, str)})
        env.update({k: v for k, v in (extra or {}).items() if k in PASSED_ENV and isinstance(v, str)})
        return env

    @staticmethod
    def _parse(stdout: str, stderr: str, code: int) -> dict:
        doc = None
        text = stdout.strip()
        if text:
            try:
                doc = json.loads(text)
            except ValueError:
                doc = None
        if isinstance(doc, dict) and isinstance(doc.get("error"), dict):
            err = doc["error"]
            raise BackendError(sanitize(str(err.get("message", "failed")))[:2000],
                               reason=str(err.get("reason", "failed"))[:40], exit_code=code)
        if code != 0 and not (isinstance(doc, dict) and doc.get("command") in ("inspect", "runtime", "repair")):
            tail = sanitize(stderr.strip().splitlines()[-1] if stderr.strip() else f"exit {code}")
            raise BackendError(tail[:2000], reason="failed", exit_code=code)
        if not isinstance(doc, dict):
            raise BackendError("boswas-winapp returned no result", reason="failed", exit_code=code or 70)
        return clean(doc)

    def call(self, *args: str, timeout: float = DEFAULT_TIMEOUT, env: dict | None = None) -> dict:
        try:
            proc = self._run([self.binary, "--json", *args], capture_output=True, text=True, timeout=timeout,
                             check=False, stdin=subprocess.DEVNULL, env=self.environment(env))
        except subprocess.TimeoutExpired as exc:
            raise BackendError(f"boswas-winapp {args[0]} did not finish within {timeout:.0f} s",
                               reason="timeout", exit_code=124) from exc
        except OSError as exc:
            raise BackendError(f"boswas-winapp is not available: {exc.strerror}", reason="unavailable",
                               exit_code=5) from exc
        return self._parse(proc.stdout or "", proc.stderr or "", proc.returncode)

    def stream(self, args: list[str], *, on_line=None, timeout: float = LONG_TIMEOUT, env: dict | None = None) -> dict:
        """Run a long operation, reporting its progress lines (stderr) as they arrive."""
        try:
            proc = self._popen([self.binary, "--json", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               stdin=subprocess.DEVNULL, text=True, env=self.environment(env))
        except OSError as exc:
            raise BackendError(f"boswas-winapp is not available: {exc.strerror}", reason="unavailable",
                               exit_code=5) from exc
        err_lines: list[str] = []

        def pump():
            for line in proc.stderr:
                line = sanitize(line.rstrip("\n"))[:500]
                if len(err_lines) < 2000:
                    err_lines.append(line)
                if on_line:
                    on_line(line)
        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        try:
            stdout = proc.stdout.read(MAX_OUTPUT)
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            proc.terminate()
            raise BackendError(f"boswas-winapp {args[0]} did not finish in time", reason="timeout",
                               exit_code=124) from exc
        finally:
            reader.join(timeout=5)
            proc.stdout.close()
            proc.stderr.close()
        return self._parse(stdout, "\n".join(err_lines), proc.returncode)

    # --- operations ----------------------------------------------------------------------
    def list(self) -> dict:
        return self.call("list")

    def list_all_users(self) -> dict:
        return self.call("list", "--all-users")

    def status(self, app_id: str) -> dict:
        return self.call("status", check_id(app_id))

    def catalog(self) -> dict:
        return self.call("catalog")

    def runtime(self) -> dict:
        return self.call("runtime", timeout=60)

    def inspect(self, path: str, app_id: str | None = None, name: str | None = None) -> dict:
        args = ["inspect", check_installer(path)]
        if app_id:
            args += ["--id", check_id(app_id)]
        if name:
            args += ["--name", _label(name)]
        return self.call(*args, timeout=900)

    def install(self, path: str, app_id: str | None = None, name: str | None = None, *, on_line=None,
                env: dict | None = None) -> dict:
        args = ["install", check_installer(path)]
        if app_id:
            args += ["--id", check_id(app_id)]
        if name:
            args += ["--name", _label(name)]
        return self.stream(args, on_line=on_line, env=env)

    def upgrade(self, app_id: str, path: str, *, on_line=None, env: dict | None = None) -> dict:
        return self.stream(["upgrade", check_id(app_id), check_installer(path)], on_line=on_line, env=env)

    def repair(self, app_id: str, *, on_line=None, env: dict | None = None) -> dict:
        return self.stream(["repair", check_id(app_id)], on_line=on_line, env=env)

    def remove(self, app_id: str) -> dict:
        return self.call("remove", check_id(app_id))

    def stop(self, app_id: str) -> dict:
        return self.call("stop", check_id(app_id), timeout=60)

    def logs(self, app_id: str, kind: str | None = None, lines: int = 200) -> dict:
        args = ["logs", check_id(app_id), "--lines", str(max(0, min(int(lines), 5000)))]
        if kind in ("install", "repair", "launch"):
            args.append(f"--{kind}")
        elif kind not in (None, "latest"):
            raise BackendError(f"unknown log kind {str(kind)[:20]!r}", reason="usage", exit_code=2)
        return self.call(*args)

    def clear_logs(self, app_id: str) -> dict:
        return self.call("logs", check_id(app_id), "--clear")

    def launch(self, app_id: str, *, env: dict | None = None):
        """Start an application in its sandbox; returns the running boswas-winapp process."""
        check_id(app_id)
        try:
            return self._popen([self.binary, "--json", "launch", app_id, "--quiet"], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True,
                               env=self.environment(env), start_new_session=True)
        except OSError as exc:
            raise BackendError(f"boswas-winapp is not available: {exc.strerror}", reason="unavailable",
                               exit_code=5) from exc


def _label(name: object) -> str:
    if not isinstance(name, str) or not name.strip() or len(name) > 80 or any(ord(c) < 32 for c in name):
        raise BackendError("the display name must be a single line of at most 80 characters", reason="usage",
                           exit_code=2)
    return name.strip()


def finish_launch(proc) -> dict:
    """Wait for a launch process and return its result document (or the error)."""
    stdout = proc.stdout.read(MAX_OUTPUT) if proc.stdout else ""
    stderr = proc.stderr.read(MAX_OUTPUT) if proc.stderr else ""
    code = proc.wait()
    for stream in (proc.stdout, proc.stderr):
        if stream:
            stream.close()
    try:
        doc = json.loads(stdout.strip()) if stdout.strip() else None
    except ValueError:
        doc = None
    if isinstance(doc, dict) and isinstance(doc.get("error"), dict):
        return {"ok": False, "exit_code": code, "reason": clean(str(doc["error"].get("reason", "failed")))[:40],
                "message": clean(str(doc["error"].get("message", "")))[:2000]}
    if isinstance(doc, dict):
        return {"ok": True, "exit_code": doc.get("exit_code"), "stopped": doc.get("stopped") is True,
                "timed_out": doc.get("timed_out") is True, "confinement": clean(doc.get("confinement"))}
    tail = sanitize(stderr.strip().splitlines()[-1]) if stderr.strip() else f"exit {code}"
    return {"ok": code == 0, "exit_code": code, "reason": "failed", "message": tail[:2000]}
