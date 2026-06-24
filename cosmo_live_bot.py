#!/usr/bin/env python3
"""
Cosmo Live -> Discord 直播通知 bot
偵測 tripleS 成員開直播，並推播到 Discord 並 tag 指定身分組。

原理：輪詢 Cosmo 的通知中心 API（FCM 壞掉也照樣有資料），
篩出 url 含 "live-viewer" 的通知，用 liveSessionId 去重，新的就推 Discord。

部署：填好下方環境變數後，丟到常駐機器（家用機/Pi/Oracle Free/VPS）用 systemd 跑。
"""

import os
import re
import time
import json
import base64
import sqlite3
import logging
import requests
from datetime import datetime, timezone, timedelta

TW_TZ = timezone(timedelta(hours=8))  # 台灣 GMT+8
KST   = timezone(timedelta(hours=9))  # 韓國 GMT+9

# ───────────────────────── 設定（用環境變數，別把 token 寫死進檔案）──────────────────
COSMO_TOKEN     = os.environ["COSMO_TOKEN"]            # 你的 Bearer token（不含 "Bearer "）
DISCORD_WEBHOOK = os.environ["DISCORD_WEBHOOK"]         # Discord 頻道 webhook URL
ROLE_ID         = os.environ.get("ROLE_ID", "")        # 要 tag 的身分組 ID（留空則不 tag）
ADMIN_WEBHOOK   = os.environ.get("ADMIN_WEBHOOK", DISCORD_WEBHOOK)  # token 出問題時通知你自己的頻道

API_URL      = "https://api.cosmo.fans/bff/v3/notification-center"
NOTICES_API  = "https://api.cosmo.fans/bff/v3/notices"
ARTIST_ID    = "tripleS"
POLL_SEC   = 45                                        # 輪詢間隔（秒）
DB_PATH    = os.environ.get("DB_PATH", "cosmo_live.db")
DEVICE_ID  = os.environ.get("DEVICE_ID", "PJH110")
APP_VER    = os.environ.get("APP_VER", "2.39.0")

HEADERS = {
    "authorization": f"Bearer {COSMO_TOKEN}",
    "appversion": APP_VER,
    "deviceid": DEVICE_ID,
    "user-agent": "okhttp/4.12.0",
    "accept": "application/json, text/plain, */*",
    "accept-language": "zh-cn",
}
PARAMS = {"take": "10", "skip": "0", "artistId": ARTIST_ID}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("cosmo")


# ───────────────────────── 去重持久化（sqlite，重啟不會重發）──────────────────────────
def db_init():
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS seen (live_id TEXT PRIMARY KEY, ts TEXT)")
    con.execute("""CREATE TABLE IF NOT EXISTS card_notices (
        notice_id TEXT PRIMARY KEY,
        claim_start_utc TEXT,
        announced INTEGER DEFAULT 0,
        reminded INTEGER DEFAULT 0,
        title TEXT
    )""")
    con.commit()
    return con

def already_sent(con, live_id):
    return con.execute("SELECT 1 FROM seen WHERE live_id=?", (live_id,)).fetchone() is not None

def mark_sent(con, live_id, ts):
    con.execute("INSERT OR IGNORE INTO seen(live_id, ts) VALUES(?,?)", (live_id, ts))
    con.commit()


# ───────────────────────── token 壽命檢查（你的 token 7 天到期）─────────────────────────
def token_expiry_warn():
    """解析 JWT 的 exp，快過期時記 log + 通知管理頻道。抓不到 refresh endpoint 前先靠這個提醒手動換。"""
    try:
        payload_b64 = COSMO_TOKEN.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)           # 補 padding
        exp = json.loads(base64.urlsafe_b64decode(payload_b64))["exp"]
        left = exp - time.time()
        if left < 0:
            notify_admin("⚠️ Cosmo token 已過期，bot 收不到資料，請更新 COSMO_TOKEN。")
            log.error("token expired")
        elif left < 86400:                                      # 剩不到 1 天
            log.warning("token 剩 %.1f 小時到期", left / 3600)
            notify_admin(f"⚠️ Cosmo token 剩約 {left/3600:.1f} 小時到期，記得更新。")
    except Exception as e:
        log.warning("無法解析 token exp: %s", e)


