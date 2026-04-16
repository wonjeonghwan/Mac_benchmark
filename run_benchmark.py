#!/usr/bin/env python3
"""
Local LLM Benchmark Runner — Apple Silicon / MLX

실행: sudo python3 run_benchmark.py [--phase a|b|all] [--model NAME]
                                    [--dry-run] [--no-power]
                                    [--fresh] [--port 8080]

설계 근거: CLAUDE.md, PLAN.md 참조.
- 런타임: MLX (mlx-lm). Ollama 미사용.
- **아키텍처: `mlx_lm.server` 1회 기동 → Phase A/B 모두 HTTP 로 통일.**
  모델당 로드 1회, OpenAI 호환 엔드포인트 (localhost:8080/v1).
- Phase A: L1~L4 프롬프트 순차 HTTP 스트림 (총 11 문항).
- Phase B: 같은 서버에 비동기 HTTP 버스트. 180초, L2_001 고정, max_tokens=200.
  - ≤40GB 모델: N 사이 서버 재기동
  - >40GB 모델: 60초 idle + /v1/cache/clear 시도
- 전력: powermetrics 서브프로세스 (sudo 필요. --no-power 로 스킵 가능).
- 재현성: temperature=0, seed=42.
- **재개 기본값**: 기존 jsonl 에 model_done 있는 모델 자동 스킵. --fresh 로 리셋.

thinking/answer 토큰 분리:
- Qwen3.5 계열의 <think>...</think> CoT 로 '정답까지 시간'이 크게 다름.
- total_output_tokens / thinking_tokens / answer_tokens / time_to_answer_ms 분리 측정.

결과 파일 (Level 2: 증분 저장 + 로그):
  results/{hostname}_{YYYYMMDD}.jsonl   이벤트 스트림 (append. 같은 날 재실행 누적)
  results/{hostname}_{YYYYMMDD}.log     사람이 읽을 로그 (타임스탬프, tail -f 가능)
  results/{hostname}_{YYYYMMDD}.json    최종 구조화 결과 (실행 종료 시 jsonl → 생성)
  results/{hostname}_{YYYYMMDD}.md      최종 요약 리포트
  results/raw/{model_safe}_{prompt_id}.md   장문 출력 (500 tok 초과)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

# =============================================================================
# 상수
# =============================================================================

ROOT = Path(__file__).parent
PROMPTS_DIR = ROOT / "prompts"
RESULTS_DIR = ROOT / "results"
# RAW_DIR 는 per-run 폴더가 도입되며 더 이상 단일 경로가 아님.
# save_raw_output 는 _RUN_RAW_DIR (main() 에서 설정) 을 사용.
_RUN_RAW_DIR: Optional[Path] = None

SEED = 42
TEMPERATURE = 0.0

# 티어별 모델 (PLAN.md §1) — 2026-04-15 Qwen3.5 로 전면 교체
# HF 경로 중 ⚠표시는 수동 확인 필요 (Qwen3-Coder-Next, Gemma-4 는 mlx-community 의
# 정확한 repo 명명 규칙을 pull 시점에 검증하고 필요하면 이 상수를 수정)
MODELS_BY_TIER: dict[int, list[str]] = {
    24: [
        "mlx-community/Qwen3.5-9B-MLX-4bit",              # ~5GB,  속도 상한
        "mlx-community/Qwen3.5-35B-A3B-4bit",             # ~20GB, MoE 주력
    ],
    32: [
        "mlx-community/Qwen3.5-9B-MLX-4bit",
        "mlx-community/Qwen3.5-35B-A3B-4bit",
        "mlx-community/Gemma-4-26B-4bit",                 # ~14GB, ⚠repo명 확인
        "mlx-community/Qwen3.5-27B-Claude-4.6-Opus-Distilled-MLX-4bit",  # ~15GB
    ],
    64: [
        "mlx-community/Qwen3.5-9B-MLX-4bit",
        "mlx-community/Qwen3.5-35B-A3B-4bit",
        "mlx-community/Gemma-4-26B-4bit",
        "mlx-community/Qwen3.5-27B-Claude-4.6-Opus-Distilled-MLX-4bit",
        "mlx-community/Qwen3-Coder-Next-80B-4bit",        # ~42GB, ⚠repo명 확인
    ],
    256: [
        "mlx-community/Qwen3.5-9B-MLX-4bit",
        "mlx-community/Qwen3.5-35B-A3B-4bit",
        "mlx-community/Gemma-4-26B-4bit",
        "mlx-community/Qwen3.5-27B-Claude-4.6-Opus-Distilled-MLX-4bit",
        "mlx-community/Qwen3-Coder-Next-80B-4bit",
        "mlx-community/Qwen3.5-397B-A17B-4bit",           # ~200GB, 플래그십 MoE
    ],
}

CONCURRENCY_LEVELS: list[int] = [1, 2, 4, 8, 16]
PHASE_B_DURATION_S: int = 180          # 모든 N, 모든 모델 공통
# Phase B max_tokens — think_mode 에 따라 동적 선택.
# off: 답만 생성. L2_001 실측 answer_tokens=255 → 2배 여유로 512 (200 이었을 때 잘림 확인).
# on:  thinking CoT 가 수백~수천 토큰 먹음. 실측 thinking 985 + answer 176 ≈ 1,161 → 2048 유지.
PHASE_B_MAX_TOKENS_OFF: int = 512      # think off: 답 + 계산 과정 전체 수용
PHASE_B_MAX_TOKENS_ON:  int = 2048     # think on : thinking + 답변 여유분
PHASE_B_PROMPT_ID: str = "L2_001"      # 고정 프롬프트
PHASE_B_PORT: int = 8080
EFFECTIVE_CONCURRENCY_CAP: int = 8     # 유효 동시 사용자 수 상한
LARGE_MODEL_GB_THRESHOLD: int = 40     # 이 이상이면 N 사이 재기동 대신 idle+cache_clear
PHASE_B_IDLE_BETWEEN_N_S: int = 60     # 대형 모델에서 N 사이 대기

# Thinking 모드: Qwen3 계열은 기본 thinking 사용. enable_thinking=False 로 비활성화.
# mlx_lm.server 는 payload.chat_template_kwargs 를 tokenizer.apply_chat_template 에 전달.
# 실행 순서: off 먼저 → on. 이유:
# (1) thinking 비활성이 빠르고 안정적 → 기기별 기본 성능 빠르게 확보
# (2) off 에서 서버 크래시하면 on 은 볼 필요 없이 다음 모델로
# (3) on 은 thinking 토큰 폭주로 시간 많이 먹으니 뒤로
THINK_MODES = ("off", "on")

# HF repo 경로가 추정값인 모델 (명명 규칙 미확정 → 실행 시점에 존재 여부 검증 + 안내)
# 이 집합의 모델이 HF 에 없으면 친절한 안내 로그 후 스킵.
TENTATIVE_MODEL_PATHS: set[str] = {
    "mlx-community/Gemma-4-26B-4bit",
    "mlx-community/Qwen3-Coder-Next-80B-4bit",
}

# 결과 저장 시 장문으로 간주할 토큰 임계 (PLAN.md §하이브리드)
RAW_TOKEN_THRESHOLD: int = 0


# =============================================================================
# 데이터 구조
# =============================================================================

@dataclass
class DeviceInfo:
    hostname: str
    ram_gb: int
    chip: str  # 예: "Apple M4"


@dataclass
class PromptItem:
    id: str
    level: int
    question: str
    expected: Optional[str]
    rubric: Optional[list[str]]
    max_tokens: int
    context: Optional[str]
    context_file: Optional[str] = None      # L3: prompts/contexts/*.md 경로
    context_size_tokens: Optional[int] = None  # L3 분량 힌트


@dataclass
class GenerationMetrics:
    """thinking / answer 분리 측정.

    <think>...</think> 태그가 있는 경우:
      - total_output_tokens = thinking + answer
      - time_to_answer_ms = </think> 이후 첫 토큰까지 (체감 TTFT)
    태그가 없으면 thinking_tokens=0, answer_tokens=total_output_tokens,
    time_to_answer_ms = ttft_ms (동일).
    """
    prompt_id: str
    level: int
    model: str
    ttft_ms: float                 # 첫 토큰까지 (thinking 시작)
    time_to_answer_ms: float       # </think> 이후 첫 답변 토큰까지
    total_ms: float                # 전체 생성 시간
    total_output_tokens: int       # thinking + answer
    thinking_tokens: int           # <think>...</think> 내부
    answer_tokens: int             # 사용자 의미 있는 답변만
    tps_total: float               # total_output_tokens / (total_ms - ttft_ms)
    tps_effective: float           # answer_tokens / (total_ms - time_to_answer_ms)
    raw_path: Optional[str]        # 장문 저장 경로 (None = 인라인)
    answer_text: Optional[str]     # thinking 제거한 답변 (RAW_TOKEN_THRESHOLD 이하만 인라인)
    full_text_in_raw: bool         # raw 파일에는 thinking 포함 전문 저장
    error: Optional[str] = None


@dataclass
class PhaseAResult:
    model: str
    items: list[GenerationMetrics] = field(default_factory=list)


@dataclass
class BurstSample:
    start_time: float
    ttft_ms: float
    total_ms: float
    output_tokens: int
    ok: bool
    error: Optional[str] = None


@dataclass
class BurstResult:
    n_concurrent: int
    duration_s: float           # 실제 관찰창 (~180)
    samples: list[BurstSample]
    aggregate_tps: float
    p50_ms: float
    p95_ms: float
    failure_rate: float


@dataclass
class PhaseBResult:
    model: str
    bursts: list[BurstResult] = field(default_factory=list)


@dataclass
class PowerSample:
    t: float          # epoch sec
    watts: float
    temp_c: Optional[float]


# =============================================================================
# 이벤트 로거 (Level 2: 증분 저장 + 로그)
# =============================================================================

class EventLogger:
    """3곳에 동시 기록:
    - stdout: 사람이 터미널에서 실시간 확인
    - .log 파일: 타임스탬프 포함, `tail -f` 로 확인 가능
    - .jsonl 파일: 기계 판독용. 이벤트 1개당 1줄. 크래시해도 여기까진 살아남음.

    이벤트 종류:
      run_start        실행 시작 (device, models, args)
      model_start      특정 모델 실행 시작
      model_path_error HF repo 존재하지 않음 (안내 URL 포함, 스킵)
      phase_a_item     Phase A 프롬프트 1개 완료 → GenerationMetrics
      phase_b_burst    Phase B N 레벨 1개 완료 → BurstResult 요약
      model_done       모델 1개 전체 완료
      model_error      모델 1개 실행 중 실패 (OOM 등) → 다음 모델로
      model_aborted    CTRL+C 로 중단된 모델 (--resume 시 재시도 대상)
      power_summary    전력 샘플 요약 (run 종료 시)
      run_done         실행 종료
    """

    def __init__(self, log_path: Path, jsonl_path: Path):
        self.log_path = log_path
        self.jsonl_path = jsonl_path
        self._log_f = None
        self._jsonl_f = None

    def __enter__(self):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_f = self.log_path.open("a", encoding="utf-8")
        self._jsonl_f = self.jsonl_path.open("a", encoding="utf-8")
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._log_f:
            self._log_f.close()
        if self._jsonl_f:
            self._jsonl_f.close()
        return False

    def _now(self) -> str:
        return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    def log(self, line: str) -> None:
        """stdout + .log 에 동시 출력 (타임스탬프 자동 prefix)."""
        ts = self._now()
        stamped = f"[{ts}] {line}"
        print(stamped, flush=True)
        if self._log_f:
            self._log_f.write(stamped + "\n")
            self._log_f.flush()

    def event(self, event_type: str, **fields) -> None:
        """.jsonl 에 이벤트 1줄 append + .log 에 요약 1줄.

        예: logger.event("phase_a_item", model=..., prompt_id=..., ttft_ms=..., ...)
        """
        record = {"ts": self._now(), "event": event_type, **fields}
        if self._jsonl_f:
            self._jsonl_f.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._jsonl_f.flush()
        # .log 요약은 이벤트별로 _format_for_log 에서 포맷
        summary = self._format_for_log(event_type, fields)
        if summary:
            self.log(summary)

    def _format_for_log(self, event: str, f: dict) -> Optional[str]:
        """이벤트 유형별 사람 친화적 한 줄 포맷."""
        if event == "run_start":
            return (
                f"=== run start ===  host={f.get('hostname')}  chip={f.get('chip')}  "
                f"ram={f.get('ram_gb')}GB  models={len(f.get('models', []))}"
            )
        if event == "model_start":
            tentative = " (TENTATIVE 경로)" if f.get("tentative") else ""
            modes = f.get("pending_modes") or []
            modes_part = f"  modes=[{','.join(modes)}]" if modes else ""
            return f"=== {f.get('model')}{tentative}{modes_part} ==="
        if event == "model_path_error":
            # 여러 줄 메시지 — guidance 는 이미 줄바꿈 포함
            return (
                f"[path-error] {f.get('model')}\n"
                f"  사유: {f.get('error')}\n"
                f"  {f.get('guidance', '')}"
            )
        if event == "phase_a_item":
            err = f.get("error")
            if err:
                return f"  {f.get('prompt_id'):>6}  [error] {err}"
            thinking = f.get("thinking_tokens", 0)
            thinking_part = f" (think={thinking})" if thinking else ""
            return (
                f"  {f.get('prompt_id'):>6}  "
                f"ttft={f.get('ttft_ms', 0):6.0f}ms  "
                f"t2a={f.get('time_to_answer_ms', 0):6.0f}ms  "
                f"tps={f.get('tps_total', 0):5.1f}  "
                f"ans={f.get('answer_tokens', 0)}{thinking_part}"
            )
        if event == "phase_b_burst":
            return (
                f"  N={f.get('n_concurrent'):2d}  "
                f"agg_tps={f.get('aggregate_tps', 0):6.1f}  "
                f"p50={f.get('p50_ms', 0):6.0f}ms  p95={f.get('p95_ms', 0):6.0f}ms  "
                f"fail={f.get('failure_rate', 0):.1%}  "
                f"samples={f.get('sample_count')}"
            )
        if event == "model_done":
            tm = f.get("think_mode", "on")
            return f"[done] {f.get('model')} think={tm}"
        if event == "model_aborted":
            tm = f.get("think_mode", "on")
            return f"[aborted] {f.get('model')} think={tm} (CTRL+C)"
        if event == "model_error":
            base = f"[error] {f.get('model')}: {f.get('error')}"
            if f.get("guidance"):
                base += f"\n  {f.get('guidance')}"
            return base
        if event == "power_summary":
            return (
                f"[power] avg={f.get('avg_watts', 0):.1f}W  "
                f"peak={f.get('peak_watts', 0):.1f}W  "
                f"peak_temp={f.get('peak_temp_c', 0):.1f}C"
            )
        if event == "run_done":
            return f"=== run done ({f.get('elapsed_s', 0):.0f}s) ==="
        return None


# =============================================================================
# 모델 경로 검증 (HF repo 존재 확인 + 사용자 안내)
# =============================================================================

def validate_model_path(path: str) -> tuple[bool, Optional[str]]:
    """HF Hub 에 repo 가 존재하는지 가볍게 확인 (다운로드 안 함).

    반환: (ok, note)
      ok = True 이면 진행 가능 (실제 존재함 OR 검증을 스킵함)
      ok = False 이면 존재하지 않음 (스킵하고 다음 모델로)
      note 는 사용자에게 보여줄 메시지 (None 이면 없음)
    """
    try:
        from huggingface_hub import repo_exists  # type: ignore
    except ImportError:
        return True, "huggingface_hub 미설치 → 경로 검증 스킵 (실패 시 mlx_lm load 에서 감지)"
    try:
        if repo_exists(path):
            return True, None
        return False, f"HF repo 존재하지 않음: {path}"
    except Exception as e:
        # 네트워크 오류 등: 검증 스킵하고 진행 시도
        return True, f"경로 검증 스킵 ({type(e).__name__})"


def hf_search_url(failed_path: str) -> str:
    """실패한 mlx-community/{name} 경로에서 모델 이름만 뽑아 HF 내부 검색 URL 생성.

    예: 'mlx-community/Gemma-4-26B-4bit' → 검색어 'Gemma 4 26B'
    """
    model_name = failed_path.rsplit("/", 1)[-1]
    # 공통 접미/토큰 제거해 핵심 이름만 남김
    tokens = [
        t for t in model_name.replace("-", " ").split()
        if t.lower() not in {"mlx", "4bit", "8bit", "q4", "q8"}
    ]
    query = "+".join(tokens) if tokens else model_name
    return f"https://huggingface.co/mlx-community?search={query}"


def build_path_error_guidance(failed_path: str, is_tentative: bool) -> str:
    """사용자에게 어디서 정확한 경로를 찾아야 하는지 안내하는 다줄 메시지."""
    url = hf_search_url(failed_path)
    if is_tentative:
        header = "이 경로는 추정값(TENTATIVE)입니다. HF 에서 실제 repo 확인 필요:"
    else:
        header = "이 경로가 유효하지 않습니다. HF 에서 정확한 repo 확인:"
    return (
        f"{header}\n"
        f"       검색: {url}\n"
        f"       mlx-community 전체: https://huggingface.co/mlx-community\n"
        f"       수정할 위치: run_benchmark.py 의 MODELS_BY_TIER / TENTATIVE_MODEL_PATHS\n"
        f"       이 모델은 스킵하고 다음 모델로 계속 진행합니다."
    )


# =============================================================================
# 기기 감지
# =============================================================================

def detect_device() -> DeviceInfo:
    """`sysctl hw.memsize`, `sysctl machdep.cpu.brand_string` 로 기기 정보 수집."""
    def _sysctl(key: str) -> str:
        return subprocess.check_output(
            ["sysctl", "-n", key], text=True
        ).strip()

    ram_bytes = int(_sysctl("hw.memsize"))
    ram_gb = round(ram_bytes / (1024 ** 3))
    try:
        chip = _sysctl("machdep.cpu.brand_string")
    except subprocess.CalledProcessError:
        chip = "Apple Silicon (unknown)"
    return DeviceInfo(
        hostname=socket.gethostname(),
        ram_gb=ram_gb,
        chip=chip,
    )


def select_models(ram_gb: int) -> list[str]:
    """RAM 에 해당하는 티어의 모델 목록 반환. 가까운 하위 티어로 스냅."""
    # TODO: 예) 22GB → 티어 24 선택 (정확히 24 이상이면 그 티어)
    for tier in sorted(MODELS_BY_TIER.keys()):
        if ram_gb <= tier:
            return MODELS_BY_TIER[tier]
    return MODELS_BY_TIER[max(MODELS_BY_TIER.keys())]


# =============================================================================
# 프롬프트 로드
# =============================================================================

_LEVEL_FILES = {
    1: "l1_recall.json",
    2: "l2_reasoning.json",
    3: "l3_longctx.json",
    4: "l4_complex.json",
}


def load_prompts() -> dict[int, list[PromptItem]]:
    """prompts/l*.json 4개 로드 → {level: [PromptItem,...]}.

    L3 는 context_file 필드가 있으면 prompts/{context_file} 을 읽어
    PromptItem.context 에 주입. 파일이 없으면 FileNotFoundError.
    """
    result: dict[int, list[PromptItem]] = {}
    for level, filename in _LEVEL_FILES.items():
        path = PROMPTS_DIR / filename
        data = json.loads(path.read_text(encoding="utf-8"))
        items: list[PromptItem] = []
        for raw in data["items"]:
            context_file = raw.get("context_file")
            context = raw.get("context")
            if context_file:
                ctx_path = PROMPTS_DIR / context_file
                if not ctx_path.exists():
                    raise FileNotFoundError(
                        f"L{level} 프롬프트 {raw['id']} 의 context_file 없음: {ctx_path}\n"
                        f"  tools/extract_samsung_pdf.py 를 먼저 실행해 생성하세요."
                    )
                context = ctx_path.read_text(encoding="utf-8")
            items.append(PromptItem(
                id=raw["id"],
                level=level,
                question=raw["question"],
                expected=raw.get("expected"),
                rubric=raw.get("rubric"),
                max_tokens=raw["max_tokens"],
                context=context,
                context_file=context_file,
                context_size_tokens=raw.get("context_size_tokens"),
            ))
        result[level] = items
    return result


def get_phase_b_prompt(prompts: dict[int, list[PromptItem]]) -> PromptItem:
    """PHASE_B_PROMPT_ID ('L2_001') 프롬프트 반환."""
    for item in prompts.get(2, []):
        if item.id == PHASE_B_PROMPT_ID:
            return item
    raise ValueError(
        f"Phase B 고정 프롬프트 {PHASE_B_PROMPT_ID} 를 L2 에서 찾지 못함"
    )


# =============================================================================
# 전력 모니터링
# =============================================================================

class PowerMonitor:
    """powermetrics 서브프로세스 래퍼.

    sudo 필요. plist 포맷 출력을 백그라운드 스레드로 파싱해 self.samples 에 누적.
    샘플: 1초마다 1건. CPU+GPU power(W), 최대 온도(°C).

    사용:
        with PowerMonitor(enabled=True) as pm:
            ... 벤치마크 ...
        # pm.samples 에 PowerSample 배열
    """

    def __init__(self, enabled: bool = True, interval_ms: int = 1000):
        self.enabled = enabled
        self.interval_ms = interval_ms
        self.samples: list[PowerSample] = []
        self._proc: Optional[subprocess.Popen] = None
        self._reader_thread = None
        self._stop_reader = False

    def __enter__(self):
        if not self.enabled:
            return self
        # sudo 권한 확인 (매번 암호 입력 정책)
        # smc 샘플러 추가 — thermal 만으로는 thermal_pressure 문자열만 나오고 실제 온도 숫자 없음
        cmd = [
            "sudo", "powermetrics",
            "--samplers", "cpu_power,gpu_power,thermal,smc",
            "-i", str(self.interval_ms),
            "-f", "plist",
        ]
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        import threading
        self._reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True,
        )
        self._reader_thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._stop_reader = True
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            except Exception:
                pass
        if self._reader_thread:
            self._reader_thread.join(timeout=3)
        return False

    def _reader_loop(self) -> None:
        """powermetrics plist 스트림 파싱. 각 plist 블록은 <?xml ... ?> ... </plist> 로 끝남.
        plist 블록 사이 \0 (NUL) 바이트로 구분.
        """
        import plistlib
        assert self._proc and self._proc.stdout
        buf = bytearray()
        while not self._stop_reader:
            chunk = self._proc.stdout.read(4096)
            if not chunk:
                break
            buf.extend(chunk)
            # plist 블록들을 NUL 로 분할 (powermetrics -f plist 는 블록마다 NUL 구분자 삽입)
            while b"\x00" in buf:
                idx = buf.index(b"\x00")
                block = bytes(buf[:idx])
                del buf[: idx + 1]
                self._parse_block(block)

    # 기기 TDP 상한 (파싱 오류로 튀는 값 필터링용)
    # M4 Mac mini ~30W, M4 Pro ~60W, M2 Ultra Mac Studio 최대 ~295W.
    # 여유 주고 500W 넘으면 단위 혼동 등 오류 판정 → 드롭.
    _MAX_REALISTIC_WATTS: float = 500.0

    @staticmethod
    def _extract_watts(data: dict) -> Optional[float]:
        """plist 에서 CPU+GPU 전력(W) 추출. 단위 혼동 방지.

        우선순위:
        1. processor.package_watts  (W 단위, macOS 14+)
        2. processor.combined_power (mW, 드문 변형)
        3. processor.cpu_power + processor.gpu_power (mW)

        단위 섞임 방지: `or` 로 필드 합치지 않고 명시적 분기.
        """
        proc = data.get("processor")
        if not isinstance(proc, dict):
            return None

        # (1) package_watts: macOS 14+ 에서 W 단위로 직접 제공
        pw = proc.get("package_watts")
        if isinstance(pw, (int, float)) and pw > 0:
            return float(pw)

        # (2) combined_power: 일부 버전에서 mW
        cp = proc.get("combined_power")
        if isinstance(cp, (int, float)) and cp > 0:
            v = float(cp)
            return v / 1000.0 if v > 100 else v  # 100W 넘으면 mW

        # (3) cpu_power + gpu_power: 둘 다 mW 가정
        cpu = proc.get("cpu_power")
        gpu = proc.get("gpu_power")
        if isinstance(cpu, (int, float)) and isinstance(gpu, (int, float)):
            total = float(cpu) + float(gpu)
            # 단위 판정: 합이 100 넘으면 mW, 그 이하는 이미 W (드문 경우)
            return total / 1000.0 if total > 100 else total

        return None

    @staticmethod
    def _extract_temp_c(data: dict) -> Optional[float]:
        """plist 에서 die/CPU 최대 온도(°C) 추출. smc 샘플러 필요.

        smc 리스트는 [{title, value, unit, ...}] 형태. unit='C' 이거나
        title 에 'temp'/'die' 가 포함된 센서만 필터. 10~150°C 범위만 채택.
        """
        smc = data.get("smc")
        if not isinstance(smc, list):
            smc = data.get("SMC") or []
        if not isinstance(smc, list):
            return None

        temps: list[float] = []
        for s in smc:
            if not isinstance(s, dict):
                continue
            val = s.get("value")
            if not isinstance(val, (int, float)):
                continue
            title = (s.get("title") or s.get("name") or "").lower()
            unit = (s.get("unit") or "").lower()
            is_temp = unit == "c" or "temp" in title or "die" in title or "tcal" in title
            if not is_temp:
                continue
            fv = float(val)
            if 10.0 < fv < 150.0:  # sane range, 파싱 오류(0, 999 등) 배제
                temps.append(fv)
        return max(temps) if temps else None

    def _parse_block(self, block: bytes) -> None:
        if not block.strip():
            return
        try:
            import plistlib
            data = plistlib.loads(block)
        except Exception:
            return
        if not isinstance(data, dict):
            return

        watts = self._extract_watts(data)
        # 기기 TDP 상한 넘으면 파싱 오류로 판정, 샘플 자체 드롭 (평균/peak 왜곡 방지)
        if watts is None or watts > self._MAX_REALISTIC_WATTS or watts < 0:
            return

        temp_c = self._extract_temp_c(data)

        self.samples.append(PowerSample(
            t=time.time(),
            watts=watts,
            temp_c=temp_c,
        ))


# =============================================================================
# HTTP 스트림 + 토큰 분리 (Phase A/B 공통)
# =============================================================================
#
# 설계: mlx_lm.server 에 OpenAI 호환 /v1/chat/completions 스트림 요청.
# 응답을 SSE 로 받아 토큰 단위로 시각 기록.
# <think>...</think> 태그가 있으면 thinking / answer 구간 분리.
# =============================================================================


_THINK_BLOCK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)
_THINK_CLOSE_TAG = "</think>"


def split_thinking_and_answer(full_text: str) -> tuple[str, str]:
    """<think>...</think> 태그로 thinking 과 answer 분리.

    태그 없으면 (thinking='', answer=full_text) 반환.
    여러 think 블록이 있으면 전부 합쳐 thinking, 제거된 나머지가 answer.
    """
    thinking_parts = _THINK_BLOCK_RE.findall(full_text)
    if not thinking_parts:
        return "", full_text.strip()
    thinking = "\n".join(t.strip() for t in thinking_parts)
    answer = _THINK_BLOCK_RE.sub("", full_text).strip()
    return thinking, answer


def _build_messages(prompt: PromptItem) -> list[dict]:
    """OpenAI 호환 messages 배열 생성. L3 는 context 를 system 으로 주입."""
    messages: list[dict] = []
    if prompt.context:
        messages.append({
            "role": "system",
            "content": (
                "다음 문서를 참고해 이어지는 질문에 답하세요.\n\n"
                f"---\n{prompt.context}\n---"
            ),
        })
    messages.append({"role": "user", "content": prompt.question})
    return messages


async def send_one_request(
    client,            # httpx.AsyncClient
    port: int,
    model: str,
    prompt: PromptItem,
    max_tokens: int,
    think_mode: str = "on",           # "on" | "off"
    save_raw: bool = True,  # False: Phase B 처럼 집계만 (raw 저장 생략)
) -> GenerationMetrics:
    """단일 프롬프트 스트림 요청 → GenerationMetrics. Phase A/B 공통.

    mlx_lm.server 는 OpenAI 호환 변형으로, thinking 을 `delta.reasoning` 필드에,
    최종 답변은 `delta.content` 필드에 분리해 보낸다. 일반 OpenAI 호환 모델
    (content만 보냄)도 같이 지원.
    """
    payload = {
        "model": model,
        "messages": _build_messages(prompt),
        "temperature": TEMPERATURE,
        "seed": SEED,
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    # thinking 비활성화는 Qwen3 공식 방식: chat_template_kwargs.enable_thinking=False
    # mlx_lm.server 가 이를 tokenizer.apply_chat_template 에 전달.
    if think_mode == "off":
        payload["chat_template_kwargs"] = {"enable_thinking": False}

    url = f"http://127.0.0.1:{port}/v1/chat/completions"
    t0 = time.perf_counter()
    ttft: Optional[float] = None
    t_answer_start: Optional[float] = None
    thinking_chunks: list[str] = []
    answer_chunks: list[str] = []
    usage_tokens: Optional[int] = None

    try:
        async with client.stream("POST", url, json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    obj = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                # usage (include_usage 요청 시 마지막 청크에 옴)
                usage = obj.get("usage")
                if usage:
                    usage_tokens = usage.get("completion_tokens") or usage_tokens

                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                reasoning = delta.get("reasoning")
                content = delta.get("content")
                if not reasoning and not content:
                    continue

                now = time.perf_counter()
                if ttft is None:
                    ttft = now - t0

                if reasoning:
                    thinking_chunks.append(reasoning)
                if content:
                    if t_answer_start is None:
                        t_answer_start = now
                    answer_chunks.append(content)
    except Exception as e:
        t_end = time.perf_counter()
        return GenerationMetrics(
            prompt_id=prompt.id,
            level=prompt.level,
            model=model,
            ttft_ms=(ttft or 0) * 1000,
            time_to_answer_ms=((t_answer_start or ttft or 0)) * 1000,
            total_ms=(t_end - t0) * 1000,
            total_output_tokens=0,
            thinking_tokens=0,
            answer_tokens=0,
            tps_total=0.0,
            tps_effective=0.0,
            raw_path=None,
            answer_text=None,
            full_text_in_raw=False,
            error=f"{type(e).__name__}: {e}",
        )

    t_end = time.perf_counter()
    thinking_text = "".join(thinking_chunks)
    answer_text = "".join(answer_chunks)

    # thinking 이 reasoning 필드로 안 오고 answer 안에 <think> 태그로 들어간 경우(구 서버/타 모델 대응)
    if not thinking_text and answer_text and "<think>" in answer_text:
        thinking_text, answer_text = split_thinking_and_answer(answer_text)

    full_text = (
        (f"<think>\n{thinking_text}\n</think>\n\n" if thinking_text else "") + answer_text
    )

    # 토큰 수 산출: usage 의 completion_tokens 를 글자수 비율로 thinking/answer 분배
    total_tokens = usage_tokens if usage_tokens is not None else max(len(full_text) // 3, 1)
    if thinking_text and answer_text:
        total_chars = len(thinking_text) + len(answer_text)
        thinking_tokens = int(total_tokens * len(thinking_text) / total_chars)
        answer_tokens = total_tokens - thinking_tokens
    elif thinking_text:
        thinking_tokens = total_tokens
        answer_tokens = 0
    else:
        thinking_tokens = 0
        answer_tokens = total_tokens

    total_s = t_end - t0
    gen_duration = max(total_s - (ttft or 0), 1e-6)
    answer_ref_time = t_answer_start if t_answer_start else (ttft or 0) + t0
    # answer_ref_time 이 언제냐: t_answer_start 는 perf_counter 절대값, ttft 는 상대값
    # 정리: t_answer_start 는 perf_counter 로 찍혔으니 절대값. 지속시간 계산:
    if t_answer_start is not None:
        answer_duration = max(t_end - t_answer_start, 1e-6)
    else:
        answer_duration = gen_duration  # 답변 구간 없으면 전체 = thinking 만
    tps_total = total_tokens / gen_duration
    tps_effective = answer_tokens / answer_duration if answer_tokens else 0.0

    # raw 저장 판단 (장문이거나 thinking 이 있으면 전문 보존)
    raw_path: Optional[str] = None
    answer_inline: Optional[str] = answer_text if answer_text else None
    full_text_in_raw = False
    if save_raw and (answer_tokens > RAW_TOKEN_THRESHOLD or thinking_tokens > 0):
        raw_path = save_raw_output(model, prompt.id, full_text, think_mode=think_mode)
        if answer_tokens > RAW_TOKEN_THRESHOLD:
            answer_inline = None
        full_text_in_raw = True

    return GenerationMetrics(
        prompt_id=prompt.id,
        level=prompt.level,
        model=model,
        ttft_ms=(ttft or 0) * 1000,
        time_to_answer_ms=(
            (t_answer_start - t0) if t_answer_start is not None else (ttft or 0)
        ) * 1000,
        total_ms=total_s * 1000,
        total_output_tokens=total_tokens,
        thinking_tokens=thinking_tokens,
        answer_tokens=answer_tokens,
        tps_total=tps_total,
        tps_effective=tps_effective,
        raw_path=raw_path,
        answer_text=answer_inline,
        full_text_in_raw=full_text_in_raw,
    )


def _safe_model_name(model: str) -> str:
    return model.replace("/", "_").replace(":", "_")


def save_raw_output(model: str, prompt_id: str, full_text: str, think_mode: str = "on") -> str:
    """현재 실행의 raw/ 폴더 (per-run) 에 전문(thinking 포함) 저장.

    main() 이 _RUN_RAW_DIR 를 설정. 미설정 시(테스트 등) results/raw 폴백.
    """
    raw_dir = _RUN_RAW_DIR if _RUN_RAW_DIR is not None else (RESULTS_DIR / "raw")
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f"{_safe_model_name(model)}_{think_mode}_{prompt_id}.md"
    header = (
        f"<!-- model: {model} -->\n"
        f"<!-- think_mode: {think_mode} -->\n"
        f"<!-- prompt_id: {prompt_id} -->\n"
        f"<!-- saved: {datetime.now().isoformat()} -->\n\n"
    )
    path.write_text(header + full_text, encoding="utf-8")
    return str(path.relative_to(ROOT))


# =============================================================================
# Phase A (품질/속도)
# =============================================================================

async def run_phase_a(
    model: str,
    prompts: dict[int, list[PromptItem]],
    port: int,
    logger: EventLogger,
    think_mode: str = "on",
    dry_run: bool = False,
) -> PhaseAResult:
    """레벨별(L1→L2→L3→L4) 순차 HTTP 스트림. 서버는 이미 기동된 상태 가정.

    프롬프트 1개 완료마다 logger.event('phase_a_item') 로 증분 저장.
    thinking/answer 토큰 분리 값도 이벤트에 포함.
    """
    import httpx  # type: ignore
    result = PhaseAResult(model=model)
    async with httpx.AsyncClient(timeout=600.0) as client:
        consecutive_conn_errors = 0
        for level in (1, 2, 3, 4):
            for item in prompts[level]:
                if dry_run:
                    logger.log(
                        f"  [dry] {model} mode={think_mode} {item.id}  max_tokens={item.max_tokens}"
                    )
                    continue
                metrics = await send_one_request(
                    client, port, model, item,
                    max_tokens=item.max_tokens,
                    think_mode=think_mode,
                )
                result.items.append(metrics)
                logger.event(
                    "phase_a_item",
                    model=model,
                    think_mode=think_mode,
                    prompt_id=metrics.prompt_id,
                    level=metrics.level,
                    ttft_ms=metrics.ttft_ms,
                    time_to_answer_ms=metrics.time_to_answer_ms,
                    total_ms=metrics.total_ms,
                    total_output_tokens=metrics.total_output_tokens,
                    thinking_tokens=metrics.thinking_tokens,
                    answer_tokens=metrics.answer_tokens,
                    tps_total=metrics.tps_total,
                    tps_effective=metrics.tps_effective,
                    raw_path=metrics.raw_path,
                    error=metrics.error,
                )
                # Fail-fast: ConnectError 2건 연속이면 서버 health 확인 → 죽었으면 중단
                err_str = (metrics.error or "").lower()
                if "connect" in err_str or "connectionerror" in err_str:
                    consecutive_conn_errors += 1
                    if consecutive_conn_errors >= 2:
                        if not is_server_alive(port, timeout=3.0):
                            raise ServerDeadError(
                                f"서버 사망 감지 (Phase A, {consecutive_conn_errors}회 연속 ConnectError)"
                            )
                else:
                    consecutive_conn_errors = 0
    return result


# =============================================================================
# Phase B (동시 사용자)
# =============================================================================

SERVER_STARTUP_TIMEOUT_S: int = 900  # 397B 대응 (로드만 ~10분)


class ServerDeadError(RuntimeError):
    """벤치 도중 mlx_lm.server 프로세스가 죽었음을 나타내는 예외.

    main() 은 이를 잡아 `model_server_dead` 이벤트 기록 후
    해당 모델의 나머지 (mode × phase) 를 건너뛰고 다음 모델로 진행.
    """


def is_server_alive(port: int, timeout: float = 3.0) -> bool:
    """빠른 health check: /v1/models 200 이면 살아있음. 네트워크 오류/타임아웃이면 죽음."""
    import urllib.request
    import urllib.error
    url = f"http://127.0.0.1:{port}/v1/models"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def start_mlx_server(
    model: str,
    port: int = PHASE_B_PORT,
    log_path: Optional[Path] = None,
) -> subprocess.Popen:
    """`mlx_lm.server` 기동 후 /v1/models 응답까지 polling.

    mlx_lm.server 는 첫 요청 시 모델을 lazy load 하는 경우가 많으므로
    /v1/models 는 금방 200 으로 응답할 수 있다. 실제 로드는 첫 completion 에서 발생.
    여기서는 프로세스 기동 + 포트 바인딩까지만 확인한다.

    Args:
        log_path: 지정 시 서버 stdout/stderr 를 이 파일로 tee.
                  OOM/크래시 원인 진단용.
    """
    # venv python 우선, 없으면 mlx_lm.server 직접
    venv_python = ROOT / ".venv" / "bin" / "python"
    cmd_python = str(venv_python) if venv_python.exists() else sys.executable
    cmd = [
        cmd_python, "-m", "mlx_lm.server",
        "--model", model,
        "--port", str(port),
        "--host", "127.0.0.1",
    ]

    # stdout/stderr 를 파일로 직접 리다이렉트 (파이프 버퍼에 갇혀 로그 소실 방지)
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(log_path, "wb")
        proc = subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
    else:
        # 파이프 모드 (후방 호환). 크래시 시 버퍼 갇힘 가능.
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    # /v1/models polling
    deadline = time.time() + SERVER_STARTUP_TIMEOUT_S
    last_err: Optional[str] = None
    while time.time() < deadline:
        if proc.poll() is not None:
            # 서버가 죽었음
            out = ""
            if log_path is not None and log_path.exists():
                try:
                    out = log_path.read_text(errors="replace")
                except Exception:
                    pass
            elif proc.stdout is not None:
                try:
                    out = proc.stdout.read() or ""
                except Exception:
                    pass
            raise RuntimeError(
                f"mlx_lm.server 프로세스가 기동 중 종료됨 (exit={proc.returncode}).\n"
                f"로그 꼬리:\n{out[-2000:]}"
            )
        if is_server_alive(port, timeout=2.0):
            return proc
        last_err = "not ready yet"
        time.sleep(1.0)
    # 타임아웃
    stop_mlx_server(proc)
    raise TimeoutError(
        f"mlx_lm.server health check 실패 ({SERVER_STARTUP_TIMEOUT_S}s 초과). "
        f"마지막 에러: {last_err}"
    )


def stop_mlx_server(proc: subprocess.Popen) -> None:
    """SIGTERM → 10초 grace → SIGKILL. 포트 해제 대기."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def model_size_gb(model_path: str) -> Optional[int]:
    """모델 파일 크기 추정. HF 캐시 디렉토리 크기 합산. 없으면 None."""
    # model_path: "mlx-community/Qwen3.5-9B-MLX-4bit"
    # HF 캐시: ~/.cache/huggingface/hub/models--{org}--{name}/
    try:
        org, name = model_path.split("/", 1)
    except ValueError:
        return None
    cache_dir = Path.home() / ".cache" / "huggingface" / "hub" / f"models--{org}--{name}"
    if not cache_dir.exists():
        return None
    total = 0
    for p in cache_dir.rglob("*"):
        if p.is_file() and not p.is_symlink():
            try:
                total += p.stat().st_size
            except OSError:
                pass
        elif p.is_symlink():
            try:
                target = p.resolve()
                if target.is_file():
                    total += target.stat().st_size
            except OSError:
                pass
    if total == 0:
        return None
    return max(1, round(total / (1024 ** 3)))


