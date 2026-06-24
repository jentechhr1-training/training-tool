"""
外訓課程自動整合工具 v4.0
================================
v4 更新內容:
  1. 介面大改版 (淡藍綠柔和配色)
  2. 協會勾選改為大型卡片
  3. 取消按鈕加大加明顯
  4. 表頭固定 (sticky)
  5. 使用時間計算器 (右上角)
  6. 信件表格邊框加粗
  7. 顯示在線人數 (取代單人限制)
  8. 預設「複訓」
  9. 移除重複的「不要外籍」選項
  10. 加入課程地點+上課時間欄位
  11. 顯示報名網址 (給後台看)
  12. 兩種信件: 同仁版 (簡潔) + 後台版 (完整)
  + 定時更新設定面板 (預設關閉)
"""

import os
import json
import re
import sqlite3
import secrets
import threading
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from functools import wraps

# 台灣時區 (UTC+8;Render 伺服器是 UTC,不轉的話時間會少 8 小時)
TW_TZ = timezone(timedelta(hours=8))
def now_tw():
    return datetime.now(TW_TZ)

# === Supabase / PostgreSQL (commit 20a) ===
try:
    import psycopg2
    import psycopg2.extras
    SUPABASE_URL = os.environ.get("DATABASE_URL", "")
    _PG_AVAILABLE = bool(SUPABASE_URL)
except ImportError:
    _PG_AVAILABLE = False
    SUPABASE_URL = ""

def _pg_conn():
    """取得 PostgreSQL 連線，失敗回傳 None"""
    if not _PG_AVAILABLE:
        return None
    try:
        conn = psycopg2.connect(SUPABASE_URL, sslmode="require", connect_timeout=10)
        return conn
    except Exception as e:
        print(f"[PG] 連線失敗: {e}")
        return None

def init_activity_log():
    """建立 activity_log 表 (如果不存在)"""
    conn = _pg_conn()
    if not conn:
        print("[PG] 無法建立 activity_log 表 (無連線)")
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS activity_log (
                id SERIAL PRIMARY KEY,
                username TEXT NOT NULL,
                action TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                duration_seconds INTEGER,
                scraper_code TEXT,
                course_ids TEXT,
                course_count INTEGER,
                email_subject TEXT,
                ip TEXT
            )
        """)
        conn.commit()
        print("[PG] activity_log 表已就緒")
    except Exception as e:
        print(f"[PG] 建立 activity_log 失敗: {e}")
    finally:
        conn.close()

def log_activity(username, action, ip="", duration_seconds=None,
                 scraper_code=None, course_ids=None, course_count=None, email_subject=None):
    """寫入一筆活動記錄 (非同步，失敗不影響主程式)"""
    def _write():
        conn = _pg_conn()
        if not conn:
            return
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO activity_log
                (username, action, timestamp, duration_seconds, scraper_code, course_ids, course_count, email_subject, ip)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                username, action,
                now_tw().strftime("%Y-%m-%d %H:%M:%S"),
                duration_seconds, scraper_code,
                json.dumps(course_ids, ensure_ascii=False) if course_ids else None,
                course_count, email_subject, ip
            ))
            conn.commit()
        except Exception as e:
            print(f"[PG] log_activity 失敗: {e}")
        finally:
            conn.close()
    threading.Thread(target=_write, daemon=True).start()

from flask import Flask, jsonify, request, render_template_string, session, redirect, url_for
import requests
from bs4 import BeautifulSoup

APP_DIR = Path(__file__).parent
DATA_FILE = APP_DIR / "course_data.json"
DB_FILE = APP_DIR / "users.db"
PORT = 5000

DEFAULT_USERS = [
    {"username": "train0", "password": "HR123", "role": "admin", "display_name": "管理員"},
    {"username": "train1", "password": "1234",  "role": "user",  "display_name": "使用者1"},
    {"username": "train2", "password": "5678",  "role": "user",  "display_name": "使用者2"},
    {"username": "train3", "password": "6789",  "role": "user",  "display_name": "使用者3"},
]

# 分會地址資料庫 (官方據點地址)
BRANCH_ADDRESSES = {
    "中壢": "桃園市中壢區中央西路二段30號10樓/14樓/15樓",
    "桃園": "桃園市桃園區建國路99號4樓",
    "新竹": "新竹市東區光復路二段101號 (詳細地址依課程而定)",
}

# 在線使用者追蹤 (key 用 "username|IP" 區分,這樣同帳號不同電腦也能正確計算)
ONLINE_USERS = {}  # {"username|ip": last_active_timestamp}
ONLINE_LOCK = threading.Lock()

# 強制登出黑名單 (username → 踢出時間戳，login_time 早於此的 session 視為無效)
FORCE_LOGOUT = {}  # {"username": timestamp}
FORCE_LOGOUT_LOCK = threading.Lock()

# 全域更新鎖 (防止多人同時更新)
IS_UPDATING = False
IS_UPDATING_BY = ""  # 記錄是誰在更新
IS_UPDATING_LOCK = threading.Lock()


def _make_key(username, ip):
    return f"{username}|{ip}"


def update_online(username, ip="?"):
    with ONLINE_LOCK:
        ONLINE_USERS[_make_key(username, ip)] = time.time()


def get_online_users():
    """取得 5 分鐘內活躍的使用者 (key 列表)"""
    with ONLINE_LOCK:
        cutoff = time.time() - 300
        active = {k: t for k, t in ONLINE_USERS.items() if t > cutoff}
        for k in list(ONLINE_USERS.keys()):
            if ONLINE_USERS[k] <= cutoff:
                del ONLINE_USERS[k]
        return list(active.keys())


def remove_online(username, ip="?"):
    with ONLINE_LOCK:
        k = _make_key(username, ip)
        if k in ONLINE_USERS:
            del ONLINE_USERS[k]


# ==========================================================================
# 資料庫
# ==========================================================================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            display_name TEXT,
            created_at TEXT
        )
    """)
    for u in DEFAULT_USERS:
        cur.execute("SELECT id FROM users WHERE username=?", (u["username"],))
        if not cur.fetchone():
            cur.execute(
                "INSERT INTO users (username, password, role, display_name, created_at) VALUES (?, ?, ?, ?, ?)",
                (u["username"], u["password"], u["role"], u["display_name"], now_tw().isoformat())
            )
    conn.commit()
    conn.close()


def verify_user(username, password):
    conn = _pg_conn()
    if conn:
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT * FROM users WHERE username=%s AND password=%s", (username, password))
            row = cur.fetchone()
            return dict(row) if row else None
        except Exception as e:
            print(f"[PG] verify_user 失敗: {e}")
        finally:
            conn.close()
    # fallback: SQLite
    conn2 = sqlite3.connect(DB_FILE)
    conn2.row_factory = sqlite3.Row
    cur2 = conn2.cursor()
    cur2.execute("SELECT * FROM users WHERE username=? AND password=?", (username, password))
    row = cur2.fetchone()
    conn2.close()
    return dict(row) if row else None


# ==========================================================================
# 課程大綱資料庫 (25 種證照)
# ==========================================================================
COURSE_TEMPLATES = {
    "粉塵作業主管": {
        "law": "依職業安全衛生法第23條及職業安全衛生教育訓練規則第11條規定 (初訓18小時)\n依職業安全衛生教育訓練規則第18條規定 (複訓每3年至少3小時)",
        "purpose": "雇主對粉塵作業主管,應使其接受有害作業主管安全教育訓練,以減少職場傷害,達到零工安事故的目標。",
        "outline_initial": ["1. 粉塵作業安全衛生相關法規 (3小時)", "2. 粉塵危害預防標準 (3小時)", "3. 粉塵危害及測定 (3小時)", "4. 粉塵作業環境改善及安全衛生防護具 (3小時)", "5. 通風換氣裝置及其維護 (3小時)", "6. 有害物作業相關法規 (3小時)"],
        "outline_retraining": ["1. 粉塵作業場所之危害評估及預防", "2. 粉塵作業相關法規及行政管理與執行", "3. 經驗及問題分享"],
    },
    "有機溶劑": {
        "law": "依職業安全衛生教育訓練規則第11條 (初訓18小時) / 第18條 (複訓每3年3小時)",
        "purpose": "雇主對有機溶劑作業主管,應使其接受有害作業主管安全教育訓練,以預防有機溶劑中毒及其相關危害。",
        "outline_initial": ["1. 有機溶劑作業相關法規 (3小時)", "2. 有機溶劑中毒預防規則 (3小時)", "3. 有機溶劑之危害及測定 (3小時)", "4. 有機溶劑作業環境改善及安全衛生防護具 (3小時)", "5. 通風換氣裝置及其維護 (3小時)", "6. 有害物作業相關法規 (3小時)"],
        "outline_retraining": ["1. 有機溶劑作業場所之危害評估及預防", "2. 有機溶劑作業相關法規及行政管理與執行", "3. 經驗及問題分享"],
    },
    "特定化學": {
        "law": "依職業安全衛生教育訓練規則第11條 (初訓18小時) / 第18條 (複訓每3年3小時)",
        "purpose": "雇主對特定化學物質作業主管,應使其接受有害作業主管安全教育訓練,以預防特定化學物質之危害。",
        "outline_initial": ["1. 特定化學物質作業相關法規 (3小時)", "2. 特定化學物質危害預防標準 (3小時)", "3. 特定化學物質之危害及測定 (3小時)", "4. 特定化學物質作業環境改善 (3小時)", "5. 通風換氣裝置 (3小時)", "6. 有害物作業相關法規 (3小時)"],
        "outline_retraining": ["1. 特定化學物質作業場所之危害評估及預防", "2. 相關法規及管理執行", "3. 經驗及問題分享"],
    },
    "缺氧": {
        "law": "依職業安全衛生教育訓練規則第11條 (初訓18小時) / 第18條 (複訓每3年3小時)",
        "purpose": "雇主對缺氧作業主管,應使其接受有害作業主管安全教育訓練,以預防缺氧災害及保護勞工安全。",
        "outline_initial": ["1. 缺氧事故處理及急救 (3小時)", "2. 缺氧危險場所之環境測定 (3小時)", "3. 缺氧危險場所危害預防及防護具 (3小時)", "4. 缺氧危險作業安全衛生管理 (3小時)", "5. 缺氧作業相關法規 (3小時)", "6. 缺氧症預防規則 (3小時)"],
        "outline_retraining": ["1. 缺氧危險作業場所之危害評估及預防", "2. 缺氧作業相關法規及管理執行", "3. 經驗及問題分享"],
    },
    "鉛作業": {
        "law": "依職業安全衛生教育訓練規則第11條 (初訓18小時) / 第18條 (複訓每3年3小時)",
        "purpose": "雇主對鉛作業主管,應使其接受有害作業主管安全教育訓練,以預防鉛中毒及相關危害。",
        "outline_initial": ["1. 鉛作業相關法規 (3小時)", "2. 鉛中毒預防規則 (3小時)", "3. 鉛之危害及測定 (3小時)", "4. 鉛作業環境改善及防護具 (3小時)", "5. 通風換氣裝置 (3小時)", "6. 有害物作業相關法規 (3小時)"],
        "outline_retraining": ["1. 鉛作業場所之危害評估及預防", "2. 鉛作業相關法規及管理執行", "3. 經驗及問題分享"],
    },
    "施工架": {
        "law": "依職業安全衛生教育訓練規則第12條 (初訓18小時) / 第18條 (複訓每3年3小時)",
        "purpose": "雇主對施工架組配作業主管,應使其接受營造作業主管安全教育訓練,以預防墜落及倒塌災害。",
        "outline_initial": ["1. 營造業相關法規 (3小時)", "2. 施工架組配作業危害預防 (3小時)", "3. 施工架構造、組配與拆除作業安全 (3小時)", "4. 防止墜落、感電及倒塌災害 (3小時)", "5. 個人防護具及安全防護設施 (3小時)", "6. 災害案例研討 (3小時)"],
        "outline_retraining": ["1. 施工架組配作業危害評估及預防", "2. 相關法規及管理執行", "3. 災害案例分享"],
    },
    "屋頂作業": {
        "law": "依職業安全衛生教育訓練規則第12條 (初訓18小時) / 第18條 (複訓每3年3小時)",
        "purpose": "雇主對屋頂作業主管,應使其接受營造作業主管安全教育訓練,以預防墜落災害。",
        "outline_initial": ["1. 營造業相關法規 (3小時)", "2. 屋頂作業危害預防 (3小時)", "3. 屋頂作業安全要領 (3小時)", "4. 墜落災害防止計畫 (3小時)", "5. 個人防護具及安全防護設施 (3小時)", "6. 災害案例研討 (3小時)"],
        "outline_retraining": ["1. 屋頂作業危害評估及預防", "2. 相關法規及管理執行", "3. 災害案例分享"],
    },
    "擋土支撐": {
        "law": "依職業安全衛生教育訓練規則第12條 (初訓18小時) / 第18條 (複訓每3年3小時)",
        "purpose": "雇主對擋土支撐作業主管,應使其接受營造作業主管安全教育訓練,以預防崩塌災害。",
        "outline_initial": ["1. 營造業相關法規 (3小時)", "2. 擋土支撐作業危害預防 (3小時)", "3. 擋土支撐構造、組立與拆除 (3小時)", "4. 土壤性質與穩定分析 (3小時)", "5. 個人防護具及安全防護設施 (3小時)", "6. 災害案例研討 (3小時)"],
        "outline_retraining": ["1. 擋土支撐作業危害評估及預防", "2. 相關法規及管理執行", "3. 災害案例分享"],
    },
    "模板支撐": {
        "law": "依職業安全衛生教育訓練規則第12條 (初訓18小時) / 第18條 (複訓每3年3小時)",
        "purpose": "雇主對模板支撐作業主管,應使其接受營造作業主管安全教育訓練,以預防倒塌災害。",
        "outline_initial": ["1. 營造業相關法規 (3小時)", "2. 模板支撐作業危害預防 (3小時)", "3. 模板支撐構造、組立與拆除 (3小時)", "4. 防止倒塌災害 (3小時)", "5. 個人防護具及安全防護設施 (3小時)", "6. 災害案例研討 (3小時)"],
        "outline_retraining": ["1. 模板支撐作業危害評估及預防", "2. 相關法規及管理執行", "3. 災害案例分享"],
    },
    "鋼構組配": {
        "law": "依職業安全衛生教育訓練規則第12條 (初訓18小時) / 第18條 (複訓每3年3小時)",
        "purpose": "雇主對鋼構組配作業主管,應使其接受營造作業主管安全教育訓練,以預防墜落及倒塌災害。",
        "outline_initial": ["1. 營造業相關法規 (3小時)", "2. 鋼構組配作業危害預防 (3小時)", "3. 鋼構構造、組配與拆除作業 (3小時)", "4. 防止墜落、感電及倒塌災害 (3小時)", "5. 個人防護具及安全防護設施 (3小時)", "6. 災害案例研討 (3小時)"],
        "outline_retraining": ["1. 鋼構組配作業危害評估及預防", "2. 相關法規及管理執行", "3. 災害案例分享"],
    },
    "露天開挖": {
        "law": "依職業安全衛生教育訓練規則第12條 (初訓18小時) / 第18條 (複訓每3年3小時)",
        "purpose": "雇主對露天開挖作業主管,應使其接受營造作業主管安全教育訓練,以預防崩塌及墜落災害。",
        "outline_initial": ["1. 營造業相關法規 (3小時)", "2. 露天開挖作業危害預防 (3小時)", "3. 土壤性質與穩定分析 (3小時)", "4. 防止崩塌災害 (3小時)", "5. 個人防護具及安全防護設施 (3小時)", "6. 災害案例研討 (3小時)"],
        "outline_retraining": ["1. 露天開挖作業危害評估及預防", "2. 相關法規及管理執行", "3. 災害案例分享"],
    },
    "堆高機": {
        "law": "依職業安全衛生教育訓練規則第14條 (初訓18小時) / 第18條 (複訓每3年3小時)",
        "purpose": "雇主對荷重在一公噸以上之堆高機操作人員,應使其接受特殊作業安全衛生教育訓練,以使其具備正確之操作觀念及安全衛生知識。",
        "outline_initial": ["1. 堆高機相關法規 (3小時)", "2. 堆高機之構造、性能與安全裝置 (3小時)", "3. 堆高機操作技術與實作 (6小時)", "4. 堆高機自動檢查及維護 (3小時)", "5. 災害預防與案例分析 (3小時)"],
        "outline_retraining": ["1. 最新堆高機相關法規修訂", "2. 堆高機自動檢查及事故預防", "3. 經驗及問題分享"],
    },
    "高空工作車": {
        "law": "依職業安全衛生教育訓練規則第14條 (初訓24小時) / 第18條 (複訓每3年3小時)",
        "purpose": "雇主對高空工作車操作人員,應使其接受特殊作業安全衛生教育訓練,以使其具備正確之操作觀念及安全衛生知識。",
        "outline_initial": ["1. 高空工作車相關法規 (3小時)", "2. 高空工作車之構造、性能與安全裝置 (3小時)", "3. 高空工作車操作技術與實作 (12小時)", "4. 高空工作車自動檢查及維護 (3小時)", "5. 災害預防與案例分析 (3小時)"],
        "outline_retraining": ["1. 最新高空工作車相關法規修訂", "2. 高空工作車自動檢查及事故預防", "3. 經驗及問題分享"],
    },
    "固定式起重機": {
        "law": "依職業安全衛生教育訓練規則第14條 (初訓18-38小時) / 第18條 (複訓每3年3小時)",
        "purpose": "雇主對固定式起重機操作人員,應使其接受特殊作業安全衛生教育訓練,以使其具備正確之操作觀念及安全衛生知識。",
        "outline_initial": ["1. 固定式起重機相關法規 (3小時)", "2. 起重機之構造、性能與安全裝置 (3小時)", "3. 起重機操作技術與實作 (10-25小時)", "4. 自動檢查與維護 (3小時)", "5. 災害預防與案例分析 (3小時)"],
        "outline_retraining": ["1. 最新固定式起重機相關法規修訂", "2. 起重機具自動檢查及事故預防", "3. 經驗及問題分享"],
    },
    "移動式起重機": {
        "law": "依職業安全衛生教育訓練規則第14條 (初訓18-38小時) / 第18條 (複訓每3年3小時)",
        "purpose": "雇主對移動式起重機操作人員,應使其接受特殊作業安全衛生教育訓練,以使其具備正確之操作觀念及安全衛生知識。",
        "outline_initial": ["1. 移動式起重機相關法規 (3小時)", "2. 起重機之構造、性能與安全裝置 (3小時)", "3. 起重機操作技術與實作 (10-25小時)", "4. 自動檢查與維護 (3小時)", "5. 災害預防與案例分析 (3小時)"],
        "outline_retraining": ["1. 最新移動式起重機相關法規修訂", "2. 起重機具自動檢查及事故預防", "3. 經驗及問題分享"],
    },
    "吊掛": {
        "law": "依職業安全衛生教育訓練規則第14條 (初訓18小時) / 第18條 (複訓每3年3小時)",
        "purpose": "雇主對使用起重機具從事吊掛作業人員,應使其接受特殊作業安全衛生教育訓練。",
        "outline_initial": ["1. 吊掛作業相關法規 (3小時)", "2. 吊掛器具之種類、構造與使用 (3小時)", "3. 吊掛作業技術與實作 (6小時)", "4. 吊掛器具之檢查與維護 (3小時)", "5. 災害預防與案例分析 (3小時)"],
        "outline_retraining": ["1. 吊掛作業相關法規", "2. 吊掛器具之檢查與維護", "3. 吊掛作業安全要領與事故預防"],
    },
    "鍋爐": {
        "law": "依職業安全衛生教育訓練規則第14條 (初訓50小時) / 第18條 (複訓每3年3小時)",
        "purpose": "雇主對鍋爐操作人員,應使其接受特殊作業安全衛生教育訓練。",
        "outline_initial": ["1. 鍋爐相關法規 (3小時)", "2. 鍋爐之構造、原理與安全裝置 (6小時)", "3. 鍋爐操作技術與實作 (30小時)", "4. 鍋爐自動檢查與維護 (6小時)", "5. 異常處置與災害預防 (5小時)"],
        "outline_retraining": ["1. 鍋爐相關法規", "2. 鍋爐自動檢查與維護", "3. 異常處置與事故預防"],
    },
    "壓力容器": {
        "law": "依職業安全衛生教育訓練規則第14條 (初訓35小時) / 第18條 (複訓每3年3小時)",
        "purpose": "雇主對第一種壓力容器操作人員,應使其接受特殊作業安全衛生教育訓練。",
        "outline_initial": ["1. 壓力容器相關法規 (3小時)", "2. 壓力容器之構造、原理與安全裝置 (6小時)", "3. 壓力容器操作技術與實作 (18小時)", "4. 壓力容器自動檢查與維護 (3小時)", "5. 異常處置與災害預防 (5小時)"],
        "outline_retraining": ["1. 壓力容器相關法規", "2. 壓力容器自動檢查", "3. 異常處置與事故預防"],
    },
    "高壓氣體": {
        "law": "依職業安全衛生教育訓練規則第14條 (初訓21-35小時) / 第18條 (複訓每3年3小時)",
        "purpose": "雇主對高壓氣體操作人員,應使其接受特殊作業安全衛生教育訓練。",
        "outline_initial": ["1. 高壓氣體相關法規 (3小時)", "2. 高壓氣體之性質與危害 (3小時)", "3. 高壓氣體設備之構造與操作 (12小時)", "4. 自動檢查與維護 (3小時)", "5. 異常處置與緊急應變 (3小時)"],
        "outline_retraining": ["1. 高壓氣體相關法規", "2. 高壓氣體設備之檢查與維護", "3. 異常處置與緊急應變"],
    },
    "急救人員": {
        "law": "依職業安全衛生教育訓練規則第15條 (初訓16小時) / 第18條 (複訓每3年3小時)",
        "purpose": "雇主應使急救人員接受急救人員安全衛生教育訓練,以具備緊急傷病處置之知識及技能。",
        "outline_initial": ["1. 急救相關法規與基本知識 (2小時)", "2. 心肺復甦術 (CPR) 與 AED 操作 (4小時)", "3. 創傷及出血處理 (2小時)", "4. 燒燙傷、化學灼傷處理 (2小時)", "5. 中毒、休克、骨折處理 (2小時)", "6. 緊急應變與運送 (2小時)", "7. 案例演練 (2小時)"],
        "outline_retraining": ["1. 急救相關法規更新", "2. CPR 與 AED 操作複習", "3. 常見傷病處置與案例演練"],
    },
    "職業安全衛生業務主管": {
        "law": "依職業安全衛生教育訓練規則第3條 (初訓21-42小時) / 第18條 (複訓每2年6小時)",
        "purpose": "雇主應使各事業單位之職業安全衛生業務主管接受教育訓練,以具備辦理職業安全衛生管理事項之能力。",
        "outline_initial": ["1. 職業安全衛生相關法規 (6小時)", "2. 職業安全衛生政策與管理計畫 (3小時)", "3. 危害辨識、風險評估與管理 (6小時)", "4. 職業災害調查、分析與案例研討 (3小時)", "5. 安全衛生管理實務 (3小時)"],
        "outline_retraining": ["1. 最新職安衛法規修訂", "2. 安全衛生管理實務新知", "3. 危害辨識與風險評估", "4. 案例分析與經驗分享"],
    },
    "職業安全衛生管理": {
        "law": "依職業安全衛生教育訓練規則第7條 (初訓115小時) / 第18條 (複訓每2年12小時)",
        "purpose": "雇主應使職業安全衛生管理人員接受教育訓練。",
        "outline_initial": ["1. 職業安全衛生法令、政策與管理系統 (12小時)", "2. 安全衛生概論與風險評估 (24小時)", "3. 機械、電氣、化學、火災爆炸危害預防 (24小時)", "4. 物理性、人因工程、健康管理 (24小時)", "5. 職業災害調查、分析與緊急應變 (12小時)", "6. 安全衛生管理實務、稽核與績效 (19小時)"],
        "outline_retraining": ["1. 最新職安衛法規修訂", "2. 風險評估與管理新趨勢", "3. 安全衛生績效管理", "4. 案例研討與經驗分享"],
    },
    "防火管理": {
        "law": "依消防法第13條及消防法施行細則第14條 (初訓12小時) / 複訓每3年6小時",
        "purpose": "依消防法規定,管理權人應指定防火管理人,並使其接受訓練,以製定消防防護計畫並執行火災預防工作。",
        "outline_initial": ["1. 消防法令與防火管理制度 (3小時)", "2. 火災基本知識與成因分析 (3小時)", "3. 消防安全設備之認識與維護 (3小時)", "4. 消防防護計畫之製定與推行 (3小時)"],
        "outline_retraining": ["1. 消防法規修訂", "2. 消防防護計畫之檢討與更新", "3. 自衛消防編組與演練"],
    },
    "道路危險物品": {
        "law": "依道路交通管理處罰條例第29-2條 (初訓16-20小時) / 複訓每3年12-14小時",
        "purpose": "使道路危險物品運送人員具備正確之運送觀念及安全知識。",
        "outline_initial": ["1. 道路危險物品相關法規 (3小時)", "2. 危險物品分類、標示與標誌 (3小時)", "3. 運送車輛與容器之安全要求 (4小時)", "4. 緊急應變與事故處理 (3小時)", "5. 案例分析 (3-7小時)"],
        "outline_retraining": ["1. 道路危險物品法規更新", "2. 危險物品分類與標示複習", "3. 運送安全與緊急應變"],
    },
    "職安卡": {
        "law": "依職業安全衛生教育訓練規則第17條 (一般安全衛生教育訓練,新雇勞工 6 小時以上)",
        "purpose": "使勞工具備基本之職業安全衛生概念及自我保護能力,符合職安卡發證要求。",
        "outline_initial": ["1. 職業安全衛生概論與法規 (1.5小時)", "2. 個人防護具之使用與維護 (1小時)", "3. 危害辨識與作業安全 (1.5小時)", "4. 緊急應變與急救基本知識 (1小時)", "5. 健康促進與職業病預防 (1小時)"],
        "outline_retraining": ["1. 職業安全衛生法規更新", "2. 危害辨識複習", "3. 個人防護具與急救"],
    },
}


def get_course_outline(course_name, hours="3", category="複訓"):
    sorted_keys = sorted(COURSE_TEMPLATES.keys(), key=len, reverse=True)
    matched = None
    for keyword in sorted_keys:
        if keyword in course_name:
            matched = COURSE_TEMPLATES[keyword]
            break
    if not matched:
        return {
            "law": "依職業安全衛生教育訓練規則相關規定辦理",
            "purpose": "使作業人員具備相關安全衛生知識及技能",
            "outline": [f"請參考協會提供之課程簡章 (預計 {hours} 小時)"],
        }
    use_initial = category == "初訓"
    if not use_initial:
        try:
            if int(hours) >= 12:
                use_initial = True
        except (ValueError, TypeError):
            pass
    return {
        "law": matched["law"],
        "purpose": matched["purpose"],
        "outline": matched["outline_initial"] if use_initial else matched["outline_retraining"],
    }


