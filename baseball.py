import os
import json
import html
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from calendar import timegm

import feedparser
from openai import OpenAI

JST = timezone(timedelta(hours=9))
API_KEY = os.environ["OPENAI_API_KEY"]
WEBHOOK_URL = os.environ["DISCORD_BASEBALL_WEBHOOK_URL"]
MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
RSS_URL = "https://news.google.com/rss/search"
USER_AGENT = "Mozilla/5.0 (compatible; DailyBaseballBriefing/1.0)"


def clean(value):
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def published(entry):
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    try:
        return datetime.fromtimestamp(timegm(parsed), tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def get_articles():
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=48)
    queries = [
        ("NPB・プロ野球", "プロ野球 OR NPB OR セ・リーグ OR パ・リーグ"),
        ("MLB", "MLB OR メジャーリーグ OR 大谷翔平 OR 山本由伸"),
    ]
    articles = []
    seen = set()

    for category, query in queries:
        params = {"q": query, "hl": "ja", "gl": "JP", "ceid": "JP:ja"}
        url = RSS_URL + "?" + urllib.parse.urlencode(params)
        feed = feedparser.parse(url)
        count = 0
        for entry in feed.entries:
            title = clean(entry.get("title", ""))
            link = entry.get("link", "").strip()
            summary = clean(entry.get("summary", ""))
            date = published(entry)
            if not title or not link or link in seen or date is None or date < cutoff:
                continue
            seen.add(link)
            articles.append({
                "category": category,
                "title": title,
                "summary": summary[:1800],
                "url": link,
                "published": date.isoformat(),
            })
            count += 1
            if count >= 12:
                break
    return articles


def summarize(articles):
    if not articles:
        raise RuntimeError("直近48時間以内の野球ニュースを取得できませんでした。")
    client = OpenAI(api_key=API_KEY)
    prompt = f"""
あなたは日本語の野球ニュース編集者です。NPBとMLBのニュース候補から、重複を避けて重要なものを最大3件選んでください。
結果はJSONのみで返してください。記事にない情報や試合結果、順位、数字を絶対に作らないでください。
各ニュースについて:
- title: 短い見出し
- summary: 事実を2文程度で説明。選手・チーム・数字・背景など、候補から確認できる具体情報を優先
- conversation_seed: 野球に詳しくない人とも話せる、自然な雑談のきっかけを1文
- url: 入力URLをそのまま使用
NPBとMLBの両方に良い候補があれば、できるだけ両方を扱う。
候補:
{json.dumps(articles, ensure_ascii=False)}
出力形式:
{{"news":[{{"category":"NPB・プロ野球またはMLB","title":"見出し","summary":"要約","conversation_seed":"雑談の種","url":"URL"}}]}}
"""
    response = client.responses.create(
        model=MODEL,
        store=False,
        max_output_tokens=3500,
        input=prompt,
    )
    raw = (response.output_text or "").strip()
    raw = re.sub(r"^\x60{3}(?:json)?\s*|\s*\x60{3}$", "", raw)
    result = json.loads(raw)
    source_urls = {a["url"] for a in articles}
    news = []
    for item in result.get("news", [])[:3]:
        url = item.get("url", "")
        if url not in source_urls:
            continue
        news.append({
            "category": item.get("category", "野球"),
            "title": clean(item.get("title", ""))[:240],
            "summary": clean(item.get("summary", ""))[:1500],
            "conversation_seed": clean(item.get("conversation_seed", ""))[:500],
            "url": url,
        })
    if not news:
        raise RuntimeError("AIが有効な野球ニュースを返しませんでした。")
    return news


def post(news):
    today = datetime.now(JST).strftime("%Y/%m/%d")
    embeds = [{
        "title": f"⚾ 野球ニュース便　{today}",
        "description": "NPB・MLBのニュースを最大3件。記事で確認できる情報を中心にまとめています。",
        "color": 0x397D76,
    }]
    for i, item in enumerate(news, 1):
        embeds.append({
            "title": f"⚾ {item['title']}"[:256],
            "description": (
                f"{item['summary']}\n\n"
                f"💬 **雑談の種**\n{item['conversation_seed']}\n\n"
                f"[記事を読む ↗]({item['url']})"
            )[:4000],
            "color": 0x397D76 if "NPB" in item["category"] or "プロ野球" in item["category"] else 0x2463A6,
            "footer": {"text": f"{i} / {len(news)}"},
        })
    payload = {"embeds": embeds, "allowed_mentions": {"parse": []}}
    request = urllib.request.Request(
        WEBHOOK_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            print(f"Discord投稿成功: HTTP {response.status}")
    except Exception as exc:
        print(f"Discord投稿失敗: {type(exc).__name__}: {exc}")
        raise


def main():
    print("野球ニュース取得開始:", datetime.now(JST).isoformat())
    articles = get_articles()
    print(f"候補記事数: {len(articles)}")
    news = summarize(articles)
    post(news)
    print("野球ニュース配信完了")


if __name__ == "__main__":
    main()
