#!/usr/bin/env python3
# ============================================================
#  app.py  ―  中央競馬オッズ急変モニター 完全版（1ファイル）
#
#  【インストール】Anaconda Prompt で:
#    pip install flask requests beautifulsoup4 lxml
#
#  【起動】
#    python app.py
#
#  【ブラウザで開く】
#    http://localhost:5000
# ============================================================

# ━━ 設定 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CHECK_INTERVAL_SEC    = 60     # オッズ確認頻度（秒）
MONITOR_BEFORE_MIN    = 600     # 発走N分前から自動監視開始
ODDS_CHANGE_THRESHOLD = 1.0   # 急変とみなす変化率（%）
REQUEST_TIMEOUT       = 15
REQUEST_INTERVAL_SEC  = 1
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

import os, re, time, threading, datetime, sys, webbrowser
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from urllib.parse import urlparse, parse_qs
from collections import deque
from flask import Flask, jsonify, Response
import requests
from bs4 import BeautifulSoup

app = Flask(__name__)

JST = datetime.timezone(datetime.timedelta(hours=9), "JST")

def now_jst():
    return datetime.datetime.now(JST).replace(tzinfo=None)

# ── グローバル状態 ─────────────────────────────────────────
state = {
    "races":        [],          # 全レース（終了済み含む）
    "odds":         {},          # {race_id: [horse, ...]}
    "prev_odds":    {},          # {race_id: {number: odds}}
    "alerts":       deque(maxlen=200),
    "last_updated": {},          # {race_id: "HH:MM:SS"}
    "monitored":    set(),       # 監視中race_idセット
    "finished":     set(),       # 終了済みrace_idセット
    "log":          deque(maxlen=300),  # システムログ
}
lock = threading.Lock()

# ── HTTP ──────────────────────────────────────────────────
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})

def _get(url, params=None):
    try:
        r = SESSION.get(url, params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        r.encoding = r.apparent_encoding
        return BeautifulSoup(r.text, "lxml")
    except Exception as e:
        _log(f"[HTTP ERROR] {url}: {e}")
        return None

def _log(msg):
    ts = now_jst().strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    print(entry, flush=True)
    with lock:
        state["log"].appendleft(entry)

# ── 時刻パーサー ──────────────────────────────────────────
def _parse_time(text):
    now = now_jst()
    m = re.search(r"(\d{1,2}):(\d{2})", text)
    if m:
        return now.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                           second=0, microsecond=0)
    return now + datetime.timedelta(hours=2)

# ── レース一覧取得 ────────────────────────────────────────
def fetch_today_races():
    _log("本日のレース一覧を取得中...")
    today = now_jst().date()
    soup = _get(
        "https://race.netkeiba.com/top/race_list_sub.html",
        params={"kaisai_date": today.strftime("%Y%m%d")}
    )
    if not soup:
        _log("レース一覧の取得に失敗しました")
        return []

    races = []
    seen = set()

    # li.RaceList_Item 固定ではなく、race_id を含むリンクを直接探す
    links = soup.select("a[href*='race_id=']")
    _log(f"race_idリンク: {len(links)}件")

    for a in links:
        href = a.get("href", "")
        qs = parse_qs(urlparse(href).query)
        race_id = qs.get("race_id", [None])[0]

        if not race_id:
            m = re.search(r"race_id=(\d+)", href)
            race_id = m.group(1) if m else None

        if not race_id or race_id in seen:
            continue

        seen.add(race_id)

        # レース情報が入っていそうな親要素を広めに見る
        box = (
            a.find_parent("li", class_="RaceList_Item")
            or a.find_parent("div", class_="RaceList_Item")
            or a.find_parent(["li", "tr", "dd", "div"])
            or a
        )

        name_tag = (
            box.select_one(".RaceName")
            or box.select_one(".RaceNameTxt")
            or box.select_one(".ItemTitle")
            or a
        )
        race_name = name_tag.get_text(" ", strip=True)

        time_tag = (
            box.select_one(".RaceTime")
            or box.select_one(".RaceList_Itemtime")
            or box.select_one(".time")
        )
        time_text = time_tag.get_text(" ", strip=True) if time_tag else box.get_text(" ", strip=True)
        start_time = _parse_time(time_text)

        venue_tag = (
            box.select_one(".RaceCource")
            or box.select_one(".RaceCourse")
            or box.select_one(".venue")
        )
        venue = venue_tag.get_text(" ", strip=True) if venue_tag else ""

        course_map = {
            "01": "札幌",
            "02": "函館",
            "03": "福島",
            "04": "新潟",
            "05": "東京",
            "06": "中山",
            "07": "中京",
            "08": "京都",
            "09": "阪神",
            "10": "小倉",
        }

        venue = course_map.get(race_id[4:6], venue)

        r_num = race_id[-2:].lstrip("0") or "?"

        races.append({
            "race_id":    race_id,
            "name":       race_name,
            "full_name":  f"{venue} {r_num}R {race_name}".strip(),
            "start_time": start_time.strftime("%H:%M"),
            "start_dt":   start_time.isoformat(),
            "venue":      venue,
            "r_num":      r_num,
        })

    _log(f"本日のレース: {len(races)}件 取得完了")
    return races
