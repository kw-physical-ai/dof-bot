# 데이터 수집 빠른 실행 — PC 게임패드

준비된 Pi 5와 이 문서로 게임패드 시범을 수집하고 저장 결과를 확인한다. **PC에 게임패드, Pi에 로봇팔·USB 카메라·OV5647 CSI 카메라**를 연결한다. 실행 파일은 `school_pdf/data_collect.py --input pc`이며 Jupyter는 필요 없다.

## 1. 연결과 주소 준비

- Pi 전원과 로봇팔 모터 전원을 모두 확인한다. 케이블·서보 포트를 바꿀 때는 Pi를 정상 종료하고 양쪽 전원을 분리한다.
- Pi의 USB 캡처 카메라는 하나, CSI는 libcamera 목록의 `csi0`여야 한다. 현재 프로그램에는 USB 단독 실행 옵션이 없다.
- 로봇팔의 케이블은 **BUS SERVO 포트**에 연결한다. PWM 핀에 꽂지 않는다. 2026-11-23에는 버스 6번에서 전체 응답이 없다가 1번으로 이동·재체결 후 6개 모두 응답했다. 포트 고장으로 확정한 것은 아니다.
- 계정·비밀번호·실제 접속 주소는 장비 담당자에게 받는다. 문서·Git·로그 공유본에 비밀번호를 남기지 않는다.

**LAN 직결에는 주소 설정이 필요하다.** DHCP 서버가 없는 케이블에서 Pi를 DHCP로 두면 주소 설정 실패로 연결이 끊길 수 있다. 실제 장비에서 45초마다 실패하는 로그와 SSH 터널 종료를 확인했다. 이미 고정 IPv4가 설정됐다면 변경하지 말고 확인만 한다.

| 확인 위치 | 명령 | 통과 조건 |
| --- | --- | --- |
| Pi 터미널 | `ip -4 addr show eth0` | 유선 IPv4가 있음 |
| Pi 터미널 | `nmcli device status` | `eth0`가 `connected` |
| Mac 터미널 | `networksetup -listallhardwareports` | USB Ethernet 서비스·장치 확인 |
| Mac 터미널 | `networksetup -getinfo '<Ethernet 서비스 이름>'` | Pi와 같은 서브넷의 IPv4 |

Windows/Linux PC도 유선 IPv4를 Pi와 같은 서브넷으로 수동 지정하고 게이트웨이를 비운다. 아래 PC 설정 명령은 Mac 전용이다.

처음 설정할 때는 다른 네트워크와 겹치지 않는 사설 `/24` 대역을 정한다. 아래 `<PC_LAN_IP>`와 `<PI_LAN_IP>`는 서로 다른 같은 대역 주소로 바꾼다. 기존 설정을 기록하고, 같은 이름의 Pi 프로필이 있으면 새로 만들지 말고 먼저 확인한다.

```bash
# Mac: 유선에만 적용. 라우터는 지정하지 않는다.
networksetup -setmanual '<Ethernet 서비스 이름>' <PC_LAN_IP> 255.255.255.0 0.0.0.0

# Pi: 화면/키보드 또는 기존 Wi-Fi SSH에서 실행. Wi-Fi 설정은 유지한다.
sudo nmcli connection add type ethernet ifname eth0 con-name dofbot-direct \
  ipv4.method manual ipv4.addresses <PI_LAN_IP>/24 ipv4.never-default yes \
  ipv6.method link-local connection.autoconnect yes connection.autoconnect-priority 100
sudo nmcli connection up dofbot-direct
```

이후 `<PI_ADDRESS>`에는 유선 IPv4를 사용한다. Tailscale 주소도 접속 가능하지만 유선 경로를 보장하지 않는다. PC의 `localhost`는 그 PC 자신이므로 **게임패드를 꽂은 PC에서 터널을 열어야 한다.**

## 2. PC에서 SSH 접속 + 터널 열기

PC 터미널에서 실행한다. 처음 접속하는 장비의 호스트 키는 담당자와 확인하며, 키 검사를 끄거나 기존 키를 무조건 삭제하지 않는다.

```bash
ssh -o ExitOnForwardFailure=yes -o ServerAliveInterval=10 -o ServerAliveCountMax=3 -L 127.0.0.1:18081:127.0.0.1:8081 pi@<PI_ADDRESS>
```

접속한 터미널은 이제 Pi 터미널이다. 창을 유지한다. `Address already in use`면 PC의 기존 18081 터널 소유자를 확인한다. 다른 사람의 SSH 프로세스를 일괄 종료하지 않는다.

## 3. Pi 프로그램 정리·코드·장치 점검

**기존 수집이 진행 중이면 담당자가 START로 저장 종료한 뒤** 업데이트한다. 실행 중인 수집 프로세스는 중복 실행하지 않는다.

