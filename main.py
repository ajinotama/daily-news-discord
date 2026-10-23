import os
import re
import json
import html
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import feedparser
import trafilatura
from openai import OpenAI

JST = timezone(timedelta(hours=9))
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "news.md")
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
MAX_AGE_HOURS_DEFAULT = 48

USER_AGENT = (
    "Mozilla/5.0 (compatible; DailyNewsBriefing/1.0; "
    "+https://github.com/tamabuchi0411/daily-news-discord)"
)

DEFAULTS = {
    "記事の最大経過時間（時間）": "48",
    "RSS取得上限（カテゴリごと）": "12",
    "AIに渡す上限（全体）": "100",
    "Discordに掲載するニュース数": "15",
    "記事本文の取得": "true",
}


# ------------------------------------------------------------
# 設定ファイル
# ------------------------------------------------------------

def get_section(text, heading, level=2):
    hashes = "#" * level
    pattern = (
        rf"(?ms)^{hashes} {re.escape(heading)}\s*\n"
        rf"(.*?)(?=^#{{1,{level}}} |\Z)"
    )
    match = re.search(pattern, text)
    return match.group(1) if match else ""


def parse_bullets(text, heading):
    body = get_section(text, heading)
    return [
        line.strip()[2:].strip()
        for line in body.splitlines()
        if line.strip().startswith("- ")
    ]


def parse_key_values(text, heading):
    result = {}
    for line in get_section(text, heading).splitlines():
        line = line.strip()
        if line.startswith("- ") and ": " in line:
            key, value = line[2:].split(": ", 1)
            result[key.strip()] = value.strip()
    return result


def load_config():
    if not os.path.isfile(CONFIG_PATH):
        raise FileNotFoundError(
            f"設定ファイルが見つかりません: {CONFIG_PATH}"
        )

    with open(CONFIG_PATH, "r", encoding="utf-8") as file:
        text = file.read()

    settings = dict(DEFAULTS)
    settings.update(parse_key_values(text, "共通設定"))

    categories = []
    category_section = get_section(text, "カテゴリ")

    pattern = r"(?ms)^### (.+?)\s*\n(.*?)(?=^### |\Z)"

    for match in re.finditer(pattern, category_section):
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
            continue

        try:
            limit = int(
                values.get(
                    "記事上限",
                    settings["RSS取得上限（カテゴリごと）"],
                )
            )
        except ValueError:
            limit = 12

        categories.append({
            "name": name,
            "query": query,
            "limit": max(1, limit),
            "focus": values.get("重要視する観点", ""),
        })

    if not categories:
        raise RuntimeError(
            "news.mdからカテゴリを読み込めません。"
            "各カテゴリに「- 検索語: ...」を設定してください。"
        )

    try:
        max_age_hours = int(
            settings["記事の最大経過時間（時間）"]
        )
        if max_age_hours <= 0:
            raise ValueError
    except ValueError:
        max_age_hours = MAX_AGE_HOURS_DEFAULT

    return {
        "settings": settings,
        "max_age_hours": max_age_hours,
        "exclude": parse_bullets(text, "除外キーワード"),
        "categories": categories,
        "text": text,
    }


# ------------------------------------------------------------
# 日時・RSS取得
# ------------------------------------------------------------

def clean_text(value):
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def parse_published(entry):
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None

    try:
        from calendar import timegm
        return datetime.fromtimestamp(
            timegm(parsed),
            tz=timezone.utc,
        )
    except (ValueError, OverflowError, OSError):
        return None


def build_feed_url(query):
    params = {
        "q": query,
        "hl": "ja",
        "gl": "JP",
        "ceid": "JP:ja",
    }
    return (
        GOOGLE_NEWS_RSS
        + "?"
        + urllib.parse.urlencode(params)
    )


