#!/usr/bin/env python3
"""
삼성전자 2024 기업지배구조 보고서 PDF 추출기 (1회성).

기능:
1. 원본 PDF 다운로드 (캐시 있으면 재사용)
2. pypdf 로 전체 텍스트 추출 (빈 결과 → pdfplumber fallback)
3. 섹션 헤더 패턴(I./II./III./IV./V.) 으로 분할
4. 3개 L3 컨텍스트 파일 생성:
   - prompts/contexts/samsung_governance_2024_sec3.md         (III장, ~6K tok 목표)
   - prompts/contexts/samsung_governance_2024_sec3_4.md       (III+IV장, ~12K tok)
   - prompts/contexts/samsung_governance_2024_full.md         (전문, ~24-32K tok)

토큰 추정: 한글 문자수 × 1.3 (Qwen 토크나이저 근사).
목표치 ±30% 벗어나면 경고 출력.
"""
from __future__ import annotations

import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PDF_URL = (
    "https://images.samsung.com/kdp/ir/corporate-governance-report/"
    "Corporate_Governance_fy_2024_kor.pdf"
)
PDF_CACHE = ROOT / "tools" / "_tmp" / "samsung_governance_2024.pdf"
CONTEXTS_DIR = ROOT / "prompts" / "contexts"

# 섹션 마커: [N00000] 패턴 (N=1~5). 예: "[300000] 3. 이사회"
SECTION_HEADER_RE = re.compile(r"^\[([1-5])00000\]\s+\d+\.\s*(.+?)\s*$", re.MULTILINE)

# 섹션 조합 규칙. 같은 문서의 다른 조각을 다른 분량으로 사용.
# "3. 이사회" 섹션이 혼자 ~100K tok 이라 앞부분만 잘라 씀.
OUTPUTS = {
    "samsung_governance_2024_sec3.md": {
        "description": "3.이사회 앞부분 — L3_001(이사회 요약+수치)",
        "sections": ["3"],
        "target_tokens": 6000,
    },
    "samsung_governance_2024_sec3_4.md": {
        "description": "3.이사회 앞부분 + 4.감사기구 — L3_002(이사/위원회/일자 JSON 추출)",
        "sections": ["3", "4"],
        "target_tokens": 12000,
    },
    "samsung_governance_2024_full.md": {
        "description": "2.주주 + 3.이사회 + 4.감사기구 — L3_003(세 축 표 정리)",
        "sections": ["2", "3", "4"],
        "target_tokens": 28000,
    },
}


def download_pdf() -> Path:
    """PDF 다운로드 (캐시 존재 시 재사용)."""
    PDF_CACHE.parent.mkdir(parents=True, exist_ok=True)
    if PDF_CACHE.exists() and PDF_CACHE.stat().st_size > 100_000:
        print(f"[cache] {PDF_CACHE} ({PDF_CACHE.stat().st_size:,} bytes)")
        return PDF_CACHE
    print(f"[download] {PDF_URL}")
    urllib.request.urlretrieve(PDF_URL, PDF_CACHE)
    print(f"[saved] {PDF_CACHE} ({PDF_CACHE.stat().st_size:,} bytes)")
    return PDF_CACHE


def extract_with_pypdf(pdf_path: Path) -> str:
    """pypdf 로 모든 페이지 텍스트 추출."""
    from pypdf import PdfReader  # type: ignore
    reader = PdfReader(str(pdf_path))
    parts: list[str] = []
    for i, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        if text.strip():
            parts.append(f"\n<!-- page {i+1} -->\n")
            parts.append(text)
    return "".join(parts)


def extract_with_pdfplumber(pdf_path: Path) -> str:
    """pypdf 가 빈 결과 반환 시 fallback."""
    try:
        import pdfplumber  # type: ignore
    except ImportError:
        print("[warn] pdfplumber 미설치. uv pip install pdfplumber 로 설치 후 재시도.")
        sys.exit(2)
    parts: list[str] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            if text.strip():
                parts.append(f"\n<!-- page {i+1} -->\n")
                parts.append(text)
    return "".join(parts)


def estimate_korean_tokens(text: str) -> int:
    """한글 기준 토큰 수 근사. Qwen 토크나이저는 한글 1글자 ≈ 1-1.5 tok.
    보수적으로 1.3 배 사용.
    """
    # 공백 / 개행 제외한 글자수 기반
    char_count = len(re.sub(r"\s", "", text))
    return int(char_count * 1.3)


