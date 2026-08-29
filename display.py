#!/usr/bin/env python3
"""Dreamland floor projection controller.

Plays the exhibit media on a projector driven by a Raspberry Pi 4.

Modes (config.json "mode", or --mode on the command line):

  loop    one mpv process cycles the playlist forever and is restarted if it
          ever exits; no camera, standard library only
  detect  a camera feeds a TFLite person detector; a detected person triggers
          one video, after which the idle image returns; a single persistent
          mpv window is driven over its JSON IPC socket so nothing flashes

Run with --dry-run to print the mpv command line without starting anything.
"""
from __future__ import annotations

import argparse
import copy
import dataclasses
import importlib
import json
import logging
import os
import random
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

__version__ = "1.0.0"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".gif"}
VALID_ROTATIONS = (0, 90, 180, 270)
VALID_FITS = ("contain", "cover")
VALID_MODES = ("loop", "detect")
VALID_SELECTIONS = ("random", "sequential")
VALID_CAMERAS = ("opencv", "picamera2")

DEFAULT_CONFIG: Dict[str, Any] = {
    "mode": "loop",
    "display": {
        "audio": True,
        "mpv_args": ["--hwdec=v4l2m2m-copy"],
    },
    "loop": {
        "image_seconds": 30,
        "restart_delay_seconds": 2,
    },
    "playlist": [],
    "detect": {
        "idle_image": "media/title.jpg",
        "camera": "opencv",
        "camera_index": 0,
        "frame_width": 640,
        "frame_height": 480,
        "detect_fps": 5,
        "model": "models/efficientdet_lite0.tflite",
        "labels": "models/labels.txt",
        "score_threshold": 0.6,
        "cooldown_seconds": 5,
        "loops": 10,
        "selection": "random",
        "max_play_seconds": 600,
        "ipc_socket": "/tmp/dreamland-mpv.sock",
    },
    "log_file": "dreamland.log",
}

# Options every mpv launch gets. Kept minimal on purpose: anything site
# specific belongs in config.json under display.mpv_args.
MPV_COMMON_ARGS = [
    "--fullscreen",
    "--no-terminal",
    "--no-osd-bar",
    "--osd-level=0",
    "--really-quiet",
    "--cursor-autohide=always",
    "--force-window=yes",
    "--keep-open=no",
]

# mpv 0.38.0 inserted an "index" argument into loadfile; see loadfile_command.
LOADFILE_INDEX_VERSION = (0, 38, 0)

LOG_NAME = "dreamland"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def merge_defaults(user: Dict[str, Any], defaults: Dict[str, Any]) -> Dict[str, Any]:
    """Overlay user values on the defaults. Dict sections merge one level deep."""
    merged: Dict[str, Any] = {}
    for key, default_value in defaults.items():
        if key not in user:
            merged[key] = copy.deepcopy(default_value)
            continue
        value = user[key]
        if isinstance(default_value, dict) and isinstance(value, dict):
            section = copy.deepcopy(default_value)
            section.update(value)
            merged[key] = section
        else:
            merged[key] = copy.deepcopy(value)
    for key, value in user.items():
        if key not in merged:
            merged[key] = copy.deepcopy(value)
    return merged


