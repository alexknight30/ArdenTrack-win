"""
Localhost HTTP server to receive OAuth tokens from Electron.

Stays alive for the lifetime of the process so re-authentication can
deliver fresh tokens at any time.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from ardentrack import auth

logger = logging.getLogger(__name__)


def start_auth_listener(
    token_received_event: threading.Event,
    *,
    open_browser: bool = True,
) -> None:
    """
    Bind POST /auth/tokens on 127.0.0.1:ARDEN_AUTH_CALLBACK_PORT (default 17951).

    The server stays running for the lifetime of the process so that
    re-authentication tokens can be received at any time.  When
    *open_browser* is True, ``AUTH_LISTENING=<port>`` is printed to stdout
    so Electron opens the login page in the default browser.
    """
    port = int((os.environ.get("ARDEN_AUTH_CALLBACK_PORT") or "17951").strip())
    expected_secret = (os.environ.get("ARDEN_AUTH_CALLBACK_TOKEN") or "").strip()

    def make_handler():
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                if urlparse(self.path).path != "/auth/tokens":
                    self.send_response(404)
                    self.end_headers()
                    return

                got_secret = self.headers.get("X-Arden-Auth-Token", "").strip()
                if not expected_secret or got_secret != expected_secret:
                    self.send_response(401)
                    self.end_headers()
                    return

                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b"{}"
                try:
                    data = json.loads(body.decode("utf-8"))
                except json.JSONDecodeError:
                    self.send_response(400)
                    self.end_headers()
                    return

                access = data.get("access_token")
                refresh = data.get("refresh_token")
                expires_in = data.get("expires_in")
                if not access or not refresh or expires_in is None:
                    self.send_response(400)
                    self.end_headers()
                    return

                try:
                    expires_in = int(expires_in)
                except (TypeError, ValueError):
                    self.send_response(400)
                    self.end_headers()
                    return

                try:
                    auth.store_tokens(access, refresh, expires_in)
                except Exception:
                    logger.exception("store_tokens from auth callback")
                    self.send_response(500)
                    self.end_headers()
                    return

                token_received_event.set()
                logger.info("Auth tokens received and stored successfully")
                self.send_response(200)
                self.end_headers()

            def log_message(self, fmt: str, *args) -> None:
                if "token" in fmt.lower():
                    return
                logger.debug("%s - %s", self.address_string(), fmt % args)

        return Handler

    def serve() -> None:
        Handler = make_handler()
        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        if open_browser:
            print(f"AUTH_LISTENING={port}\n", flush=True)
        logger.info("Auth listener running on 127.0.0.1:%d (open_browser=%s)", port, open_browser)
        try:
            httpd.serve_forever()
        except Exception:
            logger.exception("auth server serve_forever")

    threading.Thread(target=serve, daemon=True).start()