def split_sections(full_text: str) -> dict[str, str]:
    """본문을 섹션별로 분할. 키는 섹션 번호 문자열("1"~"5").

    섹션 헤더 [N00000] N. 제목 패턴으로 분할. 못 찾으면 빈 dict.
    """
    matches = list(SECTION_HEADER_RE.finditer(full_text))
    if not matches:
        return {}

    sections: dict[str, str] = {}
    for i, m in enumerate(matches):
        key = m.group(1)
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(full_text)
        sections[key] = full_text[start:end].strip()
    return sections


def truncate_to_token_budget(text: str, target_tokens: int, tolerance: float = 0.05) -> str:
    """목표 토큰에 맞춰 앞에서부터 자름. 허용 오차 내면 통째로 유지.

    한글 1자 ≈ 1.3 tok 역산: max_chars = target_tokens / 1.3
    """
    current = estimate_korean_tokens(text)
    if current <= target_tokens * (1 + tolerance):
        return text
    max_chars = int(target_tokens / 1.3)
    # 문장 경계(마침표·줄바꿈)에서 자르려 시도. 없으면 그냥 글자수로.
    clipped = text[:max_chars]
    # 마지막 문단 경계 쪽으로 줄이기
    last_break = max(clipped.rfind("\n\n"), clipped.rfind(". "), clipped.rfind("다.\n"))
    if last_break > max_chars * 0.8:
        clipped = clipped[: last_break + 1]
    return clipped + "\n\n<!-- [토큰 예산 초과로 여기서 잘림] -->\n"


def build_output(
    full_text: str,
    sections: dict[str, str],
    section_keys: list[str],
    target_tokens: int,
) -> str:
    """지정 섹션들을 순서대로 이어붙인 뒤 목표 토큰에 맞춰 자름."""
    if not sections:
        # 섹션 분할 실패 시 전체 문서를 그대로 대상으로
        return truncate_to_token_budget(full_text, target_tokens)

    parts = []
    for key in section_keys:
        if key in sections:
            parts.append(sections[key])
        else:
            print(f"[warn] 섹션 {key} 를 찾지 못함 (available: {sorted(sections.keys())})")
    combined = "\n\n".join(parts)
    return truncate_to_token_budget(combined, target_tokens)


def main() -> int:
    pdf_path = download_pdf()

    print(f"[extract] pypdf 시도")
    text = extract_with_pypdf(pdf_path)
    print(f"[extract] pypdf 결과: {len(text):,} chars")

    if len(text.strip()) < 1000:
        print("[fallback] pypdf 결과 부족 → pdfplumber 시도")
        text = extract_with_pdfplumber(pdf_path)
        print(f"[extract] pdfplumber 결과: {len(text):,} chars")

    if len(text.strip()) < 1000:
        print("[error] 텍스트 추출 실패. PDF 가 스캔 이미지일 수 있음 (OCR 필요).")
        return 1

    # 섹션 분할
    sections = split_sections(text)
    if sections:
        summary = ", ".join(
            f"{k}={estimate_korean_tokens(v):,}tok" for k, v in sorted(sections.items())
        )
        print(f"[sections] {summary}")
    else:
        print("[warn] 섹션 헤더 패턴 감지 실패. 전문만 사용 가능.")

    # 3개 출력 파일 생성
    CONTEXTS_DIR.mkdir(parents=True, exist_ok=True)
    for filename, cfg in OUTPUTS.items():
        body = build_output(text, sections, cfg["sections"], cfg["target_tokens"])
        if not body.strip():
            print(f"[skip] {filename}: 본문 비어있음")
            continue

        out_path = CONTEXTS_DIR / filename
        header = (
            f"<!-- 출처: 삼성전자 2024 기업지배구조 보고서 -->\n"
            f"<!-- URL: {PDF_URL} -->\n"
            f"<!-- 설명: {cfg['description']} -->\n\n"
        )
        out_path.write_text(header + body, encoding="utf-8")
        est_tokens = estimate_korean_tokens(body)
        target = cfg["target_tokens"]
        ratio = est_tokens / target
        status = "OK" if 0.7 <= ratio <= 1.3 else f"WARN (목표 대비 {ratio:.1%})"
        print(
            f"[write] {filename}: "
            f"{len(body):,} chars, est~{est_tokens:,} tok (목표 {target:,}) — {status}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
