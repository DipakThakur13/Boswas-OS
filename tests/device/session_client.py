#!/usr/bin/env python3
"""Call the user's Boswas session agent (as that user) and print the result as JSON.

Used by device_scenario.py, which runs as root: the session agent only
accepts its own user (SO_PEERCRED), exactly as for the Compatibility Manager.

  session_client.py OP '{"param": ...}'          one operation
  session_client.py --job OP '{"param": ...}'    an operation that returns a job: wait for it

Prints {"ok": true, "result": ...} or {"ok": false, "error": {"code", "message"}}.
"""

import json
import os
import sys
import time

sys.path.insert(0, "/usr/lib/boswas/python")

from boswas_agent.localapi import ApiClient, ApiError  # noqa: E402


def main() -> int:
    args = sys.argv[1:]
    wait = args[:1] == ["--job"]
    if wait:
        args = args[1:]
    op, params = args[0], json.loads(args[1]) if len(args) > 1 else {}
    socket_path = os.path.join(os.environ["XDG_RUNTIME_DIR"], "boswas", "session.sock")
    client = ApiClient(socket_path, timeout=600)
    try:
        result = client.call(op, **params)
        if wait:
            deadline = time.monotonic() + 1800
            while True:
                job = client.call("jobs.get", job_id=result["job_id"])
                if job["state"] != "running" or time.monotonic() > deadline:
                    result = job
                    break
                time.sleep(1)
        print(json.dumps({"ok": True, "result": result}))
    except ApiError as exc:
        print(json.dumps({"ok": False, "error": {"code": exc.code, "message": exc.message}}))
    except ConnectionError as exc:
        print(json.dumps({"ok": False, "error": {"code": "UNAVAILABLE", "message": str(exc)}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
