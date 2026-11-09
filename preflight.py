#!/usr/bin/env python3
"""Read-only checks to run on the Dofbot Raspberry Pi after OS installation."""

import importlib.util
import os
import platform
import shutil
import sys
from pathlib import Path

ROOT = Path(os.environ.get("DOFBOT_PREFLIGHT_ROOT", "/"))  # tests point this at a fake tree
I2C_BUS = int(os.environ.get("DOFBOT_I2C_BUS", "1"))


def show(label: str, value: object) -> None:
    print(f"{label}: {value}")


def read(path: str) -> str:
    try:
        return (ROOT / path.lstrip("/")).read_text(errors="replace").replace("\x00", "").strip()
    except OSError:
        return ""


def paths(pattern: str) -> str:
    matches = sorted(str(Path("/dev") / path.relative_to(ROOT / "dev"))
                     for path in (ROOT / "dev").glob(pattern))
    return ", ".join(matches) if matches else "not found"


def os_name() -> str:
    for line in read("/etc/os-release").splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip('"')
    return "unknown"


def cameras() -> list:
    """Video nodes with their kernel names. Pi 5 adds many non-camera nodes."""
    rows = []
    base = ROOT / "sys/class/video4linux"
    nodes = sorted(base.glob("video*"), key=lambda p: int(p.name[5:] or 0)) if base.exists() else []
    for node in nodes:
        name = read(f"/sys/class/video4linux/{node.name}/name") or "?"
        index = read(f"/sys/class/video4linux/{node.name}/index")
        if name.startswith(("rp1-cfe", "pispbe", "rpi-hevc", "bcm2835-codec", "bcm2835-isp")):
            role = "Pi camera pipeline / codec (not for --camera)"
        elif index not in ("", "0"):
            role = "metadata of the camera above (not for --camera)"
        else:
            role = "capture -> --camera {}".format(node.name[5:])
        rows.append("/dev/{}  {}  [{}]".format(node.name, name, role))
    return rows


def power_note(model: str) -> str:
    if "Raspberry Pi 5" not in model:
        return "n/a"
    raw = (ROOT / "proc/device-tree/chosen/power/max_current")
    try:
        milliamps = int.from_bytes(raw.read_bytes()[:4], "big")
    except OSError:
        return "unknown (check: vcgencmd get_throttled; 0x0 means no undervoltage)"
    if milliamps >= 5000:
        return f"{milliamps} mA supply negotiated"
    return (f"{milliamps} mA: USB ports may be limited to 600 mA total. Camera and "
            "gamepad can drop out; use a 5V/5A supply (see README Pi 5 section)")


def main() -> None:
    model = read("/proc/device-tree/model") or platform.machine()
    show("Board", model)
    show("OS", os_name())
    show("Kernel", platform.release())
    show("Python", f"{platform.python_version()} ({sys.executable})")
    show("Virtualenv", "yes" if sys.prefix != sys.base_prefix else "no")
    show("Arm_Lib", "available" if importlib.util.find_spec("Arm_Lib") else "not found")
    smbus = [name for name in ("smbus", "smbus2") if importlib.util.find_spec(name)]
    show("SMBus module", ", ".join(smbus) if smbus else "not found (sudo apt install python3-smbus)")
    show("OpenCV", "available" if importlib.util.find_spec("cv2") else "not found (sudo apt install python3-opencv)")
    show("I2C devices", paths("i2c-*"))
    bus = f"/dev/i2c-{I2C_BUS}"
    show("Arm I2C bus", f"{bus} " + ("present" if (ROOT / bus.lstrip("/")).exists()
                                     else "MISSING (sudo raspi-config nonint do_i2c 0, reboot)"))
    show("fuser", "available" if shutil.which("fuser") else "not found (sudo apt install psmisc)")
    print("Camera devices:")
    for row in cameras() or ["  not found"]:
        print("  " + row)
    show("Joystick devices", paths("input/js*"))
    show("Vendor serial device", "found" if (ROOT / "dev/myserial").exists() else "not found")
    show("Pi 5 power", power_note(model))
    print("No motor commands were sent.")


if __name__ == "__main__":
    main()
