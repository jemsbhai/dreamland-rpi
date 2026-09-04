"""Tests for display.py: config, playlist, mpv command construction, IPC logic.

Everything here runs on any OS with no mpv, camera, or TFLite installed.
Real playback is verified on the Pi with `python3 display.py --dry-run`
followed by an actual run.
"""
import copy
import json
import logging
import random
import socket
from pathlib import Path

import pytest

import display


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    for name in ("title.jpg", "a.mp4", "b.gif"):
        (media / name).write_bytes(b"not really media")
    return tmp_path


def write_config(repo, data):
    path = repo / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def base_config(**overrides):
    config = copy.deepcopy(display.DEFAULT_CONFIG)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(config.get(key), dict):
            config[key].update(value)
        else:
            config[key] = value
    return config


class FakeSocket:
    """Stands in for mpv's IPC socket: scripted incoming lines plus auto replies."""

    def __init__(self, incoming=(), reply=None):
        self.incoming = [line if line.endswith(b"\n") else line + b"\n" for line in incoming]
        self.sent = []
        self.reply = reply or (lambda command: {"error": "success", "data": None})

    def settimeout(self, value):
        self.timeout = value

    def sendall(self, data):
        request = json.loads(data.decode("utf-8"))
        self.sent.append(request["command"])
        response = dict(self.reply(request["command"]))
        response["request_id"] = request["request_id"]
        self.incoming.append((json.dumps(response) + "\n").encode("utf-8"))

    def recv(self, size):
        if not self.incoming:
            raise socket.timeout()
        return self.incoming.pop(0)

    def close(self):
        pass


def event(name, **fields):
    payload = {"event": name}
    payload.update(fields)
    return json.dumps(payload).encode("utf-8")


def make_ipc(incoming=(), reply=None, version=(0, 35, 1)):
    ipc = display.MpvIpc("/tmp/fake.sock", logging.getLogger("test"))
    ipc._sock = FakeSocket(incoming, reply)
    ipc.version = version
    return ipc


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def test_merge_defaults_fills_sections_and_keeps_unknown_keys():
    merged = display.merge_defaults({"display": {"audio": False}, "extra": 1}, display.DEFAULT_CONFIG)
    assert merged["display"]["audio"] is False
    assert merged["display"]["mpv_args"] == display.DEFAULT_CONFIG["display"]["mpv_args"]
    assert merged["extra"] == 1
    assert merged["mode"] == "loop"


def test_merge_defaults_does_not_alias_defaults():
    merged = display.merge_defaults({}, display.DEFAULT_CONFIG)
    merged["display"]["mpv_args"].append("--x")
    assert "--x" not in display.DEFAULT_CONFIG["display"]["mpv_args"]


def test_load_config_reads_and_validates(repo):
    path = write_config(repo, {"mode": "detect", "playlist": ["media/a.mp4"]})
    config = display.load_config(path)
    assert config["mode"] == "detect"
    assert config["loop"]["image_seconds"] == 30


def test_load_config_missing_file_raises(tmp_path):
    with pytest.raises(OSError):
        display.load_config(tmp_path / "nope.json")


@pytest.mark.parametrize("broken", [
    {"mode": "party"},
    {"playlist": "media/a.mp4"},
    {"display": {"mpv_args": "--hwdec=no"}},
    {"display": {"audio": "yes"}},
    {"loop": {"image_seconds": 0}},
    {"loop": {"restart_delay_seconds": -1}},
    {"detect": {"selection": "shuffle"}},
    {"detect": {"camera": "webcam"}},
    {"detect": {"score_threshold": 1.5}},
    {"detect": {"loops": 0}},
    {"detect": {"detect_fps": 0}},
    {"log_file": ""},
])
def test_validate_config_rejects_bad_values(broken):
    with pytest.raises(ValueError):
        display.validate_config(base_config(**broken))


# ---------------------------------------------------------------------------
# Playlist items
# ---------------------------------------------------------------------------

