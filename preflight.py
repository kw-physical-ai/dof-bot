#!/usr/bin/env python3
"""Read-only checks to run on the Dofbot Raspberry Pi after OS installation."""

import importlib.util
import platform
from pathlib import Path


def show(label: str, value: object) -> None:
    print(f"{label}: {value}")


def paths(pattern: str) -> str:
    matches = sorted(str(path) for path in Path("/dev").glob(pattern))
    return ", ".join(matches) if matches else "not found"


def main() -> None:
    show("Python", platform.python_version())
    show("System", platform.platform())
    show("Arm_Lib", "available" if importlib.util.find_spec("Arm_Lib") else "not found")
    show("Camera devices", paths("video*"))
    show("Joystick devices", paths("input/js*"))
    show("I2C devices", paths("i2c-*"))
    show("Vendor serial device", "found" if Path("/dev/myserial").exists() else "not found")
    print("No motor commands were sent.")


if __name__ == "__main__":
    main()
