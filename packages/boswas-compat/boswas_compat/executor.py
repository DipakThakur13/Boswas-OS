"""Run a sandbox command, capture its output into the application log.

Output of Windows programs is untrusted: it is decoded leniently, stripped of
terminal control sequences before it reaches the user's terminal or the log,
and the log size is capped.
"""

from __future__ import annotations

import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

CONFINEMENT_MARKER = "boswas-winapp-exec: confinement="
REFUSED_MARKER = "boswas-winapp-exec: refused:"
RUNNER_REFUSED_EXIT = 77
MAX_LOG_BYTES = 8 * 1024 * 1024
KILL_GRACE_SECONDS = 10

_CONTROL_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b.|[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def sanitize(text: str) -> str:
    """Remove escape sequences and C0/C1 control characters (keeps newline and tab).

    C1 controls (U+0080-U+009F) matter too: U+009B (CSI) and U+009D (OSC) start
    escape sequences in terminals that honour 8-bit controls.
    """
    return _CONTROL_RE.sub("", text)


@dataclass
class RunResult:
    exit_code: int | None
    confinement: str | None = None
    refused: str | None = None
    timed_out: bool = False
    duration: float = 0.0
    log: str | None = None
    stopped: bool = False

    def to_dict(self) -> dict:
        return {"exit_code": self.exit_code, "confinement": self.confinement, "refused": self.refused,
                "timed_out": self.timed_out, "stopped": self.stopped,
                "duration_seconds": round(self.duration, 1), "log": self.log}


class Executor:
    """Runs bwrap commands. Unit tests replace it with a fake."""

    def run(self, argv: list[str], log_path: Path, *, mirror: TextIO | None = None,
            timeout: float | None = None, title: str = "", stoppable: bool = False) -> RunResult:
        """Run argv, logging its output.

        stoppable: SIGTERM to this process (boswas-winapp stop) ends the
        sandbox like a timeout does, and the result reports stopped=True.
        """
        start = time.monotonic()
        result = RunResult(exit_code=None, log=str(log_path))
        written = 0
        with log_path.open("a", encoding="utf-8", errors="replace") as log:
            if title:
                log.write(f"=== {title} ({time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})\n")
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, close_fds=True)
            timers: list[threading.Timer] = []

            def terminate():
                proc.send_signal(signal.SIGTERM)
                # bwrap (--die-with-parent) takes the sandbox down with it;
                # escalate if it does not stop.
                kill = threading.Timer(KILL_GRACE_SECONDS, lambda: proc.poll() is None and proc.kill())
                kill.daemon = True
                timers.append(kill)
                kill.start()

            if timeout:
                def expire():
                    result.timed_out = True
                    terminate()
                timer = threading.Timer(timeout, expire)
                timer.daemon = True
                timers.append(timer)
                timer.start()
            previous_handler, handler_installed = None, False
            if stoppable and threading.current_thread() is threading.main_thread():
                def on_stop(_signum, _frame):
                    if not result.stopped:
                        result.stopped = True
                        terminate()
                previous_handler = signal.signal(signal.SIGTERM, on_stop)
                handler_installed = True
            try:
                assert proc.stdout is not None
                for raw in proc.stdout:
                    line = sanitize(raw.decode("utf-8", errors="replace").rstrip("\r\n")) + "\n"
                    if result.confinement is None and line.startswith(CONFINEMENT_MARKER):
                        result.confinement = line[len(CONFINEMENT_MARKER):].strip()
                    elif line.startswith(REFUSED_MARKER) and result.refused is None:
                        result.refused = line[len(REFUSED_MARKER):].strip()
                    if written < MAX_LOG_BYTES:
                        log.write(line)
                        written += len(line)
                        if written >= MAX_LOG_BYTES:
                            log.write("=== log truncated (size limit)\n")
                    if mirror is not None and not line.startswith(CONFINEMENT_MARKER):
                        mirror.write(line)
                        mirror.flush()
                result.exit_code = proc.wait()
            except KeyboardInterrupt:
                proc.terminate()
                proc.wait()
                raise
            finally:
                if handler_installed:
                    signal.signal(signal.SIGTERM, previous_handler)
                for t in timers:
                    t.cancel()
                if proc.stdout is not None:
                    proc.stdout.close()
            if result.refused and result.exit_code != RUNNER_REFUSED_EXIT:
                # Only the runner's own refusal (exit 77 with its marker) counts;
                # a Windows program printing the marker text does not.
                result.refused = None
            result.duration = time.monotonic() - start
            how = " (timed out)" if result.timed_out else (" (stopped on request)" if result.stopped else "")
            log.write(f"=== exit {result.exit_code}{how} after {result.duration:.1f} s\n")
        return result
