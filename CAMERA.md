# USB 카메라와 CSI 카메라 추가 연결

> **Pi 5 사용 시**: 이 문서의 1~2절은 Pi 4 + 제조사 Ubuntu 20.04의 **구형 카메라 설정** 기록이다. Pi 5에서는 `raspistill`, `vcgencmd get_camera`, `bcm2835_v4l2`, `start_x=1`이 동작하지 않으므로 따라 하지 않는다.
>
> - 현재 팀 설정은 **USB 카메라, `--camera 0`**이다. USB 카메라는 Pi 5에서도 OpenCV로 그대로 연다.
> - Pi 5는 카메라와 무관한 영상 노드(`pispbe`, `rpi-hevc`, CSI를 연결하면 `rp1-cfe`)를 많이 만든다. `python3 preflight.py`의 카메라 목록이나 `v4l2-ctl --list-devices`로 USB 카메라가 `/dev/video0`인지 확인한다.
> - 번호가 자꾸 바뀌면 `ls /dev/v4l/by-id/`에서 `...-video-index0`로 끝나는 경로를 `--camera`에 그대로 넣는다.
> - CSI V1.3을 Pi 5에 붙이려면 22핀↔15핀 변환 케이블이 필요하다. 확인은 `rpicam-hello --list-cameras`로 하고, 영상은 `picamera2`로 읽어야 한다. `camera_stream.py`는 CSI 노드를 직접 열지 않으며, 그런 노드를 고르면 열기 전에 거부한다. CSI 지원은 아직 코드에 없다.

Raspberry Pi 4에 기존 USB 카메라와 15핀 Camera Module V1.3을 연결하는 절차다. 2026-11-07 제조사 Ubuntu 20.04.3 이미지, 커널 `5.4.0-1050-raspi`, Python 3.8.10 환경에서 확인했다. V1.3은 통상 OV5647 센서를 쓰는 모델이다. 최신 Raspberry Pi OS나 다른 카메라 모델에는 아래 구형 카메라 설정을 그대로 적용하지 않는다.

