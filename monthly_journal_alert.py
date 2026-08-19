"""
월간 논문 서칭 -> 요약 -> Slack 전송 프로토타입
===============================================
이 스크립트는 PubMed에서 최근 30일 내 발표된 논문을 검색하고,
Claude API로 한국어 3~4문장 요약을 생성한 뒤 Slack 채널에 올립니다.

[실행 전 준비물]
1. Slack Incoming Webhook URL
   - Slack 워크스페이스 관리자 페이지 > Apps > Incoming Webhooks에서 발급
   - 환경변수 SLACK_WEBHOOK_URL 에 저장
2. Anthropic API Key (요약 품질을 위해 사용, 없으면 초록 앞부분으로 대체)
   - 환경변수 ANTHROPIC_API_KEY 에 저장
3. (선택) 본인 이메일 - PubMed API 정책상 명시 권장
   - 환경변수 PUBMED_EMAIL 에 저장

[실행 방법]
    python monthly_journal_alert.py

[매달 자동 실행 방법]
- Mac/Linux: crontab에 아래 한 줄 추가 (매일 오전 9시 실행, 스크립트 안에서
  "이번 달 첫째 주 월요일"인지 자체 판별 후 아닐 경우 조용히 종료)
    0 9 * * 1 /usr/bin/python3 /path/to/monthly_journal_alert.py
- Windows: 작업 스케줄러(Task Scheduler)에서 "매주 월요일 오전 9시" 트리거로 등록
  (동일하게 스크립트 내부에서 첫째 주인지 확인 후 아니면 종료)
"""

import os
import json
import time
import datetime
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET

# ------------------------------------------------------------------
# 0. 설정: 검색 키워드 그룹 (필요에 따라 자유롭게 수정하세요)
# ------------------------------------------------------------------
KEYWORD_GROUPS = [
    "ward deterioration prediction",
    "sepsis early warning score",
    "ICU mortality prediction model",
    "vital sign clinical decision support",
    "NEWS2 OR MEWS early warning score validation",
    "eCART early warning score",
    "Epic Sepsis Model",
    "VUNO OR AITRICS early warning",
    "FDA clearance clinical prediction algorithm",
    "real-time acute kidney injury prediction time series",
]

MAX_PAPERS = 8
DAYS_BACK = 30
SLACK_CHANNEL_NOTE = "#journal-alert"  # 실제 전송은 webhook이 지정된 채널로 감


def is_first_monday_of_month(today: datetime.date) -> bool:
    """오늘이 이번 달 '첫째 주 월요일'인지 확인 (cron을 매주 월요일로 걸어두고,
    이 함수로 매달 한 번만 실제로 동작하도록 필터링합니다)."""
    return today.weekday() == 0 and today.day <= 7


# ------------------------------------------------------------------
# 1. PubMed 검색
# ------------------------------------------------------------------
def _fetch_with_retry(url: str, max_retries: int = 3):
    """429(Too Many Requests) 발생 시 잠시 기다렸다가 재시도합니다."""
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < max_retries - 1:
                wait = 2 ** (attempt + 1)  # 2초, 4초, 8초 순으로 대기
                print(f"[재시도] 429 응답, {wait}초 대기 후 재시도...")
                time.sleep(wait)
                continue
            raise


def search_pubmed(query: str, days_back: int, email: str) -> list[str]:
    """esearch로 최근 N일 내 PMID 목록을 가져옵니다."""
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    params = {
        "db": "pubmed",
        "term": f"({query}) AND (\"last {days_back} days\"[PDat])",
        "retmax": "10",
        "retmode": "json",
        "sort": "date",
    }
    if email:
        params["email"] = email
    url = f"{base}?{urllib.parse.urlencode(params)}"
    data = json.loads(_fetch_with_retry(url).decode())
    return data.get("esearchresult", {}).get("idlist", [])


def fetch_pubmed_details(pmids: list[str]) -> list[dict]:
    """efetch로 제목/저널/초록/링크를 가져옵니다."""
    if not pmids:
        return []
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
    params = {"db": "pubmed", "id": ",".join(pmids), "retmode": "xml"}
    url = f"{base}?{urllib.parse.urlencode(params)}"
    xml_data = _fetch_with_retry(url)

    root = ET.fromstring(xml_data)
    papers = []
    for article in root.findall(".//PubmedArticle"):
        pmid = article.findtext(".//PMID", default="")
        title = article.findtext(".//ArticleTitle", default="(제목 없음)")
        journal = article.findtext(".//Journal/Title", default="(저널 미상)")
        abstract_parts = [
            el.text or "" for el in article.findall(".//AbstractText")
        ]
        abstract = " ".join(abstract_parts).strip()
        papers.append(
            {
                "pmid": pmid,
                "title": title,
                "journal": journal,
                "abstract": abstract,
                "link": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            }
        )
    return papers


