"""Validated browser gamepad mailbox; no hardware access."""
import math
import threading
import time


class RemoteGamepad:
    timeout = 0.75

    def __init__(self):
        self.lock = threading.Lock()
        self.client = None
        self.sequence = -1
        self.at = None
        self.axes = []
        self.buttons = []
        self.connected = False
        self.interrupted = False
        self.stop_requested = False

    def accept(self, payload, active=False, now=None):
        now = time.monotonic() if now is None else now
        client, seq = payload.get('client'), payload.get('sequence')
        connected = payload.get('connected')
        axes, buttons = payload.get('axes'), payload.get('buttons')
        if (not isinstance(client, str) or not 1 <= len(client) <= 80
                or type(seq) is not int or seq < 0 or type(connected) is not bool
                or not isinstance(axes, list) or not isinstance(buttons, list)
                or len(axes) > 32 or len(buttons) > 64
                or any(type(x) not in (int, float) or not math.isfinite(x) or not -1 <= x <= 1 for x in axes)
                or any(type(x) is not bool for x in buttons)
                or (connected and (len(axes) < 2 or len(buttons) < 10))):
            raise ValueError('Invalid gamepad packet')
        with self.lock:
            fresh = self.at is not None and now-self.at <= self.timeout
            if self.client != client and self.client is not None and (fresh or active):
                raise PermissionError('Another browser owns the gamepad; close it and stop collection first')
            if self.client == client and seq <= self.sequence:
                raise ValueError('Old gamepad packet')
            if self.at is not None and not fresh:
                self.interrupted = True
            self.client, self.sequence, self.at = client, seq, now
            self.axes, self.buttons, self.connected = list(axes), list(buttons), connected
            self.interrupted |= not connected
            self.stop_requested |= connected and buttons[9]

    def snapshot(self, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            fresh = self.connected and self.at is not None and now-self.at <= self.timeout
            lost = self.interrupted or not fresh
            stop = self.stop_requested
            self.interrupted = self.stop_requested = False
            return dict(axes=list(self.axes), buttons=list(self.buttons), fresh=fresh,
                        lost=lost, stop=stop, client=self.client)

    def is_fresh(self):
        with self.lock:
            return self.connected and self.at is not None and time.monotonic()-self.at <= self.timeout

    def can_move(self):
        with self.lock:
            return (self.connected and self.at is not None
                    and time.monotonic()-self.at <= self.timeout
                    and not self.interrupted and not self.stop_requested)
