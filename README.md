# Dofbot 준비

카메라로 물체를 인식해 집는 기능을 개발합니다. 현재 저장소에는 환경 점검, 단일 관절 시험, [USB 조이스틱 제어](JOYSTICK.md)가 구현되어 있습니다. 조이스틱은 로컬 테스트까지 완료했으며 Pi 실물 동작 확인은 남아 있습니다. SSH로 확인한 장비 상태, 제어권 전환, 보정 및 구현 순서는 [개발 설계](DESIGN.md)를 따릅니다. 사람이 조이스틱으로 보여 준 시범을 모아 학습하는 방식의 데이터 수집 절차와 규칙은 [시범 데이터 수집 가이드](DATA_COLLECTION.md)에 있습니다.

게임패드로 여러 관절을 조작하고, 카메라 화면을 보며 시험하고, 서보 상태를 점검하는 도구는 [게임패드 조작과 장비 점검 도구](GAMEPAD.md)에 정리되어 있다. 기존 USB 카메라에 15핀 CSI V1.3 카메라를 추가하는 방법과 VNC에서 영상을 확인하는 절차는 [카메라 연결 안내](CAMERA.md)를 따른다. 두 카메라 모두 개별 프레임 수신을 확인했으며, CSI 카메라 웹 서버의 스냅샷 응답도 확인했다. 장치 번호는 재부팅 후 다시 확인한다.

## 분리수거 과제 준비 상태

2026-11-07 로컬 과제 안내 PDF를 확인한 기준이다. 종이·비닐·플라스틱 3분류, 무작위 배치한 물체 6개, 반지름 23cm 작업 영역에서 해당 색종이로 집어 옮긴 뒤 그리퍼를 놓아야 성공이다. 자기 조와 다른 조의 물체 세트로 평가하며, 정확도는 성공 개수/전체 개수, 완료 시간은 전체 물체 이동까지다. 제한 시간은 6분 예정이며 변경 가능하다. 고정 카메라 허용 여부는 별도 확인이 필요하다. PDF 원본은 로컬 `pdf/`에만 보관한다.

현재는 수동 조종·서보 점검·카메라 관찰 단계다. 검출·3분류·좌표 보정·자동 집기·6개 연속 처리와 완료 시간 검증은 남아 있고, 시범 기록·학습·추론 코드도 아직 없다. 로컬 가짜 하드웨어 테스트 35개 통과(Pi 5 이전 검사 11개 포함)는 자동 집기 성공의 근거가 아니다. 다음 실물 확인은 실제 과제 물체 하나를 카메라와 게임패드로 집어 지정 위치에 놓는 것이다.

## Raspberry Pi 5로 옮길 때

2026-11-09 보드를 Pi 4에서 Pi 5로 교체했다. 아래 문서 중 Pi 4 점검 기록(Ubuntu 20.04, Python 3.8, 구형 카메라 설정)은 당시 기록으로 남겨 두며 Pi 5에는 그대로 적용하지 않는다. 코드는 Pi 4와 Pi 5 모두에서 그대로 동작하도록 고쳤고, Python 3.11 가상환경과 가짜 하드웨어로 테스트 35개를 통과했다. **Pi 5 실물에서는 아직 확인하지 않았다.**

**바뀐 점**

| 항목 | Pi 4 (기존) | Pi 5 |
| --- | --- | --- |
| OS | 제조사 Ubuntu 20.04 | Pi 5를 지원하는 OS 필요 (Raspberry Pi OS Bookworm 64-bit 등). 20.04는 Pi 5에서 부팅하지 않음 |
| Python | 3.8 | Bookworm 기준 3.11. 시스템 Python에 `pip install`이 기본으로 막혀 있음 |
| 확장보드 I2C | `/dev/i2c-1`, 주소 `0x15` | 40핀 헤더 I2C는 그대로 `/dev/i2c-1`. 단, I2C를 켜야 생김 |
| USB 카메라 | `/dev/video0` | 보통 `/dev/video0`. CSI 카메라를 같이 꽂으면 번호가 밀릴 수 있음 |
| CSI 카메라 | 15핀, 구형 스택(`raspistill`, `vcgencmd get_camera`) | 22핀 소형 단자라 **22핀↔15핀 변환 케이블** 필요. 구형 스택 없음, `rpicam-hello`/`picamera2` 사용. OpenCV `VideoCapture`로는 안 열림 |
| 전원 | 5V 3A | 5V 5A 권장. 부족하면 USB 포트 합계가 600mA로 제한되어 카메라·조이스틱이 끊길 수 있음 |

**처음 한 번 설정 (Pi 5에서)**

```bash
sudo raspi-config nonint do_i2c 0          # I2C 켜기, 이후 재부팅
sudo apt update
sudo apt install -y python3-smbus i2c-tools python3-opencv psmisc v4l-utils git
sudo reboot
i2cdetect -y 1                              # 15 가 보이면 확장보드 연결 정상
```

`Arm_Lib`는 제조사 자료의 `Arm_Lib` 폴더(setup.py가 있는 곳)에서 설치한다. Bookworm은 시스템 Python 보호 때문에 옵션이 필요하다.

```bash
cd <Arm_Lib 폴더>
sudo pip3 install . --break-system-packages
python3 -c "from Arm_Lib import Arm_Device; print('Arm_Lib OK')"
```

가상환경을 쓰려면 apt로 설치한 `smbus`·OpenCV를 함께 보도록 `--system-site-packages`로 만든다.

```bash
python3 -m venv --system-site-packages ~/dof-bot/.venv
source ~/dof-bot/.venv/bin/activate
pip install <Arm_Lib 폴더>
```