# ==========================================================================
# 解析器
# ==========================================================================
class TichaScraper:
    name = "台灣省工商安全衛生協會"
    code = "ticsha"
    desc = "中壢・桃園・新竹 三個分會"
    branches = {
        "中壢": "https://cli.ticsha.org.tw/course_list",
        "桃園": "https://tyn.ticsha.org.tw/course_list",
        "新竹": "https://hsz.ticsha.org.tw/course_list",
    }
    
    # 進度追蹤 (給前端讀取)
    _progress = {"stage": "idle", "current": 0, "total": 0, "message": ""}
  
    @classmethod
    def get_progress(cls):
        return dict(cls._progress)
    
    @classmethod
    def scrape(cls, fetch_details=False):
        """Commit 18d: 抓列表 + 詳細頁(學科/術科)。第二次跑有 cache 會快很多。"""
        cls._progress = {"stage": "list", "current": 0, "total": len(cls.branches), "message": "正在抓取課程列表..."}
        all_courses = []
        seen = set()

        for i, (branch_name, base_url) in enumerate(cls.branches.items()):
            try:
                cls._progress["current"] = i + 1
                cls._progress["message"] = f"抓取 {branch_name} 分會列表..."
                print(f"  抓取 {branch_name} 分會...")
                courses = cls._parse_page(base_url, branch_name)
                new_count = 0
                for c in courses:
                    if c["id"] not in seen:
                        seen.add(c["id"])
                        all_courses.append(c)
                        new_count += 1
                print(f"    → 找到 {new_count} 筆")
            except Exception as e:
                print(f"    ✗ 失敗: {e}")

        # === Commit 18f: 緊急回滾 — Ticsha 改回不抓 detail(回到 BRANCH_ADDRESSES 模式)===
        # 原因:Ticsha 並行抓 564 個 detail 讓 Render Free 0.1 CPU 撐不住,worker OOM/timeout
        # Ticsha 維持單一分會地址(犧牲學科/術科,但 Ticsha 課程大多沒有術科,影響小)
        # 未來如果升級 Render Starter 或改用外部資料庫,可以把下面那行解除註解再啟用
        # cls._fetch_all_details(all_courses)
        # =====================================================================

        cls._progress = {"stage": "done", "current": 0, "total": 0, "message": "完成"}
        return all_courses
    
    @classmethod
    def _fetch_all_details(cls, courses):
        """Commit 18d: 抓詳細地址 + cache lookup + 並行(原本是序列,現在並行+跳 cache)。"""
        from concurrent.futures import ThreadPoolExecutor

        # === Commit 18d: cache 查詢(跳過已抓過的 detail)===
        cache = getattr(cls, "_cache", {}) or {}
        force_refresh = getattr(cls, "_force_refresh", False)

        targets_to_fetch = []
        cache_hit_count = 0
        for course in courses:
            if not course.get("course_id"):
                continue
            cached = cache.get(course["id"])
            if not force_refresh and cached and cached.get("location"):
                # Cache hit:直接複製,跳過 detail
                course["location"] = cached["location"]
                cache_hit_count += 1
            else:
                targets_to_fetch.append(course)

        print(f"  [Ticsha] Cache hit: {cache_hit_count} 筆(跳過 detail),需抓 detail: {len(targets_to_fetch)} 筆")
        # =====================================================

        cls._progress = {"stage": "details", "current": 0, "total": len(targets_to_fetch), "message": "Ticsha 抓詳細地址..."}

        def _grab(course):
            detail_addr = cls._fetch_detail(course)
            if detail_addr:
                course["location"] = detail_addr
            else:
                # 抓不到 detail 就 fallback 用分會固定地址
                course["location"] = BRANCH_ADDRESSES.get(course["branch"], f"{course['branch']} (詳洽協會)")
            return course

        with ThreadPoolExecutor(max_workers=6) as pool:
            for i, _ in enumerate(pool.map(_grab, targets_to_fetch)):
                cls._progress["current"] = i + 1
                cls._progress["message"] = f"Ticsha 抓詳細地址 ({i+1}/{len(targets_to_fetch)})"
    
    @classmethod
    def _fetch_detail(cls, course):
        """抓單一課程的詳細頁面,取出乾淨的上課地址"""
        try:
            cid = course.get("course_id", "")
            if not cid:
                return None
            
            base_domain = course["url"].split("/course_list")[0]
            detail_urls = [
                f"{base_domain}/@@course_list?course_detail&id={cid}",
                f"{base_domain}/@@course_list?id={cid}",
            ]
            
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0",
                "X-Requested-With": "XMLHttpRequest",
            }
            
            for url in detail_urls:
                try:
                    resp = requests.get(url, headers=headers, timeout=10)
                    resp.encoding = "utf-8"
                    text = resp.text
                    
                    # 模式 1: 「上課地址：xxx」這種文字 (只取地址,到「樓」或「號」停止)
                    m = re.search(r"上課地[址點][:：]\s*([^<\n]{5,200})", text)
                    if m:
                        addr = m.group(1).strip()
                        # 砍掉電話、Email、傳真、教室、TEL 等雜訊 (出現這些字就截斷)
                        for stop_word in [" 0", " TEL", " Tel", " tel", "電話", "傳真", "Email", "email", " /", "yuda", "教室", "@"]:
                            idx = addr.find(stop_word)
                            if idx > 0:
                                addr = addr[:idx]
                        addr = re.sub(r"\s+", " ", addr).strip()
                        # 確認是有效地址 (至少含「市」「區」「路/街」「號/樓」)
                        if re.search(r"[市縣].+[區市].+(號|樓|街|路)", addr) and len(addr) < 60:
                            return addr
                    
                    # 模式 2: 找 HTML 中的純地址 pattern
                    soup = BeautifulSoup(text, "lxml")
                    for tag in soup.find_all(["td", "li", "p", "span", "div"]):
                        t = tag.get_text(strip=True)
                        # 純地址 pattern (市區+路+號)
                        if re.match(r"^[\u4e00-\u9fff]+[市縣][\u4e00-\u9fff]+區.{3,40}[號樓]$", t):
                            if len(t) < 60:
                                return t
                except Exception:
                    continue
            return None
        except Exception:
            return None
    
    @classmethod
    def _parse_page(cls, url, branch):
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
        resp = requests.get(url, headers=headers, timeout=20)
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "lxml")
        base_domain = "https://" + url.split("/")[2]
        courses = []
        for table in soup.find_all("table"):
            for tr in table.find_all("tr"):
                tds = tr.find_all("td")
                if len(tds) < 8:
                    continue
                try:
                    seq = tds[0].get_text(strip=True)
                    if not seq.isdigit():
                        continue
                    name = tds[1].get_text(strip=True)
                    start_date = tds[2].get_text(strip=True)
                    end_date = tds[3].get_text(strip=True)
                    class_type = tds[4].get_text(strip=True)
                    hours = tds[6].get_text(strip=True)
                    fee = tds[7].get_text(strip=True)
                    status = tds[8].get_text(strip=True) if len(tds) > 8 else ""
                    
                    category = "未知"
                    course_id = ""
                    register_url = ""
                    for a in tr.find_all("a", href=True):
                        href = a["href"]
                        if "reg_course" in href and "category=" in href:
                            if "category=initial_training" in href:
                                category = "初訓"
                            elif "category=retraining" in href:
                                category = "複訓"
                            m = re.search(r"id=(\d+)", href)
                            if m:
                                course_id = m.group(1)
                            register_url = href if href.startswith("http") else base_domain + href
                            break
                    # commit 16: 額滿課可能沒 reg_course → 改用 brochure 或列表頁
                    if not course_id or not register_url:
                        for a in tr.find_all("a", href=True):
                            if "download_brochure" in a["href"]:
                                href = a["href"]
                                if not course_id:
                                    mid = re.search(r"id=(\d+)", href)
                                    if mid:
                                        course_id = mid.group(1)
                                if not register_url:
                                    register_url = href if href.startswith("http") else base_domain + href
                                break
                    if not register_url:
                        register_url = url
                    if category == "未知":
                        try:
                            h = int(hours)
                            if h <= 6:
                                category = "複訓"
                            elif h >= 12:
                                category = "初訓"
                        except (ValueError, TypeError):
                            pass
                    
                    nationality = "本國籍"
                    for nat in ["越南籍", "印尼籍", "菲律賓籍", "泰國籍"]:
                        if nat in name:
                            nationality = nat
                            break
                    
                    # 推測上課時間 (依班別)
                    class_time = ""
                    if "日間" in class_type:
                        class_time = "上午 9:00 - 下午 17:00"
                    elif "夜間" in class_type:
                        class_time = "晚上 18:30 - 21:30"
                    elif "假日" in class_type:
                        class_time = "上午 9:00 - 下午 17:00"
                    
                    # 上課地點 (依分會)
                    location = BRANCH_ADDRESSES.get(branch, f"{branch} (詳洽協會)")
                    
                    courses.append({
                        "id": f"{cls.code}_{branch}_{course_id or seq + '_' + start_date}",
                        "course_id": course_id,
                        "institute": cls.name, "branch": branch, "category": category,
                        "nationality": nationality, "name": name, "start_date": start_date + (f"({_wt[0]})" if (_wt := locals().get('weekday_time', '')) and '\u4e00' <= _wt[0] <= '\u9fff' else ""),
                        "end_date": end_date, "class_type": class_type, "class_time": class_time,
                        "location": location, "hours": hours, "fee": fee, "status": status,
                        "url": url, "register_url": register_url,
                    })
                except (IndexError, ValueError):
                    continue
        return courses


# =========================================================
# 解析器 #2:中國生產力中心 (CPC)
# =========================================================
class CPCScraper:
    name = "中國生產力中心"
    code = "cpc"
    desc = "桃園・台北承德 / 職安・消防・營建 三類"
    base_url = "https://store.cpc.org.tw"
    # 3 個類別:職安(110) + 消防(111) + 營建(112)
    categories = {
        "職安": 110,
        "消防": 111,
        "營建": 112,
    }
    # 只保留這 2 個地區
    target_regions = {"桃園", "台北"}

    # 進度追蹤 (給前端讀取)
    _progress = {"stage": "idle", "current": 0, "total": 0, "message": ""}

    @classmethod
    def get_progress(cls):
        return dict(cls._progress)

    @classmethod
    def _make_session(cls):
        import requests
        s = requests.Session(); s.verify = False
        s.headers.update({
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/118.0.0.0 Safari/537.36"),
            "Accept-Language": "zh-TW,zh;q=0.9",
        })
        return s

    @classmethod
    def _fetch_page(cls, session, cat_id, page=1):
        import re as _re
        from datetime import datetime as _dt, timedelta as _td; _t = _dt.now(); url = f"{cls.base_url}/Train/Category/{cat_id}?StartDate={_t.strftime('%Y%m%d')}&EndDate={(_t + _td(days=90)).strftime('%Y%m%d')}"
        if page > 1:
            url += f"&page={page}"
        resp = session.get(url, timeout=20)
        resp.raise_for_status()
        return BeautifulSoup(resp.content, "html.parser")

    @classmethod
    def _last_page(cls, soup):
        import re as _re
        pag = soup.find("ul", class_="pagination")
        if not pag:
            return 1
        for a in pag.find_all("a"):
            if "最後一頁" in a.get_text():
                m = _re.search(r"page=(\d+)", a.get("href", ""))
                if m:
                    return int(m.group(1))
        return 1

    @classmethod
    def _parse_row(cls, tr):
        import re as _re
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 4:
            return None
        region_badge = tds[0].find("span", class_="badge")
        if not region_badge:
            return None
        region = region_badge.get_text(strip=True)

        status_badge = tds[1].find("span", class_="badge")
        status = status_badge.get_text(strip=True) if status_badge else ""

        a = tds[1].find("a")
        if not a:
            return None
        link = a.get("href", ""); link = "https://store.cpc.org.tw" + link if link.startswith("/Train/") else link
        title_attr = a.get("title", "").strip()
        m = _re.match(r"^(\S+)\s+(.+)$", title_attr)
        if m:
            code_, name = m.group(1), m.group(2)
        else:
            code_, name = "", title_attr

        small = a.find("small")
        subtitle = " ".join(small.get_text(separator=" ", strip=True).split()) if small else ""

        deadline = ""
        for s in tds[1].find_all("small"):
            txt = s.get_text(strip=True)
            if "報名截止日" in txt:
                dm = _re.search(r"(\d{4}-\d{2}-\d{2})", txt)
                if dm:
                    deadline = dm.group(1)

        hours = tds[2].get_text(strip=True)

        td4_text = " ".join(tds[3].get_text(separator=" ", strip=True).split())
        dm = _re.match(r"(\d{4}-\d{2}-\d{2})~(\d{4}-\d{2}-\d{2})\s*(?:\(([^)]+)\))?", td4_text)
        if dm:
            start_date = dm.group(1)
            end_date = dm.group(2)
            weekday_time = (dm.group(3) or "").strip()
        else:
            start_date = end_date = ""
            weekday_time = td4_text

        full = name + " " + subtitle + " " + weekday_time
        time_m = _re.search(r"(\d{1,2}):\d{2}", weekday_time)
        if "夜" in full:
            day_type = "夜間班"
        elif time_m and int(time_m.group(1)) >= 17:
            day_type = "夜間班"
        elif "假日" in full or any(d in weekday_time for d in ["六", "日"]):
            day_type = "假日班"
        else:
            day_type = "日間班"

        nm = name + subtitle
        if "回訓" in nm or "在職" in nm or "複訓" in nm:
            category = "複訓"
        elif "初訓" in nm:
            category = "初訓"
        else:
            category = "—"

        if any(kw in name for kw in ("移工", "外籍", "越南", "菲律賓", "印尼", "泰")):
            nationality = "外籍"
        else:
            nationality = "本國籍"

        display_name = f"{name} {subtitle}".strip() if subtitle else name

        return {
            "institute": "中國生產力中心", "id": f"cpc-{code_}",
            "code": code_,
            "name": display_name,
            "branch": region,
            "category": category,
            "nationality": nationality,
            "start_date": start_date + (f"({_wt[0]})" if (_wt := locals().get('weekday_time', '')) and '\u4e00' <= _wt[0] <= '\u9fff' else ""),
            "end_date": end_date,
            "class_time": _re.sub(r"(\d+):(\d+)-(\d+):(\d+)", lambda m: f"{'上午' if int(m.group(1))<12 else '下午'} {int(m.group(1))}:{m.group(2)} - {'上午' if int(m.group(3))<12 else '下午'} {int(m.group(3))}:{m.group(4)}", _re.sub(r"^\D+", "", weekday_time)),
            "class_type": day_type,
            "hours": hours,
            "fee": "",
            "status": status,
            "deadline": deadline,
            "register_url": link,
            "location": "",
            "source": "cpc",
        }

    @classmethod
    def _parse_detail(cls, html):
        """Commit 18b: 學科 / 術科 分開抓,合併時用換行符號連接。"""
        soup = BeautifulSoup(html, "html.parser")
        fee = ""

        # === 學科 / 術科 分開抓 ===
        _loc_xueke = ""   # 學科
        _loc_shuke = ""   # 術科

        # Strategy A: <tr>/<th>/<td> 結構優先
        for tr in soup.find_all("tr"):
            th = tr.find("th")
            if not th:
                continue
            th_text = th.get_text(strip=True)
            td = tr.find("td")
            if not td:
                continue
            td_text = " ".join(td.get_text(separator=" ", strip=True).split())

            if "學科上課地點" in th_text or "學科上課地址" in th_text:
                _loc_xueke = td_text
            elif "術科上課地點" in th_text or "術科上課地址" in th_text:
                _loc_shuke = td_text
            elif ("上課地點" in th_text or "上課地址" in th_text) and not _loc_xueke and not _loc_shuke:
                # 沒分學科 / 術科時當作學科
                _loc_xueke = td_text

        # Strategy B: 全文 regex fallback(網站可能改版)
        if not _loc_xueke or not _loc_shuke:
            body_text = soup.get_text(separator="\n", strip=True)
            if not _loc_xueke:
                m = re.search(r"學科上課地[址點][:\uff1a]?\s*([^\n]+)", body_text)
                if m:
                    _loc_xueke = m.group(1).strip()
            if not _loc_shuke:
                m = re.search(r"術科上課地[址點][:\uff1a]?\s*([^\n]+)", body_text)
                if m:
                    _loc_shuke = m.group(1).strip()
            # 都沒抓到 → 用一般「上課地點」當 fallback
            if not _loc_xueke and not _loc_shuke:
                m = re.search(r"上課地[址點][:\uff1a]?\s*([^\n]+)", body_text)
                if m:
                    _loc_xueke = m.group(1).strip()

        # 合併(換行格式,儲存時用 \n,信件顯示時會轉 <br>)
        if _loc_xueke and _loc_shuke and _loc_xueke != _loc_shuke:
            address = f"學科:{_loc_xueke}\n術科:{_loc_shuke}"
        elif _loc_xueke:
            address = _loc_xueke
        elif _loc_shuke:
            address = _loc_shuke
        else:
            address = ""
        # ========================================

        # 費用
        ps = soup.select_one("span.text-red.lead")
        if ps:
            fee = ps.get_text(strip=True).replace(",", "")
        return address, fee

    @classmethod
    def scrape(cls, fetch_details=True):
        """抓 CPC 3 個類別,自動過濾桃園+台北,並補上詳細頁的地址+費用。"""
        from concurrent.futures import ThreadPoolExecutor

        session = cls._make_session()
        cls._progress = {"stage": "list", "current": 0, "total": 0, "message": "CPC 正在掃描列表..."}

        # 階段 1:預掃每個類別第 1 頁,得知總頁數
        page_jobs = []  # 元素是 (cat_id, page_num, 預抓好的 soup 或 None)
        for cat_name, cat_id in cls.categories.items():
            try:
                soup1 = cls._fetch_page(session, cat_id, 1)
                last = cls._last_page(soup1)
                page_jobs.append((cat_id, 1, soup1))
                for p in range(2, last + 1):
                    page_jobs.append((cat_id, p, None))
                print(f"  [CPC] {cat_name}(類別 {cat_id}) 共 {last} 頁")
            except Exception as e:
                print(f"  [CPC] 抓 {cat_name} 失敗: {e}")

        cls._progress["total"] = len(page_jobs)

        # 階段 2:並行抓所有列表頁,套地區過濾
        all_courses = []
        seen_ids = set()

        def grab_page(job):
            cat_id, p, prefetched = job
            try:
                soup = prefetched if prefetched is not None else cls._fetch_page(session, cat_id, p)
                rows = []
                table = soup.find("table", class_="table table-hover")
                if table:
                    for tr in table.find_all("tr")[1:]:  # 跳過表頭
                        c = cls._parse_row(tr)
                        if c and any(r in c["branch"] for r in cls.target_regions):
                            rows.append(c)
                return rows
            except Exception as e:
                print(f"  [CPC] page {cat_id}/{p} 失敗: {e}")
                return []

        with ThreadPoolExecutor(max_workers=6) as pool:
            for idx, page_rows in enumerate(pool.map(grab_page, page_jobs)):
                cls._progress["current"] = idx + 1
                cls._progress["message"] = f"CPC 掃描列表 {idx+1}/{len(page_jobs)}..."
                for c in page_rows:
                    if c["id"] not in seen_ids:
                        seen_ids.add(c["id"])
                        all_courses.append(c)

        # 階段 3:對篩選後的課程抓詳細頁拿地址+費用(Commit 18b: cache 加速)
        if fetch_details and all_courses:
            # === Commit 18b: cache 查詢(跳過已抓過的 detail)===
            cache = getattr(cls, "_cache", {}) or {}
            force_refresh = getattr(cls, "_force_refresh", False)

            targets_to_fetch = []
            cache_hit_count = 0
            for course in all_courses:
                cached = cache.get(course["id"])
                if not force_refresh and cached and cached.get("location"):
                    # Cache hit:直接複製 location + fee,跳過 detail 頁
                    course["location"] = cached.get("location", "")
                    course["fee"] = cached.get("fee", "")
                    cache_hit_count += 1
                else:
                    # Cache miss(新課 / 上次失敗 / 強制全抓):加入待抓清單
                    targets_to_fetch.append(course)

            print(f"  [CPC] Cache hit: {cache_hit_count} 筆(跳過 detail),需抓 detail: {len(targets_to_fetch)} 筆")
            # ===================================================

            cls._progress = {
                "stage": "details", "current": 0, "total": len(targets_to_fetch),
                "message": f"抓 CPC 課程詳細資料 0/{len(targets_to_fetch)}..."
            }

            def grab_detail(course):
                try:
                    r = session.get(course["register_url"], timeout=20)
                    addr, fee = cls._parse_detail(r.text)
                    course["location"] = addr
                    course["fee"] = fee
                except Exception as e:
                    print(f"  [CPC] detail {course['code']} 失敗: {e}")
                return course

            with ThreadPoolExecutor(max_workers=6) as pool:
                for idx, _ in enumerate(pool.map(grab_detail, targets_to_fetch)):
                    cls._progress["current"] = idx + 1
                    cls._progress["message"] = f"抓 CPC 詳細 {idx+1}/{len(targets_to_fetch)}..."

        cls._progress = {"stage": "idle", "current": 0, "total": 0, "message": ""}
        print(f"  [CPC] 完成,共 {len(all_courses)} 堂課(桃園+台北)")
        return all_courses

