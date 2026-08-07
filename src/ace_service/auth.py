"""Authentication, CSRF, and browser security helpers for the private UI."""

from __future__ import annotations

import base64
import binascii
import secrets
from collections.abc import Mapping
from hmac import compare_digest
from typing import Any
from urllib.parse import parse_qs

from fastapi import HTTPException, Request, status

CSRF_COOKIE_NAME = "ace_csrf"
CSRF_FIELD_NAME = "csrf_token"
_MAX_FORM_BYTES = 128 * 1024


def require_basic_auth(request: Request, username: str, password: str) -> None:
    """Require valid Basic credentials using constant-time comparisons."""

    header = request.headers.get("authorization", "")
    supplied_username = ""
    supplied_password = ""
    scheme, separator, encoded = header.partition(" ")
    if separator and scheme.lower() == "basic":
        try:
            decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
            supplied_username, separator, supplied_password = decoded.partition(":")
            if not separator:
                supplied_username = ""
                supplied_password = ""
        except (binascii.Error, UnicodeDecodeError):
            pass

    username_matches = compare_digest(supplied_username, username)
    password_matches = compare_digest(supplied_password, password)
    if not (username_matches and password_matches):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
            headers={"WWW-Authenticate": 'Basic realm="ACE Service"'},
        )


def csrf_token(request: Request) -> str:
    """Return the browser's CSRF token, creating one for a new browser."""

    return request.cookies.get(CSRF_COOKIE_NAME) or secrets.token_urlsafe(32)


def attach_csrf_cookie(request: Request, response: Any, token: str) -> None:
    """Set the CSRF cookie when a page response needs to bootstrap a browser."""

    if request.cookies.get(CSRF_COOKIE_NAME) == token:
        return
    response.set_cookie(
        CSRF_COOKIE_NAME,
        token,
        httponly=True,
        max_age=86_400,
        path="/",
        samesite="lax",
        secure=request.url.scheme == "https",
    )


async def parse_form(request: Request) -> dict[str, str]:
    """Parse a bounded URL-encoded form without accepting arbitrary JSON input."""

    body = await request.body()
    if len(body) > _MAX_FORM_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="form is too large",
        )
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("application/x-www-form-urlencoded"):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="form must use URL encoding",
        )
    try:
        fields: Mapping[str, list[str]] = parse_qs(
            body.decode("utf-8"), keep_blank_values=True, strict_parsing=False
        )
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="form is not valid UTF-8"
        ) from exc
    return {key: values[-1] for key, values in fields.items() if values}


def require_csrf(request: Request, fields: Mapping[str, str]) -> None:
    """Require a same-site form token matching the HttpOnly cookie."""

    cookie_token = request.cookies.get(CSRF_COOKIE_NAME, "")
    form_token = fields.get(CSRF_FIELD_NAME, "")
    if not cookie_token or not form_token or not compare_digest(cookie_token, form_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF validation failed",
        )
