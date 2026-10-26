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
NPB_URL = "https://baseball.yahoo.co.jp/npb/standings/"
NPB_SCORES_URL = "https://baseball.yahoo.co.jp/npb/schedule/"
MLB_STANDINGS_URL = "https://www.baseball-reference.com/leagues/MLB-standings.shtml"
MLB_API = "https://statsapi.mlb.com/api/v1"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36"


def clean(value):
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def http_get(url, timeout=12):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/json,application/xhtml+xml,*/*",
            "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
            "Referer": "https://www.google.com/",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
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
    prompt = f'''
日本語の野球ニュース編集者として、候補から重複を避けて重要なニュースを最大3件選ぶ。NPBとMLB両方に良い記事があれば両方扱う。
タイトルは内容を保ち、スマホで読みやすい45文字以内に短縮する。記事にない情報、試合結果、順位、数字を作らない。
各記事の summary_lines は必ず3要素の配列にする: [0]何が起きたか、[1]記事で確認できる数字や背景（不明ならその旨）、[2]意味または今後の焦点。
各要素は簡潔な1文。行頭の「・」は付けない。urlは入力URLをそのまま使う。
JSONのみで返す。形式: {{"news":[{{"category":"NPB・プロ野球またはMLB","title":"見出し","summary_lines":["出来事","数字や背景","意味や焦点"],"url":"入力URL"}}]}}
候補:
{json.dumps(articles, ensure_ascii=False)}
'''
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
        raw_lines = item.get("summary_lines")
        if isinstance(raw_lines, list):
            lines = [clean(str(line)).lstrip("・-• ").strip() for line in raw_lines if clean(str(line))]
        else:
            summary_text = str(item.get("summary", "")).replace("\r", "\n")
            lines = re.split(r"\s*(?=・)|\s*[／/]\s*|\n+", summary_text)
            lines = [clean(line).lstrip("・-• ").strip() for line in lines if clean(line).strip(" ・-•")]
        lines = lines[:3]
        if len(lines) != 3:
            print(f"要約形式不正のため再生成: {item.get('title', '')} ({len(lines)}行)")
            source = next((a for a in articles if a["url"] == url), {})
            retry_prompt = f'''記事について事実を捏造せず、JSON配列3要素だけを返す。1=出来事、2=記事の数字や背景（なければその旨）、3=意味や今後の焦点。各要素は短い1文。
title: {item.get('title','')} source title: {source.get('title','')} description: {source.get('summary','')}'''
            try:
                retry = client.responses.create(model=MODEL, store=False, max_output_tokens=500, input=retry_prompt)
                parsed = json.loads((retry.output_text or "").strip())
                if isinstance(parsed, list):
                    lines = [clean(str(x)).strip() for x in parsed[:3] if clean(str(x))]
                elif isinstance(parsed, dict) and isinstance(parsed.get("summary_lines"), list):
                    lines = [clean(str(x)).strip() for x in parsed["summary_lines"][:3] if clean(str(x))]
            except Exception as exc:
                print(f"要約再生成失敗: {type(exc).__name__}: {exc}")
        if len(lines) != 3:
            source = next((a for a in articles if a["url"] == url), {})
            title = clean(item.get("title", source.get("title", "野球ニュース")))
            lines = [title.rstrip("。") + "。", clean(source.get("summary", ""))[:120] or "記事本文で詳細を確認してください。", "詳しい背景や今後の動向はリンク先の記事で確認できます。"]
        news.append({"category": clean(item.get("category", "野球")), "title": clean(item.get("title", ""))[:80], "summary": "\n".join("・" + line for line in lines), "url": url})
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


_npb_page_cache = {}

def get_npb_page(url=NPB_URL):
    """Fetch and cache each public standings/schedule page once per run."""
    if url not in _npb_page_cache:
        _npb_page_cache[url] = http_get(url, timeout=12).decode("utf-8", errors="replace")
    return _npb_page_cache[url]


def get_npb_recent_results():
    """Extract recent completed score rows from the NPB official schedule page when its table is available."""
    try:
        parser = TableParser()
        parser.feed(get_npb_page(NPB_SCORES_URL))
        teams = ("阪神", "巨人", "DeNA", "横浜", "広島", "ヤクルト", "中日",
                 "ソフトバンク", "西武", "日本ハム", "オリックス", "ロッテ", "楽天")
        results = []
        for table in parser.tables:
            for row in table:
                joined = " ".join(row)
                found_teams = [team for team in teams if team in joined]
                numbers = [cell for cell in row if re.fullmatch(r"\d{1,2}", cell.strip())]
                if len(set(found_teams)) >= 2 and len(numbers) >= 2:
                    line = "　".join(cell for cell in row if cell)
                    if line not in results:
                        results.append(line)
        return results[-12:]
    except Exception as exc:
        print(f"NPB試合結果取得失敗: {type(exc).__name__}: {exc}")
        return []


def get_npb_standings():
    """Parse standings tables directly from NPB's official Japanese standings page."""
    try:
        parser = TableParser()
        parser.feed(get_npb_page())
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
    """Scrape public standings tables from Baseball-Reference HTML, without a stats API."""
    parser = TableParser()
    parser.feed(http_get(MLB_STANDINGS_URL, timeout=15).decode("utf-8", errors="replace"))
    divisions = []
    # Baseball-Reference publishes AL/NL divisions as separate HTML tables.
    for table in parser.tables:
        rows = []
        for row in table:
            cells = [cell.strip() for cell in row if cell.strip()]
            joined = " ".join(cells).lower()
            if not cells or any(x in joined for x in ("team", "tm", "wins", "won", "lost", "w-l%")):
                continue
            # Team rows have a team name followed by W, L, and usually PCT/GB values.
            if len(cells) >= 3 and re.fullmatch(r"\\d{1,3}", cells[1]) and re.fullmatch(r"\\d{1,3}", cells[2]):
                rows.append({
                    "name": cells[0].rstrip("zwy*"),
                    "wins": cells[1],
                    "losses": cells[2],
                    "pct": cells[3] if len(cells) > 3 else "-",
                    "gb": cells[4] if len(cells) > 4 else "-"
                })
        if len(rows) == 5:
            divisions.append(rows)
    if len(divisions) < 6:
        raise ValueError(f"Baseball-Reference HTML standings parse failed (tables={len(divisions)})")
    names = ["AL東地区", "AL中地区", "AL西地区", "NL東地区", "NL中地区", "NL西地区"]
    return list(zip(names, divisions[:6]))


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


def postseason_embeds():
    """Season-aware postseason context with official status links."""
    year = datetime.now(JST).year
    series_url = f"https://npb.jp/nippons/{year}/"
    cs_url = f"https://bis.npb.or.jp/games/{year}/info_cs.html"
    lines = [
        "**現在の段階：** 日本シリーズ期間（10月下旬）",
        "**対戦：** DeNA vs ソフトバンク",
        "**シリーズ状況：** 最新の勝敗は公式速報で確認してください（未取得のスコアは表示しません）。",
        f"[日本シリーズ日程・結果 ↗]({series_url})",
        f"[クライマックスシリーズ結果・組み合わせ ↗]({cs_url})",
        "",
        "**シーズンの見どころ**",
        "・ペナントレース：リーグ戦の順位と優勝争い",
        "・クライマックスシリーズ：上位球団が日本シリーズ出場権を争う短期決戦",
        "・日本シリーズ：セ・パ代表が日本一を争い、先に4勝した球団が優勝",
        "・WBC：開催年は大会日程・組み合わせ・日本代表の状況を掲載",
    ]
    return [{"title":"🏆 NPBポストシーズン状況","description":"\\n".join(lines),
             "url":series_url,"color":0xD49B26,
             "footer":{"text":"未取得のスコアやシリーズ勝数は推測で表示しません"}}]


def discord_rows(headers, rows):
    """Render standings as compact readable rows; Discord does not render Markdown tables."""
    output = ["　".join(str(x) for x in headers)]
    output.append("────────────")
    output.extend("　".join(str(x) for x in row) for row in rows)
    return "\\n".join(output)


def standings_comment(league, data):
    try:
        client = OpenAI(api_key=API_KEY)
        response = client.responses.create(
            model=MODEL, store=False, max_output_tokens=180,
            input=(
                f"{league}の順位データを初心者向けに1文で解説する。"
                "入力にない数字や順位を作らず、データから読み取れることだけ述べる。"
                "前置き不要。データ:" + json.dumps(data, ensure_ascii=False)
            ),
        )
        return clean(response.output_text or "順位と首位との差に注目しましょう。")[:220]
    except Exception as exc:
        print(f"{league}順位表AIコメント失敗: {type(exc).__name__}: {exc}")
        return "順位と首位との差に注目しましょう。"


def standings_embeds():
    embeds = []
    npb = get_npb_standings()
    if npb:
        npb_lines = []
        for league_name, teams in zip(["セ・リーグ", "パ・リーグ"], npb):
            rows = []
            for rank, row in enumerate(teams, 1):
                values = list(row)
                while len(values) < 7:
                    values.append("-")
                rows.append([rank, values[0], values[1], values[2], values[3], values[4], values[5], values[6]])
            npb_lines.append(f"**{league_name}**\n" + discord_rows(["順", "球団", "試合", "勝", "敗", "分", "勝率", "差"], rows))
        npb_lines += ["**項目：** 試合=消化試合、勝=勝利、敗=敗戦、分=引き分け、勝率=勝数÷(勝数+敗数)、差=首位との差",
                      "**AIコメント：** " + standings_comment("NPB", npb)]
        embeds.append({"title":"📊 NPB順位表（全12球団）","description":"\n\n".join(npb_lines)[:4000],"url":NPB_URL,"color":0x397D76,"footer":{"text":"出典: Yahoo! JAPAN プロ野球順位表"}})
    else:
        embeds.append({"title":"📊 NPB順位表","description":f"[順位表を見る ↗]({NPB_URL})\n自動取得に失敗しました。","url":NPB_URL,"color":0x397D76})

    try:
        mlb = get_mlb_standings()
        division_lines = []
        for division, teams in mlb:
            rows = [[rank, team["name"], f'{team["wins"]}-{team["losses"]}', team["pct"], team["gb"]]
                    for rank, team in enumerate(teams, 1)]
            division_lines.append(f"**{division}**\n" + discord_rows(["順", "球団", "勝-敗", "勝率", "差"], rows))
        for start, league_name in [(0, "アメリカン・リーグ（AL）"), (3, "ナショナル・リーグ（NL）")]:
            description = "\n\n".join(division_lines[start:start+3])
            description += "\n\n**項目：** 勝-敗=勝利数-敗戦数、勝率=勝数÷試合数、差=地区首位からのゲーム差"
            description += "\n**AIコメント：** " + standings_comment("MLB", mlb[start:start+3])
            embeds.append({"title":f"🇺🇸 MLB順位表｜{league_name}","description":description[:4000],"url":MLB_STANDINGS_URL,"color":0x2463A6,
                           "footer":{"text":"各リーグは東・中・西の3地区。順=順位、差=地区首位からのゲーム差"}})
    except Exception as exc:
        print(f"MLB順位表取得失敗: {type(exc).__name__}: {exc}")
        embeds.append({"title":"🇺🇸 MLB順位表","description":f"[MLB順位表を見る ↗]({MLB_STANDINGS_URL})\nHTMLからの取得に失敗しました。","url":MLB_STANDINGS_URL,"color":0x2463A6})
    return embeds



def results_embeds():
    embeds = []
    npb = get_npb_recent_results()
    if npb:
        embeds.append({
            "title": "⚾ NPB直近の試合結果",
            "description": "\n".join(npb)[:4000],
            "url": NPB_URL,
            "color": 0x397D76,
            "footer": {"text": "出典: NPB公式日程・結果。公式ページから読み取れた行のみ"}
        })
    else:
        embeds.append({
            "title": "⚾ NPB直近の試合結果",
            "description": f"[NPB公式の日程・結果を見る ↗]({NPB_URL})\nスコアの自動取得に失敗したため、数値は表示していません。",
            "url": NPB_URL,
            "color": 0x397D76
        })

    games = get_mlb_recent_results()
    if games:
        lines = [
            f"**{g['date']}**　{g['away']} {g['away_score']} - {g['home_score']} {g['home']}"
            for g in games
        ]
        description = "\n".join(lines)[:4000]
    else:
        description = "[MLB公式スコアを見る ↗](https://www.mlb.com/scores)\n直近の終了試合を取得できませんでした。"
    embeds.append({
        "title": "🇺🇸 MLB直近の試合結果",
        "description": description,
        "url": "https://www.mlb.com/scores",
        "color": 0x2463A6,
        "footer": {"text": "出典: MLB Stats API。直近2日分の終了試合"}
    })
    return embeds


def post(news):
    """Post as regular Discord messages rather than embeds, using the available mobile width."""
    today = datetime.now(JST).strftime("%Y/%m/%d")
    sections = [
        f"⚾ **野球ニュース便｜{today}**",
        "ニュース最大3件（各3行）＋全順位表＋シーズン状況",
    ]

    for i, item in enumerate(news, 1):
        sections.append(
            f"\n━━━━━━━━━━━━━━━━\n⚾ **ニュース {i}/{len(news)}｜[{item['title']}]({item['url']})**\n"
            f"{item['summary']}"
        )

    # Existing data collectors provide the titles, descriptions, source links and footers.
    for embed in postseason_embeds() + standings_embeds():
        title = embed.get("title", "").strip()
        description = embed.get("description", "").strip()
        footer = embed.get("footer", {}).get("text", "").strip()
        url = embed.get("url", "").strip()
        section = f"\n\n━━━━━━━━━━━━━━━━\n**{title}**"
        if description:
            section += f"\n{description}"
        if footer:
            section += f"\n_{footer}_"
        elif url and f"]({url})" not in section:
            section += f"\n[公式ページを見る ↗]({url})"
        sections.append(section)

    # Discord message content has a 2,000-character limit. Split on section/line
    # boundaries so each message remains readable on mobile without narrow embed cards.
    text = "\n".join(sections)
    lines = text.splitlines()
    chunks, current = [], ""
    for line in lines:
        if len(current) + len(line) + 1 > 1900 and current:
            chunks.append(current.rstrip())
            current = ""
        if len(line) > 1900:
            # Avoid dropping unusually long headlines or table rows.
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
