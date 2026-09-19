"""MediaDock local-security primitives (Stage-006).

Pure, stdlib-only helpers used by the HTTP layer and the logger:

    validate_url()       -> http/https, length, control chars, host required
    check_host_header()  -> only loopback names reach the API
    check_origin()       -> absent Origin passes, otherwise YouTube only
    sanitize_url()       -> masks secrets in query strings / userinfo
    sanitize_path()      -> shortens the user's home directory to `~`
    redact()             -> sanitize_url + sanitize_path + line limit
    body_within_limit()  -> POST body size gate

Nothing here reads configuration or writes an HTTP response; `server.py`
decides the status codes (`403 forbidden_host`, `403 forbidden_origin`,
`413 payload_too_large`, `400 invalid_url`).
"""
from __future__ import annotations

import os
import re
from typing import Optional, Tuple
from urllib.parse import parse_qsl, urlsplit, urlunsplit

MAX_URL_LENGTH = 2048
MAX_PATH_LENGTH = 512
ALLOWED_SCHEMES = ("http", "https")

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

# Origins the userscript can legitimately be injected from.
YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")

# Characters that must never appear in a URL we hand to yt-dlp.
_FORBIDDEN_URL_CHARS = set(" \t\r\n\"'<>`|\\")

# Query keys that are always masked in logs/health output.
SENSITIVE_EXACT = frozenset((
    "token", "key", "sig", "signature", "pwd", "password", "passwd", "secret",
    "auth", "session", "sessionid", "cookie", "hash", "si", "code", "policy",
    "expires", "expire", "credential", "credentials", "bearer",
))
SENSITIVE_TOKENS = (
    "token", "secret", "password", "passwd", "apikey", "signature",
    "session", "cookie", "credential", "auth",
)
URL_RE = re.compile(r"https?://[^\s\"'<>|]+", re.IGNORECASE)


def clip(text: str, max_len: int) -> str:
    """Truncate with an ellipsis; never raises on small limits."""
    value = str(text)
    limit = int(max_len)
    if limit <= 3:
        return value[:max(limit, 0)]
    if len(value) <= limit:
        return value
    return value[:limit - 3] + "..."


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(key or "").lower())
    if normalized in SENSITIVE_EXACT:
        return True
    return any(token in normalized for token in SENSITIVE_TOKENS)


def validate_url(url, max_len: int = MAX_URL_LENGTH
                 ) -> Tuple[bool, str, str]:
    """`(ok, error_code, message)`; `error_code` is always `invalid_url`."""
    if url is None or not isinstance(url, str):
        return False, "invalid_url", "url must be a non-empty string"
    text = url.strip()
    if not text:
        return False, "invalid_url", "url must be a non-empty string"
    if len(text) > max_len:
        return False, "invalid_url", f"url must be at most {max_len} characters"
    for char in text:
        if ord(char) < 0x20 or ord(char) == 0x7F:
            return False, "invalid_url", "url contains control characters"
        if char in _FORBIDDEN_URL_CHARS:
            return False, "invalid_url", f"url contains a forbidden character ({char!r})"
    try:
        parts = urlsplit(text)
    except ValueError as exc:
        return False, "invalid_url", f"url cannot be parsed: {exc}"
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        return False, "invalid_url", "url must use http or https"
    if not parts.netloc or not parts.hostname:
        return False, "invalid_url", "url must include a host"
    if "@" in parts.netloc:
        return False, "invalid_url", "url must not include credentials"
    return True, "", ""


def check_host_header(value, port: Optional[int] = None
                      ) -> Tuple[bool, str]:
    """`Host` must be a loopback name; `(ok, message)`."""
    text = str(value or "").strip()
    if not text:
        return False, "missing Host header"
    if "@" in text or "/" in text or "\\" in text:
        return False, f"malformed Host header ({text})"
    host = ""
    port_text = ""
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            return False, f"malformed Host header ({text})"
        host = text[1:end]
        rest = text[end + 1:]
        if rest:
            if not rest.startswith(":"):
                return False, f"malformed Host header ({text})"
            port_text = rest[1:]
    elif text.count(":") == 0:
        host = text
    elif text.count(":") == 1:
        host, port_text = text.split(":", 1)
    else:
        return False, f"malformed Host header ({text})"
    host = host.strip().lower().rstrip(".")
    if host not in LOOPBACK_HOSTS:
        return False, f"Host {host} is not a loopback address"
    if port_text:
        if not port_text.isdigit():
            return False, f"malformed Host port ({port_text})"
        number = int(port_text, 10)
        if not 1 <= number <= 65535:
            return False, f"Host port out of range ({port_text})"
    return True, ""


def check_origin(origin) -> Tuple[bool, str]:
    """Absent Origin passes; a present Origin must be YouTube."""
    text = str(origin or "").strip()
    if not text:
        return True, "origin absent"
    lowered = text.lower()
    if lowered in ("null", "*"):
        return False, f"Origin {text} is not allowed"
    try:
        parts = urlsplit(lowered)
    except ValueError:
        return False, f"malformed Origin ({text})"
    if parts.scheme not in ALLOWED_SCHEMES or not parts.hostname:
        return False, f"malformed Origin ({text})"
    host = parts.hostname.rstrip(".")
    for allowed in YOUTUBE_HOSTS:
        if host == allowed or host.endswith("." + allowed):
            return True, ""
    return False, f"Origin host {host} is not a YouTube domain"


def sanitize_url(url, max_len: int = MAX_URL_LENGTH) -> str:
    """Mask sensitive query values and userinfo passwords; always a string."""
    text = str(url or "")
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return clip(text, max_len)
    if not parts.scheme or not parts.netloc:
        return clip(text, max_len)

    netloc = parts.netloc
    if "@" in netloc:
        creds, host_part = netloc.rsplit("@", 1)
        user = creds.split(":", 1)[0]
        netloc = f"{user}:***@{host_part}"

    query = parts.query
    if query:
        pairs = parse_qsl(query, keep_blank_values=True)
        if pairs:
            query = "&".join(
                f"{key}=***" if _is_sensitive_key(key) else f"{key}={value}"
                for key, value in pairs)

    clean = urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))
    return clip(clean, max_len)


def sanitize_path(path, home: Optional[str] = None,
                  max_len: int = MAX_PATH_LENGTH) -> str:
    """Replace the user's home directory with `~`, then limit the length."""
    text = str(path or "")
    if not text:
        return ""
    home_dir = home if home is not None else os.path.expanduser("~")
    if home_dir and len(home_dir) > 3:
        pattern = re.compile(re.escape(home_dir), re.IGNORECASE)
        text = pattern.sub("~", text, count=1)
    return clip(text, max_len)


def redact(text, max_len: int = 4000, home: Optional[str] = None) -> str:
    """Make one log line safe: mask URL secrets, shorten paths, clip length."""
    value = str(text)
    if "http://" in value or "https://" in value:
        value = URL_RE.sub(lambda match: sanitize_url(match.group(0)), value)
    value = sanitize_path(value, home=home, max_len=max_len)
    return clip(value, max_len)


def body_within_limit(length, limit) -> bool:
    """`0 <= length <= limit`; a non-positive `limit` disables the gate."""
    try:
        size = int(length)
    except (TypeError, ValueError):
        size = 0
    try:
        maximum = int(limit)
    except (TypeError, ValueError):
        return True
    if maximum <= 0:
        return True
    return 0 <= size <= maximum

