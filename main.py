
import os
import json
import urllib.request
import urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo

import feedparser
from openai import OpenAI

# ニュースの検索テーマ。記事はGoogle News RSSから取得します。
TOPICS = [
    "日本 プロ野球 MLB",
    "サッカー Jリーグ 海外サッカー",
    "バスケットボール Bリーグ NBA",
    "ゴルフ テニス バレーボール",
    "日本 政治 国会 政策",
    "国際情勢 外交 紛争",
    "海外政治 選挙 政府",
]

MODEL = "gpt-6-luna"
MAX_ARTICLES_PER_TOPIC = 6


def fetch_articles():
    articles = []
    seen_urls = set()

    for topic in TOPICS:
        params = urllib.parse.urlencode({
            "q": topic,
            "hl": "ja",
            "gl": "JP",
            "ceid": "JP:ja",
        })
        url = f"https://news.google.com/rss/search?{params}"
        feed = feedparser.parse(url)

        for entry in feed.entries[:MAX_ARTICLES_PER_TOPIC]:
            link = entry.get("link", "")
            title = entry.get("title", "").strip()

            if not title or not link or link in seen_urls:
                continue

            seen_urls.add(link)
            articles.append({
                "topic": topic,
                "title": title,
                "link": link,
                "published": entry.get("published", "日時不明"),
                "summary": entry.get("summary", "")[:500],
            })

    return articles


def make_briefing(articles):
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    # RSSの記事情報をモデルに渡し、重要ニュースを選んでもらいます。
    # 記事本文は指示ではなく、評価対象のデータとして扱います。
    response = client.responses.create(
        model=MODEL,
        store=False,
        max_output_tokens=1800,
        instructions=(
            "あなたは日本語のニュース編集者です。"
            "入力された記事候補だけを根拠に、朝の5分ニュースを作成してください。"
            "スポーツ、日本政治、国際情勢・海外政治をなるべく幅広く扱い、"
            "重要度・影響度・話題性を基準に3〜5件選んでください。"
            "同じ出来事の重複を避け、古い記事より新しい記事を優先してください。"
            "各項目に、見出し、何が起きたか、背景・なぜ重要か、"
            "今後の注目点、会話のきっかけを簡潔に書いてください。"
            "記事候補に含まれない事実やURLを作らないでください。"
            "選んだ記事のURLは入力されたURLをそのまま使用してください。"
            "事実と推測を混同せず、候補に情報がない場合は断定しないでください。"
            "最初に日付と全体の要点を短く書いてください。"
        ),
        input=json.dumps(articles, ensure_ascii=False),
    )

    return response.output_text.strip()


def post_to_discord(message):
    webhook = os.environ["DISCORD_WEBHOOK_URL"]

    # Discordのメッセージ上限を超えないよう分割します。
    chunks = []
    while message:
        if len(message) <= 1800:
            chunks.append(message)
            break

        split_at = message.rfind("\n", 0, 1800)
        if split_at < 500:
            split_at = 1800

        chunks.append(message[:split_at])
        message = message[split_at:].lstrip()

    for chunk in chunks:
        payload = json.dumps({
            "content": chunk,
            "allowed_mentions": {"parse": []},
        }).encode("utf-8")

        request = urllib.request.Request(
            webhook,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with urllib.request.urlopen(request, timeout=20) as response:
            if response.status not in (200, 204):
                raise RuntimeError(
                    f"Discordへの投稿に失敗しました: HTTP {response.status}"
                )


def main():
    now = datetime.now(ZoneInfo("Asia/Tokyo"))
    print(f"ニュース取得開始: {now.isoformat()}")

    articles = fetch_articles()
    print(f"取得記事数: {len(articles)}")

    if not articles:
        raise RuntimeError("RSSから記事を取得できませんでした。")

    briefing = make_briefing(articles)
    if not briefing:
        raise RuntimeError("GPTから要約を取得できませんでした。")

    post_to_discord(f"📰 **今日の5分ニュース**\n{now:%Y年%m月%d日}\n\n{briefing}")
    print("Discordへの投稿が完了しました。")


if __name__ == "__main__":
    main()
