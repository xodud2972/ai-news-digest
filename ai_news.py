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
# gpt-4o-mini는 "정확히 N개 선별" 같은 엄격한 수량 지시를 잘 안 따름 →
# 지시 이행 능력이 훨씬 좋은 gpt-4o 사용 (하루 1회 배치라 비용 부담 미미)
MODEL = "gpt-4o"
MAX_ARTICLES_FINAL = 15  # Slack에 보낼 최종 기사 수 상한
TARGET_ARTICLES = 15     # GPT가 맞춰야 할 목표 기사 수

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


def diversify_by_source(articles: list[dict], limit: int) -> list[dict]:
    """한 매체가 입력을 점유하지 않도록 출처별 라운드로빈으로 섞음.

    AI타임스처럼 발행량 많은 매체가 최신순 정렬 상위를 독점하면 GPT 입력이
    그 매체 기사로만 채워져 결과도 편중됨. 출처별로 돌아가며 뽑아서 균형 확보.
    """
    from collections import defaultdict, deque

    buckets: dict[str, deque] = defaultdict(deque)
    for a in articles:
        buckets[a["source"]].append(a)

    result: list[dict] = []
    # 빈 버킷을 제거하면서 라운드로빈
    while buckets and len(result) < limit:
        empty = []
        for src, bucket in list(buckets.items()):
            if not bucket:
                empty.append(src)
                continue
            result.append(bucket.popleft())
            if len(result) >= limit:
                break
        for src in empty:
            del buckets[src]
    return result


# ─────────────────────────────────────────────────────────
# 3) OpenAI로 선별 + 심화 분석
# ─────────────────────────────────────────────────────────
SYSTEM_PROMPT = """당신은 AI 뉴스 큐레이터 겸 실용적인 개발자 관점의 분석가입니다.
주어진 영문/국문 AI 관련 기사 후보에서 기사를 선별하고 한국어로 심화 분석합니다.

사용자는 영문 원문을 읽지 않습니다. 따라서 요약·시사점·개발자 의견·AI 시각이
기사를 대신할 수 있을 만큼 실질적인 정보를 담고 있어야 합니다.

=== 출력 형식 ===
- 반드시 유효한 JSON 오브젝트만 출력: {"articles": [ ... ]} 형태.
- articles 배열의 길이는 **정확히 N개** (사용자 메시지에서 지정됨). 후보가 N개 미만이면 후보 수만큼만.
- articles 각 원소 스키마:
  {
    "id": <int, 원본 번호>,
    "title_ko": <str, 한국어 제목 30자 이내 권장>,
    "summary_ko": <str, 한국어 요약 5-6문장. 맥락·핵심·수치·인물/회사 포함>,
    "implications": <str[] 3-4개>,
    "dev_opinion": <str, 3-4문장>,
    "ai_opinion": <str, 3-4문장>
  }

=== 각 필드 가이드 ===
implications: 이 기사가 **시사하는 바** 또는 **함께 고민해보면 좋을 주변 맥락**.
  단순 "생각해볼 포인트"가 아니라, 기사 본문 바깥의 업계 맥락·선행 사례·잠재적 파급 효과·
  암시되는 산업 방향성 등과 연결하는 인사이트. 한 문장씩 구체적으로.

dev_opinion: 실무 개발자 관점 의견 3-4문장. 실제 업무/프로덕트 적용 방안, 도입 장벽(비용·
  성능·생태계·라이선스), 상용화 수준, 지금 해볼 만한 구체적 제안(예: "허깅페이스에서 ○○
  모델 받아 로컬 테스트") 포함.

ai_opinion: AI 본인 관점의 논평 3-4문장. AI 발전 큰 흐름에서의 위치, 모델·기술 관점에서
  흥미롭거나 우려되는 지점, 다른 연구·제품 흐름과의 비교, 업계가 놓치는 포인트 등.
  단순 찬양 금지. 필요하면 비판적·회의적 시각 환영. "나는 AI로서 ~" 같은 진부한 말투 피할 것.

=== 선별 기준 ===
1. **수량을 반드시 채울 것.** 후보가 충분한데도 임의로 줄이지 말 것. 중요도 애매한 기사도
   흥미로운 각도(기술적 디테일, 업계 시사점, 비판적 분석 등)를 찾아 목표 개수를 채운다.
2. **매체 다양성.** 한 매체(특히 AI타임스·Google News 등)가 전체의 50%를 넘지 않게. 해외 매체
   (TechCrunch·The Verge·VentureBeat·the-decoder)와 국내 매체를 섞을 것.
3. **주제 다양성.** 모델 출시 / 업계·비즈니스 / 연구·논문 / 정책·규제 / 응용사례가 골고루.
   한 주제가 전체의 40%를 넘지 않게.
4. 중복 주제는 하나로 통합 (가장 신뢰도 높은 기사 기준).
5. 명확히 AI와 관련 없는 기사는 제외.

=== 스타일 ===
- summary_ko는 원문을 20단어 이상 연속 복사 금지. 재작성.
- implications·dev_opinion·ai_opinion은 일반 업계 지식을 활용해 구체적이고 실용적으로.
  추측성 내용은 "~일 가능성", "~것으로 보임" 등으로 톤 조절.
- 모든 한국어는 평어체(해요체 아님)로 일관.
- 본문에 이모지 사용 금지.
- 중요도·흥미도 높은 순으로 배열 앞쪽에 배치.
"""


