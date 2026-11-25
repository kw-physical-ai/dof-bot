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
from arm_control import exclusive_arm, open_arm, outside_angle, read_angles
from camera_stream import CameraStream
from school_pdf.remote_gamepad import RemoteGamepad

# Startup pose in the installed vendor APP_DOFBOT/YahboomArm.py __main__.
# SDK command angles, not signed offsets. Change only after checking the real pose.
HOME = [90, 150, 0, 0, 90, 30]
# Startup only: restore an encoder-confirmed joint to the adjacent SDK boundary.
# Normal gamepad movement still uses the SDK limits and narrow +/-3 recovery.
HOME_RECOVERY_MARGIN = 45
LIMITS = {servo: (0, 270 if servo == 5 else 180) for servo in range(1, 7)}
MANUAL_STEP = 1.5
MANUAL_MOVE_MS = 533
ROOT = Path(__file__).resolve().parent / 'datasets'
gp.DEADZONE = 3277  # PDF: approximately 0.1 of the stick range.
PAGE = '''<!doctype html><meta charset="utf-8"><title>Dofbot 수집</title>
<style>body{font:18px system-ui;max-width:1100px;margin:24px auto;padding:0 16px;background:#101820;color:#eef}button{font:inherit;padding:14px;margin:8px 8px 8px 0;cursor:pointer}main{display:flex;gap:12px}section{width:50%}img{width:100%}pre{white-space:pre-wrap} .note{color:#bcd}</style>
<h1>Dofbot · 실제 데이터 수집</h1><main><section><h2>손목 USB · cam1</h2><img id="cam0"></section><section><h2>외부 CSI · cam2</h2><img id="cam1"></section></main>
<button onclick="send('home')">기본자세 복귀 / 재시도</button><button onclick="send('start')">기본자세 확인 후 수집 시작</button><button onclick="send('stop')">정지 / 저장 종료</button>
<p>입력 모드는 아래 상태에 표시됩니다. PC 모드에서는 이 창을 활성화한 채 PC 게임패드로 조작합니다. 시작 후 스틱과 버튼을 한번 모두 놓으세요.</p>
<p>왼쪽 스틱: 1·2축 / L1·L2: 3축 / R1·R2: 4축 / 버튼 2·4: 5축 / 버튼 1·3: 집게</p>
<p><b>START(9): 정지 / SELECT(8): 기본자세 __HOME__로 이동</b></p>
<p>실행 시 기본자세로 이동합니다. 수집 시작 전 전체 관절의 기본자세 도달 확인이 필요합니다. 초기화 실패 시 전원·배선을 확인한 뒤 복귀 / 재시도를 누르세요.</p>
<p>조작 범위: 1·2·3·4·6번 0~180°, 5번 0~270°. 한 관절의 한계/응답 오류는 해당 관절만 차단합니다. 반대 방향과 다른 관절은 계속 조작할 수 있습니다. 전체 응답·입력·영상이 끊기면 수집을 정지합니다.</p>
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
  const reason=!pad?'gamepad_missing':document.visibilityState!=='visible'?'tab_hidden':!document.hasFocus()?'window_unfocused':'connected';
  const connected=reason==='connected';
  document.getElementById('pad-info').textContent=pad?pad.id+' · 축 '+pad.axes.length+' / 버튼 '+pad.buttons.length+(visible?'':' · 창 비활성: 정지'):'게임패드를 PC에 연결하고 버튼을 눌러주세요';
  const r=await fetch('/input',{method:'POST',headers:{'X-Control-Key':key,'Content-Type':'application/json'},signal:AbortSignal.timeout(500),body:JSON.stringify({client,sequence:++sequence,connected,reason,axes:connected?Array.from(pad.axes):[],buttons:connected?pad.buttons.map(b=>b.pressed):[]})});
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
        self.homed = False
        self.remote = RemoteGamepad()
        self.lock = threading.Lock()
        self.frames = {}
        self.camera_count = 2
        self.active = False
        self.command = None
        self.status = {'state': '초기화 중', 'recording': False, 'ready': False,
                       'home': HOME, 'home_verified': False, 'limits': LIMITS,
                       'manual_step': MANUAL_STEP, 'manual_move_ms': MANUAL_MOVE_MS}
        self.quit = threading.Event()
        self.key = secrets.token_urlsafe(24)
        self.file = None
        self.count = 0
        self.path = None

    def frame(self, index, at, frame):
        with self.lock:
            self.frames[index] = (at, frame.copy())

    def finish(self, reason):
        if self.active:
            self.homed = False
            self.status['home_verified'] = False
        self.active = False
        if self.file:
            self.file.close()
            self.file = None
            (self.path / 'meta.json').write_text(json.dumps(dict(
                reason=reason, frames=self.count, input_mode=self.input_mode, cameras=['USB', 'OV5647'][:self.camera_count],
                csi_excluded=self.camera_count == 1,
                angles='command targets, not measured joint positions',
                limits=LIMITS, manual_step=MANUAL_STEP, manual_move_ms=MANUAL_MOVE_MS,
                home=HOME, missing_feedback='block affected joint; stop if all unavailable',
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
        # Always verify the real pose; an idle arm may have been repositioned.
        measured = self.checked_angles(arm, for_home=True)
        if not self.homed or any(abs(measured[s]-HOME[s-1]) > 3 for s in LIMITS):
            self.initialize_pose(arm)
            self.require_input()
        with self.lock:
            fresh = len(self.frames) == self.camera_count and all(time.monotonic()-x[0] < 1 for x in self.frames.values())
        if not fresh:
            raise RuntimeError('기본자세 복귀 후 두 카메라의 최신 프레임이 필요합니다')
        self.checked_angles(arm)
        self.angles = dict(enumerate(HOME, 1))
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
                    self.reply(PAGE.replace('__KEY__',owner.key).replace('__INPUT__',owner.input_mode).replace('__HOME__',json.dumps(HOME)).encode(),'text/html; charset=utf-8')
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
                if self.path in ['/start','/home'] and owner.input_mode == 'pc':
                    if self.headers.get('X-Gamepad-Client') != owner.remote.client or not owner.remote.is_fresh():
                        self.reply('이 브라우저의 게임패드 입력을 먼저 확인하세요'.encode(),code=409);return
                if self.path in ['/start','/home'] and not owner.status.get('ready'):
                    self.reply('카메라 준비가 완료되지 않았습니다'.encode(),code=503);return
                if self.path not in ['/start','/stop','/home']:
                    self.send_error(404);return
                with owner.lock:
                    # Stop wins until the control loop consumes it.
                    if self.path == '/stop' or owner.command != 'stop':
                        owner.command = self.path[1:]
                self.reply(b'{}')
        return Handler

    def require_input(self):
        with self.lock:
            if self.command == 'stop':
                raise RuntimeError('화면 정지 요청 · 정지')
        if self.input_mode == 'pc' and not self.remote.can_move():
            raise RuntimeError('PC 입력 끊김/정지 요청 · 정지')

    def checked_angles(self, arm, partial=False, for_home=False):
        self.status['feedback_valid'] = False
        try:
            angles = read_angles(arm, recover=True, partial=partial or for_home)
            recovery = {}
            if for_home:
                for servo, angle in angles.items():
                    if angle is not None:
                        continue
                    if arm.Arm_ping_servo(servo) != 0xDA:
                        continue
                    estimate = outside_angle(arm, servo)
                    low, high = LIMITS[servo]
                    target = HOME[servo-1]
                    if estimate is not None and (
                            low-HOME_RECOVERY_MARGIN <= estimate < low and target == low
                            or high < estimate <= high+HOME_RECOVERY_MARGIN and target == high):
                        angles[servo] = estimate
                        recovery[servo] = estimate
                self.status['startup_boundary_recovery'] = recovery
            unavailable = [servo for servo, angle in angles.items() if angle is None]
            if len(unavailable) == 6:
                raise RuntimeError('모든 관절의 각도 응답 없음 · 정지')
            if unavailable and not partial:
                raise RuntimeError('기본자세 확인에 전체 관절 응답 필요: '+str(unavailable))
        except Exception as exc:
            self.status['feedback_error'] = str(exc)
            raise
        self.status.update(measured=angles, feedback_valid=not unavailable,
                           feedback_error=('각도 응답 없음: '+str(unavailable)) if unavailable else None,
                           blocked_joints={servo: '각도 응답 없음' for servo in unavailable},
                           limits=LIMITS)
        return angles

    def move(self, arm, pad, fd, home=False):
        # Read every joint before any write, including SELECT's whole-arm move.
        # Missing feedback is never replaced by the previous target or by zero.
        measured = self.checked_angles(arm, partial=True)
        lost = self.poll_input(fd, pad)
        if lost or pad.stopped or self.quit.is_set():
            raise RuntimeError('이동 전 입력/종료 상태 변경 · 정지')
        self.require_input()
        if not pad.ready:
            return False
        # A session may start without one joint. Initialize its target only
        # after real feedback returns; never guess a missing starting angle.
        for servo, angle in measured.items():
            if angle is not None and self.angles.get(servo) is None:
                self.angles[servo] = angle
        if home:
            if all(angle is not None for angle in measured.values()):
                arm.Arm_serial_servo_write6(*HOME,2000)
                self.angles = dict(enumerate(HOME,1))
            else:
                for servo, angle in measured.items():
                    if angle is None:
                        continue
                    self.require_input()
                    arm.Arm_serial_servo_write(servo, HOME[servo-1], 2000)
                    self.angles[servo] = HOME[servo-1]
        else:
            available = {servo: (self.angles[servo] if measured[servo] is not None else None)
                         for servo in LIMITS}
            targets = gp.plan(available, pad.request(), MANUAL_STEP, LIMITS)
            for servo, direction in pad.request().items():
                angle = measured[servo]
                if angle is None:
                    continue
                low, high = LIMITS[servo]
                if (angle >= high and direction > 0) or (angle <= low and direction < 0) or servo not in targets:
                    self.status['blocked_joints'][servo] = '범위 끝 · 반대 방향으로 조작하세요'
                    continue
                target = targets[servo]
                self.require_input()
                arm.Arm_serial_servo_write(servo,target,MANUAL_MOVE_MS)
                self.angles[servo] = target
        return True

    def poll_input(self, fd, pad):
        if self.input_mode == 'pi':
            gp.drain(fd, pad)
            return False
        packet = self.remote.snapshot()
        self.status['gamepad_connected'] = packet['fresh']
        self.status['gamepad_axes'] = packet['axes']
        self.status['gamepad_buttons'] = packet['buttons']
        reasons = {'input_timeout': '입력이 750ms 이상 도착하지 않음',
                   'window_unfocused': '수집 창이 키보드 포커스를 잃음',
                   'tab_hidden': '수집 탭이 숨겨짐',
                   'gamepad_missing': '브라우저에서 게임패드가 검출되지 않음'}
        if packet['lost']:
            self.status['last_input_loss'] = reasons.get(packet['reason'], packet['reason'])
            self.status['last_input_loss_at'] = datetime.now().isoformat(timespec='milliseconds')
        self.status['input_age_ms'] = packet['age_ms']
        self.status['input_max_gap_ms'] = packet['max_gap_ms']
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

    def initialize_pose(self, arm):
        self.homed = False
        self.status['home_verified'] = False
        if (len(HOME) != 6 or any(isinstance(angle, bool) or not isinstance(angle,(int,float))
                                or not LIMITS[servo][0] <= angle <= LIMITS[servo][1]
                                for servo,angle in enumerate(HOME,1))):
            raise RuntimeError('HOME은 6개 SDK 각도여야 합니다: 5번 0~270°, 나머지 0~180°')
        self.status['state'] = '시작 기본자세 준비 · 관절 응답 확인 중'
        measured = self.checked_angles(arm, for_home=True)
        self.angles = measured.copy()
        with self.lock:
            stopped = self.command == 'stop'
        if self.quit.is_set() or stopped:
            raise RuntimeError('시작 기본자세 이동 취소')
        arm.Arm_serial_servo_write6(*HOME, 2000)
        self.angles = dict(enumerate(HOME, 1))
        if self.quit.wait(2):
            raise RuntimeError('시작 기본자세 이동 중 종료')
        measured = self.checked_angles(arm)
        off_pose = {servo: angle for servo, angle in measured.items()
                    if abs(angle-HOME[servo-1]) > 3}
        if off_pose:
            raise RuntimeError('기본자세 도달 확인 실패 · 수집 차단: '+str(off_pose))
        self.homed = True
        self.status['home_verified'] = True

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
                try:
                    self.initialize_pose(arm)
                    startup_state = '준비 완료 · 수집 시작을 누르세요'
                except RuntimeError as exc:
                    startup_state = '기본자세 미완료 · 복귀 / 재시도 필요: '+str(exc)
                    print(startup_state,flush=True)
                pad = gp.Pad(stop=9,step_button=None,steps=(1,),phase_button=8)
                self.status.update(ready=True,state=startup_state,usb=str(usb[0]),camera_count=self.camera_count,input_mode=self.input_mode)
                print('PREVIEW http://localhost:8081 · home_verified='+str(self.homed),flush=True)
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
                            raise RuntimeError(str(self.status.get('last_input_loss'))+' · 다시 수집 시작을 누르세요')
                        if stream.error: raise RuntimeError(stream.error)
                        if command == 'home':
                            self.finish('기본자세 복귀 · 기존 수집 종료')
                            self.require_input()
                            self.initialize_pose(arm)
                            self.status['state']='기본자세 확인 완료 · 수집 시작을 누르세요'
                            pad.ready=False
                            continue
                        if command == 'start':self.begin(arm,pad)
                        if not self.active:
                            pad.phase_requested = False
                            continue
                        now=time.monotonic()
                        # Read feedback periodically to detect all-joint communication loss.
                        if now >= self.next_read:
                            self.checked_angles(arm, partial=True)
                            self.next_read=now+1
                            input_lost = self.poll_input(fd,pad)
                            if input_lost:raise RuntimeError(str(self.status.get('last_input_loss'))+' · 정지')
                            if pad.stopped:continue
                        if pad.phase_requested:
                            pad.phase_requested=False
                            if pad.ready:
                                if self.move(arm,pad,fd,home=True):
                                    self.next_move=time.monotonic()+2.5
                                pad.ready=False
                        if now >= self.next_move and pad.ready:
                            if pad.request():
                                self.move(arm,pad,fd)
                                self.next_read=time.monotonic()+1
                            self.next_move=time.monotonic()+.15
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
    args=parser.parse_args()
    collector=Collector(input_mode=args.input)
    for sig in (signal.SIGINT,signal.SIGTERM):
        signal.signal(sig,lambda *_:collector.quit.set())
    collector.run()