def test_parse_item_defaults(repo):
    item = display.parse_item({"file": "media/a.mp4"}, repo, 30)
    assert item.path == repo / "media" / "a.mp4"
    assert item.kind == "video"
    assert (item.rotate, item.loops, item.seconds, item.fit) == (0, 1, 30.0, "contain")


def test_parse_item_string_shorthand_and_absolute_path(repo):
    absolute = str((repo / "media" / "title.jpg").resolve())
    item = display.parse_item(absolute, repo, 12)
    assert item.kind == "image"
    assert item.path == Path(absolute)
    assert item.seconds == 12.0


def test_parse_item_gif_is_video(repo):
    assert display.parse_item("media/b.gif", repo, 30).kind == "video"


def test_parse_item_full_options(repo):
    item = display.parse_item({"file": "media/a.mp4", "rotate": 90, "loops": 3, "fit": "cover"}, repo, 30)
    assert (item.rotate, item.loops, item.fit) == (90, 3, "cover")


@pytest.mark.parametrize("raw", [
    {"file": "media/a.mp4", "rotate": 45},
    {"file": "media/a.mp4", "rotate": "90"},
    {"file": "media/a.mp4", "loops": 0},
    {"file": "media/a.mp4", "loops": 2.5},
    {"file": "media/a.mp4", "fit": "stretch"},
    {"file": "media/title.jpg", "seconds": 0},
    {"file": "media/notes.txt"},
    {"rotate": 90},
    {"file": ""},
    42,
])
def test_parse_item_rejects_bad_entries(repo, raw):
    with pytest.raises(ValueError):
        display.parse_item(raw, repo, 30)


def test_resolve_playlist_splits_present_and_missing(repo):
    config = base_config(playlist=["media/title.jpg", "media/a.mp4", "media/missing.mp4"])
    present, missing = display.resolve_playlist(config, repo)
    assert [i.path.name for i in present] == ["title.jpg", "a.mp4"]
    assert [i.path.name for i in missing] == ["missing.mp4"]


def test_idle_item_present_missing_and_wrong_kind(repo):
    assert display.idle_item(base_config(), repo).path.name == "title.jpg"
    assert display.idle_item(base_config(detect={"idle_image": "media/gone.jpg"}), repo) is None
    assert display.idle_item(base_config(detect={"idle_image": ""}), repo) is None
    with pytest.raises(ValueError):
        display.idle_item(base_config(detect={"idle_image": "media/a.mp4"}), repo)


# ---------------------------------------------------------------------------
# mpv command construction
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [(30, "30"), (30.0, "30"), (2.5, "2.5"), (0.1, "0.1")])
def test_format_seconds(value, expected):
    assert display.format_seconds(value) == expected


def test_item_options_image_uses_display_duration():
    item = display.Item(Path("t.jpg"), "image", seconds=30)
    assert display.item_options(item) == [("image-display-duration", "30")]


def test_item_options_video_loops_are_repeats():
    item = display.Item(Path("v.mp4"), "video", loops=3)
    assert display.item_options(item) == [("loop-file", "2")]
    assert display.item_options(display.Item(Path("v.mp4"), "video", loops=1)) == []


def test_item_options_rotate_and_cover():
    item = display.Item(Path("v.mp4"), "video", rotate=90, fit="cover")
    assert display.item_options(item) == [("video-rotate", "90"), ("panscan", "1.0")]


def test_item_cli_args_wraps_group():
    item = display.Item(Path("v.mp4"), "video", rotate=270, loops=2)
    assert display.item_cli_args(item) == [
        "--{", "--video-rotate=270", "--loop-file=1", "v.mp4", "--}",
    ]


def test_build_loop_command_structure():
    items = [
        display.Item(Path("t.jpg"), "image", seconds=30),
        display.Item(Path("r.mp4"), "video", rotate=90),
    ]
    command = display.build_loop_command(base_config(display={"audio": False}), items, mpv="/usr/bin/mpv")
    assert command[0] == "/usr/bin/mpv"
    assert "--loop-playlist=inf" in command
    assert "--no-audio" in command
    assert "--hwdec=v4l2m2m-copy" in command
    assert "--fullscreen" in command
    tail = command[command.index("--{"):]
    assert tail == [
        "--{", "--image-display-duration=30", "t.jpg", "--}",
        "--{", "--video-rotate=90", "r.mp4", "--}",
    ]


