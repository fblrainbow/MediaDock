"""MediaDock format model and formats probe (Stage-008).

`/formats` asks yt-dlp for the metadata of one URL and reduces it to a small,
stable model the UI can render. `/download` accepts a **preset name** or a
`format_id` that came from that model:

    preset      -> one of PRESETS (fixed allow-list, never user text)
    format_id   -> must match _FORMAT_ID_RE and be present in the probed list

Neither value is ever concatenated from raw HTTP input into argv; the resolved
yt-dlp selector is built by `selector_for()` from constants only.

The default behaviour is frozen: an empty preset resolves to the Stage-001
`core_engine.FORMAT_EXPR` policy, so existing downloads are unchanged.

Nothing here writes Task fields or parses HTTP. stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from core_engine import FORMAT_EXPR

# Preset names are the only format input the browser may send.
DEFAULT_PRESET = "best"

# yt-dlp format ids: real ids are short tokens like `137`, `251-1`, `sb0`.
_FORMAT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

# How many formats we hand to the UI (keeps the payload small and stable).
MAX_FORMATS = 40
PROBE_TIMEOUT = 60.0
MAX_PROBE_OUTPUT = 8 * 1024 * 1024

ERROR_INVALID_FORMAT = "invalid_format"
ERROR_FORMAT_NOT_AVAILABLE = "format_not_available"
ERROR_FORMATS_UNAVAILABLE = "formats_unavailable"

# Stage-012: container the `audio` preset is extracted into by FFmpeg.
# A constant, never user text (same rule as the `-f` selectors).
AUDIO_PRESET_FORMAT = "mp3"


@dataclass(frozen=True)
class Preset:
    name: str
    label: str
    kind: str            # "video" | "audio"
    selector: str        # yt-dlp -f expression (constant, never user text)
    max_height: int = 0  # 0 = no height ceiling
    audio_format: str = ""  # non-empty = extract audio into this container

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "label": self.label, "kind": self.kind,
                "selector": self.selector, "max_height": self.max_height,
                "audio_format": self.audio_format}


# Order = UI order. `best` keeps the frozen Stage-001 expression.
PRESETS: Tuple[Preset, ...] = (
    Preset("best", "Best (1080p MP4)", "video", FORMAT_EXPR, 1080),
    Preset("1080p", "1080p", "video",
           "bv*[height<=1080]+ba/b[height<=1080]", 1080),
    Preset("720p", "720p", "video", "bv*[height<=720]+ba/b[height<=720]", 720),
    Preset("480p", "480p", "video", "bv*[height<=480]+ba/b[height<=480]", 480),
    # Stage-008 selected the best audio-only stream; Stage-012 additionally
    # extracts MP3 with FFmpeg (`-x`), so the file is a real .mp3 instead of the
    # platform's native audio container (.webm/opus on YouTube).
    # Stage-009's `/audio` endpoint remains the way to convert an already
    # completed video into mp3/m4a/wav after the fact.
    Preset("audio", "Audio only (MP3)", "audio", "bestaudio/best", 0,
           AUDIO_PRESET_FORMAT),
)

PRESET_BY_NAME: Dict[str, Preset] = {p.name: p for p in PRESETS}


def preset_names() -> List[str]:
    return [p.name for p in PRESETS]


def _approx_bytes(entry: Dict[str, Any], duration: float = 0.0) -> int:
    """Best available size for one format: `filesize` else bitrate x duration."""
    size = _to_int(entry.get("filesize"))
    if size:
        return size
    tbr = _to_float(entry.get("tbr")) or _to_float(entry.get("abr"))
    if tbr and duration > 0:
        return int(tbr * 1000 / 8 * duration)
    return 0


def _best_video(formats: Sequence[Dict[str, Any]], max_height: int,
                duration: float = 0.0) -> Optional[Dict[str, Any]]:
    best: Optional[tuple] = None
    for entry in formats:
        if str(entry.get("vcodec") or "none").lower() == "none":
            continue
        height = _to_int(entry.get("height")) or 0
        if max_height and (not height or height > max_height):
            continue
        size = _approx_bytes(entry, duration)
        if not size:
            continue
        rank = (height, _to_float(entry.get("fps")) or 0.0)
        if best is None or rank > best[0]:
            best = (rank, entry, size)
    return None if best is None else {"entry": best[1], "size": best[2]}


def _best_audio(formats: Sequence[Dict[str, Any]],
                duration: float = 0.0) -> Optional[Dict[str, Any]]:
    best: Optional[tuple] = None
    for entry in formats:
        if str(entry.get("acodec") or "none").lower() == "none":
            continue
        if str(entry.get("vcodec") or "none").lower() != "none":
            continue
        size = _approx_bytes(entry, duration)
        if not size:
            continue
        rank = _to_float(entry.get("abr")) or 0.0
        if best is None or rank > best[0]:
            best = (rank, entry, size)
    return None if best is None else {"entry": best[1], "size": best[2]}


def preset_sizes(formats: Sequence[Dict[str, Any]],
                 duration: float = 0.0) -> Dict[str, int]:
    """Estimated bytes per preset (`0` = unknown, so the UI shows nothing).

    Stage-013: video presets = best video within the height ceiling plus the
    best audio-only format (`bv*+ba`, which is what the selectors merge); the
    `audio` preset = best audio-only format. A missing component means the
    estimate is unknown rather than understated.
    """
    audio = _best_audio(formats, duration)
    audio_size = audio["size"] if audio else 0
    sizes: Dict[str, int] = {"audio": audio_size}
    for preset in PRESETS:
        if preset.kind != "video":
            continue
        video = _best_video(formats, preset.max_height, duration)
        if video is None or not audio_size:
            sizes[preset.name] = 0
            continue
        sizes[preset.name] = int(video["size"]) + int(audio_size)
    return sizes


def preset_choices(formats: Sequence[Dict[str, Any]] = (),
                   duration: float = 0.0) -> List[Dict[str, Any]]:
    """Public preset table; `size_bytes` is 0 when it cannot be estimated."""
    entries = list(formats or ())
    sizes = preset_sizes(entries, duration) if entries else {}
    items = []
    for preset in PRESETS:
        item = preset.to_dict()
        item["size_bytes"] = int(sizes.get(preset.name, 0))
        items.append(item)
    return items


def presets_public(formats: Sequence[Dict[str, Any]] = (),
                   duration: float = 0.0) -> List[Dict[str, Any]]:
    """Backwards-compatible alias kept for existing callers/tests."""
    return preset_choices(formats, duration)


def resolve_preset(value: Any) -> Tuple[Optional[Preset], str, str]:
    """`(preset, error_code, message)`; absent/empty value -> default preset."""
    text = str(value or "").strip().lower()
    if not text:
        return PRESET_BY_NAME[DEFAULT_PRESET], "", ""
    preset = PRESET_BY_NAME.get(text)
    if preset is None:
        return None, ERROR_INVALID_FORMAT, (
            "preset must be one of " + ", ".join(preset_names()))
    return preset, "", ""


def validate_format_id(value: Any) -> Tuple[bool, str]:
    text = str(value or "").strip()
    if not text or not _FORMAT_ID_RE.match(text):
        return False, ("format_id must be 1-64 chars of "
                       "[A-Za-z0-9_.-] starting with a letter or digit")
    return True, ""


def audio_format_for(preset: Optional[Preset] = None,
                     format_id: str = "") -> str:
    """Container to extract into, or `""` for a plain download (Stage-012).

    Only the fixed `audio` preset requests extraction. An explicit
    `format_id` keeps whatever the caller picked: no implicit transcoding.
    """
    if format_id or preset is None:
        return ""
    return str(preset.audio_format or "")


def selector_for(preset: Optional[Preset] = None,
                 format_id: str = "") -> str:
    """Return the yt-dlp `-f` expression for a validated choice.

    `format_id` wins over `preset`; both are validated by the caller through
    `resolve_preset()` / `validate_format_id()` and never contain user text
    that could become a flag (a leading `-` is impossible).
    """
    if format_id:
        return f"{format_id}+ba/{format_id}/b"
    if preset is not None:
        return preset.selector
    return PRESET_BY_NAME[DEFAULT_PRESET].selector


def build_probe_command(ytdlp: str, url: str,
                        ffmpeg_path: str = "") -> List[str]:
    """argv for a metadata-only yt-dlp run; no shell, URL is the last arg."""
    command = [ytdlp, "--dump-single-json", "--skip-download", "--no-playlist",
               "--no-warnings"]
    if ffmpeg_path:
        command += ["--ffmpeg-location", str(ffmpeg_path)]
    command.append(url)
    return command


def _subprocess_runner(command: Sequence[str], timeout: float
                       ) -> Tuple[int, str, str]:
    """Default runner: one child process, no shell, bounded output."""
    kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
              "text": True, "encoding": "utf-8", "errors": "replace"}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(list(command), timeout=float(timeout),
                                   **kwargs)
    except FileNotFoundError:
        return 127, "", f"yt-dlp not found at {command[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"yt-dlp timed out after {timeout:.0f}s"
    except OSError as exc:
        return 126, "", f"cannot run yt-dlp: {exc}"
    return (completed.returncode, (completed.stdout or "")[:MAX_PROBE_OUTPUT],
            (completed.stderr or "")[:2000])


def parse_probe_output(text: str) -> Tuple[Optional[Dict[str, Any]], str, str]:
    """`(info, error_code, message)` from `--dump-single-json` stdout.

    yt-dlp prints one JSON object; some versions add a warning line before it,
    so we take the last line that parses as a JSON object.
    """
    raw = str(text or "").strip()
    if not raw:
        return None, ERROR_FORMATS_UNAVAILABLE, "yt-dlp returned no output"
    try:
        info = json.loads(raw)
    except ValueError:
        info = None
        for candidate in reversed(raw.splitlines()):
            line = candidate.strip()
            if not line.startswith("{"):
                continue
            try:
                info = json.loads(line)
            except ValueError:
                continue
            break
    if not isinstance(info, dict):
        return None, ERROR_FORMATS_UNAVAILABLE, "yt-dlp output is not JSON"
    return info, "", ""


def _to_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result:  # NaN
        return None
    return result


def _presets_for(entry: Dict[str, Any]) -> List[str]:
    """Presets this concrete format satisfies (used by the UI + validation)."""
    vcodec = str(entry.get("vcodec") or "none").lower()
    acodec = str(entry.get("acodec") or "none").lower()
    height = _to_int(entry.get("height")) or 0
    found: List[str] = []
    if vcodec == "none" and acodec != "none":
        found.append("audio")
    for preset in PRESETS:
        if preset.kind != "video" or vcodec == "none":
            continue
        if preset.max_height and height and height > preset.max_height:
            continue
        found.append(preset.name)
    return found


def format_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Stable per-format model; only these keys are exposed."""
    filesize = _to_int(entry.get("filesize")) or _to_int(
        entry.get("filesize_approx"))
    return {
        "format_id": str(entry.get("format_id") or ""),
        "ext": str(entry.get("ext") or ""),
        "height": _to_int(entry.get("height")),
        "width": _to_int(entry.get("width")),
        "fps": _to_float(entry.get("fps")),
        "vcodec": str(entry.get("vcodec") or "none"),
        "acodec": str(entry.get("acodec") or "none"),
        "abr": _to_float(entry.get("abr")),
        "tbr": _to_float(entry.get("tbr")),
        "filesize": filesize,
        "note": str(entry.get("format_note") or entry.get("format") or "")[:80],
        "presets": _presets_for(entry),
    }


