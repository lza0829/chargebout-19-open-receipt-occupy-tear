"""开充联动正确性验证（架构 B：可告警仍开充）。
独立临时库，跑完即弃；覆盖：正常/异常开充、硬拒绝、并发抢桩、
半截失败回滚、谓词重放、释放回收、总览对账不变量。
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading

_tmp = tempfile.mkdtemp(prefix="chargebout_test_")
os.environ["CHARGEBOUT_DB"] = os.path.join(_tmp, "t.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException  # noqa: E402

from app.db import db, db_immediate, init_db  # noqa: E402
from app.seed import seed_if_empty  # noqa: E402
from app.services import core  # noqa: E402
from app.services import sessions as svc  # noqa: E402
from app.services.predicate import classify_open, gun_openable, pile_openable  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def pile_status(conn, pid):
    return conn.execute("SELECT status FROM piles WHERE id=?", (pid,)).fetchone()["status"]


def gun_status(conn, gid):
    return conn.execute("SELECT status FROM guns WHERE id=?", (gid,)).fetchone()["status"]


seed_if_empty()

with db() as c:
    idle_pile = c.execute("SELECT id FROM piles WHERE status='空闲' ORDER BY id LIMIT 1").fetchone()["id"]
    maint_pile = c.execute("SELECT id FROM piles WHERE status='维护' ORDER BY id LIMIT 1").fetchone()["id"]
    gid_idle = c.execute("SELECT id FROM guns WHERE pile_id=? ORDER BY id LIMIT 1", (idle_pile,)).fetchone()["id"]
    gid_maint = c.execute("SELECT id FROM guns WHERE pile_id=? ORDER BY id LIMIT 1", (maint_pile,)).fetchone()["id"]

print("== 1. 正常开充：六视图同刷 ==")
with db_immediate() as c:
    before = core.overview(c)
    r = svc.open_session(c, idle_pile)
    sid = r["会话编号"]
    check("回执无告警", r["是否异常开充"] is False and r["告警"] == "", str(r))
    check("桩被破空闲", r["桩位被破空闲"] is True)
    check("枪被占用", r["枪线被占用"] is True)
    check("桩态=占用", pile_status(c, idle_pile) == "占用")
    check("枪态=占用", gun_status(c, gid_idle) == "占用")
    se = c.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    check("会话=充电中且无异常标记", se["status"] == "充电中" and se["abnormal_open"] == 0)
    au = c.execute("SELECT * FROM open_audits WHERE session_id=?", (sid,)).fetchone()
    check("审计通过且非异常", au["result"] == "通过" and au["abnormal"] == 0
          and au["snapshot_pile_status"] == "空闲")
    check("序号已推进", c.execute("SELECT value FROM settings WHERE key='会话序号'").fetchone()["value"]
          == str(1001))
ov = None
with db() as c:
    ov = core.overview(c)
    check("总览占用+1 空闲-1", ov["桩位占用"] == before["桩位占用"] + 1
          and ov["桩位空闲"] == before["桩位空闲"] - 1, str(ov))
    check("充电中+1", ov["充电中"] == before["充电中"] + 1)
    check("对账全部一致", ov["对账"]["全部一致"] is True, str(ov["对账"]))

print("== 2. 占用桩再开充：异常开充，会话照落且可核 ==")
with db_immediate() as c:
    r2 = svc.open_session(c, idle_pile)
    sid2 = r2["会话编号"]
    check("回执标记异常并带原因", r2["是否异常开充"] is True and "非空闲" in r2["异常原因"], str(r2))
    check("未二次破空闲", r2["桩位被破空闲"] is False)
    check("桩仍占用未被刷态", pile_status(c, idle_pile) == "占用")
    se2 = c.execute("SELECT * FROM sessions WHERE id=?", (sid2,)).fetchone()
    check("会话落库且 abnormal_open=1", se2["status"] == "充电中" and se2["abnormal_open"] == 1
          and se2["open_reason"] != "")
    au2 = c.execute("SELECT * FROM open_audits WHERE session_id=?", (sid2,)).fetchone()
    check("审计=通过+异常+快照占用", au2["result"] == "通过" and au2["abnormal"] == 1
          and au2["snapshot_pile_status"] == "占用")
    ev = c.execute("SELECT action FROM session_events WHERE session_id=? ORDER BY id", (sid2,)).fetchall()
    check("事件流水标注异常开会话", any(e["action"] == "异常开会话" for e in ev))
with db() as c:
    ov = core.overview(c)
    check("总览暴露异常占用≥1", ov["异常占用在充数"] >= 1, str(ov))
    check("异常分桶含占用", ov["异常占用分桶"].get("占用", 0) >= 1, str(ov["异常占用分桶"]))
    check("异常会话计入充电中", ov["充电中"] >= 2)
    check("对账仍全部一致", ov["对账"]["全部一致"] is True, str(ov["对账"]))

print("== 3. 维护桩开充：故障/维护态异常暴露，不假装空闲 ==")
with db_immediate() as c:
    r3 = svc.open_session(c, maint_pile)
    sid3 = r3["会话编号"]
    check("维护桩开充回执告警", r3["是否异常开充"] is True and "维护" in r3["异常原因"], str(r3))
    check("维护桩保持维护态", pile_status(c, maint_pile) == "维护")
    check("会话异常标记可核", c.execute("SELECT abnormal_open FROM sessions WHERE id=?", (sid3,))
          .fetchone()["abnormal_open"] == 1)
with db() as c:
    ov = core.overview(c)
    check("异常分桶含维护", ov["异常占用分桶"].get("维护", 0) >= 1, str(ov["异常占用分桶"]))
    check("维护桶未被占用桶吞掉", ov["桩位维护"] >= 1)

print("== 4. 硬拒绝：封印开充不留会话不改占用，审计留拒绝 ==")
from app.services.dayseal import seal_day  # noqa: E402
with db_immediate() as c:
    seal_day(c, "测试封印")
with db() as c:
    n_sess_before = c.execute("SELECT COUNT(*) n FROM sessions").fetchone()["n"]
    n_audit_before = c.execute("SELECT COUNT(*) n FROM open_audits").fetchone()["n"]
    ps_before = pile_status(c, idle_pile)
rejected_audit = None
try:
    with db_immediate() as c:
        svc.open_session(c, idle_pile)
    check("封印开充被拒", False)
except svc.OpenRejected as ex:
    rejected_audit = ex.audit
    check("封印开充被拒", "封印" in ex.detail)
    check("拒绝携带审计快照", ex.audit["result"] == "拒绝" and ex.audit["reason"] != "")
with db() as c:
    check("无新增会话", c.execute("SELECT COUNT(*) n FROM sessions").fetchone()["n"] == n_sess_before)
    check("桩态未变", pile_status(c, idle_pile) == ps_before)
# 路由层会补登拒绝审计；这里直接模拟补登
with db() as c:
    svc.insert_open_audit(c, rejected_audit)
with db() as c:
    check("拒绝审计已留痕", c.execute("SELECT COUNT(*) n FROM open_audits").fetchone()["n"]
          == n_audit_before + 1)
    check("拒绝行无 session_id", c.execute(
        "SELECT session_id FROM open_audits WHERE result='拒绝' ORDER BY id DESC LIMIT 1")
        .fetchone()["session_id"] is None)
    ov = core.overview(c)
    check("拒绝不计入充电中", ov["充电中"] >= 2 and ov["对账"]["全部一致"], str(ov["对账"]))
    check("总览拒绝计数+1", ov["本日开充拒绝"] >= 1)
from app.services.dayseal import unseal_day  # noqa: E402
with db_immediate() as c:
    unseal_day(c)

print("== 5. 并发双开同一空闲桩：不得双破空闲 ==")
with db() as c:
    fresh = c.execute("SELECT id FROM piles WHERE status='空闲' ORDER BY id LIMIT 1").fetchone()["id"]
    fg = c.execute("SELECT id FROM guns WHERE pile_id=? ORDER BY id LIMIT 1", (fresh,)).fetchone()["id"]
barrier = threading.Barrier(2)
results = []
errors = []


def worker():
    try:
        barrier.wait()
        with db_immediate() as c:
            results.append(svc.open_session(c, fresh))
    except Exception as ex:  # noqa: BLE001
        errors.append(ex)


t1 = threading.Thread(target=worker)
t2 = threading.Thread(target=worker)
t1.start(); t2.start(); t1.join(); t2.join()
check("两线程均成功返回（B 架构不硬闸）", len(results) == 2 and not errors, str(errors))
with db() as c:
    check("桩最终只被破一次（仍为占用）", pile_status(c, fresh) == "占用")
    check("枪最终为占用", gun_status(c, fg) == "占用")
    rows_sess = c.execute(
        "SELECT abnormal_open, COUNT(*) n FROM sessions WHERE pile_id=? AND status='充电中' GROUP BY abnormal_open",
        (fresh,),
    ).fetchall()
    m = {r["abnormal_open"]: r["n"] for r in rows_sess}
    check("恰好一笔正常+一笔异常", m.get(0) == 1 and m.get(1) == 1, str(m))
    audits = c.execute(
        "SELECT abnormal, COUNT(*) n FROM open_audits WHERE pile_id=? GROUP BY abnormal", (fresh,)
    ).fetchall()
    am = {r["abnormal"]: r["n"] for r in audits}
    check("审计同刷：一正常一异常", am.get(0) == 1 and am.get(1) == 1, str(am))
    check("并发后对账一致", core.overview(c)["对账"]["全部一致"] is True)

print("== 6. 半截失败：整笔回到开充前快照 ==")
with db() as c:
    snap_pile = c.execute("SELECT id FROM piles WHERE status='空闲' ORDER BY id LIMIT 1").fetchone()["id"]
    snap_gun = c.execute("SELECT id FROM guns WHERE pile_id=? ORDER BY id LIMIT 1", (snap_pile,)).fetchone()["id"]
    slip_before = c.execute("SELECT value FROM settings WHERE key='会话序号'").fetchone()["value"]
    sess_before = c.execute("SELECT COUNT(*) n FROM sessions").fetchone()["n"]
    audit_before = c.execute("SELECT COUNT(*) n FROM open_audits").fetchone()["n"]
try:
    with db_immediate() as c:
        svc.open_session(c, snap_pile)
        raise RuntimeError("模拟开充后段故障")
except RuntimeError:
    pass
with db() as c:
    check("桩回到空闲", pile_status(c, snap_pile) == "空闲")
    check("枪回到可用", gun_status(c, snap_gun) == "可用")
    check("无残留会话", c.execute("SELECT COUNT(*) n FROM sessions").fetchone()["n"] == sess_before)
    check("无残留审计", c.execute("SELECT COUNT(*) n FROM open_audits").fetchone()["n"] == audit_before)
    check("序号回滚", c.execute("SELECT value FROM settings WHERE key='会话序号'").fetchone()["value"]
          == slip_before)

print("== 7. 读路径谓词可由写侧同一谓词重放 ==")
with db() as c:
    plist = core.list_piles(c)
    for p in plist:
        site = c.execute("SELECT * FROM sites WHERE id=?", (p["site_id"],)).fetchone()
        prow = c.execute("SELECT * FROM piles WHERE id=?", (p["id"],)).fetchone()
        check(f"桩{p['id']} openable 与对象谓词一致",
              bool(p["openable"]) == pile_openable(prow, site), f"sql={p['openable']}")
    glist = core.list_guns(c)
    for g in glist:
        grow = c.execute("SELECT * FROM guns WHERE id=?", (g["id"],)).fetchone()
        check(f"枪{g['id']} openable 与对象谓词一致",
              bool(g["openable"]) == gun_openable(grow), f"sql={g['openable']}")
    check("classify_open 口径：占用桩=异常", classify_open("占用", "占用")[0] is True)
    check("classify_open 口径：空闲+可用=正常", classify_open("空闲", "可用") == (False, ""))

print("== 8. 结算/中断释放：多笔叠会话时最后一笔才回收 ==")
with db() as c:
    target = c.execute("SELECT id FROM piles WHERE status='占用' AND id=? ", (idle_pile,)).fetchone()["id"]
    open_ids = [r["id"] for r in c.execute(
        "SELECT id FROM sessions WHERE pile_id=? AND status='充电中' ORDER BY id", (target,)).fetchall()]
check("占用桩上有≥2笔在充", len(open_ids) >= 2, str(open_ids))
with db_immediate() as c:
    svc.settle(c, open_ids[0])
with db() as c:
    check("结第一笔后桩仍占用（还有在充）", pile_status(c, target) == "占用")
with db_immediate() as c:
    svc.abort_session(c, open_ids[1], "测试急停")
with db() as c:
    check("最后一笔结束后桩回收空闲", pile_status(c, target) == "空闲")
    check("对账一致", core.overview(c)["对账"]["全部一致"] is True)

print("== 9. 维护态在充桩不得手动改空闲（硬互斥仍在） ==")
with db_immediate() as c:
    r = svc.open_session(c, maint_pile)
    keep_sid = r["会话编号"]
try:
    with db_immediate() as c:
        svc.set_pile_status(c, maint_pile, "空闲")
    check("有在充改空闲被拒", False)
except HTTPException as ex:
    check("有在充改空闲被拒", "不可改空闲" in ex.detail)
with db() as c:
    check("维护态未被篡改", pile_status(c, maint_pile) == "维护")

print("== 10. 异常事实在超时/结算后不被静默抹掉 ==")
with db_immediate() as c:
    svc.mark_timeout(c, keep_sid)
with db() as c:
    se = c.execute("SELECT * FROM sessions WHERE id=?", (keep_sid,)).fetchone()
    check("超时后 abnormal_open 仍为 1", se["abnormal_open"] == 1)
    check("open_reason 保留", se["open_reason"] != "")
    check("note 追加超时且未丢异常原因", "非空闲" in se["note"] and "未关" in se["note"], se["note"])
with db_immediate() as c:
    svc.settle(c, keep_sid)
with db() as c:
    se = c.execute("SELECT * FROM sessions WHERE id=?", (keep_sid,)).fetchone()
    check("关账后异常标记仍可核", se["abnormal_open"] == 1 and se["status"] == "已关账")
    check("审计异常行仍在", c.execute(
        "SELECT COUNT(*) n FROM open_audits WHERE session_id=? AND abnormal=1", (keep_sid,))
        .fetchone()["n"] == 1)

print(f"\n结果：PASS={PASS}  FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
