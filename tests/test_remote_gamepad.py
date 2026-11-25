"""PC input transport and stop behavior without real motors/cameras."""
import json
import math
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from unittest.mock import patch

from school_pdf.remote_gamepad import RemoteGamepad
from school_pdf.data_collect import Collector, gp


def packet(sequence=1, client='browser-a', connected=True, axes=None, buttons=None):
    return dict(client=client, sequence=sequence, connected=connected,
                axes=[0, 0] if axes is None else axes,
                buttons=[False]*12 if buttons is None else buttons)


class RemoteGamepadTests(unittest.TestCase):
    def test_neutral_and_axes_reach_pad_without_local_device(self):
        c=Collector('pc');pad=gp.Pad(stop=9, step_button=None, phase_button=8)
        with patch('os.read', side_effect=AssertionError('must not read Pi js0')):
            c.remote.accept(packet())
            self.assertFalse(c.poll_input(None,pad))
            self.assertTrue(pad.ready)
            c.remote.accept(packet(2,axes=[1,-1]))
            c.poll_input(None,pad)
            self.assertEqual(pad.request(),{1:-1,2:-1})

    def test_held_input_requires_neutral_before_movement(self):
        c=Collector('pc');pad=gp.Pad(stop=9,step_button=None)
        c.remote.accept(packet(axes=[1,0]));c.poll_input(None,pad)
        self.assertEqual(pad.request(),{})
        c.remote.accept(packet(2));c.poll_input(None,pad)
        c.remote.accept(packet(3,axes=[1,0]));c.poll_input(None,pad)
        self.assertEqual(pad.request(),{1:-1})

    def test_stale_input_clears_pad_and_blocks_motor_guard(self):
        c=Collector('pc');pad=gp.Pad(stop=9,step_button=None)
        c.remote.accept(packet(),now=time.monotonic()-2)
        self.assertTrue(c.poll_input(None,pad))
        self.assertFalse(pad.ready)
        with self.assertRaises(RuntimeError):c.require_input()

    def test_gap_still_stops_after_fresh_packet_arrives(self):
        r=RemoteGamepad();r.accept(packet(),now=10);r.accept(packet(2),now=12)
        s=r.snapshot(now=12)
        self.assertTrue(s['fresh']);self.assertTrue(s['lost'])
        self.assertEqual(s['reason'], 'input_timeout')
        self.assertEqual(s['max_gap_ms'], 2000)

    def test_timeout_reports_age_without_claiming_hardware_disconnect(self):
        r=RemoteGamepad();r.accept(packet(),now=10)
        s=r.snapshot(now=10.8)
        self.assertEqual(s['reason'],'input_timeout')
        self.assertEqual(s['age_ms'],800)

    def test_browser_loss_reason_survives_reconnection_until_consumed(self):
        for reason in ('window_unfocused','tab_hidden','gamepad_missing'):
            with self.subTest(reason=reason):
                r=RemoteGamepad();r.accept(packet(),now=10)
                payload=packet(2,connected=False,axes=[],buttons=[])
                payload['reason']=reason
                r.accept(payload,now=10.1)
                r.accept(packet(3),now=10.2)
                s=r.snapshot(now=10.2)
                self.assertTrue(s['lost'])
                self.assertEqual(s['reason'],reason)
                self.assertIsNone(r.snapshot(now=10.3)['reason'])

    def test_web_stop_blocks_last_moment_motor_guard(self):
        c=Collector('pc');c.remote.accept(packet());c.command='stop'
        with self.assertRaisesRegex(RuntimeError,'화면 정지 요청'):
            c.require_input()

    def test_disconnect_latched_even_after_reconnect(self):
        r=RemoteGamepad();r.accept(packet(),now=10)
        r.accept(packet(2,connected=False,axes=[],buttons=[]),now=10.1)
        r.accept(packet(3),now=10.2)
        self.assertTrue(r.snapshot(now=10.2)['lost'])

    def test_short_stop_press_is_not_overwritten_by_release(self):
        c=Collector('pc');buttons=[False]*12;buttons[9]=True
        c.remote.accept(packet(buttons=buttons));c.remote.accept(packet(2))
        with self.assertRaises(RuntimeError):c.require_input()
        pad=gp.Pad(stop=9,step_button=None);c.poll_input(None,pad)
        self.assertTrue(pad.stopped)

    def test_other_client_cannot_take_over_active_session(self):
        r=RemoteGamepad();r.accept(packet(),now=10)
        with self.assertRaises(PermissionError):r.accept(packet(client='b'),active=True,now=12)
        self.assertEqual(r.client,'browser-a')
        r.accept(packet(client='b'),active=False,now=12)
        self.assertEqual(r.client,'b')

    def test_out_of_order_packet_rejected_without_refreshing_timer(self):
        r=RemoteGamepad();r.accept(packet(2),now=10)
        with self.assertRaises(ValueError):r.accept(packet(1),now=10.5)
        self.assertEqual(r.at,10)

    def test_background_tab_cannot_claim_or_refresh_ownership(self):
        r=RemoteGamepad()
        r.accept(packet(connected=False,axes=[],buttons=[]),now=10)
        self.assertIsNone(r.client)
        r.accept(packet(client='focused'),now=10.1)
        r.accept(packet(2,connected=False,axes=[],buttons=[]),now=10.2)
        self.assertEqual(r.client,'focused')
        self.assertEqual(r.at,10.1)

    def test_focused_tab_can_replace_unfocused_owner_only_when_stopped(self):
        for active in (False,True):
            with self.subTest(active=active):
                r=RemoteGamepad();r.accept(packet(),now=10)
                r.accept(packet(2,connected=False,axes=[],buttons=[]),now=10.1)
                if active:
                    with self.assertRaises(PermissionError):
                        r.accept(packet(client='focused'),active=True,now=10.2)
                else:
                    r.accept(packet(client='focused'),now=10.2)
                    self.assertEqual(r.client,'focused')

    def test_invalid_inputs_rejected(self):
        for values in [dict(axes=[math.nan,0]),dict(axes=[2,0]),dict(buttons=[1]*12),
                       dict(axes=[]),dict(sequence=True),dict(client='')]:
            with self.subTest(values=values):
                with self.assertRaises(ValueError):RemoteGamepad().accept(packet(**values))

    def test_start_without_pc_input_does_not_read_arm(self):
        c=Collector('pc')
        with patch('school_pdf.data_collect.read_angles') as read:
            with self.assertRaises(RuntimeError):c.begin(None,None)
            read.assert_not_called()


