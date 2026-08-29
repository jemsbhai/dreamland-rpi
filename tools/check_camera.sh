#!/bin/bash

echo "🔍 Raspberry Pi Camera Diagnostic Tool"
echo "======================================"

# Check if legacy camera interface is enabled
echo -e "\n🧠 Checking legacy camera support (vcgencmd)..."
VCGEN=$(vcgencmd get_camera 2>/dev/null)

if [ "$VCGEN" == "" ]; then
    echo "⚠️  vcgencmd not available or legacy camera not supported by current OS"
else
    echo "📷 Camera status: $VCGEN"
fi

# Check for /dev/video0
echo -e "\n📂 Checking for /dev/video0 (V4L2 support)..."
if [ -e /dev/video0 ]; then
    echo "✅ /dev/video0 exists! Camera is accessible by OpenCV."
else
    echo "❌ /dev/video0 not found."

    echo -e "\n🛠️ Suggesting fixes..."
    
    echo "- Check if camera is connected properly."
    echo "- Make sure legacy support is enabled:"
    echo "  sudo nano /boot/config.txt"
    echo "  → Add or edit the following:"
    echo "    start_x=1"
    echo "    gpu_mem=128"
    echo "    camera_auto_detect=1"
    echo "    dtoverlay=imx219"
    echo "  (Use imx477 for the HQ camera)"
    
    echo "- Reboot afterwards: sudo reboot"
fi

# Check if driver is loaded
echo -e "\n🔌 Checking if bcm2835-v4l2 driver is loaded..."
if lsmod | grep -q bcm2835_v4l2; then
    echo "✅ V4L2 camera driver is loaded."
else
    echo "❌ Driver not loaded. You can try:"
    echo "    sudo modprobe bcm2835-v4l2"
fi

# Suggest test tools
echo -e "\n🧪 Suggested test commands:"
echo "→ libcamera-hello (for Bookworm/libcamera stack)"
echo "→ python3 test_camera.py (for OpenCV after enabling /dev/video0)"
echo -e "\n✅ Done."