def _require_number(section: str, key: str, value: Any, minimum: Optional[float] = None,
                    maximum: Optional[float] = None, strict_min: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{section}.{key} must be a number, got {value!r}")
    if minimum is not None:
        if strict_min and value <= minimum:
            raise ValueError(f"{section}.{key} must be greater than {minimum}, got {value}")
        if not strict_min and value < minimum:
            raise ValueError(f"{section}.{key} must be at least {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{section}.{key} must be at most {maximum}, got {value}")


def validate_config(config: Dict[str, Any]) -> None:
    """Raise ValueError on anything display.py could not act on."""
    if config.get("mode") not in VALID_MODES:
        raise ValueError(f"mode must be one of {VALID_MODES}, got {config.get('mode')!r}")
    if not isinstance(config.get("playlist"), list):
        raise ValueError("playlist must be a list")
    display = config["display"]
    if not isinstance(display.get("mpv_args"), list) or not all(isinstance(a, str) for a in display["mpv_args"]):
        raise ValueError("display.mpv_args must be a list of strings")
    if not isinstance(display.get("audio"), bool):
        raise ValueError("display.audio must be true or false")
    loop = config["loop"]
    _require_number("loop", "image_seconds", loop["image_seconds"], minimum=0, strict_min=True)
    _require_number("loop", "restart_delay_seconds", loop["restart_delay_seconds"], minimum=0)
    detect = config["detect"]
    if detect["selection"] not in VALID_SELECTIONS:
        raise ValueError(f"detect.selection must be one of {VALID_SELECTIONS}")
    if detect["camera"] not in VALID_CAMERAS:
        raise ValueError(f"detect.camera must be one of {VALID_CAMERAS}")
    _require_number("detect", "detect_fps", detect["detect_fps"], minimum=0, strict_min=True)
    _require_number("detect", "score_threshold", detect["score_threshold"], minimum=0, maximum=1)
    _require_number("detect", "cooldown_seconds", detect["cooldown_seconds"], minimum=0)
    _require_number("detect", "loops", detect["loops"], minimum=1)
    _require_number("detect", "max_play_seconds", detect["max_play_seconds"], minimum=0, strict_min=True)
    _require_number("detect", "camera_index", detect["camera_index"], minimum=0)
    _require_number("detect", "frame_width", detect["frame_width"], minimum=1)
    _require_number("detect", "frame_height", detect["frame_height"], minimum=1)
    if not isinstance(config.get("log_file"), str) or not config["log_file"]:
        raise ValueError("log_file must be a non-empty path")


def load_config(path: Path) -> Dict[str, Any]:
    """Read config.json, fill in defaults, validate."""
    with open(path, "r", encoding="utf-8") as handle:
        user = json.load(handle)
    if not isinstance(user, dict):
        raise ValueError(f"{path} must contain a JSON object")
    config = merge_defaults(user, DEFAULT_CONFIG)
    validate_config(config)
    return config


# ---------------------------------------------------------------------------
# Playlist items
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Item:
    path: Path
    kind: str                # "image" or "video" (gifs count as video)
    rotate: int = 0          # clockwise degrees, one of VALID_ROTATIONS
    loops: int = 1           # total plays for a video or gif
    seconds: float = 30.0    # hold time for an image
    fit: str = "contain"     # "contain" keeps the whole frame, "cover" fills and crops


def classify(path: Path) -> str:
    """Return "image" or "video" from the file extension, or raise ValueError."""
    extension = path.suffix.lower()
    if extension in IMAGE_EXTENSIONS:
        return "image"
    if extension in VIDEO_EXTENSIONS:
        return "video"
    raise ValueError(f"unsupported media type: {path.name}")


def parse_item(raw: Any, base_dir: Path, default_seconds: float) -> Item:
    """Turn one playlist entry (dict or bare path string) into an Item."""
    if isinstance(raw, str):
        raw = {"file": raw}
    if not isinstance(raw, dict) or not raw.get("file"):
        raise ValueError(f"playlist entries need a \"file\" field, got {raw!r}")
    path = Path(str(raw["file"]))
    if not path.is_absolute():
        path = base_dir / path
    kind = classify(path)

    rotate = raw.get("rotate", 0)
    if isinstance(rotate, bool) or rotate not in VALID_ROTATIONS:
        raise ValueError(f"{path.name}: rotate must be one of {VALID_ROTATIONS}, got {rotate!r}")

    loops = raw.get("loops", 1)
    if isinstance(loops, bool) or not isinstance(loops, int) or loops < 1:
        raise ValueError(f"{path.name}: loops must be an integer of at least 1, got {loops!r}")

    seconds = raw.get("seconds", default_seconds)
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
        raise ValueError(f"{path.name}: seconds must be a positive number, got {seconds!r}")

    fit = raw.get("fit", "contain")
    if fit not in VALID_FITS:
        raise ValueError(f"{path.name}: fit must be one of {VALID_FITS}, got {fit!r}")

    return Item(path=path, kind=kind, rotate=int(rotate), loops=int(loops),
                seconds=float(seconds), fit=str(fit))


def resolve_playlist(config: Dict[str, Any], base_dir: Path) -> Tuple[List[Item], List[Item]]:
    """Parse every playlist entry and split into (present, missing) by file existence."""
    present: List[Item] = []
    missing: List[Item] = []
    default_seconds = float(config["loop"]["image_seconds"])
    for raw in config["playlist"]:
        item = parse_item(raw, base_dir, default_seconds)
        (present if item.path.is_file() else missing).append(item)
    return present, missing


def idle_item(config: Dict[str, Any], base_dir: Path) -> Optional[Item]:
    """The detect-mode idle image as an Item, or None if it is not configured or missing."""
    raw = config["detect"].get("idle_image")
    if not raw:
        return None
    item = parse_item({"file": raw}, base_dir, float(config["loop"]["image_seconds"]))
    if item.kind != "image":
        raise ValueError(f"detect.idle_image must be an image, got {item.path.name}")
    return item if item.path.is_file() else None


# ---------------------------------------------------------------------------
# mpv command construction
# ---------------------------------------------------------------------------

def format_seconds(value: float) -> str:
    """30.0 -> "30", 2.5 -> "2.5"; keeps mpv arguments readable."""
    value = float(value)
    return str(int(value)) if value.is_integer() else repr(value)


def item_options(item: Item) -> List[Tuple[str, str]]:
    """(option, value) pairs mpv needs for this item, independent of syntax."""
    options: List[Tuple[str, str]] = []
    if item.rotate:
        options.append(("video-rotate", str(item.rotate)))
    if item.fit == "cover":
        options.append(("panscan", "1.0"))
    if item.kind == "image":
        options.append(("image-display-duration", format_seconds(item.seconds)))
    elif item.loops > 1:
        # --loop-file counts additional repeats, so N plays means N-1 repeats.
        options.append(("loop-file", str(item.loops - 1)))
    return options


def item_cli_args(item: Item) -> List[str]:
    """Command-line form: a per-file option group around the path."""
    args = ["--{"]
    args.extend(f"--{name}={value}" for name, value in item_options(item))
    args.append(str(item.path))
    args.append("--}")
    return args


def _display_args(config: Dict[str, Any]) -> List[str]:
    args: List[str] = []
    if not config["display"]["audio"]:
        args.append("--no-audio")
    args.extend(config["display"]["mpv_args"])
    return args


def build_loop_command(config: Dict[str, Any], items: Sequence[Item], mpv: str = "mpv") -> List[str]:
    """Full argv for loop mode: one mpv, whole playlist, forever."""
    if not items:
        raise ValueError("nothing to play: none of the playlist files exist")
    command = [mpv, *MPV_COMMON_ARGS, "--loop-playlist=inf"]
    command.extend(_display_args(config))
    for item in items:
        command.extend(item_cli_args(item))
    return command


def build_detect_command(config: Dict[str, Any], mpv: str = "mpv") -> List[str]:
    """Argv for the persistent, idle, IPC-controlled mpv used by detect mode."""
    command = [
        mpv,
        *MPV_COMMON_ARGS,
        "--idle=yes",
        "--image-display-duration=inf",
        f"--input-ipc-server={config['detect']['ipc_socket']}",
    ]
    command.extend(_display_args(config))
    return command


# ---------------------------------------------------------------------------
# mpv JSON IPC
# ---------------------------------------------------------------------------

def parse_mpv_version(text: str) -> Optional[Tuple[int, int, int]]:
    """"mpv 0.35.1" or "mpv v0.38.0-123-gabc" -> (0, 35, 1); None if no version found."""
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def loadfile_command(path: str, options: Sequence[Tuple[str, str]],
                     version: Optional[Tuple[int, int, int]]) -> List[Any]:
    """Build a loadfile command for the given mpv version.

    Before 0.38: loadfile <url> [<flags> [<options>]]
    From 0.38:   loadfile <url> [<flags> [<index> [<options>]]], index -1 when
                 options are passed. Unknown version is treated as current.
    """
    joined = ",".join(f"{name}={value}" for name, value in options)
    if not joined:
        return ["loadfile", path, "replace"]
    if version is not None and version < LOADFILE_INDEX_VERSION:
        return ["loadfile", path, "replace", joined]
    return ["loadfile", path, "replace", -1, joined]


def encode_command(args: Sequence[Any], request_id: int) -> bytes:
    """One JSON IPC request line."""
    return (json.dumps({"command": list(args), "request_id": request_id}) + "\n").encode("utf-8")


def decode_message(line: bytes) -> Dict[str, Any]:
    """Parse one JSON IPC line (a reply or an event)."""
    message = json.loads(line.decode("utf-8"))
    if not isinstance(message, dict):
        raise ValueError(f"unexpected IPC message: {message!r}")
    return message


class MpvIpc:
    """Minimal client for mpv's JSON IPC over a Unix socket."""

    def __init__(self, socket_path: str, log: logging.Logger):
        self.socket_path = socket_path
        self.log = log
        self._sock: Optional[socket.socket] = None
        self._buffer = b""
        self._events: List[Dict[str, Any]] = []
        self._next_id = 1
        self.version: Optional[Tuple[int, int, int]] = None

    def connect(self, timeout: float = 20.0) -> None:
        """Connect, retrying until mpv has created the socket."""
        deadline = time.monotonic() + timeout
        last_error: Optional[Exception] = None
        while time.monotonic() < deadline:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # type: ignore[attr-defined]
            try:
                sock.connect(self.socket_path)
            except OSError as exc:
                last_error = exc
                sock.close()
                time.sleep(0.25)
                continue
            self._sock = sock
            break
        if self._sock is None:
            raise RuntimeError(f"could not connect to mpv IPC socket {self.socket_path}: {last_error}")
        version_text = str(self.get_property("mpv-version"))
        self.version = parse_mpv_version(version_text)
        self.log.info("connected to mpv IPC: %s", version_text)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _read_message(self, timeout: float) -> Optional[Dict[str, Any]]:
        """Next JSON message, or None if nothing arrived within timeout seconds."""
        if self._sock is None:
            raise RuntimeError("not connected to mpv")
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                line = self._buffer[:newline]
                self._buffer = self._buffer[newline + 1:]
                if not line.strip():
                    continue
                try:
                    return decode_message(line)
                except ValueError as exc:
                    self.log.warning("ignoring malformed IPC line: %s", exc)
                    continue
            self._sock.settimeout(max(timeout, 0.001))
            try:
                chunk = self._sock.recv(65536)
            except socket.timeout:
                return None
            if not chunk:
                raise ConnectionError("mpv closed the IPC socket")
            self._buffer += chunk

    def command(self, *args: Any, timeout: float = 10.0) -> Any:
        """Send a command and return its "data"; events seen meanwhile are queued."""
        if self._sock is None:
            raise RuntimeError("not connected to mpv")
        request_id = self._next_id
        self._next_id += 1
        self._sock.sendall(encode_command(args, request_id))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"no reply from mpv to {args[0]!r}")
            message = self._read_message(remaining)
            if message is None:
                continue
            if "event" in message:
                self._events.append(message)
                continue
            if message.get("request_id") != request_id:
                continue
            if message.get("error") != "success":
                raise RuntimeError(f"mpv rejected {list(args)!r}: {message.get('error')}")
            return message.get("data")

    def get_property(self, name: str) -> Any:
        return self.command("get_property", name)

    def set_property(self, name: str, value: Any) -> Any:
        return self.command("set_property", name, value)

    def loadfile(self, path: str, options: Sequence[Tuple[str, str]]) -> None:
        """loadfile with per-file options, adapting to the mpv version's signature."""
        try:
            self.command(*loadfile_command(path, options, self.version))
            return
        except RuntimeError as exc:
            if not options:
                raise
            old_style = self.version is not None and self.version < LOADFILE_INDEX_VERSION
            self.version = LOADFILE_INDEX_VERSION if old_style else (LOADFILE_INDEX_VERSION[0], LOADFILE_INDEX_VERSION[1] - 1, 0)
            self.log.warning("loadfile rejected (%s); retrying with the %s signature",
                             exc, "0.38+" if old_style else "pre-0.38")
        self.command(*loadfile_command(path, options, self.version))

    def drain_events(self) -> None:
        """Discard queued and pending events so waits only see new ones."""
        self._events.clear()
        try:
            while self._read_message(0.01) is not None:
                pass
        except ConnectionError:
            raise
        except OSError:
            pass

    def wait_for_event(self, names: Iterable[str], timeout: float) -> Optional[Dict[str, Any]]:
        """Return the next event whose name is in names, or None on timeout."""
        wanted = set(names)
        for index, event in enumerate(self._events):
            if event.get("event") in wanted:
                del self._events[:index + 1]
                return event
        self._events.clear()
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            message = self._read_message(remaining)
            if message is not None and message.get("event") in wanted:
                return message


# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------

def terminate_process(proc: Optional[subprocess.Popen], timeout: float = 5.0) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def install_signal_handlers() -> None:
    """Make SIGTERM (systemd stop) behave like Ctrl+C so cleanup runs."""
    def handler(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGTERM, handler)
    except (ValueError, OSError, AttributeError):
        pass


# ---------------------------------------------------------------------------
# Loop mode
# ---------------------------------------------------------------------------

def describe_item(item: Item) -> str:
    parts = [item.path.name]
    if item.kind == "image":
        parts.append(f"{format_seconds(item.seconds)} s")
    elif item.loops > 1:
        parts.append(f"x{item.loops}")
    if item.rotate:
        parts.append(f"rotate {item.rotate}")
    if item.fit != "contain":
        parts.append(item.fit)
    return ", ".join(parts)


def run_loop(config: Dict[str, Any], items: Sequence[Item], mpv: str, log: logging.Logger) -> int:
    """Run mpv over the playlist forever, relaunching it whenever it exits."""
    command = build_loop_command(config, items, mpv)
    delay = float(config["loop"]["restart_delay_seconds"])
    log.info("loop mode with %d items:", len(items))
    for item in items:
        log.info("  %s", describe_item(item))
    while True:
        log.info("starting mpv")
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL)
        try:
            code = proc.wait()
        except KeyboardInterrupt:
            terminate_process(proc)
            raise
        log.warning("mpv exited with code %s; restarting in %.1f s", code, delay)
        time.sleep(delay)