def preset_satisfied(preset: Preset, formats: Sequence[Dict[str, Any]]) -> bool:
    """True when at least one probed format can serve the preset.

    The default preset is always allowed: its selector is the frozen Stage-001
    MVP policy with its own fallback chain, and Stage-008 must never reject a
    download that the pre-Stage-008 code accepted.
    """
    if preset.name == DEFAULT_PRESET:
        return True
    return any(preset.name in entry.get("presets", ()) for entry in formats)


def format_id_present(format_id: str,
                      formats: Sequence[Dict[str, Any]]) -> bool:
    return any(entry.get("format_id") == format_id for entry in formats)


def build_formats_payload(info: Dict[str, Any], url: str, platform: str,
                          video_id: str = "",
                          limit: int = MAX_FORMATS) -> Dict[str, Any]:
    """Reduce yt-dlp metadata to the `/formats` response contract."""
    entries: List[Dict[str, Any]] = []
    for entry in info.get("formats") or []:
        if not isinstance(entry, dict):
            continue
        item = format_entry(entry)
        if item["format_id"]:
            entries.append(item)
    total = len(entries)
    if limit and limit > 0:
        entries = entries[:int(limit)]
    return {
        "url": url,
        "platform": platform,
        "video_id": video_id or str(info.get("id") or ""),
        "title": str(info.get("title") or "")[:200],
        "uploader": str(info.get("uploader") or info.get("channel") or "")[:120],
        "duration": _to_float(info.get("duration")),
        "extractor": str(info.get("extractor_key") or info.get("extractor")
                         or "")[:80],
        "default_preset": DEFAULT_PRESET,
        "presets": preset_choices(entries, _to_float(info.get("duration")) or 0.0),
        "formats": entries,
        "count": len(entries),
        "total": total,
    }


