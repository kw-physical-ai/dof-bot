#!/usr/bin/env python3
"""School-style 3 FPS target-angle recording with a Pi or PC browser gamepad and USB+CSI."""
import argparse
import csv
import json
import os
from pathlib import Path
import secrets
import select
import signal
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cv2
import gamepad_control as gp
from arm_control import exclusive_arm, open_arm, read_angles
from camera_stream import CameraStream
from school_pdf.remote_gamepad import RemoteGamepad

HOME = [92, 77, 11, 18, 99, 91]
ROOT = Path(__file__).resolve().parent / 'datasets'
gp.DEADZONE = 3277  # PDF: approximately 0.1 of the stick range.
PAGE = '''<!doctype html><meta charset="utf-8"><title>Dofbot 수집</title>
<style>body{font:18px system-ui;max-width:1100px;margin:24px auto;padding:0 16px;background:#101820;color:#eef}button{font:inherit;padding:14px;margin:8px 8px 8px 0;cursor:pointer}main{display:flex;gap:12px}section{width:50%}img{width:100%}pre{white-space:pre-wrap} .note{color:#bcd}</style>
<h1>Dofbot · 실제 데이터 수집</h1><main><section><h2>손목 USB · cam1</h2><img id="cam0"></section><section><h2>외부 CSI · cam2</h2><img id="cam1"></section></main>
<button onclick="send('start')">수집 시작 · 현재 자세</button><button onclick="send('stop')">정지 / 저장 종료</button>
<p>입력 모드는 아래 상태에 표시됩니다. PC 모드에서는 이 창을 활성화한 채 PC 게임패드로 조작합니다. 시작 후 스틱과 버튼을 한번 모두 놓으세요.</p>
<p>왼쪽 스틱: 1·2축 / L1·L2: 3축 / R1·R2: 4축 / 버튼 2·4: 5축 / 버튼 1·3: 집게</p>
<p><b>START(9): 정지 / SELECT(8): PDF 초기 자세로 이동</b></p>
<p class="note">3 FPS · 244×244 · 목표각 CSV. 두 카메라는 동시 노출이 아닌 최근 수신 프레임입니다.<br>PC 모드는 입력이 0.75초 끊기면 새 명령과 수집을 중단합니다. 재연결 후 수집 시작을 다시 누르세요. 정지는 START(9) 또는 화면의 정지 버튼입니다. 실제 물체를 집는 성공 여부는 직접 확인하세요.</p><p id="pad-info"></p><pre id="status">연결 중</pre>
<script>const key='__KEY__', inputMode='__INPUT__', client=crypto.randomUUID();async function send(action){try{const r=await fetch('/'+action,{method:'POST',headers:{'X-Control-Key':key,'X-Gamepad-Client':client}});if(!r.ok)alert(await r.text())}catch(e){alert(e)}}
let pending=false;setInterval(async()=>{if(pending)return;pending=true;try{const r=await fetch('/status',{headers:{'X-Control-Key':key}});const s=await r.json();document.getElementById('status').textContent=JSON.stringify(s,null,2);for(let i=0;i<(s.camera_count||1);i++)document.getElementById('cam'+i).src='/cam/'+i+'?t='+Date.now()}catch(e){document.getElementById('status').textContent='연결 끊김: 다시 연결하세요.'}finally{pending=false}},500);
let inputBusy=false, sequence=0;
async function sendPad(){
 if(inputMode!=='pc'||inputBusy)return;
 inputBusy=true;
 try{
  if(!window.isSecureContext||!navigator.getGamepads)throw Error('localhost 주소에서 Chrome/Edge로 열어주세요');
  const pads=Array.from(navigator.getGamepads()).filter(p=>p&&p.connected);
  const pad=pads[0], visible=document.visibilityState==='visible'&&document.hasFocus();
  const connected=!!pad&&visible;
  document.getElementById('pad-info').textContent=pad?pad.id+' · 축 '+pad.axes.length+' / 버튼 '+pad.buttons.length+(visible?'':' · 창 비활성: 정지'):'게임패드를 PC에 연결하고 버튼을 눌러주세요';
  const r=await fetch('/input',{method:'POST',headers:{'X-Control-Key':key,'Content-Type':'application/json'},signal:AbortSignal.timeout(500),body:JSON.stringify({client,sequence:++sequence,connected,axes:connected?Array.from(pad.axes):[],buttons:connected?pad.buttons.map(b=>b.pressed):[]})});
  if(!r.ok)throw Error(await r.text());
 }catch(e){document.getElementById('pad-info').textContent=String(e)}finally{inputBusy=false}
}
setInterval(sendPad,50);
window.addEventListener('blur',sendPad);
document.addEventListener('visibilitychange',sendPad);
</script>'''


