"""Pi 5 migration checks with fake hardware: I2C bus, camera selection, preflight."""

import argparse
import contextlib
import importlib
import io
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import arm_control
import camera_stream


class FakeSMBus:
    opened = []

    def __init__(self, bus):
        self.bus = bus
        FakeSMBus.opened.append(bus)


def fake_arm_lib():
    """Mimics Yahboom Arm_Lib: Arm_Device() opens smbus.SMBus(1)."""
    smbus = types.ModuleType('smbus')
    smbus.SMBus = FakeSMBus
    lib = types.ModuleType('Arm_Lib')

    class Arm_Device:
        def __init__(self):
            self.addr = 0x15
            self.bus = sys.modules['smbus'].SMBus(1)

    lib.Arm_Device = Arm_Device
    return {'smbus': smbus, 'Arm_Lib': lib}


class OpenArmTest(unittest.TestCase):
    def setUp(self):
        FakeSMBus.opened = []

    def test_default_bus_1(self):
        with patch.dict(sys.modules, fake_arm_lib()), \
                patch.object(arm_control, 'check_i2c_device'):
            arm = arm_control.open_arm()
        self.assertEqual(arm.bus.bus, 1)

    def test_sdk_default_seven_is_overridden_by_selected_bus(self):
        modules = fake_arm_lib()
        class NewSDK:
            def __init__(self, i2c_bus=7):
                self.bus_number = i2c_bus
        modules['Arm_Lib'].Arm_Device = NewSDK
        with patch.dict(sys.modules, modules), \
                patch.object(arm_control, 'check_i2c_device'):
            arm = arm_control.open_arm()
        self.assertEqual(arm.bus_number, arm_control.I2C_BUS)

    def test_override_bus_redirects_only_during_creation(self):
        modules = fake_arm_lib()
        with patch.dict(sys.modules, modules), \
                patch.object(arm_control, 'I2C_BUS', 13), \
                patch.object(arm_control, 'check_i2c_device'):
            arm = arm_control.open_arm()
            self.assertEqual(arm.bus.bus, 13)
            self.assertIs(modules['smbus'].SMBus, FakeSMBus)  # restored

    def test_missing_bus_explains_fix(self):
        with patch.object(arm_control, 'I2C_DEVICE', '/dev/i2c-does-not-exist'):
            with self.assertRaises(RuntimeError) as ctx:
                arm_control.open_arm()
        self.assertIn('raspi-config', str(ctx.exception))

    def test_lock_refuses_before_fuser_when_bus_missing(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch('arm_control.Path.home', return_value=Path(directory)), \
                patch.object(arm_control, 'I2C_DEVICE', '/dev/i2c-does-not-exist'), \
                patch('arm_control.subprocess.run') as run:
            with self.assertRaises(RuntimeError):
                with arm_control.exclusive_arm():
                    self.fail('must not enter without the I2C device')
            run.assert_not_called()

    def test_env_variable_sets_bus(self):
        with patch.dict(os.environ, {'DOFBOT_I2C_BUS': '3'}):
            module = importlib.reload(arm_control)
            self.assertEqual(module.I2C_DEVICE, '/dev/i2c-3')
        importlib.reload(arm_control)


class CameraArgTest(unittest.TestCase):
    def test_index_and_path(self):
        self.assertEqual(camera_stream.camera_arg('0'), 0)
        self.assertEqual(camera_stream.camera_arg('/dev/v4l/by-id/usb-cam-video-index0'),
                         '/dev/v4l/by-id/usb-cam-video-index0')
        with self.assertRaises(argparse.ArgumentTypeError):
            camera_stream.camera_arg('front')

    def test_default_camera_is_video0(self):
        self.assertEqual(camera_stream.device_path(0), '/dev/video0')

    def test_pipeline_node_rejected_before_opening(self):
        fake_cv2 = types.ModuleType('cv2')
        fake_cv2.VideoCapture = lambda *a: self.fail('must not open a pipeline node')
        with patch.dict(sys.modules, {'cv2': fake_cv2}), \
                patch.object(camera_stream, 'device_name', return_value='rp1-cfe-csi2_ch0'):
            with self.assertRaises(RuntimeError) as ctx:
                camera_stream.CameraStream(0, device=0)
        self.assertIn('v4l2-ctl --list-devices', str(ctx.exception))

    def test_scripts_accept_camera_path(self):
        import gamepad_control
        import joystick_control
        for module in (gamepad_control, joystick_control):
            source = Path(module.__file__).read_text()
            self.assertIn('type=camera_arg', source)
            self.assertNotIn('from Arm_Lib import Arm_Device', source)


class PreflightTest(unittest.TestCase):
    def make_pi5_tree(self, root, max_current=3000):
        (root / 'proc/device-tree/chosen/power').mkdir(parents=True)
        (root / 'proc/device-tree/model').write_text('Raspberry Pi 5 Model B Rev 1.0\x00')
        (root / 'proc/device-tree/chosen/power/max_current').write_bytes(
            max_current.to_bytes(4, 'big'))
        (root / 'etc').mkdir()
        (root / 'etc/os-release').write_text(
            'PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\n')
        (root / 'dev/input').mkdir(parents=True)
        for name in ('i2c-1', 'i2c-13', 'video0', 'video1', 'video19', 'input/js0'):
            (root / 'dev' / name).touch()
        nodes = {'video0': ('USB 2.0 Camera: USB Camera', '0'),
                 'video1': ('USB 2.0 Camera: USB Camera', '1'),
                 'video19': ('pispbe-input', '0')}
        for node, (name, index) in nodes.items():
            d = root / 'sys/class/video4linux' / node
            d.mkdir(parents=True)
            (d / 'name').write_text(name + '\n')
            (d / 'index').write_text(index + '\n')

    def run_preflight(self, root):
        with patch.dict(os.environ, {'DOFBOT_PREFLIGHT_ROOT': str(root)}):
            import preflight
            preflight = importlib.reload(preflight)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                preflight.main()
        return out.getvalue()

    def test_pi5_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_pi5_tree(root)
            text = self.run_preflight(root)
        self.assertIn('Raspberry Pi 5', text)
        self.assertIn('bookworm', text)
        self.assertIn('/dev/i2c-1 present', text)
        self.assertIn('/dev/video0  USB 2.0 Camera: USB Camera  [capture -> --camera 0]', text)
        self.assertIn('video1', text)
        self.assertIn('metadata', text)
        self.assertIn('pispbe-input  [Pi camera pipeline', text)
        self.assertIn('3000 mA', text)
        self.assertIn('No motor commands were sent.', text)

    def test_missing_i2c_is_flagged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_pi5_tree(root, max_current=5000)
            (root / 'dev/i2c-1').unlink()
            text = self.run_preflight(root)
        self.assertIn('MISSING', text)
        self.assertIn('5000 mA supply negotiated', text)


if __name__ == '__main__':
    unittest.main()
