"""开充成功灯/空闲灯撕裂夹具回归（架构：可告警仍开充 + 四者同命运）。

覆盖题面夹具：
  1) 单号绿空闲灯仍亮
  2) 占用钉会话缺
  3) 审计成功总览未加
  4) 同桩连点半截
  5) 开充与急停交错：回执绿而落账回滚
真实 uvicorn + 独立临时库；并额外直插脏写验证缺口可被识破而非被读路径抹平。
"""
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

_tmp = tempfile.mkdtemp(prefix="chargebout_tear_")
DB_FILE = os.path.join(_tmp, "tear.db")
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


BASE = "http://127.0.0.1:11600"


def call(method: str, path: str, body=None):
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


def db_exec(sql: str, args=()):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


config = uvicorn.Config(app, host="127.0.0.1", port=11600, log_level="warning")
server = uvicorn.Server(config)
threading.Thread(target=server.run, daemon=True).start()
for _ in range(50):
    try:
        urllib.request.urlopen(BASE + "/api/健康", timeout=1).read()
        break
    except Exception:
        time.sleep(0.1)

print("== 1. 单号绿：成功回执必须与空闲灯熄灭同命运 ==")
_, piles = call("GET", "/api/桩位")
idle = next(p for p in piles if p["status"] == "空闲")
status, r = call("POST", "/api/开会话", {"桩位编号": idle["id"]})
check("开充 200", status == 200, str(r))
check("回执声明成功且四者同命运", r.get("成功") is True and r.get("四者同命运") is True, str(r))
check("回执带桩/枪/会话/审计证据", all(k in (r.get("证据") or {})
      for k in ("桩位占用钉", "枪线占用钉", "会话行", "审计行")), str(r.get("证据")))
_, piles2 = call("GET", "/api/桩位")
p = next(x for x in piles2 if x["id"] == idle["id"])
check("单号绿后空闲灯必须灭", p["idle_lamp"] is False and p["status"] == "占用", str(p))
check("桩位页证据不撕裂", p["evidence_conflict"] is False, str(p))
_, sessions = call("GET", "/api/会话")
sid = r["会话编号"]
se = next(s for s in sessions if s["id"] == sid)
check("会话册出现充电中行且钉住", se["status"] == "充电中" and se["evidence_conflict"] is False, str(se))
_, audits = call("GET", "/api/开充审计")
au = next(a for a in audits if a.get("session_id") == sid)
check("审计通过行可重放同一会话", au["result"] == "通过" and au["replayable"] is True, str(au))
_, ov = call("GET", "/api/总览")
check("总览已开充+1 且证据完整", ov["已开充(证据完整)"] >= 1 and ov["已开充证据撕裂"] == 0, str(ov))
check("总览对账全部一致", ov["对账"]["全部一致"] is True, json.dumps(ov["对账"], ensure_ascii=False))

print("== 2. 占用钉会话缺：幽灵占用必须被识破，不得读成已开充 ==")
_, piles3 = call("GET", "/api/桩位")
free = next(p for p in piles3 if p["status"] == "空闲")
_, ov_before2 = call("GET", "/api/总览")
charging_before2 = ov_before2["已开充(证据完整)"]
db_exec("UPDATE piles SET status='占用' WHERE id=?", (free["id"],))
_, ov2 = call("GET", "/api/总览")
check("无会话占用桩数>0", ov2["对账"]["无会话占用桩数"] >= 1, json.dumps(ov2["对账"], ensure_ascii=False))
check("对账不再宣称全部一致", ov2["对账"]["全部一致"] is False)
check("缺口册列出该桩", any(g["对象"] == "桩位" and g["编号"] == free["id"] for g in ov2["开充缺口"]),
      str(ov2["开充缺口"]))
_, piles4 = call("GET", "/api/桩位")
pfree = next(x for x in piles4 if x["id"] == free["id"])
check("桩位页标出证据撕裂", pfree["evidence_conflict"] is True, str(pfree))
check("幽灵占用不计入已开充", ov2["已开充(证据完整)"] == charging_before2,
      f"before={charging_before2} after={ov2['已开充(证据完整)']}")
db_exec("UPDATE piles SET status='空闲' WHERE id=?", (free["id"],))

print("== 3. 审计成功总览未加：审计绿但会话缺必须可见 ==")
conn = sqlite3.connect(DB_FILE)
conn.row_factory = sqlite3.Row
pile_row = conn.execute("SELECT * FROM piles WHERE status='空闲' LIMIT 1").fetchone()
conn.close()
db_exec(
    """INSERT INTO open_audits(created_at,business_day,pile_id,gun_id,slip_no,result,
                               abnormal,snapshot_pile_status,snapshot_gun_status,reason,session_id)
       VALUES(datetime('now','localtime'),date('now','localtime'),?,?,?,'通过',0,'空闲','可用','',NULL)""",
    (pile_row["id"], None, "充X999"),
)
_, ov3 = call("GET", "/api/总览")
check("审计通过无会话数>0", ov3["对账"]["审计通过无会话数"] >= 1,
      json.dumps(ov3["对账"], ensure_ascii=False))
