"""Only the inspected OLED display may share the arm's I2C bus."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import arm_control as module


class OwnershipTests(unittest.TestCase):
    def result(self, code, output='', error=''):
        return SimpleNamespace(returncode=code, stdout=output, stderr=error)

    def test_empty_bus_needs_no_service_exception(self):
        with patch.object(module.subprocess, 'run', return_value=self.result(1)) as run:
            module.check_i2c_owners()
        self.assertEqual(run.call_count, 1)

    def test_only_verified_active_oled_owner_is_allowed(self):
        service='ActiveState=active\nControlGroup=/system.slice/yahboom_oled.service\n'
        for verified in (True, False):
            with self.subTest(verified=verified), \
                 patch.object(module.subprocess, 'run', side_effect=[
                     self.result(0, '123', module.I2C_DEVICE+':'), self.result(0, service)]), \
                 patch.object(module, '_verified_oled_pid', return_value=verified):
                if verified:
                    module.check_i2c_owners()
                else:
                    with self.assertRaises(RuntimeError): module.check_i2c_owners()

    def test_permission_error_and_malformed_pids_fail_closed(self):
        for result in (self.result(1, error='sudo denied'), self.result(0, '123x'),
                       self.result(0, ''), self.result(2)):
            with self.subTest(result=result), patch.object(module.subprocess, 'run', return_value=result):
                with self.assertRaises(RuntimeError): module.check_i2c_owners()

    def test_oled_identity_requires_exact_script_and_service_cgroup(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);proc=root/'123';proc.mkdir()
            def path(*args):
                return root if args == ('/proc',) else Path(*args)
            for script,group,expected in (
                    ('/home/pi/oled_yahboom/yahboom_oled.py','/system.slice/yahboom_oled.service',True),
                    ('/tmp/yahboom_oled.py','/system.slice/yahboom_oled.service',False),
                    ('/home/pi/oled_yahboom/yahboom_oled.py','/user.slice/other',False)):
                (proc/'cmdline').write_bytes(b'python3\0'+script.encode()+b'\0')
                (proc/'cgroup').write_text('0::'+group+'\n')
                with self.subTest(script=script,group=group), patch.object(module,'Path',side_effect=path):
                    self.assertEqual(module._verified_oled_pid('123','/system.slice/yahboom_oled.service'),expected)


if __name__ == '__main__':
    unittest.main()