async def cache_clear_via_http(client, port: int) -> bool:
    """대형 모델용: /v1/cache/clear 호출 시도. 미지원이면 False 반환."""
    try:
        resp = await client.post(
            f"http://127.0.0.1:{port}/v1/cache/clear",
            timeout=10.0,
        )
        return resp.status_code == 200
    except Exception:
        return False


async def burst_test(
    n_concurrent: int,
    duration_s: int,
    prompt: PromptItem,
    model: str,
    port: int,
    think_mode: str = "on",
) -> BurstResult:
    """N 병렬로 duration_s 동안 계속 요청 발사. p50/p95/실패율 집계.

    구조:
    - N 개 워커가 병렬로 루프 돌면서 다음 요청을 발사
    - 각 워커는 이전 응답 완료 후 바로 다음 요청 (세마포어 불필요, 각자 직렬)
    - 시계가 end 넘으면 워커가 자연 종료
    """
    import httpx  # type: ignore
    samples: list[BurstSample] = []
    t_start = time.time()
    end_time = t_start + duration_s

    # think 모드에 따라 max_tokens 동적 결정 (thinking 이 200 토큰에 잘리지 않게)
    phase_b_max_tokens = (
        PHASE_B_MAX_TOKENS_ON if think_mode == "on" else PHASE_B_MAX_TOKENS_OFF
    )

    async def worker(worker_id: int):
        async with httpx.AsyncClient(timeout=600.0) as client:
            while time.time() < end_time:
                t_req_start = time.time()
                metrics = await send_one_request(
                    client, port, model, prompt,
                    max_tokens=phase_b_max_tokens,
                    think_mode=think_mode,
                    save_raw=False,
                )
                samples.append(BurstSample(
                    start_time=t_req_start,
                    ttft_ms=metrics.ttft_ms,
                    total_ms=metrics.total_ms,
                    output_tokens=metrics.total_output_tokens,
                    ok=metrics.error is None,
                    error=metrics.error,
                ))

    await asyncio.gather(*[worker(i) for i in range(n_concurrent)])

    actual_duration = time.time() - t_start
    ok_samples = [s for s in samples if s.ok]
    failure_rate = (len(samples) - len(ok_samples)) / max(len(samples), 1)
    total_tokens = sum(s.output_tokens for s in ok_samples)
    aggregate_tps = total_tokens / actual_duration if actual_duration > 0 else 0.0

    latencies = sorted(s.total_ms for s in ok_samples)

    def _pct(lst: list[float], q: float) -> float:
        if not lst:
            return 0.0
        idx = min(int(len(lst) * q), len(lst) - 1)
        return lst[idx]

    return BurstResult(
        n_concurrent=n_concurrent,
        duration_s=actual_duration,
        samples=samples,
        aggregate_tps=aggregate_tps,
        p50_ms=_pct(latencies, 0.50),
        p95_ms=_pct(latencies, 0.95),
        failure_rate=failure_rate,
    )


