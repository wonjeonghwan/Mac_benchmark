#!/usr/bin/env bash
# start.sh — 벤치마크 실행 진입점. '시작' 명령에 해당.
#
# 사용:
#   ./start.sh                      # 전체 실행 (Phase A + B, powermetrics 포함)
#   ./start.sh --no-power           # sudo 없이 실행 (전력 측정 스킵)
#   ./start.sh --phase a            # Phase A 만
#   ./start.sh --phase b            # Phase B 만
#   ./start.sh --model <경로>       # 특정 모델 1개만
#   ./start.sh --dry-run            # 실행 계획만 출력
#   ./start.sh --fresh              # 기존 jsonl 백업 후 처음부터
#   ./start.sh --think-mode off     # thinking 끄고 실행
#
# powermetrics 는 sudo 필요 → 기본적으로 sudo 로 감싸 실행.
# --no-power 가 인자에 있으면 sudo 없이 실행.

set -euo pipefail

cd "$(dirname "$0")"

# ── venv 확인 ──────────────────────────────────────────────────────────
if [ ! -d .venv ]; then
    echo "❌ .venv 없음. 먼저 ./setup.sh 를 실행하세요." >&2
    exit 1
fi

# ── --no-power / --dry-run 여부 감지 → sudo 필요 여부 결정 ────────────
NEEDS_SUDO=1
for arg in "$@"; do
    case "$arg" in
        --no-power|--dry-run|--finalize-only) NEEDS_SUDO=0 ;;
    esac
done

# ── venv 의 python 경로 (sudo 시 환경 변수 보존 안 되므로 절대 경로 사용) ─
VENV_PY="$(pwd)/.venv/bin/python"

if [ ! -x "$VENV_PY" ]; then
    echo "❌ .venv/bin/python 실행 불가. ./setup.sh 재실행하세요." >&2
    exit 1
fi

# ── 실행 ───────────────────────────────────────────────────────────────
if [ "$NEEDS_SUDO" -eq 1 ]; then
    echo "▶ powermetrics 포함 실행 → sudo 암호 요청됨 (처음 1회)"
    echo "  전력 측정 스킵하려면: ./start.sh --no-power"
    echo ""
    exec sudo -E "$VENV_PY" run_benchmark.py "$@"
else
    exec "$VENV_PY" run_benchmark.py "$@"
fi
