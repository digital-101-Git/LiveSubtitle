"""Authenticate and bound HTTP bodies before FastAPI reads/parses JSON."""
from __future__ import annotations

import hmac

from starlette.datastructures import Headers
from starlette.responses import JSONResponse


class HTTPGuard:
    def __init__(self, app, token: str, allowed_origin, max_bytes: int = 65536):
        self.app = app
        self.expected = ("Bearer " + token).encode("utf-8")
        self.allowed_origin = allowed_origin
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or (scope["path"] == "/health" and scope["method"] == "GET"):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)

        async def reject(code, message):
            response = JSONResponse({"detail": message}, status_code=code,
                                    headers={"Connection": "close"})
            await response(scope, receive, send)

        if not self.allowed_origin(headers.get("origin")):
            await reject(403, "허용되지 않은 브라우저 출처입니다.")
            return
        if not hmac.compare_digest(headers.get("authorization", "").encode("utf-8"), self.expected):
            await reject(401, "엔진 인증이 필요합니다.")
            return
        length = headers.get("content-length")
        if length and (not length.isdigit() or int(length) > self.max_bytes):
            await reject(413, "요청이 너무 큽니다.")
            return
        body = bytearray()
        while True:
            incoming = await receive()
            if incoming["type"] == "http.disconnect":
                return
            chunk = incoming.get("body", b"")
            if len(body) + len(chunk) > self.max_bytes:
                await reject(413, "요청이 너무 큽니다.")
                return
            body.extend(chunk)
            if not incoming.get("more_body", False):
                break
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, bounded_receive, send)
