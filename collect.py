#!/usr/bin/env python3
"""Run the usual dual-camera gamepad control and record one demonstration."""
import sys
from gamepad_control import main

if __name__ == '__main__':
    task = '분리수거 물체를 알맞은 위치로 옮겨라.'
    if sys.stdin.isatty():
        task = input('이번 시범의 지시문 (Enter=분리수거): ').strip() or task
    print('집고 들어 올린 뒤 모든 이동 입력을 놓고 START: 운반 구간 표시. SELECT: 종료.')
    raise SystemExit(main(collect_task=task))