# =========================================================
# 解析器 #3:中華民國工業安全衛生協會 (ISHA)
# =========================================================
class ISHAScraper:
    name = "中華民國工業安全衛生協會"
    code = "isha"
    desc = "北區 5 個職訓中心：台北・新北・桃園・中壢・新竹"
    base_url = "https://isha.org.tw"
    list_url = "https://isha.org.tw/Msite/tech/serch.aspx"
    detail_url_tpl = "https://isha.org.tw/Msite/tech/serch_inner.aspx?WorkLogID={}"

    # 北區 5 站對應到的簡稱
    # 注意:「中壢」必須在「桃園」之前(因為「桃園職業訓練中心(中壢教室)」兩個關鍵字都會匹配)
    NORTH_STATIONS = (
        ("中壢", "中壢"),
        ("台北", "台北"),
        ("新北", "新北"),
        ("桃園", "桃園"),
        ("新竹", "新竹"),
    )

    _progress = {"stage": "idle", "current": 0, "total": 0, "message": ""}
    _debug_counter = 0

    @classmethod
    def get_progress(cls):
        return dict(cls._progress)

    @classmethod
    def _make_session(cls):
        import urllib3 as _isha_urllib3
        _isha_urllib3.disable_warnings(_isha_urllib3.exceptions.InsecureRequestWarning)
        s = requests.Session()
        s.verify = False
        s.headers.update({
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/118.0.0.0 Safari/537.36"),
            "Accept-Language": "zh-TW,zh;q=0.9",
        })
        return s

    @classmethod
    def _vs(cls, soup):
        d = {}
        for n in ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"):
            i = soup.find("input", {"name": n})
            d[n] = i.get("value", "") if i else ""
        return d

    @classmethod
    def _roc_to_west(cls, s):
        m = re.match(r"^(\d{3})(\d{2})(\d{2})$", str(s).strip())
        if not m:
            return ""
        y, mo, d = m.groups()
        return f"{int(y) + 1911:04d}-{mo}-{d}"

    @classmethod
    def _find_next_page_target(cls, soup, current_page):
        # Strategy 1: BT_<current+1>
        for tag in soup.find_all(["a", "input"]):
            onclick = (tag.get("href", "") + " " + (tag.get("onclick") or ""))
            pm = re.search(rf"__doPostBack\('([^']*\$BT_{current_page + 1})'", onclick)
            if pm:
                return pm.group(1), current_page + 1
        # Strategy 2: smallest BT_N > current
        next_n = None
        next_t = None
        for tag in soup.find_all(["a", "input"]):
            onclick = (tag.get("href", "") + " " + (tag.get("onclick") or ""))
            pm = re.search(r"__doPostBack\('([^']*\$BT_(\d+))'", onclick)
            if pm:
                n = int(pm.group(2))
                if n > current_page:
                    if next_n is None or n < next_n:
                        next_n = n
                        next_t = pm.group(1)
        if next_t:
            return next_t, next_n
        # Strategy 3: BT_right (ISHA 下一頁 icon)
        for tag in soup.find_all(["a", "input"]):
            onclick = (tag.get("href", "") + " " + (tag.get("onclick") or ""))
            pm = re.search(r"__doPostBack\('([^']*\$BT_right)'", onclick)
            if pm:
                return pm.group(1), current_page + 1
        # Strategy 4: BT_end (跳下一批)
        for tag in soup.find_all(["a", "input"]):
            onclick = (tag.get("href", "") + " " + (tag.get("onclick") or ""))
            pm = re.search(r"__doPostBack\('([^']*\$BT_end)'", onclick)
            if pm:
                return pm.group(1), current_page + 1
        # Strategy 5: 含「下一頁/>/Next」文字
        for tag in soup.find_all(["a", "input"]):
            text = (tag.get_text(strip=True) or "") + " " + \
                   str(tag.get("value", "")) + " " + str(tag.get("title", ""))
            if any(kw in text for kw in (">", "下一頁", "下頁", "Next", "next", "→")):
                onclick = (tag.get("href", "") + " " + (tag.get("onclick") or ""))
                pm = re.search(r"__doPostBack\('([^']+)'", onclick)
                if pm:
                    target = pm.group(1)
                    if f"BT_{current_page}" in target or target.endswith("BT_1"):
                        continue
                    return target, current_page + 1
        return None, None

    # -----------------------------------------------------
    # 列表頁解析
    # -----------------------------------------------------
    @classmethod
    def _parse_list(cls, soup):
        results = []
        seen_wid = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            wid_m = re.search(r"WorkLogID=([A-Za-z0-9]+)", href)
            if not wid_m:
                continue
            wid = wid_m.group(1)
            if wid in seen_wid:
                continue
            seen_wid.add(wid)

            full = " ".join(a.get_text(" ", strip=True).split())
            if not full:
                continue

            # status — v7: 加「預定開課」
            name = full
            status = ""
            first_idx = len(full)
            for _isha_st in ("確定開課", "預定開課", "招生中", "已截止", "已額滿", "完成報名"):
                idx = full.find(_isha_st)
                if 0 <= idx < first_idx:
                    first_idx = idx
                    status = _isha_st
            if status:
                name = full[:first_idx].strip()

            ct_m = re.search(r"(日間班|夜間班|假日班)", full)
            class_type = ct_m.group(1) if ct_m else ""

            hours = ""
            hr_m = re.search(r"課程時數[:：]?\s*(\d+\.?\d*)", full)
            if hr_m:
                hours = hr_m.group(1)

            cat_raw = ""
            cm = re.search(r"初/在職[:：]?\s*(\S+?)(?=\s|開課|課程|$)", full)
            if cm:
                cat_raw = cm.group(1)

            start_date = ""
            end_date = ""
            dr_m = re.search(r"開課日期[:：]?\s*(\d{7})\s*[-~]\s*(\d{7})", full)
            if dr_m:
                start_date = cls._roc_to_west(dr_m.group(1))
                end_date = cls._roc_to_west(dr_m.group(2))
            else:
                ds_m = re.search(r"開課日期[:：]?\s*(\d{7})", full)
                if ds_m:
                    start_date = cls._roc_to_west(ds_m.group(1))

            fee = ""
            fe_m = re.search(r"課程費用[:：]?\s*([\d,]+)\s*元", full)
            if fe_m:
                fee = fe_m.group(1).replace(",", "")

            if href.startswith("http"):
                reg = href
            else:
                reg = cls.base_url + ("" if href.startswith("/") else "/") + href

            nat = "外國籍" if any(k in name for k in
                ("越南", "印尼", "菲律賓", "泰國", "外籍", "外國", "Vietnam", "Indonesia")
            ) else "本國籍"

            if "初訓" in cat_raw:
                category = "初訓"
            elif "在職" in cat_raw or "回訓" in cat_raw:
                category = "複訓"
            else:
                category = ""  # v7: 留空,讓詳細頁覆蓋

            if not class_type:
                if "夜" in name:
                    class_type = "夜間班"
                elif "假日" in name or "週六" in name or "週日" in name:
                    class_type = "假日班"
                else:
                    class_type = "日間班"

            results.append({
                "institute": cls.name,
                "id": f"isha-{wid}",
                "code": wid,
                "name": name,
                "branch": "",
                "category": category,
                "nationality": nat,
                "start_date": start_date,
                "end_date": end_date,
                "class_type": class_type,
                "class_time": "",
                "hours": hours,
                "fee": fee,
                "status": status,
                "deadline": "",
                "register_url": reg,
                "location": "",
                "source": "isha",
                "work_log_id": wid,
            })
        return results

    # -----------------------------------------------------
    # 詳細頁解析 (v7: 含重試 + 多欄位 fallback)
    # -----------------------------------------------------
    @classmethod
    def _fetch_detail(cls, session, course):
        try:
            wid = course.get("work_log_id") or course.get("code", "")
            if not wid:
                return
            url = cls.detail_url_tpl.format(wid)

            # v7: 1 次重試
            r = None
            for attempt in range(2):
                try:
                    r = session.get(url, timeout=20)
                    r.encoding = "utf-8"
                    break
                except Exception:
                    if attempt == 0:
                        time.sleep(2)
                        continue
                    raise
            if r is None:
                return

            soup = BeautifulSoup(r.text, "html.parser")
            text = soup.get_text(separator="\n", strip=True)

            if cls._debug_counter < 3:
                cls._debug_counter += 1
                idx = cls._debug_counter
                print(f"  [ISHA DEBUG#{idx}] wid={wid} HTTP={r.status_code} text_len={len(text)}")
                preview = text[:800].replace("\n", " ⏎ ")
                print(f"  [ISHA DEBUG#{idx}] text[:800]={preview!r}")

            # === 站別 (v8: 嚴格優先「開課站別」,Pattern C 防呆) ===
            raw_station = ""
            # Pattern A (嚴格): 優先「開課站別:」
            if (m := re.search(r"開課站別\s*[:：]\s*([^\n]+)", text)):
                raw_station = m.group(1).strip()
            # Pattern A2 (寬鬆): 一般「站別:」
            elif (m := re.search(r"站別\s*[:：]\s*([^\n]+)", text)):
                raw_station = m.group(1).strip()
            # Pattern B: 「站別」標籤後 3 行內找站別值
            if not raw_station:
                lines = text.split("\n")
                for i, line in enumerate(lines):
                    if "站別" in line:
                        for j in range(i + 1, min(i + 4, len(lines))):
                            cand = lines[j].strip()
                            if cand and ("職訓" in cand or "職業訓練" in cand or "服務處" in cand or "教室" in cand):
                                raw_station = cand
                                break
                        if raw_station:
                            break
            # Pattern C (v8: 防呆 — 只有當文本完全沒「站別」字眼才fallback,避免誤抓)
            if not raw_station and "站別" not in text:
                for kw in ("台北職訓中心", "新北職業訓練中心", "桃園職業訓練中心",
                           "新竹區職業訓練中心", "台中職業訓練中心", "彰化區職業訓練中心",
                           "雲林職訓中心", "高雄服務處", "台南職訓中心", "台中職訓"):
                    if kw in text:
                        raw_station = kw
                        break
            course["_raw_station"] = raw_station
            for short, alias in cls.NORTH_STATIONS:
                if alias in raw_station:
                    course["branch"] = short
                    break

            # === 開課日期/結業日期 (v7 新增) ===
            if not course.get("start_date"):
                date_section = re.search(r"課程日期(.*?)(?=課程時數|時段|報名截止|電話|學科上課|備註|$)", text, re.DOTALL)
                if date_section:
                    dates = re.findall(r"(\d{3})年(\d{1,2})月(\d{1,2})日", date_section.group(1))
                    if dates:
                        y, mo, d = dates[0]
                        course["start_date"] = f"{int(y)+1911:04d}-{int(mo):02d}-{int(d):02d}"
                        if len(dates) >= 2:
                            y, mo, d = dates[1]
                            course["end_date"] = f"{int(y)+1911:04d}-{int(mo):02d}-{int(d):02d}"

            # === 課程時數 (v7 新增 — 從詳細頁補) ===
            if not course.get("hours"):
                if (h := re.search(r"課程時數\s*[:：]?\s*(\d+\.?\d*)", text)):
                    course["hours"] = h.group(1)

            # === 初/在職 → category (v7 新增 — 從詳細頁補) ===
            if not course.get("category"):
                if (c := re.search(r"初/在職\s*[:：]?\s*(\S+)", text)):
                    cat = c.group(1)
                    if "初訓" in cat:
                        course["category"] = "初訓"
                    elif "在職" in cat or "回訓" in cat:
                        course["category"] = "複訓"

            # === 課程費用 ===
            if not course.get("fee"):
                if (_isha_fee := re.search(r"課程費用\s*[:：]?\s*([\d,]+)\s*元?", text)):
                    course["fee"] = _isha_fee.group(1).replace(",", "")

            # === 上課地址 (v8: 修 typo 地點→地址 + 同時抓學科 & 術科) ===
            _loc_xueke = ""   # 學科
            _loc_shuke = ""   # 術科
            if (m := re.search(r"學科上課地址\s*[:：]\s*([^\n]+)", text)):
                _loc_xueke = m.group(1).strip()
            if (m := re.search(r"術科上課地址\s*[:：]\s*([^\n]+)", text)):
                _loc_shuke = m.group(1).strip()
            # 萬一網站改字眼,備用 fallback (地點)
            if not _loc_xueke and (m := re.search(r"學科上課地點\s*[:：]\s*([^\n]+)", text)):
                _loc_xueke = m.group(1).strip()
            if not _loc_shuke and (m := re.search(r"術科上課地點\s*[:：]\s*([^\n]+)", text)):
                _loc_shuke = m.group(1).strip()
            # 通用 fallback
            if not _loc_xueke and not _loc_shuke:
                if (m := re.search(r"上課地[址點]\s*[:：]\s*([^\n]+)", text)):
                    _loc_xueke = m.group(1).strip()
            # 合併 (Commit 18c: 改用換行格式,跟 CPC 一致,信件會自動轉 <br>)
            if _loc_xueke and _loc_shuke and _loc_xueke != _loc_shuke:
                course["location"] = f"學科:{_loc_xueke}\n術科:{_loc_shuke}"
            elif _loc_xueke:
                course["location"] = _loc_xueke
            elif _loc_shuke:
                course["location"] = _loc_shuke

            # === 時段 → class_time ===
            for _isha_label in ("時段", "上課時間"):
                if course.get("class_time"):
                    break
                _isha_sec = re.search(rf"{_isha_label}\s*[:：]\s*([^\n]+)", text)
                if not _isha_sec:
                    continue
                raw = _isha_sec.group(1).strip()
                _tm = re.search(r"(\d{1,2})[:：]?(\d{2})\s*[-~]\s*(\d{1,2})[:：]?(\d{2})", raw)
                if _tm:
                    h1, m1, h2, m2 = (int(x) for x in _tm.groups())
                    if 0 <= h1 <= 23 and 0 <= h2 <= 23 and 0 <= m1 <= 59 and 0 <= m2 <= 59:
                        course["class_time"] = (
                            f"{'上午' if h1 < 12 else '下午'} {h1}:{m1:02d} - "
                            f"{'上午' if h2 < 12 else '下午'} {h2}:{m2:02d}"
                        )

            # === 報名截止 ===
            if (_isha_dl := re.search(r"報名截止日期\s*[:：]?\s*(\d{3})年(\d{1,2})月(\d{1,2})日", text)):
                y, mo, d = _isha_dl.groups()
                course["deadline"] = f"{int(y) + 1911:04d}-{int(mo):02d}-{int(d):02d}"

            # === class_time fallback (v7 新增 — 按 班別 給預設,或顯示「未定」) ===
            if not course.get("class_time"):
                ct = course.get("class_type", "")
                if "夜" in ct:
                    course["class_time"] = "晚上 18:30 - 21:30"
                elif "假日" in ct or "日間" in ct:
                    course["class_time"] = "上午 9:00 - 下午 17:00"
                else:
                    course["class_time"] = "未定,待協會正式通知"

            # === category fallback (v7 新增) ===
            if not course.get("category"):
                # 用 hours 猜:>=12 hr 通常是初訓
                try:
                    if float(course.get("hours") or 0) >= 12:
                        course["category"] = "初訓"
                    else:
                        course["category"] = "複訓"
                except (ValueError, TypeError):
                    course["category"] = "—"

        except Exception as e:
            print(f"  [ISHA] detail {course.get('code', '?')} 失敗: {e}")

    # -----------------------------------------------------
    # 主流程
    # -----------------------------------------------------
    @classmethod
    def scrape(cls, fetch_details=True):
        from concurrent.futures import ThreadPoolExecutor

        cls._debug_counter = 0
        cls._progress = {"stage": "list", "current": 0, "total": 0, "message": "ISHA 載入..."}
        session = cls._make_session()

        try:
            r = session.get(cls.list_url, timeout=20)
            r.encoding = "utf-8"
            soup = BeautifulSoup(r.text, "html.parser")
        except Exception as e:
            print(f"  [ISHA] 初始 GET 失敗: {e}")
            cls._progress = {"stage": "idle", "current": 0, "total": 0, "message": ""}
            return []

        max_page_seen = 1
        for tag in soup.find_all(["a", "input"]):
            onclick = (tag.get("href", "") + " " + (tag.get("onclick") or ""))
            pm = re.search(r"__doPostBack\('[^']*\$BT_(\d+)'", onclick)
            if pm:
                n = int(pm.group(1))
                if n > max_page_seen:
                    max_page_seen = n
        print(f"  [ISHA] 初始頁能看到的最大頁碼: {max_page_seen}")
        cls._progress["total"] = max(max_page_seen, 60)

        all_rows = []
        seen_ids = set()

        def _absorb(page_num, page_soup):
            page_rows = cls._parse_list(page_soup)
            new = 0
            for c in page_rows:
                if c["id"] not in seen_ids:
                    seen_ids.add(c["id"])
                    all_rows.append(c)
                    new += 1
            print(f"  [ISHA] 第 {page_num} 頁: 解析 {len(page_rows)} 筆 (新加 {new})")
            return new

        _absorb(1, soup)

        current_page = 1
        max_iterations = 100
        consecutive_empty = 0

        while current_page < max_iterations:
            next_target, next_page = cls._find_next_page_target(soup, current_page)
            if not next_target:
                print(f"  [ISHA] 第 {current_page} 頁找不到下一頁,結束分頁")
                break

            try:
                vs = cls._vs(soup)
                form = {
                    "__EVENTTARGET": next_target,
                    "__EVENTARGUMENT": "",
                    "__VIEWSTATE": vs["__VIEWSTATE"],
                    "__VIEWSTATEGENERATOR": vs["__VIEWSTATEGENERATOR"],
                    "__EVENTVALIDATION": vs["__EVENTVALIDATION"],
                }
                r = session.post(cls.list_url, data=form, timeout=25)
                r.encoding = "utf-8"
                soup = BeautifulSoup(r.text, "html.parser")
                current_page = next_page
                cls._progress["current"] = current_page
                cls._progress["message"] = f"ISHA 列表 {current_page}/{cls._progress['total']}..."
                new = _absorb(current_page, soup)
                if new == 0:
                    consecutive_empty += 1
                    if consecutive_empty >= 3:
                        print(f"  [ISHA] 連續 3 頁無新資料,停止分頁")
                        break
                else:
                    consecutive_empty = 0
            except Exception as e:
                print(f"  [ISHA] 第 {current_page + 1} 頁失敗: {e}")
                break

        print(f"  [ISHA] 列表抓完,共 {len(all_rows)} 筆")

        if fetch_details and all_rows:
            # === Commit 18c: cache 查詢(跳過已抓過的 detail)===
            cache = getattr(cls, "_cache", {}) or {}
            force_refresh = getattr(cls, "_force_refresh", False)

            # 要從 cache 複製的欄位(都是 detail 頁才有的欄位)
            _isha_cache_fields = ["branch", "start_date", "end_date", "hours", "category",
                                  "fee", "location", "class_time", "deadline"]

            targets_to_fetch = []
            cache_hit_count = 0
            for course in all_rows:
                cached = cache.get(course["id"])
                if not force_refresh and cached and cached.get("location"):
                    # Cache hit:複製 detail 頁抓的欄位,跳過 detail 抓取
                    for _field in _isha_cache_fields:
                        if cached.get(_field):
                            course[_field] = cached[_field]
                    cache_hit_count += 1
                else:
                    # Cache miss:加入待抓清單
                    targets_to_fetch.append(course)

            print(f"  [ISHA] Cache hit: {cache_hit_count} 筆(跳過 detail),需抓 detail: {len(targets_to_fetch)} 筆")
            # ==================================================

            cls._progress = {
                "stage": "details", "current": 0, "total": len(targets_to_fetch),
                "message": f"ISHA 抓詳細 0/{len(targets_to_fetch)}...",
            }

            def _isha_do(c):
                cls._fetch_detail(session, c)
                return c

            with ThreadPoolExecutor(max_workers=2) as pool:
                for idx, _ in enumerate(pool.map(_isha_do, targets_to_fetch)):
                    cls._progress["current"] = idx + 1
                    cls._progress["message"] = f"ISHA 抓詳細 {idx+1}/{len(targets_to_fetch)}..."

        filtered = [c for c in all_rows if c.get("branch") in ("台北", "新北", "桃園", "中壢", "新竹")]
        non_north_count = len(all_rows) - len(filtered)
        empty_count = sum(1 for c in all_rows if not c.get("branch"))

        print(f"  [ISHA] 站別範例 (前 5 筆):")
        for c in all_rows[:5]:
            print(f"    code={c['code']} raw_station={c.get('_raw_station', '(無)')!r} → branch={c.get('branch', '')!r}")
        print(f"  [ISHA] 北區 {len(filtered)} 筆 / 非北區或無 {non_north_count} 筆 / 空 {empty_count} 筆")

        for c in filtered:
            c.pop("_raw_station", None)

        cls._progress = {"stage": "idle", "current": 0, "total": 0, "message": ""}
        print(f"  [ISHA] 完成,共 {len(filtered)} 堂課(北區 5 站)")
        return filtered


# =========================================================
# 解析器 #4:中國勞工安全衛生管理學會 (CSHM)
# =========================================================
class CSHMScraper:
    name = "中國勞工安全衛生管理學會"
    code = "cshm"
    desc = "台北・桃園・中壢 三個分會"
    base_url = "https://www.cshm.org.tw"
    # 只抓台北/桃園/中壢三區
    target_areas = {
        "台北": "A01",
        "桃園": "A03",
        "中壢": "A04",
    }

    _progress = {"stage": "idle", "current": 0, "total": 0, "message": ""}

    @classmethod
    def get_progress(cls):
        return dict(cls._progress)

    @classmethod
    def _roc_to_west(cls, roc_str):
        """民國年 '115年6月3日' → '2026-06-03'"""
        m = re.search(r"(\d{2,3})年(\d{1,2})月(\d{1,2})日", roc_str)
        if not m:
            return ""
        y, mo, d = m.groups()
        return f"{int(y)+1911:04d}-{int(mo):02d}-{int(d):02d}"

    @classmethod
    def _parse_list_page(cls, html, branch):
        """解析列表頁，回傳課程清單"""
        soup = BeautifulSoup(html, "html.parser")
        courses = []
        seen = set()

        for table in soup.find_all("table"):
            for tr in table.find_all("tr"):
                td_list = tr.find_all("td")
                if not td_list:
                    continue
                full_text = " ".join(td.get_text(" ", strip=True) for td in td_list)
                if not full_text.strip():
                    continue

                # 狀態
                status = ""
                for st in ("確定開課", "額滿候補", "預定開課", "請先報名"):
                    if st in full_text:
                        status = st
                        break
                if not status:
                    continue

                # 課程名稱（第二個 td 通常含名稱）
                name = ""
                for td in td_list:
                    t = td.get_text(" ", strip=True)
                    # 跳過純狀態文字
                    if t in ("確定開課", "額滿候補", "預定開課", "請先報名"):
                        continue
                    if len(t) > 4 and "報名" not in t[:2] and "詳細" not in t[:2]:
                        name = t
                        # 去掉狀態字串
                        for st in ("確定開課", "額滿候補", "預定開課", "請先報名"):
                            name = name.replace(st, "").strip()
                        break

                if not name:
                    continue

                # 班別
                class_type = "日間班"
                for ct in ("日間", "夜間", "假日", "綜合", "電洽"):
                    if f"【{ct}】" in full_text:
                        if ct == "夜間":
                            class_type = "夜間班"
                        elif ct == "假日":
                            class_type = "假日班"
                        else:
                            class_type = f"{ct}班"
                        break

                # 開課日（民國年）
                start_date = ""
                dm = re.search(r"(\d{2,3})年(\d{1,2})月(\d{1,2})日", full_text)
                if dm:
                    start_date = cls._roc_to_west(dm.group(0))

                # 報名連結 + 詳細資料連結
                register_url = ""
                detail_url = ""
                for a in tr.find_all("a", href=True):
                    href = a["href"]
                    if not href.startswith("http"):
                        href = cls.base_url + href
                    if "Regist/Regist" in href and not register_url:
                        register_url = href
                    if "BrochureInfo" in href and not detail_url:
                        detail_url = href

                if not register_url and not detail_url:
                    continue

                # ID 用報名連結末段數字
                cid_m = re.search(r"/(\d+)$", register_url or detail_url)
                cid = cid_m.group(1) if cid_m else re.sub(r"\W", "", name)[:20]
                course_id = f"cshm-{branch}-{cid}"

                if course_id in seen:
                    continue
                seen.add(course_id)

                # category
                if "回訓" in name or "在職" in name:
                    category = "複訓"
                elif "初訓" in name or any(k in name for k in ("作業主管", "操作人員", "管理師", "管理員", "業務主管")):
                    category = "初訓"
                else:
                    category = ""

                nationality = "本國籍"

                courses.append({
                    "id": course_id,
                    "institute": cls.name,
                    "branch": branch,
                    "name": name,
                    "category": category,
                    "nationality": nationality,
                    "start_date": start_date,
                    "end_date": "",
                    "class_type": class_type,
                    "class_time": "",
                    "hours": "",
                    "fee": "",
                    "status": status,
                    "deadline": "",
                    "register_url": register_url or detail_url,
                    "detail_url": detail_url,
                    "location": "",
                    "source": "cshm",
                })
        return courses

    @classmethod
    def _fetch_detail(cls, session, course):
        """抓詳細頁：地址、時數、費用、上課時間、日期"""
        detail_url = course.get("detail_url", "")
        if not detail_url:
            return
        try:
            r = session.get(detail_url, timeout=20)
            r.encoding = "utf-8"
            soup = BeautifulSoup(r.text, "html.parser")
            text = soup.get_text(separator="\n", strip=True)

            # 地址（地點）— 格式為「地點　桃園市...」（tab/空白分隔）
            m = re.search(r"地點\s+([^\n\t]{5,60})", text)
            if not m:
                m = re.search(r"地點\s*[：:]\s*([^\n]+)", text)
            if m:
                addr = m.group(1).strip()
                # 過濾掉「時段」等非地址內容
                if any(kw in addr for kw in ("時段", "時數", "費用", "地區")):
                    addr = ""
                if addr:
                    course["location"] = addr

            # 時數
            if not course.get("hours"):
                m = re.search(r"時數\s*[：:]?\s*(\d+)", text)
                if m:
                    course["hours"] = m.group(1)

            # 費用（支援「優惠價1300元」「定價:1500元」「費用：1300」等格式）
            if not course.get("fee"):
                m = re.search(r"(?:優惠價?|定價|費用)[：:\s]*(\d[\d,]*)\s*元?", text)
                if not m:
                    m = re.search(r"費用\s*\n\s*(?:優惠)?(\d[\d,]*)", text)
                if m:
                    course["fee"] = m.group(1).replace(",", "")

            # 上課時間（說明欄）
            if not course.get("class_time"):
                m = re.search(r"說明\s*[：:]\s*([^\n]+)", text)
                if m:
                    raw = m.group(1).strip()
                    tm = re.search(r"(\d{4})-(\d{4})", raw)
                    if tm:
                        h1, h2 = int(tm.group(1)[:2]), int(tm.group(2)[:2])
                        m1, m2 = int(tm.group(1)[2:]), int(tm.group(2)[2:])
                        course["class_time"] = (
                            f"{'上午' if h1 < 12 else '下午'} {h1}:{m1:02d} - "
                            f"{'上午' if h2 < 12 else '下午'} {h2}:{m2:02d}"
                        )

            # 開課日（從詳細頁補）
            if not course.get("start_date"):
                dm = re.search(r"日期\s*[：:]\s*(\d{2,3})年(\d{1,2})月(\d{1,2})日", text)
                if dm:
                    course["start_date"] = cls._roc_to_west(dm.group(0).split("：")[-1].split(":")[-1])

            # category fallback
            if not course.get("category"):
                m = re.search(r"時數\s*[：:]?\s*(\d+)", text)
                if m:
                    try:
                        course["category"] = "初訓" if int(m.group(1)) >= 12 else "複訓"
                    except Exception:
                        course["category"] = "複訓"

            # class_time fallback
            if not course.get("class_time"):
                ct = course.get("class_type", "")
                if "夜" in ct:
                    course["class_time"] = "晚上 18:30 - 21:30"
                else:
                    course["class_time"] = "上午 9:00 - 下午 17:00"

        except Exception as e:
            print(f"  [CSHM] detail 失敗 {detail_url}: {e}")

    @classmethod
    def scrape(cls, fetch_details=True):
        from concurrent.futures import ThreadPoolExecutor
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "zh-TW,zh;q=0.9",
        })

        cls._progress = {"stage": "list", "current": 0, "total": len(cls.target_areas), "message": "CSHM 載入列表..."}
        all_courses = []
        seen_ids = set()

        for i, (branch, area_code) in enumerate(cls.target_areas.items()):
            cls._progress["current"] = i + 1
            cls._progress["message"] = f"CSHM 抓取 {branch} 列表..."
            try:
                url = f"{cls.base_url}/Training/AreaIndex/{area_code}"
                r = session.get(url, timeout=20)
                r.encoding = "utf-8"
                courses = cls._parse_list_page(r.text, branch)
                new = 0
                for c in courses:
                    if c["id"] not in seen_ids:
                        seen_ids.add(c["id"])
                        all_courses.append(c)
                        new += 1
                print(f"  [CSHM] {branch}: 找到 {new} 筆")
            except Exception as e:
                print(f"  [CSHM] {branch} 列表失敗: {e}")

        print(f"  [CSHM] 列表抓完，共 {len(all_courses)} 筆")

        if fetch_details and all_courses:
            cache = getattr(cls, "_cache", {}) or {}
            force_refresh = getattr(cls, "_force_refresh", False)
            _cache_fields = ["location", "hours", "fee", "class_time", "start_date", "category"]

            targets = []
            cache_hit = 0
            for course in all_courses:
                cached = cache.get(course["id"])
                if not force_refresh and cached and cached.get("location"):
                    for f in _cache_fields:
                        if cached.get(f):
                            course[f] = cached[f]
                    cache_hit += 1
                else:
                    targets.append(course)

            print(f"  [CSHM] Cache hit: {cache_hit} 筆，需抓 detail: {len(targets)} 筆")
            cls._progress = {"stage": "details", "current": 0, "total": len(targets), "message": f"CSHM 抓詳細 0/{len(targets)}..."}

            def _do(c):
                cls._fetch_detail(session, c)
                return c

            with ThreadPoolExecutor(max_workers=2) as pool:
                for idx, _ in enumerate(pool.map(_do, targets)):
                    cls._progress["current"] = idx + 1
                    cls._progress["message"] = f"CSHM 抓詳細 {idx+1}/{len(targets)}..."

        # 移除 detail_url（不需要存入 JSON）
        for c in all_courses:
            c.pop("detail_url", None)

        cls._progress = {"stage": "idle", "current": 0, "total": 0, "message": ""}
        print(f"  [CSHM] 完成，共 {len(all_courses)} 堂課")
        return all_courses


class TeteScraper:
    name = "台灣能量輻射防護偵測有限公司"
    code = "tete"
    desc = "北部地區（台北・中壢），輻射類課程"
    _progress = {"stage": "idle", "current": 0, "total": 0, "message": ""}

    @classmethod
    def get_progress(cls):
        return cls._progress.copy()

    @classmethod
    def scrape(cls, fetch_details=True):
        import requests
        from bs4 import BeautifulSoup
        import re

        base_url = "https://www.tete.url.tw"
        courses = []

        # 列表頁設定：(課程名稱, 小時數, 列表頁URL)
        list_pages = [
            ("3小時輻射繼續教育積分班", 3, f"{base_url}/custom_cg54564.html"),
            ("18小時輻射操作人員訓練班", 18, f"{base_url}/custom_cg54566.html"),
            ("36小時輻射操作人員訓練班", 36, f"{base_url}/custom_cg54991.html"),
        ]

        cls._progress["stage"] = "listing"
        cls._progress["message"] = "抓取課程列表中..."
        cls._progress["total"] = len(list_pages)
        cls._progress["current"] = 0

        detail_links = []  # [(課程名, 小時數, 詳細頁URL)]

        for course_name, hours, list_url in list_pages:
            try:
                r = requests.get(list_url, timeout=15)
                r.encoding = "utf-8"
                soup = BeautifulSoup(r.text, "html.parser")
                for h3 in soup.select("h3 a[href]"):
                    href = h3["href"]
                    if not href.startswith("http"):
                        href = base_url + "/" + href.lstrip("/")
                    detail_links.append((course_name, hours, href, h3.get_text(strip=True)))
            except Exception as e:
                print(f"[TeteScraper] 列表頁失敗 {list_url}: {e}")
            cls._progress["current"] += 1

        cls._progress["stage"] = "details"
        cls._progress["total"] = len(detail_links)
        cls._progress["current"] = 0
        cls._progress["message"] = f"抓取課程詳細頁（共 {len(detail_links)} 筆）..."

        for course_name, hours, detail_url, title_text in detail_links:
            try:
                r = requests.get(detail_url, timeout=15)
                r.encoding = "utf-8"
                soup = BeautifulSoup(r.text, "html.parser")
                full_text = soup.get_text(separator="\n")

                # 地點（從「上課地點：」後抓第一行地址）
                loc_match = re.search(r'上課地點[：:]\s*\n?\s*(.+)', full_text)
                location = loc_match.group(1).strip() if loc_match else ""

                # 分會（從標題括號判斷）
                if "台北" in title_text:
                    branch = "台北"
                elif "中壢" in title_text:
                    branch = "中壢"
                else:
                    branch = "北部"

                # 費用：一般場次 + 全日班（全日班通常為兩倍）
                fee_match = re.search(r'每人\s*(\d+)\s*元', full_text)
                base_fee = int(fee_match.group(1)) if fee_match else 0
                fullday_match = re.search(r'全日班\D*?(\d+)\s*元', full_text)
                fullday_fee = int(fullday_match.group(1)) if fullday_match else base_fee

                # 從報名「班次」區塊抓出所有場次（上午班 / 下午班 / 全日班）
                block_m = re.search(r'班次(.*?)姓名', full_text, re.DOTALL)
                block = block_m.group(1) if block_m else ""
                session_re = re.compile(
                    r'(\d{2,3})年(\d{1,2})月(\d{1,2})日'
                    r'(?:\s*星期[一二三四五六日])?'
                    r'\s*(?:\(([^)]*)\))?'
                    r'\s*(上午班|下午班|全日班|全天班)?'
                )
                sessions = session_re.findall(block)

                # 標題日期（沒抓到班次時的 fallback，含多日範圍）
                date_match = re.search(r'(\d{2,3})年(\d{2})月(\d{2})日', title_text)
                if date_match:
                    t_year = int(date_match.group(1)) + 1911
                    t_month, t_day = date_match.group(2), date_match.group(3)
                    title_start = f"{t_year}-{t_month}-{t_day}"
                else:
                    title_start = ""
                end_match = re.search(r'~(\d{2})日', title_text)
                title_end = f"{t_year}-{t_month}-{end_match.group(1).zfill(2)}" if (end_match and date_match) else title_start

                if not sessions:
                    sessions = [(None, None, None, "", "")]

                course_id = detail_url.split("_")[-1].replace(".html", "")
                seen_sess = set()
                for (sy, sm, sd_, _loc_hint, sess) in sessions:
                    if sy:
                        start_date = f"{int(sy)+1911:04d}-{int(sm):02d}-{int(sd_):02d}"
                        end_date = start_date
                        if title_end != title_start and start_date == title_start:
                            end_date = title_end
                    else:
                        start_date, end_date = title_start, title_end

                    if sess == "全日班":
                        class_time, this_fee = "全日班", fullday_fee
                        this_hours = hours * 2 if hours == 3 else hours
                    elif sess:
                        class_time, this_fee, this_hours = sess, base_fee, hours
                    else:
                        class_time, this_fee, this_hours = "時間以協會通知為準", base_fee, hours

                    dedup_key = f"{start_date}|{sess}"
                    if dedup_key in seen_sess:
                        continue
                    seen_sess.add(dedup_key)

                    sess_tag = {"上午班": "am", "下午班": "pm", "全日班": "full"}.get(sess, "x")
                    courses.append({
                        "institute": cls.name,
                        "id": f"tete_{course_id}_{sess_tag}",
                        "code": cls.code,
                        "name": course_name,
                        "branch": branch,
                        "category": "輻射",
                        "nationality": "本國籍",
                        "start_date": start_date,
                        "end_date": end_date,
                        "class_type": "",
                        "class_time": class_time,
                        "hours": this_hours,
                        "fee": this_fee,
                        "status": "確定開班",
                        "register_url": detail_url,
                        "location": location,
                        "source": detail_url,
                    })
            except Exception as e:
                print(f"[TeteScraper] 詳細頁失敗 {detail_url}: {e}")
            cls._progress["current"] += 1

        cls._progress["stage"] = "done"
        cls._progress["message"] = f"完成，共 {len(courses)} 筆"
        return courses


