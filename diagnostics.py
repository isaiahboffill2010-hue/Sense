"""
diagnostics.py
==============

Small helpers that answer "what machine am I actually running on?".

This exists so that if you accidentally run `python main.py` on your Windows
development PC, the program says so immediately and clearly instead of
throwing a confusing import error deep inside a camera library.
"""

import platform
import sys


def _read_first_line(path):
    """Read a /proc or /sys text file, or return None if it is not there."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(256)
    except OSError:
        return None
    # Device-tree strings are NUL terminated.
    return raw.decode("utf-8", "replace").replace("\x00", "").strip() or None


def raspberry_pi_model():
    """Return e.g. 'Raspberry Pi 3 Model B Plus Rev 1.3', or None."""
    for path in ("/proc/device-tree/model", "/sys/firmware/devicetree/base/model"):
        model = _read_first_line(path)
        if model:
            return model
    # Fallback for kernels without a device tree model node.
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("Model"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return None


def describe_platform():
    """Collect everything we want to print in the startup banner."""
    model = raspberry_pi_model()
    return {
        "system": platform.system(),                 # 'Linux' / 'Windows'
        "release": platform.release(),
        "machine": platform.machine(),               # 'aarch64', 'armv7l', ...
        "python": platform.python_version(),
        "executable": sys.executable,
        "is_linux": platform.system() == "Linux",
        "pi_model": model,
        "is_raspberry_pi": bool(model and "raspberry pi" in model.lower()),
    }


def print_platform_report(info):
    """Print the 'what am I running on' section of the startup banner."""
    print("Operating system : {} {}".format(info["system"], info["release"]))
    print("Architecture     : {}".format(info["machine"]))
    print("Python           : {}".format(info["python"]))

    if info["is_raspberry_pi"]:
        print("Raspberry Pi detected: {}".format(info["pi_model"]))
    elif info["is_linux"]:
        print("Raspberry Pi detected: NO (Linux, but no Raspberry Pi model string)")
    else:
        print("Raspberry Pi detected: NO")


def print_wrong_os_warning(info):
    """Loud, unmissable message for 'you ran this on the wrong computer'."""
    print("")
    print("=" * 62)
    print("  WRONG COMPUTER")
    print("=" * 62)
    print("  This program drives real Raspberry Pi hardware:")
    print("    - the CSI camera through Picamera2 / libcamera")
    print("    - the HC-SR04 through the Pi's GPIO header")
    print("")
    print("  Neither of those exists on {}.".format(info["system"]))
    print("")
    print("  Push this repository to GitHub, clone it on the Raspberry Pi,")
    print("  and run it there:")
    print("")
    print("      python3 main.py")
    print("")
    print("  (If you really want to continue here anyway, add --force.")
    print("   Expect real import errors - nothing will be simulated.)")
    print("=" * 62)
    print("")