# ---------------------------------------------------------------------------
# Detect mode: playback
# ---------------------------------------------------------------------------

def choose_index(count: int, selection: str, previous: int, rng: random.Random = random) -> int:
    """Next playlist index. Sequential wraps; random never repeats the last pick."""
    if count <= 0:
        raise ValueError("no items to choose from")
    if selection == "sequential" or count == 1:
        return (previous + 1) % count
    candidates = [i for i in range(count) if i != previous]
    return rng.choice(candidates)


def idle_options(item: Item) -> List[Tuple[str, str]]:
    """Options for the idle image; its hold time comes from the global inf setting."""
    return [(name, value) for name, value in item_options(item) if name != "image-display-duration"]


def wait_until_finished(ipc: MpvIpc, item: Item, max_play_seconds: float, log: logging.Logger) -> str:
    """Block until the file just loaded ends. Returns mpv's end reason or "timeout"."""
    started = False
    entry_id: Any = None
    deadline = time.monotonic() + max_play_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log.warning("%s reached max_play_seconds (%.0f); stopping it", item.path.name, max_play_seconds)
            try:
                ipc.command("stop")
            except (RuntimeError, OSError):
                pass
            return "timeout"
        event = ipc.wait_for_event(("start-file", "end-file", "idle"), remaining)
        if event is None:
            continue
        name = event.get("event")
        if name == "start-file":
            started = True
            entry_id = event.get("playlist_entry_id")
        elif name == "end-file":
            if not started:
                continue  # the file we replaced, not the one we loaded
            event_id = event.get("playlist_entry_id")
            if entry_id is not None and event_id is not None and event_id != entry_id:
                continue
            reason = str(event.get("reason", "unknown"))
            if reason == "error":
                log.warning("mpv could not play %s: %s", item.path.name, event.get("file_error", "unknown error"))
            return reason
        elif name == "idle" and started:
            return "idle"


