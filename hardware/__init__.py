"""
hardware package
================

One module per physical device:

    camera.py       Raspberry Pi CSI camera (Picamera2 / libcamera)
    ultrasonic.py   HC-SR04 distance sensor on the GPIO header
    audio.py        beep generation + playback through the headphone jack
    gpio_backend.py thin wrapper so ultrasonic.py works with lgpio or RPi.GPIO

Nothing is imported here on purpose. Importing this package on a Windows PC
must not drag in Raspberry-Pi-only libraries; each module imports its own
dependencies lazily, inside open(), so that failures surface as clear
runtime messages instead of import-time crashes.
"""
