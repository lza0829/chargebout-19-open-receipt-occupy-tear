from __future__ import annotations
from fastapi import HTTPException

from app.services.core import current_band_price, get_setting, is_day_sealed, now_str, today
from app.services.predicate import (
    GUN_TAKE_CLAUSE,
    PILE_TAKE_CLAUSE,
    classify_open,
)


class OpenRejected(Exception):
    """硬拒绝（封印/不存在/站点停用）。携带审计快照，由路由在独立短事务补登流水。

    非空闲不属硬拒绝——它走异常开充通道，不抛本异常。
    """

    def __init__(self, detail: str, audit: dict):
        super().__init__(detail)
        self.detail = detail
        self.audit = audit


class OpenTorn(Exception):
    """开充同命运校验失败：四者（空闲谓词/占用钉/会话行/审计）任一不成立。

    必须抛出以触发整笔事务回滚，绝不允许回执绿而桩仍空闲。
    """


def _next_slip(conn) -> str:
    n = int(get_setting(conn, "会话序号", "1000")) + 1
    conn.execute(
        "INSERT INTO settings(key,value) VALUES('会话序号',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(n),),
    )
    return f"充{n}"


def insert_open_audit(conn, audit: dict) -> int:
    cur = conn.execute(
        """INSERT INTO open_audits(created_at,business_day,pile_id,gun_id,slip_no,result,
                                   abnormal,snapshot_pile_status,snapshot_gun_status,
                                   reason,session_id)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            audit["created_at"],
            audit["business_day"],
            audit["pile_id"],
            audit.get("gun_id"),
            audit.get("slip_no", ""),
            audit["result"],
            audit.get("abnormal", 0),
            audit.get("snapshot_pile_status", ""),
            audit.get("snapshot_gun_status", ""),
            audit.get("reason", ""),
            audit.get("session_id"),
        ),
    )
    return cur.lastrowid


# 模块内别名
_insert_audit = insert_open_audit


def _reject(pile_id: int, gun_id: int | None, pile_status: str, gun_status: str, reason: str) -> OpenRejected:
    return OpenRejected(
        reason,
        {
            "created_at": now_str(),
            "business_day": today(),
            "pile_id": pile_id,
            "gun_id": gun_id,
            "slip_no": "",
            "result": "拒绝",
            "abnormal": 0,
            "snapshot_pile_status": pile_status,
            "snapshot_gun_status": gun_status,
            "reason": reason,
            "session_id": None,
        },
    )


def open_session(conn, pile_id: int, gun_id: int | None = None) -> dict:
    """开充入口（可告警仍开充 + 四者同命运原子重放）。

    调用方须以 db_immediate() 提供连接：整笔开充与占用推进在同一立即事务内。
    成功灯成立当且仅当以下四者同时落定，任一不成立抛 OpenTorn 整笔回滚：
      ① 空闲谓词结果可重放（正常开充必须真正破空闲；异常开充保留真实开前态快照）
      ② 桩/枪占用钉（条件 UPDATE 重放写侧谓词）
      ③ 充电中会话行
      ④ 开充审计「通过」行
    非空闲不硬闸：会话照落、abnormal_open=1、回执与会话册可核、总览暴露异常占用。
    """
    # —— 硬拒绝通道（不属非空闲口径）：封印 / 桩不存在 / 站点停用 ——
    if is_day_sealed(conn):
        raise _reject(pile_id, gun_id, "", "", "充日已封印，禁止新开充")
    pile = conn.execute("SELECT * FROM piles WHERE id=?", (pile_id,)).fetchone()
    if not pile:
        raise _reject(pile_id, gun_id, "", "", "桩位不存在")
    site = conn.execute("SELECT * FROM sites WHERE id=?", (pile["site_id"],)).fetchone()
    if not site or not site["active"]:
        raise _reject(pile_id, gun_id, pile["status"], "", "站点未启用")

    # —— 选枪：指定则取指定，否则自动挑一把本桩的枪（不限可用态，异常也要可开） ——
    if gun_id is None:
        gun = conn.execute(
            "SELECT * FROM guns WHERE pile_id=? ORDER BY id LIMIT 1",
            (pile_id,),
        ).fetchone()
    else:
        gun = conn.execute(
            "SELECT * FROM guns WHERE id=? AND pile_id=?", (gun_id, pile_id)
        ).fetchone()
    if not gun:
        raise _reject(pile_id, gun_id, pile["status"], "", "无可用枪线")

    snapshot_pile_status = pile["status"]
    snapshot_gun_status = gun["status"]
    abnormal, abnormal_reason = classify_open(snapshot_pile_status, snapshot_gun_status)

    ts = now_str()
    price = current_band_price(conn, pile["rate_plan_id"])
    slip = _next_slip(conn)

    # —— ③ 落会话行：异常事实随会话持久化，不可静默抹掉 ——
    cur = conn.execute(
        """INSERT INTO sessions(slip_no,site_id,pile_id,gun_id,rate_plan_id,business_day,status,
                                 kwh,amount,price_locked,started_at,note,abnormal_open,open_reason)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            slip,
            pile["site_id"],
            pile_id,
            gun["id"],
            pile["rate_plan_id"],
            today(),
            "充电中",
            0,
            0,
            price,
            ts,
            abnormal_reason,
            1 if abnormal else 0,
            abnormal_reason,
        ),
    )
    sid = cur.lastrowid

    # —— ② 占用钉：条件 UPDATE 重放写侧同一谓词 ——
    # 仅空闲→占用算「破空闲」；非空闲（占用/故障/维护）保留原态，绝不假装改空闲。
    pile_taken = conn.execute(
        f"UPDATE piles SET {PILE_TAKE_CLAUSE}", (pile_id,)
    ).rowcount
    # 仅可用枪被改写；故障/已占用枪保持原态
    gun_taken = conn.execute(
        f"UPDATE guns SET {GUN_TAKE_CLAUSE}", (gun["id"],)
    ).rowcount

    event_note = f"锁定电价{price}"
    if abnormal:
        event_note = f"异常开充：{abnormal_reason}；{event_note}"
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (sid, "异常开会话" if abnormal else "开会话", ts, event_note),
    )

    # —— ④ 开充审计流水（通过行随主事务同刷） ——
    audit_id = _insert_audit(
        conn,
        {
            "created_at": ts,
            "business_day": today(),
            "pile_id": pile_id,
            "gun_id": gun["id"],
            "slip_no": slip,
            "result": "通过",
            "abnormal": 1 if abnormal else 0,
            "snapshot_pile_status": snapshot_pile_status,
            "snapshot_gun_status": snapshot_gun_status,
            "reason": abnormal_reason,
            "session_id": sid,
        },
    )

    # —— ① 空闲谓词结果 + 四者同命运校验：读回真实落定态，任一撕裂即回滚 ——
    pile_now = conn.execute("SELECT status FROM piles WHERE id=?", (pile_id,)).fetchone()
    gun_now = conn.execute("SELECT status FROM guns WHERE id=?", (gun["id"],)).fetchone()
    se_now = conn.execute(
        "SELECT slip_no, status FROM sessions WHERE id=?", (sid,)
    ).fetchone()
    au_now = conn.execute(
        "SELECT result, session_id, slip_no FROM open_audits WHERE id=?", (audit_id,)
    ).fetchone()

    if not se_now or se_now["slip_no"] != slip or se_now["status"] != "充电中":
        raise OpenTorn("会话行未落定")
    if not au_now or au_now["result"] != "通过" or au_now["session_id"] != sid:
        raise OpenTorn("审计通过行未落定")
    # 在充会话不得对应空闲桩/可用枪——正常开充必须破空闲，异常开充也不得留下假空闲
    if pile_now is None or pile_now["status"] == "空闲":
        raise OpenTorn("桩位占用钉未落定（桩仍空闲）")
    if gun_now is None or gun_now["status"] == "可用":
        raise OpenTorn("枪线占用钉未落定（枪仍可用）")
    if not abnormal and not (pile_taken and gun_taken):
        raise OpenTorn("正常开充未同时破空闲/占枪，证据撕裂")

    return {
        "会话号": slip,
        "会话编号": sid,
        "锁定电价": price,
        "状态": "充电中",
        "是否异常开充": abnormal,
        "异常原因": abnormal_reason,
        "桩位开前态": snapshot_pile_status,
        "枪线开前态": snapshot_gun_status,
        "桩位被破空闲": bool(pile_taken),
        "枪线被占用": bool(gun_taken),
        "告警": abnormal_reason if abnormal else "",
        # 成功灯只在四者同事务落定后点亮；回执附证据便于对账
        "成功": True,
        "四者同命运": True,
        "证据": {
            "桩位占用钉": pile_now["status"],
            "枪线占用钉": gun_now["status"],
            "会话行": slip,
            "审计行": audit_id,
        },
    }