class HwahsingScraper:
    name = "華新輻射防護偵測股份有限公司"
    code = "hwahsing"
    desc = "北部地區（台北・桃園・竹科），輻射類課程"
    base_url = "https://www.hwahsing.com.tw"
    form_url = "https://docs.google.com/forms/d/e/1FAIpQLSeJSgsNOPBMafsUGM256yl5BzeiLKOI0ZxxMetw0YsyycGdOg/viewform"
    _progress = {"stage": "idle", "current": 0, "total": 0, "message": ""}
    NORTH_KEYWORDS = ("台北", "桃園", "竹科", "新竹")

    @classmethod
    def get_progress(cls):
        return cls._progress.copy()

    @classmethod
    def _roc_to_west(cls, s):
        m = re.search(r"(\d{2,3})年\s*(\d{1,2})月\s*(\d{1,2})日", s)
        if not m:
            return ""
        y, mo, d = m.groups()
        return f"{int(y)+1911:04d}-{int(mo):02d}-{int(d):02d}"

    @classmethod
    def _md_to_west(cls, s):
        m = re.search(r"(\d{1,2})/(\d{1,2})", s)
        if not m:
            return ""
        return f"{now_tw().year:04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"

    @classmethod
    def _is_north(cls, text):
        return any(k in text for k in cls.NORTH_KEYWORDS)

    @classmethod
    def _branch_of(cls, text):
        for kw, br in (("台北", "台北"), ("桃園", "桃園"), ("竹科", "新竹"), ("新竹", "新竹"),
                       ("中科", "中科"), ("台中", "台中"), ("南科", "南科"), ("台南", "台南"), ("高雄", "高雄")):
            if kw in text:
                return br
        return text

    @classmethod
    def _mk(cls, **kw):
        base = {"institute": cls.name, "code": cls.code, "category": "輻射",
                "nationality": "本國籍", "register_url": cls.form_url, "source": cls.code}
        base.update(kw)
        return base

    @classmethod
    def _parse_continuing(cls, html):
        soup = BeautifulSoup(html, "html.parser")
        courses = []
        for table in soup.select("table.schedule-table"):
            for tr in (table.select("tbody tr") or table.find_all("tr")):
                tds = tr.find_all("td")
                if len(tds) < 4:
                    continue
                batch = tds[0].get_text(strip=True)
                if not re.match(r"第[A-Za-z]?\d+梯", batch):
                    continue
                start_date = cls._roc_to_west(tds[1].get_text(strip=True))
                loc_span = tds[2].find("span", class_="address-tip")
                loc_text = tds[2].get_text(strip=True)
                full_addr = loc_span.get("data-address", "").strip() if loc_span else ""
                if not cls._is_north(loc_text + " " + full_addr):
                    continue
                status = "已結訓" if "結訓" in tds[3].get_text(strip=True) else "確定開班"
                class_type = "假日班" if re.match(r"第A\d+梯", batch) else "平日班"
                branch = cls._branch_of(loc_text)
                for sess, sname, sh, sf, st in [
                    ("am", "3小時輻防繼續教育(換照積分)", 3, 1000, "上午 09:00-12:00"),
                    ("pm", "3小時輻防繼續教育(換照積分)", 3, 1000, "下午 13:00-16:00"),
                    ("full", "6小時輻防繼續教育(換照積分)", 6, 1700, "全天 09:00-16:00"),
                ]:
                    courses.append(cls._mk(
                        id=f"hwahsing_ce_{start_date}_{class_type}_{sess}",
                        name=sname, branch=branch, class_type=class_type, class_time=st,
                        start_date=start_date, end_date=start_date, hours=sh, fee=sf,
                        status=status, location=full_addr or loc_text))
        return courses

    @classmethod
    def _parse_18h(cls, html):
        soup = BeautifulSoup(html, "html.parser")
        courses = []
        for card in soup.select(".city-card"):
            title_el = card.select_one(".city-title")
            venue_el = card.select_one(".venue-box")
            city_title = title_el.get_text(strip=True) if title_el else ""
            if not cls._is_north(city_title):
                continue
            branch = cls._branch_of(city_title)
            venue = venue_el.get_text(" ", strip=True) if venue_el else ""
            table = card.select_one("table.schedule-table")
            if not table:
                continue
            for tr in (table.select("tbody tr") or table.find_all("tr")):
                tds = tr.find_all("td")
                if len(tds) < 4:
                    continue
                type_text = tds[0].get_text(strip=True)
                if "平日" not in type_text and "假日" not in type_text:
                    continue
                class_type = "假日班" if "假日" in type_text else "平日班"
                parts = [p for p in re.split(r"[、,]", tds[1].get_text(strip=True)) if p.strip()]
                start_date = cls._md_to_west(parts[0]) if parts else ""
                end_date = cls._md_to_west(parts[-1]) if parts else start_date
                status = "已額滿" if "額滿" in tds[3].get_text(strip=True) else "確定開班"
                courses.append(cls._mk(
                    id=f"hwahsing_18h_{branch}_{start_date}",
                    name="18小時輻射操作人員訓練班(初訓)", branch=branch,
                    class_type=class_type, class_time=tds[2].get_text(strip=True),
                    start_date=start_date, end_date=end_date, hours=18, fee=5500,
                    status=status, location=venue))
        return courses

    @classmethod
    def _parse_36h(cls, html):
        soup = BeautifulSoup(html, "html.parser")
        courses = []
        addr_el = soup.select_one(".location-premium-card .address-box")
        default_addr = addr_el.get_text(" ", strip=True).replace("地址：", "").strip() if addr_el else ""
        for table in soup.select("table.clean-table"):
            for tr in (table.select("tbody tr") or table.find_all("tr")):
                tds = tr.find_all("td")
                if len(tds) < 4 or "梯" not in tds[0].get_text(strip=True):
                    continue
                place = tds[3].get_text(strip=True)
                if not cls._is_north(place):
                    continue
                parts = [p for p in re.split(r"[、,]", tds[1].get_text(strip=True)) if p.strip()]
                start_date = cls._md_to_west(parts[0]) if parts else ""
                end_date = cls._md_to_west(parts[-1]) if parts else start_date
                courses.append(cls._mk(
                    id=f"hwahsing_36h_{cls._branch_of(place)}_{start_date}",
                    name="36小時輻射操作人員訓練班(初訓)", branch=cls._branch_of(place),
                    class_type="平日班", class_time="08:30-18:00",
                    start_date=start_date, end_date=end_date, hours=36, fee=10000,
                    status="確定開班", location=default_addr))
        return courses

    @classmethod
    def scrape(cls, fetch_details=True):
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"}
        pages = [
            ("18小時初訓", f"{cls.base_url}/class01.htm", cls._parse_18h),
            ("繼續教育", f"{cls.base_url}/class02.htm", cls._parse_continuing),
            ("36小時初訓", f"{cls.base_url}/class03.htm", cls._parse_36h),
        ]
        all_courses = []
        cls._progress = {"stage": "list", "current": 0, "total": len(pages), "message": "華新 載入..."}
        for i, (label, url, parser) in enumerate(pages):
            cls._progress["current"] = i + 1
            cls._progress["message"] = f"華新 抓取 {label}..."
            try:
                r = requests.get(url, headers=headers, timeout=20)
                r.encoding = "utf-8"
                rows = parser(r.text)
                all_courses.extend(rows)
                print(f"  [Hwahsing] {label}: {len(rows)} 筆")
            except Exception as e:
                print(f"  [Hwahsing] {label} 失敗: {e}")
        cls._progress = {"stage": "idle", "current": 0, "total": 0, "message": ""}
        print(f"  [Hwahsing] 完成，共 {len(all_courses)} 筆")
        return all_courses


SCRAPERS = {    "ticsha": TichaScraper,
    "cpc": CPCScraper,
    "isha": ISHAScraper,
    "cshm": CSHMScraper,
    "tete": TeteScraper,
}

def load_data():
    conn = _pg_conn()
    if conn:
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT courses, last_updated FROM course_cache WHERE id=1")
            row = cur.fetchone()
            if row and row["courses"]:
                lu = row["last_updated"] or ""
                scraper_updated = {}
                ts = lu
                try:
                    lu_obj = json.loads(lu) if lu and lu.startswith("{") else None
                    if lu_obj:
                        ts = lu_obj.get("ts", "")
                        scraper_updated = lu_obj.get("scraper_updated", {})
                except Exception:
                    pass
                raw = row["courses"]
                courses = raw if isinstance(raw, list) else (raw.get("courses", []) if isinstance(raw, dict) else [])
                return {"courses": courses, "last_updated": ts, "scraper_updated": scraper_updated}
        except Exception as e:
            print(f"[PG] load_data 失敗: {e}")
        finally:
            conn.close()
    # fallback: 本地 JSON
    if DATA_FILE.exists():
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"courses": [], "last_updated": None}


