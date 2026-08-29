# Dreamland floor projection

Display controller for an exhibit installation: a Raspberry Pi 4 drives a
projector mounted overhead, throwing images and videos onto the floor. The
artwork is by Tec (Tec Fase). This repository holds the code that plays it and
the media it plays, so the Pi is updated with a single `git pull` and started
with a single script.

The code is MIT licensed. The artwork under `media/` is not: it belongs to the
artist and is included for this installation only. See NOTICE.

## Hardware

- Raspberry Pi 4, Raspberry Pi OS desktop image (Bookworm or newer)
- Projector on HDMI at 1920x1080, mounted overhead pointing at the floor
- Optional: a camera (USB webcam or Pi camera module) for detect mode

## Quick start on the Pi

On the Pi (over SSH or at a terminal):

    sudo apt-get update && sudo apt-get install -y git
    git clone https://github.com/jemsbhai/dreamland-rpi.git ~/dreamland
    cd ~/dreamland
    python3 display.py --dry-run
    ./run.sh

No credentials are needed: the repository is public and the Pi only ever
pulls. Pushes come from the laptop.

Every time after that:

    cd ~/dreamland && ./run.sh

`run.sh` pulls the latest commit, installs anything missing (mpv, plus the
Python packages if detect mode is configured), then starts the display. It works
from the Pi's own desktop session or over SSH.

    ./run.sh              # mode from config.json
    ./run.sh loop         # force loop mode
    ./run.sh detect       # force detect mode
    SKIP_PULL=1 ./run.sh  # start without touching git (no network needed)

To stop it: Ctrl+C in the terminal that started it, or `pkill -f display.py`.
A plain `./run.sh` over SSH ends when the SSH session ends; for the
installation, use the boot service below.

### Start at boot (optional, recommended for the installation)

    ./install-service.sh

installs a systemd service that starts the display after the desktop logs in,
restarts it if it ever dies, and survives power cuts. Once the service is
installed, `./run.sh` pulls and then restarts the service instead of opening a
second copy, so the update routine stays the same. Useful commands:

    journalctl -u dreamland -f      # follow the log
    sudo systemctl stop dreamland   # stop
    FOREGROUND=1 ./run.sh           # stop the service and run in this terminal for debugging
    ./install-service.sh --remove   # uninstall the service

## Modes

### loop (default)

One mpv process cycles through `playlist` in `config.json` forever: the title
image for its `seconds`, then each video in order, then back to the top. If mpv
exits for any reason, `display.py` restarts it after
`loop.restart_delay_seconds`. No camera, and no Python dependencies beyond the
standard library.

### detect

`display.py` reads frames from the camera, runs an EfficientDet-Lite0 TFLite
person detector, and when a person is seen plays one video from the playlist
(`detect.selection`: `random` or `sequential`) `detect.loops` times, then
returns to `detect.idle_image`. A single persistent mpv window stays fullscreen
the whole time and is driven over its JSON IPC socket, so there is no desktop
flash between image and video. Model files are expected under `models/`; see
"Detect mode setup".

## Layout

    run.sh                   single entry point on the Pi
    display.py               the controller (both modes)
    config.json              everything tunable
    install-service.sh       optional: start at boot via systemd (uses dreamland.service)
    media/                   artwork (see NOTICE)
    models/                  TFLite model and labels for detect mode
    tools/check_camera.sh    camera diagnostic
    legacy/                  the original three scripts, reference only
    tests/                   pytest suite (runs on any OS, no mpv needed)
    requirements-detect.txt  pip packages for detect mode
    requirements-dev.txt     pip packages for running the tests

## config.json reference

Paths are relative to the repository root.

Top level:

| key | meaning |
|---|---|
| `mode` | `loop` or `detect` |
| `display.audio` | `false` passes `--no-audio` to mpv (silent installation) |
| `display.mpv_args` | extra mpv options appended to every launch; default `--hwdec=v4l2m2m-copy` (Pi 4 hardware H.264 decode; mpv falls back to software decoding by itself if it fails) |
| `loop.image_seconds` | default hold time for images without their own `seconds` |
| `loop.restart_delay_seconds` | pause before relaunching mpv if it exits |
| `log_file` | where `display.py` appends its log |