def add_pulse(conn, session_id: int, delta_kwh: float, power_kw: float = 0) -> dict:
    if delta_kwh <= 0:
        raise HTTPException(400, detail="脉冲电量须大于零")
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    if se["status"] != "充电中":
        raise HTTPException(400, detail=f"会话状态为{se['status']}，不可记脉冲")
    # 充电中必须由桩/枪占用钉钉住；桩已空闲或枪已可用说明证据撕裂，脉冲入口不得误亮。
    pile_status = conn.execute(
        "SELECT status FROM piles WHERE id=?", (se["pile_id"],)
    ).fetchone()["status"]
    gun_status = conn.execute(
        "SELECT status FROM guns WHERE id=?", (se["gun_id"],)
    ).fetchone()["status"]
    if pile_status == "空闲" or gun_status == "可用":
        raise HTTPException(
            409,
            detail=f"会话{se['slip_no']}证据撕裂（桩{pile_status}/枪{gun_status}），不可记脉冲，请先急停或修复占用",
        )
    ts = now_str()
    conn.execute(
        "INSERT INTO pulses(session_id,delta_kwh,power_kw,created_at) VALUES(?,?,?,?)",
        (session_id, delta_kwh, power_kw, ts),
    )
    new_kwh = float(se["kwh"]) + float(delta_kwh)
    price = float(se["price_locked"] or current_band_price(conn, se["rate_plan_id"]))
    amount = round(new_kwh * price, 2)
    conn.execute(
        "UPDATE sessions SET kwh=?, amount=? WHERE id=?",
        (round(new_kwh, 3), amount, session_id),
    )
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (session_id, "脉冲", ts, f"+{delta_kwh}"),
    )
    return {"会话编号": session_id, "累计电量": round(new_kwh, 3), "预估金额": amount}