def save_data(data):
    conn = _pg_conn()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO course_cache (id, courses, last_updated)
                VALUES (1, %s::JSONB, %s)
                ON CONFLICT (id) DO UPDATE SET courses=EXCLUDED.courses, last_updated=EXCLUDED.last_updated
            """, (json.dumps(data["courses"], ensure_ascii=False), json.dumps({"ts": data.get("last_updated"), "scraper_updated": data.get("scraper_updated", {})})))
            conn.commit()
            print(f"[PG] 課程資料已存入 Supabase ({len(data['courses'])} 筆)")
        except Exception as e:
            print(f"[PG] save_data 失敗: {e}")
        finally:
            conn.close()
    # 同時存本地備份
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# === Commit 18a: cache 與 cutoff 設定 ===
CUTOFF_DAYS_PAST = 0      # 已開課的就過濾掉(同仁不能選過去的日期)
CUTOFF_DAYS_FUTURE = 180  # 只看未來半年內的課


def _build_cache_by_scraper(courses):
    """把上次的課程資料按 scraper code 分組,變成 {scraper_code: {course_id: course}}。"""
    cache = {}
    for c in courses:
        code = c.get("_scraper_code", "")
        if not code:
            continue
        cache.setdefault(code, {})[c.get("id", "")] = c
    return cache


def _is_in_cutoff_range(course, today=None):
    """檢查開課日是否在 [-CUTOFF_DAYS_PAST, +CUTOFF_DAYS_FUTURE] 範圍內。
    沒有 start_date 的保留(寬鬆),避免漏掉沒抓到日期的課。"""
    from datetime import date as _date, datetime as _dt
    sd = (course.get("start_date") or "").strip()
    if not sd:
        return True
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", sd)
    if not m:
        return True
    try:
        course_date = _dt.strptime(m.group(0), "%Y-%m-%d").date()
        today = today or _date.today()
        delta_days = (course_date - today).days
        return -CUTOFF_DAYS_PAST <= delta_days <= CUTOFF_DAYS_FUTURE
    except Exception:
        return True


def update_courses(codes, force_refresh=False):
    # === Commit 18a: 建立 cache(從上次抓過的資料)===
    old_data = load_data()
    if force_refresh:
        cache_by_scraper = {}
        print("⚡ 強制全抓模式:忽略 cache,所有課程都會重抓 detail")
    else:
        cache_by_scraper = _build_cache_by_scraper(old_data.get("courses", []))
        if cache_by_scraper:
            for ckey, cmap in cache_by_scraper.items():
                print(f"📦 Cache: {ckey} 有 {len(cmap)} 筆舊資料可用(detail 頁可跳過)")
        else:
            print("📦 Cache: 空(第一次跑或容器重啟,所有 detail 都會抓)")
    # ===============================================

    all_courses = [c for c in old_data.get("courses", []) if c.get("_scraper_code") not in codes]
    for code in codes:
        if code in SCRAPERS:
            scraper = SCRAPERS[code]
            # === Commit 18a: 把 cache 給 scraper(透過 class attribute,scraper 內部可選擇使用)===
            scraper._cache = cache_by_scraper.get(code, {})
            scraper._force_refresh = force_refresh
            # =================================================================================
            print(f"\n=== 更新 {scraper.name} ===")
            try:
                new_rows = [{**c, "_scraper_code": code} for c in scraper.scrape()]
                all_courses.extend(new_rows)
                now_str = now_tw().strftime("%Y-%m-%d %H:%M:%S")
                # 增量儲存:每個協會跑完就先存一份,萬一後面爆掉至少保住前面
                data = {"courses": all_courses, "last_updated": now_str}
                # 記錄各協會個別更新時間
                if "scraper_updated" not in data:
                    old = load_data()
                    data["scraper_updated"] = old.get("scraper_updated", {})
                data["scraper_updated"][code] = now_str
                save_data(data)
                print(f"=== {scraper.name} 完成,已存檔(本次 {len(new_rows)} 筆 / 累計 {len(all_courses)} 筆) ===")
            except Exception as e:
                print(f"=== {scraper.name} 失敗: {e} ===")
                import traceback
                traceback.print_exc()

    # === Commit 18a: 課程 cutoff 過濾(只留 -7d ~ +180d)===
    before_filter = len(all_courses)
    all_courses = [c for c in all_courses if _is_in_cutoff_range(c)]
    after_filter = len(all_courses)
    if before_filter != after_filter:
        print(f"📅 Cutoff 過濾:{before_filter} → {after_filter} 筆 (-{CUTOFF_DAYS_PAST}d ~ +{CUTOFF_DAYS_FUTURE}d)")
    # ======================================================

    # 把迴圈裡存好的 scraper_updated 讀回來,避免最後這次 save 把它洗成空的
    _final = load_data()
    data = {"courses": all_courses, "last_updated": now_tw().strftime("%Y-%m-%d %H:%M:%S"), "scraper_updated": _final.get("scraper_updated", {})}
    save_data(data)
    return data


# ==========================================================================
# Flask App
# ==========================================================================
app = Flask(__name__)
# Commit 18e (緊急修復):固定 secret_key,避免 worker 重啟導致使用者被自動登出
# 優先讀環境變數 SECRET_KEY (建議到 Render 設),沒設就用內建備用值
app.secret_key = os.environ.get("SECRET_KEY", "training-tool-fixed-key-2026-jentechhr1-please-rotate-yearly")


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"error": "未登入", "redirect": "/login"}), 401
            return redirect(url_for("login_page"))
        # 更新在線狀態 (用 username + IP 區分)
        uname = session["user"]["username"]
        login_time = session["user"].get("login_time", 0)
        # 檢查是否被強制登出
        with FORCE_LOGOUT_LOCK:
            kick_time = FORCE_LOGOUT.get(uname, 0)
        if login_time < kick_time:
            session.pop("user", None)
            remove_online(uname, request.remote_addr or "?")
            if request.path.startswith("/api/"):
                return jsonify({"error": "已被登出", "redirect": "/login"}), 401
            return redirect(url_for("login_page"))
        update_online(uname, request.remote_addr or "?")
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        force = request.form.get("force", "") == "1"  # 強制踢掉舊登入
        
        user = verify_user(username, password)
        if not user:
            return render_template_string(LOGIN_TEMPLATE, error="帳號或密碼錯誤", show_force=False)
        
        # 檢查該帳號是否已有人在線（任何 IP，包含同 IP 不同視窗）
        my_ip = request.remote_addr or "?"
        existing = []
        with ONLINE_LOCK:
            for key in list(ONLINE_USERS.keys()):
                u, ip = key.split("|", 1) if "|" in key else (key, "?")
                if u == username and ONLINE_USERS[key] > time.time() - 300:
                    existing.append(ip)

        if existing and not force:
            return render_template_string(LOGIN_TEMPLATE,
                error=f"⚠️ 此帳號目前已有人登入（IP: {existing[0]}）。若要強制踢掉並使用本帳號,請按下方「強制登入」",
                show_force=True, _username=username, _password=password)
        
        # 如果是強制登入,踢掉所有其他 IP 的這個帳號
        if force:
            with ONLINE_LOCK:
                for key in list(ONLINE_USERS.keys()):
                    u, ip = key.split("|", 1) if "|" in key else (key, "?")
                    if u == username:
                        del ONLINE_USERS[key]
        
        session["user"] = {
            "username": user["username"],
            "role": user["role"],
            "display_name": user["display_name"],
            "login_time": time.time(),
        }
        update_online(user["username"], my_ip)
        log_activity(user["username"], "login", ip=my_ip)
        return redirect(url_for("index"))
    
    return render_template_string(LOGIN_TEMPLATE, error=None, show_force=False)


@app.route("/logout")
def logout():
    if "user" in session:
        uname = session["user"]["username"]
        login_time = session["user"].get("login_time", time.time())
        duration = int(time.time() - login_time)
        remove_online(uname, request.remote_addr or "?")
        log_activity(uname, "logout", ip=request.remote_addr or "?", duration_seconds=duration)
    session.pop("user", None)
    return redirect(url_for("login_page"))


@app.route("/")
@login_required
def index():
    return render_template_string(HTML_TEMPLATE, user=session["user"])


@app.route("/api/courses")
@login_required
def api_courses():
    return jsonify(load_data())


@app.route("/api/scrapers")
@login_required
def api_scrapers():
    data = load_data()
    scraper_updated = data.get("scraper_updated", {})
    result = []
    for code, cls in SCRAPERS.items():
        result.append({
            "code": code,
            "name": cls.name,
            "desc": getattr(cls, "desc", ""),
            "last_updated": scraper_updated.get(code, ""),
        })
    result.append({
        "code": "hwahsing",
        "name": "華新輻射防護偵測股份有限公司",
        "desc": "北部地區（台北・桃園・竹科），輻射類課程",
        "last_updated": scraper_updated.get("hwahsing", ""),
        "manual": True,
    })
    return jsonify(result)

@app.route("/api/online")
@login_required
def api_online():
    return jsonify({"users": get_online_users(), "count": len(get_online_users())})


@app.route("/admin")
@login_required
def admin_page():
    if session["user"]["role"] != "admin":
        return redirect(url_for("index"))
    return render_template_string(ADMIN_TEMPLATE, user=session["user"])





@app.route("/api/admin/change_password", methods=["POST"])
@login_required
def api_change_password():
    if session["user"]["role"] != "admin":
        return jsonify({"ok": False, "error": "無權限"}), 403
    body = request.get_json() or {}
    target = body.get("username", "").strip()
    new_pw = body.get("new_password", "").strip()
    if not target or not new_pw:
        return jsonify({"ok": False, "error": "請填寫完整"})
    if len(new_pw) < 4:
        return jsonify({"ok": False, "error": "密碼至少 4 個字元"})
    conn = _pg_conn()
    if not conn:
        return jsonify({"ok": False, "error": "資料庫無連線"})
    try:
        cur = conn.cursor()
        cur.execute("UPDATE users SET password=%s WHERE username=%s", (new_pw, target))
        conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})
    finally:
        conn.close()


@app.route("/api/admin/change_display_name", methods=["POST"])
@login_required
def api_change_display_name():
    if session["user"]["role"] != "admin":
        return jsonify({"ok": False, "error": "無權限"}), 403
    body = request.get_json() or {}
    target = body.get("username", "").strip()
    new_name = body.get("display_name", "").strip()
    if not target or not new_name:
        return jsonify({"ok": False, "error": "請填寫完整"})
    conn = _pg_conn()
    if not conn:
        return jsonify({"ok": False, "error": "資料庫無連線"})
    try:
        cur = conn.cursor()
        cur.execute("UPDATE users SET display_name=%s WHERE username=%s", (new_name, target))
        conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})
    finally:
        conn.close()


@app.route("/api/admin/list_users")
@login_required
def api_list_users():
    if session["user"]["role"] != "admin":
        return jsonify({"ok": False, "error": "無權限"}), 403
    conn = _pg_conn()
    if not conn:
        return jsonify({"ok": False, "error": "資料庫無連線"})
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT username, role, display_name FROM users ORDER BY id")
        rows = [dict(r) for r in cur.fetchall()]
        return jsonify({"ok": True, "users": rows})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})
    finally:
        conn.close()


@app.route("/api/admin/add_user", methods=["POST"])
@login_required
def api_add_user():
    if session["user"]["role"] != "admin":
        return jsonify({"ok": False, "error": "無權限"}), 403
    body = request.get_json() or {}
    username = body.get("username", "").strip()
    password = body.get("password", "").strip()
    display_name = body.get("display_name", "").strip()
    if not username or not password or not display_name:
        return jsonify({"ok": False, "error": "請填寫完整"})
    if len(password) < 4:
        return jsonify({"ok": False, "error": "密碼至少 4 個字元"})
    conn = _pg_conn()
    if not conn:
        return jsonify({"ok": False, "error": "資料庫無連線"})
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO users (username, password, role, display_name, created_at) VALUES (%s, %s, 'user', %s, %s)",
                    (username, password, display_name, now_tw().isoformat()))
        conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        if "unique" in str(e).lower():
            return jsonify({"ok": False, "error": f"帳號 {username} 已存在"})
        return jsonify({"ok": False, "error": str(e)})
    finally:
        conn.close()


@app.route("/api/admin/delete_user", methods=["POST"])
@login_required
def api_delete_user():
    if session["user"]["role"] != "admin":
        return jsonify({"ok": False, "error": "無權限"}), 403
    body = request.get_json() or {}
    username = body.get("username", "").strip()
    if username == "train0":
        return jsonify({"ok": False, "error": "不能刪除管理員帳號"})
    conn = _pg_conn()
    if not conn:
        return jsonify({"ok": False, "error": "資料庫無連線"})
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM users WHERE username=%s", (username,))
        conn.commit()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})
    finally:
        conn.close()
    with ONLINE_LOCK:
        for key in list(ONLINE_USERS.keys()):
            u, _ = key.split("|", 1) if "|" in key else (key, "?")
            if u == username:
                del ONLINE_USERS[key]
    with FORCE_LOGOUT_LOCK:
        FORCE_LOGOUT[username] = time.time()
    return jsonify({"ok": True})


@app.route("/api/admin/change_role", methods=["POST"])
@login_required
def api_change_role():
    if session["user"]["role"] != "admin":
        return jsonify({"ok": False, "error": "無權限"}), 403
    body = request.get_json() or {}
    target = body.get("username", "").strip()
    new_role = body.get("role", "").strip()
    if new_role not in ("admin", "user"):
        return jsonify({"ok": False, "error": "角色參數錯誤"})
    if not target:
        return jsonify({"ok": False, "error": "請指定帳號"})
    if target == "train0" and new_role != "admin":
        return jsonify({"ok": False, "error": "不能變更主管理員 train0 的權限"})
    conn = _pg_conn()
    if not conn:
        return jsonify({"ok": False, "error": "資料庫無連線"})
    try:
        cur = conn.cursor()
        if new_role == "user":
            cur.execute("SELECT COUNT(*) FROM users WHERE role='admin'")
            if (cur.fetchone()[0] or 0) <= 1:
                return jsonify({"ok": False, "error": "至少需保留一位管理員"})
        cur.execute("UPDATE users SET role=%s WHERE username=%s", (new_role, target))
        conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})
    finally:
        conn.close()


@app.route("/api/admin/force_logout", methods=["POST"])
@login_required
def api_force_logout():
    if session["user"]["role"] != "admin":
        return jsonify({"ok": False, "error": "無權限"}), 403
    body = request.get_json() or {}
    username = body.get("username", "").strip()
    with ONLINE_LOCK:
        for key in list(ONLINE_USERS.keys()):
            u, _ = key.split("|", 1) if "|" in key else (key, "?")
            if u == username:
                del ONLINE_USERS[key]
    return jsonify({"ok": True})


@app.route("/api/admin/stats")
@login_required
def api_admin_stats():
    if session["user"]["role"] != "admin":
        return jsonify({"ok": False, "error": "無權限"}), 403
    period = request.args.get("period", "week")  # today / week / month
    conn = _pg_conn()
    if not conn:
        return jsonify({"ok": False, "error": "資料庫無連線"})
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        until = "9999-12-31 23:59:59"
        start = request.args.get("start", "").strip()
        end = request.args.get("end", "").strip()
        if period == "custom" and start:
            since = start + " 00:00:00"
            until = (end or start) + " 23:59:59"
        elif period == "today":
            since = now_tw().strftime("%Y-%m-%d") + " 00:00:00"
        elif period == "month":
            since = (now_tw().replace(day=1)).strftime("%Y-%m-%d") + " 00:00:00"
        else:
            from datetime import timedelta
            since = (now_tw() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")

        # 登入次數
        cur.execute("SELECT username, COUNT(*) as cnt FROM activity_log WHERE action='login' AND timestamp >= %s AND timestamp <= %s GROUP BY username ORDER BY cnt DESC", (since, until))
        login_counts = [dict(r) for r in cur.fetchall()]

        # 使用時長 (logout duration_seconds 加總)
        cur.execute("SELECT username, SUM(duration_seconds) as total_sec FROM activity_log WHERE action='logout' AND timestamp >= %s AND timestamp <= %s GROUP BY username ORDER BY total_sec DESC", (since, until))
        durations = [dict(r) for r in cur.fetchall()]

# 平均每月使用時長 (全部時間，不受區間影響)
        cur.execute("SELECT username, SUM(duration_seconds) as total_sec, COUNT(DISTINCT LEFT(timestamp,7)) as months FROM activity_log WHERE action='logout' AND duration_seconds IS NOT NULL GROUP BY username")
        monthly_avg = []
        for _mr in cur.fetchall():
            _mr = dict(_mr)
            _months = _mr.get("months") or 1
            _total = _mr.get("total_sec") or 0
            _mr["avg_min"] = round(_total / _months / 60)
            monthly_avg.append(_mr)
        monthly_avg.sort(key=lambda x: x["avg_min"], reverse=True)

        # 各協會更新次數
        cur.execute("SELECT username, scraper_code, COUNT(*) as cnt FROM activity_log WHERE action='update_scraper' AND timestamp >= %s AND timestamp <= %s GROUP BY username, scraper_code ORDER BY cnt DESC", (since, until))
        scraper_counts = [dict(r) for r in cur.fetchall()]

        # 信件產出
        cur.execute("SELECT username, COUNT(*) as cnt, SUM(course_count) as total_courses FROM activity_log WHERE action='generate_email' AND timestamp >= %s AND timestamp <= %s GROUP BY username ORDER BY cnt DESC", (since, until))
        email_counts = [dict(r) for r in cur.fetchall()]

        # 熱門課程 + 協會分布 + 課程類型分布 (course_ids JSON 展開)
        id_to_course = {c.get("id"): c for c in load_data().get("courses", [])}
        cur.execute("SELECT course_ids FROM activity_log WHERE action='generate_email' AND timestamp >= %s AND timestamp <= %s AND course_ids IS NOT NULL", (since, until))
        course_counter = {}
        institute_counter = {}
        category_counter = {}
        for row in cur.fetchall():
            try:
                ids = json.loads(row["course_ids"])
                for cid in ids:
                    course_counter[cid] = course_counter.get(cid, 0) + 1
                    c = id_to_course.get(cid)
                    if c:
                        inst = c.get("institute") or "其他"
                        cat = c.get("category") or "其他"
                        institute_counter[inst] = institute_counter.get(inst, 0) + 1
                        category_counter[cat] = category_counter.get(cat, 0) + 1
            except Exception:
                pass
        top_courses = sorted(course_counter.items(), key=lambda x: x[1], reverse=True)[:10]
        institute_dist = sorted(institute_counter.items(), key=lambda x: x[1], reverse=True)
        category_dist = sorted(category_counter.items(), key=lambda x: x[1], reverse=True)

        # 最近活動記錄 (最新 20 筆)
        cur.execute("SELECT username, action, timestamp, scraper_code, course_count, email_subject FROM activity_log ORDER BY timestamp DESC LIMIT 20", )
        recent = [dict(r) for r in cur.fetchall()]

        return jsonify({
            "ok": True, "period": period,
            "login_counts": login_counts, "durations": durations,
            "monthly_avg": monthly_avg,
            "scraper_counts": scraper_counts,
            "email_counts": email_counts,
            "top_courses": top_courses,
            "institute_dist": institute_dist,
            "category_dist": category_dist,
            "recent": recent,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})
    finally:
        conn.close()


@app.route("/api/admin/import_hwahsing", methods=["POST"])
@login_required
def api_import_hwahsing():
    if session["user"]["role"] != "admin":
        return jsonify({"ok": False, "error": "無權限"}), 403
    body = request.get_json() or {}
    sources = [
        (body.get("htmlce", ""), "hwahsing_ce_", HwahsingScraper._parse_continuing),
        (body.get("html18", ""), "hwahsing_18h_", HwahsingScraper._parse_18h),
        (body.get("html36", ""), "hwahsing_36h_", HwahsingScraper._parse_36h),
    ]
    if not any((s[0] or "").strip() for s in sources):
        return jsonify({"ok": False, "error": "請至少貼上一頁的原始碼"})
    try:
        data = load_data()
        courses = data.get("courses", [])
        total_new = 0
        for html, prefix, parser in sources:
            html = (html or "").strip()
            if not html:
                continue
            new_rows = parser(html)
            courses = [c for c in courses if not c.get("id", "").startswith(prefix)]
            for c in new_rows:
                c["_scraper_code"] = "hwahsing"
                if _is_in_cutoff_range(c):
                    courses.append(c)
                    total_new += 1
        now_str = now_tw().strftime("%Y-%m-%d %H:%M:%S")
        su = data.get("scraper_updated", {})
        su["hwahsing"] = now_str
        save_data({"courses": courses, "last_updated": now_str, "scraper_updated": su})
        log_activity(session["user"]["username"], "import_hwahsing",
                     ip=request.remote_addr or "?", course_count=total_new)
        return jsonify({"ok": True, "count": total_new})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/admin/online_detail")
@login_required
def api_online_detail():
    if session["user"]["role"] != "admin":
        return jsonify({"ok": False, "error": "無權限"}), 403
    with ONLINE_LOCK:
        cutoff = time.time() - 300
        result = []
        for key, ts in ONLINE_USERS.items():
            if ts > cutoff:
                u, ip = key.split("|", 1) if "|" in key else (key, "?")
                mins_ago = int((time.time() - ts) / 60)
                result.append({"username": u, "ip": ip, "mins_ago": mins_ago})
    return jsonify({"ok": True, "online": result})


@app.route("/api/fetch_location", methods=["POST"])
@login_required
def api_fetch_location():
    """抓取單一課程的詳細上課地點 (在使用者勾選後才抓,不是全抓)"""
    body = request.get_json() or {}
    course = body.get("course", {})
    
    if not course.get("course_id"):
        return jsonify({"location": None, "error": "無 course_id"})
    
    # 補充 url 欄位 (給 _fetch_detail 用)
    if not course.get("url"):
        course["url"] = f"https://cli.ticsha.org.tw/course_list"
    
    location = TichaScraper._fetch_detail(course)
    return jsonify({"location": location or ""})


@app.route("/api/update_progress")
@login_required
def api_update_progress():
    """查詢更新進度,給前端 polling"""
    return jsonify(TichaScraper.get_progress())


@app.route("/api/update", methods=["POST"])
@login_required
def api_update():
    global IS_UPDATING, IS_UPDATING_BY
    with IS_UPDATING_LOCK:
        if IS_UPDATING:
            return jsonify({"ok": False, "error": f"目前 {IS_UPDATING_BY} 正在更新資料，請稍後再試"}), 423
        IS_UPDATING = True
        IS_UPDATING_BY = session["user"]["display_name"]
    try:
        body = request.get_json() or {}
        codes = body.get("scrapers", ["ticsha"])
        force_refresh = bool(body.get("force_refresh", False))
        data = update_courses(codes, force_refresh=force_refresh)
        for code in codes:
            log_activity(session["user"]["username"], "update_scraper",
                        ip=request.remote_addr or "?", scraper_code=code)
        return jsonify({"ok": True, "count": len(data["courses"]), "last_updated": data["last_updated"]})
    finally:
        with IS_UPDATING_LOCK:
            IS_UPDATING = False
            IS_UPDATING_BY = ""


def _format_date_with_weekday(date_str):
    """加上星期。'2026-06-05' → '2026-06-05(四)'。已有 (X) 結尾的保留原樣。"""
    if not date_str:
        return ""
    m = re.match(r"(\d{4}-\d{2}-\d{2})", date_str)
    if not m:
        return date_str
    ymd = m.group(1)
    if re.search(r"\([一二三四五六日]\)$", date_str):
        return date_str
    try:
        dt = datetime.strptime(ymd, "%Y-%m-%d")
        weekdays = ["一", "二", "三", "四", "五", "六", "日"]
        return f"{ymd}({weekdays[dt.weekday()]})"
    except Exception:
        return date_str


def _format_hours(h):
    """時數正規化:16.0 → 16,3.5 → 3.5。"""
    s = str(h or "").strip()
    if not s:
        return ""
    try:
        n = float(s)
        return str(int(n)) if n == int(n) else str(n)
    except (ValueError, TypeError):
        return s


def _format_fee(fee):
    """費用顯示:免費類 → 「免費」;空/0 → 「—」;數字 → 「XXX 元」"""
    s = str(fee or "").strip()
    if not s or s == "0":
        return "免費"
    if "免" in s:
        return "免費"
    if s.endswith("元"):
        return s
    return f"{s} 元"


def _format_date_range(course):
    """組合 start_date / end_date → 「2026-06-01(一) 至 2026-06-05(五)」;同一天只顯示開始日。"""
    start = _format_date_with_weekday(course.get("start_date", ""))
    end = _format_date_with_weekday(course.get("end_date", ""))
    if not start:
        return ""
    if not end:
        return start
    s_pure = re.sub(r"\([^)]*\)$", "", start)
    e_pure = re.sub(r"\([^)]*\)$", "", end)
    if s_pure == e_pure:
        return start
    return f"{start} 至 {end}"


@app.route("/api/email", methods=["POST"])
@login_required

def api_email():
    body = request.get_json() or {}
    # 新版:多同仁不同部門
    employees = body.get("employees", [])

    # 向下相容:如果還有人送舊格式 (emp_name + emp_dept) 也能用
    if not employees:
        emp_name = body.get("emp_name", "")
        emp_dept = body.get("emp_dept", "")
        name_list = [n.strip() for n in re.split(r"[,，、\s]+", emp_name) if n.strip()]
        employees = [{"name": n, "dept": emp_dept} for n in name_list]

    # 過濾掉空白名字,若完全沒人就放預設
    employees = [e for e in employees if e.get("name", "").strip()]
    if not employees:
        employees = [{"name": "(同仁姓名)", "dept": "(部門)"}]

    emp_month = body.get("emp_month", "")
    course_name = body.get("course_name", "")
    train_type = body.get("train_type", "複訓")
    selected = body.get("selected_courses", [])
    mode = body.get("mode", "external")  # "external" 給同仁 / "internal" 給後台

    name_display = "、".join(e["name"] for e in employees)
    name_suffix = f"_{name_display}" if name_display and name_display != "(同仁姓名)" else ""
    subject = f"2026/{emp_month} 外訓通知(請回覆可以安排的時段)_{course_name}{name_suffix}"

 # 優先用手動輸入的「欲派訓課程名稱」當關鍵字,沒填才退而求其次用勾選的課程
    ref_name = course_name if course_name else (selected[0]["name"] if selected else "")
    ref_hours = selected[0]["hours"] if selected else "3"
    outline = get_course_outline(ref_name, ref_hours, train_type)

    # 每位同仁一列,各自部門
    emp_rows = "".join(
        f'<tr><td style="border:1px solid #BBB;">{course_name}</td>'
        f'<td style="border:1px solid #BBB;">{e["dept"] or "(未填)"}</td>'
        f'<td style="border:1px solid #BBB;">{e["name"]}</td>'
        f'<td style="border:1px solid #BBB;">{emp_month} 月</td></tr>'
        for e in employees
    )
    
    # 共用開頭
    html_parts = [
        '<div style="font-family:微軟正黑體,sans-serif;font-size:14px;color:#333;line-height:1.7;">',
        '<p>Dear All,</p>',
        f'<p>訓練單位將安排以下同仁外出訓練進行職安證照<b>{train_type}</b>課程,派訓人員名單如下:</p>',
        '<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;border:1px solid #888;margin:10px 0;">',
        '<tr style="background:#4472C4;color:white;font-weight:bold;">',
        '<td style="border:1px solid #BBB;">課程名稱</td><td style="border:1px solid #BBB;">部門</td>',
        '<td style="border:1px solid #BBB;">姓名</td><td style="border:1px solid #BBB;">預訂月份</td></tr>',
        emp_rows,
        '</table>',
        '<p><b>📋 課程介紹:</b></p>',
        '<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;border:1px solid #888;margin:10px 0;width:100%;">',
        f'<tr><td style="background:#E7E6E6;width:100px;border:1px solid #BBB;"><b>法規依據</b></td><td style="border:1px solid #BBB;">{outline["law"].replace(chr(10), "<br>")}</td></tr>',
        f'<tr><td style="background:#E7E6E6;border:1px solid #BBB;"><b>訓練目的</b></td><td style="border:1px solid #BBB;">{outline["purpose"]}</td></tr>',
        f'<tr><td style="background:#E7E6E6;vertical-align:top;border:1px solid #BBB;"><b>課程大綱</b></td><td style="border:1px solid #BBB;">{"<br>".join(outline["outline"])}</td></tr>',
        '</table>',
        '<p>再請回覆可以安排受訓的日期及時段,若以下時段不合適,再請聯繫我們,謝謝!</p>',
        '<p><b>相關課程時段如下:</b></p>',
        '<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;border:1px solid #888;margin:10px 0;">',
    ]
    
    # 合併版:不分同仁/後台,一張完整表;按開課日升冪排序(近的在前)
    selected_sorted = sorted(selected, key=lambda c: c.get("start_date", "") or "9999-99-99")
    html_parts.extend([
        '<tr style="background:#FFF3CD;"><td colspan="8" style="border:1px solid #BBB;color:#C44569;font-weight:700;padding:10px;text-align:center;font-size:13px;">※ 報名連結為訓練單位協助報名使用,請勿自行點擊/報名</td></tr>',
        '<tr style="background:#4472C4;color:white;font-weight:bold;">',
        '<td style="border:1px solid #BBB;">場次</td>',
        '<td style="border:1px solid #BBB;">主辦單位</td>',
        '<td style="border:1px solid #BBB;">日期</td>',
        '<td style="border:1px solid #BBB;">上課時間</td>',
        '<td style="border:1px solid #BBB;">上課地點</td>',
        '<td style="border:1px solid #BBB;">時數</td>',
        '<td style="border:1px solid #BBB;">費用</td>',
        '<td style="border:1px solid #BBB;">報名連結</td></tr>',
    ])
    for i, c in enumerate(selected_sorted, 1):
        reg_link = c.get("register_url", "")
        link_html = f'<a href="{reg_link}" target="_blank">點此報名</a>' if reg_link else "(無連結)"
        html_parts.append(
            f'<tr><td style="text-align:center;border:1px solid #BBB;"><b>{i}</b></td>'
            f'<td style="border:1px solid #BBB;">{c.get("institute","")} ({c.get("branch","")})</td>'
            f'<td style="border:1px solid #BBB;">{_format_date_range(c)}</td>'
            f'<td style="border:1px solid #BBB;">{c.get("class_time","")}</td>'
            f'<td style="border:1px solid #BBB;">{c.get("location","").replace(chr(10), "<br>")}</td>'
            f'<td style="text-align:center;border:1px solid #BBB;">{_format_hours(c.get("hours",""))} 小時</td>'
            f'<td style="text-align:right;border:1px solid #BBB;">{_format_fee(c.get("fee",""))}</td>'
            f'<td style="border:1px solid #BBB;">{link_html}</td></tr>'
        )
    
    html_parts.extend([
        '</table>',
        '<p>敬請回覆,謝謝!</p>',
        '</div>',
    ])
    log_activity(
        session["user"]["username"], "generate_email",
        ip=request.remote_addr or "?",
        course_ids=[c.get("id", "") for c in selected],
        course_count=len(selected),
        email_subject=subject,
    )
    return jsonify({"subject": subject, "html": "".join(html_parts)})


# ==========================================================================
# 後台管理頁
# ==========================================================================
ADMIN_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-TW"><head><meta charset="UTF-8"><title>後台管理</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { 
  font-family: 'Microsoft JhengHei', sans-serif; 
  background: 
    radial-gradient(ellipse at 20% 20%, rgba(147,210,240,0.5) 0%, transparent 50%),
    radial-gradient(ellipse at 80% 80%, rgba(180,220,245,0.4) 0%, transparent 50%),
    linear-gradient(160deg, #DCF0FA 0%, #C8E0F0 50%, #D5EBF5 100%);
  min-height: 100vh; background-attachment: fixed; 
}
.header { background: linear-gradient(120deg, #1a5fa8 0%, #4AAEE0 100%); padding: 16px 28px; display: flex; justify-content: space-between; align-items: center; position: sticky; top: 0; z-index: 100; box-shadow: 0 3px 12px rgba(26,95,168,0.25); }
.header h1 { font-size: 22px; font-weight: 800; color: white; text-shadow: 0 2px 6px rgba(0,0,0,0.3); letter-spacing: 0.5px; }
.back-btn { background: rgba(255,255,255,0.25); border: 1px solid rgba(255,255,255,0.5); color: white; padding: 10px 20px; border-radius: 20px; cursor: pointer; font-family: inherit; font-size: 14px; font-weight: 700; transition: all 0.2s; }
.back-btn:hover { background: rgba(255,255,255,0.38); transform: translateY(-1px); }
.container { max-width: 900px; margin: 30px auto; padding: 0 20px; }
.card { background: rgba(255,255,255,0.65); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); border-radius: 16px; padding: 24px; margin-bottom: 20px; box-shadow: 0 4px 24px rgba(26,95,168,0.12), 0 1px 4px rgba(255,255,255,0.8) inset; border: 1px solid rgba(255,255,255,0.75); }
.card h2 { font-size: 17px; font-weight: 800; color: #1a5fa8; margin-bottom: 16px; padding-bottom: 10px; border-bottom: 1px solid rgba(26,95,168,0.15); }
.user-row { display: flex; align-items: center; gap: 10px; padding: 14px 0; border-bottom: 1px solid #F0F0F0; flex-wrap: wrap; }
.user-row:last-child { border-bottom: none; }
.user-label { font-weight: 600; font-size: 16px; min-width: 90px; }
.user-role { font-size: 13px; background: #E5EEF4; color: #3F72AF; padding: 3px 10px; border-radius: 10px; }
.online-dot { width: 8px; height: 8px; border-radius: 50%; background: #ccc; display: inline-block; margin-right: 4px; }
.online-dot.on { background: #4CAF50; box-shadow: 0 0 4px #4CAF50; }
input[type=text], input[type=password] { 
  padding: 8px 14px; border: 1.5px solid rgba(74,144,217,0.3); 
  border-radius: 20px; font-family: inherit; font-size: 14px; width: 150px;
  background: white; color: #1A202C; transition: all 0.2s;
}
input:focus { outline: none; border-color: #4A90D9; box-shadow: 0 0 0 3px rgba(74,144,217,0.15); }
.btn { padding: 7px 14px; border: none; border-radius: 8px; cursor: pointer; font-family: inherit; font-size: 12px; font-weight: 700; }
.btn-blue { background: #3F72AF; color: white; }
.btn-blue:hover { background: #2C5F9E; }
.btn-red { background: #FFE5E5; color: #C44569; border: 1.5px solid #FFB8B8; }
.btn-red:hover { background: #C44569; color: white; }
.btn-orange { background: #FFF3CD; color: #856404; border: 1.5px solid #F4B860; }
.btn-orange:hover { background: #F4B860; color: white; }
.msg { font-size: 12px; margin-left: 4px; }
.msg.ok { color: #2E7D32; }
.msg.err { color: #C44569; }
.add-form { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; padding: 14px; background: #F8FBFD; border-radius: 10px; margin-top: 14px; }
.add-form input { width: 130px; }
.tab-btn {
  padding: 10px 20px; border: none; border-radius: 20px; cursor: pointer;
  font-family: inherit; font-size: 14px; font-weight: 700;
  background: rgba(255,255,255,0.5); color: #4A7BA7;
  transition: all 0.2s;
}
.tab-btn:hover { background: rgba(255,255,255,0.8); }
.tab-btn.active {
  background: linear-gradient(135deg, #2980d4, #4AAEE0);
  color: white; box-shadow: 0 4px 12px rgba(41,128,212,0.3);
}
</style></head>
<body>
<div class="header">
  <div style="display:flex;flex-direction:column;gap:4px;">
    <div style="font-size:17px;display:flex;align-items:center;gap:8px;">
      <a href="/" style="color:rgba(255,255,255,0.85);font-weight:700;text-decoration:none;text-shadow:0 1px 3px rgba(0,0,0,0.2);">📚 外訓課程整合管理系統</a>
      <span style="color:rgba(255,255,255,0.5);">/</span>
      <span style="color:white;font-weight:700;text-shadow:0 1px 3px rgba(0,0,0,0.2);">⚙️ 後台管理</span>
    </div>
  </div>
  <button class="back-btn" onclick="location.href='/'">← 返回主頁</button>
</div>
<div class="container">

  <!-- tab 導航 -->
  <div style="display:flex;gap:8px;margin-bottom:20px;">
    <button class="tab-btn active" id="tab-stats" onclick="switchTab('stats')">📊 使用統計</button>
    <button class="tab-btn" id="tab-online" onclick="switchTab('online')">🟢 在線狀態</button>
    <button class="tab-btn" id="tab-users" onclick="switchTab('users')">👥 使用者管理</button>
    <button class="tab-btn" id="tab-hwahsing" onclick="switchTab('hwahsing')">📥 華新匯入</button>
  </div>

  <!-- 統計儀表板 -->
  <div id="panel-stats" class="tab-panel">
  <div class="card">
    <h2>📊 使用統計儀表板</h2>
    <div style="display:flex;gap:8px;margin-bottom:16px;">
      <button class="btn btn-blue" onclick="loadStats('today')" id="btnToday">今日</button>
      <button class="btn" onclick="loadStats('week')" id="btnWeek" style="background:#E5EEF4;color:#3F72AF;">本週</button>
      <button class="btn" onclick="loadStats('month')" id="btnMonth" style="background:#E5EEF4;color:#3F72AF;">本月</button>
      <span id="statsNote" style="font-size:12px;color:#999;margin-left:8px;align-self:center;"></span>
    </div>
    <div style="display:flex;gap:6px;align-items:center;margin-bottom:16px;flex-wrap:wrap;background:#F8FBFD;padding:10px 12px;border-radius:10px;">
      <span style="font-size:13px;color:#3F72AF;font-weight:700;">📅 自訂區間</span>
      <input type="date" id="customStart" style="padding:6px 10px;border:1.5px solid rgba(74,144,217,0.3);border-radius:8px;font-family:inherit;font-size:13px;">
      <span style="color:#888;">~</span>
      <input type="date" id="customEnd" style="padding:6px 10px;border:1.5px solid rgba(74,144,217,0.3);border-radius:8px;font-family:inherit;font-size:13px;">
      <button class="btn btn-blue" onclick="loadStats('custom')">查詢</button>
    </div>
    <div id="statsDashboard">載入中</div>
  </div>

  </div><!-- end panel-stats -->

  <!-- 在線狀態 -->
  <div id="panel-online" class="tab-panel" style="display:none;">
  <div class="card">
    <h2>🟢 目前在線狀態 <span id="onlineRefreshBtn" onclick="loadOnline()" style="font-size:12px;font-weight:400;color:#888;cursor:pointer;margin-left:8px;">🔄 刷新</span></h2>
    <div id="onlineList">載入中...</div>
  </div>
  </div><!-- end panel-online -->

  <!-- 使用者管理 -->
  <div id="panel-users" class="tab-panel" style="display:none;">
  <div class="card">
    <h2>👥 使用者管理</h2>
    <div id="userList">載入中...</div>
    <div class="add-form">
      <strong style="font-size:13px;">➕ 新增使用者：</strong>
      <input type="text" id="newUsername" placeholder="帳號 (例: train4)">
      <input type="text" id="newDisplayName" placeholder="顯示名稱">
      <input type="password" id="newPassword" placeholder="密碼">
      <button class="btn btn-blue" onclick="addUser()">新增</button>
      <span class="msg" id="addMsg"></span>
    </div>
  </div>

</div><!-- end panel-users -->

  <!-- 華新匯入 -->
  <div id="panel-hwahsing" class="tab-panel" style="display:none;">
  <div class="card">
    <h2>📥 華新課程匯入（手動貼上）</h2>
    <div style="background:#FFF3CD;border:1.5px solid #F4B860;border-radius:10px;padding:12px 16px;font-size:13px;color:#856404;line-height:1.7;margin-bottom:16px;">
      華新網站會擋海外伺服器，無法自動抓取。請從<b>台灣的瀏覽器</b>打開下面三頁，在每一頁按 <b>Ctrl+U</b>（檢視原始碼）→ 接著 <b>Ctrl+A → Ctrl+C</b> 複製，貼到對應欄位後按「匯入」。<span style="color:#C0392B;font-weight:700;">（貼進去要是一堆 &lt;tag&gt; 程式碼，不是一行網址）</span><br>
      只更新某一頁時，<b>只貼那一頁即可</b>（沒貼的欄位會保留原本資料）。約一個月貼一次。
    </div>
    <div style="margin-bottom:14px;">
      <label style="font-weight:700;">① 繼續教育（3/6小時）<a href="https://www.hwahsing.com.tw/class02.htm" target="_blank" style="font-size:12px;margin-left:6px;">開啟頁面 ↗</a></label>
      <textarea id="hwHtmlCe" placeholder="貼上 class02.htm 整頁原始碼" style="width:100%;height:90px;margin-top:6px;border:1.5px solid rgba(74,144,217,0.3);border-radius:10px;padding:10px;font-family:monospace;font-size:12px;"></textarea>
    </div>
    <div style="margin-bottom:14px;">
      <label style="font-weight:700;">② 18小時初訓 <a href="https://www.hwahsing.com.tw/class01.htm" target="_blank" style="font-size:12px;margin-left:6px;">開啟頁面 ↗</a></label>
      <textarea id="hwHtml18" placeholder="貼上 class01.htm 整頁原始碼" style="width:100%;height:90px;margin-top:6px;border:1.5px solid rgba(74,144,217,0.3);border-radius:10px;padding:10px;font-family:monospace;font-size:12px;"></textarea>
    </div>
    <div style="margin-bottom:16px;">
      <label style="font-weight:700;">③ 36小時初訓 <a href="https://www.hwahsing.com.tw/class03.htm" target="_blank" style="font-size:12px;margin-left:6px;">開啟頁面 ↗</a></label>
      <textarea id="hwHtml36" placeholder="貼上 class03.htm 整頁原始碼（目前多為台中場，北部可能無資料）" style="width:100%;height:90px;margin-top:6px;border:1.5px solid rgba(74,144,217,0.3);border-radius:10px;padding:10px;font-family:monospace;font-size:12px;"></textarea>
    </div>
    <button class="btn btn-blue" onclick="importHwahsing()" style="font-size:14px;padding:10px 24px;">📥 匯入</button>
    <span class="msg" id="hwImportMsg" style="margin-left:10px;"></span>
  </div>
  </div><!-- end panel-hwahsing -->

</div>
<script>
let onlineUsernames = new Set();

function switchTab(tab) {
  document.querySelectorAll('.tab-panel').forEach(p => p.style.display = 'none');
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('panel-' + tab).style.display = 'block';
  document.getElementById('tab-' + tab).classList.add('active');
}

async function loadOnline() {
  const r = await fetch('/api/admin/online_detail');
  const d = await r.json();
  if (!d.ok) return;
  onlineUsernames = new Set(d.online.map(o => o.username));
  if (d.online.length === 0) {
    document.getElementById('onlineList').innerHTML = '<div style="color:#999;padding:8px;">目前沒有在線使用者</div>';
  } else {
    document.getElementById('onlineList').innerHTML = d.online.map(o => `
      <div class="user-row">
        <span class="online-dot on"></span>
        <div class="user-label">${o.username}</div>
        <div style="color:#888;font-size:12px;">IP: ${o.ip} ｜ ${o.mins_ago === 0 ? '剛剛活躍' : o.mins_ago + ' 分鐘前'}</div>
        <button class="btn btn-orange" style="margin-left:auto;" onclick="forceLogout('${o.username}')">⚡ 強制登出</button>
        <span class="msg" id="logoutMsg_${o.username}"></span>
      </div>
    `).join('');
  }
  // 同步更新使用者列表的在線指示燈
  document.querySelectorAll('.online-indicator').forEach(el => {
    const u = el.dataset.username;
    el.className = 'online-dot online-indicator' + (onlineUsernames.has(u) ? ' on' : '');
  });
}

async function forceLogout(username) {
  const msg = document.getElementById('logoutMsg_' + username);
  const r = await fetch('/api/admin/force_logout', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({username})
  });
  const d = await r.json();
  if (d.ok) {
    if (msg) { msg.textContent = '✓ 已登出'; msg.className = 'msg ok'; }
    setTimeout(loadOnline, 1000);
  }
}

async function loadUsers() {
  const r = await fetch('/api/admin/list_users');
  const d = await r.json();
  if (!d.ok) { document.getElementById('userList').textContent = '載入失敗'; return; }
  document.getElementById('userList').innerHTML = d.users.map(u => `
    <div class="user-row">
      <span class="online-dot online-indicator" data-username="${u.username}"></span>
      <div class="user-label">${u.display_name}</div>
      <div class="user-role">${u.role === 'admin' ? '管理員' : '使用者'}</div>
      <div style="color:#888;font-size:12px;">@${u.username}</div>
      <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-left:auto;">
        <input type="text" id="name_${u.username}" placeholder="新顯示名稱" value="${u.display_name}">
        <button class="btn btn-blue" onclick="changeName('${u.username}')">改名稱</button>
        <span class="msg" id="nameMsg_${u.username}"></span>
      </div>
      <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;">
        <input type="password" id="pw_${u.username}" placeholder="新密碼">
        <button class="btn btn-blue" onclick="changePw('${u.username}')">改密碼</button>
        <span class="msg" id="pwMsg_${u.username}"></span>
      </div>
      ${u.username !== 'train0' ? (u.role === 'admin' ? `<button class="btn btn-orange" onclick="changeRole('${u.username}','user','${u.display_name}')">改為一般</button>` : `<button class="btn btn-blue" onclick="changeRole('${u.username}','admin','${u.display_name}')">設為管理員</button>`) : ''}
      ${u.role !== 'admin' ? `<button class="btn btn-red" onclick="deleteUser('${u.username}', '${u.display_name}')">🗑 刪除</button>` : ''}
    </div>
  `).join('');
  // 套用在線狀態 + 角色切換功能
  document.querySelectorAll('.online-indicator').forEach(el => {
    const u = el.dataset.username;
    el.className = 'online-dot online-indicator' + (onlineUsernames.has(u) ? ' on' : '');
  });
}

async function changeRole(username, role, displayName) {
  const verb = role === 'admin' ? '設為管理員' : '改為一般使用者';
  if (!confirm('確定要把「' + displayName + '」' + verb + ' 嗎?\n\n變更後對方需重新登入才會生效。')) return;
  const _r = await fetch('/api/admin/change_role', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({username: username, role: role}) });
  const _d = await _r.json();
  if (_d.ok) { alert('✓ 已變更權限,對方重新登入後生效'); loadUsers(); } else { alert('✗ ' + _d.error); }
}



async function changePw(username) {
  const pw = document.getElementById('pw_' + username).value.trim();
  const msg = document.getElementById('pwMsg_' + username);
  if (!pw) { msg.textContent = '請輸入新密碼'; msg.className = 'msg err'; return; }
  const r = await fetch('/api/admin/change_password', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({username, new_password: pw})
  });
  const d = await r.json();
  msg.textContent = d.ok ? '✓ 已更新' : ('✗ ' + d.error);
  msg.className = 'msg ' + (d.ok ? 'ok' : 'err');
  if (d.ok) document.getElementById('pw_' + username).value = '';
}

async function changeName(username) {
  const name = document.getElementById('name_' + username).value.trim();
  const msg = document.getElementById('nameMsg_' + username);
  if (!name) { msg.textContent = '請輸入名稱'; msg.className = 'msg err'; return; }
  const r = await fetch('/api/admin/change_display_name', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({username, display_name: name})
  });
  const d = await r.json();
  msg.textContent = d.ok ? '✓ 已更新' : ('✗ ' + d.error);
  msg.className = 'msg ' + (d.ok ? 'ok' : 'err');
}

async function deleteUser(username, displayName) {
  if (!confirm(`確定要刪除「${displayName}」(@${username}) 嗎？\n\n此操作無法復原。`)) return;
  const r = await fetch('/api/admin/delete_user', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({username})
  });
  const d = await r.json();
  if (d.ok) {
    alert('✓ 已刪除');
    loadUsers();
    loadOnline();
  } else {
    alert('✗ ' + d.error);
  }
}

async function addUser() {
  const username = document.getElementById('newUsername').value.trim();
  const displayName = document.getElementById('newDisplayName').value.trim();
  const password = document.getElementById('newPassword').value.trim();
  const msg = document.getElementById('addMsg');
  if (!username || !displayName || !password) {
    msg.textContent = '請填寫完整'; msg.className = 'msg err'; return;
  }
  const r = await fetch('/api/admin/add_user', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({username, display_name: displayName, password})
  });
  const d = await r.json();
  msg.textContent = d.ok ? '✓ 已新增' : ('✗ ' + d.error);
  msg.className = 'msg ' + (d.ok ? 'ok' : 'err');
  if (d.ok) {
    document.getElementById('newUsername').value = '';
    document.getElementById('newDisplayName').value = '';
    document.getElementById('newPassword').value = '';
    loadUsers();
  }
}

async function loadStats(period = 'week') {
  ['today','week','month'].forEach(p => {
    const btn = document.getElementById('btn' + p.charAt(0).toUpperCase() + p.slice(1));
    btn.style.background = p === period ? '#3F72AF' : '#E5EEF4';
    btn.style.color = p === period ? 'white' : '#3F72AF';
  });
  document.getElementById('statsNote').textContent = '載入中...';
  let _url = '/api/admin/stats?period=' + period;
  if (period === 'custom') {
    const _s = document.getElementById('customStart').value;
    const _e = document.getElementById('customEnd').value;
    if (!_s) { document.getElementById('statsNote').textContent = '⚠️ 請先選開始日期'; return; }
    _url += '&start=' + _s + '&end=' + _e;
  }
  const r = await fetch(_url);
  const d = await r.json();
  if (!d.ok) {
    document.getElementById('statsDashboard').innerHTML = `<div style="color:#C44569;padding:12px;">⚠️ ${d.error}</div>`;
    document.getElementById('statsNote').textContent = '';
    return;
  }
  const periodLabel = {today:'今日',week:'本週',month:'本月',custom:'自訂區間'}[period];
  document.getElementById('statsNote').textContent = `顯示${periodLabel}資料`;

  const scraperResp = await fetch('/api/scrapers');
  const scraperList = await scraperResp.json();
  const scraperLabel = Object.fromEntries(scraperList.map(s => [s.code, s.name]));

  // 登入次數表
  const loginRows = d.login_counts.map(r => `<tr><td>${r.username}</td><td style="text-align:center;">${r.cnt} 次</td></tr>`).join('') || '<tr><td colspan="2" style="color:#999;text-align:center;">無資料</td></tr>';

  // 使用時長表
  const durRows = d.durations.map(r => {
    const mins = Math.round((r.total_sec || 0) / 60);
    const _h=Math.floor(mins/60),_m=mins%60; return `<tr><td>${r.username}</td><td style="text-align:center;">${_h>0?_h+' 小時 ':''}${_m} 分</td></tr>`;
  }).join('') || '<tr><td colspan="2" style="color:#999;text-align:center;">無資料</td></tr>';

  // 協會更新表
  const scraperRows = d.scraper_counts.map(r => `<tr><td>${r.username}</td><td>${scraperLabel[r.scraper_code]||r.scraper_code}</td><td style="text-align:center;">${r.cnt} 次</td></tr>`).join('') || '<tr><td colspan="3" style="color:#999;text-align:center;">無資料</td></tr>';

  // 信件產出表
  const emailRows = d.email_counts.map(r => `<tr><td>${r.username}</td><td style="text-align:center;">${r.cnt} 封</td><td style="text-align:center;">${r.total_courses||0} 門課</td></tr>`).join('') || '<tr><td colspan="3" style="color:#999;text-align:center;">無資料</td></tr>';

  // 最近活動
  const actionLabel = {login:'登入',logout:'登出',update_scraper:'更新協會',generate_email:'產生信件'};
  const recentRows = d.recent.map(r => {
    let detail = '';
    if (r.scraper_code) detail = scraperLabel[r.scraper_code]||r.scraper_code;
    if (r.course_count) detail = `${r.course_count} 門課`;
    return `<tr><td style="color:#888;font-size:12px;">${r.timestamp}</td><td>${r.username}</td><td>${actionLabel[r.action]||r.action}</td><td style="font-size:12px;color:#666;">${detail}</td></tr>`;
  }).join('') || '<tr><td colspan="4" style="color:#999;text-align:center;">無資料</td></tr>';

  const tableStyle = 'width:100%;border-collapse:collapse;font-size:13px;margin-bottom:4px;border-radius:8px;overflow:hidden;';
  const thStyle = 'background:linear-gradient(135deg,#2980d4,#4AAEE0);color:white;padding:10px 12px;text-align:left;font-weight:700;';
  const tdStyle = 'padding:9px 12px;border-bottom:1px solid rgba(74,144,217,0.1);';

  const instRows = (d.institute_dist && d.institute_dist.length) ? d.institute_dist.map(r => `<tr><td>${r[0]}</td><td style="text-align:center;">${r[1]} 門</td></tr>`).join('') : '<tr><td colspan="2" style="color:#999;text-align:center;">無資料</td></tr>';
  const catRows = (d.category_dist && d.category_dist.length) ? d.category_dist.map(r => `<tr><td>${r[0]}</td><td style="text-align:center;">${r[1]} 門</td></tr>`).join('') : '<tr><td colspan="2" style="color:#999;text-align:center;">無資料</td></tr>';

  document.getElementById('statsDashboard').innerHTML = `
    <div style="margin:0 0 18px;display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;">
      <div style="background:#FBF7EF;border:1px solid #E5D9BF;border-radius:12px;padding:14px 16px;"><div style="font-size:13px;color:#4A7BA7;">🔑 總登入</div><div style="font-size:24px;font-weight:800;color:#1a5fa8;margin-top:4px;">${d.login_counts.reduce((a,x)=>a+(+x.cnt||0),0)} 次</div></div>
      <div style="background:#FBF7EF;border:1px solid #E5D9BF;border-radius:12px;padding:14px 16px;"><div style="font-size:13px;color:#4A7BA7;">⏱ 總使用時長</div><div style="font-size:24px;font-weight:800;color:#1a5fa8;margin-top:4px;">${(()=>{const s=d.durations.reduce((a,x)=>a+(+x.total_sec||0),0);const m=Math.round(s/60);const h=Math.floor(m/60);return (h>0?h+' 小時 ':'')+(m%60)+' 分';})()}</div></div>
      <div style="background:#FBF7EF;border:1px solid #E5D9BF;border-radius:12px;padding:14px 16px;"><div style="font-size:13px;color:#4A7BA7;">📧 信件產出</div><div style="font-size:24px;font-weight:800;color:#1a5fa8;margin-top:4px;">${d.email_counts.reduce((a,x)=>a+(+x.cnt||0),0)} 封</div></div>
      <div style="background:#FBF7EF;border:1px solid #E5D9BF;border-radius:12px;padding:14px 16px;"><div style="font-size:13px;color:#4A7BA7;">📚 派訓課程數</div><div style="font-size:24px;font-weight:800;color:#1a5fa8;margin-top:4px;">${d.email_counts.reduce((a,x)=>a+(+x.total_courses||0),0)} 門</div></div>
    </div>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px;margin-bottom:16px;">
      <div style="background:#FBF7EF;border:1px solid #E5D9BF;border-radius:12px;padding:14px 16px;">
        <div style="font-weight:700;color:#3F72AF;margin-bottom:8px;">🔑 登入次數</div>
        <table style="${tableStyle}">
          <tr><th style="${thStyle}">使用者</th><th style="${thStyle}">次數</th></tr>
          ${loginRows}
        </table>
      </div>
      <div style="background:#FBF7EF;border:1px solid #E5D9BF;border-radius:12px;padding:14px 16px;">
        <div style="font-weight:700;color:#3F72AF;margin-bottom:8px;">⏱ 使用時長</div>
        <table style="${tableStyle}">
          <tr><th style="${thStyle}">使用者</th><th style="${thStyle}">時長</th></tr>
          ${durRows}
        </table>
      </div>
      <div style="background:#FBF7EF;border:1px solid #E5D9BF;border-radius:12px;padding:14px 16px;">
        <div style="font-weight:700;color:#3F72AF;margin-bottom:8px;">🔄 協會更新次數</div>
        <table style="${tableStyle}">
          <tr><th style="${thStyle}">使用者</th><th style="${thStyle}">協會</th><th style="${thStyle}">次數</th></tr>
          ${scraperRows}
        </table>
      </div>
      <div style="background:#FBF7EF;border:1px solid #E5D9BF;border-radius:12px;padding:14px 16px;">
        <div style="font-weight:700;color:#3F72AF;margin-bottom:8px;">📧 信件產出</div>
        <table style="${tableStyle}">
          <tr><th style="${thStyle}">使用者</th><th style="${thStyle}">封數</th><th style="${thStyle}">總課程數</th></tr>
          ${emailRows}
        </table>
      </div>
      <div style="background:#FBF7EF;border:1px solid #E5D9BF;border-radius:12px;padding:14px 16px;">
        <div style="font-weight:700;color:#3F72AF;margin-bottom:8px;">📅 平均每月使用時長</div>
        <table style="${tableStyle}">
          <tr><th style="${thStyle}">使用者</th><th style="${thStyle}">每月平均</th></tr>
          ${(d.monthly_avg && d.monthly_avg.length) ? d.monthly_avg.map(r => { const _mm = r.avg_min||0; const _h = Math.floor(_mm/60), _m = _mm%60; return `<tr><td>${r.username}</td><td style="text-align:center;">${_h>0?_h+' 小時 ':''}${_m} 分 / 月</td></tr>`; }).join('') : `<tr><td colspan="2" style="color:#999;text-align:center;">無資料</td></tr>`}
        </table>
      </div>
    </div>
    
    
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px;margin-bottom:16px;">
      <div style="background:#FBF7EF;border:1px solid #E5D9BF;border-radius:12px;padding:14px 16px;">
        <div style="font-weight:700;color:#3F72AF;margin-bottom:8px;">🏢 協會分布 (派訓信件選用)</div>
        ${(()=>{const rows=d.institute_dist||[];if(!rows.length)return '<div style="color:#999;font-size:13px;padding:6px;">無資料</div>';const max=Math.max.apply(null,rows.map(r=>r[1]))||1;return rows.map(r=>{const pct=Math.round(r[1]/max*100);return '<div style="margin-bottom:10px;"><div style="display:flex;justify-content:space-between;font-size:13px;margin-bottom:3px;"><span>'+r[0]+'</span><span style="color:#888;">'+r[1]+'</span></div><div style="height:8px;background:#E5EEF4;border-radius:4px;"><div style="height:100%;width:'+pct+'%;background:#3F72AF;border-radius:4px;"></div></div></div>';}).join('');})()}
      </div>
      <div style="background:#FBF7EF;border:1px solid #E5D9BF;border-radius:12px;padding:14px 16px;">
        <div style="font-weight:700;color:#3F72AF;margin-bottom:8px;">🏷️ 課程類型分布</div>
        ${(()=>{const rows=d.category_dist||[];if(!rows.length)return '<div style="color:#999;font-size:13px;padding:6px;">無資料</div>';const max=Math.max.apply(null,rows.map(r=>r[1]))||1;return rows.map(r=>{const pct=Math.round(r[1]/max*100);return '<div style="margin-bottom:10px;"><div style="display:flex;justify-content:space-between;font-size:13px;margin-bottom:3px;"><span>'+r[0]+'</span><span style="color:#888;">'+r[1]+'</span></div><div style="height:8px;background:#E5EEF4;border-radius:4px;"><div style="height:100%;width:'+pct+'%;background:#3F72AF;border-radius:4px;"></div></div></div>';}).join('');})()}
      </div>
    </div>
    <div style="background:#FBF7EF;border:1px solid #E5D9BF;border-radius:12px;padding:14px 16px;">
      <div style="font-weight:700;color:#3F72AF;margin-bottom:8px;">🕐 最近活動記錄</div>
      <table style="${tableStyle}">
        <tr><th style="${thStyle}">時間</th><th style="${thStyle}">使用者</th><th style="${thStyle}">動作</th><th style="${thStyle}">詳情</th></tr>
        ${recentRows}
      </table>
    </div>
  `;
}

async function importHwahsing() {
  const msg = document.getElementById('hwImportMsg');
  const htmlce = document.getElementById('hwHtmlCe').value;
  const html18 = document.getElementById('hwHtml18').value;
  const html36 = document.getElementById('hwHtml36').value;
  if (!htmlce.trim() && !html18.trim() && !html36.trim()) {
    msg.textContent = '請至少貼上一頁'; msg.className = 'msg err'; return;
  }
  msg.textContent = '匯入中...'; msg.className = 'msg';
  try {
    const r = await fetch('/api/admin/import_hwahsing', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({htmlce, html18, html36})
    });
    const d = await r.json();
    if (d.ok) { msg.textContent = '✓ 匯入成功，本次 ' + d.count + ' 筆'; msg.className = 'msg ok'; }
    else { msg.textContent = '✗ ' + d.error; msg.className = 'msg err'; }
  } catch (e) { msg.textContent = '✗ ' + e.message; msg.className = 'msg err'; }
}

loadStats('week');
loadOnline();
loadUsers();
setInterval(loadOnline, 30000);
</script>
</body></html>
"""

