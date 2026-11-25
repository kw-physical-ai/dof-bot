# 분리수거 시범 수집부터 학습까지

CSI를 주 영상으로 쓰는 3재질 모방학습 경로다. Pi는 조작·수집하고, 학습은 별도 PC/Mac 환경에서 한다. 손목 카메라는 그대로 두고 두 영상을 모두 보관한다. 먼저 CSI 모델을 학습하고 같은 시범 분할로 CSI+손목 모델과 비교한다.

현재 모델은 작은 CNN 기반 행동 복제(BC) 기준 모델이다. CSI의 초기 영상으로 재질을 분류하고, 선택한 재질·현재 실측각·영상으로 6개 관절의 `감소 / 유지 / 증가`를 예측한다. ACT나 학습 완료된 자동 집기 모델이 아니다. 한 시범의 대상 물체는 하나이며, 종이·플라스틱·비닐 중 한 재질이다. 자동 재질 인식용 초기 시야에는 분류 대상 물체를 하나만 둔다. 여러 물체 중 하나를 선택하고 6개를 연속 처리하는 검출·계획 기능은 이 모델에 포함하지 않는다.

## 1. 목적지 배치를 한 번 정하기

Pi에서 `school_pdf/task_config.json`을 편집한다. 각 `destination`을 실제 색종이 이름이나 목적지 이름으로 바꾼다. 세 이름은 달라야 한다. 실제 배치와 대조한 뒤에만 `mapping_confirmed`를 `true`로 바꾼다.

```json
{
  "version": 1,
  "layout_id": "layout-v1",
  "mapping_confirmed": true,
  "tasks": {
    "paper": {"destination": "paper_bin"},
    "plastic": {"destination": "plastic_bin"},
    "vinyl": {"destination": "vinyl_bin"}
  }
}
```

위 이름은 예시다. 실제 색종이 대응을 확인하지 않은 기본 파일은 `mapping_confirmed=false`라 수집 시작을 차단한다. 배치가 바뀌면 `layout_id`도 바꾼다. 서로 다른 목적지 배치의 데이터를 같은 학습에 자동으로 섞지 않는다. `vinyl`은 과제에서 부르는 비닐 분류다.

## 2. 실행과 수집

LAN 연결·터널·장치 점검은 [PC_LAN.md](school_pdf/PC_LAN.md)를 따른다. 제조사 앱과 수집기를 함께 실행하지 않는다. 알려진 제조사 앱은 Docker의 장치 경로 때문에 `fuser`에 빠질 수 있어 프로세스도 확인한다. 프로그램은 다른 앱을 자동 종료하거나 서보 토크를 자동 복구하지 않는다.

```bash
cd ~/dof-bot
.venv/bin/python -u school_pdf/data_collect.py --input pc
```

실행 시 기본자세 `[90,150,0,0,90,30]`로 움직인다. 전체 실측각이 목표의 ±3° 이내인지 확인해야 수집할 수 있다. 최근 1번 관절이 복귀를 따르지 않은 문제가 있었으므로 실제 이동도 확인한다. 목표 도달 실패를 무시하고 데이터를 모으지 않는다.

같은 PC에서 `http://localhost:18081`을 열고 다음 순서로 한다.

1. USB·CSI 영상과 게임패드 인식을 확인한다. 수집 중에는 이 창을 선택한 상태로 유지한다.
2. 이번 재질, 물체 ID, 시작 위치, 장면 ID, 조작자, 카메라 배치를 입력한다. 예를 들어 `paper / paper-01 / A1 / desk-01 / operator-a / fixed-v1`이다. 물체 ID는 같은 실물에 계속 같은 이름을 쓴다.
3. 물체 하나를 정한 시작 위치에 놓고 **기본자세 확인 후 수집 시작**을 누른다.
4. 스틱·버튼을 모두 놓고 **처음 약 2초는 물체가 CSI에 보이는 상태로 기다린다.** 이 초기 시야를 재질 분류 학습에 쓴다.
5. 집어서 정해진 목적지로 운반하고 놓는다. 끝난 뒤 START(9) 또는 화면 정지로 저장한다.
6. 실제로 올바른 목적지에 놓였으면 **성공**, 놓침·오배치면 **실패**, 복구·장비 문제면 **중단**을 누른다. 결과를 표시하지 않은 데이터는 학습에서 제외한다.

수집 시작마다 기본자세를 확인한다. SELECT로 복귀한 준비 세션은 종료하고 새 시범을 시작한다. 수집 중 다른 창으로 전환하거나 입력이 끊기면 재개 버튼을 다시 눌러야 한다.