class PersistentPlayer:
    """One long-lived mpv window for detect mode, controlled over IPC."""

    def __init__(self, config: Dict[str, Any], mpv: str, log: logging.Logger):
        self.config = config
        self.mpv = mpv
        self.log = log
        self.proc: Optional[subprocess.Popen] = None
        self.ipc: Optional[MpvIpc] = None

    def start(self) -> None:
        socket_path = self.config["detect"]["ipc_socket"]
        try:
            if os.path.exists(socket_path):
                os.remove(socket_path)
        except OSError:
            pass
        self.proc = subprocess.Popen(build_detect_command(self.config, self.mpv), stdin=subprocess.DEVNULL)
        self.ipc = MpvIpc(socket_path, self.log)
        try:
            self.ipc.connect()
        except Exception:
            terminate_process(self.proc)
            raise

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None and self.ipc is not None

    def close(self) -> None:
        if self.ipc is not None:
            self.ipc.close()
            self.ipc = None
        terminate_process(self.proc)
        self.proc = None

    def restart(self) -> None:
        self.close()
        self.start()

    def show_idle(self, idle: Optional[Item]) -> None:
        assert self.ipc is not None
        self.ipc.drain_events()
        if idle is None:
            self.ipc.command("stop")
            return
        self.ipc.loadfile(str(idle.path), idle_options(idle))

    def play(self, item: Item, loops: int, max_play_seconds: float) -> str:
        assert self.ipc is not None
        options = item_options(dataclasses.replace(item, loops=loops))
        self.ipc.drain_events()
        self.ipc.loadfile(str(item.path), options)
        return wait_until_finished(self.ipc, item, max_play_seconds, self.log)