```bash
cd ~/dof-bot
curl -s --max-time 2 http://127.0.0.1:8081/status
systemctl --user is-active dofbot-data-collect
pgrep -af 'school_pdf/data_collect[.]py'
```

`recording: true`면 중단하고 담당자와 조율한다. 수집이 끝났으면 기존 수동 실행 터미널에서 Ctrl+C, 서비스 실행이었다면 `systemctl --user stop dofbot-data-collect`로 종료한다.

```bash
cd ~/dof-bot
git status --short                 # 추적 파일 수정은 보존·확인 후 진행. reset --hard 금지.
git pull --ff-only origin main
git rev-parse HEAD                 # 사용한 SHA를 별도 수집 기록에 남긴다.
.venv/bin/python -c 'import Arm_Lib, cv2, picamera2; print("dependencies OK")'
.venv/bin/python preflight.py
sudo fuser -v /dev/i2c-1 /dev/video*
rpicam-hello --list-cameras
ls -l /dev/v4l/by-id/
.venv/bin/python motor_check.py --reads 3
vcgencmd get_throttled
```

통과 조건은 **6개 서보 모두 `ping ok / OK`, USB by-id 캡처 장치 하나, CSI OV5647 인식, 현재 전원 경고 없음**이다. `.venv` 또는 의존성이 없다면 준비된 수집 환경이 아니므로 시작하지 않는다. Pi 호스트의 Picamera2·OpenCV를 쓰며 제조사 Docker에서 실행하지 않는다.

`YahboomArm.pyc` 등 제조사 앱이 USB 카메라·I2C를 점유할 수 있다. PID와 실제 프로그램을 확인해 그 앱만 정상 종료한다. 반응하지 않으면 정확히 같은 프로세스인지 재확인한 뒤 해당 앱만 강제 종료한다. Docker 전체·무관한 프로세스를 종료하거나 자동 시작 설정을 임의 변경하지 않는다. Pi의 ISP 노드를 쓰는 pipewire만으로 USB 점유라고 단정하지 않는다.

## 4. 서버 실행 → PC 브라우저

Pi 터미널에서:

```bash
cd ~/dof-bot
.venv/bin/python -u school_pdf/data_collect.py --input pc
```

`READY`가 나오면 **같은 PC의 Chrome/Edge에서 `http://localhost:18081`**을 연다. `http://<PI_ADDRESS>:8081` 직접 접속이나 Mac의 포트를 LAN HTTP로 공개하는 방식은 사용하지 않는다. 서버는 loopback에 바인딩하며 브라우저 입력에는 localhost 보안 컨텍스트가 필요하다.

1. USB·CSI 영상이 둘 다 보이는지 확인한다. 아직 수집을 시작하지 않는다.
2. 게임패드 버튼을 한 번 눌러 이름과 축·버튼 값이 나오는지 확인한다.
3. START가 브라우저 버튼 9, SELECT가 8인지 확인한다. 실제 번호가 다르면 수집 전에 매핑을 수정·검증한다.
4. 조작자는 한 명, 게임패드는 하나만 쓴다. 다른 사람이 보려면 각자 PC에서 위 SSH 터널을 연다. 같은 서버의 시작·정지 기능도 공유되므로 담당자를 정한다.

## 5. 시범 수집

팔·케이블 주변을 비우고 물체 하나와 목적지를 정한다. 소프트웨어 정지는 이미 보낸 이동을 취소하지 못하므로 장비 옆에서 모터 전원을 차단할 수 있어야 한다.

1. 화면의 **수집 시작 · 현재 자세**를 누른다.
2. 스틱과 버튼을 모두 놓은 뒤 조작한다. 수집 탭을 활성화하고 창 포커스를 유지한다.
3. 물체를 집어 목적지에 놓는다. 처음에는 짧은 시험 1개만 저장한다.
4. **START(9) 또는 화면 정지**로 저장을 마친다. 다음 수집 시작은 새 폴더를 만든다.

| 입력 | 동작 |
| --- | --- |
| 왼쪽 스틱 X / Y | 1번 베이스 / 2번 어깨 |
| L1 / L2, 버튼 4 / 6 | 3번 감소 / 증가 |
| R1 / R2, 버튼 5 / 7 | 4번 감소 / 증가 |
| 버튼 1 / 3 | 5번 증가 / 감소 |
| 버튼 0 / 2 | 집게 증가 / 감소 |
| **START, 버튼 9** | **정지·저장 종료** |
| **SELECT, 버튼 8** | **기본자세 `[92,77,11,18,99,91]`로 2초 이동** |

기본자세 복귀는 수집 중, 입력을 모두 놓아 준비된 상태에서만 동작하며 집게도 움직인다. 준비용 복귀를 했다면 START로 그 세션을 종료하고 새 시범을 시작한다. 실행만으로 자동 이동하지 않는다.

