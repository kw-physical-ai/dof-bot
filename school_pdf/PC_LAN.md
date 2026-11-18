# 다른 PC에서 게임패드로 조작하기 (유선 LAN)

```
PC: USB 게임패드 + 브라우저
              │ 랜선 / SSH 터널
Pi: Jupyter + 수집 Python + 로봇팔 + USB/CSI 카메라
```

PC에는 Python이나 Arm_Lib 설치가 필요 없다. Windows PowerShell, macOS/Linux 터미널의 SSH와 브라우저를 사용한다. 게임패드를 Pi에서 빼서 **이 브라우저를 여는 PC**에 꽂는다. 두 카메라는 Pi에 그대로 둔다.

학교 방식처럼 PC 브라우저 입력을 Pi Python으로 전달한다. 단, 이 버전은 `ipywidgets.Controller` 대신 브라우저 Gamepad API를 50ms 간격으로 전송한다. Jupyter는 서버 실행·로그 확인에 사용하고, 조작 화면은 별도 탭에서 연다. Pi 호스트의 Picamera2를 사용하므로 제조사 Docker에서 실행하지 않는다. 원본 학교 노트북은 비교용으로 보존한다.

## 1. 유선 IP 확인

PC와 Pi를 Ethernet 케이블로 연결한다. PC의 인터넷 공유로 Ethernet에 주소를 할당하거나 두 장치에 같은 대역의 고정 IP를 설정한다. 케이블만 꽂아서는 DHCP 주소가 자동으로 생기지 않을 수 있다.

Pi의 화면/터미널에서 `ip -4 addr show eth0`로 **유선 IPv4**를 확인한다. 아래 `<PI_LAN_IP>`는 그 실제 주소로 바꾼다. Tailscale 주소 대신 유선 주소를 사용해야 해당 Wi-Fi/Tailscale 경로를 거치지 않는다. 먼저 PC에서 `ssh pi@<PI_LAN_IP>`가 되는지 확인한다. IP 설정은 각 PC의 OS와 네트워크 정책에 맞게 진행한다.

## 2. Pi 코드·커널 준비 (한 번)

Pi에서 기존 저장소를 업데이트한다. 기존 수집 프로그램은 먼저 종료하고, 수정한 파일이 있으면 보존한 뒤 업데이트한다.

```bash
cd ~/dof-bot
git pull --ff-only origin main
.venv/bin/python -m pip install jupyterlab ipykernel
.venv/bin/python -m ipykernel install --user --name dofbot-pi --display-name 'Dofbot Pi (.venv)'
.venv/bin/python -c 'import Arm_Lib, cv2, picamera2; print("dependencies OK")'
```

`.venv`가 없다면 루트 README의 Pi 설치 절차부터 따른다. 시스템 apt의 Picamera2/OpenCV를 사용할 수 있도록 가상환경은 `--system-site-packages`로 만든다. Pi 전원·카메라 케이블 문제는 이 입력 방식으로 해결되지 않는다.

기존 수집 서비스가 있다면 `systemctl --user stop dofbot-data-collect`로 종료한다. `sudo fuser -v /dev/i2c-1 /dev/video*`로 USB 카메라를 잡은 프로세스를 확인한다. `YahboomArm.pyc`가 보이면 `docker ps`와 `docker top <컨테이너>`로 현재 제조사 컨테이너를 확인하고, 로봇이 움직이지 않는지 확인한 뒤 그 컨테이너만 종료한다. 자동 생성 컨테이너 이름은 재부팅 후 바뀔 수 있다. 자세한 복구 절차는 [RUN_PI.md](RUN_PI.md)를 따른다.

## 3. PC에서 SSH 터널 열기

다른 PC의 PowerShell 또는 터미널에서 아래 명령을 실행하고 창을 유지한다.

```bash
ssh -N -o ServerAliveInterval=10 -o ServerAliveCountMax=3 -L 127.0.0.1:9999:127.0.0.1:9999 -L 127.0.0.1:18081:127.0.0.1:8081 pi@<PI_LAN_IP>
```

포트가 이미 사용 중이면 이전 터널을 종료한다. 다른 PC에서 우리 Mac의 `localhost` 주소를 그대로 재사용하는 것이 아니라, **그 PC에서 위 터널을 열어야 한다.**

## 4. Jupyter 실행

별도 PC 터미널에서 Pi로 SSH 접속하고:

```bash
cd ~/dof-bot
.venv/bin/jupyter lab --ip=127.0.0.1 --port=9999 --ServerApp.port_retries=0 --no-browser
```

출력된 토큰을 사용해 PC 브라우저에서 `http://localhost:9999`에 접속한다. 토큰을 코드나 Git에 저장하지 않는다. `school_pdf/pc_collect.ipynb`를 열고 **Dofbot Pi (.venv)** 커널을 선택한 뒤 셀을 순서대로 실행한다.

