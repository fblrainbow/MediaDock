"""MediaDock media-processing boundary (Stage-009).

Turns one already-downloaded video into an audio file with FFmpeg *without*
letting any HTTP input reach the command line:

    target name  -> AudioTarget (fixed allow-list: mp3/m4a/wav)
    source file  -> resolved inside the configured download directory only
    output file  -> same stem + target extension, inside the same directory

Progress, pause, cancel and error reporting reuse the Stage-002..004 Task
model, so a media task looks exactly like a download task to the API and UI.

Nothing here writes Task fields directly (the processor asks the TaskManager),
and nothing here parses HTTP. stdlib + FFmpeg executable only.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from core_control import terminate_tree
from core_files import discover_task_files, is_inside, is_temp_file

DEFAULT_TARGET = "mp3"

# Task type used for media jobs (the `type` field has existed since Stage-002).
AUDIO_TASK_TYPE = "audio"

ERROR_INVALID_TARGET = "invalid_audio_target"
ERROR_SOURCE_NOT_FOUND = "source_not_found"
ERROR_SOURCE_OUTSIDE = "source_outside_download_dir"
ERROR_FFMPEG_MISSING = "ffmpeg_missing"
ERROR_FFMPEG_FAILED = "ffmpeg_failed"
ERROR_OUTPUT_MISSING = "output_missing"
ERROR_INSUFFICIENT_SPACE = "insufficient_space"

# ffmpeg prints the input duration on stderr; we merge it into the progress
# stream so one parser can compute a percentage.
_PROGRESS_PREFIXES = ("out_time_ms=", "out_time_us=", "out_time=", "progress=",
                      "Duration:", "Error", "Invalid", "Output file is empty")


@dataclass(frozen=True)
class AudioTarget:
    name: str
    label: str
    extension: str
    codec_args: Tuple[str, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "label": self.label,
                "extension": self.extension}


TARGETS: Tuple[AudioTarget, ...] = (
    AudioTarget("mp3", "MP3", ".mp3",
                ("-c:a", "libmp3lame", "-q:a", "2")),
    AudioTarget("m4a", "M4A (AAC)", ".m4a",
                ("-c:a", "aac", "-b:a", "192k")),
    AudioTarget("wav", "WAV (PCM)", ".wav",
                ("-c:a", "pcm_s16le")),
)

TARGET_BY_NAME: Dict[str, AudioTarget] = {t.name: t for t in TARGETS}


def target_names() -> List[str]:
    return [t.name for t in TARGETS]


def targets_public() -> List[Dict[str, Any]]:
    return [t.to_dict() for t in TARGETS]


def resolve_target(value: Any) -> Tuple[Optional[AudioTarget], str, str]:
    """`(target, error_code, message)`; absent/empty value -> default target."""
    text = str(value or "").strip().lower()
    if not text:
        return TARGET_BY_NAME[DEFAULT_TARGET], "", ""
    target = TARGET_BY_NAME.get(text)
    if target is None:
        return None, ERROR_INVALID_TARGET, (
            "target must be one of " + ", ".join(target_names()))
    return target, "", ""


def build_ffmpeg_command(ffmpeg: str, source: str, destination: str,
                         target: AudioTarget,
                         with_progress: bool = True) -> List[str]:
    """argv for one conversion; no shell, all paths are single arguments."""
    command = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", str(source),
               "-vn"]
    command += list(target.codec_args)
    if with_progress:
        command += ["-progress", "pipe:1", "-nostats"]
    command.append(str(destination))
    return command


def output_path_for(source_path: str, target: AudioTarget) -> str:
    """`<same stem><target extension>` next to the source file."""
    stem, _ = os.path.splitext(str(source_path))
    return stem + target.extension


def find_source_file(download_dir: str, url: str,
                     explicit: str = "") -> Tuple[str, str, str]:
    """`(path, error_code, message)` for the video to convert.

    An explicit path must live inside `download_dir`; otherwise the newest
    non-temp file for this video id inside `download_dir` is used. Either way
    the result can only be a real file inside the configured directory.
    """
    root = str(download_dir or "")
    if explicit:
        candidate = str(explicit)
        if not is_inside(root, candidate):
            return "", ERROR_SOURCE_OUTSIDE, (
                "source must live inside the download directory")
        if not os.path.isfile(candidate):
            return "", ERROR_SOURCE_NOT_FOUND, "source file does not exist"
        return os.path.realpath(candidate), "", ""
    candidates = [path for path in discover_task_files(root, url, (), 0.0)
                  if not is_temp_file(path)]
    if not candidates:
        return "", ERROR_SOURCE_NOT_FOUND, (
            "no downloaded file found for this task")
    try:
        newest = max(candidates, key=lambda path: os.path.getmtime(path))
    except OSError:
        return "", ERROR_SOURCE_NOT_FOUND, "source file is not readable"
    return newest, "", ""


def parse_duration_seconds(text: str) -> float:
    """`HH:MM:SS.ss` (ffmpeg `Duration:` value) -> seconds; 0.0 when unknown."""
    value = str(text or "").strip()
    if not value:
        return 0.0
    parts = value.split(":")
    if len(parts) != 3:
        return 0.0
    try:
        hours = float(parts[0])
        minutes = float(parts[1])
        seconds = float(parts[2])
    except ValueError:
        return 0.0
    total = hours * 3600.0 + minutes * 60.0 + seconds
    return total if total > 0 else 0.0


@dataclass
class MediaEvent:
    kind: str          # "duration" | "progress" | "done" | "error" | "info"
    seconds: float = 0.0
    percent: Optional[float] = None
    message: str = ""


def parse_media_line(line: str, duration: float = 0.0) -> MediaEvent:
    """One line of the merged `-progress pipe:1` stream -> a small event."""
    text = str(line or "").strip()
    if not text:
        return MediaEvent("info")
    if text.startswith("Duration:"):
        value = text.split("Duration:", 1)[1].split(",", 1)[0].strip()
        return MediaEvent("duration", seconds=parse_duration_seconds(value))
    for prefix in ("out_time_ms=", "out_time_us="):
        if text.startswith(prefix):
            try:
                micros = float(text.split("=", 1)[1])
            except ValueError:
                return MediaEvent("info")
            seconds = micros / 1000000.0
            percent = None
            if duration > 0:
                percent = max(0.0, min(99.9, seconds / duration * 100.0))
            return MediaEvent("progress", seconds=seconds, percent=percent)
    if text.startswith("out_time="):
        return MediaEvent("progress", seconds=parse_duration_seconds(
            text.split("=", 1)[1]))
    if text.startswith("progress="):
        value = text.split("=", 1)[1].strip().lower()
        if value == "end":
            return MediaEvent("done")
        return MediaEvent("info")
    if text.startswith(("Error", "Invalid", "Output file is empty")):
        return MediaEvent("error", message=text[:300])
    return MediaEvent("info")


def has_free_space(directory: str, needed_bytes: float,
                   reserve: int = 64 * 1024 * 1024) -> bool:
    """True when `directory` has room for `needed_bytes` plus a reserve."""
    try:
        free = shutil.disk_usage(str(directory or ".")).free
    except OSError:
        return True
    return free - float(needed_bytes or 0) >= float(reserve)


def _subprocess_popen(command, **kwargs):  # pragma: no cover - thin wrapper
    return subprocess.Popen(command, **kwargs)


class AudioProcessor:
    """Runs one audio conversion for a Task, using the download state model."""

    def __init__(self, manager, ffmpeg: str = "",
                 download_dir: Optional[str] = None,
                 popen_factory: Optional[Callable] = None,
                 logger: Optional[Callable[..., None]] = None):
        self.manager = manager
        self.ffmpeg = ffmpeg or ""
        self.download_dir = download_dir or os.path.dirname(
            os.path.abspath(__file__))
        self._popen_factory = popen_factory or _subprocess_popen
        self._log = logger or (lambda *a: None)

    # -- helpers -----------------------------------------------------
    def _fail(self, task_id: str, code: str, message: str) -> Dict[str, Any]:
        try:
            self.manager.transition(task_id, "error", error_code=code,
                                    error_message=message)
        except Exception as exc:  # noqa: BLE001 - diagnostics only
            self._log(f"media {task_id} could not be marked error: {exc}")
        self._log(f"media {task_id} failed: {code} {message}")
        return {"task_id": task_id, "error_code": code, "message": message}

    # -- one conversion ----------------------------------------------
    def run(self, task_id: str, media_job: Dict[str, Any],
            control: Any = None) -> Dict[str, Any]:
        """Convert `media_job['source']` and drive the Task state machine.

        `media_job` is the scheduler's send-time record
        (`{"kind": "audio", "source": ..., "target": ...}`); it never comes
        from raw HTTP text. Returns a small dict; never raises.
        """
        job = dict(media_job or {})
        target, code, message = resolve_target(job.get("target"))
        if target is None:
            return self._fail(task_id, code, message)
        source = str(job.get("source") or "")
        if not source:
            return self._fail(task_id, ERROR_SOURCE_NOT_FOUND,
                              "no source file recorded for this task")
        if not self.ffmpeg:
            return self._fail(task_id, ERROR_FFMPEG_MISSING,
                              "ffmpeg path is not configured")
        try:
            self.manager.transition(task_id, "downloading")
        except Exception as exc:  # noqa: BLE001 - diagnostics only
            self._log(f"media {task_id} could not start: {exc}")
            return {"task_id": task_id, "error_code": "start_failed",
                    "message": str(exc)}
        if control is not None and control.cancel_requested():
            return self._finish_cancelled(task_id, control, "")
        if control is not None and control.pause_requested():
            return self._finish_paused(task_id)

        try:
            needed = os.path.getsize(source) * 2.0
        except OSError:
            needed = 0.0
        if not has_free_space(self.download_dir, needed):
            return self._fail(task_id, ERROR_INSUFFICIENT_SPACE,
                              "not enough free disk space for the conversion")

        destination = output_path_for(source, target)
        command = build_ffmpeg_command(self.ffmpeg, source, destination, target)
        self._log(f"media {task_id} start: {source} -> {destination}")
        duration = 0.0
        error_text = ""
        try:
            kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT,
                      "text": True, "encoding": "utf-8", "errors": "replace"}
            if sys.platform == "win32":
                kwargs["creationflags"] = getattr(subprocess,
                                                  "CREATE_NO_WINDOW", 0)
            process = self._popen_factory(command, **kwargs)
            if control is not None:
                control.attach_process(process)
            for raw in process.stdout:
                if control is not None and (control.cancel_requested()
                                            or control.pause_requested()):
                    terminate_tree(process, self._log)
                    break
                line = (raw or "").strip()
                if not line:
                    continue
                self._log(f"[{task_id}] {line}")
                event = parse_media_line(line, duration)
                if event.kind == "duration":
                    duration = event.seconds
                elif event.kind == "progress" and event.percent is not None:
                    self.manager.report_progress(task_id, event.percent,
                                                 "", "")
                elif event.kind == "error" and not error_text:
                    error_text = event.message
            process.wait()
        except FileNotFoundError:
            return self._fail(task_id, ERROR_FFMPEG_MISSING,
                              f"ffmpeg not found at {self.ffmpeg}")
        except Exception as exc:  # noqa: BLE001 - never leak a slot
            return self._fail(task_id, ERROR_FFMPEG_FAILED, str(exc))

        if control is not None and control.cancel_requested():
            return self._finish_cancelled(task_id, control, destination)
        if control is not None and control.pause_requested():
            return self._finish_paused(task_id)
        if process.returncode != 0:
            self._discard_partial(destination)
            return self._fail(task_id, ERROR_FFMPEG_FAILED,
                              error_text or f"returncode={process.returncode}")
        if not os.path.isfile(destination) or os.path.getsize(destination) <= 0:
            self._discard_partial(destination)
            return self._fail(task_id, ERROR_OUTPUT_MISSING,
                              "ffmpeg reported success but produced no file")
        self.manager.transition(task_id, "completed", percent=100.0,
                                speed="", eta="", file_path=destination)
        self._log(f"media {task_id} completed: {destination}")
        return {"task_id": task_id, "target": target.name,
                "file_path": destination}

    def _discard_partial(self, destination: str) -> bool:
        """Never leave an output file that only looks finished."""
        try:
            if destination and os.path.isfile(destination):
                os.remove(destination)
                return True
        except OSError as exc:
            self._log(f"media cleanup failed for {destination}: {exc}")
        return False

    def _finish_paused(self, task_id: str) -> Dict[str, Any]:
        try:
            self.manager.transition(task_id, "paused")
        except Exception as exc:  # noqa: BLE001 - diagnostics only
            self._log(f"media {task_id} could not be marked paused: {exc}")
        self._log(f"media {task_id} paused (partial output kept for retry)")
        return {"task_id": task_id, "error_code": "paused"}

    def _finish_cancelled(self, task_id: str, control: Any,
                          destination: str) -> Dict[str, Any]:
        try:
            self.manager.transition(task_id, "cancelled")
        except Exception as exc:  # noqa: BLE001 - diagnostics only
            self._log(f"media {task_id} could not be marked cancelled: {exc}")
        removed = self._discard_partial(destination) if destination else False
        if control is not None:
            try:
                control.detach_process()
            except Exception:  # noqa: BLE001 - best effort
                pass
        self._log(f"media {task_id} cancelled (removed_output={removed})")
        return {"task_id": task_id, "error_code": "cancelled",
                "removed_output": removed}
