



import cv2
import os
import time
import subprocess
import unicodedata

# Configuration
STATIC_IMAGE = "/home/raspberry/display_system/static_image.jpg"
VIDEO_DIR = "/home/raspberry/display_system/videos"
WINDOW_NAME = "DISPLAY"
RESOLUTION = (1920, 1080)
IMAGE_DISPLAY_TIME = 30
LOG_FILE = "/home/raspberry/display_system/sanitized_log.txt"



def sanitize_all_filenames(directory):
    renamed = []
    for filename in os.listdir(directory):
        original_path = os.path.join(directory, filename)
        if not filename.lower().endswith(".mp4") or not os.path.isfile(original_path):
            continue

        # Normalize to ASCII
        safe_name = unicodedata.normalize('NFKD', filename).encode('ascii', 'ignore').decode('ascii')
        # Replace spaces with underscores and strip any bad characters
        safe_name = safe_name.replace(' ', '_')
        # Avoid duplicate renaming
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



def show_static_image(image_path, duration=30):
    if not os.path.exists(image_path):
        print(f" Static image not found: {image_path}")
        return

    try:
        img = cv2.imread(image_path)
        img = cv2.resize(img, RESOLUTION)
        cv2.namedWindow(WINDOW_NAME, cv2.WND_PROP_FULLSCREEN)
        cv2.setWindowProperty(WINDOW_NAME, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
        cv2.imshow(WINDOW_NAME, img)
        print(f" Displaying static image for {duration} seconds...")
        start = time.time()
        while time.time() - start < duration:
            cv2.imshow(WINDOW_NAME, img)  # Keep refreshing window
            cv2.waitKey(100)
        cv2.destroyAllWindows()
    except Exception as e:
        print(f" Error displaying image: {e}")

def play_video(video_path):
    try:
        if not os.path.exists(video_path):
            print("Video not found:", video_path.encode('ascii', 'ignore').decode())
            return

        # Get a printable filename (safe for logs)
        filename_display = os.path.basename(video_path).encode('ascii', 'ignore').decode()
        print(f"Playing video: {filename_display}")

        subprocess.run([
            "cvlc",
            "--fullscreen",
            "--no-video-title-show",
            "--play-and-exit",
            "--quiet",
            video_path
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    except Exception as e:
        msg = str(e).encode('ascii', 'ignore').decode()
        print("Error playing video:", msg)


def main():
    
    sanitize_all_filenames(VIDEO_DIR)

    while True:
        show_static_image(STATIC_IMAGE, IMAGE_DISPLAY_TIME)

        video_files = sorted([
            os.path.join(VIDEO_DIR, f)
            for f in os.listdir(VIDEO_DIR)
            if f.lower().endswith(".mp4")
        ])

        if not video_files:
            print("No videos found. Waiting before retrying.")
            time.sleep(10)
            continue

        for video in video_files:
            play_video(video)

        print("Loop complete. Restarting...\n")
    

if __name__ == "__main__":
    main()