async def run_phase_b(
    model: str,
    phase_b_prompt: PromptItem,
    port: int,
    logger: EventLogger,
    server_manager,  # 재기동 전략 주입 (아래 ServerManager 참고)
    think_mode: str = "on",
    dry_run: bool = False,
) -> PhaseBResult:
    """N = 1/2/4/8/16 순차 실행. 각 180초.

    N 사이 캐시 정리 정책:
    - 모델 크기 ≤ LARGE_MODEL_GB_THRESHOLD: 서버 재기동 (server_manager.restart())
    - 모델 크기 >  LARGE_MODEL_GB_THRESHOLD: PHASE_B_IDLE_BETWEEN_N_S 초 대기 + cache_clear 시도

    N 레벨 1개 완료마다 logger.event('phase_b_burst') 로 증분 저장.
    """
    result = PhaseBResult(model=model)
    if dry_run:
        for n in CONCURRENCY_LEVELS:
            logger.log(f"  [dry] {model}  N={n}  duration={PHASE_B_DURATION_S}s")
        return result

    import httpx  # type: ignore
    size_gb = model_size_gb(model) or 999  # 알 수 없으면 보수적으로 대형
    is_large = size_gb > LARGE_MODEL_GB_THRESHOLD

    async with httpx.AsyncClient(timeout=600.0) as client:
        for i, n in enumerate(CONCURRENCY_LEVELS):
            # 첫 N 전에는 재기동/대기 불필요 (Phase A 끝난 상태)
            if i > 0:
                if is_large:
                    logger.log(f"  [idle {PHASE_B_IDLE_BETWEEN_N_S}s + cache_clear]")
                    await asyncio.sleep(PHASE_B_IDLE_BETWEEN_N_S)
                    await cache_clear_via_http(client, port)
                else:
                    logger.log(f"  [server restart between N]")
                    try:
                        server_manager.restart()  # 재기동 후 health check
                    except Exception as e:
                        raise ServerDeadError(
                            f"Phase B N={n} 직전 서버 재기동 실패: {e}"
                        )

            # 각 N 시작 전 서버 살아있는지 한 번 더 확인 (재기동 직후이지만 안전장치)
            if not is_server_alive(port, timeout=3.0):
                raise ServerDeadError(
                    f"Phase B N={n} 시작 직전 서버 사망 감지"
                )

            burst = await burst_test(
                n, PHASE_B_DURATION_S, phase_b_prompt, model, port,
                think_mode=think_mode,
            )
            result.bursts.append(burst)
            logger.event(
                "phase_b_burst",
                model=model,
                think_mode=think_mode,
                n_concurrent=burst.n_concurrent,
                duration_s=burst.duration_s,
                aggregate_tps=burst.aggregate_tps,
                p50_ms=burst.p50_ms,
                p95_ms=burst.p95_ms,
                failure_rate=burst.failure_rate,
                sample_count=len(burst.samples),
            )

            # 이 N 의 실패율이 90% 넘으면 서버 확인 후 죽었으면 중단
            if burst.failure_rate >= 0.90:
                if not is_server_alive(port, timeout=3.0):
                    raise ServerDeadError(
                        f"Phase B N={n} 종료 후 서버 사망 감지 (fail_rate={burst.failure_rate:.1%})"
                    )
    return result