def fetch_news(config):
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=config["max_age_hours"])

    try:
        total_limit = int(
            config["settings"]["AIに渡す上限（全体）"]
        )
    except ValueError:
        total_limit = 100

    exclude_words = [
        word.lower() for word in config["exclude"]
    ]

    articles = []
    seen_links = set()
    rejected_old = 0
    rejected_unknown_date = 0

    print("ニュース取得開始:", datetime.now(JST).isoformat())
    print(
        f"記事の最大経過時間: {config['max_age_hours']}時間"
    )

    for category in config["categories"]:
        print(f"RSS取得中: {category['name']}")

        try:
            feed = feedparser.parse(
                build_feed_url(category["query"])
            )

            count = 0

            for entry in feed.entries:
                title = clean_text(entry.get("title", ""))
                link = entry.get("link", "").strip()
                summary = clean_text(entry.get("summary", ""))

                if not title or not link or link in seen_links:
                    continue

                published_at = parse_published(entry)

                # 公開日時が分からない記事は安全側に倒して除外
                if published_at is None:
                    rejected_unknown_date += 1
                    continue

                # 配信時点から48時間を超えた記事を除外
                if published_at < cutoff:
                    rejected_old += 1
                    continue

                combined = f"{title} {summary}".lower()

                if any(word in combined for word in exclude_words):
                    continue

                seen_links.add(link)

                articles.append({
                    "category": category["name"],
                    "title": title,
                    "link": link,
                    "rss_summary": summary[:2000],
                    "published": published_at.isoformat(),
                    "published_dt": published_at,
                    "focus": category["focus"],
                })

                count += 1

                if count >= category["limit"]:
                    break

        except Exception as exc:
            print(
                f"RSS取得エラー ({category['name']}): "
                f"{type(exc).__name__}: {exc}"
            )

    # カテゴリ別の候補を交互に並べる
    by_category = {}
    for article in articles:
        by_category.setdefault(
            article["category"], []
        ).append(article)

    balanced = []
    depth = 0

    while len(balanced) < total_limit:
        added = False

        for category in config["categories"]:
            items = by_category.get(category["name"], [])

            if depth < len(items):
                balanced.append(items[depth])
                added = True

                if len(balanced) >= total_limit:
                    break

        if not added:
            break

        depth += 1

    print(f"48時間超で除外: {rejected_old}件")
    print(f"公開日時不明で除外: {rejected_unknown_date}件")
    print(f"AIに渡す記事数: {len(balanced)}件")

    return balanced


# ------------------------------------------------------------
# 記事本文の取得
# ------------------------------------------------------------

def fetch_article_text(article):
    try:
        downloaded = trafilatura.fetch_url(article["link"])

        if downloaded:
            extracted = trafilatura.extract(
                downloaded,
                include_comments=False,
                include_tables=False,
                favor_precision=True,
            )

            if extracted and len(extracted) >= 250:
                article["article_text"] = extracted[:9000]
                article["body_source"] = "本文"
                return article

    except Exception as exc:
        print(
            f"本文取得失敗: {article['title'][:50]} "
            f"({type(exc).__name__})"
        )

    article["article_text"] = article["rss_summary"]
    article["body_source"] = "RSS概要"
    return article


def fetch_article_bodies(articles, enabled=True):
    if not enabled:
        for article in articles:
            article["article_text"] = article["rss_summary"]
            article["body_source"] = "RSS概要"
        return articles

    print("記事本文の取得開始")
    results = []

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [
            pool.submit(fetch_article_text, article)
            for article in articles
        ]

        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                print(
                    f"本文取得エラー: {type(exc).__name__}: {exc}"
                )

    by_link = {
        article["link"]: article
        for article in results
    }

    output = [
        by_link.get(article["link"], article)
        for article in articles
    ]

    full_count = sum(
        1 for article in output
        if article.get("body_source") == "本文"
    )

    print(f"本文取得成功: {full_count}/{len(output)}件")
    return output


# ------------------------------------------------------------
# AIによる選定・要約
# ------------------------------------------------------------