이 문서는 제조사 OS 설치, SSH·VNC 접속, `~/dof-bot` 저장소와 Python 3·OpenCV·`v4l2-ctl`이 준비된 장비를 전제로 한다. 저장소 준비는 [README.md](README.md#설치와-점검)를 따른다. 이 장비의 OpenCV는 4.8.1이며, 아래 절차는 새 OS의 전체 설치 안내가 아니다.

## 확인된 상태

| 항목 | 확인 결과 |
| --- | --- |
| USB 카메라 | OpenCV로 640×480 프레임 수신 성공 |
| CSI V1.3 | `supported=1 detected=1`, OpenCV로 640×480 프레임 수신·JPEG 저장 성공 |
| 실제 사진 | 작업대, 색종이, 물체, 로봇팔이 보이는 것을 확인 |
| CSI 웹 서버 | `camera_stream.py --camera 2 --port 8081 --bind 127.0.0.1` 실행, `/snapshot.jpg` HTTP 200 확인. Pi Firefox 화면 표시와 `/stream` 연속 수신은 별도 미검증 |
| 두 카메라 동시 운용 | 각 카메라의 개별 캡처는 확인. 동시 송출의 지속 안정성·fps·지연·CPU 사용률과 프레임 동기화는 미검증 |
| 자동 집기 | 카메라 추가만으로 구현되지 않음. 좌표 보정·검출·분류·집기 검증은 별도 |

최종 점검 당시 장치 구성은 다음과 같다. **번호는 재부팅이나 재연결 후 달라질 수 있으므로 실행 전에 다시 확인한다.**

| 경로 | 역할 |
| --- | --- |
| `/dev/video0` | USB 카메라의 영상 캡처 |
| `/dev/video1` | 같은 USB 카메라의 메타데이터. 두 번째 카메라가 아님 |
| `/dev/video2` | CSI 카메라, 이름 `camera0` |
| `/dev/video10`~`/dev/video16` | 코덱·영상 처리 장치. 카메라 개수가 아님 |

## 1. 전원을 끄고 CSI 단자에 연결

1. 로봇 제어와 카메라 프로그램을 정상 종료한다. 팔을 받치고 모터 전원 차단에 대비한다.
2. Pi에서 `sudo shutdown -h now`를 실행하고 종료가 끝난 뒤 전원을 분리한다. 외부 전원으로 역급전되지 않도록 연결도 확인한다.
3. Pi의 **CAMERA/CSI** 단자를 확인한다. 비슷하게 생긴 **DISPLAY/DSI** 단자와 구별한다. 단자 위치와 케이블 방향은 [공식 연결 안내](https://www.raspberrypi.com/documentation/accessories/camera-software.html#connect-the-camera)의 Pi 4 그림을 따른다.
4. 잠금 탭을 조심히 열고, 케이블 금속 접점이 커넥터 접점과 마주 보도록 끝까지 수평으로 넣고 잠근다. 카메라 쪽 케이블도 확인한다. 파란 보강판 방향만으로 판단하지 않는다.
5. 케이블이 팔·그리퍼에 끼거나 당겨지지 않게 고정한 뒤 전원을 켠다.

이번 점검에서는 설정 적용 후에도 `detected=0`이었고, 작업자가 연결 단자를 바꾼 뒤 `detected=1`과 실제 영상 수신을 확인했다. 원래 연결된 단자가 무엇이었는지 원격으로 확인하지는 않았다.

## 2. 기존 OS에서 카메라 활성화

현재 이미지에는 `raspistill`, `vcgencmd`, `bcm2835_v4l2`와 카메라용 펌웨어가 있다. `rpicam-hello`, `libcamera-hello`는 없었다. OS나 라이브러리를 일괄 업그레이드하지 않고 기존 구형 카메라 방식을 사용했다. [공식 구형 부팅 설정](https://www.raspberrypi.com/documentation/computers/legacy_config_txt.html)과 [카메라 소프트웨어 안내](https://www.raspberrypi.com/documentation/accessories/camera-software.html)를 참고한다.

먼저 현재 설정과 필요한 파일을 확인한다.

```bash
cat /boot/firmware/usercfg.txt
grep -nE 'include|start_x|gpu_mem|start_file|fixup_file' \
  /boot/firmware/config.txt /boot/firmware/syscfg.txt /boot/firmware/usercfg.txt
ls -l /boot/firmware/start4x.elf /boot/firmware/fixup4x.dat
```

이 이미지의 `/boot/firmware/config.txt`는 `usercfg.txt`를 포함하며, 사용자 변경을 그 파일에 두도록 안내한다. 기존 설정을 백업하고 편집한다.

```bash
camera_backup="/boot/firmware/usercfg.txt.before-csi-$(date +%Y%m%d-%H%M%S)"
sudo cp /boot/firmware/usercfg.txt "$camera_backup"
echo "Backup: $camera_backup"
sudo nano /boot/firmware/usercfg.txt
```

파일 끝에 아래 설정을 둔다. 이미 같은 항목이 있으면 중복 추가하지 말고 기존 값과 적용 범위를 확인한다.

```ini
[all]
start_x=1
gpu_mem=128
```

재부팅 전에 실행 중인 작업을 종료하고 로봇 주변을 비운다. 제조사 서비스가 자동 시작되면 카메라·모터 제어권을 다시 가져갈 수 있다. VNC와 SSH 연결도 끊긴다.

```bash
systemctl is-active yb-bigProgram dofbot_oled
systemctl is-enabled yb-bigProgram dofbot_oled
sudo reboot
```

재접속 후 실제로 재부팅됐는지와 설정 적용을 확인한다. 이번 장비에서는 자동 업데이트 다운로드 작업의 종료 대기 때문에 재부팅이 지연됐다. `systemctl list-jobs --no-pager`로 상태를 보고 강제 전원 차단은 피한다.

```bash
uptime
sudo vcgencmd get_camera
sudo vcgencmd get_mem gpu
```

확인된 결과는 `supported=1 detected=1`, `gpu=128M`이다. `vcgencmd`는 이 구형 환경에서 사용한 검사이며 최신 카메라 스택의 공통 판정법으로 사용하지 않는다.

되돌릴 때는 위에서 출력한 **실제 백업 파일 경로**를 아래 명령에 넣고 복사한 뒤, 같은 절차로 재부팅한다. 재접속한 터미널에서는 `camera_backup` 변수가 남아 있지 않을 수 있다.

```bash
sudo cp /boot/firmware/usercfg.txt.before-csi-YYYYMMDD-HHMMSS /boot/firmware/usercfg.txt
```

## 3. 장치와 점유 프로세스 확인

```bash
cd ~/dof-bot
python3 preflight.py
for p in /sys/class/video4linux/video*; do
  echo "$p: $(cat "$p/name")"
done
v4l2-ctl --list-devices
```

`preflight.py`는 경로만 나열한다. 카메라 개수·영상 수신 성공 여부는 판단하지 않는다. 이름이 같거나 역할이 불분명하면 개별 장치의 `Video Capture`와 `Metadata Capture` 항목을 확인한다.

```bash
v4l2-ctl -d /dev/video2 --all
sudo fuser -v /dev/video0 /dev/video1 /dev/video2
```

명령의 경로는 실제 목록에 맞춘다. **같은 카메라를 여는 프로세스는 하나만 둔다.** 기존 스트림이나 노트북이 사용 중이면 먼저 해당 프로그램을 정상 종료한다. 서로 다른 카메라는 별도 프로세스에서 열 수 있으나, 이 장비의 동시 운용 성능은 별도 측정해야 한다.

카메라 단독 확인에는 I2C나 모터를 열지 않는다. 제조사 서비스가 선택한 카메라를 점유한다면 [제어권 확인 절차](JOYSTICK.md#1-제어권-확인)에 따라 원래 상태를 기록하고 필요한 서비스를 일시 정지한다. `camera_stream.py`는 모터 도구의 파일 잠금·I2C 검사를 수행하지 않는다.

## 4. VNC에서 영상 보기

VNC로 Pi 데스크톱에 접속하고 Pi 터미널에서 실행한다. 아래 `2`는 최종 점검 때 확인한 CSI 장치 번호다.

```bash
cd ~/dof-bot
python3 camera_stream.py --camera 2 --port 8081 --bind 127.0.0.1
```

**VNC 안의 Pi Firefox**에서 `http://127.0.0.1:8081/`을 연다. `/snapshot.jpg`는 사진 한 장, `/stream`은 MJPEG 영상이다. `127.0.0.1`은 주소를 여는 컴퓨터 자신을 뜻하므로 Mac 브라우저에서 같은 주소를 열어 Pi에 접속할 수는 없다. 영상은 읽기 전용이며 키보드·마우스 로봇 조종 기능이 없다. 종료는 실행한 터미널에서 `Ctrl+C`다.

USB 카메라를 별도로 확인하려면 장치 번호를 다시 확인하고, 다른 Pi 터미널에서 실행한다. 제조사 앱이 점유 중이면 먼저 위의 제어권 절차를 따른다.

```bash
python3 camera_stream.py --camera 0 --port 8080 --bind 127.0.0.1
```

Pi Firefox에서 `http://127.0.0.1:8080/`을 연다. 서로 다른 카메라·포트를 사용해야 한다. 기본 바인딩은 `0.0.0.0`이고 인증 기능은 없으므로 VNC로 확인할 때는 위처럼 `--bind 127.0.0.1`을 명시한다.

### 다른 사람이 사용하도록 해제

앞의 포그라운드 명령은 실행한 터미널에서 `Ctrl+C`로 종료한다. 백그라운드로 실행했거나 원래 터미널이 없다면 명령과 점유 PID를 먼저 대조한다.

```bash
ps -eo pid,args | grep '[c]amera_stream.py'
sudo fuser -v /dev/video2
```

본인이 실행한 스트림임을 확인한 **실제 PID**에 `kill -INT PID`를 보낸다. 다른 사람이나 제조사 프로세스를 일괄 종료하지 않는다. 종료 뒤 위의 `fuser` 결과에 점유 PID가 없는지, `ss -ltn 'sport = :8081'`에 수신 소켓이 없는지 확인한다. 이번 점검에서는 실행했던 CSI 스트림을 이 방법으로 정상 종료하고 장치·포트 해제를 확인했다.

## 5. 게임패드와 함께 사용

VNC는 Pi 화면과 터미널을 조작하는 수단이다. 현재 로봇 제어 코드는 **Pi에 연결한 USB 조이스틱**을 읽으며 Mac의 게임패드를 전달하지 않는다. 모터 제어를 시작하기 전 [게임패드 시작 절차](GAMEPAD.md#시작-전-절차)를 마친다.

게임패드 프로그램에서 CSI 영상을 함께 띄우려면, 먼저 같은 CSI 카메라의 단독 스트림을 종료하고 다음처럼 `--camera 2`를 지정할 수 있다.

```bash
python3 gamepad_control.py --move --steps 3 --camera 2 --stream 8081 --bind 127.0.0.1
```

이 명령은 **모터를 움직이는 모드**다. CSI 단독 송출은 확인했지만 CSI 영상과 게임패드를 결합한 실물 동작은 아직 검증하지 않았다. 영상 확인만 필요하면 4절의 `camera_stream.py`를 사용한다.

## 문제별 확인

| 증상 | 확인할 것 |
| --- | --- |
| `supported=0 detected=0` | 현재 구형 OS의 카메라 활성화 설정, 필요한 펌웨어 파일, 재부팅 여부. 이것만으로 카메라 고장으로 판단하지 않음 |
| `supported=1 detected=0` | 전원을 분리한 상태에서 CAMERA/CSI 단자, 양쪽 케이블 방향·삽입·잠금 확인. 계속 실패하면 케이블·모듈 교차 시험 |
| `detected=1`인데 CSI 영상 노드가 없음 | 이 구형 이미지에서 `lsmod`와 `modinfo bcm2835_v4l2`로 모듈을 확인. 모듈이 있으나 로드되지 않았다면 `sudo modprobe bcm2835_v4l2` 후 장치 목록 재확인. 실제 프레임 수신까지 성공해야 사용 가능 |
| 일반 사용자에서 `VCHI initialization failed` | 이 장비에서는 `sudo vcgencmd get_camera`로 조회 성공. 이 오류와 센서 미감지를 구분 |
| `Cannot open /dev/video0` | 실제 영상 장치 번호를 다시 확인. 경로가 없다는 메시지가 모든 카메라의 고장을 뜻하지 않음 |
| 영상이 안 열림 | `fuser`로 점유 확인, `v4l2-ctl`로 영상 장치인지 확인. 메타데이터·코덱 노드는 선택하지 않음 |
| 포트 사용 중 | 기존 스트림을 확인해 정상 종료하거나 다른 포트 사용. 같은 카메라에 두 스트림을 실행하지 않음 |

본 수집 전에 카메라 위치·고정 방법·시야를 정하고 [수집 가이드](DATA_COLLECTION.md#43-카메라-장착-확인-본-수집-전)의 검사를 마친다. 카메라 구성이나 장착이 바뀌면 `camera_setup`을 새로 기록한다. 장비 주소, 비밀번호, 사진, 영상, 개인 로그는 공개 저장소에 올리지 않는다.
