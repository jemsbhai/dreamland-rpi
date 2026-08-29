import os
import time
import subprocess
import unicodedata

# Configuration
STATIC_IMAGE = "/home/raspberry/display_system/static_image.jpg"
VIDEO_DIR = "/home/raspberry/display_system/videos"
IMAGE_DISPLAY_TIME = 30  # seconds
LOG_FILE = "/home/raspberry/display_system/sanitized_log.txt"

def sanitize_all_filenames(directory):
    renamed = []
    for filename in os.listdir(directory):
        original_path = os.path.join(directory, filename)
        if not filename.lower().endswith(".mp4") or not os.path.isfile(original_path):
            continue

        safe_name = unicodedata.normalize('NFKD', filename).encode('ascii', 'ignore').decode('ascii')
        safe_name = safe_name.replace(' ', '_')
        if filename != safe_name:
            safe_path = os.path.join(directory, safe_name)
            os.rename(original_path, safe_path)
            renamed.append((filename, safe_name))

    if renamed:
        with open(LOG_FILE, 'a') as log:
            log.write(f"\nRenamed at {time.ctime()}:\n")
            for orig, new in renamed:
                log.write(f"{orig} -> {new}\n")
        print(f"Sanitized {len(renamed)} filenames. Log written to {LOG_FILE}")

def show_image_with_mpv(image_path, duration):
    print("Displaying static image...")
    subprocess.run([
        "mpv",
        "--fs",
        "--no-terminal",
        "--no-osd-bar",
        "--really-quiet",
        "--loop=no",
        f"--image-display-duration={duration}",
        image_path
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def play_all_videos_with_mpv():
    video_files = sorted([
        os.path.join(VIDEO_DIR, f)
        for f in os.listdir(VIDEO_DIR)
        if f.lower().endswith(".mp4")
    ])

    if not video_files:
        print("No videos found.")
        return

    print("Playing videos...")
    subprocess.run([
        "mpv",
        "--fs",
        "--no-terminal",
        "--no-osd-bar",
        "--loop=no",
        "--really-quiet",
        *video_files
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def main():
    sanitize_all_filenames(VIDEO_DIR)

    while True:
        show_image_with_mpv(STATIC_IMAGE, IMAGE_DISPLAY_TIME)
        play_all_videos_with_mpv()
        print("Loop complete. Restarting...\n")

if __name__ == "__main__":
    main()