Playlist items:

| key | default | meaning |
|---|---|---|
| `file` | required | image (jpg, jpeg, png, webp, bmp), video (mp4, mov, mkv, webm, avi) or gif |
| `rotate` | `0` | clockwise degrees: 0, 90, 180 or 270. Use 90 for portrait Instagram reels so they fill the 16:9 floor; use 270 if they come out upside down for the viewer |
| `loops` | `1` | how many times to play a video or gif before moving on |
| `seconds` | `loop.image_seconds` | how long to hold an image |
| `fit` | `contain` | `contain` shows the whole frame (bars if the aspect does not match); `cover` fills the floor and crops the edges |

Missing files are logged and skipped, so the config may list media that has not
been copied in yet.

Detect section:

| key | meaning |
|---|---|
| `idle_image` | shown whenever no video is playing |
| `camera` | `opencv` (USB webcam or any V4L2 device) or `picamera2` (Pi camera module on Bookworm) |
| `camera_index` | V4L2 index for opencv, usually 0 |
| `frame_width`, `frame_height` | capture size; small is fine, the detector resizes anyway |
| `detect_fps` | detection rate; 5 is plenty and keeps the CPU cool |
| `model`, `labels` | EfficientDet-Lite0 TFLite file and its label list |
| `score_threshold` | minimum person confidence, 0 to 1 |
| `cooldown_seconds` | ignore detections for this long after a video ends |
| `loops` | how many times the triggered video plays |
| `selection` | `random` or `sequential` |
| `max_play_seconds` | safety cap per trigger, in case mpv never reports end of file |
| `ipc_socket` | mpv IPC socket path |

## Adding or replacing media

1. Put the file in `media/`.
2. Add a line to `playlist` in `config.json` (or edit an existing one).
3. Commit and push from the laptop; `./run.sh` on the Pi pulls it.

Instagram reels are downloaded on the laptop with yt-dlp, with the artist's
permission, choosing an H.264 mp4 so the Pi can hardware-decode it:

    yt-dlp --cookies-from-browser firefox -S "vcodec:h264,res:1080" --merge-output-format mp4 -o "media/tecfase_%(autonumber)02d.%(ext)s" <post-url> <post-url> <post-url>

Reels are portrait (1080x1920). With `"rotate": 90` a 9:16 reel becomes 16:9
and fills the 1920x1080 projector exactly. A 4:5 reel (1080x1350) becomes 5:4
and leaves thin bars on the sides with `contain`, or is cropped with `cover`.

Keep individual files under 50 MB where possible: GitHub warns above 50 MB and
refuses above 100 MB.

## Detect mode setup

On the Pi, detect mode needs:

    sudo apt-get install -y python3-opencv python3-numpy
    pip install --break-system-packages -r requirements-detect.txt

plus the model files in `models/`. `run.sh` installs the packages itself when
`mode` is `detect`. With the Pi camera module on Bookworm, set
`"camera": "picamera2"` (the libcamera stack does not expose the module as
/dev/video0 for OpenCV). `tools/check_camera.sh` helps diagnose camera problems.

## Troubleshooting

- Black screen when started over SSH: `run.sh` finds the Pi's own Wayland or
  X11 socket and exports `WAYLAND_DISPLAY` / `DISPLAY` for mpv. The Pi must be
  logged in to the desktop (auto-login is the default on Raspberry Pi OS
  desktop images).
- `./run.sh: Permission denied`: `chmod +x run.sh install-service.sh` (the
  executable bit is stored in git, but a copied checkout can lose it).
- Stutter on the reels: try `"mpv_args": ["--hwdec=no"]` (software decode is
  fine for 1080p30 on a Pi 4).
- Reels upside down for the viewer: change `rotate` from 90 to 270.
- Nothing plays: `python3 display.py --dry-run` prints the exact mpv command;
  run it by hand to see mpv's own error.
- Log: `tail -f dreamland.log`.

## Development

Tests cover the pure logic (config parsing, playlist and mpv argument
construction, IPC message formatting) and run on any OS:

    pip install -r requirements-dev.txt
    pytest tests/ -v

Real playback needs a Pi (or any Linux machine with mpv) and is checked with
`python3 display.py --dry-run` followed by an actual run.