class Collector:
    def __init__(self, input_mode='pi'):
        self.input_mode = input_mode
        self.remote = RemoteGamepad()
        self.lock = threading.Lock()
        self.frames = {}
        self.camera_count = 2
        self.active = False
        self.command = None
        self.status = {'state': '초기화 중', 'recording': False, 'ready': False}
        self.quit = threading.Event()
        self.key = secrets.token_urlsafe(24)
        self.file = None
        self.count = 0
        self.path = None

    def frame(self, index, at, frame):
        with self.lock:
            self.frames[index] = (at, frame.copy())

    def finish(self, reason):
        self.active = False
        if self.file:
            self.file.close()
            self.file = None
            (self.path / 'meta.json').write_text(json.dumps(dict(
                reason=reason, frames=self.count, input_mode=self.input_mode, cameras=['USB', 'OV5647'][:self.camera_count],
                csi_excluded=self.camera_count == 1,
                angles='command targets, not measured joint positions',
                synchronization='latest frames; host monotonic receive times in frames.csv',
                result='unreviewed'), ensure_ascii=False, indent=2))
        if getattr(self, 'times_file', None):
            self.times_file.close()
            self.times_file = None
        self.status.update(state=reason, recording=False, frames=self.count)

    def begin(self, arm, pad):
        if self.active:
            return
        if self.input_mode == 'pc' and not self.remote.is_fresh():
            raise RuntimeError('PC 게임패드를 연결하고 수집 화면을 활성화하세요')
        with self.lock:
            fresh = len(self.frames) == self.camera_count and all(time.monotonic()-x[0] < 1 for x in self.frames.values())
        if not fresh:
            raise RuntimeError('두 카메라의 최신 프레임이 필요합니다')
        angles = read_angles(arm)
        if any(not 0 <= angles[i] <= 180 for i in range(1,7)):
            raise RuntimeError('학교 코드의 0~180도 범위 밖 관절이 있습니다')
        self.angles = angles
        self.path = ROOT / datetime.now().strftime('dataset_%Y%m%d_%H%M%S_%f')
        for name in ['cam1', 'cam2'][:self.camera_count]:
            (self.path/name).mkdir(parents=True, exist_ok=True)
        self.file = (self.path/'labels.csv').open('w', newline='')
        self.writer = csv.writer(self.file)
        self.writer.writerow(['frame_id','timestamp','m1','m2','m3','m4','m5','m6'])
        self.times_file = (self.path/'frames.csv').open('w', newline='')
        self.times = csv.writer(self.times_file)
        self.times.writerow(['frame_id']+[f'cam{i+1}_receive_monotonic' for i in range(self.camera_count)])
        self.count = 0
        self.next_frame = self.next_move = 0
        self.next_read = time.monotonic()+1
        self.last_ids = None
        pad.ready = False
        pad.stopped = False
        pad.phase_requested = False
        self.active = True
        self.status.update(state='수집 중 · 게임패드 입력 대기', recording=True, folder=str(self.path))

    def save(self):
        with self.lock:
            frames = dict(self.frames)
        now = time.monotonic()
        if len(frames) != self.camera_count or any(now-at > 1 for at,_ in frames.values()):
            raise RuntimeError('카메라 프레임 수신이 끊겼습니다')
        ids = tuple(frames[i][0] for i in range(self.camera_count))
        if self.last_ids and any(a == b for a,b in zip(ids,self.last_ids)):
            return
        number = self.count+1
        for i in range(self.camera_count):
            img = cv2.resize(frames[i][1], (244,244))
            if not cv2.imwrite(str(self.path/f'cam{i+1}'/f'{number:05d}.jpg'),img,[cv2.IMWRITE_JPEG_QUALITY,85]):
                raise RuntimeError('이미지 저장 실패')
        self.writer.writerow([number,datetime.now().strftime('%H%M%S.%f')[:-3]]+[self.angles[i] for i in range(1,7)])
        self.times.writerow([number,*ids])
        self.file.flush()
        self.times_file.flush()
        self.count = number
        self.last_ids = ids
        self.status.update(frames=self.count, targets=self.angles)

    def handler(self):
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def reply(self,body,kind='application/json',code=200):
                self.send_response(code)
                self.send_header('Content-Type',kind)
                self.send_header('Cache-Control','no-store')
                self.send_header('Content-Length',str(len(body)))
                self.end_headers()
                try:self.wfile.write(body)
                except (BrokenPipeError,ConnectionResetError):pass
            def do_GET(self):
                path = self.path.split('?')[0]
                if path == '/':
                    self.reply(PAGE.replace('__KEY__',owner.key).replace('__INPUT__',owner.input_mode).encode(),'text/html; charset=utf-8')
                elif path == '/status':
                    self.reply(json.dumps(owner.status,ensure_ascii=False).encode())
                elif path in ['/cam/0','/cam/1']:
                    with owner.lock: frame = owner.frames.get(int(path[-1]))
                    if frame:
                        ok,b = cv2.imencode('.jpg',frame[1])
                        self.reply(b.tobytes(),'image/jpeg') if ok else self.send_error(503)
                    else:self.send_error(503)
                else:self.send_error(404)
            def do_POST(self):
                if self.headers.get('X-Control-Key') != owner.key:
                    self.send_error(403);return
                if self.path == '/input' and owner.input_mode == 'pc':
                    try:
                        length = int(self.headers.get('Content-Length', '0'))
                        if not 0 < length <= 4096: raise ValueError('Invalid packet size')
                        self.connection.settimeout(1)
                        payload = json.loads(self.rfile.read(length))
                        if not isinstance(payload, dict): raise ValueError('Expected object')
                        owner.remote.accept(payload, active=owner.active)
                    except PermissionError as exc:
                        self.reply(str(exc).encode(), code=409);return
                    except (ValueError, OSError) as exc:
                        self.reply(str(exc).encode(), code=400);return
                    self.reply(b'{}');return
                if self.path == '/start' and owner.input_mode == 'pc':
                    if self.headers.get('X-Gamepad-Client') != owner.remote.client or not owner.remote.is_fresh():
                        self.reply('이 브라우저의 게임패드 입력을 먼저 확인하세요'.encode(),code=409);return
                if self.path == '/start' and not owner.status.get('ready'):
                    self.reply('카메라 준비가 완료되지 않았습니다'.encode(),code=503);return
                if self.path not in ['/start','/stop']:
                    self.send_error(404);return
                with owner.lock:
                    # Stop wins until the control loop consumes it.
                    if self.path == '/stop' or owner.command != 'stop':
                        owner.command = self.path[1:]
                self.reply(b'{}')
        return Handler

    def require_input(self):
        if self.input_mode == 'pc' and not self.remote.can_move():
            raise RuntimeError('PC 입력 끊김/정지 요청 · 정지')

    def poll_input(self, fd, pad):
        if self.input_mode == 'pi':
            gp.drain(fd, pad)
            return False
        packet = self.remote.snapshot()
        self.status['gamepad_connected'] = packet['fresh']
        self.status['gamepad_axes'] = packet['axes']
        self.status['gamepad_buttons'] = packet['buttons']
        if packet['lost']:
            pad.axes.clear()
            pad.buttons.clear()
            pad.ready = False
            pad.phase_requested = False
        if packet['fresh']:
            for number, value in enumerate(packet['axes']):
                pad.event(gp.AXIS, number, round(value*32767))
            for number, value in enumerate(packet['buttons']):
                pad.event(gp.BUTTON, number, int(value))
            pad.settle()
        pad.stopped |= packet['stop']
        return packet['lost']

    def run(self):
        usb = sorted(Path('/dev/v4l/by-id').glob('*video-index0'))
        if len(usb) != 1:
            raise RuntimeError('USB 카메라 하나가 필요합니다: /dev/v4l/by-id')
        fd = None
        stream = None
        server = ThreadingHTTPServer(('127.0.0.1',8081),self.handler())
        threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            if self.input_mode == 'pi':
                fd = os.open('/dev/input/js0',os.O_RDONLY|os.O_NONBLOCK)
            self.status.update(state='USB 카메라 준비 중',csi='OV5647 / csi0')
            with exclusive_arm():
                arm = open_arm()
                stream = CameraStream(0,bind='127.0.0.1',device=[str(usb[0]),'csi0'],fps=10)
                for i,source in enumerate(stream.sources):
                    source.set_frame_sink(lambda fid,at,frame,i=i:self.frame(i,at,frame))
                pad = gp.Pad(stop=9,step_button=None,steps=(1,),phase_button=8)
                self.status.update(ready=True,state='준비 완료 · 수집 시작을 누르세요',usb=str(usb[0]),camera_count=self.camera_count,input_mode=self.input_mode)
                print('READY http://localhost:8081 (preview only until Start)',flush=True)
                while not self.quit.is_set():
                    if fd is not None:select.select([fd],[],[],.02)
                    else:self.quit.wait(.02)
                    input_lost = self.poll_input(fd,pad)
                    with self.lock:
                        command,self.command = self.command,None
                    if pad.stopped or command == 'stop':
                        self.finish('정지됨 · 저장 완료')
                        pad.stopped = False
                        pad.ready = False
                        continue
                    try:
                        if input_lost and self.active:
                            raise RuntimeError('PC 게임패드 연결 끊김/창 비활성화 · 다시 수집 시작을 누르세요')
                        if stream.error: raise RuntimeError(stream.error)
                        if command == 'start':self.begin(arm,pad)
                        if not self.active:
                            pad.phase_requested = False
                            continue
                        now=time.monotonic()
                        # Read feedback periodically to detect all-joint communication loss.
                        if now >= self.next_read:
                            measured=read_angles(arm)
                            self.status['measured']=measured
                            self.next_read=now+1
                            input_lost = self.poll_input(fd,pad)
                            if input_lost:raise RuntimeError('PC 게임패드 연결 끊김 · 정지')
                            if pad.stopped:continue
                        if pad.phase_requested:
                            pad.phase_requested=False
                            if pad.ready:
                                self.require_input()
                                arm.Arm_serial_servo_write6(*HOME,2000)
                                self.angles=dict(enumerate(HOME,1))
                                self.next_move=now+2.5
                                pad.ready=False
                        if now >= self.next_move and pad.ready:
                            for servo,direction in pad.request().items():
                                self.angles[servo]=max(0,min(180,self.angles[servo]+direction))
                                self.require_input()
                                arm.Arm_serial_servo_write(servo,self.angles[servo],800)
                            self.next_move=now+.15
                        if now >= self.next_frame:
                            self.save()
                            self.next_frame=now+1/3
                    except Exception as exc:
                        self.finish('오류: '+str(exc))
                        pad.ready=False
        except Exception as exc:
            self.status['ready']=False
            self.finish('시작 실패: '+str(exc))
            print(self.status['state'],flush=True)
            self.quit.wait()
        finally:
            self.finish('프로그램 종료')
            if server:server.shutdown();server.server_close()
            if stream:stream.close()
            if fd is not None:os.close(fd)

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',choices=['pi','pc'],default='pi',help='gamepad attached to Pi or browser PC')
    collector=Collector(input_mode=parser.parse_args().input)
    for sig in (signal.SIGINT,signal.SIGTERM):
        signal.signal(sig,lambda *_:collector.quit.set())
    collector.run()