def summarize_with_openai(articles: list[dict]) -> list[dict]:
    """OpenAI에 기사 목록을 넘겨 선별 + 한국어 심화 분석."""
    if not articles:
        return []

    # 매체 다양성 확보 위해 출처별 라운드로빈으로 60개 뽑기
    trimmed = diversify_by_source(articles, 60)
    target = min(TARGET_ARTICLES, len(trimmed))

    # 입력 기사들의 출처 분포 로그
    from collections import Counter
    source_counts = Counter(a["source"] for a in trimmed)
    print(f"[INFO] GPT 입력: {len(trimmed)}개, 출처 분포: {dict(source_counts)}")

    user_parts = [
        f"AI 관련 기사 후보 {len(trimmed)}개입니다.\n",
        f"**반드시 정확히 {target}개를 선별**해서 JSON 배열로 출력하세요.\n",
        f"배열 길이가 {target}이 아니면 응답이 무효입니다.\n",
        "매체·주제 다양성을 반드시 확보하세요.\n\n",
        "=== 후보 기사 ===\n",
    ]
    for i, a in enumerate(trimmed):
        user_parts.append(f"[{i}] 제목: {a['title']}\n")
        user_parts.append(f"    설명: {a['summary'][:300]}\n")
        user_parts.append(f"    출처: {a['source']}\n\n")
    user_parts.append(
        f"\n=== 최종 체크 ===\n"
        f"출력 JSON 배열의 길이가 정확히 {target}개인지 확인 후 응답하세요.\n"
    )
    user_text = "".join(user_parts)

    client = OpenAI(api_key=OPENAI_API_KEY)
    response = client.chat.completions.create(
        model=MODEL,
        max_tokens=12000,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ],
        response_format={"type": "json_object"},
    )

    raw = (response.choices[0].message.content or "").strip()
    # json_object 모드여서 {"items": [...]} 형태로 올 수 있음 → 배열 추출
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[ERROR] OpenAI JSON parse failed: {e}\nraw={raw[:500]}", file=sys.stderr)
        return []

    # json_object 모드는 반드시 오브젝트여야 함. 배열을 감싸는 키를 찾아서 꺼낸다
    if isinstance(parsed, dict):
        # 흔한 키 이름들 탐색
        for key in ("articles", "items", "results", "selected", "data", "list"):
            if isinstance(parsed.get(key), list):
                selected = parsed[key]
                break
        else:
            # 첫 번째 list 값 사용
            list_vals = [v for v in parsed.values() if isinstance(v, list)]
            selected = list_vals[0] if list_vals else []
    elif isinstance(parsed, list):
        selected = parsed
    else:
        selected = []

    print(f"[INFO] GPT 반환 항목 수: {len(selected)} (목표: {target})")

    # 원본 링크 매핑
    enriched = []
    for item in selected:
        if not isinstance(item, dict):
            continue
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

    print(f"[INFO] 유효성 검증 후 최종 항목 수: {len(enriched)}")
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
