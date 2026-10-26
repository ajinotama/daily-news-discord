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
WEBHOOK_URL = os.environ["DISCORD_KEIBA_WEBHOOK_URL"]
MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
RSS_URL = "https://news.google.com/rss/search"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36"

SOURCES = [
    ("JRA・中央競馬", '(競馬 OR JRA OR 競走馬 OR G1 OR 重賞)'),
    ("地方競馬", '(地方競馬 OR NAR OR 大井競馬 OR 川崎競馬 OR 園田競馬)'),
]


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
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    articles, seen = [], set()
    for category, query in SOURCES:
        params = {"q": query, "hl": "ja", "gl": "JP", "ceid": "JP:ja"}
        feed = feedparser.parse(RSS_URL + "?" + urllib.parse.urlencode(params))
        count = 0
        for entry in feed.entries:
            title = clean(entry.get("title", ""))
            link = entry.get("link", "").strip()
            date = published(entry)
            if not title or not link or link in seen or date is None or date < cutoff:
                continue
            seen.add(link)
            articles.append({
                "category": category,
                "title": title,
                "summary": clean(entry.get("summary", ""))[:1800],
                "url": link,
                "published": date.isoformat()
            })
            count += 1
            if count >= 15:
                break
    return articles


def summarize(articles):
    if not articles:
        raise RuntimeError("直近48時間以内の競馬ニュースを取得できませんでした。")
    client = OpenAI(api_key=API_KEY)
    prompt = f'''
あなたは日本の競馬ニュース編集者です。候補記事から重複を避け、話題性と競馬ファンにとっての有用性を考えて最大3件選んでください。
JRA・中央競馬と地方競馬の両方に重要な記事があれば、できるだけ両方を扱ってください。
レース開催情報、重賞・G1、競走馬・騎手・調教師、引退や移籍、競馬界のニュースを優先します。
タイトルは内容を保って45文字以内に短縮。記事にない出走予定、着順、払戻金、数字、理由を作らないでください。
各記事の summary_lines は必ず3要素の配列:
1. 何が起きたか
2. 記事で確認できる背景・数字（なければ無理に入れない）
3. 競馬ファンにとっての注目点・今後の焦点
各要素は短い1文。専門用語を使う場合は初心者にも伝わる表現にする。行頭記号は付けない。
urlは入力URLをそのまま使う。JSONのみで返す。
形式: {{"news":[{{"category":"JRA・中央競馬または地方競馬","title":"見出し","summary_lines":["出来事","背景","注目点"],"url":"入力URL"}}]}}
候補:
{json.dumps(articles, ensure_ascii=False)}
'''
    response = client.responses.create(model=MODEL, store=False, max_output_tokens=3000, input=prompt)
    raw = (response.output_text or "").strip()
    raw = re.sub(r"^\x60{3}(?:json)?\s*|\s*\x60{3}$", "", raw)
    result = json.loads(raw)
    valid = {a["url"]: a for a in articles}
    news = []
    for item in result.get("news", [])[:3]:
        url = item.get("url", "")
        if url not in valid:
            continue
        lines = item.get("summary_lines", [])
        if not isinstance(lines, list):
            lines = []
        lines = [clean(str(line)).lstrip("・-• ").strip() for line in lines if clean(str(line))]
        if len(lines) != 3:
            source = valid[url]
            retry_prompt = f'''次の記事の内容を捏造せず、初心者向けの短い日本語3文にしてください。
1文目=何が起きたか、2文目=確認できる背景、3文目=注目点。
JSON配列3要素だけを返してください。
タイトル: {source["title"]}
記事概要: {source["summary"]}'''
            try:
                retry = client.responses.create(model=MODEL, store=False, max_output_tokens=450, input=retry_prompt)
                parsed = json.loads((retry.output_text or "").strip())
                if isinstance(parsed, list):
                    lines = [clean(str(x)) for x in parsed[:3] if clean(str(x))]
            except Exception as exc:
                print(f"要約再生成失敗: {type(exc).__name__}: {exc}")
        if len(lines) != 3:
            source = valid[url]
            lines = [
                clean(item.get("title", source["title"])) + "。",
                clean(source.get("summary", ""))[:120] or "記事概要から確認できる情報は限られています。",
                "詳細や今後の予定はリンク先の記事で確認できます。"
            ]
        news.append({
            "category": clean(item.get("category", "競馬")),
            "title": clean(item.get("title", valid[url]["title"]))[:80],
            "summary": "\n".join("・" + line for line in lines),
            "url": url
        })
    if not news:
        raise RuntimeError("AIが有効な競馬ニュースを返しませんでした。")
    return news


def race_context():
    today = datetime.now(JST)
    date_text = today.strftime("%Y年%m月%d日（%a）")
    lines = [
        f"**今日の日付：** {date_text}",
        "**中央競馬の開催・出馬表・結果：** [JRA公式 ↗](https://www.jra.go.jp/)",
        "**地方競馬の開催・結果：** [地方競馬情報サイト ↗](https://www.keiba.go.jp/)",
        "**重賞日程：** [JRA重賞カレンダー ↗](https://www.jra.go.jp/keiba/calendar/)",
        "",
        "※開催日・出走馬・着順・オッズ・払戻金は、この配信で自動確認できていない場合は断定しません。",
        "※用語メモ：G1は大きな格付けのレース。重賞は特に注目度の高い公式格付けレースです。"
    ]
    return "\n".join(lines)


def post(news):
    today = datetime.now(JST).strftime("%Y/%m/%d")
    sections = [
        f"🏇 **競馬ニュース便｜{today}**",
        "直近48時間のニュースから最大3件を紹介（各3行）"
    ]
    for i, item in enumerate(news, 1):
        sections.append(
            f"\n━━━━━━━━━━━━━━━━\n🏇 **{i}｜[{item['title']}]({item['url']})**\n"
            f"{item['summary']}"
        )
    sections.append(
        "\n\n━━━━━━━━━━━━━━━━\n**🗓️ 開催情報・チェック先**\n" + race_context()
    )

    text = "\n".join(sections)
    chunks, current = [], ""
    for line in text.splitlines():
        if len(current) + len(line) + 1 > 1900 and current:
            chunks.append(current.rstrip())
            current = ""
        while len(line) > 1900:
            chunks.append(line[:1900])
            line = line[1900:]
        current += line + "\n"
    if current.strip():
        chunks.append(current.rstrip())

    for index, chunk in enumerate(chunks, 1):
        payload = {"content": chunk, "flags": 4, "allowed_mentions": {"parse": []}}
        request = urllib.request.Request(
            WEBHOOK_URL,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            method="POST"
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            print(f"Discord投稿成功: HTTP {response.status} ({index}/{len(chunks)})")


def main():
    print("競馬ニュース取得開始:", datetime.now(JST).isoformat())
    articles = get_articles()
    print(f"候補記事数: {len(articles)}")
    news = summarize(articles)
    post(news)
    print("競馬ニュース配信完了")


if __name__ == "__main__":
    main()
