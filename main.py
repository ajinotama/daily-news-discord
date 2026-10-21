
import os
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

import feedparser
from openai import OpenAI


# ============================================================
# 設定
# ============================================================

JST = timezone(timedelta(hours=9))

# 既存のGitHub Secretsから読み込む
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

# 既存コードで使用していたモデル名を維持
OPENAI_MODEL = "gpt-6-luna"

# Google News RSS
NEWS_FEEDS = {
    "スポーツ": (
        "https://news.google.com/rss/search?"
        "q=%E3%82%B9%E3%83%9D%E3%83%BC%E3%83%84"
        "&hl=ja&gl=JP&ceid=JP:ja"
    ),
    "日本政治": (
        "https://news.google.com/rss/search?"
        "q=%E6%97%A5%E6%9C%AC%E6%94%BF%E6%B2%BB"
        "&hl=ja&gl=JP&ceid=JP:ja"
    ),
    "国際情勢": (
        "https://news.google.com/rss/search?"
        "q=%E5%9B%BD%E9%9A%9B%E6%83%85%E5%8B%A2"
        "&hl=ja&gl=JP&ceid=JP:ja"
    ),
}

MAX_ARTICLES_PER_FEED = 15
MAX_ARTICLES_TOTAL = 45


# ============================================================
# ニュース取得
# ============================================================

def fetch_news():
    print("ニュース取得開始:", datetime.now(JST).isoformat())

    articles = []
    seen_links = set()

    for category, feed_url in NEWS_FEEDS.items():
        print(f"RSS取得中: {category}")

        try:
            feed = feedparser.parse(feed_url)

            if feed.bozo and not feed.entries:
                print(f"RSS取得失敗: {category}")
                continue

            count = 0

            for entry in feed.entries:
                title = entry.get("title", "").strip()
                link = entry.get("link", "").strip()
                summary = entry.get("summary", "").strip()

                if not title or not link:
                    continue

                if link in seen_links:
                    continue

                seen_links.add(link)

                articles.append({
                    "category": category,
                    "title": title,
                    "link": link,
                    "summary": summary,
                    "published": entry.get("published", ""),
                })

                count += 1

                if count >= MAX_ARTICLES_PER_FEED:
                    break

        except Exception as e:
            print(f"RSS取得エラー ({category}): {type(e).__name__}: {e}")

    articles = articles[:MAX_ARTICLES_TOTAL]

    print(f"取得記事数: {len(articles)}")
    return articles


# ============================================================
# AI要約
# ============================================================

def summarize_news(articles):
    if not articles:
        return "本日はニュースを取得できませんでした。"

    client = OpenAI(api_key=OPENAI_API_KEY)

    article_text = []

    for i, article in enumerate(articles, start=1):
        article_text.append(
            f"[{i}] カテゴリ: {article['category']}\n"
            f"タイトル: {article['title']}\n"
            f"概要: {article['summary'][:500]}\n"
            f"公開日時: {article['published']}\n"
            f"URL: {article['link']}"
        )

    prompt = f"""
あなたは日本語のニュース編集者です。
以下のRSS記事をもとに、日本語で「今日の5分ニュース」を作成してください。

【対象分野】
- スポーツ
- 日本政治
- 国際情勢

【編集方針】
- 重要度と読者への影響を重視する
- 重要なニュースを優先し、似た内容の記事はまとめる
- 記事に書かれていない事実を推測して断定しない
- 情報が不十分な場合は、そのことが分かる表現にする
- 可能な範囲で日付や数字を明確にする
- 各ニュースに元記事のURLを付ける
- 日本語で分かりやすく、簡潔に書く

【出力形式】
📰 今日の重要ニュース

■ スポーツ
・ニュースの見出し
  何が起きたか、なぜ重要か
  出典: URL

■ 日本政治
・ニュースの見出し
  何が起きたか、なぜ重要か
  出典: URL

■ 国際情勢
・ニュースの見出し
  何が起きたか、なぜ重要か
  出典: URL

■ 今日のポイント
全体を通して押さえておきたい点を2〜3文でまとめる。

重要なニュースがない分野について、無理にニュースを作らないでください。
記事のタイトルや概要は未検証の情報を含む可能性があります。
提供された記事情報の範囲内で要約してください。

【記事一覧】
{chr(10).join(article_text)}
"""

    print("AI要約開始")

    response = client.responses.create(
        model=OPENAI_MODEL,
        store=False,
        max_output_tokens=1800,
        input=prompt,
    )

    briefing = response.output_text.strip()

    if not briefing:
        raise RuntimeError("AIから空の要約が返されました。")

    print(f"AI要約完了: {len(briefing)}文字")
    return briefing


# ============================================================
# Discord投稿
# ============================================================

def post_to_discord(message):
    # Discordの通常メッセージ上限は2000文字。
    # 長い場合は複数のメッセージに分割する。
    chunks = []
    remaining = message

    while remaining:
        if len(remaining) <= 1900:
            chunks.append(remaining)
            break

        split_at = remaining.rfind("\n", 0, 1900)

        if split_at < 500:
            split_at = 1900

        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip()

    for index, chunk in enumerate(chunks, start=1):
        payload = {
            "content": chunk,
            "allowed_mentions": {"parse": []},
        }

        request = urllib.request.Request(
            DISCORD_WEBHOOK_URL,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                print(
                    f"Discord投稿成功: "
                    f"HTTP {response.status} "
                    f"({index}/{len(chunks)})"
                )

        except urllib.error.HTTPError as e:
            print(f"Discord HTTP error: {e.code}")

            # 原因調査に必要なヘッダーだけ出力
            for key in (
                "Server",
                "Content-Type",
                "CF-Ray",
                "Retry-After",
            ):
                value = e.headers.get(key)
                if value:
                    print(f"{key}: {value}")

            try:
                error_body = e.read().decode(
                    "utf-8",
                    errors="replace",
                )
                print(f"Discord error response: {error_body[:2000]}")
            except Exception:
                print("エラー本文を取得できませんでした。")

            raise

        except urllib.error.URLError as e:
            print(f"Discord接続エラー: {e.reason}")
            raise


# ============================================================
# メイン処理
# ============================================================

def main():
    articles = fetch_news()

    if not articles:
        raise RuntimeError(
            "ニュースを取得できませんでした。RSSの状態を確認してください。"
        )

    briefing = summarize_news(articles)
    now = datetime.now(JST)

    message = (
        f"📰 **今日の5分ニュース**\n"
        f"{now:%Y年%m月%d日}\n\n"
        f"{briefing}"
    )

    post_to_discord(message)

    print("ニュース配信完了")


if __name__ == "__main__":
    main()
