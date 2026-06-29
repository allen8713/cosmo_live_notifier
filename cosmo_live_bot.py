#!/usr/bin/env python3
"""
Cosmo Live -> Discord 直播通知 bot
偵測 tripleS 成員開直播，並推播到 Discord 並 tag 指定身分組。

原理：輪詢 Cosmo 的通知中心 API（FCM 壞掉也照樣有資料），
篩出 url 含 "live-viewer" + "liveSessionId" 的通知，去重後推 Discord。
另含「領卡公告」三段通知（公告 / 領卡前 3 分鐘 / 領卡開始當下）。

部署：填好下方環境變數後，丟到常駐機器（Railway / Pi / VPS）跑。
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
DISCORD_WEBHOOK = os.environ["DISCORD_WEBHOOK"]         # 直播/領卡推播頻道
ROLE_ID         = os.environ.get("ROLE_ID", "")        # 要 tag 的身分組 ID（留空則不 tag）
ADMIN_WEBHOOK   = os.environ.get("ADMIN_WEBHOOK", DISCORD_WEBHOOK)  # token 出問題的提醒頻道
# 心跳 / 延遲監控專用 webhook（除錯用，不 tag 任何人）
MONITOR_WEBHOOK = os.environ.get("MONITOR_WEBHOOK", "")

API_URL      = "https://api.cosmo.fans/bff/v3/notification-center"
NOTICES_API  = "https://api.cosmo.fans/bff/v3/notices"
ARTIST_ID    = "tripleS"
POLL_SEC   = int(os.environ.get("POLL_SEC", "45"))     # 輪詢間隔（秒）
DB_PATH    = os.environ.get("DB_PATH", "cosmo_live.db")
DEVICE_ID  = os.environ.get("DEVICE_ID", "PJH110")
APP_VER    = os.environ.get("APP_VER", "2.39.0")
# 心跳：每隔多久發一次「我還活著 + 最新通知延遲」到監控頻道（秒）
HEARTBEAT_SEC = int(os.environ.get("HEARTBEAT_SEC", "1800"))   # 預設 30 分鐘

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
    # 領卡公告：三段通知各自獨立旗標
    #   announced       = 已發「公告」（Cosmo 一發公告就推）
    #   reminded_3min   = 已發「領卡前 3 分鐘提醒」
    #   reminded_start  = 已發「領卡開始當下提醒」
    con.execute("""CREATE TABLE IF NOT EXISTS card_notices (
        notice_id TEXT PRIMARY KEY,
        claim_start_utc TEXT,
        announced INTEGER DEFAULT 0,
        reminded_3min INTEGER DEFAULT 0,
        reminded_start INTEGER DEFAULT 0,
        title TEXT
    )""")
    con.commit()
    _migrate_card_table(con)
    return con


def _migrate_card_table(con):
    """舊版 schema（單一 reminded 欄位）自動補上新欄位，避免既有 db 壞掉。"""
    cols = {row[1] for row in con.execute("PRAGMA table_info(card_notices)").fetchall()}
    if "reminded_3min" not in cols:
        con.execute("ALTER TABLE card_notices ADD COLUMN reminded_3min INTEGER DEFAULT 0")
    if "reminded_start" not in cols:
        con.execute("ALTER TABLE card_notices ADD COLUMN reminded_start INTEGER DEFAULT 0")
    if "reminded" in cols:
        try:
            con.execute("UPDATE card_notices SET reminded_3min=reminded WHERE reminded_3min=0")
        except Exception:
            pass
    con.commit()


def already_sent(con, live_id):
    return con.execute("SELECT 1 FROM seen WHERE live_id=?", (live_id,)).fetchone() is not None

def mark_sent(con, live_id, ts):
    con.execute("INSERT OR IGNORE INTO seen(live_id, ts) VALUES(?,?)", (live_id, ts))
    con.commit()


# ───────────────────────── token 壽命檢查（你的 token 7 天到期）─────────────────────────
def token_expiry_warn():
    try:
        payload_b64 = COSMO_TOKEN.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload_b64))["exp"]
        left = exp - time.time()
        if left < 0:
            notify_admin("⚠️ Cosmo token 已過期，bot 收不到資料，請更新 COSMO_TOKEN。")
            log.error("token expired")
        elif left < 86400:
            log.warning("token 剩 %.1f 小時到期", left / 3600)
            notify_admin(f"⚠️ Cosmo token 剩約 {left/3600:.1f} 小時到期，記得更新。")
    except Exception as e:
        log.warning("無法解析 token exp: %s", e)


# ───────────────────────── 時間工具 ─────────────────────────────────────────────────
def to_tw_time(sent_at):
    try:
        dt = datetime.fromisoformat(sent_at.replace("Z", "+00:00")).astimezone(TW_TZ)
        return dt.strftime("%Y/%m/%d %H:%M")
    except Exception:
        return sent_at


def lag_seconds(sent_at):
    """回傳「現在距離 sentAt 過了幾秒」；無法解析回 None。"""
    try:
        dt = datetime.fromisoformat(sent_at.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:
        return None


def is_stale(sent_at, max_age=3600):
    s = lag_seconds(sent_at)
    return s is not None and s > max_age


# ───────────────────────── 領卡公告偵測 ──────────────────────────────────────────────
def parse_claim_time(content):
    """從公告內文解析領卡開始時間，回傳 UTC datetime 或 None。"""
    m = re.search(
        r'⏰\s*(?:Schedule|일정)\s*:\s*(\d{2,4})\.(\d{2})\.(\d{2})\s+(\d{2}):(\d{2})\s*KST',
        content,
    )
    if not m:
        return None
    y, mo, d, h, mi = (int(m.group(i)) for i in range(1, 6))
    if y < 100:
        y += 2000
    return datetime(y, mo, d, h, mi, tzinfo=KST).astimezone(timezone.utc)


def fetch_notice_detail(notice_id):
    r = requests.get(f"{NOTICES_API}/{notice_id}", headers=HEADERS, timeout=15)
    if r.status_code == 200:
        return r.json().get("result", {})
    return None


def check_card_notices(con, notifications):
    """掃描通知列表，偵測新領卡公告 → 發第 1 段「公告」通知。"""
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
        secs_left = (claim_start - datetime.now(timezone.utc)).total_seconds()

        # 寫 db；若領卡時間已過/太近，先把對應旗標補上避免後續亂發
        pre_3min  = 1 if secs_left <= 180 else 0
        pre_start = 1 if secs_left <= 0   else 0
        con.execute(
            """INSERT OR IGNORE INTO card_notices
               (notice_id, claim_start_utc, announced, reminded_3min, reminded_start, title)
               VALUES(?,?,1,?,?,?)""",
            (notice_id, claim_start.isoformat(), pre_3min, pre_start, title),
        )
        con.commit()

        claim_tw_str = claim_start.astimezone(TW_TZ).strftime("%Y/%m/%d %H:%M")
        notify_card_announce(title, claim_tw_str)
        log.info("領卡公告 notice=%s: %s（領取 %s 台灣時間，距今 %.0f 分）",
                 notice_id, title, claim_tw_str, secs_left / 60)


def check_card_reminders(con):
    """每輪檢查倒數，發第 2 段（前 3 分鐘）與第 3 段（開始當下）通知，各自獨立旗標。"""
    now = datetime.now(timezone.utc)
    rows = con.execute(
        """SELECT notice_id, claim_start_utc, title, reminded_3min, reminded_start
           FROM card_notices WHERE reminded_3min=0 OR reminded_start=0"""
    ).fetchall()

    for notice_id, claim_start_str, title, r3, rs in rows:
        claim_start = datetime.fromisoformat(claim_start_str)
        secs_left = (claim_start - now).total_seconds()
        claim_tw_str = claim_start.astimezone(TW_TZ).strftime("%Y/%m/%d %H:%M")

        # 第 2 段：領卡前 3 分鐘（進入 180 秒內、尚未到 0）
        if not r3 and 0 < secs_left <= 180:
            mins_left = max(int(round(secs_left / 60)), 1)
            notify_card_reminder(title, claim_tw_str, mins_left)
            con.execute("UPDATE card_notices SET reminded_3min=1 WHERE notice_id=?", (notice_id,))
            con.commit()
            log.info("領卡 3 分鐘提醒已發 notice=%s（剩 %.0f 秒）", notice_id, secs_left)

        # 第 3 段：領卡開始當下（時間已到）
        if not rs and secs_left <= 0:
            if secs_left > -300:        # 開始後 5 分鐘內才補發，避免停機後亂發舊的
                notify_card_start(title, claim_tw_str)
                log.info("領卡開始通知已發 notice=%s", notice_id)
            else:
                log.info("領卡開始已過太久，跳過 notice=%s（過了 %.0f 秒）", notice_id, -secs_left)
            con.execute(
                "UPDATE card_notices SET reminded_start=1, reminded_3min=1 WHERE notice_id=?",
                (notice_id,),
            )
            con.commit()


# ───────────────────────── Discord 推播 ──────────────────────────────────────────────
def _post(webhook, content, tag_role=True):
    if tag_role and ROLE_ID:
        mentions = {"roles": [ROLE_ID]}
    else:
        mentions = {"parse": []}
    r = requests.post(webhook, json={"content": content, "allowed_mentions": mentions}, timeout=10)
    r.raise_for_status()


def notify_live(content, live_id, url, sent_at):
    mention = f"<@&{ROLE_ID}> " if ROLE_ID else ""
    tw_time = to_tw_time(sent_at)
    _post(DISCORD_WEBHOOK,
          f"{mention}🔴 **有直播！**\n{content}\n🕐 開始時間：{tw_time}（台灣時間）\n👉 快打開 Cosmo App 觀看！")
    log.info("已推播直播 session=%s : %s（%s）", live_id, content, tw_time)


def notify_card_announce(title, claim_tw_str):
    mention = f"<@&{ROLE_ID}> " if ROLE_ID else ""
    _post(DISCORD_WEBHOOK,
          f"{mention}🃏 **領卡公告！**\n{title}\n可領取時間：{claim_tw_str}（台灣時間）\n屆時會再提醒！")


def notify_card_reminder(title, claim_tw_str, mins_left):
    mention = f"<@&{ROLE_ID}> " if ROLE_ID else ""
    _post(DISCORD_WEBHOOK,
          f"{mention}⏰ **領卡提醒！**\n{title}\n再 {mins_left} 分鐘就可以領卡了！\n"
          f"領取時間：{claim_tw_str}（台灣時間）\n👉 準備打開 Cosmo App！")


def notify_card_start(title, claim_tw_str):
    mention = f"<@&{ROLE_ID}> " if ROLE_ID else ""
    _post(DISCORD_WEBHOOK,
          f"{mention}🎉 **領卡開始！**\n{title}\n現在可以領卡了！\n👉 快打開 Cosmo App 領取！")


def notify_admin(msg):
    try:
        _post(ADMIN_WEBHOOK, msg, tag_role=False)
    except Exception as e:
        log.error("admin 通知失敗: %s", e)


def notify_monitor(msg):
    """心跳 / 延遲監控訊息，發到監控頻道，不 tag 任何人。"""
    if not MONITOR_WEBHOOK:
        return
    try:
        _post(MONITOR_WEBHOOK, msg, tag_role=False)
    except Exception as e:
        log.error("monitor 通知失敗: %s", e)


# ───────────────────────── 主輪詢 ────────────────────────────────────────────────────
def poll(con):
    """回傳 (最新通知 sentAt, 最新通知 content)，供心跳監控用。"""
    r = requests.get(API_URL, headers=HEADERS, params=PARAMS, timeout=15)
    if r.status_code == 401:
        raise PermissionError("401 Unauthorized — token 失效")
    r.raise_for_status()

    notifications = r.json().get("notifications", [])

    # ── 方法一：每輪都記錄「最新通知 sentAt + 延遲秒數」到 log ──
    newest_sent, newest_content = "", ""
    if notifications:
        newest = notifications[0]
        newest_sent = newest.get("sentAt", "")
        newest_content = newest.get("content", "")[:24]
        lag = lag_seconds(newest_sent)
        log.info("輪詢 OK｜最新 sentAt=%s｜距今 %s 秒｜%s",
                 to_tw_time(newest_sent),
                 f"{lag:.0f}" if lag is not None else "?",
                 newest_content)

    for n in notifications:
        url = n.get("url", "")
        if "live-viewer" not in url or "liveSessionId" not in url:   # 收嚴：排除 video-viewer 重播
            continue
        live_id = url.split("liveSessionId=")[-1]
        if already_sent(con, live_id):
            continue
        sent_at = n.get("sentAt", "")
        if is_stale(sent_at):
            mark_sent(con, live_id, sent_at)
            continue

        # ── 方法二：新直播當下，回報「偵測延遲」到監控頻道 ──
        det_lag = lag_seconds(sent_at)
        det_now = datetime.now(TW_TZ).strftime("%H:%M:%S")
        notify_live(n.get("content", "直播中"), live_id, url, sent_at)
        mark_sent(con, live_id, sent_at)
        if det_lag is not None:
            notify_monitor(
                f"🔍 偵測到新直播 `{n.get('content','')[:30]}`\n"
                f"・sentAt（Cosmo 標記）：{to_tw_time(sent_at)}\n"
                f"・bot 偵測當下：{det_now}（台灣）\n"
                f"・落後 sentAt：**{det_lag:.0f} 秒**"
            )
        else:
            notify_monitor(f"🔍 偵測到新直播 `{n.get('content','')[:30]}`（無法計算延遲）")

    check_card_notices(con, notifications)
    return newest_sent, newest_content


def main():
    con = db_init()
    log.info("Cosmo live bot 啟動，輪詢間隔 %ss", POLL_SEC)
    notify_monitor(f"✅ bot 已啟動（輪詢 {POLL_SEC}s／心跳 {HEARTBEAT_SEC}s）")
    token_expiry_warn()
    last_token_check = time.time()
    last_heartbeat = 0.0
    fail_streak = 0

    while True:
        try:
            newest_sent, newest_content = poll(con)
            check_card_reminders(con)
            fail_streak = 0

            # ── 方法一：定期心跳，回報「最新通知延遲」到監控頻道 ──
            if MONITOR_WEBHOOK and time.time() - last_heartbeat > HEARTBEAT_SEC:
                lag = lag_seconds(newest_sent)
                if lag is not None:
                    notify_monitor(
                        f"💓 心跳｜運作正常\n"
                        f"・最新通知：{to_tw_time(newest_sent)}（{newest_content}）\n"
                        f"・距今：{lag:.0f} 秒"
                    )
                else:
                    notify_monitor("💓 心跳｜運作正常（暫無通知資料）")
                last_heartbeat = time.time()

        except PermissionError as e:
            log.error("%s", e)
            notify_admin("⚠️ Cosmo token 失效（401），請更新 COSMO_TOKEN 後重啟 bot。")
            notify_monitor("🔴 token 失效（401），bot 暫停輪詢，請更新 COSMO_TOKEN。")
            time.sleep(300)
        except Exception as e:
            fail_streak += 1
            wait = min(POLL_SEC * fail_streak, 600)
            log.warning("輪詢失敗（連續 %d 次）：%s，%ss 後重試", fail_streak, e, wait)
            notify_monitor(f"⚠️ 輪詢失敗（連續 {fail_streak} 次）：{str(e)[:80]}，{wait}s 後重試")
            time.sleep(wait)
            continue

        if time.time() - last_token_check > 6 * 3600:
            token_expiry_warn()
            last_token_check = time.time()

        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
