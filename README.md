# AI News Digest Bot

매일 아침 어제의 AI 관련 주요 뉴스를 카테고리별로 정리해 Slack 채널로 전송합니다.
(월요일엔 지난 금/토/일 주말 3일치)

## 동작 방식

```
GitHub Actions cron (UTC 00:00 = KST 09:00)
       ↓
Python 스크립트 실행
  1. 날짜 범위 결정 (어제 or 월요일이면 금~일)
  2. 여러 RSS 피드에서 AI 기사 수집 + 중복 제거
  3. Claude Haiku로 상위 기사 선별 + 4개 카테고리로 분류 + 한국어 요약
  4. Slack Incoming Webhook으로 전송
```

## 카테고리

- 🚀 모델 출시 & 업데이트
- 💼 AI 업계 & 비즈니스
- 🔬 AI 연구 & 논문
- ⚖️ AI 정책 & 규제

## 최초 셋업

### 1. GitHub에 레포 생성 후 이 파일들 업로드

```
ai-news-digest/
├── .github/workflows/daily-ai-news.yml
├── ai_news.py
├── requirements.txt
└── README.md
```

레포는 **Public**으로 만들면 GitHub Actions를 무료 무제한으로 쓸 수 있습니다 (Private은 월 2000분 한도).

### 2. Secrets 등록

레포 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

두 개를 등록:

| Name | Value |
|---|---|
| `SLACK_WEBHOOK_URL` | `https://hooks.slack.com/services/...` |
| `ANTHROPIC_API_KEY` | `sk-ant-...` (https://console.anthropic.com/ 에서 발급) |

### 3. 수동 실행으로 테스트

레포의 **Actions** 탭 → **Daily AI News Digest** 워크플로우 선택 → **Run workflow** 버튼

Slack 채널에 다이제스트가 도착하면 성공. 이후 매일 UTC 00:00 (한국시간 09:00)에 자동 실행됩니다.

## 비용

- **GitHub Actions**: Public 레포라면 무료 무제한. 실행 시간은 1회당 1-2분
- **Anthropic Claude API (Haiku)**: 1회 실행당 약 $0.01-0.03 (월 $0.3-1)
- **RSS 피드**: 무료

## 운영 팁

- 스크립트가 실패해도 가능한 한 Slack으로 에러 메시지를 보내므로, Slack이 조용하면 무조건 GitHub Actions 로그 확인
- 뉴스 품질이 마음에 안 들면 `ai_news.py`의 `FEEDS` 리스트와 `SYSTEM_PROMPT`를 조정
- 카테고리를 바꾸고 싶으면 `CATEGORIES`, `CATEGORY_EMOJI`, `SYSTEM_PROMPT` 3곳을 함께 수정