# ── オッズ取得 ────────────────────────────────────────────
driver = None

def get_driver():
    global driver
    if driver is None:
        options = Options()
        options.add_argument("--headless")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument(f"user-agent={USER_AGENT}")

        driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()),
            options=options
        )
    return driver


def fetch_odds(race_id):
    url = f"https://race.netkeiba.com/odds/index.html?race_id={race_id}&type=b1"
    drv = get_driver()
    drv.get(url)
    time.sleep(6)

    horses = []
    rows = drv.find_elements(By.CSS_SELECTOR, "tbody tr")

    for row in rows:
        try:
            cols = row.find_elements(By.TAG_NAME, "td")
            if len(cols) < 4:
                continue

            num = int(cols[0].text.strip())
            name = cols[1].text.strip()
            odds_text = cols[-2].text.strip().replace("倍", "")
            odds = float(odds_text)

            horses.append({
                "number": num,
                "name": name,
                "odds": odds
            })
        except:
            continue

    return sorted(horses, key=lambda x: x["number"])

# ── 急変検知 ──────────────────────────────────────────────
def detect_changes(race, prev_map, curr):
    for h in curr:
        old = prev_map.get(h["number"])
        if old is None or old <= 0:
            continue
        pct = (h["odds"] - old) / old * 100
        if abs(pct) >= ODDS_CHANGE_THRESHOLD:
            direction = "急落 ⬇" if h["odds"] < old else "急騰 ⬆"
            alert = {
                "time":       now_jst().strftime("%H:%M:%S"),
                "race":       race["full_name"],
                "race_id":    race["race_id"],
                "horse_num":  h["number"],
                "horse_name": h["name"],
                "old_odds":   old,
                "new_odds":   h["odds"],
                "change_pct": round(pct, 1),
                "direction":  direction,
            }
            with lock:
                state["alerts"].appendleft(alert)
            _log(f"🚨 急変検知! {race['full_name']} {h['number']}番 {h['name']} "
                 f"{old}倍→{h['odds']}倍 ({direction} {abs(pct):.1f}%)")

# ── メイン監視ループ ──────────────────────────────────────
def monitor_loop():
    """
    バックグラウンドで常時動作。
    発走30分前になったレースを自動で監視開始。
    発走5分経過後は監視終了（終了済みに移行）。
    """
    _log("監視スレッド起動")
    while True:
        try:
            with lock:
                races = list(state["races"])

            now = now_jst()

            for race in races:
                race_id = race["race_id"]
                start_dt = datetime.datetime.fromisoformat(race["start_dt"])
                mt = (start_dt - now).total_seconds() / 60  # 発走までの分数

                # 終了済み判定（発走5分以上経過）
                if mt < -5:
                    with lock:
                        state["finished"].add(race_id)
                        state["monitored"].discard(race_id)
                    continue

                # 監視対象外（まだ30分前でない）
                if mt > MONITOR_BEFORE_MIN:
                    continue

                # ── 監視対象 ──────────────────────────────
                with lock:
                    is_new = race_id not in state["monitored"]
                    state["monitored"].add(race_id)

                if is_new:
                    _log(f"▶ 監視開始: {race['full_name']} "
                         f"(発走 {race['start_time']} / あと{mt:.0f}分)")

                horses = fetch_odds(race_id)
                if not horses:
                    continue

                with lock:
                    prev_map = state["prev_odds"].get(race_id, {})

                if prev_map:
                    detect_changes(race, prev_map, horses)
                else:
                    _log(f"  初回オッズ取得: {race['full_name']} ({len(horses)}頭)")

                with lock:
                    state["odds"][race_id]         = horses
                    state["prev_odds"][race_id]    = {h["number"]: h["odds"] for h in horses}
                    state["last_updated"][race_id] = now.strftime("%H:%M:%S")

                time.sleep(REQUEST_INTERVAL_SEC)

        except Exception as e:
            _log(f"[MONITOR ERROR] {e}")

        time.sleep(CHECK_INTERVAL_SEC)

# 初回レース取得スレッド
def init_races():
    time.sleep(1)
    races = fetch_today_races()
    with lock:
        state["races"] = races