check("总览不再全部一致", ov3["对账"]["全部一致"] is False)
_, audits3 = call("GET", "/api/开充审计")
ghost = [a for a in audits3 if a["slip_no"] == "充X999"]
check("该审计行标不可重放", ghost and ghost[0]["replayable"] is False, str(ghost[:1]))
check("审计绿未被算成已开充", ov3["已开充(证据完整)"] >= 1)  # 只算真实钉住的会话
db_exec("DELETE FROM open_audits WHERE slip_no='充X999'")

print("== 4. 同桩连点半截：回滚后重开不得一绿一僵 ==")
_, pl = call("GET", "/api/桩位")
target = next(p for p in pl if p["status"] == "空闲")
s1, b1 = call("POST", "/api/开会话", {"桩位编号": target["id"]})
check("第一笔成功", s1 == 200 and b1.get("成功") is True, str(b1))
# 模拟半截：把审计与占用回滚而会话行残留（等价于陈旧旁路）
conn = sqlite3.connect(DB_FILE)
conn.row_factory = sqlite3.Row
sid1 = conn.execute("SELECT id FROM sessions WHERE pile_id=? AND status='充电中'", (target["id"],)).fetchone()["id"]
conn.close()
db_exec("DELETE FROM open_audits WHERE session_id=?", (sid1,))
_, ov4 = call("GET", "/api/总览")
check("半截残留被识别为缺口", ov4["对账"]["会话无通过审计数"] >= 1
      and ov4["对账"]["全部一致"] is False, json.dumps(ov4["对账"], ensure_ascii=False))
# 清理残留后再连点同一桩
call("POST", f"/api/会话/{sid1}/中断", {"原因": "夹具复位"})
s2, b2 = call("POST", "/api/开会话", {"桩位编号": target["id"]})
check("复位后重开成功", s2 == 200 and b2.get("成功") is True, str(b2))
_, pl2 = call("GET", "/api/桩位")
tp = next(p for p in pl2 if p["id"] == target["id"])
check("重开后桩占用且空闲灯灭（不僵）", tp["status"] == "占用" and tp["idle_lamp"] is False, str(tp))
_, ov5 = call("GET", "/api/总览")
check("重开后对账恢复一致（不留半截）", ov5["对账"]["全部一致"] is True,
      json.dumps(ov5["对账"], ensure_ascii=False))

print("== 5. 开充/急停交错：回执绿不得伴随落账回滚 ==")
_, pl3 = call("GET", "/api/桩位")
race_pile = next(p for p in pl3 if p["status"] == "空闲")
results = []
barrier = threading.Barrier(2)


def opener():
    barrier.wait()
    results.append(("open", *call("POST", "/api/开会话", {"桩位编号": race_pile["id"]})))


def aborter():
    # 等开充先落一笔，再急停；与开充入口交错
    barrier.wait()
    time.sleep(0.02)
    _, cur = call("GET", "/api/会话")
    live = [s for s in cur if s["status"] == "充电中"]
    if live:
        results.append(("abort", *call("POST", f"/api/会话/{live[0]['id']}/中断", {"原因": "夹具急停"})))
    else:
        results.append(("abort", 0, {"skipped": True}))


t1 = threading.Thread(target=opener)
t2 = threading.Thread(target=aborter)
t1.start(); t2.start(); t1.join(); t2.join()
opens = [r for r in results if r[0] == "open"]
check("开充请求成功返回", opens and opens[0][1] == 200 and opens[0][2].get("成功") is True, str(opens))
_, ov6 = call("GET", "/api/总览")
check("交错后对账全部一致（无幽灵占用/无绿而无账）", ov6["对账"]["全部一致"] is True,
      json.dumps(ov6["对账"], ensure_ascii=False))
check("交错后无在用撕裂", ov6["已开充证据撕裂"] == 0, str(ov6))
_, pl4 = call("GET", "/api/桩位")
rp = next(p for p in pl4 if p["id"] == race_pile["id"])
check("交错后桩态与空闲灯自洽", (rp["status"] == "空闲") == (rp["idle_lamp"] is True), str(rp))

print("== 6. 脉冲入口不得在证据撕裂时误亮 ==")
_, pl5 = call("GET", "/api/桩位")
pp = next(p for p in pl5 if p["status"] == "占用")
_, sess5 = call("GET", "/api/会话", None)
live5 = [s for s in sess5 if s["status"] == "充电中" and s["pile_id"] == pp["id"]]
if live5:
    sid5 = live5[0]["id"]
    db_exec("UPDATE piles SET status='空闲' WHERE id=?", (pp["id"],))
    s5, b5 = call("POST", f"/api/会话/{sid5}/脉冲", {"电量": 1.0, "功率": 7})
    check("撕裂时脉冲被 409 拒绝", s5 == 409 and "撕裂" in b5.get("detail", ""), str(b5))
    db_exec("UPDATE piles SET status='占用' WHERE id=?", (pp["id"],))
else:
    check("夹具前置：存在可测在充会话", False, "no live session")

server.should_exit = True
print(f"\n结果：PASS={PASS}  FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
