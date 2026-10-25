import os
import json
import html
import re
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from datetime import datetime, timezone, timedelta
from calendar import timegm

import feedparser
from openai import OpenAI

JST = timezone(timedelta(hours=9))
API_KEY = os.environ["OPENAI_API_KEY"]
WEBHOOK_URL = os.environ["DISCORD_BASEBALL_WEBHOOK_URL"]
MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
RSS_URL = "https://news.google.com/rss/search"
NPB_URL = "https://npb.jp/scores/"
MLB_API = "https://statsapi.mlb.com/api/v1"
USER_AGENT = "Mozilla/5.0 (compatible; DailyBaseballBriefing/1.0)"


def clean(value):
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def http_get(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=25) as response:
        return response.read()


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
    queries = [
        ("NPB・プロ野球", "プロ野球 OR NPB OR セ・リーグ OR パ・リーグ"),
        ("MLB", "MLB OR メジャーリーグ OR 大谷翔平 OR 山本由伸"),
    ]
    articles, seen = [], set()
    for category, query in queries:
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
                "category": category, "title": title,
                "summary": clean(entry.get("summary", ""))[:1800],
                "url": link, "published": date.isoformat()
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
日本語の野球ニュース編集者として、NPBとMLBの候補から重複を避けて重要なニュースを最大3件選ぶ。
記事にない情報、試合結果、順位、数字を作らない。NPBとMLB両方に良い記事があれば両方扱う。
各 summary は必ず3行で、各行は「・」から始める:
1行目: 何が起きたか
2行目: 記事で確認できる数字や背景
3行目: 意味または今後の焦点。推測を事実のように書かない
urlは入力URLをそのまま使う。
JSONのみで返す。形式:
{{"news":[{{"category":"NPB・プロ野球またはMLB","title":"見出し","summary":"・出来事\\\\n・数字や背景\\\\n・意味や焦点","url":"入力URL"}}]}}
候補:
{json.dumps(articles, ensure_ascii=False)}
"""
    response = client.responses.create(model=MODEL, store=False, max_output_tokens=3500, input=prompt)
    raw = (response.output_text or "").strip()
    raw = re.sub(r"^\x60{3}(?:json)?\s*|\s*\x60{3}$", "", raw)
    result = json.loads(raw)
    valid_urls = {a["url"] for a in articles}
    news = []
    for item in result.get("news", [])[:3]:
        url = item.get("url", "")
        if url not in valid_urls:
            continue
        lines = [clean(line).lstrip("・-• ").strip() for line in str(item.get("summary", "")).splitlines() if clean(line)]
        lines = lines[:3]
        while len(lines) < 3:
            lines.append("詳細は記事本文で確認できます。")
        news.append({
            "category": clean(item.get("category", "野球")),
            "title": clean(item.get("title", ""))[:240],
            "summary": "\n".join("・" + line for line in lines),
            "url": url
        })
    if not news:
        raise RuntimeError("AIが有効な野球ニュースを返しませんでした。")
    return news


class TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables = []
        self.table = []
        self.row = []
        self.cell = []
        self.in_table = self.in_row = self.in_cell = False

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.in_table, self.table = True, []
        elif self.in_table and tag == "tr":
            self.in_row, self.row = True, []
        elif self.in_row and tag in ("td", "th"):
            self.in_cell, self.cell = True, []

    def handle_data(self, data):
        if self.in_cell:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.in_cell:
            self.row.append(clean(" ".join(self.cell)))
            self.in_cell = False
        elif tag == "tr" and self.in_row:
            if self.row:
                self.table.append(self.row)
            self.in_row = False
        elif tag == "table" and self.in_table:
            self.tables.append(self.table)
            self.in_table = False


def get_npb_standings():
    """Parse standings tables directly from NPB's official Japanese standings page."""
    try:
        parser = TableParser()
        parser.feed(http_get(NPB_URL).decode("utf-8", errors="replace"))
        found = []
        for table in parser.tables:
            header_index = None
            for i, row in enumerate(table):
                joined = " ".join(row)
                if ("勝率" in joined or "勝" in joined) and "敗" in joined and ("試合" in joined or "勝率" in joined):
                    header_index = i
                    break
            if header_index is None:
                continue
            rows = []
            for row in table[header_index + 1:]:
                joined = " ".join(row)
                if not any(name in joined for name in ("阪神", "巨人", "DeNA", "広島", "ヤクルト", "中日", "ソフトバンク", "西武", "日本ハム", "オリックス", "ロッテ", "楽天")):
                    continue
                row = [v for v in row if v]
                if len(row) >= 5:
                    rows.append(row)
            if len(rows) >= 6:
                found.append(rows[:6])
        if len(found) >= 2:
            return found[:2]
        raise ValueError("公式ページの順位表を識別できませんでした")
    except Exception as exc:
        print(f"NPB順位表取得失敗: {type(exc).__name__}: {exc}")
        return []


def get_mlb_standings():
    params = urllib.parse.urlencode({
        "leagueId": "103,104",
        "season": datetime.now(JST).year,
        "standingsTypes": "regularSeason",
    })
    data = json.loads(http_get(MLB_API + "/standings?" + params).decode("utf-8"))
    result = []
    for record in data.get("records", []):
        division = record.get("division", {}).get("name", "Division")
        rows = []
        for team in record.get("teamRecords", []):
            rows.append({
                "name": team.get("team", {}).get("name", "Unknown"),
                "wins": team.get("wins", 0),
                "losses": team.get("losses", 0),
                "pct": team.get("winningPercentage", "-"),
                "gb": team.get("gamesBack", "-"),
                "rank": team.get("divisionRank", "?"),
            })
        result.append((division, rows))
    return result


