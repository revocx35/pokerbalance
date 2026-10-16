"""FastAPI app: security middleware, static PWA shell and the JSON API."""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import threading
import time

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import api, auth, config, db

log = logging.getLogger("pokerbalance")
STATIC = os.path.join(os.path.dirname(__file__), "static")
MAX_BODY = 64 * 1024

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; manifest-src 'self'; worker-src 'self'; font-src 'self'; object-src 'none'; "
       "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
SECURITY_HEADERS = [
    (b"content-security-policy", CSP.encode()),
    (b"strict-transport-security", b"max-age=63072000; includeSubDomains"),
    (b"x-frame-options", b"DENY"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=(), usb=()"),
]


def _trusted(a: str) -> bool:
    try:
        ip = ipaddress.ip_address(a)
    except ValueError:
        return False
    return any(ip in n for n in config.TRUSTED_PROXIES)


def proxied_plain_http(scope) -> bool:
    """True if a trusted reverse proxy says the browser used plain http:// (proxy without "Force SSL")."""
    peer = (scope.get("client") or ("0.0.0.0", 0))[0]
    if not config.TRUSTED_PROXIES or not _trusted(peer):
        return False
    for k, v in scope.get("headers", []):
        if k == b"x-forwarded-proto":
            return v.decode("latin-1").split(",")[0].strip().lower() == "http"
    return False


def client_ip(scope) -> str:
    """The peer address, or the right-most X-Forwarded-For entry not added by a trusted proxy."""
    peer = (scope.get("client") or ("0.0.0.0", 0))[0]
    trusted = _trusted

    if not config.TRUSTED_PROXIES or not trusted(peer):
        return peer
    xff = ""
    for k, v in scope.get("headers", []):
        if k == b"x-forwarded-for":
            xff += ("," if xff else "") + v.decode("latin-1")
    hops = [h.strip() for h in xff.split(",") if h.strip()]
    for h in reversed(hops):
        if not trusted(h):
            try:
                return str(ipaddress.ip_address(h))
            except ValueError:
                return peer
    return hops[0] if hops else peer


class Guard:
    """Pure ASGI middleware: security headers, body-size cap, per-IP request rate limit,
    and a same-origin check on every state-changing API request."""

    RATE, WINDOW = 600, 60

    def __init__(self, app):
        self.app = app
        self.hits: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    def _rate_ok(self, ip: str) -> bool:
        now = time.monotonic()
        with self.lock:
            if len(self.hits) > 20000:
                self.hits = {k: v for k, v in self.hits.items() if v and v[-1] > now - self.WINDOW}
            h = [t for t in self.hits.get(ip, []) if t > now - self.WINDOW]
            h.append(now)
            self.hits[ip] = h
            return len(h) <= self.RATE

    async def _reject(self, send, status: int, msg: str):
        body = ('{"detail":"%s"}' % msg).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"cache-control", b"no-store"),
                                (b"content-length", str(len(body)).encode())] + SECURITY_HEADERS})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        ip = client_ip(scope)
        scope.setdefault("state", {})["client_ip"] = ip
        path = scope["path"]
        method = scope["method"]
        if proxied_plain_http(scope):
            # Browsers drop the Secure session cookie on http:// pages, so sign-in would silently fail.
            host = headers.get("host", "")
            if method not in ("GET", "HEAD") or not re.fullmatch(r"[A-Za-z0-9.\-]+(:\d+)?|\[[0-9A-Fa-f:.]+\](:\d+)?", host):
                return await self._reject(send, 403, "Use HTTPS.")
            qs = scope.get("query_string", b"").decode("latin-1")
            loc = f"https://{host.split(':')[0] if not host.startswith('[') else host.rsplit(':', 1)[0]}{path}" + (f"?{qs}" if qs else "")
            await send({"type": "http.response.start", "status": 308,
                        "headers": [(b"location", loc.encode("latin-1")), (b"content-length", b"0"),
                                    (b"cache-control", b"no-store")] + SECURITY_HEADERS})
            await send({"type": "http.response.body", "body": b""})
            return
        if path.startswith("/api/"):
            if not self._rate_ok(ip):
                return await self._reject(send, 429, "Too many requests, slow down.")
            if method not in ("GET", "HEAD", "OPTIONS"):
                ct = headers.get("content-type", "").split(";")[0].strip().lower()
                if ct != "application/json":
                    return await self._reject(send, 415, "Expected JSON.")
                site = headers.get("sec-fetch-site")
                if site and site not in ("same-origin", "none"):
                    return await self._reject(send, 403, "Cross-site request refused.")
                origin = headers.get("origin")
                if origin and re.sub(r"^https?://", "", origin) != headers.get("host", ""):
                    return await self._reject(send, 403, "Cross-site request refused.")
        try:
            length = int(headers.get("content-length", "0"))
        except ValueError:
            return await self._reject(send, 400, "Bad request.")
        if length > MAX_BODY:
            return await self._reject(send, 413, "Request too large.")

        received = 0

        async def limited_receive():
            nonlocal received
            msg = await receive()
            if msg["type"] == "http.request":
                received += len(msg.get("body", b""))
                if received > MAX_BODY:
                    raise ValueError("body too large")
            return msg

        async def send_with_headers(msg):
            if msg["type"] == "http.response.start":
                hs = [h for h in msg.get("headers", []) if h[0].lower() != b"server"]
                hs += SECURITY_HEADERS
                if path.startswith("/api/"):
                    hs = [h for h in hs if h[0].lower() != b"cache-control"] + [(b"cache-control", b"no-store")]
                msg["headers"] = hs
            await send(msg)

        await self.app(scope, limited_receive, send_with_headers)


def create_app() -> FastAPI:
    db.init()
    conn = db.connect()
    try:
        auth.ensure_setup_token(conn)
    finally:
        conn.close()

    app = FastAPI(title="Poker Balance", version=config.__version__, docs_url=None, redoc_url=None,
                  openapi_url=None)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        field = ".".join(str(x) for x in first.get("loc", [])[1:]) or "request"
        return JSONResponse({"detail": f"Invalid {field}: {first.get('msg', 'bad value')}"}, status_code=400)

    app.include_router(api.router, prefix="/api")

    def static_file(name: str, media: str | None = None, cache: str = "no-cache"):
        def handler():
            return FileResponse(os.path.join(STATIC, name), media_type=media, headers={"cache-control": cache})
        return handler

    app.get("/", include_in_schema=False)(static_file("index.html", "text/html"))
    app.get("/sw.js", include_in_schema=False)(static_file("sw.js", "text/javascript"))
    app.get("/manifest.webmanifest", include_in_schema=False)(
        static_file("manifest.webmanifest", "application/manifest+json"))

    allowed = {}
    for root, _, files in os.walk(STATIC):
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), STATIC).replace(os.sep, "/")
            allowed[rel] = os.path.join(root, f)

    @app.get("/static/{name:path}", include_in_schema=False)
    def static(name: str):
        p = allowed.get(name)
        if not p:
            return JSONResponse({"detail": "Not found."}, status_code=404)
        return FileResponse(p, headers={"cache-control": "no-cache"})

    return Guard(app)