threading.Thread(target=init_races,  daemon=True).start()
threading.Thread(target=monitor_loop, daemon=True).start()

# ── API ───────────────────────────────────────────────────
@app.route("/api/races/refresh")
def api_races_refresh():
    races = fetch_today_races()
    with lock:
        state["races"] = races
    return jsonify(races)

@app.route("/api/races")
def api_races():
    with lock:
        races     = list(state["races"])
        monitored = set(state["monitored"])
        finished  = set(state["finished"])

    result = []
    now = now_jst()

    for r in races:
        race_id  = r["race_id"]
        start_dt = datetime.datetime.fromisoformat(r["start_dt"])
        mt       = (start_dt - now).total_seconds() / 60

        if mt < -5:
            status = "finished"
        elif race_id in monitored:
            status = "monitoring"
        else:
            status = "waiting"

        result.append({**r, "status": status, "mt": round(mt, 1)})

    return jsonify(result)

def _race_by_id(race_id):
    with lock:
        races = list(state["races"])
    return next((r for r in races if r["race_id"] == race_id), None)

@app.route("/api/odds/<race_id>")
def api_odds(race_id):
    # キャッシュがあればそれを、なければ新規取得
    with lock:
        cached = state["odds"].get(race_id)
        updated = state["last_updated"].get(race_id, "")
    if cached:
        return jsonify({"horses": cached, "updated": updated, "cached": True})

    horses = fetch_odds(race_id)
    updated = now_jst().strftime("%H:%M:%S")
    with lock:
        state["odds"][race_id]         = horses
        state["prev_odds"][race_id]    = {h["number"]: h["odds"] for h in horses}
        state["last_updated"][race_id] = updated
    return jsonify({"horses": horses,
                    "updated": updated,
                    "cached": False})

@app.route("/api/odds/<race_id>/force")
def api_odds_force(race_id):
    horses = fetch_odds(race_id)
    updated = now_jst().strftime("%H:%M:%S")
    race = _race_by_id(race_id)
    with lock:
        prev_map = state["prev_odds"].get(race_id, {})

    if horses and prev_map and race:
        detect_changes(race, prev_map, horses)

    with lock:
        state["odds"][race_id]         = horses
        state["prev_odds"][race_id]    = {h["number"]: h["odds"] for h in horses}
        state["last_updated"][race_id] = updated
    return jsonify({"horses": horses,
                    "updated": updated,
                    "cached": False})

@app.route("/api/alerts")
def api_alerts():
    with lock:
        return jsonify(list(state["alerts"]))

@app.route("/api/log")
def api_log():
    with lock:
        return jsonify(list(state["log"]))

@app.route("/api/status")
def api_status():
    with lock:
        return jsonify({
            "race_count":  len(state["races"]),
            "monitored":   list(state["monitored"]),
            "finished":    list(state["finished"]),
            "alert_count": len(state["alerts"]),
        })

# ── HTML ──────────────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#08090d">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="JRAオッズ">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/static/icon-192.png">
<title>🏇 JRA オッズ急変モニター</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@400;700;900&family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>
:root {
  --bg:#08090d; --surface:#0f1117; --surface2:#161820;
  --border:#1f2235; --border2:#2a2d40;
  --accent:#f0c040; --accent-dim:#7a6020;
  --up:#34d399; --up-dim:#0d4a30;
  --down:#f87171; --down-dim:#4a1010;
  --waiting:#60a5fa; --waiting-dim:#0d2a4a;
  --text:#d4d8e8; --muted:#5a5f7a; --muted2:#3a3f55;
  --mono:'JetBrains Mono',monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;overflow:hidden}
body{background:var(--bg);color:var(--text);font-family:'Noto Sans JP',sans-serif;display:flex;flex-direction:column}

