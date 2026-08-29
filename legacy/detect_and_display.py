import cv2
import numpy as np
import os
import time
import random
import subprocess
from tflite_runtime.interpreter import Interpreter

# Configuration
VIDEO_DIR = "/home/raspberry/display_system/videos"
STATIC_IMAGE = "/home/raspberry/display_system/static_image.jpg"
MODEL_PATH = "/home/raspberry/person_detection/efficientdet_lite0.tflite"
LABEL_PATH = "/home/raspberry/person_detection/labels.txt"
WINDOW_NAME = "DISPLAY"
DETECTION_TIMEOUT = 5
LOOP_COUNT = 10
RESOLUTION = (1920, 1080)  # Adjust based on your projector resolution

# Load labels
with open(LABEL_PATH, 'r') as f:
    labels = [line.strip() for line in f.readlines()]

# Load TensorFlow Lite model
interpreter = Interpreter(MODEL_PATH)
interpreter.allocate_tensors()
input_details = interpreter.get_input_details()
output_details = interpreter.get_output_details()
input_height = input_details[0]['shape'][1]
input_width = input_details[0]['shape'][2]

def detect_person(frame):
    resized = cv2.resize(frame, (input_width, input_height))
    input_data = np.expand_dims(resized, axis=0).astype(np.uint8)
    interpreter.set_tensor(input_details[0]['index'], input_data)
    interpreter.invoke()
    boxes = interpreter.get_tensor(output_details[0]['index'])[0]
    classes = interpreter.get_tensor(output_details[1]['index'])[0]
    scores = interpreter.get_tensor(output_details[2]['index'])[0]
    for i in range(len(scores)):
        if scores[i] > 0.6 and labels[int(classes[i])] == "person":
            return True
    return False

def fade_from_image(image, duration=1.0, steps=20):
    black = np.zeros_like(image)
    for i in range(steps):
        alpha = 1 - (i / steps)
        blended = cv2.addWeighted(image, alpha, black, 1 - alpha, 0)
        cv2.namedWindow(WINDOW_NAME, cv2.WND_PROP_FULLSCREEN)
        cv2.setWindowProperty(WINDOW_NAME, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
        cv2.imshow(WINDOW_NAME, blended)
        cv2.waitKey(1)
        time.sleep(duration / steps)
    cv2.destroyAllWindows()

def fade_to_image(image, duration=1.0, steps=20):
    black = np.zeros_like(image)
    for i in range(steps):
        alpha = i / steps
        blended = cv2.addWeighted(image, alpha, black, 1 - alpha, 0)
        cv2.namedWindow(WINDOW_NAME, cv2.WND_PROP_FULLSCREEN)
        cv2.setWindowProperty(WINDOW_NAME, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
        cv2.imshow(WINDOW_NAME, blended)
        cv2.waitKey(1)
        time.sleep(duration / steps)
    cv2.destroyAllWindows()

def get_video_duration(video_path):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", video_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        return float(result.stdout.strip())
    except:
        print("⚠️ Could not determine video duration. Using default 10s.")
        return 10.0

def play_video_loops(video_path, background_image, loops=10, fade=True):
    print(f"🎞️ Playing {os.path.basename(video_path)} for {loops} loops")

    if fade:
        fade_from_image(background_image)

    # VLC handles loops using input-repeat=N (N = loops - 1)
    vlc_process = subprocess.Popen([
        "cvlc",
        "--fullscreen",
        "--no-video-title-show",
        "--play-and-exit",
        f"--input-repeat={loops - 1}",
        video_path
    ])

    vlc_process.wait()

    if fade:
        fade_to_image(background_image)

def main():
    cap = cv2.VideoCapture(0)
    time.sleep(2)
    last_trigger = 0

    static_image = cv2.imread(STATIC_IMAGE)
    static_image = cv2.resize(static_image, RESOLUTION)

    while True:
        ret, frame = cap.read()
        if not ret:
            continue

        person_found = detect_person(frame)

        if person_found and (time.time() - last_trigger > DETECTION_TIMEOUT):
            print("🧍 Person detected!")

            video_files = [os.path.join(VIDEO_DIR, f)
                           for f in os.listdir(VIDEO_DIR) if f.endswith(".mp4")]

            if video_files:
                selected_video = random.choice(video_files)
                cap.release()
                play_video_loops(selected_video, static_image, loops=LOOP_COUNT, fade=True)
                cap = cv2.VideoCapture(0)
                time.sleep(2)
                last_trigger = time.time()
        else:
            fade_to_image(static_image, duration=0.5)
            time.sleep(0.1)

if __name__ == "__main__":
    main()
