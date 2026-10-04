"""TLS HTTP servers of the Control Plane.

One server per listener (api.LISTENERS): on the device port devices
authenticate with client certificates from the device CA (verified by the
TLS layer when presented); on the operator port operators use bearer tokens
and no client certificate is requested. The TLS
handshake runs in the worker thread of each connection, so a slow client
cannot stall the accept loop, and every connection has an idle timeout.
"""

from __future__ import annotations

import logging
import shutil
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .api import DEVICE, OPERATOR, SECURITY_HEADERS, Api, Request, access_line, error, parse_target

log = logging.getLogger("boswas-control-plane")
IDLE_TIMEOUT = 60
MAX_DRAIN = 16 * 1024 * 1024


class TLSServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(self, address, api: Api, context: ssl.SSLContext, listener: str = DEVICE):
        if listener not in (DEVICE, OPERATOR):
            raise ValueError(f"unknown listener {listener!r}")
        self.api, self.context, self.listener = api, context, listener
        family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
        self.address_family = family
        super().__init__(address, Handler)

    def finish_request(self, request, client_address):
        request.settimeout(IDLE_TIMEOUT)
        try:
            tls = self.context.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError) as exc:
            log.debug("TLS handshake with %s failed: %s", client_address[0], exc)
            try:
                request.close()
            except OSError:
                pass
            return
        try:
            Handler(tls, client_address, self)
        finally:
            try:
                tls.close()
            except OSError:
                pass
            self.api.cp.store.close()          # this worker thread's database connection


class Handler(BaseHTTPRequestHandler):
    server_version = "boswas-control-plane"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):          # access lines are written by _serve
        pass

    def _serve(self):
        started = time.monotonic()
        path, query = parse_target(self.path)
        length = self.headers.get("Content-Length")
        try:
            length_value = int(length) if length is not None else None
            if length_value is not None and length_value < 0:
                raise ValueError
        except ValueError:
            self._send(error(400, "INVALID_LENGTH", "invalid Content-Length"))
            return
        certificate = None
        try:
            certificate = self.connection.getpeercert(binary_form=True)
        except (AttributeError, ValueError, ssl.SSLError):
            certificate = None
        consumed = [0]

        def read(size: int) -> bytes:
            size = min(size, (length_value or 0) - consumed[0])
            data = self.rfile.read(size) if size > 0 else b""
            consumed[0] += len(data)
            return data
        req = Request(method=self.command, path=path, query=query,
                      headers={k.lower(): v for k, v in self.headers.items()}, client=self.client_address[0],
                      certificate=certificate or None, read=read, length=length_value,
                      listener=self.server.listener)
        response, actor = self.server.api.dispatch(req)
        unread = (length_value or 0) - consumed[0]
        if unread > 0:
            # A refused request's body: read it away so the client receives the
            # answer (closing with unread data makes TCP reset the connection),
            # or give up on the connection for absurdly large bodies.
            if unread <= MAX_DRAIN:
                try:
                    while read(1024 * 1024):
                        pass
                except OSError:
                    self.close_connection = True
            else:
                self.close_connection = True
        self._send(response)
        log.info(access_line(req, response, actor, started))

    def _send(self, response):
        self.send_response(response.status)
        headers = {**SECURITY_HEADERS, **response.headers}
        if response.content_type and response.status != 204:
            headers.setdefault("Content-Type", response.content_type)
        if response.file is not None:
            size = response.file.stat().st_size
            headers["Content-Length"] = str(size)
        else:
            headers["Content-Length"] = str(len(response.body))
        headers.setdefault("Cache-Control", "no-store")
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command == "HEAD":
            return
        if response.file is not None:
            with open(response.file, "rb") as fh:
                shutil.copyfileobj(fh, self.wfile, 1024 * 1024)
        elif response.body:
            self.wfile.write(response.body)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _serve


def serve(api: Api, context: ssl.SSLContext, host: str, port: int, listener: str = DEVICE) -> TLSServer:
    """Create a running server (in a background thread); call shutdown() to stop it."""
    server = TLSServer((host, port), api, context, listener)
    thread = threading.Thread(target=server.serve_forever, name=f"boswas-cp-{listener}", daemon=True)
    thread.start()
    return server