def parse_json_output(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if match:
            return json.loads(match.group(0))
        raise


def shorten_title(title, max_length=70):
    title = clean_text(title)
    title = re.sub(r"\s[-|｜]\s[^-|｜]{2,40}$", "", title)
    title = re.sub(r"\s+", " ", title).strip()

    if len(title) <= max_length:
        return title

    return title[:max_length - 1].rstrip() + "…"


def summarize_news(articles, config):
    try:
        max_news = int(
            config["settings"]["Discordに掲載するニュース数"]
        )
    except ValueError:
        max_news = 15

    if not articles:
        raise RuntimeError(
            "48時間以内のニュースがありません。"
        )

    client = OpenAI(api_key=OPENAI_API_KEY)

    candidates = []

    for idx, article in enumerate(articles, start=1):
        candidates.append({
            "id": idx,
            "category": article["category"],
            "title": article["title"],
            "published": article["published"],
            "body_source": article["body_source"],
            "article_text": article["article_text"][:7000],
            "url": article["link"],
        })

    prompt = f"""
あなたは日本語のニュース編集者です。
以下の記事候補から、幅広い分野のニュースを最大{max_news}件選んでください。

設定:
{config["text"][:7000]}

記事候補:
{json.dumps(candidates, ensure_ascii=False)}

必須ルール:
- スポーツだけに偏らず、候補にある複数分野を幅広く選ぶ。
- 同一カテゴリは原則2件までにする。ただし候補が不足する場合は柔軟に判断する。
- 重要度ランキングや点数付けはしない。
- 本文を取得できている記事は、本文に書かれた具体的な情報を読んで要約する。
- 「話題になっている」「注目されている」だけの抽象的な要約は禁止。
- タイトルは短く具体的に編集する。配信元名などの余計な文言は削除する。
- summaryは必ず箇条書き3行にする。
- 各行は「・」から始める。各行は原則1文で、具体的な事実を含める。
- 1行目は主な出来事や結果。
- 2行目は具体的な数字、順位、背景、関係者の発言など、記事にある詳細。
- 3行目はそのニュースの意味、影響、今後の焦点。
- ただし記事に書かれていない情報を推測で補わない。材料がなければ分かる範囲で簡潔にする。
- 記事本文が取れずRSS概要しかない場合、概要にない詳細を作らない。
- URLは入力されたものをそのまま使う。
- idも入力された番号をそのまま使う。
- JSON以外の文章は出力しない。

出力形式:
{{
  "news": [
    {{
      "id": 1,
      "title": "短く具体的な見出し",
      "summary": "・1行目\\n・2行目\\n・3行目",
      "url": "入力URL"
    }}
  ],
  "today_points": "今日のニュース全体を横断する短いまとめ。1〜2文。"
}}
"""

    print("AIによる本文要約・選定開始")

    response = client.responses.create(
        model=OPENAI_MODEL,
        store=False,
        max_output_tokens=9000,
        input=prompt,
    )

    raw = response.output_text or ""

    try:
        result = parse_json_output(raw)
        if not isinstance(result, dict) or not isinstance(
            result.get("news"), list
        ):
            raise ValueError("news配列がありません。")
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"AI出力解析エラー: {exc}")
        print("AI出力の先頭:", raw[:500])
        raise RuntimeError(
            "AIの出力を解析できませんでした。"
        )

    valid_articles = {
        idx: article
        for idx, article in enumerate(articles, start=1)
    }

    selected = []
    seen_ids = set()

    for item in result["news"]:
        try:
            article_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue

        source = valid_articles.get(article_id)

        if not source or article_id in seen_ids:
            continue

        seen_ids.add(article_id)

        title = shorten_title(
            item.get("title") or source["title"]
        )

        summary = str(item.get("summary", "")).strip()

        # 3行の箇条書き形式に整える
        lines = [
            line.strip().lstrip("・-• ").strip()
            for line in summary.splitlines()
            if line.strip()
        ]
        lines = lines[:3]

        while len(lines) < 3:
            # 内容を捏造して行数を埋めない
            lines.append("詳細は記事本文で確認できます。")

        summary = "\n".join(
            f"・{line}" for line in lines
        )

        selected.append({
            "category": source["category"],
            "title": title,
            "summary": summary,
            "url": source["link"],
        })

        if len(selected) >= max_news:
            break

    if not selected:
        raise RuntimeError(
            "AIが有効なニュースを返しませんでした。"
        )

    return {
        "news": selected,
        "today_points": str(
            result.get("today_points", "")
        ).strip()[:1000],
    }


# ------------------------------------------------------------
# Discord表示
# ------------------------------------------------------------

