"""MediaDock platform Adapter boundary (Stage-007).

One place decides which platform a URL belongs to:

    detect_platform(url) -> (adapter, error_code, message)

    adapter is None -> error_code is one of
        invalid_url          (Stage-006 URL validation failed)
        unsupported_platform (valid http(s) but no registered Adapter)
        platform_not_ready   (Adapter known but `ready is False`)
    adapter is not None -> (adapter, "", "")

Detection is a pure string operation: no network access, no Task creation, no
state change. `server.py` only maps the returned error code to a status code.

Responsibility boundary (Stage-007.md 5.4):

    core_platform -> recognition, normalization, video id, extension point
    server.Handler -> call the detector and render the error code
    core_scheduler -> create the Task from the `platform` name
    core_files     -> keep the YouTube video-id file boundary

The extension point is `PlatformRegistry.register()`: adding a platform means
adding an Adapter, never editing `server.py` or the state machine. stdlib only.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit

from core_files import video_id_from_url
from core_security import validate_url

# Error codes owned by this module (Stage-007.md 5.3).
ERROR_INVALID_URL = "invalid_url"
ERROR_UNSUPPORTED_PLATFORM = "unsupported_platform"
ERROR_PLATFORM_NOT_READY = "platform_not_ready"

# The single source of truth for the platforms the browser entry may target.
YOUTUBE_HOSTS: Tuple[str, ...] = ("youtube.com", "youtu.be",
                                  "youtube-nocookie.com")


def host_matches(host: str, hosts: Sequence[str]) -> bool:
    """True when `host` is one of `hosts` or a subdomain of it."""
    text = str(host or "").strip().lower().rstrip(".")
    if not text:
        return False
    for candidate in hosts or ():
        name = str(candidate or "").strip().lower().rstrip(".")
        if not name:
            continue
        if text == name or text.endswith("." + name):
            return True
    return False


def url_host(url: Any) -> str:
    """Hostname of a URL, lowercased; empty string when unparsable."""
    try:
        parts = urlsplit(str(url or "").strip())
    except ValueError:
        return ""
    return (parts.hostname or "").lower().rstrip(".")


def strip_fragment(url: Any) -> str:
    """Drop the `#fragment` part; everything else is left untouched.

    Stage-007 deliberately does not rewrite the query string: yt-dlp must see
    the same URL it saw before the Adapter layer existed.
    """
    text = str(url or "").strip()
    if "#" not in text:
        return text
    try:
        parts = urlsplit(text)
    except ValueError:
        return text
    if not parts.scheme or not parts.netloc:
        return text
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


@dataclass(frozen=True)
class PlatformInfo:
    """What an Adapter knows about one URL before any download starts."""

    name: str
    url: str
    video_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "url": self.url, "video_id": self.video_id}


class PlatformAdapter:
    """Base Adapter: recognition + normalization + best-effort metadata.

    Subclasses only override data (`name`, `ready`, `hosts`) and pure helpers.
    An Adapter must never touch the Task state machine, HTTP or the filesystem.
    """

    name: str = ""
    ready: bool = True
    hosts: Tuple[str, ...] = ()

    def matches(self, url: Any) -> bool:
        """Pure hostname judgement; never raises, never accesses the network."""
        return host_matches(url_host(url), self.hosts)

    def normalize(self, url: Any) -> str:
        """Canonical URL handed to the download engine."""
        return strip_fragment(url)

    def video_id(self, url: Any) -> str:
        """Best-effort platform video id; empty string when unknown."""
        return ""

    def info(self, url: Any) -> PlatformInfo:
        """Assemble the immutable descriptor for a URL this Adapter matched."""
        normalized = self.normalize(url)
        return PlatformInfo(name=self.name, url=normalized,
                            video_id=self.video_id(normalized))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} name={self.name!r} ready={self.ready}>"


class YouTubeAdapter(PlatformAdapter):
    """The only platform wired into the MVP download flow (plan-whole D-005)."""

    name = "youtube"
    hosts = YOUTUBE_HOSTS

    def video_id(self, url: Any) -> str:
        # Reuse the Stage-004 file-boundary parser so there is exactly one
        # YouTube id semantics in the codebase (Stage-007.md 13.3).
        return video_id_from_url(str(url or ""))


class PlatformRegistry:
    """Ordered Adapter collection; the first matching Adapter wins."""

    def __init__(self, adapters: Sequence[PlatformAdapter] = ()):
        self._adapters: List[PlatformAdapter] = []
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: PlatformAdapter) -> PlatformAdapter:
        """Add an Adapter at the end of the match order (extension point)."""
        if adapter is None:
            raise ValueError("adapter must not be None")
        name = str(getattr(adapter, "name", "") or "").strip()
        if not name:
            raise ValueError("adapter.name must be a non-empty string")
        self._adapters.append(adapter)
        return adapter

    def adapters(self) -> Tuple[PlatformAdapter, ...]:
        return tuple(self._adapters)

    def names(self) -> List[str]:
        return [adapter.name for adapter in self._adapters]

    def get(self, name: str) -> Optional[PlatformAdapter]:
        target = str(name or "").strip().lower()
        for adapter in self._adapters:
            if adapter.name.lower() == target:
                return adapter
        return None

    def match(self, url: Any) -> Optional[PlatformAdapter]:
        """First registered Adapter whose `hosts` cover the URL host."""
        for adapter in self._adapters:
            try:
                if adapter.matches(url):
                    return adapter
            except Exception:  # noqa: BLE001 - an Adapter must not break the gate
                continue
        return None

    def detect(self, url: Any) -> Tuple[Optional[PlatformAdapter], str, str]:
        """`(adapter, error_code, message)`; no side effects (Stage-007.md 5.2)."""
        ok, code, message = validate_url(url)
        if not ok:
            return None, code or ERROR_INVALID_URL, message
        text = str(url).strip()
        adapter = self.match(text)
        if adapter is None:
            host = url_host(text) or "(unknown host)"
            return None, ERROR_UNSUPPORTED_PLATFORM, (
                f"platform not supported for host {host}")
        if not getattr(adapter, "ready", True):
            return None, ERROR_PLATFORM_NOT_READY, (
                f"platform {adapter.name} is not available yet")
        return adapter, "", ""


DEFAULT_REGISTRY = PlatformRegistry((YouTubeAdapter(),))


def detect_platform(url: Any, registry: Optional[PlatformRegistry] = None
                    ) -> Tuple[Optional[PlatformAdapter], str, str]:
    """`(adapter, error_code, message)` using `DEFAULT_REGISTRY` by default."""
    active = registry if registry is not None else DEFAULT_REGISTRY
    return active.detect(url)


def platform_names(registry: Optional[PlatformRegistry] = None) -> List[str]:
    """Registered platform names in match order (diagnostics / Stage-008)."""
    active = registry if registry is not None else DEFAULT_REGISTRY
    return active.names()