def get_mlb_recent_results():
    """Yesterday's completed MLB games, based on US date, via MLB Stats API."""
    today_jst = datetime.now(JST).date()
    # US games can finish after midnight JST; check yesterday and today JST dates.
    dates = [today_jst - timedelta(days=1), today_jst]
    games = []
    for day in dates:
        params = urllib.parse.urlencode({
            "sportId": 1, "date": day.isoformat(),
            "hydrate": "team,linescore"
        })
        try:
            data = json.loads(http_get(MLB_API + "/schedule?" + params).decode("utf-8"))
            for date_block in data.get("dates", []):
                for game in date_block.get("games", []):
                    status = game.get("status", {}).get("abstractGameState", "")
                    if status != "Final":
                        continue
                    away = game.get("teams", {}).get("away", {})
                    home = game.get("teams", {}).get("home", {})
                    games.append({
                        "date": day.isoformat(),
                        "away": away.get("team", {}).get("name", "Away"),
                        "away_score": away.get("score", "-"),
                        "home": home.get("team", {}).get("name", "Home"),
                        "home_score": home.get("score", "-"),
                        "url": "https://www.mlb.com/scores"
                    })
        except Exception as exc:
            print(f"MLB試合結果取得失敗 ({day}): {type(exc).__name__}: {exc}")
    unique = {}
    for game in games:
        key = (game["date"], game["away"], game["home"])
        unique[key] = game
    return list(unique.values())[-15:]


def standings_embeds():
    embeds = []
    npb = get_npb_standings()
    if npb:
        for league, rows in zip(("セ・リーグ", "パ・リーグ"), npb):
            lines = []
            for rank, row in enumerate(rows, 1):
                # NPB official table columns commonly: team, games, wins, losses, ties, pct, games back.
                team = row[0]
                stats = "　".join(row[1:])
                lines.append(f"{rank}. **{team}**　{stats}")
            embeds.append({
                "title": f"📊 NPB {league}順位表",
                "description": "\n".join(lines)[:4000],
                "url": NPB_URL,
                "color": 0x397D76,
                "footer": {"text": "出典: NPB公式 勝敗表"}
            })
    else:
        embeds.append({
            "title": "📊 NPB セ・パ順位表",
            "description": f"[NPB公式の勝敗表を見る ↗]({NPB_URL})\n順位データの自動取得に失敗したため、数値は表示していません。",
            "url": NPB_URL,
            "color": 0x397D76
        })

    try:
        mlb = get_mlb_standings()
        for division, rows in mlb:
            lines = [
                f"{row['rank']}. **{row['name']}**　{row['wins']}-{row['losses']}　勝率 {row['pct']}　差 {row['gb']}"
                for row in rows
            ]
            embeds.append({
                "title": "🇺🇸 MLB " + division,
                "description": "\n".join(lines)[:4000],
                "url": "https://www.mlb.com/standings",
                "color": 0x2463A6,
                "footer": {"text": "出典: MLB Stats API / レギュラーシーズン"}
            })
    except Exception as exc:
        print(f"MLB順位表取得失敗: {type(exc).__name__}: {exc}")
        embeds.append({
            "title": "🇺🇸 MLB順位表",
            "description": "[MLB公式順位表を見る ↗](https://www.mlb.com/standings)\n自動取得に失敗したため、数値は表示していません。",
            "url": "https://www.mlb.com/standings",
            "color": 0x2463A6
        })
    return embeds


def results_embeds():
    games = get_mlb_recent_results()
    if not games:
        return [{
            "title": "⚾ MLB直近の試合結果",
            "description": "[MLB公式スコアを見る ↗](https://www.mlb.com/scores)\n直近の終了試合を取得できませんでした。",
            "url": "https://www.mlb.com/scores",
            "color": 0x2463A6
        }]
    lines = [
        f"**{g['date']}**　{g['away']} {g['away_score']} - {g['home_score']} {g['home']}"
        for g in games
    ]
    return [{
        "title": "⚾ MLB直近の試合結果",
        "description": "\n".join(lines)[:4000],
        "url": "https://www.mlb.com/scores",
        "color": 0x2463A6,
        "footer": {"text": "出典: MLB Stats API。直近2日分の終了試合"}
    }]


def post(news):
    today = datetime.now(JST).strftime("%Y/%m/%d")
    embeds = [{
        "title": f"⚾ 野球ニュース便　{today}",
        "description": "野球ニュース最大3件（各3行）＋NPB・MLB順位表＋MLB直近試合結果。",
        "color": 0x397D76
    }]
    for i, item in enumerate(news, 1):
        embeds.append({
            "title": f"⚾ {item['title']}"[:256],
            "description": f"{item['summary']}\n\n[記事を読む ↗]({item['url']})"[:4000],
            "color": 0x397D76 if "NPB" in item["category"] or "プロ野球" in item["category"] else 0x2463A6,
            "footer": {"text": f"ニュース {i} / {len(news)}"}
        })
    embeds.extend(results_embeds())
    embeds.extend(standings_embeds())
    # Discord accepts at most 10 embeds per message; send all sections in batches.
    chunks = [embeds[i:i + 10] for i in range(0, len(embeds), 10)]
    for index, chunk in enumerate(chunks, 1):
        payload = {"embeds": chunk, "allowed_mentions": {"parse": []}}
        request = urllib.request.Request(
            WEBHOOK_URL,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                print(f"Discord投稿成功: HTTP {response.status} ({index}/{len(chunks)})")
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