class ServerManager:
    """모델 한 개의 서버 생명주기 관리. Phase B N 사이 재기동 책임.

    사용:
        with ServerManager(model, port, log_dir=...) as sm:
            await run_phase_a(model, prompts, port, logger)
            await run_phase_b(model, p_b_prompt, port, logger, sm)
    """

    def __init__(self, model: str, port: int, log_dir: Optional[Path] = None):
        self.model = model
        self.port = port
        self.log_dir = log_dir
        self.proc: Optional[subprocess.Popen] = None
        self._restart_counter = 0

    def _log_path(self) -> Optional[Path]:
        if self.log_dir is None:
            return None
        safe = _safe_model_name(self.model)
        suffix = f"_restart{self._restart_counter}" if self._restart_counter > 0 else ""
        return self.log_dir / f"server_{safe}{suffix}.log"

    def __enter__(self):
        self.proc = start_mlx_server(self.model, self.port, log_path=self._log_path())
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.proc:
            stop_mlx_server(self.proc)
        return False

    def restart(self):
        """현재 서버 종료 후 같은 모델로 재기동 (Phase B 캐시 초기화용)."""
        if self.proc:
            stop_mlx_server(self.proc)
        self._restart_counter += 1
        self.proc = start_mlx_server(self.model, self.port, log_path=self._log_path())

    def is_alive(self) -> bool:
        """현재 서버 프로세스 + 포트 health."""
        if self.proc is None or self.proc.poll() is not None:
            return False
        return is_server_alive(self.port, timeout=2.0)


