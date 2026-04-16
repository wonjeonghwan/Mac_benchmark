#!/usr/bin/env bash
# setup.sh — 최초 1회, 각 기기에서 실행.
# Python venv 생성 + requirements.txt 설치.
# 모델 다운로드는 ./pull_models.sh 에서 별도 수행.
#
# Python 3.10+ 가 필요. 다음 순서로 탐색:
#   1. uv  (추천: https://github.com/astral-sh/uv)
#   2. python3.13 / 3.12 / 3.11 / 3.10 명시 버전
#   3. python3  (단, --version 이 3.10 이상일 때만)

set -euo pipefail

cd "$(dirname "$0")"

# ── 1. macOS / Apple Silicon 확인 ──────────────────────────────────────
if [ "$(uname -s)" != "Darwin" ]; then
    echo "❌ macOS 가 아님. MLX 는 Apple Silicon 전용." >&2
    exit 1
fi
if [ "$(uname -m)" != "arm64" ]; then
    echo "❌ arm64 가 아님 ($(uname -m)). Apple Silicon 필요." >&2
    exit 1
fi
echo "✅ macOS $(sw_vers -productVersion) / Apple Silicon"

# ── 2. Python / 도구 탐색 ─────────────────────────────────────────────
USE_UV=0
PY_BIN=""

# uv 는 공식 설치 시 ~/.local/bin 에 놓이므로 PATH 에 없을 수 있음
UV_BIN=""
if command -v uv >/dev/null 2>&1; then
    UV_BIN=$(command -v uv)
elif [ -x "$HOME/.local/bin/uv" ]; then
    UV_BIN="$HOME/.local/bin/uv"
    export PATH="$HOME/.local/bin:$PATH"
fi

if [ -n "$UV_BIN" ]; then
    USE_UV=1
    echo "✅ uv 감지: $("$UV_BIN" --version)"
else
    for candidate in python3.13 python3.12 python3.11 python3.10; do
        if command -v "$candidate" >/dev/null 2>&1; then
            PY_BIN=$(command -v "$candidate")
            echo "✅ ${candidate} 감지: ${PY_BIN}"
            break
        fi
    done
    if [ -z "$PY_BIN" ]; then
        # 마지막: python3 의 버전 확인
        if command -v python3 >/dev/null 2>&1; then
            V=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
            MAJOR=$(python3 -c 'import sys; print(sys.version_info.major)')
            MINOR=$(python3 -c 'import sys; print(sys.version_info.minor)')
            if [ "$MAJOR" -ge 3 ] && [ "$MINOR" -ge 10 ]; then
                PY_BIN=$(command -v python3)
                echo "✅ python3 감지: ${PY_BIN} (${V})"
            fi
        fi
    fi
    if [ -z "$PY_BIN" ]; then
        cat <<'MSG' >&2
❌ Python 3.10 이상을 찾을 수 없음.

다음 중 하나를 설치하세요:
  (추천) uv 설치:
    curl -LsSf https://astral.sh/uv/install.sh | sh

  또는 Homebrew 로 Python 3.11+:
    brew install python@3.12

설치 후 이 스크립트를 다시 실행.
MSG
        exit 1
    fi
fi

# ── 3. venv 생성 ───────────────────────────────────────────────────────
if [ -d .venv ]; then
    echo "✅ .venv 이미 존재 (재사용)"
else
    echo "▶ .venv 생성 중..."
    if [ "$USE_UV" -eq 1 ]; then
        uv venv --python 3.11 .venv
    else
        "$PY_BIN" -m venv .venv
    fi
fi

# shellcheck disable=SC1091
source .venv/bin/activate

VENV_PY_VERSION=$(python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")')
echo "   venv Python: ${VENV_PY_VERSION}"

# ── 4. 의존성 설치 ─────────────────────────────────────────────────────
echo "▶ requirements.txt 설치 중..."
if [ "$USE_UV" -eq 1 ]; then
    uv pip install -r requirements.txt
else
    python -m pip install --upgrade pip --quiet
    pip install -r requirements.txt
fi

# ── 5. 확인 ─────────────────────────────────────────────────────────────
echo ""
echo "── 설치 확인 ──────────────────────────"
python -c "import mlx_lm; v=getattr(mlx_lm,'__version__','installed'); print(f'  mlx-lm      {v}')"
python -c "import httpx; print(f'  httpx       {httpx.__version__}')"
python -c "import huggingface_hub; print(f'  huggingface {huggingface_hub.__version__}')"
python -c "import pypdf; print(f'  pypdf       {pypdf.__version__}')"

# ── 6. 기기 정보 출력 (참고용) ──────────────────────────────────────────
RAM_BYTES=$(sysctl -n hw.memsize)
RAM_GB=$(( (RAM_BYTES + 1024*1024*1024/2) / (1024*1024*1024) ))
CHIP=$(sysctl -n machdep.cpu.brand_string)
DISK_FREE=$(df -h ~ | awk 'NR==2 {print $4}')
echo ""
echo "── 기기 정보 ──────────────────────────"
echo "  호스트명: $(hostname)"
echo "  칩:      ${CHIP}"
echo "  RAM:     ${RAM_GB}GB"
echo "  디스크:  ${DISK_FREE} 여유"

echo ""
echo "✅ 세팅 완료. 다음 단계:"
echo "   ./pull_models.sh      # 기기 티어에 맞는 모델만 HF 에서 다운로드"
echo "   ./start.sh            # 벤치마크 실행"
