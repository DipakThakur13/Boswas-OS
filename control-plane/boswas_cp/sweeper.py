"""Background maintenance: offline detection and command expiry."""

from __future__ import annotations

import logging
import threading

from .service import ControlPlane

log = logging.getLogger("boswas-control-plane")


class Sweeper(threading.Thread):
    def __init__(self, cp: ControlPlane, interval: float = 30.0):
        super().__init__(name="boswas-cp-sweeper", daemon=True)
        self.cp, self.interval = cp, interval
        self.stopping = threading.Event()

    def run(self) -> None:
        while not self.stopping.wait(self.interval):
            try:
                result = self.cp.sweep()
                if result["offline"] or result["expired"]:
                    log.info("sweep: %(offline)d device(s) offline, %(expired)d command(s) expired", result)
            except Exception as exc:        # keep sweeping
                log.exception("sweep failed: %s", exc)
            finally:
                self.cp.store.close()       # this thread's connection; reopened on demand

    def stop(self) -> None:
        self.stopping.set()