def _release_occupancy(conn, pile_id: int, gun_id: int) -> None:
    """关账/中断/超时后释放占用。一桩可能叠多笔会话：
    仅当本桩已无任何在充会话时才回收；且只回收处于占用态的对象，
    故障/维护态保持原样，绝不借关账静默改空闲。
    """
    remain = conn.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE pile_id=? AND status IN ('充电中','结算中')",
        (pile_id,),
    ).fetchone()["n"]
    if remain == 0:
        conn.execute(
            "UPDATE piles SET status='空闲' WHERE id=? AND status='占用'",
            (pile_id,),
        )
    # 该枪若无其他在充会话且当前为占用，回收为可用
    gun_busy = conn.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE gun_id=? AND status IN ('充电中','结算中')",
        (gun_id,),
    ).fetchone()["n"]
    if gun_busy == 0:
        conn.execute(
            "UPDATE guns SET status='可用' WHERE id=? AND status='占用'",
            (gun_id,),
        )


def settle(conn, session_id: int) -> dict:
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    if se["status"] not in ("充电中", "结算中", "已超时"):
        raise HTTPException(400, detail=f"会话状态为{se['status']}，不可关账")
    ts = now_str()
    price = float(se["price_locked"] or current_band_price(conn, se["rate_plan_id"]))
    amount = round(float(se["kwh"]) * price, 2)
    conn.execute(
        "UPDATE sessions SET status='已关账', amount=?, closed_at=?, price_locked=? WHERE id=?",
        (amount, ts, price, session_id),
    )
    _release_occupancy(conn, se["pile_id"], se["gun_id"])
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (session_id, "结算关账", ts, f"金额{amount}"),
    )
    return {"会话编号": session_id, "电量": float(se["kwh"]), "金额": amount, "状态": "已关账"}