CATEGORY_ICONS = {
    "プロ野球・高校野球": "⚾",
    "MLB・海外野球": "⚾",
    "サッカー": "⚽",
    "バスケットボール・バレーボール": "🏀",
    "その他のスポーツ": "🏅",
    "日本政治・行政": "🏛️",
    "国際情勢": "🌍",
    "経済・ビジネス": "💹",
    "IT・AI・セキュリティ": "💻",
    "科学・研究・新発見": "🔬",
    "社会・暮らし・制度": "🏙️",
    "ゲーム・VTuber": "🎮",
    "音楽・漫画・ディズニー": "🎵",
    "世界を知る・教養トピック": "💡",
}

CATEGORY_COLORS = {
    "プロ野球・高校野球": 0x397D76,
    "MLB・海外野球": 0x397D76,
    "サッカー": 0x397D76,
    "バスケットボール・バレーボール": 0x397D76,
    "その他のスポーツ": 0x397D76,
    "日本政治・行政": 0xB85C5C,
    "国際情勢": 0x6A78B8,
    "経済・ビジネス": 0xB88745,
    "IT・AI・セキュリティ": 0x397D76,
    "科学・研究・新発見": 0x6A8F55,
    "社会・暮らし・制度": 0x8A6FB0,
    "ゲーム・VTuber": 0x9A63A8,
    "音楽・漫画・ディズニー": 0xC16B8A,
    "世界を知る・教養トピック": 0x3C8D73,
}


def build_embeds(result):
    today = datetime.now(JST).strftime("%m/%d")
    news = result["news"]

    embeds = [{
        "title": f"🗞️ 朝のニュース便　{today}",
        "description": (
            f"今日の{len(news)}トピック\n"
            "幅広い分野から、要点を3行でお届けします。"
        ),
        "color": 0x3478A8,
    }]

    for index, item in enumerate(news, start=1):
        icon = CATEGORY_ICONS.get(
            item["category"], "📰"
        )

        title = shorten_title(item["title"], 75)
        description = (
            f"{item['summary']}\n\n"
            f"[続きはこちら ↗]({item['url']})"
        )

        embeds.append({
            "title": f"{icon} {title}",
            "description": description[:4000],
            "color": CATEGORY_COLORS.get(
                item["category"], 0x5865F2
            ),
            "footer": {
                "text": f"{index:02d} / {len(news)}"
            },
        })

    if result.get("today_points"):
        embeds.append({
            "title": "🌱 今日の横断メモ",
            "description": result["today_points"][:1000],
            "color": 0x3C8D73,
        })

    return embeds


def post_to_discord(result):
    embeds = build_embeds(result)

    # Discord Webhookは1回につき最大10 embeds
    chunks = [
        embeds[i:i + 10]
        for i in range(0, len(embeds), 10)
    ]

    for index, chunk in enumerate(chunks, start=1):
        payload = {
            "embeds": chunk,
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
            with urllib.request.urlopen(
                request, timeout=30
            ) as response:
                print(
                    f"Discord投稿成功: HTTP {response.status} "
                    f"({index}/{len(chunks)})"
                )

        except urllib.error.HTTPError as exc:
            print(f"Discord HTTP error: {exc.code}")

            for key in (
                "Server",
                "Content-Type",
                "CF-Ray",
                "Retry-After",
            ):
                if exc.headers.get(key):
                    print(f"{key}: {exc.headers.get(key)}")

            try:
                print(
                    "Discord error response:",
                    exc.read().decode(
                        "utf-8", errors="replace"
                    )[:1500],
                )
            except Exception:
                pass

            raise


# ------------------------------------------------------------
# メイン処理
# ------------------------------------------------------------

def main():
    config = load_config()

    print(
        "設定カテゴリ:",
        ", ".join(
            category["name"]
            for category in config["categories"]
        ),
    )

    articles = fetch_news(config)

    if not articles:
        raise RuntimeError(
            "48時間以内の記事を取得できませんでした。"
            "news.mdの検索語やRSSの公開日時を確認してください。"
        )

    enabled = (
        config["settings"]
        .get("記事本文の取得", "true")
        .lower()
        not in ("false", "no", "0")
    )

    articles = fetch_article_bodies(
        articles,
        enabled=enabled,
    )

    result = summarize_news(articles, config)
    post_to_discord(result)

    print("ニュース配信完了")


if __name__ == "__main__":
    main()