수집 시작·이동 명령 묶음·기본자세 명령 직전에 6개 관절을 읽는다. 하나라도 읽지 못하면 새 이동과 수집을 중단한다. 실패값을 0이나 이전 목표각으로 대체하지 않는다. 이 검사는 통신 장애 자체나 기구적 충돌을 예방하지 않으며 이동 전 읽기 때문에 조작 주기가 길어질 수 있다.

## 6. 저장 확인·종료

Pi의 `~/dof-bot/school_pdf/datasets/dataset_날짜시간/`에 저장한다.

| 파일 | 의미 |
| --- | --- |
| `cam1/*.jpg`, `cam2/*.jpg` | USB·CSI 이미지, 244×244 JPEG |
| `labels.csv` | `frame_id,timestamp,m1..m6`, **실측각이 아닌 목표각** |
| `frames.csv` | 같은 행에 묶은 각 카메라의 호스트 수신 시각. 동시 노출 아님 |
| `meta.json` | 정상 종료 시 프레임 수·종료 사유·`unreviewed` 결과 |

목표 저장률은 3 FPS이며 실제 속도는 낮아질 수 있다. 원본 640×480을 정사각형으로 resize하므로 비율이 바뀐다. START로 종료한 뒤 Pi에서 마지막 세션의 대응을 확인한다.

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
import csv, json
p = max(Path('school_pdf/datasets').glob('dataset_*'), key=lambda x: x.stat().st_mtime)
with (p/'labels.csv').open() as f: rows = list(csv.DictReader(f))
with (p/'frames.csv').open() as f: times = list(csv.DictReader(f))
meta = json.loads((p/'meta.json').read_text())
assert len(rows) > 0 and len(rows) == len(times) == meta['frames']
expected = {f"{int(r['frame_id']):05d}.jpg" for r in rows}
assert len(expected) == len(rows)
assert [r['frame_id'] for r in rows] == [r['frame_id'] for r in times]
for camera in ('cam1', 'cam2'):
    assert {x.name for x in (p/camera).glob('*.jpg')} == expected
print(p, 'frames=', len(rows), 'reason=', meta['reason'])
PY
```

이미지와 실제 동작도 확인하고, 세션명·성공/실패·대상/목적지·코드 SHA·카메라 배치를 별도 기록에 남긴다. `unreviewed`를 성공 데이터로 취급하지 않는다. 전원 차단으로 메타데이터나 파일이 불완전하면 미완료로 보존한다. 이미지·CSV·개인정보를 Git에 추가하지 않는다.

모든 수집이 끝나면 Pi 터미널에서 Ctrl+C로 서버를 종료한다. 서비스 실행이었다면 `systemctl --user stop dofbot-data-collect`를 쓴다. 서버 종료 후 SSH 터미널을 `exit`로 닫는다.

## 7. 오류 대응

| 증상 | 확인·대응 |
| --- | --- |
| 브라우저 전체가 ‘연결 끊김’ | PC의 18081 터널, Pi의 8081 서버, LAN IPv4를 각각 확인. Pi에서 `curl -s --max-time 2 http://127.0.0.1:8081/status`. 직결 DHCP 실패면 1절 확인 |
| `last_input_loss`가 입력 시간 초과 | 750ms 동안 입력 미수신. `input_age_ms`, `input_max_gap_ms`와 SSH 경로 확인. 이동 차단 제한 시간을 늘려 숨기지 않는다 |
| 창 포커스 상실 / 탭 숨김 | 수집 창을 선택. 새로고침 후에도 입력 확인→수집 시작 순서로 진행 |
| 게임패드 미검출 | PC 연결, localhost, 버튼 누르기, 브라우저 입력값 확인 |
| `Servo N angle unavailable` | 수집 중단. 서버 종료 후 `motor_check.py --reads 3`. 전부 무응답이면 공통 모터 전원·BUS 배선 확인. 읽기 검사 우회·강제 기본자세·임의 토크 변경 금지 |
| `BOUNDARY` | 응답은 있지만 SDK 경계 부근. 수집 허용 범위를 넓혀 계속하지 않는다. 별도 감독하에 안쪽 복귀 후 정상 읽기 확인 |
| `gave no frame` | USB by-id, 점유 프로세스, CSI 인식 확인. 시작 실패 서버는 Ctrl+C로 종료한 뒤 재시도 |
| `Another browser owns...` | 수집을 정지하고 기존 조작 탭을 닫은 뒤 담당자 전환 |

`input_max_gap_ms`는 서버 실행 후 최대 수신 간격이며 편도 지연이 아니다. `feedback_valid=false`일 때 이전 `measured`는 현재 위치가 아니다. 재연결만으로 자동 재개하지 않는다. **저장 대응과 실제 동작을 검수한 뒤 수집량을 늘린다.**