def mark_timeout(conn, session_id: int) -> dict:
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    if se["status"] != "充电中":
        raise HTTPException(400, detail=f"会话状态为{se['status']}，不可标超时")
    minutes = int(get_setting(conn, "超时分钟", "120"))
    ts = now_str()
    # 追加而非覆盖 note，避免抹掉异常开充原因（权威字段 open_reason 永不改写）
    prev_note = se["note"] or ""
    timeout_note = f"超过{minutes}分钟未关"
    conn.execute(
        "UPDATE sessions SET status='已超时', note=? WHERE id=?",
        ((prev_note + "；" + timeout_note).strip("；"), session_id),
    )
    # 已超时不再计入在充，占用必须同步回收，否则会留下「占用但无在充会话」的幽灵占用
    _release_occupancy(conn, se["pile_id"], se["gun_id"])
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (session_id, "超时", ts, f"阈值{minutes}分钟"),
    )
    return {"会话编号": session_id, "状态": "已超时"}


def abort_session(conn, session_id: int, reason: str = "急停") -> dict:
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    if se["status"] not in ("充电中", "结算中", "已超时"):
        raise HTTPException(400, detail=f"会话状态为{se['status']}，不可中断")
    ts = now_str()
    conn.execute(
        "UPDATE sessions SET status='已中断', closed_at=?, note=? WHERE id=?",
        (ts, reason, session_id),
    )
    _release_occupancy(conn, se["pile_id"], se["gun_id"])
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (session_id, "异常中断", ts, reason),
    )
    return {"会话编号": session_id, "状态": "已中断", "原因": reason}


def set_pile_status(conn, pile_id: int, status: str) -> dict:
    if status not in ("空闲", "占用", "故障", "维护"):
        raise HTTPException(400, detail="非法桩位状态")
    pile = conn.execute("SELECT * FROM piles WHERE id=?", (pile_id,)).fetchone()
    if not pile:
        raise HTTPException(404, detail="桩位不存在")
    open_se = conn.execute(
        "SELECT id, abnormal_open FROM sessions WHERE pile_id=? AND status IN ('充电中','结算中') ORDER BY id",
        (pile_id,),
    ).fetchall()
    busy_guns = conn.execute(
        "SELECT COUNT(*) AS n FROM guns WHERE pile_id=? AND status='占用'", (pile_id,)
    ).fetchone()["n"]
    # 改空闲是硬互斥：有在充会话或占用枪的桩不得假装空闲可办（旁路改空闲灯不算修完）。
    if status == "空闲" and open_se:
        raise HTTPException(400, detail="仍有在充会话，不可改空闲")
    if status == "空闲" and busy_guns:
        raise HTTPException(400, detail="仍有占用枪线，不可改空闲")
    # 改占用必须有会话行钉住，否则就是无会话的幽灵占用。
    if status == "占用" and not open_se:
        raise HTTPException(400, detail="无在充会话，不可改为占用")
    conn.execute("UPDATE piles SET status=? WHERE id=?", (status, pile_id))
    ts = now_str()
    for se in open_se:
        if status in ("故障", "维护"):
            conn.execute(
                "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
                (se["id"], "桩态变更", ts, f"桩位置为{status}，异常占用持续可核"),
            )
    return {"桩位编号": pile_id, "状态": status, "在充会话数": len(open_se)}
