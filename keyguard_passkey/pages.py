"""The approval and enrolment pages, as both the loopback ceremony and the unlock service serve them.

They are read through the package loader, so a zipped copy of this client serves them too.
"""
from __future__ import annotations

import pkgutil
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Optional

HTML: Final = "text/html; charset=utf-8"
JAVASCRIPT: Final = "text/javascript; charset=utf-8"
CSS: Final = "text/css; charset=utf-8"
APPROVE_ROUTE: Final = "/approve"
ENROLL_ROUTE: Final = "/enroll"
ROUTES: Final = {
    APPROVE_ROUTE: ("approve.html", HTML),
    ENROLL_ROUTE: ("enroll.html", HTML),
    "/approve.js": ("approve.js", JAVASCRIPT),
    "/enroll.js": ("enroll.js", JAVASCRIPT),
    "/page.js": ("page.js", JAVASCRIPT),
    "/store-client.js": ("store-client.js", JAVASCRIPT),
    "/transport-local.js": ("transport-local.js", JAVASCRIPT),
    "/transport-service.js": ("transport-service.js", JAVASCRIPT),
    "/unlock-core.js": ("unlock-core.js", JAVASCRIPT),
    "/owner-message.js": ("owner-message.js", JAVASCRIPT),
    "/typage.js": ("typage.js", JAVASCRIPT),
    "/unlock.css": ("unlock.css", CSS),
}
SECURITY_HEADERS: Final = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; connect-src 'self'; style-src 'self'; img-src 'self' data:; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


@dataclass(frozen=True)
class Page:
    body: bytes
    content_type: str


PageSource = Callable[[str], Optional[Page]]


def page(route: str) -> Page | None:
    if route not in ROUTES:
        return None
    name, content_type = ROUTES[route]
    try:
        body = pkgutil.get_data(__package__ or "keyguard_passkey", f"pages/{name}")
    except OSError:
        return None
    return None if body is None else Page(body, content_type)