def collect_candidate_papers(email: str) -> list[dict]:
    seen_pmids = set()
    all_papers = []
    for kw in KEYWORD_GROUPS:
        try:
            pmids = search_pubmed(kw, DAYS_BACK, email)
        except Exception as e:
            print(f"[경고] 검색 실패 ({kw}): {e}")
            continue
        new_pmids = [p for p in pmids if p not in seen_pmids]
        seen_pmids.update(new_pmids)
        if new_pmids:
            time.sleep(0.4)  # esearch와 efetch 사이 최소 대기 (PubMed 요청 제한 준수)
            all_papers.extend(fetch_pubmed_details(new_pmids))
        time.sleep(0.4)  # 다음 키워드 검색 전 대기
    return all_papers[: MAX_PAPERS * 2]  # 넉넉히 모아서 이후 상위 N개만 선별


# ------------------------------------------------------------------
# 2. 한국어 요약 생성 (Anthropic API 사용, 키 없으면 초록 일부로 대체)
# ------------------------------------------------------------------
def summarize_with_claude(paper: dict, api_key: str) -> str:
    if not api_key or not paper.get("abstract"):
        # API 키가 없거나 초록이 없으면 간단 대체 요약
        return (paper.get("abstract") or "초록 정보 없음")[:200]

    prompt = (
        "다음은 의료 AI 관련 논문의 제목과 초록입니다. "
        "이 논문이 어떤 데이터를 사용했고, 어떤 방법론을 썼으며, "
        "어떤 결과를 얻었는지 한국어 3~4문장으로 간결하게 요약해줘. "
        "AITRICS의 VitalCare(병동 악화 예측, 패혈증, ICU 사망률) 제품과의 "
        "관련성이 있다면 마지막 문장에 간단히 언급해줘.\n\n"
        f"제목: {paper['title']}\n초록: {paper['abstract']}"
    )
    body = json.dumps(
        {
            "model": "claude-sonnet-4-6",
            "max_tokens": 400,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        return "".join(
            block.get("text", "") for block in data.get("content", [])
        ).strip()
    except Exception as e:
        print(f"[경고] Claude 요약 실패: {e}")
        return (paper.get("abstract") or "초록 정보 없음")[:200]


# ------------------------------------------------------------------
# 3. Slack 전송
# ------------------------------------------------------------------
def build_slack_message(papers_with_summary: list[dict]) -> dict:
    today_str = datetime.date.today().strftime("%Y-%m-%d")
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"📚 월간 타겟 논문 알림 ({today_str})",
            },
        },
        {"type": "divider"},
    ]
    for i, p in enumerate(papers_with_summary, 1):
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*{i}. {p['title']}*\n"
                        f"_{p['journal']}_  |  <{p['link']}|PubMed 링크>\n"
                        f"{p['summary']}"
                    ),
                },
            }
        )
    if not papers_with_summary:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "이번 달은 조건에 맞는 신규 논문을 찾지 못했습니다.",
                },
            }
        )
    return {"blocks": blocks}


def send_to_slack(webhook_url: str, message: dict):
    body = json.dumps(message).encode()
    req = urllib.request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(f"[Slack] 전송 완료, 응답 코드: {resp.status}")


# ------------------------------------------------------------------
# 4. 메인 실행 흐름
# ------------------------------------------------------------------
def main():
    today = datetime.date.today()
    if not is_first_monday_of_month(today):
        print(f"오늘({today})은 매달 첫째 주 월요일이 아니므로 실행하지 않습니다.")
        return

    slack_webhook = os.environ.get("SLACK_WEBHOOK_URL")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    pubmed_email = os.environ.get("PUBMED_EMAIL", "")

    if not slack_webhook:
        raise SystemExit(
            "SLACK_WEBHOOK_URL 환경변수가 설정되어 있지 않습니다. "
            "Slack Incoming Webhook URL을 먼저 발급받아 설정해주세요."
        )

    print("PubMed 검색 중...")
    candidates = collect_candidate_papers(pubmed_email)
    print(f"후보 논문 {len(candidates)}건 수집됨. 상위 {MAX_PAPERS}건 요약 진행...")

    top_papers = candidates[:MAX_PAPERS]
    for p in top_papers:
        p["summary"] = summarize_with_claude(p, anthropic_key)

    message = build_slack_message(top_papers)
    send_to_slack(slack_webhook, message)
    print(f"완료: {SLACK_CHANNEL_NOTE} 채널로 전송했습니다.")


if __name__ == "__main__":
    main()