# ---------------------------------------------------------------------------
# Detect mode: camera and detector
# ---------------------------------------------------------------------------

def read_labels(path: Path) -> List[str]:
    """Label list, one per line; a leading index ("0 person") is stripped."""
    labels: List[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(maxsplit=1)
        if len(parts) == 2 and parts[0].isdigit():
            line = parts[1]
        labels.append(line)
    return labels


def load_interpreter_class() -> Any:
    """Return a TFLite Interpreter class from whichever package is installed."""
    attempts: List[str] = []
    for module_name in ("tflite_runtime.interpreter", "ai_edge_litert.interpreter", "tensorflow.lite"):
        try:
            module = importlib.import_module(module_name)
            return getattr(module, "Interpreter")
        except Exception as exc:  # ImportError, or a broken install
            attempts.append(f"{module_name}: {exc}")
    raise RuntimeError("no TFLite interpreter available; install tflite-runtime "
                       "(see requirements-detect.txt). Tried " + "; ".join(attempts))


def identify_outputs(details: Sequence[Dict[str, Any]],
                     values_of: Callable[[int], Sequence[float]]) -> Optional[Dict[str, int]]:
    """Map output roles (boxes, classes, scores, optional count) to tensor positions.

    Order differs between TFLite detector exports (boxes/classes/scores/count
    for the TF1 SSD models, scores/boxes/count/classes for Model Maker
    EfficientDet-Lite), so nothing is assumed. Names are used first, then
    shapes, then the values themselves: class ids are integral, scores are not.
    Returns None when the current frame cannot settle it; call again next frame.
    """
    count = len(details)

    def shape_of(index: int) -> Tuple[int, ...]:
        return tuple(int(x) for x in details[index].get("shape", ()))

    def size_of(index: int) -> int:
        size = 1
        for dim in shape_of(index):
            size *= dim
        return size

    roles: Dict[str, int] = {}
    for index in range(count):
        name = str(details[index].get("name", "")).lower()
        if "box" in name:
            roles.setdefault("boxes", index)
        elif "class" in name:
            roles.setdefault("classes", index)
        elif "score" in name:
            roles.setdefault("scores", index)
        elif "num" in name or "count" in name:
            roles.setdefault("count", index)

    taken = set(roles.values())
    for index in range(count):
        if index in taken:
            continue
        shape = shape_of(index)
        if "boxes" not in roles and len(shape) >= 2 and shape[-1] == 4:
            roles["boxes"] = index
            taken.add(index)
        elif "count" not in roles and size_of(index) == 1:
            roles["count"] = index
            taken.add(index)

    flat = [i for i in range(count) if i not in taken and size_of(i) > 1]
    need_classes = "classes" not in roles
    need_scores = "scores" not in roles
    if need_classes and need_scores:
        if len(flat) != 2:
            return None
        integral = [all(float(v).is_integer() for v in values_of(i)) for i in flat]
        if integral[0] == integral[1]:
            return None  # this frame does not tell them apart
        classes_index = flat[0] if integral[0] else flat[1]
        roles["classes"] = classes_index
        roles["scores"] = flat[1] if classes_index == flat[0] else flat[0]
    elif need_classes or need_scores:
        if len(flat) != 1:
            return None
        roles["classes" if need_classes else "scores"] = flat[0]

    if "boxes" not in roles or "classes" not in roles or "scores" not in roles:
        return None
    return roles


class PersonDetector:
    """EfficientDet-Lite (or any TFLite SSD-style detector) person presence check."""

    def __init__(self, model_path: Path, labels_path: Path, threshold: float, log: logging.Logger):
        if not model_path.is_file():
            raise FileNotFoundError(f"model not found: {model_path} (see models/README.md)")
        if not labels_path.is_file():
            raise FileNotFoundError(f"labels not found: {labels_path} (see models/README.md)")
        interpreter_class = load_interpreter_class()
        self.interpreter = interpreter_class(model_path=str(model_path))
        self.interpreter.allocate_tensors()
        self.input = self.interpreter.get_input_details()[0]
        self.outputs = self.interpreter.get_output_details()
        shape = [int(x) for x in self.input["shape"]]
        self.height, self.width = shape[1], shape[2]
        self.labels = read_labels(labels_path)
        self.person_ids = {i for i, name in enumerate(self.labels) if name.lower() == "person"}
        if not self.person_ids:
            raise RuntimeError(f"{labels_path} has no \"person\" entry")
        self.threshold = float(threshold)
        self.roles: Optional[Dict[str, int]] = None
        self.log = log
        self._warned_float = False
        log.info("detector loaded: %s, input %dx%d, %d labels", model_path.name, self.width, self.height, len(self.labels))

    def _tensor(self, position: int) -> Any:
        return self.interpreter.get_tensor(self.outputs[position]["index"])

    def detect(self, frame_bgr: Any) -> bool:
        """True when a person is present above the threshold."""
        import cv2  # noqa: PLC0415 (detect mode only)
        import numpy as np  # noqa: PLC0415

        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        resized = cv2.resize(rgb, (self.width, self.height))
        tensor = np.expand_dims(resized, axis=0)
        if np.issubdtype(self.input["dtype"], np.floating):
            if not self._warned_float:
                self.log.info("float input model: feeding pixels scaled to 0..1")
                self._warned_float = True
            tensor = tensor.astype(np.float32) / 255.0
        else:
            tensor = tensor.astype(self.input["dtype"])
        self.interpreter.set_tensor(self.input["index"], tensor)
        self.interpreter.invoke()

        if self.roles is None:
            self.roles = identify_outputs(
                self.outputs, lambda i: self._tensor(i).flatten().tolist())
            if self.roles is None:
                return False
            self.log.info("model outputs identified: %s", self.roles)

        scores = self._tensor(self.roles["scores"]).flatten()
        classes = self._tensor(self.roles["classes"]).flatten()
        limit = min(len(scores), len(classes))
        if "count" in self.roles:
            limit = min(limit, int(self._tensor(self.roles["count"]).flatten()[0]))
        for i in range(limit):
            if float(scores[i]) >= self.threshold and int(classes[i]) in self.person_ids:
                return True
        return False


class OpenCvCamera:
    """USB webcam or any V4L2 device through OpenCV."""

    def __init__(self, index: int, width: int, height: int):
        import cv2  # noqa: PLC0415
        self.capture = cv2.VideoCapture(index)
        if not self.capture.isOpened():
            raise RuntimeError(f"camera index {index} could not be opened (tools/check_camera.sh may help)")
        self.capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)

    def read(self) -> Any:
        ok, frame = self.capture.read()
        return frame if ok else None

    def flush(self, frames: int = 5) -> None:
        """Drop buffered frames captured while a video was playing."""
        for _ in range(frames):
            self.capture.grab()

    def close(self) -> None:
        self.capture.release()