def test_build_loop_command_audio_on_and_extra_args():
    items = [display.Item(Path("r.mp4"), "video")]
    config = base_config(display={"audio": True, "mpv_args": ["--hwdec=no", "--vo=gpu"]})
    command = display.build_loop_command(config, items)
    assert "--no-audio" not in command
    assert command[command.index("--hwdec=no"):command.index("--hwdec=no") + 2] == ["--hwdec=no", "--vo=gpu"]


def test_build_loop_command_empty_raises():
    with pytest.raises(ValueError):
        display.build_loop_command(base_config(), [])


def test_build_detect_command():
    command = display.build_detect_command(base_config(display={"audio": False}), mpv="mpv")
    assert "--idle=yes" in command
    assert "--image-display-duration=inf" in command
    assert "--input-ipc-server=/tmp/dreamland-mpv.sock" in command
    assert "--no-audio" in command
    assert "--loop-playlist=inf" not in command
    assert "--{" not in command


# ---------------------------------------------------------------------------
# IPC helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("mpv 0.35.1", (0, 35, 1)),
    ("mpv v0.38.0-123-gabcdef", (0, 38, 0)),
    ("mpv 0.40.0", (0, 40, 0)),
    ("mpv git-abcdef", None),
    ("", None),
])
def test_parse_mpv_version(text, expected):
    assert display.parse_mpv_version(text) == expected


def test_loadfile_command_signatures():
    options = [("video-rotate", "90"), ("loop-file", "9")]
    assert display.loadfile_command("/m/v.mp4", options, (0, 35, 1)) == [
        "loadfile", "/m/v.mp4", "replace", "video-rotate=90,loop-file=9"]
    assert display.loadfile_command("/m/v.mp4", options, (0, 38, 0)) == [
        "loadfile", "/m/v.mp4", "replace", -1, "video-rotate=90,loop-file=9"]
    assert display.loadfile_command("/m/v.mp4", options, None) == [
        "loadfile", "/m/v.mp4", "replace", -1, "video-rotate=90,loop-file=9"]
    assert display.loadfile_command("/m/v.mp4", [], (0, 35, 1)) == ["loadfile", "/m/v.mp4", "replace"]


def test_encode_and_decode_messages():
    line = display.encode_command(["get_property", "mpv-version"], 7)
    assert line.endswith(b"\n")
    assert json.loads(line) == {"command": ["get_property", "mpv-version"], "request_id": 7}
    assert display.decode_message(b'{"event":"end-file","reason":"eof"}') == {"event": "end-file", "reason": "eof"}
    with pytest.raises(ValueError):
        display.decode_message(b"[1, 2]")


def test_command_returns_data_and_queues_events():
    ipc = make_ipc(incoming=[event("start-file", playlist_entry_id=1)],
                   reply=lambda command: {"error": "success", "data": "mpv 0.35.1"})
    assert ipc.command("get_property", "mpv-version") == "mpv 0.35.1"
    assert ipc._events == [{"event": "start-file", "playlist_entry_id": 1}]


def test_command_error_raises():
    ipc = make_ipc(reply=lambda command: {"error": "invalid parameter"})
    with pytest.raises(RuntimeError):
        ipc.command("loadfile", "x")


def test_wait_for_event_prefers_queued_then_socket():
    ipc = make_ipc(incoming=[event("end-file", reason="eof")])
    ipc._events = [{"event": "pause"}, {"event": "start-file"}]
    assert ipc.wait_for_event(("start-file",), 1.0) == {"event": "start-file"}
    assert ipc._events == []
    assert ipc.wait_for_event(("end-file",), 1.0) == {"event": "end-file", "reason": "eof"}
    assert ipc.wait_for_event(("end-file",), 0.01) is None