# =============================================================================
# 결과 저장
# =============================================================================

def finalize_results(
    jsonl_path: Path,
    device: DeviceInfo,
    power_samples: list[PowerSample],
) -> tuple[Path, Path]:
    """jsonl 을 읽어 최종 .json + .md 생성. 독립적으로 재실행 가능."""
    if not jsonl_path.exists():
        raise FileNotFoundError(f"jsonl 파일 없음: {jsonl_path}")

    # jsonl 파싱
    events: list[dict] = []
    with jsonl_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    # (model, think_mode) 쌍별 집계. 과거 이벤트(think_mode 없음)는 'on' 으로 간주.
    by_pair: dict[tuple[str, str], dict] = {}

    def pair_entry(m: str, tm: str) -> dict:
        key = (m, tm)
        if key not in by_pair:
            by_pair[key] = {
                "model": m,
                "think_mode": tm,
                "phase_a_items": [],
                "phase_b_bursts": [],
                "status": "pending",  # done / error / aborted / path_error
                "error": None,
                "tentative": False,
            }
        return by_pair[key]

    for ev in events:
        et = ev.get("event")
        m = ev.get("model")
        tm = ev.get("think_mode", "on")
        if not m:
            continue
        if et == "model_start":
            # pending_modes 가 있으면 각각 항목 생성
            for pm_mode in (ev.get("pending_modes") or [tm]):
                e = pair_entry(m, pm_mode)
                e["tentative"] = ev.get("tentative", False)
        elif et == "phase_a_item":
            pair_entry(m, tm)["phase_a_items"].append(ev)
        elif et == "phase_b_burst":
            pair_entry(m, tm)["phase_b_bursts"].append(ev)
        elif et == "model_done":
            pair_entry(m, tm)["status"] = "done"
        elif et == "model_error":
            e = pair_entry(m, tm)
            e["status"] = "error"
            e["error"] = ev.get("error")
        elif et == "model_aborted":
            pair_entry(m, tm)["status"] = "aborted"
        elif et == "model_path_error":
            e = pair_entry(m, tm)
            e["status"] = "path_error"
            e["error"] = ev.get("error")
        elif et == "model_server_dead":
            e = pair_entry(m, tm)
            e["status"] = "server_dead"
            e["error"] = ev.get("error")
            e["server_log_path"] = ev.get("server_log_path")

    # 모델별 요약 계산
    MIN_OK_SAMPLES_RELIABLE = 10  # Phase B 에서 ok 샘플이 이 이하면 unreliable

    def summarize_phase_a(items: list[dict]) -> dict:
        """레벨별 집계. solved = answer_tokens>0 건수 (cap hit 으로 답 없음 제외)."""
        if not items:
            return {}
        by_level: dict[int, list[dict]] = {}
        for it in items:
            by_level.setdefault(it.get("level", 0), []).append(it)
        levels_sum = {}
        for lv, its in by_level.items():
            valid = [x for x in its if not x.get("error")]
            n_solved = sum(1 for x in valid if (x.get("answer_tokens") or 0) > 0)
            if not valid:
                levels_sum[f"L{lv}"] = {
                    "n_items": len(its),
                    "n_errors": len(its),
                    "n_solved": 0,
                }
                continue
            levels_sum[f"L{lv}"] = {
                "n_items": len(its),
                "n_errors": len(its) - len(valid),
                "n_solved": n_solved,  # answer_tokens > 0 건수
                "avg_tps_total": sum(x.get("tps_total", 0) for x in valid) / len(valid),
                "avg_tps_effective": sum(x.get("tps_effective", 0) for x in valid) / len(valid),
                "avg_ttft_ms": sum(x.get("ttft_ms", 0) for x in valid) / len(valid),
                "avg_t2a_ms": sum(x.get("time_to_answer_ms", 0) for x in valid) / len(valid),
                "total_thinking_tokens": sum(x.get("thinking_tokens", 0) for x in valid),
                "total_answer_tokens": sum(x.get("answer_tokens", 0) for x in valid),
            }
        return levels_sum

    def summarize_phase_b(bursts: list[dict]) -> dict:
        """Phase B 버스트별 집계.

        신뢰성 판단 (unreliable):
        - 해당 N 의 ok_samples 가 MIN_OK_SAMPLES_RELIABLE 미만이면 p50/p95 None 처리.
          (서버 크래시로 1~2건만 '성공'한 경우 fake latency 출력 방지)

        유효 동시 사용자 공식: reliable 버스트만 고려.
        """
        if not bursts:
            return {}

        def _ok_samples_of(b: dict) -> int:
            total = b.get("sample_count") or 0
            fr = b.get("failure_rate") or 0.0
            return int(round(total * (1.0 - fr)))

        bursts_out: list[dict] = []
        for b in bursts:
            ok_cnt = _ok_samples_of(b)
            reliable = ok_cnt >= MIN_OK_SAMPLES_RELIABLE
            entry = {
                "n": b.get("n_concurrent"),
                "agg_tps": b.get("aggregate_tps"),
                "p50_ms": b.get("p50_ms") if reliable else None,
                "p95_ms": b.get("p95_ms") if reliable else None,
                "failure_rate": b.get("failure_rate"),
                "samples": b.get("sample_count"),
                "ok_samples": ok_cnt,
                "reliable": reliable,
            }
            bursts_out.append(entry)

        # 유효 동시 사용자: reliable 버스트 중에서만 계산
        reliable_bursts = [b for b in bursts_out if b["reliable"]]
        single = next((b for b in reliable_bursts if b["n"] == 1), None)
        base_p95 = single.get("p95_ms") if single else None
        effective_n = 1 if single else 0
        for b in reliable_bursts:
            n = b["n"] or 0
            if n > EFFECTIVE_CONCURRENCY_CAP:
                break
            p95 = b.get("p95_ms")
            if base_p95 is None or (p95 is not None and p95 <= base_p95 * 2):
                effective_n = max(effective_n, n)

        return {
            "bursts": bursts_out,
            "effective_concurrency": effective_n,
            "n_reliable_bursts": len(reliable_bursts),
        }

    for key, data in by_pair.items():
        data["phase_a_summary"] = summarize_phase_a(data["phase_a_items"])
        data["phase_b_summary"] = summarize_phase_b(data["phase_b_bursts"])

        # Status 재조정: "done" 으로 기록됐지만 실제로는 전부 실패한 경우 "unreliable" 로.
        if data["status"] == "done":
            a_items = data["phase_a_items"]
            a_ok = sum(1 for x in a_items if not x.get("error"))
            pb = data["phase_b_summary"]
            n_reliable = pb.get("n_reliable_bursts", 0) if pb else 0

            if a_items and a_ok == 0 and n_reliable == 0:
                # Phase A 전부 에러 + Phase B reliable 0 → 쓰레기 데이터
                data["status"] = "unreliable"
                data["error"] = data.get("error") or "all phase A errored and no reliable phase B bursts"
            elif n_reliable > 0 and n_reliable < len([b for b in data["phase_b_bursts"]]):
                # 일부 버스트만 신뢰 가능 → 완전 done 아님
                data["partial_phase_b"] = True

    # 전력 요약 (이벤트에 있으면 사용, 아니면 인자로 받은 것)
    power_event = next(
        (ev for ev in reversed(events) if ev.get("event") == "power_summary"), None
    )
    if power_event:
        power_summary = {
            "avg_watts": power_event.get("avg_watts"),
            "peak_watts": power_event.get("peak_watts"),
            "peak_temp_c": power_event.get("peak_temp_c"),
            "sample_count": power_event.get("sample_count"),
        }
    elif power_samples:
        temps = [s.temp_c for s in power_samples if s.temp_c is not None]
        power_summary = {
            "avg_watts": sum(s.watts for s in power_samples) / len(power_samples),
            "peak_watts": max(s.watts for s in power_samples),
            "peak_temp_c": max(temps) if temps else None,
            "sample_count": len(power_samples),
        }
    else:
        power_summary = None

    # JSON 직렬화: tuple key 는 "model|think_mode" 문자열로
    models_out = {f"{k[0]}|{k[1]}": v for k, v in by_pair.items()}
    final = {
        "device": asdict(device),
        "jsonl_source": str(jsonl_path),
        "models": models_out,
        "power_summary": power_summary,
        "finalized_at": datetime.now().isoformat(),
    }

    json_path = jsonl_path.with_suffix(".json")
    md_path = jsonl_path.with_suffix(".md")
    json_path.write_text(
        json.dumps(final, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    # Markdown 요약
    lines = [
        f"# 벤치마크 결과 — {device.hostname}",
        "",
        f"- **Chip**: {device.chip}",
        f"- **RAM**: {device.ram_gb} GB",
        f"- **Finalized**: {final['finalized_at']}",
        f"- **Source**: `{jsonl_path.name}`",
        "",
    ]
    if power_summary:
        pt = power_summary.get("peak_temp_c")
        pt_str = f"{pt:.1f} °C" if isinstance(pt, (int, float)) else "n/a (센서 미감지)"
        lines += [
            "## 전력",
            f"- avg: {power_summary.get('avg_watts', 0):.1f} W",
            f"- peak: {power_summary.get('peak_watts', 0):.1f} W",
            f"- peak temp: {pt_str}",
            f"- samples: {power_summary.get('sample_count', 0)}",
            "",
        ]

    # (model, think_mode) 별 섹션. model 로 먼저 그룹핑 후 모드 순.
    models_in_order: list[str] = []
    for (m, _tm) in by_pair.keys():
        if m not in models_in_order:
            models_in_order.append(m)

    for m in models_in_order:
        lines.append(f"## {m}")
        for tm in ("off", "on"):  # 리포트도 실행 순서와 동일하게
            key = (m, tm)
            if key not in by_pair:
                continue
            data = by_pair[key]
            lines.append(f"\n### think_mode = `{tm}`")
            status_badge = {
                "done": "✅ done",
                "error": "❌ error",
                "aborted": "⏸️ aborted",
                "path_error": "🚫 path error",
                "server_dead": "💥 server crashed",
                "unreliable": "🟡 unreliable",
                "pending": "⏳ pending",
            }.get(data["status"], data["status"])
            lines.append(f"- **Status**: {status_badge}")
            if data.get("error"):
                lines.append(f"- **Error**: `{data['error']}`")
            if data.get("server_log_path"):
                lines.append(f"- **Server log**: `{data['server_log_path']}`")

            a = data.get("phase_a_summary", {})
            if a:
                lines.append("\n**Phase A** (레벨별 평균)")
                lines.append(
                    "| 레벨 | N | err | solve | TPS(전체) | TPS(실효) | TTFT(ms) | t2a(ms) | answer tok | think tok |"
                )
                lines.append("|------|---|-----|-------|----------|----------|---------|--------|-----------|---------|")
                for lv in sorted(a.keys()):
                    s = a[lv]
                    solve_str = f"{s.get('n_solved', 0)}/{s['n_items']}"
                    if s.get("avg_tps_total") is None:
                        lines.append(
                            f"| {lv} | {s['n_items']} | {s['n_errors']} | {solve_str} | - | - | - | - | - | - |"
                        )
                        continue
                    lines.append(
                        f"| {lv} | {s['n_items']} | {s['n_errors']} | {solve_str} | "
                        f"{s['avg_tps_total']:.1f} | {s['avg_tps_effective']:.1f} | "
                        f"{s['avg_ttft_ms']:.0f} | {s['avg_t2a_ms']:.0f} | "
                        f"{s['total_answer_tokens']} | {s['total_thinking_tokens']} |"
                    )

            b = data.get("phase_b_summary", {})
            if b.get("bursts"):
                lines.append("\n**Phase B** (동시 사용자)")
                lines.append("| N | agg TPS | p50(ms) | p95(ms) | fail | samples | ok | reliable |")
                lines.append("|---|---------|--------|--------|-----|--------|----|----------|")
                for burst in b["bursts"]:
                    p50 = burst.get("p50_ms")
                    p95 = burst.get("p95_ms")
                    p50_str = f"{p50:.0f}" if isinstance(p50, (int, float)) else "n/a"
                    p95_str = f"{p95:.0f}" if isinstance(p95, (int, float)) else "n/a"
                    reliable_str = "✅" if burst.get("reliable") else "⚠️"
                    lines.append(
                        f"| {burst['n']} | {burst.get('agg_tps', 0):.1f} | "
                        f"{p50_str} | {p95_str} | "
                        f"{burst.get('failure_rate', 0):.1%} | {burst.get('samples', 0)} | "
                        f"{burst.get('ok_samples', 0)} | {reliable_str} |"
                    )
                lines.append(
                    f"\n- **유효 동시 사용자**: {b.get('effective_concurrency', 0)} "
                    f"(상한 {EFFECTIVE_CONCURRENCY_CAP}, reliable 버스트 {b.get('n_reliable_bursts', 0)}/{len(b['bursts'])})"
                )

            lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path


# =============================================================================
# 메인
# =============================================================================

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Local LLM Benchmark (MLX)")
    p.add_argument("--phase", choices=["a", "b", "all"], default="all")
    p.add_argument("--model", help="특정 모델 1개만 실행 (MLX 경로)")
    p.add_argument("--dry-run", action="store_true", help="실제 생성 없이 실행 계획만 출력")
    p.add_argument("--no-power", action="store_true", help="powermetrics 스킵 (sudo 불필요)")
    p.add_argument("--port", type=int, default=PHASE_B_PORT)
    p.add_argument(
        "--fresh",
        action="store_true",
        help="기존 {hostname}_{date}.jsonl 를 .bak.{HHMMSS} 로 백업 후 처음부터 재실행",
    )
    p.add_argument(
        "--finalize-only",
        metavar="JSONL",
        help="기존 jsonl 만 읽어 json+md 생성 (실행 없음)",
    )
    p.add_argument(
        "--think-mode",
        choices=["both", "on", "off"],
        default="both",
        help="thinking 활성화 모드. both: on/off 둘 다 실행 (기본). on: thinking 유지. off: /no_think 로 비활성화.",
    )
    return p.parse_args()


def already_done_model_modes(jsonl_path: Path) -> set[tuple[str, str]]:
    """기존 jsonl 에서 (model, think_mode) 완료 쌍 집합 반환 (--resume 기본 동작).

    과거 이벤트에 think_mode 필드가 없으면 'on' 으로 간주 (backward compat).
    """
    done: set[tuple[str, str]] = set()
    if not jsonl_path.exists():
        return done
    with jsonl_path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("event") == "model_done" and rec.get("model"):
                done.add((rec["model"], rec.get("think_mode", "on")))
    return done


def backup_jsonl_for_fresh(paths: dict[str, Path]) -> None:
    """(더 이상 사용 안 함) 예전 평면 파일 구조에서 --fresh 시 백업 용도.

    Per-run 폴더 구조로 바뀌면서 --fresh 는 새 timestamp 폴더를 만드는 방식으로 대체됨.
    이 함수는 하위 호환을 위해 남겨둠.
    """
    stamp = datetime.now().strftime("%H%M%S")
    for key in ("jsonl", "log"):
        if key in paths and paths[key].exists():
            paths[key].rename(paths[key].with_suffix(paths[key].suffix + f".bak.{stamp}"))


def _is_run_complete(jsonl_path: Path) -> bool:
    """jsonl 의 마지막 run_done 이벤트가 aborted=False 이면 완료 간주."""
    if not jsonl_path.exists():
        return False
    last_done = None
    try:
        with jsonl_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("event") == "run_done":
                    last_done = ev
    except Exception:
        return False
    return bool(last_done) and not last_done.get("aborted")


def _paths_for_folder(folder: Path) -> dict[str, Path]:
    """폴더 하나에 대한 표준 경로 dict."""
    (folder / "raw").mkdir(parents=True, exist_ok=True)
    (folder / "server_logs").mkdir(parents=True, exist_ok=True)
    return {
        "folder": folder,
        "jsonl": folder / "run.jsonl",
        "log": folder / "run.log",
        "json": folder / "run.json",
        "md": folder / "run.md",
        "raw_dir": folder / "raw",
        "server_log_dir": folder / "server_logs",
    }


def result_paths(hostname: str, fresh: bool = False) -> dict[str, Path]:
    """Per-run 폴더 경로.

    폴더명: `results/{hostname}_{YYYYMMDD_HHMMSS}/`
    구성: run.jsonl / run.log / run.json / run.md / raw/ / server_logs/

    Resume 정책 (fresh=False):
    - 가장 최근 `{hostname}_*` 폴더를 찾아, 그 안 run.jsonl 이 "미완료"면 재사용.
      "미완료" = 마지막 `run_done` 이벤트가 없거나, aborted=True 로 기록됨.
    - 완료된 폴더가 발견되면 새 timestamp 폴더 생성.
    - 폴더가 아예 없으면 새로 생성.

    fresh=True: 항상 새 timestamp 폴더 생성 (resume 무시).
    """
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    if not fresh:
        # {hostname}_* 디렉토리 중 최근순 (mtime)
        candidates = sorted(
            (p for p in RESULTS_DIR.glob(f"{hostname}_*") if p.is_dir()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for folder in candidates:
            jsonl = folder / "run.jsonl"
            if jsonl.exists() and not _is_run_complete(jsonl):
                return _paths_for_folder(folder)
            # 완료된 폴더는 건너뛰고 계속 찾음 → 없으면 새로

    # 새 timestamp 폴더
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder = RESULTS_DIR / f"{hostname}_{stamp}"
    folder.mkdir(parents=True, exist_ok=False)
    return _paths_for_folder(folder)


def main() -> int:
    args = parse_args()

    # --finalize-only: 실행 없이 기존 jsonl 을 json+md 로 정리만
    if args.finalize_only:
        device = detect_device()
        jsonl_path = Path(args.finalize_only)
        json_path, md_path = finalize_results(jsonl_path, device, power_samples=[])
        print(f"[finalize] {json_path}")
        print(f"           {md_path}")
        return 0

    device = detect_device()
    # Per-run 폴더: --fresh 면 무조건 새, 아니면 미완료 폴더 이어 쓰거나 새로.
    paths = result_paths(device.hostname, fresh=args.fresh)
    # save_raw_output 가 이 실행의 raw/ 로 저장하도록 전역 설정
    global _RUN_RAW_DIR
    _RUN_RAW_DIR = paths["raw_dir"]
    logger_hint_folder = paths["folder"].relative_to(ROOT)
    print(f"[run_folder] {logger_hint_folder}")

    # think 모드 목록 결정
    if args.think_mode == "both":
        think_modes = list(THINK_MODES)
    else:
        think_modes = [args.think_mode]

    # 대상 (모델, 모드) 쌍 선정 + --resume 스킵 처리
    all_models = [args.model] if args.model else select_models(device.ram_gb)
    all_pairs: list[tuple[str, str]] = [
        (m, tm) for m in all_models for tm in think_modes
    ]
    done_pairs = already_done_model_modes(paths["jsonl"])
    pending_pairs = [p for p in all_pairs if p not in done_pairs]

    prompts = load_prompts()
    phase_b_prompt = get_phase_b_prompt(prompts)

    start_time = time.time()
    with EventLogger(paths["log"], paths["jsonl"]) as logger, \
         PowerMonitor(enabled=not args.no_power) as pm:

        logger.event(
            "run_start",
            hostname=device.hostname,
            chip=device.chip,
            ram_gb=device.ram_gb,
            models=all_models,
            think_modes=think_modes,
            pending_pairs=[f"{m}:{tm}" for m, tm in pending_pairs],
            skipped_done=[f"{m}:{tm}" for m, tm in sorted(done_pairs & set(all_pairs))],
            phase=args.phase,
            dry_run=args.dry_run,
            no_power=args.no_power,
            fresh=args.fresh,
        )

        aborted = False
        # 모델별로 서버를 한 번만 띄우고, 그 안에서 모드를 순회 (재로드 회피)
        # 단 현재 모델에 대해 pending 쌍만 처리
        models_to_run: list[str] = []
        for m in all_models:
            if any((m, tm) in pending_pairs for tm in think_modes):
                models_to_run.append(m)

        for model in models_to_run:
            is_tentative = model in TENTATIVE_MODEL_PATHS
            pending_modes_for_model = [
                tm for tm in think_modes if (model, tm) in pending_pairs
            ]
            logger.event(
                "model_start",
                model=model,
                tentative=is_tentative,
                pending_modes=pending_modes_for_model,
            )

            # --- 1) HF 경로 사전 검증 (다운로드 전 조기 차단) ---
            ok, note = validate_model_path(model)
            if not ok:
                guidance = build_path_error_guidance(model, is_tentative)
                logger.event(
                    "model_path_error",
                    model=model,
                    tentative=is_tentative,
                    error=note,
                    guidance=guidance,
                )
                continue  # 다음 모델로
            if note:
                logger.log(f"  [warn] {note}")

            # --- 2) 서버 기동 → 모든 pending 모드 순회 → 서버 종료 ---
            try:
                if args.dry_run:
                    for tm in pending_modes_for_model:
                        logger.log(f"--- think_mode={tm} (dry) ---")
                        if args.phase in ("a", "all"):
                            asyncio.run(run_phase_a(
                                model, prompts, args.port, logger,
                                think_mode=tm, dry_run=True,
                            ))
                        if args.phase in ("b", "all"):
                            asyncio.run(run_phase_b(
                                model, phase_b_prompt, args.port, logger,
                                server_manager=None,
                                think_mode=tm, dry_run=True,
                            ))
                else:
                    server_log_dir = paths.get("server_log_dir")
                    with ServerManager(model, args.port, log_dir=server_log_dir) as sm:
                        for tm in pending_modes_for_model:
                            logger.log(f"--- think_mode={tm} ---")
                            try:
                                if args.phase in ("a", "all"):
                                    asyncio.run(run_phase_a(
                                        model, prompts, args.port, logger,
                                        think_mode=tm,
                                    ))
                                if args.phase in ("b", "all"):
                                    asyncio.run(run_phase_b(
                                        model, phase_b_prompt, args.port, logger, sm,
                                        think_mode=tm,
                                    ))
                                logger.event(
                                    "model_done", model=model, think_mode=tm,
                                )
                            except KeyboardInterrupt:
                                logger.event(
                                    "model_aborted",
                                    model=model, think_mode=tm,
                                    tentative=is_tentative,
                                )
                                raise
                            except ServerDeadError as e:
                                # 서버 크래시 — 이 모델의 남은 모드/phase 스킵 후 다음 모델로
                                server_log = None
                                if server_log_dir:
                                    candidate = server_log_dir / f"server_{_safe_model_name(model)}.log"
                                    if candidate.exists():
                                        server_log = str(candidate.relative_to(ROOT))
                                logger.event(
                                    "model_server_dead",
                                    model=model,
                                    think_mode=tm,
                                    tentative=is_tentative,
                                    error=str(e),
                                    server_log_path=server_log,
                                    guidance=(
                                        "서버 로그 확인: " + server_log
                                        if server_log else
                                        "서버 로그 없음 (log_dir 미설정)"
                                    ),
                                )
                                # 이 모델의 나머지 모드 루프 탈출 → 다음 모델로
                                break
                # dry-run 에서는 model_done 을 기록하지 않음 (다음 --resume 에서 실행되도록)
            except KeyboardInterrupt:
                aborted = True
                break
            except Exception as e:
                err_msg = str(e)
                is_repo_err = any(
                    kw in err_msg.lower()
                    for kw in ("repository not found", "404", "does not exist", "revision")
                )
                extra_guidance = (
                    "\n  " + build_path_error_guidance(model, is_tentative)
                    if is_repo_err
                    else ""
                )
                logger.event(
                    "model_error",
                    model=model,
                    tentative=is_tentative,
                    error=err_msg,
                    error_type=type(e).__name__,
                    guidance=extra_guidance.strip() or None,
                )
            # ServerManager.__exit__ 이 서버 종료 → 메모리 반환

        # 전력 요약 기록
        if pm.samples:
            logger.event(
                "power_summary",
                sample_count=len(pm.samples),
                avg_watts=sum(s.watts for s in pm.samples) / len(pm.samples),
                peak_watts=max(s.watts for s in pm.samples),
                peak_temp_c=max((s.temp_c or 0) for s in pm.samples),
            )

        logger.event(
            "run_done",
            elapsed_s=time.time() - start_time,
            aborted=aborted,
        )

    if not args.dry_run and not aborted:
        json_path, md_path = finalize_results(paths["jsonl"], device, pm.samples)
        print(f"[finalize] {json_path}")
        print(f"           {md_path}")
    elif aborted:
        print("\n[aborted] 다음 실행 시 --resume 이 기본값이므로 같은 명령으로 이어집니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
