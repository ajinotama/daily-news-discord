import os
import json
import re
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

import feedparser
from openai import OpenAI


# ============================================================
# 基本設定
# ============================================================

JST = timezone(timedelta(hours=9))
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
OPENAI_MODEL = "gpt-6-luna"

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "news.md")
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
USER_AGENT = (
    "DiscordBot "
    "(https://github.com/tamabuchi0411/daily-news-discord, 1.0)"
)

DEFAULTS = {
    "当日記事のみ": "false",
    "RSS取得上限（カテゴリごと）": "20",
    "AIに渡す上限（全体）": "45",
    "Discordに掲載するニュース数": "8",
    "重要度の最低値": "5",
    "優先キーワード一致による加点上限": "5",
}


# ============================================================
# Markdown設定の読み込み
# ============================================================

def parse_bullet_section(text, heading):
    """指定見出しの配下にある箇条書きを取得する。"""
    pattern = rf"(?ms)^## {re.escape(heading)}\s*\n(.*?)(?=^## |\Z)"
    match = re.search(pattern, text)
    if not match:
        return []

    items = []
    for line in match.group(1).splitlines():
        line = line.strip()
        if line.startswith("- "):
            value = line[2:].strip()
            if value:
                items.append(value)
    return items


def parse_key_value_section(text, heading):
    """指定見出し配下の '- キー: 値' を辞書にする。"""
    result = {}
    pattern = rf"(?ms)^## {re.escape(heading)}\s*\n(.*?)(?=^## |\Z)"
    match = re.search(pattern, text)
    if not match:
        return result

    for line in match.group(1).splitlines():
        line = line.strip()
        if line.startswith("- ") and ": " in line:
            key, value = line[2:].split(": ", 1)
            result[key.strip()] = value.strip()
    return result


def load_config():
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(
            f"設定ファイルがありません: {CONFIG_PATH}"
        )

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        text = f.read()

    common = parse_key_value_section(text, "共通設定")
    settings = dict(DEFAULTS)
    settings.update(common)

    priority = parse_bullet_section(text, "優先キーワード")
    exclude = parse_bullet_section(text, "除外キーワード")

    categories = []
    category_pattern = (
        r"(?ms)^### (.+?)\s*\n(.*?)(?=^### |\Z)"
    )

    category_section_match = re.search(
        r"(?ms)^## カテゴリ\s*\n(.*?)(?=^## |\Z)",
        text,
    )
    category_text = category_section_match.group(1) if category_section_match else ""

    for match in re.finditer(category_pattern, category_text):
        name = match.group(1).strip()
        body = match.group(2)
        values = {}

        for line in body.splitlines():
            line = line.strip()
            if line.startswith("- ") and ": " in line:
                key, value = line[2:].split(": ", 1)
                values[key.strip()] = value.strip()

        query = values.get("検索語", "").strip()
        if not query:
            print(f"カテゴリをスキップ（検索語なし）: {name}")
            continue

        try:
            limit = int(values.get(
                "記事上限",
                settings["RSS取得上限（カテゴリごと）"]
            ))
        except ValueError:
            limit = int(settings["RSS取得上限（カテゴリごと）"])

        categories.append({
            "name": name,
            "query": query,
            "limit": max(1, limit),
            "focus": values.get("重要視する観点", ""),
        })

    if not categories:
        raise RuntimeError(
            "news.mdからカテゴリを読み込めませんでした。"
            "各カテゴリに「- 検索語: ...」を設定してください。"
        )

    return {
        "settings": settings,
        "priority": priority,
        "exclude": exclude,
        "categories": categories,
        "text": text,
    }


# ============================================================
# ニュース取得・一次選別
# ============================================================

def build_feed_url(query):
    params = {
        "q": query,
        "hl": "ja",
        "gl": "JP",
        "ceid": "JP:ja",
    }
    return GOOGLE_NEWS_RSS + "?" + urllib.parse.urlencode(params)