class PiCamera2Camera:
    """Raspberry Pi camera module through picamera2 (Bookworm libcamera stack)."""

    def __init__(self, width: int, height: int):
        from picamera2 import Picamera2  # noqa: PLC0415
        self.camera = Picamera2()
        # picamera2's "RGB888" delivers bytes in B, G, R order, matching OpenCV.
        config = self.camera.create_video_configuration(main={"format": "RGB888", "size": (width, height)})
        self.camera.configure(config)
        self.camera.start()
        time.sleep(0.5)

    def read(self) -> Any:
        return self.camera.capture_array()

    def flush(self, frames: int = 5) -> None:
        return None  # capture_array always returns the latest frame

    def close(self) -> None:
        self.camera.stop()


def make_camera(detect: Dict[str, Any]) -> Any:
    width, height = int(detect["frame_width"]), int(detect["frame_height"])
    if detect["camera"] == "picamera2":
        return PiCamera2Camera(width, height)
    return OpenCvCamera(int(detect["camera_index"]), width, height)


# ---------------------------------------------------------------------------
# Detect mode: main loop
# ---------------------------------------------------------------------------

def run_detect(config: Dict[str, Any], items: Sequence[Item], base_dir: Path,
               mpv: str, log: logging.Logger) -> int:
    detect = config["detect"]
    videos = [item for item in items if item.kind == "video"]
    if not videos:
        log.error("detect mode needs at least one existing video in the playlist")
        return 2
    idle = idle_item(config, base_dir)
    if idle is None:
        log.warning("idle image missing; the screen will be black between videos")

    detector = PersonDetector(base_dir / detect["model"], base_dir / detect["labels"],
                              float(detect["score_threshold"]), log)
    camera = make_camera(detect)
    player = PersistentPlayer(config, mpv, log)

    interval = 1.0 / float(detect["detect_fps"])
    cooldown = float(detect["cooldown_seconds"])
    loops = int(detect["loops"])
    max_play = float(detect["max_play_seconds"])
    selection = str(detect["selection"])
    last_index = -1
    allow_after = 0.0
    failed_reads = 0

    log.info("detect mode with %d videos (%s), threshold %.2f, %d loops per trigger:",
             len(videos), selection, detector.threshold, loops)
    for item in videos:
        log.info("  %s", describe_item(item))

    try:
        player.start()
        player.show_idle(idle)
        while True:
            if not player.alive():
                log.warning("mpv is gone; restarting it")
                player.restart()
                player.show_idle(idle)

            frame = camera.read()
            if frame is None:
                failed_reads += 1
                if failed_reads == 1 or failed_reads % 100 == 0:
                    log.warning("camera returned no frame (%d in a row)", failed_reads)
                time.sleep(interval)
                continue
            failed_reads = 0

            if time.monotonic() >= allow_after and detector.detect(frame):
                last_index = choose_index(len(videos), selection, last_index)
                item = videos[last_index]
                log.info("person detected: playing %s x%d", item.path.name, loops)
                try:
                    reason = player.play(item, loops, max_play)
                    log.info("playback ended (%s)", reason)
                    player.show_idle(idle)
                except (RuntimeError, OSError) as exc:
                    log.error("playback failed: %s; mpv will be restarted", exc)
                    player.close()
                camera.flush()
                allow_after = time.monotonic() + cooldown
            time.sleep(interval)
    finally:
        camera.close()
        player.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def setup_logging(log_path: Optional[Path]) -> logging.Logger:
    log = logging.getLogger(LOG_NAME)
    log.setLevel(logging.INFO)
    for handler in list(log.handlers):
        log.removeHandler(handler)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    log.addHandler(stream)
    if log_path is not None:
        try:
            file_handler = logging.FileHandler(str(log_path), encoding="utf-8")
            file_handler.setFormatter(formatter)
            log.addHandler(file_handler)
        except OSError as exc:
            log.warning("cannot open log file %s: %s", log_path, exc)
    return log


def parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dreamland floor projection controller")
    parser.add_argument("--config", default=str(Path(__file__).resolve().parent / "config.json"),
                        help="path to config.json (default: next to this script)")
    parser.add_argument("--mode", choices=VALID_MODES, help="override the mode in config.json")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the mpv command line and the resolved playlist, then exit")
    parser.add_argument("--mpv", help="path to the mpv binary (default: search PATH)")
    parser.add_argument("--version", action="version", version=f"display.py {__version__}")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    config_path = Path(args.config).resolve()
    base_dir = config_path.parent
    log = setup_logging(None)
    try:
        config = load_config(config_path)
    except (OSError, ValueError) as exc:
        log.error("config error: %s", exc)
        return 2
    if not args.dry_run:
        log = setup_logging(base_dir / config["log_file"])
    mode = args.mode or config["mode"]

    try:
        present, missing = resolve_playlist(config, base_dir)
    except ValueError as exc:
        log.error("playlist error: %s", exc)
        return 2
    for item in missing:
        log.warning("missing media, skipped: %s", item.path)

    mpv = args.mpv or shutil.which("mpv")

    if args.dry_run:
        mpv = mpv or "mpv"
        try:
            if mode == "loop":
                command = build_loop_command(config, present, mpv)
            else:
                command = build_detect_command(config, mpv)
        except ValueError as exc:
            log.error("%s", exc)
            return 2
        print(f"mode: {mode}")
        print("command:")
        print("  " + shlex.join(command))
        print("playlist:")
        for item in present:
            print("  " + describe_item(item))
        return 0

    if mpv is None:
        log.error("mpv not found on PATH; install it with: sudo apt-get install -y mpv")
        return 2

    install_signal_handlers()
    try:
        if mode == "loop":
            return run_loop(config, present, mpv, log)
        return run_detect(config, present, base_dir, mpv, log)
    except KeyboardInterrupt:
        log.info("stopped")
        return 0
    except (ValueError, RuntimeError, OSError) as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
