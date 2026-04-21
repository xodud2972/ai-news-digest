#!/usr/bin/env python3
"""매일 아침 AI 뉴스 다이제스트 → Slack.

GitHub Actions cron으로 트리거. 어제 (월요일이면 금~일) AI 기사를 RSS에서 수집하여
OpenAI GPT로 한국어 요약 + 시사점 + 개발자 관점 의견 + AI 시각 분석 후 Slack 전송.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import feedparser
from openai import OpenAI

# ─────────────────────────────────────────────────────────
# 환경 변수
# ─────────────────────────────────────────────────────────
SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

# ─────────────────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────────────────
KST = timezone(timedelta(hours=9))
MODEL = "gpt-4o-mini"
MAX_ARTICLES_FINAL = 15  # Slack에 보낼 최종 기사 수 상한

# RSS 소스 — 전부 합쳐서 GPT가 선별하게 함.
FEEDS = [
    # Google News 검색 기반 (영문, 최신성 높음)
    "https://news.google.com/rss/search?q=artificial+intelligence&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=AI+model+release+launch&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=AI+startup+funding+acquisition&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=AI+research+breakthrough&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=AI+regulation+policy&hl=en-US&gl=US&ceid=US:en",
    # Google News 한국 (한글 기사도 일부 수집)
    "https://news.google.com/rss/search?q=AI+%EC%9D%B8%EA%B3%B5%EC%A7%80%EB%8A%A5&hl=ko&gl=KR&ceid=KR:ko",
    # 영문 전문 매체 RSS
    "https://techcrunch.com/category/artificial-intelligence/feed/",
    "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
    "https://venturebeat.com/category/ai/feed/",
    # AI 전문 해외 매체 (추가)
    "https://the-decoder.com/feed/",
    # 한국 AI 전문 매체 (추가)
    "https://www.aitimes.com/rss/allArticle.xml",
]

# ─────────────────────────────────────────────────────────
# 1) 날짜 범위 결정
# ─────────────────────────────────────────────────────────
def date_range() -> tuple[date, date, str]:
    """평일: 어제 하루 / 월요일: 지난 금요일~일요일 3일치."""
    today = datetime.now(KST).date()
    dow = today.weekday()  # 0=Mon
    if dow == 0:  # Monday
        start = today - timedelta(days=3)
        end = today - timedelta(days=1)
        label = f"{start.strftime('%Y-%m-%d (금)')} ~ {end.strftime('%Y-%m-%d (일)')} 주말 포함 3일"
    else:
        start = end = today - timedelta(days=1)
        weekday_ko = ["월", "화", "수", "목", "금", "토", "일"][start.weekday()]
        label = f"{start.strftime('%Y-%m-%d')} ({weekday_ko})"
    return start, end, label


# ─────────────────────────────────────────────────────────
# 2) RSS에서 기사 수집
# ─────────────────────────────────────────────────────────
def _parse_entry_date(entry) -> datetime | None:
    """RSS entry에서 발행 시각 뽑기 (KST 기준 datetime)."""
    try:
        if getattr(entry, "published_parsed", None):
            dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        elif getattr(entry, "updated_parsed", None):
            dt = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)
        elif hasattr(entry, "published"):
            dt = parsedate_to_datetime(entry.published)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        else:
            return None
        return dt.astimezone(KST)
    except Exception:
        return None


def _clean_summary(text: str) -> str:
    """HTML 태그 제거 + 공백 정리."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:400]


def fetch_all_articles(start: date, end: date) -> list[dict]:
    """모든 피드 순회하며 날짜 범위 내 기사 수집 + 중복 제거."""
    seen_titles = set()
    articles: list[dict] = []

    for feed_url in FEEDS:
        try:
            feed = feedparser.parse(feed_url)
        except Exception as e:
            print(f"[WARN] feed fetch failed: {feed_url} :: {e}", file=sys.stderr)
            continue

        for entry in feed.entries:
            dt = _parse_entry_date(entry)
            if dt is None:
                continue
            if not (start <= dt.date() <= end):
                continue

            title = getattr(entry, "title", "").strip()
            if not title:
                continue

            # 아주 느슨한 dedup: 제목 앞 60자 기준
            key = title[:60].lower()
            if key in seen_titles:
                continue
            seen_titles.add(key)

            articles.append({
                "title": title,
                "link": getattr(entry, "link", ""),
                "summary": _clean_summary(getattr(entry, "summary", "")),
                "source": getattr(feed.feed, "title", "Unknown"),
                "published": dt.isoformat(),
            })

    # 신선한 순으로 정렬
    articles.sort(key=lambda a: a["published"], reverse=True)
    return articles


