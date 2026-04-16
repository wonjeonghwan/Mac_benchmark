#!/usr/bin/env bash
# pull_models.sh — RAM 자동 감지 → 해당 티어 모델만 HuggingFace 사전 다운로드.
# 벤치마크 실행 전 모델을 미리 받아두면 TTFT/로딩 측정이 다운로드 시간에 오염되지 않음.
#
# 모델 목록은 run_benchmark.py 의 MODELS_BY_TIER / TENTATIVE_MODEL_PATHS 를 그대로 import.
# → "진실 원천 1개" 유지. 모델 추가/수정은 run_benchmark.py 에서만.
#
# 다운로드는 `huggingface_hub.snapshot_download` Python API 직접 호출 (CLI v0/v1 차이 회피).

set -eo pipefail   # -u 제거: 빈 배열 참조 시 unbound 오류 방지

cd "$(dirname "$0")"

# ── venv 확인 ──────────────────────────────────────────────────────────
if [ ! -d .venv ]; then
    echo "❌ .venv 없음. 먼저 ./setup.sh 를 실행하세요." >&2
    exit 1
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# ── HF 고속 다운로드 힌트 (hf_transfer 설치돼 있으면 활용) ──────────────
export HF_HUB_ENABLE_HF_TRANSFER=${HF_HUB_ENABLE_HF_TRANSFER:-1}

# ── run_benchmark.py 에서 티어 로직 재사용 (3줄 출력) ──────────────────
TIER_INFO=$(python3 - <<'PY'
from run_benchmark import detect_device, select_models, TENTATIVE_MODEL_PATHS
d = detect_device()
print(d.ram_gb)
print(" ".join(select_models(d.ram_gb)))
print(" ".join(sorted(TENTATIVE_MODEL_PATHS)))
PY
)
RAM_GB=$(echo "$TIER_INFO" | sed -n '1p')
MODELS_LINE=$(echo "$TIER_INFO" | sed -n '2p')
TENTATIVES_LINE=$(echo "$TIER_INFO" | sed -n '3p')

# shellcheck disable=SC2206
MODELS=( ${MODELS_LINE} )
# shellcheck disable=SC2206
TENTATIVES=( ${TENTATIVES_LINE} )

echo "── 티어 감지 ────────────────────────────"
echo "  호스트명: $(hostname)"
echo "  RAM:     ${RAM_GB}GB"
echo "  모델 수:  ${#MODELS[@]}"
echo ""
echo "── 다운로드 대상 ────────────────────────"
for m in "${MODELS[@]}"; do
    tag=""
    for t in "${TENTATIVES[@]}"; do
        if [ "$m" = "$t" ]; then tag="  ⚠ TENTATIVE (HF 경로 미확정)"; fi
    done
    echo "  • $m$tag"
done
echo ""

# 확인 (--yes 로 스킵 가능)
if [ "${1:-}" != "--yes" ] && [ "${1:-}" != "-y" ]; then
    read -r -p "계속 진행? [y/N] " ans
    case "$ans" in
        [yY]|[yY][eE][sS]) ;;
        *) echo "중단."; exit 0 ;;
    esac
fi

# ── 다운로드 루프 (Python 직접 호출) ──────────────────────────────────
# 각 모델 한 개씩 호출: 성공/실패/tentative-skip 판별
OK=()
SKIPPED=()
FAILED=()

for m in "${MODELS[@]}"; do
    echo ""
    echo "▶▶▶ $m"

    is_tentative=0
    for t in "${TENTATIVES[@]}"; do
        if [ "$m" = "$t" ]; then is_tentative=1; fi
    done

    # 종료 코드:
    #   0 = 성공
    #   2 = repo 없음 (tentative 면 skip, 아니면 실패)
    #   1 = 그 외 에러 (네트워크 등 → 실패)
    set +e
    MODEL_ID="$m" python3 - <<'PY'
import os, sys
repo = os.environ["MODEL_ID"]
try:
    from huggingface_hub import snapshot_download, repo_exists
except Exception as e:
    print(f"   huggingface_hub import 실패: {e}", file=sys.stderr)
    sys.exit(1)

# 1) 존재 확인 (없으면 조기 종료)
try:
    exists = repo_exists(repo)
except Exception as e:
    print(f"   repo_exists 확인 실패 (네트워크?): {e}", file=sys.stderr)
    sys.exit(1)
if not exists:
    print(f"   repo 없음: {repo}", file=sys.stderr)
    sys.exit(2)

# 2) 다운로드
try:
    path = snapshot_download(
        repo_id=repo,
        repo_type="model",
        # hf_transfer 가속 가능
    )
    print(f"   ✅ {path}")
except Exception as e:
    print(f"   snapshot_download 실패: {e}", file=sys.stderr)
    sys.exit(1)
PY
    ec=$?
    set -e

    case "$ec" in
        0)
            OK+=("$m")
            echo "   ✅ 완료"
            ;;
        2)
            if [ "$is_tentative" -eq 1 ]; then
                SKIPPED+=("$m")
                echo "   ⚠ 스킵 (TENTATIVE — mlx-community 에서 실제 repo 확인 후 run_benchmark.py MODELS_BY_TIER 수정)"
                echo "     검색: https://huggingface.co/mlx-community?search_models=${m##*/}"
            else
                FAILED+=("$m")
                echo "   ❌ 실패 (repo 없음)"
            fi
            ;;
        *)
            FAILED+=("$m")
            echo "   ❌ 실패 (종료코드 $ec)"
            ;;
    esac
done

# ── 요약 ───────────────────────────────────────────────────────────────
echo ""
echo "── 요약 ────────────────────────────────"
echo "  ✅ 성공 : ${#OK[@]}"
if [ "${#OK[@]}" -gt 0 ]; then
    for m in "${OK[@]}"; do echo "     • $m"; done
fi
if [ "${#SKIPPED[@]}" -gt 0 ]; then
    echo "  ⚠ 스킵 : ${#SKIPPED[@]} (TENTATIVE)"
    for m in "${SKIPPED[@]}"; do echo "     • $m"; done
fi
if [ "${#FAILED[@]}" -gt 0 ]; then
    echo "  ❌ 실패 : ${#FAILED[@]}"
    for m in "${FAILED[@]}"; do echo "     • $m"; done
    exit 1
fi

echo ""
echo "✅ 모든 모델 준비 완료. 다음: ./start.sh"
