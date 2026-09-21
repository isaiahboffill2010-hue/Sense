"""
hardware package
================

One module per physical device:

    camera.py       Raspberry Pi CSI camera (Picamera2 / libcamera)
    ultrasonic.py   SunFounder ultrasonic module via the robot_hat library
    audio.py        beep generation + playback through the headphone jack

Nothing is imported here on purpose. Importing this package on a Windows PC
must not drag in Raspberry-Pi-only libraries; each module imports its own
dependencies lazily, inside open(), so that failures surface as clear
runtime messages instead of import-time crashes.
"""
