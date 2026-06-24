#!/usr/bin/env python3
"""
Cosmo Live -> Discord 直播通知 bot
偵測 tripleS 成員開直播，並推播到 Discord 並 tag 指定身分組。

原理：輪詢 Cosmo 的通知中心 API（FCM 壞掉也照樣有資料），
篩出 url 含 "live-viewer" 的通知，用 liveSessionId 去重，新的就推 Discord。

部署：填好下方環境變數後，丟到常駐機器（家用機/Pi/Oracle Free/VPS）用 systemd 跑。
"""

import os
import time
import json
import base64
import sqlite3
import logging
import requests
from datetime import datetime, timezone, timedelta

TW_TZ = timezone(timedelta(hours=8))  # 台灣 GMT+8

# ───────────────── 設定──────────────────
COSMO_TOKEN     = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ0eXBlIjoiYWNjZXNzIiwiaWF0IjoxNzgyMTQ4MDAyLCJleHAiOjE3ODI3NTI4MDIsInN1YiI6IjU3NjY0MiJ9.nCva7zVzztyrzxWMg1hoCQo6GIiD2H8rL824AzE_UXQ"            # 你的 Bearer token（不含 "Bearer "）
DISCORD_WEBHOOK = "https://discord.com/api/webhooks/1518652491791597680/9E-fXV0-3GQNGlFGDykJ9U6qhI85G3rtIG-2CEEKLOqC-SXIvflcrwC7KBbYKeYnuGjO"         # Discord 頻道 webhook URL
ROLE_ID         = "1518653880999739473"     # 要 tag 的身分組 ID（留空則不 tag）
ADMIN_WEBHOOK   = "https://discord.com/api/webhooks/1518665208665608463/Wu4fSGUTbPv8FbrR85x-j0341Mku6KxL139KE6n5XdzGZ_zdz3e1xcs0R9G5u-ZADpTb"  # token 出問題時通知你自己的頻道

API_URL    = "https://api.cosmo.fans/bff/v3/notification-center"
ARTIST_ID  = "tripleS"
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


# ───────────────────────── Discord 推播 ──────────────────────────────────────────────
def notify_live(content, live_id, url, sent_at):
    mention = f"<@&{ROLE_ID}> " if ROLE_ID else ""
    tw_time = to_tw_time(sent_at)
    # url 本身是 cosmo:// 深連結，手機點了會直接開 Cosmo app 進入該直播
    body = {
        "content": (
            f"{mention}🔴 **有直播！**\n"
            f"{content}\n"
            f"開始時間：{tw_time}\n"
        ),
        "allowed_mentions": {"roles": [ROLE_ID] if ROLE_ID else []},
    }
    r = requests.post(DISCORD_WEBHOOK, json=body, timeout=10)
    r.raise_for_status()
    log.info("已推播直播 session=%s : %s（%s）", live_id, content, tw_time)

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

    for n in r.json().get("notifications", []):
        url = n.get("url", "")
        if "live-viewer" not in url:                            # 只要直播通知
            continue
        live_id = url.split("liveSessionId=")[-1]
        if already_sent(con, live_id):
            continue
        notify_live(n.get("content", "直播中"), live_id, url, n.get("sentAt", ""))
        mark_sent(con, live_id, n.get("sentAt", ""))


def main():
    con = db_init()
    log.info("Cosmo live bot 啟動，輪詢間隔 %ss", POLL_SEC)
    token_expiry_warn()
    last_token_check = time.time()
    fail_streak = 0

    while True:
        try:
            poll(con)
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