# ==========================================================================
# 登入頁
# ==========================================================================
LOGIN_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-TW"><head><meta charset="UTF-8"><title>登入 - External Training Management System</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: 'Microsoft JhengHei', sans-serif;
  background: linear-gradient(160deg, #C8DFF5 0%, #B8D4EE 50%, #C5E0F0 100%);
  min-height: 100vh; display: flex; align-items: center; justify-content: center;
  background-attachment: fixed;
}
.login-box {
  background: rgba(255,255,255,0.72);
  backdrop-filter: blur(16px);
  -webkit-backdrop-filter: blur(16px);
  border-radius: 24px; padding: 48px;
  box-shadow: 0 20px 60px rgba(26,95,168,0.2), 0 1px 4px rgba(255,255,255,0.9) inset;
  border: 1px solid rgba(255,255,255,0.85);
  width: 420px;
}
.login-box .brand { color: #1a5fa8; font-size: 13px; font-weight: 700; letter-spacing: 2px; margin-bottom: 6px; opacity: 0.7; }
.login-box h1 { color: #1a5fa8; font-size: 24px; font-weight: 800; margin-bottom: 8px; }
.login-box .sub { color: #4A7BA7; font-size: 13px; margin-bottom: 32px; }
.field { margin-bottom: 18px; }
.field label { display: block; font-size: 13px; color: #1a5fa8; margin-bottom: 8px; font-weight: 700; }
.field input {
  width: 100%; padding: 13px 16px; border: 2px solid rgba(74,144,217,0.25);
  border-radius: 12px; font-size: 15px; font-family: inherit; 
  background: rgba(255,255,255,0.7); color: #1A202C;
  transition: all 0.2s;
}
.field input:focus { outline: none; border-color: #4A90D9; background: white; box-shadow: 0 0 0 3px rgba(74,144,217,0.15); }
button {
  width: 100%; 
  background: linear-gradient(135deg, #2980d4, #4AAEE0); 
  color: white; border: none;
  padding: 14px; font-size: 16px; font-weight: 700; border-radius: 12px;
  cursor: pointer; font-family: inherit; margin-top: 12px;
  box-shadow: 0 4px 16px rgba(41,128,212,0.35);
  transition: all 0.2s;
}
button:hover { transform: translateY(-2px); box-shadow: 0 6px 20px rgba(41,128,212,0.45); }
button.force-btn { 
  background: linear-gradient(135deg, #FFB6B9, #FAE3D9); color: #B73E3E; margin-top: 8px;
  box-shadow: 0 4px 12px rgba(255,182,185,0.4);
}
.error { background: rgba(255,229,229,0.8); color: #C44569; padding: 12px; border-radius: 10px; margin-bottom: 16px; font-size: 13px; line-height: 1.5; border: 1px solid rgba(196,69,105,0.2); }
.footer { text-align: center; margin-top: 22px; font-size: 11px; color: #4A7BA7; line-height: 1.6; opacity: 0.7; }
</style></head>
<body>
<div class="login-box">
  <div class="brand">EXTERNAL TRAINING MANAGEMENT</div>
  <h1>📚 外訓課程整合管理系統</h1>
  <div class="sub">請輸入您的帳號及密碼</div>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}
  <form method="post">
    <div class="field">
      <label>帳號</label>
      <input type="text" name="username" required autofocus value="{{ _username or '' }}">
    </div>
    <div class="field">
      <label>密碼</label>
      <input type="password" name="password" required value="{{ _password or '' }}">
    </div>
    <button type="submit">登入</button>
    {% if show_force %}
    <input type="hidden" name="force" value="1">
    <button type="submit" class="force-btn" onclick="return confirm('確定要強制登入嗎? 對方會被踢出系統')">⚡ 強制登入 (踢掉舊使用者)</button>
    {% endif %}
  </form>
  <div class="footer">
    External Training Management System v7.0<br>
    © 2026 Training Dept. ｜ System internal use only.
  </div>
</div>
</body></html>
"""


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<title>外訓課程整合工具</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --sky: #BEE3F8;       /* 清晨淡藍 */
    --mint: #EBF8FF;      /* 極淡藍白 */
    --gold: #63B3ED;      /* 天空藍強調 */
    --blue: #2B6CB0;      /* 中藍主色 */
    --teal: #4A90D9;      /* 天空藍輔色 */
    --warm: #F0F7FF;      /* 清晨背景 */
    --paper: #EEF5FD;     /* 主背景淡藍白 */
    --ink: #2D3748;       /* 深灰藍文字 */
    --ink-soft: #4A7BA7;  /* 中藍灰輔助 */
    --line: #BEE3F8;      /* 極淡藍分隔線 */
  }
  body { 
    font-family: 'Microsoft JhengHei', sans-serif; 
    background: 
      linear-gradient(160deg, #F7F9FB 0%, #EEF1F5 100%);
    color: var(--ink); min-height: 100vh; background-attachment: fixed; 
  }
  
  /* === Header === */
  .header {
    background: rgba(74,144,217,0.82);
    backdrop-filter: blur(16px);
    -webkit-backdrop-filter: blur(16px);
    color: white; padding: 9px 28px;
    display: flex; justify-content: space-between; align-items: center;
    box-shadow: 0 2px 16px rgba(26,95,168,0.2);
    position: sticky; top: 0; z-index: 100;
    border-bottom: 1px solid rgba(255,255,255,0.2);
  }
  .header-left h1 { 
    font-size: 19px; font-weight: 800; color: white; 
    letter-spacing: 0.5px;
    text-shadow: 0 1px 3px rgba(0,0,0,0.15);
  }
  .header-left .sub { 
    font-size: 12px; margin-top: 3px; color: rgba(255,255,255,0.8);
    letter-spacing: 0.3px;
  }
  .header-right { display: flex; align-items: center; gap: 10px; }
  .header-stat {
    background: rgba(255,255,255,0.15); padding: 7px 14px; border-radius: 20px;
    font-size: 13px; color: rgba(255,255,255,0.9); border: 1px solid rgba(255,255,255,0.25);
    display: flex; align-items: center; gap: 5px;
  }
  .header-stat strong { color: white; font-weight: 800; font-size: 15px; }
  .user-info { 
    font-size: 14px; background: rgba(255,255,255,0.25); 
    padding: 7px 14px; border-radius: 20px;
    border: 1px solid rgba(255,255,255,0.5);
    color: white; font-weight: 600;
    text-shadow: 0 1px 2px rgba(0,0,0,0.2);
  }
  .user-info .name { font-weight: 800; color: white; }
  .user-info .role { 
    background: rgba(255,255,255,0.4); color: white; 
    padding: 2px 10px; border-radius: 10px; font-size: 12px; margin-left: 6px;
    font-weight: 700;
  }
  .logout-btn {
    background: rgba(255,255,255,0.25); border: 1px solid rgba(255,255,255,0.5); 
    color: white; padding: 8px 18px;
    font-family: inherit; font-size: 14px; border-radius: 20px; cursor: pointer;
    font-weight: 700; transition: all 0.2s;
    text-shadow: 0 1px 2px rgba(0,0,0,0.2);
  }
  .logout-btn:hover { background: rgba(255,255,255,0.38); transform: translateY(-1px); }
  
  .container { max-width: 1400px; margin: 0 auto; padding: 20px 40px; }
  
  /* === Cards === */
  .card {
    background: rgba(255,255,255,0.65);
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    border-radius: 16px; padding: 24px; margin-bottom: 18px;
    box-shadow: 0 4px 24px rgba(26,95,168,0.12), 0 1px 4px rgba(255,255,255,0.8) inset;
    border: 1px solid rgba(255,255,255,0.8);
    transition: box-shadow 0.2s, transform 0.2s;
  }
  .card:hover { 
    box-shadow: 0 8px 32px rgba(26,95,168,0.18); 
    transform: translateY(-1px);
  }
  .card h2 { 
    font-size: 16px; margin-bottom: 18px; color: #1a5fa8; 
    display: flex; align-items: center; gap: 10px; font-weight: 800;
    padding-bottom: 12px; border-bottom: 1px solid rgba(26,95,168,0.12);
  }
  .card h2::before { content:''; width:4px; height:20px; background: linear-gradient(180deg, #2980d4, #4AAEE0); border-radius: 3px; }
  
  /* === Buttons === */
  button.btn-primary {
    background: linear-gradient(135deg, #2980d4, #4AAEE0);
    color: white; border: none; padding: 12px 28px;
    font-family: inherit; font-size: 14px; font-weight: 700;
    border-radius: 20px; cursor: pointer; transition: all 0.2s;
    box-shadow: 0 4px 14px rgba(41,128,212,0.35);
  }
  button.btn-primary:hover { transform: translateY(-2px); box-shadow: 0 6px 20px rgba(41,128,212,0.45); }
  button.btn-secondary {
    background: white; color: #2980d4; 
    border: 1.5px solid rgba(74,144,217,0.4);
    padding: 10px 22px; font-family: inherit; font-size: 13px; font-weight: 700;
    border-radius: 20px; cursor: pointer; transition: all 0.2s;
    box-shadow: 0 2px 8px rgba(74,144,217,0.1);
  }
  button.btn-secondary:hover { background: #EBF8FF; border-color: #4A90D9; transform: translateY(-1px); }
  
  /* === Institute update button (commit 15: 單獨更新此協會) === */
  .inst-update-btn {
    background: linear-gradient(135deg, #2980d4, #4AAEE0);
    color: white; border: none;
    padding: 10px 18px; font-family: inherit; font-size: 13px; font-weight: 700;
    border-radius: 12px; cursor: pointer; flex-shrink: 0; white-space: nowrap;
    transition: all 0.2s; box-shadow: 0 3px 10px rgba(41,128,212,0.3);
  }
  .inst-update-btn:hover {
    background: linear-gradient(135deg, #1a6bb5, #2980d4);
    transform: translateY(-2px); box-shadow: 0 5px 14px rgba(41,128,212,0.4);
  }

  /* === Institute card (協會勾選) === */
  .inst-card {
    background: white;
    border: 2px solid #C8DCF0;
    border-radius: 12px; padding: 14px 20px;
    display: flex; align-items: center; gap: 12px;
    cursor: pointer; transition: all 0.2s;
    user-select: none;
  }
  .inst-card:hover { transform: translateY(-2px); box-shadow: 0 6px 16px rgba(44,82,130,0.15); }
  .inst-card.checked {
    background: #EBF8FF;
    border-color: #4A90D9;
    box-shadow: 0 4px 14px rgba(74,144,217,0.2);
  }
  .inst-card.checked .label { color: #2B6CB0; }
  .inst-card.checked .desc { color: #4A7BA7; }
  .inst-card input { width: 22px; height: 22px; cursor: pointer; accent-color: #4A90D9; }
  .inst-card .label { font-weight: 700; font-size: 14px; color: #1A202C; }
  .inst-card .desc { font-size: 11px; color: #4A6080; }

  /* 未勾選：淡化 */
  #instTicsha, #instCpc, #instIsha, #instCshm, #instTete, #instHwahsing { opacity: 0.7; }

  /* 勾選後：各自彩色條 + 淡色背景 + 恢復不透明 */
  #instTicsha.checked { border-left: 5px solid #3182CE; background: #EBF8FF; opacity: 1; }
  #instCpc.checked    { border-left: 5px solid #38A169; background: #F0FFF4; opacity: 1; }
  #instIsha.checked   { border-left: 5px solid #D69E2E; background: #FEFCBF; opacity: 1; }
  #instCshm.checked   { border-left: 5px solid #805AD5; background: #FAF5FF; opacity: 1; }
  #instTete.checked   { border-left: 5px solid #E53E3E; background: #FFF5F5; opacity: 1; }
  #instHwahsing.checked { border-left: 5px solid #319795; background: #E6FFFA; opacity: 1; }
  
  /* === Inputs === */
  input[type=text], input[type=number], select {
    padding: 10px 16px; border: 1.5px solid rgba(74,144,217,0.25); border-radius: 20px;
    font-family: inherit; font-size: 14px; background: white; color: #1A202C;
    transition: all 0.2s; box-shadow: 0 1px 4px rgba(26,95,168,0.06);
  }
  input:focus, select:focus { 
    outline: none; border-color: #4A90D9; background: white;
    box-shadow: 0 0 0 3px rgba(74,144,217,0.15);
  }
  label { font-size: 13px; color: #1a5fa8; font-weight: 600; }
  
  /* === Badges === */
  .badge { display: inline-block; padding: 4px 11px; border-radius: 14px; font-size: 11px; font-weight: 700; }
  .badge-open { background: #D4EDDA; color: #1B5E20; }
  .badge-full { background: #FFE5E5; color: #B71C1C; }
  .badge-promo { background: #FFF3CD; color: #856404; }
  .badge-other { background: #E2E3E5; color: #383D41; }
  .badge-cat-init { background: #CCE5FF; color: #004085; }
  .badge-cat-re { background: #F3E0FF; color: #4A148C; }
  .badge-nat-tw { background: #E8F5E9; color: #1B5E20; }
  .badge-nat-fg { background: #FFF8E1; color: #6D4C41; }
  
  /* === Table === */
  .filter-bar {
    display: grid;
    grid-template-columns: 2.4fr 1fr;
    gap: 10px; margin-bottom: 16px;
    background: rgba(255,255,255,0.6);
    border-radius: 16px;
    padding: 14px 16px;
    border: 1px solid rgba(255,255,255,0.8);
    box-shadow: 0 2px 12px rgba(26,95,168,0.08);
  }
  .stat { 
    font-size: 13px; color: #4A7BA7; margin-bottom: 12px;
    background: rgba(255,255,255,0.6);
    backdrop-filter: blur(8px);
    padding: 10px 16px; border-radius: 12px;
    border: 1px solid rgba(74,144,217,0.15);
    display: flex; align-items: center; justify-content: space-between;
    position: relative; z-index: 1;
  }
  .stat strong { 
    color: #1a5fa8; font-weight: 800; font-size: 20px;
    text-shadow: 0 1px 2px rgba(26,95,168,0.15);
  }
  
  .table-wrap {
    max-height: 480px; overflow-y: auto; overflow-x: hidden;
    border: 2px solid var(--line); border-radius: 12px; background: white;
    position: relative; z-index: 1;
  }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  
  /* === Sticky header (第 4 點) === */
  thead { position: sticky; top: 0; z-index: 5; }
  thead tr { background: var(--blue) !important; }
  th {
    padding: 12px 8px; text-align: left; font-weight: 700;
    color: white; background: var(--blue);
    border-bottom: 2px solid var(--teal);
  }
  td { padding: 10px 8px; border-bottom: 1px solid var(--line); }
  
  /* === Row selection (第 2 點 - 整列點擊) === */
  tbody tr { cursor: pointer; transition: background 0.15s; }
  tbody tr:nth-child(even) { background: rgba(74,144,217,0.04); }
  tbody tr:hover { background: rgba(74,144,217,0.1); }
  tbody tr.selected { background: linear-gradient(90deg, #fff3cd, #ffeaa0); border-left: 4px solid #f6a623; }
  tbody tr.selected:hover { background: linear-gradient(90deg, #ffeaa0, #ffd96a); }
  
  /* 大型勾選方塊 */
  .big-check {
    width: 24px; height: 24px; border: 2.5px solid var(--ink-soft);
    border-radius: 6px; display: flex; align-items: center; justify-content: center;
    background: white; font-size: 18px; color: var(--blue);
  }
  tr.selected .big-check {
    background: var(--blue); border-color: var(--blue); color: white;
  }
  
  /* === Selected list (第 3 點 - 取消按鈕優化) === */
  .selected-list {
    background: linear-gradient(135deg, #FFF9E6 0%, #FFF4E6 100%);
    border: 2.5px dashed #F4B860;
    border-radius: 12px; padding: 14px; margin-bottom: 14px; min-height: 70px;
  }
  .sel-item {
    background: white; padding: 12px 14px; border-radius: 10px;
    margin-bottom: 8px; display: flex; justify-content: space-between; align-items: center;
    box-shadow: 0 1px 3px rgba(0,0,0,0.06);
  }
  .sel-item .info { flex: 1; font-size: 13px; }
  .sel-item .info b { color: var(--blue); }
  .sel-item .info .meta { font-size: 11px; color: var(--ink-soft); margin-top: 3px; }
  .sel-item .remove-btn {
    background: #FFE5E5; color: #C44569; border: 2px solid #FFB8B8;
    padding: 8px 16px; border-radius: 8px; cursor: pointer;
    font-family: inherit; font-size: 13px; font-weight: 700;
    transition: all 0.15s;
  }
  .sel-item .remove-btn:hover { background: #C44569; color: white; border-color: #C44569; }
  
  /* === Forms === */
  .form-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 14px; margin-bottom: 16px; }
  .form-grid label { display: block; margin-bottom: 6px; }
  .form-grid input, .form-grid select { width: 100%; }
  
  /* === Loading & Toast === */
  .loading {
    display: none; background: rgba(0,0,0,0.5); position: fixed;
    top: 0; left: 0; right: 0; bottom: 0; z-index: 999;
    align-items: center; justify-content: center;
  }
  .loading.show { display: flex; }
  .loading-box { background: white; padding: 32px 48px; border-radius: 14px; text-align: center; }
  .spinner {
    border: 4px solid var(--line); border-top: 4px solid var(--teal);
    border-radius: 50%; width: 44px; height: 44px;
    animation: spin 1s linear infinite; margin: 0 auto 14px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  
  .toast {
    position: fixed; bottom: 24px; right: 24px;
    background: var(--blue); color: white; padding: 14px 22px;
    border-radius: 10px; box-shadow: 0 6px 16px rgba(0,0,0,0.2);
    z-index: 1000; opacity: 0; transition: opacity 0.3s; font-weight: 600;
  }
  .toast.show { opacity: 1; }
  .toast.success { background: #4FB3BF; }
  .toast.error { background: #C44569; }
  
  /* === Modal === */
  .modal {
    display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.5); z-index: 998;
    align-items: center; justify-content: center; padding: 24px;
  }
  .modal.show { display: flex; }
  .modal-box {
    background: white; border-radius: 14px; max-width: 1000px; width: 100%;
    max-height: 90vh; overflow: hidden; display: flex; flex-direction: column;
  }
  .modal-head {
    padding: 16px 22px; border-bottom: 1px solid var(--line);
    display: flex; justify-content: space-between; align-items: center; gap: 12px;
  }
  .modal-meta {
    padding: 10px 22px; background: var(--paper);
    border-bottom: 1px solid var(--line); font-size: 13px;
  }
  .modal-body { padding: 22px; overflow-y: auto; flex: 1; }
  
  /* === Auto update panel === */
  .setting-row {
    display: flex; justify-content: space-between; align-items: center;
    padding: 10px 0; border-bottom: 1px dashed var(--line);
  }
  .setting-row:last-child { border-bottom: none; }
  .toggle-switch {
    width: 44px; height: 24px; background: #ccc; border-radius: 12px;
    position: relative; cursor: pointer; transition: background 0.2s;
  }
  .toggle-switch.on { background: var(--teal); }
  .toggle-switch::after {
    content: ''; position: absolute; width: 20px; height: 20px;
    background: white; border-radius: 50%; top: 2px; left: 2px;
    transition: left 0.2s;
  }
  .toggle-switch.on::after { left: 22px; }
  
  details { margin-top: 12px; }
  details summary {
    cursor: pointer; color: var(--blue); font-size: 13px; font-weight: 600;
    padding: 6px 0;
  }
  
  .info-line { font-size: 12px; color: var(--ink-soft); margin-top: 6px; }
</style>
</head>
<body>

<div class="header">
  <div class="header-left">
    <div style="font-size:10px;letter-spacing:1.5px;font-weight:700;opacity:0.7;">EXTERNAL TRAINING MANAGEMENT</div>
    <h1>📚 外訓課程整合管理系統</h1>
    <div class="sub">自動抓取協會課程資料 → 視覺化挑選 → 一鍵產生派訓信件</div>
  </div>
  <div class="header-right">
    <div class="header-stat">
      ⏱ 使用時間 <strong id="usageTime">0</strong> 分鐘
    </div>
    <div class="header-stat">
      👥 在線 <strong id="onlineCount">1</strong> 人
    </div>
    <div class="user-info">
      <span class="name">{{ user.display_name }}</span>
      <span class="role">{{ '管理員' if user.role == 'admin' else '使用者' }}</span>
    </div>
    {% if user.role == 'admin' %}<button class="logout-btn" onclick="location.href='/admin'" style="background:rgba(255,255,255,0.3);border:1px solid rgba(255,255,255,0.5);color:white;font-weight:700;">⚙️ 後台管理</button>{% endif %}
    <button class="logout-btn" onclick="logout()">登出</button>
  </div>
</div>

<div class="container">

  <div id="overdueBanner" style="display:none;"></div>

  <!-- ① 抓取課程 -->
  <div class="card">
    <div onclick="toggleScrapeSection()" style="display:flex;align-items:center;justify-content:space-between;cursor:pointer;user-select:none;">
      <h2 style="margin:0;">① 抓取課程資料 <span style="font-size:13px;font-weight:400;color:#888;">（平常收合，要更新課程時再點開）</span></h2>
      <span id="scrapeToggleIcon" style="font-size:16px;color:#319795;font-weight:700;white-space:nowrap;">▼ 展開</span>
    </div>
    <div id="scrapeBody" style="display:none;margin-top:14px;">
      <style>
        #instCardContainer .inst-card{padding:10px 12px!important;border-radius:10px!important;}
        #instCardContainer .label{font-size:13.5px!important;}
        #instCardContainer .desc{font-size:11px!important;line-height:1.4!important;}
        #instCardContainer .inst-update-btn{padding:6px 10px!important;font-size:12px!important;}
      </style>
      <div style="display:grid; grid-template-columns: repeat(4, 1fr); gap:14px;">
        <div id="instCardContainer" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px;grid-column:1/-1;"></div>
      </div>
      <div style="background:#FFF3CD;border:2px solid #F4B860;border-radius:10px;padding:10px 16px;font-size:13px;color:#856404;line-height:1.6;margin-top:12px;">
        ⚠️ <b>請分開更新</b>，依序按各協會的「🔄 只更新」按鈕
        <span style="font-size:11px;opacity:0.8;margin-left:8px;">（同時更新多個協會會造成系統記憶體不足）</span>
      </div>
      <div class="info-line" id="lastUpdate">尚未抓取</div>
    </div>
    
    
  </div>

  <!-- ② 篩選並挑選 -->
  <div class="card">
    <h2>② 篩選並挑選課程</h2>
    <div class="filter-bar">
      <input type="text" id="searchKw" placeholder="🔍 搜尋 (空格=都要 逗號=任一，例: 越南 堆高機)" oninput="renderTable()">
      <select id="filterInstitute" onchange="renderTable()">
        <option value="">全部主辦單位</option>
        <option value="台灣省工商安全衛生協會">台灣省工商安全衛生協會</option>
        <option value="中國生產力中心">中國生產力中心 (CPC)</option>
        <option value="中華民國工業安全衛生協會">中華民國工業安全衛生協會 (ISHA)</option>
        <option value="中國勞工安全衛生管理學會">中國勞工安全衛生管理學會 (CSHM)</option>
        <option value="台灣能量輻射防護偵測有限公司">台灣能量輻射防護偵測有限公司 (TETE)</option>
        <option value="華新輻射防護偵測股份有限公司">華新輻射防護偵測股份有限公司 (華新)</option>
      </select>
      <select id="filterBranch" onchange="renderTable()" style="display:none;"><option value="">全部分會</option></select>

      <div id="categoryMultiBox" style="display:none;">
        <button type="button" onclick="toggleCategoryDropdown()" id="categoryToggleBtn"
                style="width:100%;padding:10px 14px;border:2px solid #B8CCE4;border-radius:10px;background:white;color:#0F1F2E;text-align:left;cursor:pointer;font-family:inherit;font-size:14px;">
          🏷️ 全部類別
        </button>
        <div id="categoryDropdown" style="display:none;position:absolute;top:100%;left:0;min-width:200px;background:white;border:2px solid var(--teal);border-radius:10px;margin-top:4px;padding:10px;z-index:500;box-shadow:0 4px 12px rgba(0,0,0,0.15);">
          <div style="border-bottom:1px solid #eee;padding-bottom:6px;margin-bottom:6px;display:flex;gap:6px;">
            <button onclick="selectAllCategories()" style="flex:1;background:var(--teal);color:white;border:none;padding:5px;border-radius:5px;cursor:pointer;font-size:12px;">全選</button>
            <button onclick="clearAllCategories()" style="flex:1;background:#ccc;color:white;border:none;padding:5px;border-radius:5px;cursor:pointer;font-size:12px;">清除</button>
          </div>
          <label style="display:flex;align-items:center;gap:8px;padding:6px 8px;cursor:pointer;border-radius:6px;" onmouseover="this.style.background='#f0f8ff'" onmouseout="this.style.background=''"><input type="checkbox" value="複訓" onchange="onCategoryChange()" style="width:18px;height:18px;cursor:pointer;"> 複訓</label>
          <label style="display:flex;align-items:center;gap:8px;padding:6px 8px;cursor:pointer;border-radius:6px;" onmouseover="this.style.background='#f0f8ff'" onmouseout="this.style.background=''"><input type="checkbox" value="初訓" onchange="onCategoryChange()" style="width:18px;height:18px;cursor:pointer;"> 初訓</label>
          <label style="display:flex;align-items:center;gap:8px;padding:6px 8px;cursor:pointer;border-radius:6px;" onmouseover="this.style.background='#f0f8ff'" onmouseout="this.style.background=''"><input type="checkbox" value="輻射" onchange="onCategoryChange()" style="width:18px;height:18px;cursor:pointer;"> 輻射(全部)</label>
          <label style="display:flex;align-items:center;gap:8px;padding:6px 8px;cursor:pointer;border-radius:6px;" onmouseover="this.style.background='#f0f8ff'" onmouseout="this.style.background=''"><input type="checkbox" value="3小時" onchange="onCategoryChange()" style="width:18px;height:18px;cursor:pointer;"> ↳ 輻射3小時</label>
          <label style="display:flex;align-items:center;gap:8px;padding:6px 8px;cursor:pointer;border-radius:6px;" onmouseover="this.style.background='#f0f8ff'" onmouseout="this.style.background=''"><input type="checkbox" value="18小時" onchange="onCategoryChange()" style="width:18px;height:18px;cursor:pointer;"> ↳ 輻射18小時</label>
          <label style="display:flex;align-items:center;gap:8px;padding:6px 8px;cursor:pointer;border-radius:6px;" onmouseover="this.style.background='#f0f8ff'" onmouseout="this.style.background=''"><input type="checkbox" value="36小時" onchange="onCategoryChange()" style="width:18px;height:18px;cursor:pointer;"> ↳ 輻射36小時</label>
        </div>
      </div>


    </div>
    <style>
      .chip-filter{display:flex;align-items:flex-start;gap:10px;flex-wrap:wrap;margin-bottom:10px;}
      .chip-label{font-size:15px;font-weight:700;color:var(--ink-soft);padding-top:8px;white-space:nowrap;min-width:60px;}
      .chip-row{display:flex;flex-wrap:wrap;gap:6px;flex:1;}
      .chip{padding:8px 18px;border-radius:18px;border:1.5px solid #B8CCE4;background:white;color:#33546e;font-size:15px;cursor:pointer;font-family:inherit;transition:all .12s;}
      .chip:hover{border-color:#319795;}
      .chip.on{background:#319795;border-color:#319795;color:white;font-weight:700;}
      .chip-all{min-width:72px;text-align:center;}
      .chip-all{min-width:64px;text-align:center;}
      .chip-mini{font-size:13px;color:#319795;font-weight:700;background:none;border:none;cursor:pointer;padding:4px 6px;text-decoration:underline;align-self:center;}
    </style>
    <div style="background:#F5F9FD;border-radius:12px;padding:12px 14px;margin-bottom:14px;">
      <div class="chip-filter">
        <span class="chip-label">🏷️ 類別</span>
        <div id="categoryChips" class="chip-row"></div>
        <button class="chip-mini" onclick="setCategoryAll()">清除</button>
      </div>
      <div class="chip-filter">
        <span class="chip-label">📍 分會</span>
        <div id="branchChips" class="chip-row"></div>

        <button class="chip-mini" onclick="clearBranchSel()">清除</button>
      </div>
      <div class="chip-filter">
        <span class="chip-label">🕒 班別</span>
        <div id="classChips" class="chip-row"></div>

        <button class="chip-mini" onclick="clearClassSel()">清除</button>
      </div>
      <div class="chip-filter">
        <span class="chip-label">🌐 國籍</span>
        <div id="natChips" class="chip-row"></div>
        <button class="chip-mini" onclick="setNatFilter('')">清除</button>
      </div>
      <div class="chip-filter" style="margin-bottom:0;">
        <span class="chip-label">📋 狀態</span>
        <div id="statChips" class="chip-row"></div>
        <button class="chip-mini" onclick="setStatFilter('')">清除</button>
      </div>
    </div>
    <div class="stat">
      共 <strong id="totalCount">0</strong> 筆,目前顯示 <strong id="visCount">0</strong> 筆,已選擇 <strong id="selCount">0</strong> 筆
      <span style="float:right;color:var(--ink-soft);font-size:12px;">💡 點選整列即可勾選/取消</span>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th width="50">選</th><th>課程名稱</th><th width="70">分會</th>
          <th width="60">類別</th><th width="65">國籍</th>
          <th width="110">開課日</th><th width="85">班別</th>
          <th width="50">時數</th><th width="60">費用</th><th width="90">狀態</th>
        </tr></thead>
        <tbody id="tbody">
          <tr><td colspan="10" style="text-align:center;padding:50px;color:#999;">請先按「立即更新」抓取資料</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- ③ 產生派訓信件 -->
  <div class="card">
    <h2>③ 產生派訓信件 (含課程大綱)</h2>
    <div class="selected-list" id="selectedList">
      <div style="color:#999;text-align:center;padding:10px;">尚未挑選任何課程</div>
    </div>
    <div class="form-grid">
  <div style="grid-column:1/-1;">
          <label>派訓同仁(可加多人,每人各自部門)</label>
          <div id="empList" style="display:flex;flex-direction:column;gap:6px;margin-top:4px;"></div>
          <button type="button" onclick="addEmployee()" onmouseover="this.style.background='#D4E9FF';this.style.borderColor='#2980d4';" onmouseout="this.style.background='#EBF5FF';this.style.borderColor='#7FB3E8';" style="margin-top:8px;background:#EBF5FF;border:2px dashed #7FB3E8;color:#2980d4;padding:10px 22px;border-radius:10px;font-size:14px;font-weight:700;cursor:pointer;font-family:inherit;transition:all 0.2s;">➕ 新增同仁</button>
        </div>
        
      <div><label>預訂月份</label><input type="number" id="empMonth" placeholder="例: 5" min="1" max="12"></div>
      <div>
        <label>參訓類別</label>
        <select id="trainType"><option>複訓</option><option>初訓</option><option>在職教育訓練</option></select>
      </div>
      <div style="grid-column:1/3;">
        <label>欲派訓課程名稱</label>
        <input type="text" id="courseName" placeholder="例: 粉塵作業主管-複訓">
      </div>
    </div>
    
    <div style="display:flex; gap:10px; flex-wrap:wrap; margin-top:8px;">
      <button class="btn-primary" onclick="previewExternal()" style="font-size:16px;padding:14px 36px;">👁️ 預覽並複製信件</button>
      <button class="btn-secondary" onclick="clearForm()" style="font-size:15px;padding:14px 28px;">🧹 清空表單</button>
    </div>
    <div class="info-line">
      信件含完整資訊:場次、主辦、日期(含星期)、時間、地點、班別、時數、費用、狀態、報名連結 — 按開課日升冪排序
    </div>
  </div>

  <div style="text-align:center;padding:24px;font-size:12px;line-height:1.8;">
    <span style="color:rgba(44,82,130,0.6);font-weight:600;letter-spacing:0.5px;">External Training Management System v7.0</span><br>
    <span style="color:rgba(44,82,130,0.45);">© 2026 Training Dept. ｜ System internal use only.</span>
  </div>

</div>

<div class="loading" id="loading"><div class="loading-box"><div class="spinner"></div><div id="loadingText">處理中...</div></div></div>
<div class="toast" id="toast"></div>

<!-- 預覽 Modal -->
<div class="modal" id="previewModal">
  <div class="modal-box">
    <div class="modal-head">
      <strong>📧 信件預覽 <span id="prevMode" style="background:var(--lavender);padding:2px 10px;border-radius:10px;font-size:12px;margin-left:8px;"></span></strong>
      <div style="display:flex;gap:8px;">
        <button class="btn-primary" onclick="copyFromPreview()" style="padding:8px 16px;font-size:13px;">📋 複製信件</button>
        <button class="btn-secondary" onclick="document.getElementById('previewModal').classList.remove('show')">關閉</button>
      </div>
    </div>
    <div class="modal-meta"><strong>主旨:</strong> <span id="prevSubject"></span></div>
    <div class="modal-body" id="prevBody"></div>
  </div>
</div>

<script>
let allCourses = [];
let selected = new Map();
let startTime = Date.now();

// 使用時間計時器
setInterval(() => {
  const mins = Math.floor((Date.now() - startTime) / 60000);
  document.getElementById('usageTime').textContent = mins;
}, 5000);

// 在線人數刷新
async function refreshOnline() {
  try {
    const resp = await fetch('/api/online');
    if (resp.status === 401) { window.location.href = '/login'; return; }
    const data = await resp.json();
    document.getElementById('onlineCount').textContent = data.count;
  } catch (e) {}
}
setInterval(refreshOnline, 30000);

function showLoading(t) { document.getElementById('loadingText').textContent = t || '處理中...'; document.getElementById('loading').classList.add('show'); }
function hideLoading() { document.getElementById('loading').classList.remove('show'); }
function toast(msg, type) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.className = 'toast show ' + (type || '');
  setTimeout(() => t.classList.remove('show'), 3000);
}

function logout() {
  if (confirm('確定要登出嗎?')) window.location.href = '/logout';
}

function toggleInst(code) {
  const cb = document.getElementById('instCheck' + code.charAt(0).toUpperCase() + code.slice(1));
  cb.checked = !cb.checked;
  const card = document.getElementById('inst' + code.charAt(0).toUpperCase() + code.slice(1));
  card.classList.toggle('checked', cb.checked);
}

function checkOverdue() {
  fetch('/api/scrapers').then(r => r.json()).then(scrapers => {
    const DAY = 86400000;
    const isOverdue = s => {
      const limit = s.manual ? 30 : 14;
      if (!s.last_updated) return true;
      return (Date.now() - new Date(s.last_updated).getTime()) > limit * DAY;
    };
    scrapers.forEach(s => {
      const cap = s.code.charAt(0).toUpperCase() + s.code.slice(1);
      const el = document.getElementById(s.manual ? 'instHwahsingInfo' : 'inst' + cap);
      if (el) {
        if (isOverdue(s)) { el.style.boxShadow = 'inset 0 0 0 2px #E53E3E'; }
        else { el.style.boxShadow = ''; }
      }
    });
    const overdue = scrapers.filter(isOverdue);
    const banner = document.getElementById('overdueBanner');
    if (!banner) return;
    if (overdue.length === 0 || sessionStorage.getItem('overdueDismissed') === '1') {
      banner.style.display = 'none';
      return;
    }
    const names = overdue.map(s => s.name + (s.manual ? '（需管理員匯入）' : '')).join('、');
    banner.innerHTML =
      '<div style="background:#FDECEA;border:1.5px solid #E53E3E;border-radius:10px;padding:12px 16px;display:flex;align-items:center;gap:12px;">' +
        '<span style="font-size:13px;color:#B71C1C;line-height:1.6;flex:1;">⚠️ 有 <b>' + overdue.length + '</b> 個協會的課程資料較久未更新：<b>' + names + '</b>，建議盡快更新。</span>' +
        '<button onclick="sessionStorage.setItem(\'overdueDismissed\',\'1\');document.getElementById(\'overdueBanner\').style.display=\'none\';" style="background:none;border:none;color:#B71C1C;cursor:pointer;font-size:13px;white-space:nowrap;">✕ 關閉</button>' +
      '</div>';
    banner.style.display = 'block';
  }).catch(() => {});
}

function toggleScrapeSection() {
  const b = document.getElementById('scrapeBody');
  const ic = document.getElementById('scrapeToggleIcon');
  const open = b.style.display === 'none';
  b.style.display = open ? 'block' : 'none';
  ic.textContent = open ? '▲ 收合' : '▼ 展開';
}

async function loadScrapers() {
  try {
    const resp = await fetch('/api/scrapers');
    if (resp.status === 401) return;
    const scrapers = await resp.json();
    const container = document.getElementById('instCardContainer');
    container.innerHTML = scrapers.map(s => {
      if (s.manual) {
        const lastUp = s.last_updated ? s.last_updated.slice(0,16) : '尚未匯入';
        return `
          <div class="inst-card" id="instHwahsingInfo" style="cursor:default;border-left:5px solid #319795;background:#E6FFFA;opacity:1;">
            <div style="flex:1;">
              <div class="label">${s.name}</div>
              <div class="desc">${s.desc || ''}</div>
              <div style="font-size:11px;margin-top:4px;color:#0E7C7B;font-weight:700;">🔒 此協會無法自動抓取，更新請通知管理員</div>
              <div style="font-size:11px;margin-top:2px;color:#0E7C7B;">📥 管理員上次匯入：${lastUp}</div>
            </div>
          </div>`;
      }
      const capCode = s.code.charAt(0).toUpperCase() + s.code.slice(1);
      const lastUp = s.last_updated ? s.last_updated.slice(0,16) : '尚未更新';
      const isOld = s.last_updated ? (Date.now() - new Date(s.last_updated).getTime() > 7*24*60*60*1000) : true;
      return `
        <label class="inst-card checked" id="inst${capCode}" onclick="toggleInst('${s.code}')">
          <input type="checkbox" id="instCheck${capCode}" checked>
          <div style="flex:1;">
            <div class="label">${s.name}</div>
            <div class="desc">${s.desc || ''}</div>
            <div style="font-size:10px;margin-top:4px;color:${isOld?'#E53E3E':'#38A169'};">
              ${isOld?'⚠️':'✅'} 上次更新：${lastUp}
            </div>
          </div>
          <button type="button" class="inst-update-btn"
            onclick="event.stopPropagation();event.preventDefault();updateOnly('${s.code}');"
            title="只更新此協會,不影響其他">🔄 只更新</button>
        </label>`;
    }).join('');
  } catch(e) { console.error(e); }
}

async function loadCourses() {
  try {
    const resp = await fetch('/api/courses');
    if (resp.status === 401) { window.location.href = '/login'; return; }
    const data = await resp.json();
    allCourses = data.courses || [];
    document.getElementById('lastUpdate').textContent = data.last_updated ? '上次更新: ' + data.last_updated : '尚未抓取';
    renderCategoryChips();
    populateBranches();
    populateClasses();
    renderNatChips();
    renderStatChips();
    renderTable();
    checkOverdue();
  } catch (e) { console.error(e); }
}

let selectedBranches = new Set();  // 已選的分會

let branchInit = false;
function populateBranches() {
  const all = [...new Set(allCourses.map(c => c.branch == null ? '' : c.branch))];
  const real = all.filter(b => b !== '').sort();
  const list = all.includes('') ? [...real, ''] : real;
  const _allOn = selectedBranches.size === 0;
  const _allChip = `<button class="chip chip-all ${_allOn?'on':''}" onclick="clearBranchSel()">全部</button>`;
  document.getElementById('branchChips').innerHTML = _allChip + list.map(b => {
    const label = b === '' ? '(未分類)' : b;
    return `<button class="chip ${selectedBranches.has(b)?'on':''}" onclick="toggleBranchChip('${b.replace(/'/g,"\\'")}')">${label}</button>`;
  }).join('');
}

function clearBranchSel() {
  selectedBranches.clear();
  populateBranches(); renderTable();
}

function toggleBranchChip(b) {
  if (selectedBranches.has(b)) selectedBranches.delete(b);
  else selectedBranches.add(b);
  populateBranches(); renderTable();
}

function selectAllBranchChips(on) {
  selectedBranches.clear();
  if (on) [...new Set(allCourses.map(c => c.branch))].forEach(b => selectedBranches.add(b));
  populateBranches(); renderTable();
}

let selectedClasses = new Set();

let classInit = false;
function populateClasses() {
  const real = [...new Set(allCourses.map(c => (c.class_type || '').trim()).filter(Boolean))].sort();
  const hasBlank = allCourses.some(c => !(c.class_type || '').trim());
  const list = hasBlank ? [...real, '__blank__'] : real;
  const _allOn = selectedClasses.size === 0;
  const _allChip = `<button class="chip chip-all ${_allOn?'on':''}" onclick="clearClassSel()">全部</button>`;
  document.getElementById('classChips').innerHTML = _allChip + list.map(t => {
    const label = t === '__blank__' ? '(未標註)' : t;
    return `<button class="chip ${selectedClasses.has(t)?'on':''}" onclick="toggleClassChip('${t}')">${label}</button>`;
  }).join('');
}

function clearClassSel() {
  selectedClasses.clear();
  populateClasses(); renderTable();
}

function toggleClassChip(t) {
  if (selectedClasses.has(t)) selectedClasses.delete(t);
  else selectedClasses.add(t);
  populateClasses(); renderTable();
}

function selectAllClassChips(on) {
  selectedClasses.clear();
  if (on) [...new Set(allCourses.map(c => (c.class_type || '').trim() || '__blank__'))].forEach(t => selectedClasses.add(t));
  populateClasses(); renderTable();
}

function toggleClassDropdown() {
  const d = document.getElementById('classDropdown');
  d.style.display = d.style.display === 'none' ? 'block' : 'none';
}

function onClassCheck(cb) {
  if (cb.checked) selectedClasses.add(cb.value);
  else selectedClasses.delete(cb.value);
  updateClassButtonText();
  renderTable();
}

function selectAllClasses() {
  document.querySelectorAll('#classOptions input[type=checkbox]').forEach(cb => {
    cb.checked = true; selectedClasses.add(cb.value);
  });
  updateClassButtonText(); renderTable();
}

function clearAllClasses() {
  document.querySelectorAll('#classOptions input[type=checkbox]').forEach(cb => { cb.checked = false; });
  selectedClasses.clear();
  updateClassButtonText(); renderTable();
}

function updateClassButtonText() {
  const all = [...new Set(allCourses.map(c => (c.class_type || '').trim() || '__blank__'))];
  const sel = [...selectedClasses];
  const btn = document.getElementById('classToggleBtn');
  if (sel.length === 0) btn.textContent = '🕒 (未選班別)';
  else if (sel.length === all.length) btn.textContent = '🕒 全部班別';
  else btn.textContent = '🕒 ' + sel.map(t => t === '__blank__' ? '未標註' : t).join(', ');
}

function toggleCategoryDropdown() {
  const dd = document.getElementById('categoryDropdown');
  dd.style.display = dd.style.display === 'none' ? 'block' : 'none';
}

function selectAllCategories() {
  document.querySelectorAll('#categoryDropdown input[type=checkbox]').forEach(cb => { cb.checked = true; });
  onCategoryChange();
}

function clearAllCategories() {
  document.querySelectorAll('#categoryDropdown input[type=checkbox]').forEach(cb => { cb.checked = false; });
  onCategoryChange();
}

function onCategoryChange() {
  const checked = [...document.querySelectorAll('#categoryDropdown input[type=checkbox]:checked')];
  const btn = document.getElementById('categoryToggleBtn');
  const total = document.querySelectorAll('#categoryDropdown input[type=checkbox]').length;
  if (checked.length === 0) {
    btn.textContent = '🏷️ (未選類別)';
  } else if (checked.length === total) {
    btn.textContent = '🏷️ 全部類別';
  } else {
    btn.textContent = `🏷️ 已選 ${checked.length} 項`;
  }
  renderTable();
}

function toggleBranchDropdown() {
  const d = document.getElementById('branchDropdown');
  d.style.display = d.style.display === 'none' ? 'block' : 'none';
}

function onBranchCheck(cb) {
  if (cb.checked) selectedBranches.add(cb.value);
  else selectedBranches.delete(cb.value);
  updateBranchButtonText();
  renderTable();
}

function selectAllBranches() {
  document.querySelectorAll('#branchOptions input[type=checkbox]').forEach(cb => {
    cb.checked = true;
    selectedBranches.add(cb.value);
  });
  updateBranchButtonText();
  renderTable();
}

function clearAllBranches() {
  document.querySelectorAll('#branchOptions input[type=checkbox]').forEach(cb => {
    cb.checked = false;
  });
  selectedBranches.clear();
  updateBranchButtonText();
  renderTable();
}

function updateBranchButtonText() {
  const all = [...new Set(allCourses.map(c => c.branch))];
  const sel = [...selectedBranches];
  const btn = document.getElementById('branchToggleBtn');
  if (sel.length === 0) btn.textContent = '📍 (未選分會)';
  else if (sel.length === all.length) btn.textContent = `📍 全部分會 (${all.length})`;
  else btn.textContent = `📍 ${sel.join(', ')}`;
}

// 點外面關閉下拉
document.addEventListener('click', function(e) {
  const box = document.getElementById('branchMultiBox');
  if (box && !box.contains(e.target)) {
    document.getElementById('branchDropdown').style.display = 'none';
  }
  const catBox = document.getElementById('categoryMultiBox');
  if (catBox && !catBox.contains(e.target)) {
    document.getElementById('categoryDropdown').style.display = 'none';
  }
  const clsBox = document.getElementById('classMultiBox');
  if (clsBox && !clsBox.contains(e.target)) {
    document.getElementById('classDropdown').style.display = 'none';
  }
});

// === Commit 15: 單獨更新某一個協會 (不影響其他) ===
async function updateOnly(code) {
  const card = document.getElementById('inst' + code.charAt(0).toUpperCase() + code.slice(1));
  const label = card ? card.querySelector('.label').textContent : code;
  if (!confirm(`只更新「${label}」?\n\n其他協會的資料會保留不動。`)) return;
  showLoading(`正在抓取 ${label}...`);
  const progressTimer = setInterval(async () => {
    try {
      const r = await fetch('/api/update_progress');
      const p = await r.json();
      if (p.stage === 'list') {
        document.getElementById('loadingText').innerHTML =
          `📋 ${p.message}<br><small style="opacity:0.7;">${p.current}/${p.total}</small>`;
      } else if (p.stage === 'details') {
        const pct = p.total > 0 ? Math.round(p.current / p.total * 100) : 0;
        document.getElementById('loadingText').innerHTML =
          `📍 ${p.message}<br>
           <small style="opacity:0.7;">${p.current}/${p.total} (${pct}%)</small><br>
           <div style="background:#eee;border-radius:8px;height:8px;margin-top:8px;overflow:hidden;width:280px;">
             <div style="background:linear-gradient(90deg,#4FB3BF,#87BDD8);height:100%;width:${pct}%;transition:width 0.3s;"></div>
           </div>`;
      }
    } catch(e) {}
  }, 1000);
  try {
    const resp = await fetch('/api/update', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({scrapers: [code]})
    });
    if (resp.status === 401) { window.location.href = '/login'; return; }
    const data = await resp.json();
    if (data.ok) {
      toast(`✓ ${label} 更新完成! 共 ${data.count} 筆課程`, 'success');
      await loadCourses(); await loadScrapers();      
    }
  } catch (e) {
    toast('更新失敗: ' + e.message, 'error');
  } finally {
    clearInterval(progressTimer);
    hideLoading();
  }
}

async function startUpdate(forceRefresh = false) {
  showLoading(forceRefresh ? '⚡ 強制全抓中(較慢,請耐心等候)...' : '正在抓取課程資料...');
  
  // 啟動 polling 取得進度
  const progressTimer = setInterval(async () => {
    try {
      const r = await fetch('/api/update_progress');
      const p = await r.json();
      if (p.stage === 'list') {
        document.getElementById('loadingText').innerHTML = 
          `📋 ${p.message}<br><small style="opacity:0.7;">第 ${p.current}/${p.total} 個分會</small>`;
      } else if (p.stage === 'details') {
        const pct = p.total > 0 ? Math.round(p.current / p.total * 100) : 0;
        document.getElementById('loadingText').innerHTML = 
          `📍 抓取詳細上課地點中...<br>
           <small style="opacity:0.7;">${p.current}/${p.total} (${pct}%)</small><br>
           <div style="background:#eee;border-radius:8px;height:8px;margin-top:8px;overflow:hidden;width:280px;">
             <div style="background:linear-gradient(90deg,#4FB3BF,#87BDD8);height:100%;width:${pct}%;transition:width 0.3s;"></div>
           </div>
           <small style="opacity:0.6;font-size:11px;margin-top:6px;display:block;">${forceRefresh ? '強制全抓:約 8-10 分鐘' : '一般更新:約 3-5 分鐘(cache 生效後)'},請耐心等候</small>`;
      }
    } catch(e) {}
  }, 1000);
  
  try {
// 收集所有勾選的協會
    const scraperCodes = [...document.querySelectorAll('.inst-card.checked')]
      .map(card => card.id.replace('inst', '').toLowerCase())
      .filter(Boolean);
    if (scraperCodes.length === 0) {
      toast('請至少勾選一個協會', 'error');
      clearInterval(progressTimer);
      hideLoading();
      return;
    }
    const resp = await fetch('/api/update', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({scrapers: scraperCodes, force_refresh: forceRefresh})
    });
    
    if (resp.status === 401) { window.location.href = '/login'; return; }
    const data = await resp.json();
    if (data.ok) {
      toast(`✓ 更新完成! 共 ${data.count} 筆課程${forceRefresh ? ' (強制全抓)' : ''}`, 'success');
      await loadCourses(); await loadScrapers();     
    }
  } catch (e) {
    toast('更新失敗: ' + e.message, 'error');
  } finally {
    clearInterval(progressTimer);
    hideLoading();
  }
}

// === 日期/時數 格式化 helpers (commit 14) ===
function _formatDateWithWeekday(s) {
  if (!s) return '';
  const m = s.match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (!m) return s;
  if (/\([一二三四五六日]\)$/.test(s)) return s;  // 已有星期就保留原樣
  const [, yy, mm, dd] = m;
  const dt = new Date(parseInt(yy), parseInt(mm)-1, parseInt(dd));
  if (isNaN(dt.getTime())) return s;
  return `${yy}-${mm}-${dd}(${['日','一','二','三','四','五','六'][dt.getDay()]})`;
}
function _formatDateRange(c) {
  const s = _formatDateWithWeekday(c.start_date || '');
  const e = _formatDateWithWeekday(c.end_date || '');
  if (!s) return '';
  if (!e) return s;
  const sP = s.replace(/\([^)]*\)$/, '');
  const eP = e.replace(/\([^)]*\)$/, '');
  if (sP === eP) return s;
  return `${s} 至 ${e}`;
}
function _normalizeHours(h) {
  if (!h) return '';
  const n = parseFloat(h);
  if (isNaN(n)) return String(h);
  return n % 1 === 0 ? String(Math.round(n)) : String(n);
}

let natFilter = '';
let statFilter = '';
const NAT_OPTIONS = [['', '全部'], ['本國', '只看本國籍'], ['外籍', '只看外籍'], ['未標註', '未標註']];
const STAT_OPTIONS = [['', '全部'], ['open', '可報名(含候補)'], ['full', '已額滿(無候補)']];

function renderNatChips() {
  document.getElementById('natChips').innerHTML = NAT_OPTIONS.map(([v, label]) =>
    `<button class="chip ${v === '' ? 'chip-all' : ''} ${natFilter === v ? 'on' : ''}" onclick="setNatFilter('${v}')">${label}</button>`
  ).join('');
}
function setNatFilter(v) { natFilter = v; renderNatChips(); renderTable(); }

function renderStatChips() {
  document.getElementById('statChips').innerHTML = STAT_OPTIONS.map(([v, label]) =>
    `<button class="chip ${v === '' ? 'chip-all' : ''} ${statFilter === v ? 'on' : ''}" onclick="setStatFilter('${v}')">${label}</button>`
  ).join('');
}
function setStatFilter(v) { statFilter = v; renderStatChips(); renderTable(); }

let selectedCategories = new Set();
const CAT_OPTIONS = [['複訓','複訓'],['初訓','初訓'],['輻射','輻射(全部)'],['3小時','↳ 輻射3小時'],['18小時','↳ 輻射18小時'],['36小時','↳ 輻射36小時']];
function renderCategoryChips() {
  const allOn = selectedCategories.size === 0;
  let html = `<button class="chip chip-all ${allOn?'on':''}" onclick="setCategoryAll()">全部</button>`;
  html += CAT_OPTIONS.map(([v, label]) =>
    `<button class="chip ${selectedCategories.has(v)?'on':''}" onclick="toggleCategoryChip('${v}')">${label}</button>`
  ).join('');
  document.getElementById('categoryChips').innerHTML = html;
}
function toggleCategoryChip(v) {
  if (selectedCategories.has(v)) selectedCategories.delete(v);
  else selectedCategories.add(v);
  renderCategoryChips(); renderTable();
}
function setCategoryAll() {
  selectedCategories.clear();
  renderCategoryChips(); renderTable();
}

function renderTable() {
  const kw = document.getElementById('searchKw').value.toLowerCase();
  const institute = document.getElementById('filterInstitute').value;

  const nat = natFilter;
  const stat = statFilter;


  let visible = allCourses;
  if (kw) {
    const groups = kw.split(/[,，]/).map(g => g.trim().split(/\s+/).filter(Boolean)).filter(g => g.length);
    visible = visible.filter(c => {
      const h = `${c.name||''} ${c.nationality||''} ${c.category||''} ${c.branch||''} ${c.institute||''} ${c.class_type||''}`.toLowerCase();
      return groups.some(group => group.every(term => h.includes(term)));
    });
  }
  if (institute) visible = visible.filter(c => c.institute === institute);
  if (selectedBranches.size > 0) {
    visible = visible.filter(c => selectedBranches.has(c.branch));
  }
  if (selectedCategories.size > 0) {
      visible = visible.filter(c => 
        [...selectedCategories].some(sel => /^\d+小時$/.test(sel) ? (c.category === '輻射' && (c.name || '').includes(sel)) : (c.category === sel))
      );
    }
  if (nat === '本國') visible = visible.filter(c => /本國/.test(c.nationality || ''));
  else if (nat === '外籍') visible = visible.filter(c => /外|越|印|菲|泰/.test(c.nationality || ''));
  else if (nat === '未標註') visible = visible.filter(c => !c.nationality);
  if (stat === 'open') visible = visible.filter(c => { const s = c.status || ''; if (!s) return true; if (/截止|結訓|停辦|取消|結束/.test(s)) return false; if (/額滿/.test(s) && !/候補/.test(s)) return false; return true; });
  if (stat === 'full') visible = visible.filter(c => /額滿/.test(c.status || '') && !/候補/.test(c.status || ''));
  if (selectedClasses.size > 0) {
    visible = visible.filter(c => selectedClasses.has((c.class_type || '').trim() || '__blank__'));
  }

  // 按開課日升冪排序 (近的在前)
  visible.sort((a, b) => (a.start_date || '9999').localeCompare(b.start_date || '9999'));

  document.getElementById('totalCount').textContent = allCourses.length;
  document.getElementById('visCount').textContent = visible.length;
  document.getElementById('selCount').textContent = selected.size;

  const tbody = document.getElementById('tbody');
  if (visible.length === 0) {
    tbody.innerHTML = '<tr><td colspan="10" style="text-align:center;padding:50px;color:#999;cursor:default;">沒有符合條件的課程</td></tr>';
    return;
  }
  tbody.innerHTML = visible.map(c => {
    const isSel = selected.has(c.id);
    const dateStr = _formatDateRange(c);
    const hoursStr = _normalizeHours(c.hours);
    const classCell = c.class_time
      ? `${escHtml(c.class_type)}<br><small style="color:#888;font-size:11px;">${escHtml(c.class_time)}</small>`
      : escHtml(c.class_type);
    return `<tr class="${isSel?'selected':''}" onclick="toggleSel('${escHtml(c.id)}')">
      <td style="text-align:center;"><div class="big-check">${isSel?'✓':''}</div></td>
      <td><strong>${escHtml(c.name)}</strong></td>
      <td>${escHtml(c.branch)}</td>
      <td>${catBadge(c.category)}</td>
      <td>${natBadge(c.nationality)}</td>
      <td>${escHtml(dateStr)}</td>
      <td>${classCell}</td>
      <td style="text-align:center;">${escHtml(hoursStr)}</td>
      <td style="text-align:right;">${c.fee ? (String(c.fee).endsWith('元') ? escHtml(c.fee) : escHtml(c.fee) + ' 元') : '免費'}</td>
      <td>${statBadge(c.status)}</td>
    </tr>`;
  }).join('');
}

function catBadge(c) {
  if (c === '初訓') return '<span class="badge badge-cat-init">初訓</span>';
  if (c === '複訓') return '<span class="badge badge-cat-re">複訓</span>';
  if (c === '輻射') return '<span class="badge" style="background:#FEF3C7;color:#92400E;border:1px solid #F59E0B;">☢ 輻射</span>';
  return `<span class="badge badge-other">${c||'?'}</span>`;
}
function natBadge(n) {
  if (n === '本國籍') return '<span class="badge badge-nat-tw">本國</span>';
  if (n) return `<span class="badge badge-nat-fg">${n.replace('籍','')}</span>`;
  return '';
}
function statBadge(s) {
  if (!s) return '<span class="badge badge-other">-</span>';
  if (/額滿/.test(s)) return `<span class="badge badge-full">${s}</span>`;
  if (/確定/.test(s)) return `<span class="badge badge-open">${s}</span>`;
  if (/招生|強力/.test(s)) return `<span class="badge badge-promo">${s}</span>`;
  return `<span class="badge badge-other">${s}</span>`;
}

function toggleSel(id) {
  const wasEmpty = selected.size === 0;
  if (selected.has(id)) selected.delete(id);
  else { const c = allCourses.find(x => x.id === id); if (c) selected.set(id, c); }
  renderTable();
  renderSelected();
  // 第一次勾選時自動捲動到③
  if (wasEmpty && selected.size === 1) {
    document.getElementById('selectedList').scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
}

function clearSelected() {
  if (selected.size === 0) return;
  if (!confirm('確定清空所有已挑選的課程嗎？')) return;
  selected.clear();
  renderTable();
  renderSelected();
}

function clearForm() {
  if (!confirm('確定清空表單嗎？(同仁、月份、課程名稱、已挑選課程都會清掉)')) return;
  document.getElementById('empList').innerHTML = '';
  addEmployee();
  document.getElementById('empMonth').value = '';
  document.getElementById('courseName').value = '';
  selected.clear();
  renderTable();
  renderSelected();
  toast('表單已清空', 'success');
}

function renderSelected() {
  const wrap = document.getElementById('selectedList');
  if (selected.size === 0) {
    wrap.innerHTML = '<div style="color:#999;text-align:center;padding:10px;">尚未挑選任何課程</div>';
    return;
  }
  const header = `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;padding:0 4px;">
    <span style="font-weight:700;color:#2980d4;font-size:14px;">已挑選 ${selected.size} 門課程</span>
    <button onclick="clearSelected()" onmouseover="this.style.background='#C44569';this.style.color='white';" onmouseout="this.style.background='white';this.style.color='#C44569';" style="background:white;border:1.5px solid #FFB8B8;color:#C44569;padding:6px 14px;border-radius:8px;cursor:pointer;font-family:inherit;font-size:13px;font-weight:700;transition:all 0.15s;">🗑 清空全部</button>
  </div>`;
  wrap.innerHTML = header + [...selected.values()].map((c, i) => `
    <div class="sel-item">
      <div class="info">
        <b>${i+1}. ${escHtml(c.name)}</b>
        <div class="meta">
          📍 ${escHtml(c.branch)} ｜ ${escHtml(c.category)} ｜ ${escHtml(c.start_date)} ${escHtml(c.class_type)} ｜ ${escHtml(c.location)} ｜ ${c.fee ? (String(c.fee).endsWith('元') ? escHtml(c.fee) : escHtml(c.fee) + ' 元') : '免費'}
        </div>
      </div>
      <button class="remove-btn" onclick="toggleSel('${escHtml(c.id)}')">✕ 移除</button>
    </div>
  `).join('');
}

function escHtml(s) {
  return String(s||'').replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
}

async function buildEmail(mode) {
  const _m = document.getElementById('empMonth').value.trim();
  if (_m && (isNaN(_m) || +_m < 1 || +_m > 12)) {
    toast('預訂月份請填 1–12 的數字', 'error');
    return null;
  }
  // 收集多位同仁(每人各自部門)
  const employees = Array.from(document.querySelectorAll('#empList .emp-row')).map(row => ({
    name: row.querySelector('.emp-name-input').value.trim(),
    dept: row.querySelector('.emp-dept-input').value.trim(),
  })).filter(e => e.name);  // 過濾掉沒填名字的空列
  const body = {
    employees: employees,
    emp_month: document.getElementById('empMonth').value || '(月)',
    course_name: document.getElementById('courseName').value || '(課程名稱)',
    train_type: document.getElementById('trainType').value,
    selected_courses: [...selected.values()],
    mode: mode,
  };

  const resp = await fetch('/api/email', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });
  if (resp.status === 401) { window.location.href = '/login'; return null; }
  return await resp.json();
}

async function copyFromPreview() {
  const html = document.getElementById('prevBody').innerHTML;
  try {
    const blob = new Blob([html], {type: 'text/html'});
    await navigator.clipboard.write([new ClipboardItem({'text/html': blob})]);
    toast('✓ 已複製！打開 Outlook 按 Ctrl+V 即可貼上', 'success');
  } catch(e) {
    await navigator.clipboard.writeText(html);
    toast('已複製 (HTML 原始碼)', 'success');
  }
}

async function previewExternal() { await preview('external', '信件'); }

async function preview(mode, label) {
  const e = await buildEmail(mode);
  if (!e) return;
  document.getElementById('prevMode').textContent = label;
  document.getElementById('prevSubject').textContent = e.subject;
  document.getElementById('prevBody').innerHTML = e.html;
  document.getElementById('previewModal').classList.add('show');
}

async function generateExternal() { await generate('external', '信件'); }

async function generate(mode, label) {
  if (selected.size === 0) { toast('請先挑選至少一個課程', 'error'); return; }
  const e = await buildEmail(mode);
  if (!e) return;
  try {
    const blob = new Blob([e.html], {type: 'text/html'});
    const data = [new ClipboardItem({'text/html': blob})];
    await navigator.clipboard.write(data);
    toast(`✓ ${label}版已複製! 打開 Outlook 按 Ctrl+V 即可貼上`, 'success');
  } catch (err) {
    await navigator.clipboard.writeText(e.html);
    toast('已複製 (HTML 原始碼)', 'success');
  }
}

// === 多同仁列表管理 ===
function addEmployee(name = '', dept = '') {
  const list = document.getElementById('empList');
  const row = document.createElement('div');
  row.className = 'emp-row';
  row.style.cssText = 'display:flex;gap:6px;align-items:center;';
  row.innerHTML = `
    <input type="text" class="emp-name-input" placeholder="姓名,例: 王小明" value="${escHtml(name)}" style="flex:1;">
    <input type="text" class="emp-dept-input" placeholder="部門,例: 訓練單位" value="${escHtml(dept)}" style="flex:1;">
    <button type="button" onclick="removeEmployee(this)" onmouseover="this.style.background='#C44569';this.style.color='white';" onmouseout="this.style.background='#FFE5E5';this.style.color='#C44569';" style="background:#FFE5E5;border:none;color:#C44569;width:36px;height:36px;border-radius:10px;cursor:pointer;font-size:16px;font-weight:bold;flex-shrink:0;transition:all 0.2s;" title="刪除這位同仁">✕</button>
  `;
  list.appendChild(row);
}
function removeEmployee(btn) {
  const row = btn.closest('.emp-row');
  if (row) row.remove();
  if (document.querySelectorAll('#empList .emp-row').length === 0) addEmployee();
}
addEmployee(); // 預設先放一列空的

loadScrapers();
loadCourses();
refreshOnline();
</script>

</body>
</html>
"""


def open_browser():
    time.sleep(1.2)
    webbrowser.open(f"http://localhost:{PORT}")


if __name__ == "__main__":
    # 雲端部署: PORT 從環境變數讀取 (Render/Heroku/Railway 都這樣)
    port = int(os.environ.get("PORT", PORT))
    is_cloud = "PORT" in os.environ  # 雲端環境會設定 PORT
    
    print("=" * 60)
    print("  外訓課程自動整合工具")
    print("=" * 60)
    
    try:
        init_db()
        print("  [OK] 資料庫初始化完成")
    except Exception as e:
        print(f"  [ERROR] 資料庫初始化失敗: {e}")
        import traceback
        traceback.print_exc()
    
    # 本機才產生「密碼.txt」(雲端不需要,因為沒人看得到雲端檔案)
    if not is_cloud:
        try:
            pwd_file = APP_DIR / "密碼.txt"
            if not pwd_file.exists():
                with open(pwd_file, "w", encoding="utf-8") as f:
                    f.write("【外訓課程整合工具 - 帳號密碼】\n")
                    f.write("=" * 40 + "\n\n")
                    f.write("⚠️  此檔案請勿給其他人看!\n\n")
                    for u in DEFAULT_USERS:
                        f.write(f"帳號: {u['username']}\n")
                        f.write(f"密碼: {u['password']}\n")
                        f.write(f"角色: {u['display_name']} ({u['role']})\n")
                        f.write("-" * 40 + "\n")
                print(f"  [OK] 已產生 [密碼.txt] (請妥善保管)")
        except Exception as e:
            print(f"  [WARN] 無法寫入密碼.txt (略過): {e}")
    
    print("")
    if is_cloud:
        print(f"  [Cloud] 雲端模式啟動,監聽 port {port}")
    else:
        print(f"  伺服器啟動中... http://localhost:{port}")
        print(f"  同事連線網址: http://[你的 IP]:{port}")
        print(f"  (查你的 IP: 開新 cmd → 輸入 ipconfig)")
        print(f"  關閉程式: 按 Ctrl+C 或關閉此視窗")
    print("=" * 60)
    
    # 只在本機才開瀏覽器,雲端不開
    if not is_cloud:
        threading.Thread(target=open_browser, daemon=True).start()
    
    app.run(host="0.0.0.0", port=port, debug=False)


# 雲端 (gunicorn) 不會跑 if __name__ == "__main__",所以下面這段是雲端用的
# gunicorn 直接 import app:app,需要先初始化 DB
try:
    init_db()
    init_activity_log()
    print("[Cloud Init] 雲端模式,資料庫初始化完成")
except Exception as e:
    print(f"[Cloud Init ERROR] {e}")
    import traceback
    traceback.print_exc()
