"""Bounded, read-only HTTP transport that never follows redirects."""

import asyncio
from dataclasses import dataclass
from http.client import HTTPException
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

BODY_LIMIT = 1024 * 1024


@dataclass
class HttpResponse:
    status: int
    body: bytes


class HttpGetError(Exception):
    """A transport failure with a generic, safe message."""


class HttpGet(Protocol):
    async def __call__(self, url: str, headers: Mapping[str, str], timeout_s: float) -> HttpResponse: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str,
    ) -> Request | None:
        return None


def _get(url: str, headers: Mapping[str, str], timeout_s: float) -> HttpResponse:
    try:
        opener = build_opener(_NoRedirect())
        try:
            response = opener.open(Request(url, headers=dict(headers), method="GET"), timeout=timeout_s)
        except HTTPError as error:
            response = error
        with response:
            body = response.read(BODY_LIMIT + 1) if response.fp is not None else b""
            if len(body) > BODY_LIMIT:
                raise HttpGetError("response too large")
            return HttpResponse(response.status, body)
    except TimeoutError:
        raise HttpGetError("timed out") from None
    except (URLError, OSError, HTTPException):
        raise HttpGetError("network error") from None


async def http_get(url: str, headers: Mapping[str, str], timeout_s: float) -> HttpResponse:
    """Run the blocking stdlib GET outside the event loop."""
    return await asyncio.to_thread(_get, url, headers, timeout_s)