수집 화면 링크 `http://localhost:18081`을 새 탭에서 열고 그 탭을 활성화한다. `http://<PI_LAN_IP>:8081`로 직접 열지 않는다. 서버는 loopback에만 바인딩하며, 브라우저 Gamepad API도 secure context가 필요하다. SSH 터널의 localhost 접속을 사용한다.

Jupyter 없이 실행하려면 Pi에서 아래 명령만 사용해도 같은 PC 입력 경로로 동작한다:

```bash
cd ~/dof-bot
.venv/bin/python school_pdf/data_collect.py --input pc
```

## 5. 입력 확인 → 수집

1. 게임패드 버튼을 한번 누른다. 브라우저에 게임패드 이름과 축/버튼 값이 나타나는지 확인한다. 한 개만 연결한다.
2. 영상 두 개가 나오는지 확인한다. 아직 팔은 움직이지 않는다.
3. **수집 시작**을 누르고 모든 스틱·버튼을 놓은 뒤 조작한다.
4. **START(브라우저 버튼 9)** 또는 화면 정지로 저장 종료한다. **SELECT(8)는 지정 초기 자세 이동**이다.
5. 서버를 종료하려면 노트북의 종료 셀 또는 터미널 Ctrl+C를 사용한다.

버튼 매핑은 [RUN_PI.md](RUN_PI.md)와 같다. 브라우저 표준 매핑은 Linux 번호와 다를 수 있으므로 처음에는 값 표시와 실제 버튼을 대응시키고 주변을 비운 상태에서 확인한다. 기대한 버튼과 다르면 본 수집을 시작하지 않는다.

브라우저가 게임패드를 읽지 못하면 Chrome/Edge에서 localhost 주소인지, 창이 활성 상태인지, 버튼을 한번 눌렀는지 확인한다. 동시에 여러 브라우저/탭에서 입력하지 않는다. 첫 입력 탭이 제어권을 가지며, 수집 중 다른 탭의 입력은 거부한다.

## Tailnet에서 SSH 터널 없이 HTTPS로 접속

수집 서버는 기본적으로 Pi의 `127.0.0.1:8081`에만 바인딩한다. 같은 Tailscale 네트워크에 있는 사용자가 브라우저에서 직접 열어야 한다면 Tailscale Serve로 HTTPS 프록시를 만든다. Serve는 Tailnet 안에서만 공유하며, 공개 인터넷 공유용 Funnel과 혼동하지 않는다.

Pi에서 한 번 설정한다.

```bash
sudo tailscale serve status
sudo tailscale serve --bg 8081
```

출력에 나온 `https://<장치>.<tailnet>.ts.net/` 주소를 같은 Tailnet의 브라우저에서 연다. 첫 실행에 Serve가 활성화되지 않았다고 나오면 안내된 Tailscale 관리자 페이지에서 HTTPS/Serve를 허용한 뒤 명령을 다시 실행한다. 주소를 `100.x` IP로 추측하지 말고 Serve가 출력한 HTTPS 이름을 사용한다. Tailscale Serve는 HTTPS를 제공해 브라우저 Gamepad API에 필요한 보안 출처도 충족한다.

`--bg` 설정은 재부팅 뒤에도 유지되지만, 수집 프로그램 자체는 다시 실행해야 한다. 공유를 해제할 때는 Pi에서 다음을 실행한다.

```bash
sudo tailscale serve --https=443 off
```

Tailnet Serve 상태는 다음으로 확인한다.

```bash
sudo tailscale serve status
```

## 연결이 끊기면

입력이 0.75초 이상 도착하지 않거나 게임패드가 분리되거나 탭이 비활성화되면 수집을 종료하고 새 모터 명령을 막는다. 주기적 피드백 읽기·파일 IO가 실행 중이면 상태 표시/종료 처리가 늦어질 수 있다. 모터 명령 직전에도 입력 유효성을 다시 검사한다. 이미 보낸 800ms 이동이나 SELECT의 2초 이동을 물리적으로 취소하는 비상정지는 아니다.

재연결만으로 자동 재개하지 않는다. 화면의 **수집 시작**을 다시 눌러 새 세션을 만든다. 다른 탭이 제어권을 가진 경우 먼저 정지하고 그 탭을 닫은 뒤 다시 시도한다.

기본 `--input pi` 모드는 기존 동작을 유지한다. PC 게임패드를 쓸 때 `--input pc`를 빠뜨리면 Pi의 js0을 찾으므로 실행에 실패할 수 있다.

## 저장과 검증 범위

저장 위치·CSV 의미·카메라 비동기성은 [RUN_PI.md](RUN_PI.md)를 따른다. PC로 입력만 옮겼으며 사진과 CSV는 Pi에 저장된다. 이 변경은 해당 다른 PC/유선 구성에서 아직 실물 검증하지 않았다. 자동 검사 결과와 실제 조작 성공을 구분한다.

참고: [브라우저 Gamepad API](https://developer.mozilla.org/en-US/docs/Web/API/Navigator/getGamepads).