# ─────────────────────────────────────────────────────────
# 3) OpenAI로 선별 + 심화 분석
# ─────────────────────────────────────────────────────────
SYSTEM_PROMPT = """당신은 AI 뉴스 큐레이터 겸 실용적인 개발자 관점의 분석가입니다. \
주어진 영문/국문 AI 관련 기사 목록에서 가장 중요하고 흥미로운 기사들을 선별하고, \
한국어로 심화 분석합니다.

중요: 사용자는 영문 원문을 읽지 않습니다. 따라서 요약·시사점·개발자 의견·AI 시각이 \
기사를 대신할 수 있을 만큼 실질적인 정보를 담고 있어야 합니다.

출력 규칙:
- 반드시 유효한 JSON 배열만 출력 (설명 텍스트 금지, 코드펜스 금지)
- 각 원소는 다음 키를 모두 포함:
  * "id": 원본 번호 (int)
  * "title_ko": 한국어 제목 (30자 이내 권장)
  * "summary_ko": 한국어 요약 5-6문장. 원문의 맥락 / 핵심 / 수치 / 주요 인물·회사까지 포함.
  * "implications": 문자열 배열 3-4개. 이 기사가 **시사하는 바** 또는 **함께 고민해보면 좋을 주변 맥락**.
      예) 업계 전반에 주는 함의, 선행·경쟁 사례와의 연결, 잠재적 파급 효과,
          이 뉴스가 암시하는 산업 방향성, 놓치기 쉬운 이면의 이슈 등.
      단순히 "생각해볼 포인트"가 아니라, 기사 본문 바깥의 맥락과 연결하는 인사이트 중심으로.
      각 포인트는 한 문장으로 구체적으로.
  * "dev_opinion": 실무 개발자 관점의 의견 3-4문장.
      - 이 기술/뉴스가 실제 업무나 프로덕트에 어떻게 쓰일 수 있는가
      - 도입 시 장벽·한계는 무엇인가 (비용, 성능, 생태계, 라이선스 등)
      - 현재 상용화 수준인지 실험 단계인지
      - 지금 바로 해볼 만한 구체적 제안 (예: "허깅페이스에서 ○○ 모델 받아서 로컬 테스트" 같이)
  * "ai_opinion": AI 본인의 관점에서 본 의견 3-4문장.
      - 이 뉴스가 AI 발전의 큰 흐름에서 어떤 위치에 있는지
      - AI 모델·기술 관점에서 흥미롭거나 우려되는 지점
      - 다른 AI 연구·제품 흐름과의 비교, 혹은 업계가 놓치고 있는 포인트
      - 단순 찬양·긍정 금지. 필요하면 비판적·회의적 시각도 담을 것.
      - "나는 AI로서 ~" 같은 진부한 말투는 피하고, 자연스럽고 담백한 논평으로.

분량/스타일 규칙:
- 전체 최대 15개 기사 선별 (중요도·흥미도 높은 순)
- 중복 주제는 하나로 통합 (가장 신뢰도 높은 기사 기준)
- 명확히 AI와 관련 없는 기사는 제외
- summary_ko는 원문을 20단어 이상 연속 복사 금지. 반드시 재작성.
- implications·dev_opinion·ai_opinion은 일반적 업계 지식을 활용해 구체적이고 실용적으로 작성.
  추측성 내용은 "~일 가능성", "~것으로 보임" 등으로 톤 조절.
- 모든 한국어 출력은 평어체(해요체 아님)로 일관.
- 본문에 이모지 사용 금지 (Slack 포맷은 코드에서 처리).
"""


def summarize_with_openai(articles: list[dict]) -> list[dict]:
    """OpenAI에 기사 목록을 넘겨 선별 + 한국어 심화 분석."""
    if not articles:
        return []

    # 입력이 너무 많으면 상위 40개만 (토큰 절약)
    trimmed = articles[:40]

    user_parts = ["다음은 어제 발행된 AI 관련 기사들입니다. 중요도 순으로 최대 15개 선별하고 심화 분석해주세요.\n\n"]
    for i, a in enumerate(trimmed):
        user_parts.append(f"[{i}] 제목: {a['title']}\n")
        user_parts.append(f"    설명: {a['summary'][:300]}\n")
        user_parts.append(f"    출처: {a['source']}\n\n")
    user_text = "".join(user_parts)

    client = OpenAI(api_key=OPENAI_API_KEY)
    response = client.chat.completions.create(
        model=MODEL,
        max_tokens=12000,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ],
    )

    raw = (response.choices[0].message.content or "").strip()
    # 혹시 코드펜스로 감쌌으면 제거
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()

    try:
        selected = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[ERROR] OpenAI JSON parse failed: {e}\nraw={raw[:500]}", file=sys.stderr)
        return []

    # 원본 링크 매핑
    enriched = []
    for item in selected:
        idx = item.get("id")
        if not isinstance(idx, int) or not (0 <= idx < len(trimmed)):
            continue
        original = trimmed[idx]

        implications = item.get("implications") or []
        if not isinstance(implications, list):
            implications = []
        implications = [str(p).strip() for p in implications if str(p).strip()]

        enriched.append({
            "title_ko": item.get("title_ko", original["title"]),
            "summary_ko": item.get("summary_ko", ""),
            "implications": implications[:4],
            "dev_opinion": str(item.get("dev_opinion", "")).strip(),
            "ai_opinion": str(item.get("ai_opinion", "")).strip(),
            "link": original["link"],
            "source": original["source"],
        })

    return enriched[:MAX_ARTICLES_FINAL]