class FormatsProbe:
    """Runs one metadata-only yt-dlp call and returns the parsed payload.

    The subprocess boundary is injected (`runner`) so unit tests and probes
    never need the network; `fetch()` never raises.
    """

    def __init__(self, ytdlp: str = "", ffmpeg: str = "",
                 runner: Optional[Callable[..., Tuple[int, str, str]]] = None,
                 timeout: float = PROBE_TIMEOUT,
                 logger: Optional[Callable[..., None]] = None):
        self.ytdlp = ytdlp
        self.ffmpeg = ffmpeg
        self._runner = runner or _subprocess_runner
        self._timeout = float(timeout)
        self._log = logger or (lambda *a: None)

    def fetch(self, url: str, platform: str = "youtube", video_id: str = ""
              ) -> Tuple[Optional[Dict[str, Any]], str, str]:
        if not self.ytdlp:
            return (None, ERROR_FORMATS_UNAVAILABLE,
                    "yt-dlp path is not configured")
        command = build_probe_command(self.ytdlp, url, self.ffmpeg)
        code, out, err = self._runner(command, self._timeout)
        if code != 0:
            lines = (err or out or "").strip().splitlines()
            message = lines[-1] if lines else f"yt-dlp exit code {code}"
            self._log(f"formats probe failed rc={code}: {message}")
            return None, ERROR_FORMATS_UNAVAILABLE, message[:300]
        info, error, message = parse_probe_output(out)
        if info is None:
            self._log(f"formats probe unparsable: {message}")
            return None, error, message
        payload = build_formats_payload(info, url, platform, video_id)
        if payload["count"] == 0:
            return None, ERROR_FORMATS_UNAVAILABLE, "yt-dlp reported no formats"
        return payload, "", ""
