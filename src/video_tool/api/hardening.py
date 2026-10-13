"""Checks that apply to every request, before any route runs.

- A request that says its body is over ``MAX_BODY`` bytes is refused.
  The reverse proxy must also cap bodies, because a sender can leave out
  the size.
- Every reply gets headers that stop caching and content guessing, and
  keep download links out of the Referer header.

This is a plain ASGI layer, not Starlette's BaseHTTPMiddleware, so large
file downloads stream straight through.
"""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse

MAX_BODY = 4096

HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


class Hardening:
    def __init__(self, app, max_body: int = MAX_BODY) -> None:
        self.app = app
        self.max_body = max_body

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)

            return

        for name, value in scope["headers"]:
            if (
                name == b"content-length"
                and value.isdigit()
                and int(value) > self.max_body
            ):
                reply = JSONResponse(
                    {
                        "detail": {
                            "title": "Request too large",
                            "message": "The request body is too big.",
                        }
                    },
                    status_code=413,
                )
                await reply(scope, receive, send)

                return

        async def send_safe(message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)

                for key, value in HEADERS.items():
                    headers[key] = value

            await send(message)

        await self.app(scope, receive, send_safe)
