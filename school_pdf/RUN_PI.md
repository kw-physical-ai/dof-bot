# Pi에 게임패드를 연결해 실제 수집

게임패드가 다른 PC에 연결되어 있으면 이 문서 대신 [PC_LAN.md](PC_LAN.md)를 따른다. 아래는 기존 `--input pi` 모드 설명이다.

학습용 v2 시범 정보·목적지 설정·결과 검수는 [TRAINING.md](../TRAINING.md)를 따른다. PC 모드와 마찬가지로 웹 화면에서 재질·물체·위치를 입력한다.

## 연결 및 환경

게임패드, 손목 USB 카메라, OV5647 CSI 카메라를 Pi에 모두 연결한다. Pi 5 CSI 단자에는 맞는 변환 케이블이 필요하다. 리본 케이블은 전원을 끄고 연결한다.

저장소 루트의 Python 가상환경 `.venv`에서 Arm_Lib, OpenCV, Picamera2를 사용할 수 있어야 한다. 기존 README의 Pi 5 설치 절차를 따른다. `preflight.py`로 I2C와 장치를 확인하고 `rpicam-hello --list-cameras`, `v4l2-ctl --list-devices`로 카메라를 확인한다. 현재 구현은 USB 캡처 장치 하나와 libcamera 목록의 CSI 0번을 요구한다.

제조사 `YahboomArm.pyc`, 다른 수집 프로그램, 노트북 커널이 장치를 사용 중이면 먼저 종료한다. `sudo fuser -v /dev/i2c-1 /dev/video*`로 소유 프로세스를 확인한다. 재부팅 후 제조사 앱이 다시 실행될 수 있다. 임의의 PID를 복사해 종료하지 않는다.

## 실행과 접속

Pi에서 저장소 루트 기준:

```bash
.venv/bin/python school_pdf/data_collect.py
```

노트북에서 별도 터미널을 열고 `<PI_ADDRESS>`에 팀의 Pi LAN/Tailscale 주소를 넣는다.

```bash
ssh -N -L 127.0.0.1:18081:127.0.0.1:8081 pi@<PI_ADDRESS>
```

브라우저에서 `http://localhost:18081`을 연다. 서버는 Pi의 `127.0.0.1:8081`에만 바인딩하므로 다른 사람도 자신의 SSH 터널을 열어 접속한다. 한 사람이 조작을 담당한다. 화면을 열기만 하면 팔은 움직이지 않는다.

실행 시 제조사 부팅 자세 `[90,150,0,0,90,30]`로 자동 이동한다. 실행 전에 팔 주변을 비운다.

1. USB·CSI 영상과 `home_verified=true`를 확인한다.
2. **기본자세 확인 후 수집 시작**을 누른다. 기본자세 도달을 확인하고 새 세션을 만든다.
3. 스틱·버튼을 한번 모두 놓은 뒤 조작한다.
4. **START(9)** 또는 웹 **정지**로 저장을 마무리한다.

| 입력 | 동작 |
| --- | --- |
| 왼쪽 스틱 X / Y | 1축 / 2축 |
| L1 / L2 (4 / 6) | 3축 감소 / 증가 |
| R1 / R2 (5 / 7) | 4축 감소 / 증가 |
| 버튼 2 / 4 (1 / 3) | 5축 증가 / 감소 |
| 버튼 1 / 3 (0 / 2) | 집게 증가 / 감소 |
| SELECT(8) | `[90,150,0,0,90,30]` 초기 자세로 2초 이동 |
| START(9) | 수집·새 이동 명령 정지 |

숫자는 Linux 게임패드 번호다. 장치에 인쇄된 번호와 다를 수 있다. 정지는 이미 전송된 모터 이동을 물리적으로 취소하지 않는다. 웹 연결이 끊겨도 Pi 게임패드 조작과 로컬 수집은 계속될 수 있다. 장비 옆에서 START로 멈춘 뒤 상태를 확인한다.

시작 기본자세 도달 확인은 필수다. 실패하면 영상을 유지한 채 수집을 차단한다.
문제를 해결한 뒤 **기본자세 복귀 / 재시도**를 누른다. 시작 복귀의 경계 피드백 처리,
SDK 범위, 수집 중 관절별 차단과 전체 정지 조건은 [PC_LAN.md 5절](PC_LAN.md#5-시범-수집)을 따른다.
SDK 범위는 5번 0~270°, 나머지 0~180°이며 수동 조작은 명령당 3°·150ms다.

## 저장 데이터

`school_pdf/datasets/dataset_날짜시간/`:

- `cam1/00001.jpg`: USB, `cam2/00001.jpg`: CSI. 같은 행에 연결한 두 프레임.
- `labels.csv`: `frame_id,timestamp,m1,m2,m3,m4,m5,m6`. 각도는 **목표각**이다.
- `frames.csv`: 각 카메라의 호스트 단조시계 수신 시간. 동시 노출은 보장하지 않는다.
- `meta.json`: 정상 종료 시 프레임 수·종료 사유·카메라·미검수 상태.

목표 3 FPS, 244×244 JPEG 품질 85다. 처리 지연으로 실측 저장률은 낮아질 수 있다. 640×480 원본을 정사각형으로 리사이즈하므로 비율이 변하지만 crop하지 않는다. 정상 종료 전에 전원이나 프로세스가 끊기면 meta.json이 없거나 마지막 이미지/CSV가 불완전할 수 있으므로 검수한다. 실제 수집물은 Git에 포함하지 않는다.

## 입력만 확인

수집을 종료한 뒤 Pi에서:

```bash
.venv/bin/python school_pdf/gamepad_check.py
```

노트북에서 `ssh -N -L 127.0.0.1:18082:127.0.0.1:8082 pi@<PI_ADDRESS>` 후 `http://localhost:18082` 접속. 모터 명령 없이 6축·12버튼 상태와 실제 이벤트 기록을 확인한다.

## 터미널과 분리해 실행 (선택)

```bash
cd ~/dof-bot
systemd-run --user --unit=dofbot-data-collect --collect \
  --working-directory="$PWD" \
  "$PWD/.venv/bin/python" -u "$PWD/school_pdf/data_collect.py"
```

종료: `systemctl --user stop dofbot-data-collect`

로그: `journalctl --user -u dofbot-data-collect -n 50 --no-pager`

이 서비스는 임시 실행이며 재부팅 자동 시작 설정이 아니다. 재부팅 후 서버와 SSH 터널을 다시 실행한다. 수집 시작은 웹 버튼으로 별도 수행한다.