# ───────────────────────── 時間轉換（UTC -> 台灣 GMT+8）─────────────────────────────
def to_tw_time(sent_at):
    """把 API 的 sentAt（UTC, 如 2026-06-22T14:42:18.442Z）轉成台灣時間字串。"""
    try:
        dt = datetime.fromisoformat(sent_at.replace("Z", "+00:00")).astimezone(TW_TZ)
        return dt.strftime("%Y/%m/%d %H:%M")
    except Exception:
        return sent_at  # 解析失敗就原樣顯示


def is_stale(sent_at, max_age=3600):
    """sentAt 超過 max_age 秒（預設 24 小時）就算過期，不推播。"""
    try:
        dt = datetime.fromisoformat(sent_at.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds() > max_age
    except Exception:
        return False


# ───────────────────────── 領卡公告偵測 ──────────────────────────────────────────────
def parse_claim_time(content):
    """從公告內文解析領卡開始時間，回傳 UTC datetime 或 None。
    支援 4 位數年份 (2026.06.24) 和 2 位數 (26.06.24)。"""
    m = re.search(
        r'⏰\s*(?:Schedule|일정)\s*:\s*(\d{2,4})\.(\d{2})\.(\d{2})\s+(\d{2}):(\d{2})\s*KST',
        content,
    )
    if not m:
        return None
    y, mo, d, h, mi = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
    if y < 100:
        y += 2000
    return datetime(y, mo, d, h, mi, tzinfo=KST).astimezone(timezone.utc)


def fetch_notice_detail(notice_id):
    """打 /bff/v3/notices/{id} 取得公告內文。"""
    r = requests.get(f"{NOTICES_API}/{notice_id}", headers=HEADERS, timeout=15)
    if r.status_code == 200:
        return r.json().get("result", {})
    return None


def check_card_notices(con, notifications):
    """掃描通知列表，偵測新的領卡公告 → 立刻發 Discord 預告。"""
    for n in notifications:
        url = n.get("url", "")
        m = re.search(r'notice\?id=(\d+)', url)
        if not m:
            continue
        notice_id = m.group(1)

        if con.execute("SELECT 1 FROM card_notices WHERE notice_id=?", (notice_id,)).fetchone():
            continue

        if is_stale(n.get("sentAt", "")):
            continue

        detail = fetch_notice_detail(notice_id)
        if not detail:
            continue

        claim_start = parse_claim_time(detail.get("content", ""))
        if not claim_start:
            continue

        title = detail.get("title", n.get("content", ""))
        con.execute(
            "INSERT OR IGNORE INTO card_notices(notice_id, claim_start_utc, announced, reminded, title) VALUES(?,?,0,0,?)",
            (notice_id, claim_start.isoformat(), title),
        )
        con.commit()

        claim_tw_str = claim_start.astimezone(TW_TZ).strftime("%Y/%m/%d %H:%M")
        notify_card_announce(title, claim_tw_str)
        con.execute("UPDATE card_notices SET announced=1 WHERE notice_id=?", (notice_id,))
        con.commit()
        log.info("領卡公告 notice=%s: %s（領取 %s 台灣時間）", notice_id, title, claim_tw_str)

        now_utc = datetime.now(timezone.utc)
        if (claim_start - now_utc).total_seconds() <= 0:
            con.execute("UPDATE card_notices SET reminded=1 WHERE notice_id=?", (notice_id,))
            con.commit()
            log.info("領卡時間已過，跳過提醒 notice=%s", notice_id)


def check_card_reminders(con):
    """每輪檢查：有沒有領卡時間快到（≤3 分鐘內）要發提醒的。"""
    now = datetime.now(timezone.utc)
    rows = con.execute(
        "SELECT notice_id, claim_start_utc, title FROM card_notices WHERE reminded=0"
    ).fetchall()

    for notice_id, claim_start_str, title in rows:
        claim_start = datetime.fromisoformat(claim_start_str)
        secs_left = (claim_start - now).total_seconds()

        if secs_left > 180:
            continue

        claim_tw_str = claim_start.astimezone(TW_TZ).strftime("%Y/%m/%d %H:%M")
        mins_left = max(int(secs_left / 60), 0)
        notify_card_reminder(title, claim_tw_str, mins_left)

        con.execute("UPDATE card_notices SET reminded=1 WHERE notice_id=?", (notice_id,))
        con.commit()
        log.info("領卡提醒已發送 notice=%s（剩 %d 分鐘）", notice_id, mins_left)


# ───────────────────────── Discord 推播 ──────────────────────────────────────────────
def notify_live(content, live_id, url, sent_at):
    mention = f"<@&{ROLE_ID}> " if ROLE_ID else ""
    tw_time = to_tw_time(sent_at)
    body = {
        "content": (
            f"{mention}🔴 **有直播！**\n"
            f"{content}\n"
            f"🕐 開始時間：{tw_time}（台灣時間）\n"
            f"👉 快打開 Cosmo App 觀看！"
        ),
        "allowed_mentions": {"roles": [ROLE_ID] if ROLE_ID else []},
    }
    r = requests.post(DISCORD_WEBHOOK, json=body, timeout=10)
    r.raise_for_status()
    log.info("已推播直播 session=%s : %s（%s）", live_id, content, tw_time)

def notify_card_announce(title, claim_tw_str):
    mention = f"<@&{ROLE_ID}> " if ROLE_ID else ""
    body = {
        "content": (
            f"{mention}🃏 **領卡公告！**\n"
            f"{title}\n"
            f"可領取時間：{claim_tw_str}（台灣時間）\n"
            f"届時會再提醒！"
        ),
        "allowed_mentions": {"roles": [ROLE_ID] if ROLE_ID else []},
    }
    r = requests.post(DISCORD_WEBHOOK, json=body, timeout=10)
    r.raise_for_status()


def notify_card_reminder(title, claim_tw_str, mins_left):
    mention = f"<@&{ROLE_ID}> " if ROLE_ID else ""
    countdown = f"再 {mins_left} 分鐘就可以領卡了！" if mins_left > 0 else "現在可以領卡了！"
    body = {
        "content": (
            f"{mention}⏰ **領卡提醒！**\n"
            f"{title}\n"
            f"{countdown}\n"
            f"領取時間：{claim_tw_str}（台灣時間）\n"
            f"👉 快打開 Cosmo App 領取！"
        ),
        "allowed_mentions": {"roles": [ROLE_ID] if ROLE_ID else []},
    }
    r = requests.post(DISCORD_WEBHOOK, json=body, timeout=10)
    r.raise_for_status()


def notify_admin(msg):
    try:
        requests.post(ADMIN_WEBHOOK, json={"content": msg, "allowed_mentions": {"parse": []}}, timeout=10)
    except Exception as e:
        log.error("admin 通知失敗: %s", e)


# ───────────────────────── 主輪詢 ────────────────────────────────────────────────────
def poll(con):
    r = requests.get(API_URL, headers=HEADERS, params=PARAMS, timeout=15)
    if r.status_code == 401:
        raise PermissionError("401 Unauthorized — token 失效")
    r.raise_for_status()

    notifications = r.json().get("notifications", [])

    for n in notifications:
        url = n.get("url", "")
        if "live-viewer" not in url:
            continue
        live_id = url.split("liveSessionId=")[-1]
        if already_sent(con, live_id):
            continue
        sent_at = n.get("sentAt", "")
        if is_stale(sent_at):
            mark_sent(con, live_id, sent_at)
            continue
        notify_live(n.get("content", "直播中"), live_id, url, sent_at)
        mark_sent(con, live_id, sent_at)

    check_card_notices(con, notifications)


def main():
    con = db_init()
    log.info("Cosmo live bot 啟動，輪詢間隔 %ss", POLL_SEC)
    token_expiry_warn()
    last_token_check = time.time()
    fail_streak = 0

    while True:
        try:
            poll(con)
            check_card_reminders(con)
            fail_streak = 0
        except PermissionError as e:
            log.error("%s", e)
            notify_admin("⚠️ Cosmo token 失效（401），請更新 COSMO_TOKEN 後重啟 bot。")
            time.sleep(300)                                     # token 死了，慢慢等人來修
        except Exception as e:
            fail_streak += 1
            wait = min(POLL_SEC * fail_streak, 600)             # 指數退避，最多等 10 分鐘
            log.warning("輪詢失敗（連續 %d 次）：%s，%ss 後重試", fail_streak, e, wait)
            time.sleep(wait)
            continue

        # 每 6 小時檢查一次 token 壽命
        if time.time() - last_token_check > 6 * 3600:
            token_expiry_warn()
            last_token_check = time.time()

        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