def parse_published(entry):
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    try:
        from datetime import datetime
        from time import mktime
        return datetime.fromtimestamp(mktime(parsed), tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def article_priority(article, keywords):
    text = f"{article['title']} {article['summary']}".lower()
    return sum(1 for keyword in keywords if keyword.lower() in text)


def fetch_news(config):
    print("ニュース取得開始:", datetime.now(JST).isoformat())

    try:
        per_feed_limit = int(
            config["settings"]["RSS取得上限（カテゴリごと）"]
        )
    except ValueError:
        per_feed_limit = 20

    try:
        total_limit = int(config["settings"]["AIに渡す上限（全体）"])
    except ValueError:
        total_limit = 45

    only_today = (
        config["settings"].get("当日記事のみ", "false").lower()
        in ("true", "yes", "1")
    )

    priority_keywords = config["priority"]
    exclude_keywords = config["exclude"]
    articles = []
    seen_links = set()
    now_jst = datetime.now(JST)
    start_of_day = now_jst.replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    for category in config["categories"]:
        print(f"RSS取得中: {category['name']}")
        url = build_feed_url(category["query"])

        try:
            feed = feedparser.parse(url)
            if feed.bozo and not feed.entries:
                print(f"RSS取得失敗: {category['name']}")
                continue

            category_count = 0
            limit = min(category["limit"], per_feed_limit)

            for entry in feed.entries:
                title = entry.get("title", "").strip()
                link = entry.get("link", "").strip()
                summary = re.sub(
                    r"<[^>]+>", " ", entry.get("summary", "")
                ).strip()

                if not title or not link or link in seen_links:
                    continue

                combined = f"{title} {summary}".lower()
                if any(word.lower() in combined for word in exclude_keywords):
                    continue

                published_dt = parse_published(entry)
                if only_today and published_dt:
                    if published_dt.astimezone(JST) < start_of_day:
                        continue

                seen_links.add(link)
                article = {
                    "category": category["name"],
                    "title": title,
                    "link": link,
                    "summary": summary[:800],
                    "published": entry.get("published", ""),
                    "priority_score": article_priority(
                        {"title": title, "summary": summary},
                        priority_keywords,
                    ),
                    "focus": category["focus"],
                }
                articles.append(article)
                category_count += 1

                if category_count >= limit:
                    break

        except Exception as e:
            print(
                f"RSS取得エラー ({category['name']}): "
                f"{type(e).__name__}: {e}"
            )

    articles.sort(key=lambda item: item["priority_score"], reverse=True)
    articles = articles[:max(1, total_limit)]

    print(f"取得記事数（一次選別後）: {len(articles)}")
    return articles


# ============================================================
# AIによる選定・要約
# ============================================================

def extract_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


def summarize_news(articles, config):
    if not articles:
        return {
            "news": [],
            "today_points": "本日は条件に合うニュースを取得できませんでした。",
        }

    try:
        max_news = int(
            config["settings"]["Discordに掲載するニュース数"]
        )
    except ValueError:
        max_news = 8

    try:
        min_importance = int(
            config["settings"]["重要度の最低値"]
        )
    except ValueError:
        min_importance = 5

    client = OpenAI(api_key=OPENAI_API_KEY)

    article_text = []
    for i, article in enumerate(articles, start=1):
        article_text.append({
            "id": i,
            "category": article["category"],
            "title": article["title"],
            "summary": article["summary"][:500],
            "published": article["published"],
            "url": article["link"],
            "priority_score": article["priority_score"],
            "category_focus": article["focus"],
        })

    prompt = f"""
あなたは日本語のニュース編集者です。
入力されたRSS記事だけを根拠に、今日読む価値の高いニュースを選定・要約してください。

設定ファイルに記載された方針:
{config["text"][:10000]}

候補記事:
{json.dumps(article_text, ensure_ascii=False)}

要件:
- 最大{max_news}件を選ぶ。重要なニュースが少なければ無理に埋めない。
- 重要度は1〜10の整数。社会的影響、緊急性、今後への影響を評価する。
- 類似記事を重複して選ばない。
- 入力にない事実を作らない。裏付けが弱い場合は断定を避ける。
- 要約は2〜3文。注目理由は1〜2文。
- urlは候補記事のURLをそのまま使い、変更しない。
- idは候補記事のidを指定する。
- categoryは候補記事のカテゴリを使用する。
- 重要度が高い順に並べる。
- JSON以外の文章やMarkdownを出力しない。

出力形式:
{{
  "news": [
    {{
      "id": 1,
      "category": "国際情勢",
      "title": "ニュース見出し",
      "summary": "何が起きたかを説明する要約",
      "why_it_matters": "注目する理由",
      "importance": 8,
      "url": "候補記事のURL"
    }}
  ],
  "today_points": "今日のニュース全体から押さえるべき点を2〜3文でまとめる"
}}
"""

    print("AI要約・選定開始")

    response = client.responses.create(
        model=OPENAI_MODEL,
        store=False,
        max_output_tokens=2500,
        input=prompt,
    )

    output_text = response.output_text.strip()
    if not output_text:
        raise RuntimeError("AIから空の要約が返されました。")

    try:
        result = extract_json(output_text)
        if not isinstance(result, dict) or not isinstance(result.get("news"), list):
            raise ValueError("AI出力のJSON形式が想定と異なります。")

        valid_by_id = {
            i: article for i, article in enumerate(articles, start=1)
        }
        clean_news = []

        for item in result["news"]:
            try:
                article_id = int(item.get("id"))
            except (ValueError, TypeError):
                continue

            source = valid_by_id.get(article_id)
            if not source:
                continue

            try:
                importance = int(item.get("importance", 5))
            except (ValueError, TypeError):
                importance = 5

            if importance < min_importance:
                continue

            # URLはAIの返却値を信用せず、候補記事から採用する
            clean_news.append({
                "category": source["category"],
                "title": str(item.get("title") or source["title"])[:256],
                "summary": str(item.get("summary", "")).strip()[:1500],
                "why_it_matters": str(
                    item.get("why_it_matters", "")
                ).strip()[:1000],
                "importance": max(1, min(10, importance)),
                "url": source["link"],
            })

        clean_news.sort(key=lambda item: item["importance"], reverse=True)
        clean_news = clean_news[:max_news]

        print(f"AI選定完了: {len(clean_news)}件")
        return {
            "news": clean_news,
            "today_points": str(
                result.get("today_points", "")
            ).strip()[:1500],
        }

    except (json.JSONDecodeError, ValueError, TypeError) as e:
        print(f"AI出力の解析に失敗: {e}")
        print("フォールバック: 優先キーワード上位の記事を掲載します。")

        fallback = []
        for article in articles[:max_news]:
            fallback.append({
                "category": article["category"],
                "title": article["title"][:256],
                "summary": article["summary"][:1500] or "概要情報なし。",
                "why_it_matters": (
                    "優先キーワードによる一次選別で候補になった記事です。"
                ),
                "importance": max(
                    min_importance,
                    min(10, 5 + article["priority_score"]),
                ),
                "url": article["link"],
            })

        return {
            "news": fallback,
            "today_points": "AIによる選定に失敗したため、キーワードで選んだ記事を表示しています。",
        }


# ============================================================
# Discord Embed投稿
# ============================================================

def importance_color(importance):
    if importance >= 8:
        return 0xE74C3C  # 赤
    if importance >= 6:
        return 0xF1C40F  # 黄
    return 0x3498DB  # 青


def build_embeds(result):
    today = datetime.now(JST).strftime("%Y年%m月%d日")
    embeds = [{
        "title": "📰 今日の5分ニュース",
        "description": (
            f"{today}\n"
            f"選定ニュース: {len(result['news'])}件"
        ),
        "color": 0x5865F2,
    }]

    for item in result["news"]:
        description_parts = []

        if item["summary"]:
            description_parts.append(
                f"**要約**\n{item['summary']}"
            )
        if item["why_it_matters"]:
            description_parts.append(
                f"**注目する理由**\n{item['why_it_matters']}"
            )
        description_parts.append(
            f"[元記事を読む]({item['url']})"
        )

        description = "\n\n".join(description_parts)
        if len(description) > 4000:
            description = description[:3950] + "\n…"

        embeds.append({
            "title": item["title"][:256],
            "url": item["url"],
            "description": description,
            "color": importance_color(item["importance"]),
            "fields": [
                {
                    "name": "カテゴリ",
                    "value": item["category"][:1024],
                    "inline": True,
                },
                {
                    "name": "重要度",
                    "value": f"{item['importance']}/10",
                    "inline": True,
                },
            ],
        })

    if result.get("today_points"):
        embeds.append({
            "title": "💡 今日のポイント",
            "description": result["today_points"][:4000],
            "color": 0x2ECC71,
        })

    return embeds


def post_to_discord(result):
    embeds = build_embeds(result)

    # Discord Webhookは1リクエストあたり最大10 embeds。
    # ニュース数が多い場合は10件ずつ送る。
    chunks = [embeds[i:i + 10] for i in range(0, len(embeds), 10)]

    for index, embed_chunk in enumerate(chunks, start=1):
        payload = {
            "embeds": embed_chunk,
            "allowed_mentions": {"parse": []},
        }

        request = urllib.request.Request(
            DISCORD_WEBHOOK_URL,
            data=json.dumps(
                payload,
                ensure_ascii=False,
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                print(
                    f"Discord投稿成功: HTTP {response.status} "
                    f"({index}/{len(chunks)})"
                )

        except urllib.error.HTTPError as e:
            print(f"Discord HTTP error: {e.code}")

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
    config = load_config()
    print(
        "設定カテゴリ:",
        ", ".join(category["name"] for category in config["categories"]),
    )

    articles = fetch_news(config)

    if not articles:
        raise RuntimeError(
            "ニュースを取得できませんでした。"
            "news.mdの検索語やRSSの状態を確認してください。"
        )

    result = summarize_news(articles, config)
    post_to_discord(result)

    print("ニュース配信完了")


if __name__ == "__main__":
    main()