이동은 실측각에서 3°·150ms씩 계획한다. 한 관절이 반복 명령 후에도 진행하지 않으면 해당 방향을 차단하고 다른 관절은 유지한다. 반대 방향으로 빠져나오는 조작은 허용한다. 1~5번의 이런 시범은 하드웨어 이상으로 표시해 성공을 눌러도 학습용으로 자동 채택하지 않는다. 집게(6번)는 물체 접촉 때문에 진행이 멈출 수도 있으므로 접촉/미진행으로 따로 표시하고 같은 방향을 차단한다. 접촉 성공이라고 자동 판단하지 않으며 실제 집기 결과는 사람이 검수한다. 이는 모터 온도·전류를 측정하는 진단이 아니며, 이미 전송한 이동을 취소하지 못한다.

## 3. 무엇을 얼마나 모을지

먼저 각 재질 2~3회씩 수집해 저장·검수·학습 명령이 이어지는지 확인한다. 이것은 동작 경로 확인용이지 충분한 학습량이 아니다.

첫 성능 실험은 **재질마다 성공 시범 20~30회**, 최소 3개 이상의 서로 다른 실물, 여러 시작 위치를 권한다. 이는 수집 계획의 시작점이며 성공률을 보장하는 수량은 아니다. 같은 물체를 같은 곳에서 반복한 프레임 수보다 독립된 물체·배치·시범 수가 중요하다.

CSI 카메라와 목적지는 고정한다. 조명이나 카메라 위치가 바뀌면 장면·카메라 배치 ID를 바꾼다. 평가용 실물은 학습용 실물과 분리한다. 데이터 변환은 동일 물체 ID와 복사된 시범이 학습·검증 양쪽에 들어가지 않게 묶는다. 일반 학습 내보내기는 세 재질 각각 학습·검증에 독립 물체 그룹이 있어야 한다.

## 4. 저장 형식과 검수

새 시범은 `school_pdf/datasets/dataset_.../`에 저장된다.

| 파일 | 의미 |
| --- | --- |
| `cam1/`, `cam2/`, `labels.csv`, `frames.csv` | 기존 호환 기록. 244×244 영상과 **명령 목표각** |
| `events/cam1/`, `events/cam2/` | 이동 직전의 원본 비율 영상. 손목/CSI 역할을 구분 |
| `steps.jsonl` | 영상 수신·실측각 읽기·관측·실제 명령의 시각과 방향. 실패/차단도 기록 |
| `meta.json` | v2 형식, 코드 SHA, 물체·배치, 설정, 완료/중단 상태와 장비 이상 |
| `review.json` | 사람이 확인한 성공/실패, 검수자, 원본 해시 |

실측각을 이전 목표각이나 0으로 채우지 않는다. 경계 추정값은 읽기 출처를 표시한다. 쓰기 함수가 반환됐다는 것만으로 실제 도달했다고 기록하지 않는다. 두 카메라는 호스트 수신 시각을 기록하며 동시 노출이 아니다.

```bash
# Pi에서 개별 시범의 무결성·학습 적합성 확인
.venv/bin/python -m school_pdf.dataset inspect school_pdf/datasets/dataset_실제이름

# 화면에서 결과를 표시하지 못했을 때. 실제 결과를 본 사람이 실행한다.
.venv/bin/python -m school_pdf.dataset review school_pdf/datasets/dataset_실제이름 \
  --result success --reviewer operator-a
```

프레임 수, 이미지 디코딩, 실제 관측→명령 순서, 피드백·영상의 시차, 완료 상태, 장비 이상을 검사한다. 같은 카메라 프레임이 반복된 샘플은 별도로 제외하며 수신 간격은 노출 동기화 증거가 아니다. 검수 후 원본이 바뀌면 다시 검수해야 한다.

기존 성공 시범은 지우지 않는다. 다만 v1에는 실측각과 명령 직전 관측이 없으므로 이번 **실측 관절 상태를 쓰는 모델의 학습 자료로 자동 변환하지 않는다.** v1도 이미지/CSV 무결성 점검과 성공 기록은 할 수 있다. 실측각을 만들어 넣는 변환은 제공하지 않는다.

## 5. 학습 PC 환경과 데이터 복사

학습용 환경은 Pi 수집 환경과 분리한다. Python 3.11에서 CPU/Mac MPS 경로를 검증했고 CUDA 선택 코드도 제공한다. NVIDIA GPU 실물 검증은 별도로 필요하다. 명시한 장치가 없으면 자동으로 숨겨서 대체하지 않고 오류를 낸다.

```bash
# Mac/학습 PC의 저장소에서
python3.11 -m venv .venv-train
.venv-train/bin/python -m pip install -r requirements-train.txt

mkdir -p training_data
rsync -av pi@<PI_ADDRESS>:~/dof-bot/school_pdf/datasets/ training_data/
```

기존 장비의 SSH 별칭이 필요하면 `rsync -av -e 'ssh -o HostKeyAlias=yahboom.local' ...`를 쓴다. Windows는 해당 환경의 가상환경 Python 경로를 사용하고 데이터 폴더를 복사한다. 원본 데이터, 모델과 내보내기는 `.gitignore`로 제외한다.

