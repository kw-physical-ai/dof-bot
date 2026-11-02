# Dofbot 준비

카메라로 물체를 인식해 집는 기능을 개발합니다. 현재 저장소에는 환경 점검, 단일 관절 시험, [USB 조이스틱 제어](JOYSTICK.md)가 구현되어 있습니다. 조이스틱은 로컬 테스트까지 완료했으며 Pi 실물 동작 확인은 남아 있습니다. SSH로 확인한 장비 상태, 제어권 전환, 보정 및 구현 순서는 [개발 설계](DESIGN.md)를 따릅니다.

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