def test_drain_events_clears_queue_and_socket():
    ipc = make_ipc(incoming=[event("idle")])
    ipc._events = [{"event": "pause"}]
    ipc.drain_events()
    assert ipc._events == []
    assert ipc.wait_for_event(("idle",), 0.01) is None


def test_loadfile_retries_with_other_signature():
    def reply(command):
        if command[0] == "loadfile" and len(command) == 4:
            return {"error": "invalid parameter"}
        return {"error": "success", "data": None}

    ipc = make_ipc(reply=reply, version=(0, 35, 1))
    ipc.loadfile("/m/v.mp4", [("video-rotate", "90")])
    assert ipc._sock.sent[-1] == ["loadfile", "/m/v.mp4", "replace", -1, "video-rotate=90"]
    assert ipc.version == (0, 38, 0)


# ---------------------------------------------------------------------------
# Detect-mode playback logic
# ---------------------------------------------------------------------------

LOG = logging.getLogger("test")
VIDEO = display.Item(Path("/m/v.mp4"), "video")


def test_wait_until_finished_ignores_replaced_file():
    ipc = make_ipc(incoming=[
        event("end-file", reason="stop", playlist_entry_id=1),
        event("start-file", playlist_entry_id=2),
        event("end-file", reason="eof", playlist_entry_id=2),
    ])
    assert display.wait_until_finished(ipc, VIDEO, 5.0, LOG) == "eof"
    assert ipc._sock.sent == []


def test_wait_until_finished_matches_entry_id():
    ipc = make_ipc(incoming=[
        event("start-file", playlist_entry_id=7),
        event("end-file", reason="eof", playlist_entry_id=3),
        event("end-file", reason="stop", playlist_entry_id=7),
    ])
    assert display.wait_until_finished(ipc, VIDEO, 5.0, LOG) == "stop"


def test_wait_until_finished_reports_error_and_idle():
    ipc = make_ipc(incoming=[
        event("start-file", playlist_entry_id=1),
        event("end-file", reason="error", file_error="Failed to recognize file format.", playlist_entry_id=1),
    ])
    assert display.wait_until_finished(ipc, VIDEO, 5.0, LOG) == "error"
    ipc = make_ipc(incoming=[event("start-file"), event("idle")])
    assert display.wait_until_finished(ipc, VIDEO, 5.0, LOG) == "idle"


def test_wait_until_finished_times_out_and_stops():
    ipc = make_ipc()
    assert display.wait_until_finished(ipc, VIDEO, 0.05, LOG) == "timeout"
    assert ipc._sock.sent == [["stop"]]


def test_idle_options_drop_hold_time():
    idle = display.Item(Path("t.jpg"), "image", rotate=180, seconds=30)
    assert display.idle_options(idle) == [("video-rotate", "180")]


def test_choose_index_sequential_wraps():
    picks = []
    previous = -1
    for _ in range(5):
        previous = display.choose_index(3, "sequential", previous)
        picks.append(previous)
    assert picks == [0, 1, 2, 0, 1]


def test_choose_index_random_never_repeats_last():
    rng = random.Random(0)
    previous = -1
    for _ in range(200):
        pick = display.choose_index(4, "random", previous, rng)
        assert 0 <= pick < 4 and pick != previous
        previous = pick


def test_choose_index_single_and_empty():
    assert display.choose_index(1, "random", 0) == 0
    with pytest.raises(ValueError):
        display.choose_index(0, "random", -1)


# ---------------------------------------------------------------------------
# Detector output identification
# ---------------------------------------------------------------------------

def test_identify_outputs_by_name():
    details = [
        {"name": "num_detections", "shape": (1,)},
        {"name": "detection_scores", "shape": (1, 10)},
        {"name": "detection_classes", "shape": (1, 10)},
        {"name": "detection_boxes", "shape": (1, 10, 4)},
    ]
    roles = display.identify_outputs(details, lambda i: [])
    assert roles == {"count": 0, "scores": 1, "classes": 2, "boxes": 3}