## 6. CSI 기준 모델 학습

```bash
.venv-train/bin/python -m school_pdf.dataset export training_data \
  --output training_exports/csi --camera-mode csi --seed 42

.venv-train/bin/python -m school_pdf.train train training_exports/csi/manifest.json \
  --output training_runs/csi --device auto --epochs 30 --batch-size 32

.venv-train/bin/python -m school_pdf.train evaluate \
  training_runs/csi/best.pt training_exports/csi/manifest.json --device auto
```

원본 검수 해시를 다시 확인하고 성공·완료·설정 일치 데이터만 내보낸다. 정규화는 SDK 범위를 사용하고, 클래스 가중치는 학습 분할에서만 계산한다. 검증은 물체/시범 단위로 분리하고 정지 동작만 예측하는 기준값도 함께 보고한다. 단순 전체 정확도 대신 행동 macro-F1로 체크포인트를 선택한다.

`metrics.json`에서 이동 중 관절 정확도, 행동 혼동행렬·macro-F1, 재질별/관절별 결과, 초기 CSI 영상의 시범 단위 재질 인식률을 확인한다. 이 수치는 저장된 관측에 대한 평가다. 실제 집기 성공률이나 6개 연속 처리 성공률이 아니다.

데이터가 적어 배관만 확인하려면 내보내기에 `--smoke-only`를 붙인다. 이때 `last.pt`만 저장하며 검증 점수·`best.pt`·일반화 성공을 만들지 않는다.

## 7. CSI+손목 비교와 예측 확인

```bash
.venv-train/bin/python -m school_pdf.dataset export training_data \
  --output training_exports/dual --camera-mode dual --seed 42
.venv-train/bin/python -m school_pdf.train train training_exports/dual/manifest.json \
  --output training_runs/dual --device auto --epochs 30 --batch-size 32

# 정답 재질을 조건으로 주는 정책의 저장 영상 예측
.venv-train/bin/python -m school_pdf.preview \
  training_runs/csi/best.pt training_exports/csi/manifest.json \
  --output artifacts/csi-preview.json --device auto

# 초기 CSI 영상에서 재질을 선택한 뒤 그 선택을 유지하는 경로
.venv-train/bin/python -m school_pdf.preview \
  training_runs/csi/best.pt training_exports/csi/manifest.json \
  --task auto --single-target --confidence-threshold 0.8 \
  --output artifacts/csi-auto-preview.json --device auto
```

자동 재질 선택은 초기 관측만 순서대로 모으고, 충분히 확신할 때 한 번 선택해 이후 시범 동안 고정한다. 초기 관측 중에는 이동을 제안하지 않는다. `--single-target`은 촬영한 장면에 대상이 하나라는 작업자 확인이며 물체 개수를 검출하는 기능이 아니다. 낮은 확신이면 `stop`을 기록한다. 높은 확신도 재질 정답·물리적 안전을 보장하지 않는다.

두 모델은 아래 명령으로 비교한다. 검증 물체·시범·샘플이 다르면 비교를 거부한다.

```bash
.venv-train/bin/python -m school_pdf.train compare \
  training_runs/csi/best.pt training_exports/csi/manifest.json \
  training_runs/dual/best.pt training_exports/dual/manifest.json --device auto
```

 한 카메라의 불량으로 채택된 시범이 달라지면 점수를 그대로 우열 비교하지 않는다. 기본 예측은 정답 재질을 주므로, 자동 재질 인식과는 별도 결과다.

`preview`는 JSON 파일에 후보 방향·각도·확신을 쓸 뿐 모터에 연결하지 않는다. 유지/경계 차단 관절은 `command_mask=false`, 후보 목표각은 `null`로 표시해 이동 명령과 구분한다. 예측이 좋아 보이면 그다음에 별도 감시 하의 느린 실물 시험, 처음 보는 실물·위치의 성공률, 세 재질 연속 처리, 6개 전체 완료 시간 순서로 검증한다. 현재 1번 관절의 반복 불응 원인은 해결 확인이 필요하며 이 검증을 코드 검사로 대신하지 않는다.

## 구현·검증 범위

수집기와 학습기는 로봇 SDK를 공유하지 않는다. 데이터 도구는 Torch 없이 실행된다. 모델 학습/평가/예측은 로봇 모듈을 import하지 않는다. 이벤트 기반 6개 관절의 3분류 행동 표현을 택해, 드문 영상 프레임에서 미래 실측각을 만들어 내거나 목표값을 관측으로 사용하는 오류를 피했다.

기본 테스트는 `python3 -m unittest discover -s tests`로 실행한다. 학습 환경에서는 `.venv-train/bin/python -m unittest discover -s tests -p 'test_training_pipeline.py'`로 실제 최적화·저장·재로드·평가·예측을 확인한다. 생성한 모의 데이터에서의 통과와 실제 분리수거 성능은 구분한다.