class RemoteHTTPTests(unittest.TestCase):
    def setUp(self):
        self.c=Collector('pc')
        self.server=ThreadingHTTPServer(('127.0.0.1',0),self.c.handler())
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.base='http://127.0.0.1:'+str(self.server.server_port)
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown();self.server.server_close();self.thread.join()

    def post(self,path,data=None,key=None,client='browser-a'):
        headers={'X-Control-Key':self.c.key if key is None else key,'X-Gamepad-Client':client}
        return urlopen(Request(self.base+path,data=json.dumps(data).encode() if data is not None else b'',headers=headers,method='POST'),timeout=2)

    def test_input_start_stop_protocol(self):
        self.c.status['ready']=True
        with self.post('/input',packet()):pass
        with self.post('/start'):pass
        self.assertEqual(self.c.command,'start')
        with self.post('/stop'):pass
        with self.post('/start'):pass
        self.assertEqual(self.c.command,'stop')

    def test_unauthorized_and_other_tab_start_rejected(self):
        with self.assertRaises(HTTPError) as error:self.post('/input',packet(),key='bad')
        self.assertEqual(error.exception.code,403)
        with self.post('/input',packet()):pass
        with self.assertRaises(HTTPError) as error:self.post('/start',client='other')
        self.assertEqual(error.exception.code,409)
        self.assertIsNone(self.c.command)

    def test_non_object_input_is_rejected(self):
        with self.assertRaises(HTTPError) as error:self.post('/input',[])
        self.assertEqual(error.exception.code,400)


if __name__=='__main__':unittest.main()