**확인 순서**: `python3 preflight.py`에서 Board가 Raspberry Pi 5, `Arm I2C bus: /dev/i2c-1 present`, 카메라 목록에서 `capture -> --camera 0`인 줄이 USB 카메라인지 본다. 그다음 `python3 motor_check.py`로 6개 서보가 모두 OK인지 확인한다.

**코드에서 바뀐 점**

- 모든 모터 도구가 `arm_control.open_arm()`으로 장치를 연다. I2C 장치가 없으면 해결 방법과 함께 바로 멈춘다. 확장보드가 다른 버스에 잡히면 `DOFBOT_I2C_BUS=번호`로 바꿀 수 있다.
- `--camera`는 기본 `0`(`/dev/video0`)을 그대로 쓰고, 번호 대신 `/dev/v4l/by-id/...-video-index0` 같은 경로도 받는다. 경로는 재부팅이나 CSI 카메라 추가로 번호가 바뀌어도 같은 USB 카메라를 가리킨다.
- 선택한 장치가 Pi 5의 CSI 수신기·ISP·코덱 노드(`rp1-cfe`, `pispbe` 등)이면 열기 전에 거부하고 `v4l2-ctl --list-devices` 확인을 안내한다.
- `preflight.py`가 보드·OS·가상환경 여부·SMBus/OpenCV 설치·I2C 버스·전원 협상값·카메라 노드별 역할을 함께 보여 준다.

제조사 서비스 이름(`yb-bigProgram`, `dofbot_oled`)은 Pi 5 이미지에서 다를 수 있다. `systemctl list-units | grep -i -E "yb|dofbot"`로 먼저 확인한다.

## 설치와 점검

Dofbot OS 설치가 끝나면 이 저장소를 라즈베리파이에 clone하고 실행 환경을 확인합니다. `~/dof-bot`이 이미 있으면 기존 파일을 백업하거나 다른 경로에 clone하세요.

```bash
git clone https://github.com/kw-physical-ai/dof-bot.git ~/dof-bot
cd ~/dof-bot
python3 preflight.py
```

이 스크립트는 Python과 시스템 정보, `Arm_Lib` 설치 여부, 카메라·조이스틱·I2C·제조사 직렬 장치 경로를 출력합니다. 장치를 열거나 모터를 움직이지 않습니다. `not found`는 곧바로 고장을 뜻하지 않습니다. 사용 중인 OS 이미지와 연결 방식에 따라 장치 이름이 다를 수 있습니다.

제조사 앱이나 노트북이 장치를 사용 중이면 먼저 [제어권 확인 절차](DESIGN.md#제어권과-동작-조건)를 따릅니다. 모터 전원을 켜고 로봇팔 주변 공간을 비운 뒤 현재 각도를 읽습니다.

```bash
python3 arm_smoke.py
```

각도 읽기 모드는 6개 관절을 모두 읽어야 성공합니다. 그래도 주변 간섭이나 전체 동작 준비를 보장하지는 않습니다. 단일 관절 시험과 조이스틱은 같은 파일 잠금을 사용하며, 시작 전에 다른 프로세스의 I2C 점유를 확인합니다. 관절 하나를 최대 10도까지 시험할 수 있으며 `--move`를 명시해야 움직입니다. 예시는 1번 관절을 현재 위치에서 5도 움직입니다.

```bash
python3 arm_smoke.py --move --servo 1 --delta 5
```

SDK 명령 범위는 5번 관절이 0~270도, 나머지가 0~180도이며, 충돌 없는 기구적 범위를 뜻하지 않습니다. 현재 각도를 읽지 못하거나 목표 각도가 범위를 벗어나면 움직이지 않습니다. 이동 후 출력되는 각도는 참고값이며, 사람이 실제 움직임과 간섭 여부를 함께 확인해야 합니다.

코드는 Mac에서 수정·커밋·푸시하고, Pi에서는 저장소 폴더에서 아래 명령으로 갱신합니다. 실행 중인 프로그램을 종료한 뒤 갱신하고 다시 실행합니다.

```bash
git pull --ff-only && python3 preflight.py
```

Pi에서는 추적 중인 파일을 직접 수정하지 않는 것을 기본으로 합니다. 로컬 변경 때문에 pull이 실패하면 변경 내용을 먼저 확인하고 보존하세요.

조립·OS 설치·조작 안내 PDF는 로컬 `pdf/`에 별도로 보관하며 공개 저장소에는 포함하지 않습니다. 조작 자료에 나온 Jupyter 노트북 `Dofbot/1.telecontrol/4.arm_handle`도 이 저장소에 포함되어 있지 않습니다.

모터 제어 API는 [Yahboom의 단일 서보 제어 안내](https://github.com/YahboomTechnology/dofbot-Pi/blob/main/10.Basic%20control%20course/3.Control%20single%20servo.pdf)와 [각도 읽기 안내](https://github.com/YahboomTechnology/dofbot-Pi/blob/main/10.Basic%20control%20course/4.Read%20servo%20angle.pdf)를 기준으로 작성했습니다. 설치 이미지의 `Arm_Lib` 동작은 실제 장비에서 확인해야 합니다.

게임패드와 USB + CAM0 OV5647 두 화면은 `python3 gamepad_control.py`로 실행한다.
기본 포트는 8080, 이동 단위는 3도이며 중립 입력 확인 후 모터 조작을 허용한다.
[두 카메라 설정·실행 안내](CAMERA.md#pi-5-usb--cam0-ov5647-측면-화면)를 따른다.