/* ヘッダー */
header{
  flex-shrink:0;
  background:var(--surface);
  border-bottom:1px solid var(--border);
  padding:0 24px;
  height:56px;
  display:flex;align-items:center;justify-content:space-between;
  gap:16px;
}
.logo{font-size:1.1rem;font-weight:900;white-space:nowrap;letter-spacing:.02em}
.logo em{color:var(--accent);font-style:normal}
.header-stats{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.stat-chip{
  padding:3px 10px;border-radius:12px;font-size:.72rem;
  font-family:var(--mono);font-weight:700;white-space:nowrap;
}
.chip-mon{background:var(--up-dim);color:var(--up);border:1px solid var(--up)}
.chip-wait{background:var(--waiting-dim);color:var(--waiting);border:1px solid var(--waiting)}
.chip-done{background:var(--muted2);color:var(--muted);border:1px solid var(--border2)}
.chip-alert{background:#4a1a0d;color:#fca5a5;border:1px solid var(--down)}
.header-btn{
  padding:5px 14px;border-radius:6px;font-size:.8rem;cursor:pointer;
  font-family:'Noto Sans JP',sans-serif;font-weight:700;white-space:nowrap;
  border:1px solid var(--border2);background:var(--surface2);color:var(--text);
  transition:all .15s;
}
.header-btn:hover{border-color:var(--accent);color:var(--accent)}

/* レイアウト */
.layout{flex:1;display:grid;grid-template-columns:280px 1fr;min-height:0}


.venue-tabs{
  flex-shrink:0;
  display:flex;
  gap:6px;
  padding:8px 10px;
  border-bottom:1px solid var(--border);
  overflow-x:auto;
  background:var(--surface);
}
.venue-tab{
  padding:5px 10px;
  border-radius:999px;
  border:1px solid var(--border2);
  background:var(--surface2);
  color:var(--muted);
  font-size:.75rem;
  font-weight:700;
  white-space:nowrap;
  cursor:pointer;
  transition:all .15s;
}
.venue-tab:hover{
  color:var(--text);
  border-color:var(--accent-dim);
}
.venue-tab.active{
  color:var(--accent);
  border-color:var(--accent);
  background:#201a0a;
}

/* サイドバー */
.sidebar{
  border-right:1px solid var(--border);
  display:flex;flex-direction:column;overflow:hidden;
  background:var(--surface);
}
.sb-head{
  flex-shrink:0;padding:12px 14px;
  border-bottom:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;
}
.sb-head-title{font-size:.75rem;color:var(--muted);letter-spacing:.1em;text-transform:uppercase;font-family:var(--mono)}
.race-list{flex:1;overflow-y:auto;padding:4px 0}

.venue-title{
  position:sticky;
  top:0;
  z-index:2;
  padding:7px 14px;
  background:#0b0d13;
  border-top:1px solid var(--border);
  border-bottom:1px solid var(--border);
  color:var(--accent);
  font-size:.72rem;
  font-family:var(--mono);
  font-weight:700;
  letter-spacing:.08em;
}
.venue-group:first-child .venue-title{border-top:none}

/* レースアイテム */
.race-item{
  padding:10px 14px;cursor:pointer;
  border-left:3px solid transparent;
  transition:background .12s,border-color .12s;
  display:flex;flex-direction:column;gap:3px;
}
.race-item+.race-item{border-top:1px solid var(--border)}
.race-item:hover{background:var(--surface2)}
.race-item.active{background:var(--surface2);border-left-color:var(--accent)}
.race-item.s-monitoring{border-left-color:var(--up)}
.race-item.s-finished{opacity:.55}
.ri-top{display:flex;align-items:center;gap:6px}
.ri-badge{
  font-size:.62rem;padding:1px 6px;border-radius:10px;
  font-family:var(--mono);font-weight:700;flex-shrink:0;
}
.badge-mon{background:var(--up-dim);color:var(--up)}
.badge-wait{background:var(--waiting-dim);color:var(--waiting)}
.badge-done{background:var(--muted2);color:var(--muted)}
.ri-name{font-size:.85rem;font-weight:700;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ri-bottom{display:flex;justify-content:space-between;font-size:.72rem;font-family:var(--mono);color:var(--muted)}
.ri-time{color:var(--accent)}
.ri-status-mon{color:var(--up)}
.ri-status-wait{color:var(--waiting)}
.ri-status-done{color:var(--muted)}

/* メイン */
.main{display:flex;flex-direction:column;overflow:hidden;background:var(--bg)}
.tabs{
  flex-shrink:0;display:flex;
  border-bottom:1px solid var(--border);
  background:var(--surface);
}
.tab{
  padding:10px 20px;cursor:pointer;font-size:.82rem;font-weight:700;
  border-bottom:2px solid transparent;color:var(--muted);
  transition:all .15s;white-space:nowrap;
}
.tab.active{border-bottom-color:var(--accent);color:var(--accent)}
.tab:hover:not(.active){color:var(--text)}
.tab-content{flex:1;overflow-y:auto;padding:20px 24px}
.tab-panel{display:none}
.tab-panel.active{display:block}

/* オッズパネル */
.odds-bar{
  display:flex;align-items:center;justify-content:space-between;
  margin-bottom:14px;flex-wrap:wrap;gap:8px;
}
.odds-race-name{font-size:1rem;font-weight:900}
.odds-meta{display:flex;align-items:center;gap:8px}
.updated-tag{font-size:.72rem;color:var(--muted);font-family:var(--mono)}
.reload-btn{
  padding:4px 12px;border-radius:5px;font-size:.75rem;cursor:pointer;
  border:1px solid var(--border2);background:var(--surface2);color:var(--text);
  transition:all .15s;font-family:'Noto Sans JP',sans-serif;
}
.reload-btn:hover{border-color:var(--accent);color:var(--accent)}

table{width:100%;border-collapse:collapse}
th{
  text-align:left;padding:7px 10px;font-size:.7rem;
  color:var(--muted);letter-spacing:.08em;text-transform:uppercase;
  border-bottom:1px solid var(--border);font-family:var(--mono);
}
td{padding:9px 10px;border-bottom:1px solid #0e1018;font-size:.88rem}
tr:hover td{background:#0f1118}
.hn{
  display:inline-flex;align-items:center;justify-content:center;
  width:26px;height:26px;border-radius:50%;background:var(--surface2);
  font-size:.75rem;font-weight:700;font-family:var(--mono);border:1px solid var(--border2);
}
.odds-num{
  font-family:var(--mono);font-size:.95rem;font-weight:700;
  color:var(--accent);
}
.odds-num.low{color:var(--down)}
.odds-num.high{color:var(--up)}
.pop-tag{font-family:var(--mono);font-size:.75rem;color:var(--muted)}

/* アラートパネル */
.alerts-bar{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}
.alerts-title{font-size:.95rem;font-weight:900}
.clear-btn{
  padding:4px 12px;border-radius:5px;font-size:.75rem;cursor:pointer;
  border:1px solid var(--down-dim);background:transparent;color:var(--down);
  transition:all .15s;font-family:'Noto Sans JP',sans-serif;
}
.clear-btn:hover{background:var(--down-dim)}
.alert-cards{display:flex;flex-direction:column;gap:8px}
.alert-card{
  background:var(--surface);border:1px solid var(--border);
  border-radius:8px;padding:12px 16px;
  display:grid;grid-template-columns:64px 1fr auto;gap:12px;align-items:center;
  animation:fadeUp .25s ease;
}
@keyframes fadeUp{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:none}}
.alert-card.down{border-left:3px solid var(--down)}
.alert-card.up{border-left:3px solid var(--up)}
.ac-time{font-family:var(--mono);font-size:.7rem;color:var(--muted);text-align:center}
.ac-body{display:flex;flex-direction:column;gap:2px}
.ac-race{font-size:.7rem;color:var(--muted)}
.ac-horse{font-weight:700;font-size:.88rem}
.ac-change{font-family:var(--mono);font-size:.9rem;font-weight:700;text-align:right;white-space:nowrap}
.ac-change.down{color:var(--down)}
.ac-change.up{color:var(--up)}
.ac-pct{font-size:.75rem}

/* ログパネル */
.log-list{display:flex;flex-direction:column;gap:4px}
.log-entry{
  font-family:var(--mono);font-size:.75rem;color:var(--muted);
  padding:4px 8px;border-radius:4px;background:var(--surface);
  border:1px solid var(--border);
  animation:fadeUp .2s ease;
}
.log-entry.alert{color:var(--down);background:#1a0a0a}
.log-entry.monitor{color:var(--up);background:#0a1a0f}

/* 空状態 */
.empty{text-align:center;color:var(--muted);padding:60px 0;font-size:.88rem;line-height:2}

/* トースト */
#toast{
  position:fixed;bottom:20px;right:20px;z-index:9999;
  background:#0f1a10;border:1px solid var(--up);border-radius:8px;
  padding:12px 18px;max-width:300px;display:none;
  animation:fadeUp .3s ease;box-shadow:0 4px 24px #000a;
}
.toast-title{font-weight:700;color:var(--up);margin-bottom:3px;font-size:.85rem}
.toast-body{font-size:.78rem;color:var(--text);font-family:var(--mono)}

/* スクロールバー */
::-webkit-scrollbar{width:4px;height:4px}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:4px}
::-webkit-scrollbar-track{background:transparent}

/* パルスアニメ */
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.pulse{animation:pulse 1.5s infinite}
</style>
</head>
<body>

<header>
  <div class="logo">🏇 <em>JRA</em> オッズ急変モニター</div>
  <div class="header-stats" id="header-stats">
    <span class="stat-chip chip-wait">読み込み中...</span>
  </div>
  <button class="header-btn" onclick="forceRefreshRaces()">🔄 レース再取得</button>
</header>

<div class="layout">
  <!-- サイドバー -->
  <aside class="sidebar">
    <div class="sb-head">
  <span class="sb-head-title">本日のレース</span>
  <span id="race-count" style="font-size:.72rem;color:var(--muted);font-family:var(--mono)">-件</span>
</div>

<div class="venue-tabs" id="venue-tabs"></div>

<div class="race-list" id="race-list">
      <div class="empty">読み込み中...</div>
    </div>
  </aside>

  <!-- メイン -->
  <main class="main">
    <div class="tabs">
      <div class="tab active" onclick="switchTab('odds')">📊 オッズ</div>
      <div class="tab" onclick="switchTab('alerts')">🚨 急変アラート <span id="alert-badge"></span></div>
      <div class="tab" onclick="switchTab('log')">📋 ログ</div>
    </div>
    <div class="tab-content">

      <!-- オッズタブ -->
      <div id="tab-odds" class="tab-panel active">
        <div class="empty" id="odds-ph">← 左のレースをクリックでオッズを確認</div>
        <div id="odds-main" style="display:none">
          <div class="odds-bar">
            <div class="odds-race-name" id="odds-title"></div>
            <div class="odds-meta">
              <span class="updated-tag" id="odds-updated"></span>
              <button class="reload-btn" onclick="reloadOdds()">⟳ 再取得</button>
            </div>
          </div>
          <table>
            <thead><tr>
              <th>番</th><th>馬名</th><th>単勝オッズ</th><th>人気</th>
            </tr></thead>
            <tbody id="odds-tbody"></tbody>
          </table>
        </div>
      </div>

      <!-- アラートタブ -->
      <div id="tab-alerts" class="tab-panel">
        <div class="alerts-bar">
          <div class="alerts-title">急変アラートログ
            <span style="font-size:.75rem;color:var(--muted);font-weight:400">
              （閾値 ±THRESHOLD_VAL%）
            </span>
          </div>
          <button class="clear-btn" onclick="clearAlerts()">クリア</button>
        </div>
        <div class="alert-cards" id="alert-cards">
          <div class="empty">急変はまだ検知されていません</div>
        </div>
      </div>

      <!-- ログタブ -->
      <div id="tab-log" class="tab-panel">
        <div class="alerts-bar">
          <div class="alerts-title">システムログ</div>
        </div>
        <div class="log-list" id="log-list">
          <div class="empty">ログはまだありません</div>
        </div>
      </div>

    </div>
  </main>
</div>

<!-- トースト通知 -->
<div id="toast">
  <div class="toast-title" id="toast-title"></div>
  <div class="toast-body"  id="toast-body"></div>
</div>

<script>
// ── 状態 ──────────────────────────────────────────────────
let selRace = null;
let knownAlerts = 0;
let toastTimer  = null;
let races       = [];
let selectedVenue = null;

// ── 初期化 ───────────────────────────────────────────────
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => navigator.serviceWorker.register('/service-worker.js').catch(() => {}));
}

window.onload = () => {
  loadRaces();
  setInterval(loadRaces,  30000);   // 30秒ごとに自動更新
  setInterval(pollAlerts, 5000);    // 5秒ごとにアラート確認
  setInterval(pollLog,    8000);    // 8秒ごとにログ確認
  setInterval(refreshSelectedOdds, 60000); // 選択中レースは60秒ごとに最新オッズへ更新
};

// ── レース一覧 ───────────────────────────────────────────
async function loadRaces() {
  const el = document.getElementById('race-list');
  try {
    const res = await fetch('/api/races', { cache: 'no-store' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    races = await res.json();
    renderRaces(races);
    updateHeaderStats(races);
  } catch(e) {
    console.error(e);
    if (el) el.innerHTML = '<div class="empty">レース一覧の読み込みに失敗しました<br><small>再読み込みしてください</small></div>';
  }
}

async function forceRefreshRaces() {
  try {
    await fetch('/api/races/refresh');
    await loadRaces();
  } catch(e) {}
}

function renderRaces(list) {
  const el = document.getElementById('race-list');
  const tabs = document.getElementById('venue-tabs');

  document.getElementById('race-count').textContent = list.length + '件';

  if (!list.length) {
    tabs.innerHTML = '';
    el.innerHTML = '<div class="empty">本日のレースが見つかりません<br><small>非開催日の可能性があります</small></div>';
    return;
  }

  const venues = [...new Set(list.map(r => r.venue || 'その他'))];

  if (!selectedVenue || !venues.includes(selectedVenue)) {
    selectedVenue = venues[0];
  }

  tabs.innerHTML = venues.map(v => {
    const active = v === selectedVenue ? 'active' : '';
    return `<button class="venue-tab ${active}" data-venue="${v}">${v}</button>`;
  }).join('');

  const filtered = list.filter(r => (r.venue || 'その他') === selectedVenue);

  el.innerHTML = filtered.map(r => {
    const sc  = r.status === 'monitoring' ? 's-monitoring'
             : r.status === 'finished'  ? 's-finished' : '';
    const bc  = r.status === 'monitoring' ? 'badge-mon'
             : r.status === 'finished'  ? 'badge-done' : 'badge-wait';
    const bl  = r.status === 'monitoring' ? '監視中'
             : r.status === 'finished'  ? '終了'       : '待機';
    const stc = r.status === 'monitoring' ? 'ri-status-mon'
             : r.status === 'finished'  ? 'ri-status-done' : 'ri-status-wait';
    const sts = r.status === 'monitoring' ? `監視中 <span class="pulse">●</span>`
             : r.status === 'finished'  ? '発走済み'
             : r.mt > 0 ? `あと${Math.round(r.mt)}分`
             : r.mt > -5 ? '発走直後' : '発走済み';
    const isActive = selRace && selRace.race_id === r.race_id ? 'active' : '';

    return `<div class="race-item ${sc} ${isActive}" id="ri-${r.race_id}" data-race-id="${r.race_id}">
      <div class="ri-top">
        <span class="ri-badge ${bc}">${bl}</span>
        <span class="ri-name">${r.r_num}R ${r.name}</span>
      </div>
      <div class="ri-bottom">
        <span class="ri-time">${r.start_time}</span>
        <span class="${stc}">${sts}</span>
      </div>
    </div>`;
  }).join('');

  tabs.querySelectorAll('.venue-tab').forEach(btn => {
    btn.addEventListener('click', () => selectVenue(btn.dataset.venue));
  });
  el.querySelectorAll('.race-item').forEach(item => {
    item.addEventListener('click', () => pickRaceById(item.dataset.raceId));
  });
}

function selectVenue(venue) {
  selectedVenue = venue;
  renderRaces(races);
}

function updateHeaderStats(list) {
  const mon  = list.filter(r => r.status === 'monitoring').length;
  const wait = list.filter(r => r.status === 'waiting').length;
  const done = list.filter(r => r.status === 'finished').length;
  const ac   = parseInt(document.getElementById('alert-badge').textContent.replace(/[()]/g,'')) || 0;
  document.getElementById('header-stats').innerHTML = `
    <span class="stat-chip chip-mon">監視中 ${mon}</span>
    <span class="stat-chip chip-wait">待機 ${wait}</span>
    <span class="stat-chip chip-done">終了 ${done}</span>
    ${ac ? `<span class="stat-chip chip-alert">🚨 急変 ${ac}件</span>` : ''}
  `;
}

// ── レース選択 ───────────────────────────────────────────
async function pickRaceById(raceId) {
  const race = races.find(r => r.race_id === raceId);
  if (!race) return;
  await pickRace(race);
}

async function pickRace(race) {
  selRace = race;
  document.querySelectorAll('.race-item').forEach(e => e.classList.remove('active'));
  document.getElementById('ri-' + race.race_id)?.classList.add('active');
  switchTab('odds');
  await loadOdds(race.race_id, race.full_name, true);
}

async function loadOdds(raceId, raceName, force) {
  const ph = document.getElementById('odds-ph');
  const mc = document.getElementById('odds-main');
  ph.textContent = '取得中...'; ph.style.display = 'block'; mc.style.display = 'none';

  const endpoint = force ? `/api/odds/${raceId}/force` : `/api/odds/${raceId}`;
  let data;
  try {
    data = await (await fetch(endpoint)).json();
  } catch(e) {
    ph.textContent = 'オッズの取得に失敗しました'; return;
  }

  const horses = data.horses;
  if (!horses || !horses.length) {
    ph.textContent = 'オッズを取得できませんでした（発売前または終了後の可能性があります）';
    return;
  }

  ph.style.display = 'none'; mc.style.display = 'block';
  document.getElementById('odds-title').textContent = raceName;
  const cacheTag = data.cached ? ' (キャッシュ)' : '';
  document.getElementById('odds-updated').textContent = `更新: ${data.updated}${cacheTag}`;

  const sorted = [...horses].sort((a, b) => a.odds - b.odds);
  document.getElementById('odds-tbody').innerHTML = horses.map(h => {
    const rank = sorted.findIndex(s => s.number === h.number) + 1;
    const cls  = h.odds <= 3 ? 'low' : h.odds >= 30 ? 'high' : '';
    return `<tr>
      <td><span class="hn">${h.number}</span></td>
      <td>${h.name}</td>
      <td><span class="odds-num ${cls}">${h.odds.toFixed(1)}倍</span></td>
      <td><span class="pop-tag">${rank}番人気</span></td>
    </tr>`;
  }).join('');
}

async function reloadOdds() {
  if (selRace) await loadOdds(selRace.race_id, selRace.full_name, true);
}

async function refreshSelectedOdds() {
  if (!selRace) return;
  if (!document.getElementById('tab-odds').classList.contains('active')) return;
  await loadOdds(selRace.race_id, selRace.full_name, true);
}

// ── アラート ─────────────────────────────────────────────
async function pollAlerts() {
  try {
    const alerts = await (await fetch('/api/alerts')).json();
    if (alerts.length > knownAlerts && knownAlerts > 0) {
      const a = alerts[0];
      showToast(
        `🚨 ${a.race}`,
        `${a.horse_num}番 ${a.horse_name}  ${a.old_odds}→${a.new_odds}倍  ${a.direction} ${Math.abs(a.change_pct)}%`
      );
    }
    knownAlerts = alerts.length;
    const badge = document.getElementById('alert-badge');
    badge.textContent = alerts.length ? `(${alerts.length})` : '';

    const el = document.getElementById('alert-cards');
    if (!alerts.length) {
      el.innerHTML = '<div class="empty">急変はまだ検知されていません</div>';
      return;
    }
    el.innerHTML = alerts.map(a => {
      const d = a.change_pct < 0 ? 'down' : 'up';
      return `<div class="alert-card ${d}">
        <div class="ac-time">${a.time}</div>
        <div class="ac-body">
          <div class="ac-race">${a.race}</div>
          <div class="ac-horse">${a.horse_num}番 ${a.horse_name}</div>
        </div>
        <div class="ac-change ${d}">
          ${a.old_odds}倍→${a.new_odds}倍<br>
          <span class="ac-pct">${a.direction} ${Math.abs(a.change_pct)}%</span>
        </div>
      </div>`;
    }).join('');
  } catch(e) {}
}

function clearAlerts() {
  knownAlerts = 0;
  document.getElementById('alert-cards').innerHTML = '<div class="empty">急変はまだ検知されていません</div>';
  document.getElementById('alert-badge').textContent = '';
}

// ── ログ ─────────────────────────────────────────────────
async function pollLog() {
  try {
    const logs = await (await fetch('/api/log')).json();
    const el = document.getElementById('log-list');
    if (!logs.length) { el.innerHTML = '<div class="empty">ログはまだありません</div>'; return; }
    el.innerHTML = logs.map(l => {
      const cls = l.includes('🚨') ? 'alert' : l.includes('▶') ? 'monitor' : '';
      return `<div class="log-entry ${cls}">${l}</div>`;
    }).join('');
  } catch(e) {}
}

// ── タブ切替 ─────────────────────────────────────────────
function switchTab(name) {
  document.querySelectorAll('.tab').forEach((t, i) =>
    t.classList.toggle('active', ['odds','alerts','log'][i] === name));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  if (name === 'alerts') pollAlerts();
  if (name === 'log')    pollLog();
}

// ── トースト ─────────────────────────────────────────────
function showToast(title, body) {
  document.getElementById('toast-title').textContent = title;
  document.getElementById('toast-body').textContent  = body;
  document.getElementById('toast').style.display = 'block';
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => document.getElementById('toast').style.display = 'none', 7000);
}
</script>
</body>
</html>
"""

@app.route("/manifest.json")
def manifest():
    return jsonify({
        "name": "JRA オッズ急変モニター",
        "short_name": "JRAオッズ",
        "description": "中央競馬のオッズ急変を監視するアプリ",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#08090d",
        "theme_color": "#08090d",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"}
        ]
    })

@app.route("/service-worker.js")
def service_worker():
    js = """
const CACHE_NAME = 'jra-odds-monitor-v2';
const CORE_ASSETS = ['/manifest.json', '/static/icon-192.png', '/static/icon-512.png'];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(CORE_ASSETS)));
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys => Promise.all(keys.map(key => caches.delete(key))))
  );
  self.clients.claim();
});

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.mode === 'navigate' || url.pathname === '/' || url.pathname.startsWith('/api/')) return;
  event.respondWith(fetch(event.request).catch(() => caches.match(event.request)));
});
""".strip()
    return Response(js, mimetype="application/javascript")

@app.route("/")
def index():
    html = HTML.replace("THRESHOLD_VAL", str(ODDS_CHANGE_THRESHOLD))
    return Response(html, mimetype="text/html", headers={"Cache-Control": "no-store"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    is_local = "PORT" not in os.environ
    url = f"http://localhost:{port}"

    print("=" * 52)
    print("  🏇  JRA オッズ急変モニター 起動中...")
    print(f"  ブラウザで開く →  {url}")
    print("  停止: Ctrl+C")
    print("=" * 52)

    if is_local:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    app.run(debug=False, host="0.0.0.0", port=port, threaded=True)