# ─────────────────────────────────────────────────────────
# 4) Slack 메시지 포맷팅 (mrkdwn)
# ─────────────────────────────────────────────────────────
def build_slack_message(items: list[dict], label: str, total_candidates: int) -> str:
    weekday_ko = ["월", "화", "수", "목", "금", "토", "일"][datetime.now(KST).weekday()]
    today_str = datetime.now(KST).strftime(f"%Y년 %m월 %d일 ({weekday_ko})")

    divider = "━━━━━━━━━━━━━━━━━━━━━━━"

    lines = [
        f"*AI 뉴스 다이제스트 — {today_str}*",
        f"_{label}의 주요 소식 · 총 {len(items)}개 기사_",
    ]

    if not items:
        lines.append("")
        lines.append("오늘은 수집된 AI 뉴스가 없어요.")
        lines.append(f"_수집 후보 기사 수: {total_candidates}개_")
        return "\n".join(lines)

    for i, it in enumerate(items, 1):
        lines.append("")
        lines.append(divider)
        lines.append("")
        lines.append(f"*{i}. {it['title_ko']}*")
        lines.append("")

        summary = it.get("summary_ko", "").strip()
        if summary:
            lines.append(summary)
            lines.append("")

        implications = it.get("implications") or []
        if implications:
            lines.append("*시사점 · 함께 볼 맥락*")
            for p in implications:
                lines.append(f"•  {p}")
            lines.append("")

        dev_opinion = it.get("dev_opinion", "").strip()
        if dev_opinion:
            lines.append("*개발자 관점*")
            lines.append(dev_opinion)
            lines.append("")

        ai_opinion = it.get("ai_opinion", "").strip()
        if ai_opinion:
            lines.append("*AI의 시각*")
            lines.append(ai_opinion)
            lines.append("")

        lines.append(f"<{it['link']}|원문 보기>  ·  _{it['source']}_")

    lines.append("")
    lines.append(divider)
    lines.append(
        f"_총 {len(items)}개 기사 (후보 {total_candidates}개 중 선별) · 다음 업데이트: 내일 아침 9시_"
    )
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────
# 5) Slack 전송
# ─────────────────────────────────────────────────────────
def send_to_slack(message: str) -> None:
    payload = json.dumps({"text": message}).encode("utf-8")
    req = urllib.request.Request(
        SLACK_WEBHOOK_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = resp.read().decode()
        status = resp.status
        print(f"[SLACK] status={status} body={body}")
        if status != 200 or body != "ok":
            raise RuntimeError(f"Slack 전송 실패: {status} {body}")


# ─────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────
def main() -> int:
    start, end, label = date_range()
    print(f"[INFO] 날짜 범위: {start} ~ {end} ({label})")

    articles = fetch_all_articles(start, end)
    print(f"[INFO] 수집된 기사 후보: {len(articles)}개")

    if not articles:
        send_to_slack(
            f"*AI 뉴스 다이제스트*\n\n"
            f"{label} 기간의 AI 관련 기사를 찾지 못했어요.\n"
            f"RSS 소스에 문제가 있는지 확인이 필요합니다."
        )
        return 0

    try:
        selected = summarize_with_openai(articles)
    except Exception as e:
        print(f"[ERROR] OpenAI 요약 실패: {e}", file=sys.stderr)
        send_to_slack(
            f"*AI 뉴스 다이제스트 — 요약 실패*\n\n"
            f"기사 {len(articles)}개를 수집했지만 OpenAI 요약 중 오류가 발생했습니다.\n"
            f"오류: `{e}`"
        )
        return 1

    print(f"[INFO] 최종 선별 기사: {len(selected)}개")

    message = build_slack_message(selected, label, len(articles))
    print(f"[INFO] Slack 메시지 길이: {len(message)} chars")
    send_to_slack(message)
    print("[DONE]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
