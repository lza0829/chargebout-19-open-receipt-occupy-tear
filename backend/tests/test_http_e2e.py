"""HTTP 端到端 + 旧库迁移验证（真实 uvicorn + urllib，无额外依赖）。"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uvicorn

_tmp = tempfile.mkdtemp(prefix="chargebout_http_")
DB_FILE = os.path.join(_tmp, "http.db")
os.environ["CHARGEBOUT_DB"] = DB_FILE
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


BASE = "http://127.0.0.1:11599"


def call(method: str, path: str, body=None):
    # urllib 要求非 ASCII 路径逐段百分号编码（查询串同样处理，保留 = & ?）
    quoted = urllib.parse.quote(path, safe="/?=&")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + quoted, data=data,
        headers={"Content-Type": "application/json"}, method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or "null")
    except urllib.error.HTTPError as ex:
        raw = ex.read()
        try:
            return ex.code, json.loads(raw)
        except Exception:
            return ex.code, {"raw": raw.decode()}


config = uvicorn.Config(app, host="127.0.0.1", port=11599, log_level="warning")
server = uvicorn.Server(config)
threading.Thread(target=server.run, daemon=True).start()
for _ in range(50):
    try:
        urllib.request.urlopen(BASE + "/api/健康", timeout=1).read()
        break
    except Exception:
        time.sleep(0.1)

print("== A. HTTP 正常/异常/拒绝三通道 ==")
_, piles = call("GET", "/api/桩位")
check("桩位带 openable 谓词标记", all("openable" in p for p in piles))
idle = next(p for p in piles if p["status"] == "空闲")
maint = next(p for p in piles if p["status"] == "维护")

s1, b1 = call("POST", "/api/开会话", {"桩位编号": idle["id"]})
check("空闲桩开充 200", s1 == 200, str(b1))
check("回执正常无告警", b1["是否异常开充"] is False and b1["告警"] == "", str(b1))

s2, b2 = call("POST", "/api/开会话", {"桩位编号": idle["id"]})
check("占用桩再开充仍 200（可告警仍开充）", s2 == 200, str(b2))
check("回执带告警与开前快照", b2["是否异常开充"] is True and b2["桩位开前态"] == "占用"
      and b2["异常原因"], str(b2))

s3, b3 = call("POST", "/api/开会话", {"桩位编号": maint["id"]})
check("维护桩开充 200 且异常", s3 == 200 and b3["是否异常开充"] is True, str(b3))

call("POST", "/api/充日/封印", {"备注": "封"})
s4, b4 = call("POST", "/api/开会话", {"桩位编号": idle["id"]})
check("封印开充 400", s4 == 400 and "封印" in b4["detail"], str(b4))
call("POST", "/api/充日/解封", {})

_, audits = call("GET", "/api/开充审计")
check("审计接口可查", len(audits) >= 3)
rej = [a for a in audits if a["result"] == "拒绝"]
check("拒绝尝试已补登审计且无会话号", rej and rej[0]["slip_no"] == "" and rej[0]["session_id"] is None,
      str(rej[:1]))
abn = [a for a in audits if a["abnormal"] == 1]
check("异常开充审计可核且带快照", len(abn) >= 2 and all(a["snapshot_pile_status"] for a in abn))

_, ov = call("GET", "/api/总览")
check("总览暴露异常占用与对账", ov["异常占用在充数"] >= 2 and ov["对账"]["全部一致"] is True,
      json.dumps(ov["对账"], ensure_ascii=False))
check("总览含开充通过/拒绝计数", ov["本日开充通过"] >= 3 and ov["本日开充拒绝"] >= 1)

print("== B. 真实 uvicorn 下 5 并发抢同一空闲桩 ==")
_, plist = call("GET", "/api/桩位")
fresh_id = next(p["id"] for p in plist if p["status"] == "空闲")
outcomes = []
barrier = threading.Barrier(5)


def post_open():
    barrier.wait()
    outcomes.append(call("POST", "/api/开会话", {"桩位编号": fresh_id}))


threads = [threading.Thread(target=post_open) for _ in range(5)]
for t in threads:
    t.start()
for t in threads:
    t.join()

check("5 个请求全部 200", len(outcomes) == 5 and all(o[0] == 200 for o in outcomes), str(outcomes))
normal = [o for o in outcomes if not o[1]["是否异常开充"]]
abnormal = [o for o in outcomes if o[1]["是否异常开充"]]
check("恰好 1 笔正常破空闲、4 笔异常", len(normal) == 1 and len(abnormal) == 4,
      f"normal={len(normal)} abnormal={len(abnormal)}")
_, pnow_list = call("GET", "/api/桩位")
pnow = next(p for p in pnow_list if p["id"] == fresh_id)
check("该桩最终占用且只破一次", pnow["status"] == "占用", pnow["status"])
_, ov2 = call("GET", "/api/总览")
check("并发后总览对账一致", ov2["对账"]["全部一致"] is True, json.dumps(ov2["对账"], ensure_ascii=False))
server.should_exit = True

print("== C. 旧库（无异常列/无审计表）在线迁移 ==")
old_db = os.path.join(_tmp, "old.db")
conn = sqlite3.connect(old_db)
conn.executescript(
    """
    CREATE TABLE sites(id INTEGER PRIMARY KEY, name TEXT UNIQUE, address TEXT DEFAULT '', active INT DEFAULT 1);
    CREATE TABLE rate_plans(id INTEGER PRIMARY KEY, name TEXT UNIQUE, peak_price REAL, flat_price REAL, valley_price REAL, active INT DEFAULT 1);
    CREATE TABLE piles(id INTEGER PRIMARY KEY, site_id INT, name TEXT, rated_kw REAL DEFAULT 7, status TEXT DEFAULT '空闲', rate_plan_id INT);
    CREATE TABLE guns(id INTEGER PRIMARY KEY, pile_id INT, name TEXT, status TEXT DEFAULT '可用');
    CREATE TABLE sessions(id INTEGER PRIMARY KEY, slip_no TEXT UNIQUE, site_id INT, pile_id INT, gun_id INT,
        rate_plan_id INT, business_day TEXT, status TEXT, kwh REAL DEFAULT 0, amount REAL DEFAULT 0,
        price_locked REAL, started_at TEXT, closed_at TEXT, note TEXT DEFAULT '');
    CREATE TABLE pulses(id INTEGER PRIMARY KEY, session_id INT, delta_kwh REAL, power_kw REAL DEFAULT 0, created_at TEXT, note TEXT DEFAULT '');
    CREATE TABLE session_events(id INTEGER PRIMARY KEY, session_id INT, action TEXT, created_at TEXT, note TEXT DEFAULT '');
    CREATE TABLE day_seals(id INTEGER PRIMARY KEY, business_day TEXT UNIQUE, sealed INT DEFAULT 0, sealed_at TEXT, note TEXT DEFAULT '');
    CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT);
    INSERT INTO sites VALUES(1,'老站','',1);
    INSERT INTO rate_plans VALUES(1,'老费率',1,0.8,0.4,1);
    INSERT INTO piles VALUES(1,1,'老桩',7,'空闲',1);
    INSERT INTO guns VALUES(1,1,'主枪','可用');
    """
)
conn.commit()
conn.close()

import app.config as config_mod  # noqa: E402
from pathlib import Path  # noqa: E402
config_mod.DB_PATH = Path(old_db)
import app.db as dbmod  # noqa: E402
dbmod.DB_PATH = Path(old_db)
dbmod.init_db()
with dbmod.db() as c:
    cols = {r["name"] for r in c.execute("PRAGMA table_info(sessions)")}
    check("迁移补齐 abnormal_open/open_reason", {"abnormal_open", "open_reason"} <= cols, str(cols))
    tables = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    check("迁移新建 open_audits", "open_audits" in tables, str(tables))
    check("老数据保留", c.execute("SELECT name FROM piles WHERE id=1").fetchone()["name"] == "老桩")
    from app.services import sessions as svc  # noqa: E402
    rr = svc.open_session(c, 1)
    check("迁移库可开充且正常", rr["是否异常开充"] is False, str(rr))

print(f"\n结果：PASS={PASS}  FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
