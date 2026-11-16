# 학교 방식 데이터 수집

학교 자료의 조작 방식과 이미지·목표각 CSV 형식을 바탕으로 수집 코드를 준비했다. 현재 실물에서 사용한 진입점은 **`data_collect.py`**다. 게임패드와 두 카메라를 모두 Pi에 연결한다.

- [실행 안내](RUN_PI.md): 설치 전제, 실행, 화면 접속, 조작, 저장 위치, 종료.
- [확인 기록](SESSION_NOTES.md): 실제 확인한 범위, 연결 문제, 카메라 시야와 데이터 해석.

| 파일 | 용도 |
| --- | --- |
| `data_collect.py` | Pi 게임패드 직접 입력, USB + OV5647 CSI 미리보기·수집 |
| `gamepad_check.py` | 모터·카메라를 열지 않는 게임패드 입력 확인 화면 |
| `data_collect.ipynb` | 학교 4번 PDF 12~17쪽 코드 화면을 옮긴 Jupyter 버전 |
| `arm_handle.ipynb` | 제조사 Docker의 `Dofbot/1.telecontrol/4.arm_handle/arm_handle.ipynb` 복사. 실행 출력은 제거 |

## 학교 노트북과 실행 스크립트의 차이

| 항목 | PDF 기반 노트북 | 현재 Python 스크립트 |
| --- | --- | --- |
| 게임패드 연결 | Jupyter 브라우저를 여는 PC | Pi USB |
| 카메라 | USB 하나, `VideoCapture(0)` | USB 고정 by-id 경로 + Picamera2 `csi0` |
| 시작 자세 | 첫 코드 셀에서 지정 자세로 자동 이동 | 현재 실측 자세에서 시작 |
| 저장 | cam1 + labels.csv | cam1 + cam2 + labels.csv + frames.csv + meta.json |
| 정지 | 버튼 9 | 버튼 9 또는 웹 정지 |
| 통신 확인 | 목표각 전송 | 초기 각도 읽기 + 수집 중 약 1초 간격 피드백 확인 |

`data_collect.ipynb`는 원본 수집 파일을 받은 것이 아니라 PDF 화면을 옮긴 코드다. 카메라 번호를 1에서 0으로 바꾸고 설명·셀 구분·출력 문구를 정리했다. 제어 구조와 저장 형식은 유지했다. PDF 자체는 업로드하지 않는다.

## Jupyter 참고 경로

학교 3번 자료는 Pi Docker에서 Jupyter를 실행하고 PC 브라우저로 접속하는 방식이다. 제조사 컨테이너 이름은 부팅마다 바뀔 수 있으므로 `docker ps`로 확인한다. Jupyter 실행 파일이 PATH에 없다면 컨테이너의 `/root/.local/bin/jupyter-lab`을 확인한다.

노트북은 팀에서 정한 호스트 공유 경로에 복사해 컨테이너가 바뀌어도 보존한다. 이번 장비에서는 `/home/pi/APP_DOFBOT` ↔ `/root/APP_DOFBOT` 기존 bind mount를 사용했다. Samba 설정과 Docker의 `/root/Dofbot`은 자동으로 같은 저장 위치가 되지 않는다.

PC에 게임패드를 연결하고 Controller 위젯부터 확인한다. 수집 노트북 첫 코드 셀은 즉시 `[92,77,11,18,99,91]`로 2초 이동하므로 실행 전 주변을 확인한다. 조작 노트북의 SELECT는 전체 90도 정렬, 수집 노트북의 SELECT는 위 지정 자세 이동이다. 두 제어 노트북·스크립트를 동시에 실행하지 않는다.

노트북 재수집 전 커널을 재시작한다. 원본 구조를 유지했으므로 카메라 실패 시 빈 데이터가 남을 수 있고, 제어 스레드 오류가 촬영 스레드를 자동 종료하지 않는다. PDF의 “빨간 경고 무시”는 `setDaemon` 사용 중단 예정 경고에 해당하며 모든 오류를 뜻하지 않는다.
