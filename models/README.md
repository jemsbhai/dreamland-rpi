# models/

Detect mode expects two files here:

- `efficientdet_lite0.tflite`: the EfficientDet-Lite0 object detector
  (uint8 input, outputs boxes / classes / scores / count)
- `labels.txt`: one COCO class name per line, `person` among them

The original installation kept them at `/home/raspberry/person_detection/` on
the Pi. Copy them in with:

    cp /home/raspberry/person_detection/efficientdet_lite0.tflite models/
    cp /home/raspberry/person_detection/labels.txt models/

Loop mode does not use this directory.