def test_identify_outputs_model_maker_order_by_shape_and_values():
    details = [
        {"name": "StatefulPartitionedCall:3", "shape": (1, 25)},
        {"name": "StatefulPartitionedCall:2", "shape": (1, 25, 4)},
        {"name": "StatefulPartitionedCall:1", "shape": (1,)},
        {"name": "StatefulPartitionedCall:0", "shape": (1, 25)},
    ]
    values = {0: [0.71, 0.2, 0.05], 3: [0.0, 1.0, 56.0]}
    roles = display.identify_outputs(details, lambda i: values.get(i, []))
    assert roles == {"boxes": 1, "count": 2, "scores": 0, "classes": 3}


def test_identify_outputs_ambiguous_frame_returns_none():
    details = [{"shape": (1, 10, 4)}, {"shape": (1, 10)}, {"shape": (1, 10)}, {"shape": (1,)}]
    assert display.identify_outputs(details, lambda i: [0.0] * 10) is None


def test_identify_outputs_without_count():
    details = [{"shape": (1, 10, 4)}, {"shape": (1, 10)}, {"shape": (1, 10)}]
    values = {1: [0.0, 1.0], 2: [0.9, 0.3]}
    roles = display.identify_outputs(details, lambda i: values[i])
    assert roles == {"boxes": 0, "classes": 1, "scores": 2}


def test_read_labels_strips_indices_and_blank_lines(tmp_path):
    labels = tmp_path / "labels.txt"
    labels.write_text("0 person\n1 bicycle\n\n???\n", encoding="utf-8")
    assert display.read_labels(labels) == ["person", "bicycle", "???"]


# ---------------------------------------------------------------------------
# Entry point (dry run)
# ---------------------------------------------------------------------------

def test_main_dry_run_loop(repo, capsys, caplog):
    path = write_config(repo, {"playlist": ["media/title.jpg", "media/a.mp4", "media/missing.mp4"]})
    with caplog.at_level(logging.WARNING, logger=display.LOG_NAME):
        assert display.main(["--config", str(path), "--dry-run", "--mpv", "mpv"]) == 0
    out = capsys.readouterr().out
    assert "mode: loop" in out
    assert "--loop-playlist=inf" in out
    assert "a.mp4" in out
    assert "missing.mp4" not in out
    assert any("missing media" in record.message and "missing.mp4" in record.message for record in caplog.records)


def test_main_dry_run_mode_override(repo, capsys):
    path = write_config(repo, {"playlist": ["media/a.mp4"]})
    assert display.main(["--config", str(path), "--dry-run", "--mode", "detect", "--mpv", "mpv"]) == 0
    out = capsys.readouterr().out
    assert "mode: detect" in out
    assert "--idle=yes" in out
    assert "--input-ipc-server=" in out


def test_main_dry_run_nothing_to_play(repo):
    path = write_config(repo, {"playlist": ["media/missing.mp4"]})
    assert display.main(["--config", str(path), "--dry-run", "--mpv", "mpv"]) == 2


def test_main_bad_config_returns_2(repo):
    path = write_config(repo, {"mode": "party"})
    assert display.main(["--config", str(path), "--dry-run"]) == 2


# ---------------------------------------------------------------------------
# The repository's own config files
# ---------------------------------------------------------------------------

def test_repo_config_files_are_valid_and_complete():
    """Every show shipped in the repo must parse and reference only files that exist.

    This is the guard against pushing a config that the operator's single
    command then fails on (typo in a path, media never committed).
    """
    root = Path(display.__file__).resolve().parent
    config_paths = sorted(root.glob("config*.json"))
    assert config_paths, "no config files found next to display.py"
    for path in config_paths:
        config = display.load_config(path)
        present, missing = display.resolve_playlist(config, root)
        assert present, f"{path.name}: no playlist entry points at an existing file"
        assert not missing, f"{path.name}: missing files {[i.path.name for i in missing]}"
