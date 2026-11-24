from __future__ import annotations
from datetime import datetime

from app.db import row_dict, rows
from app.services.predicate import (
    GUN_IDLE_LAMP_SQL,
    GUN_OPEN_SESSIONS_SQL,
    GUN_OPENABLE_SQL,
    PILE_BUSY_GUNS_SQL,
    PILE_IDLE_LAMP_SQL,
    PILE_OPEN_SESSIONS_SQL,
    PILE_OPENABLE_SQL,
    gun_evidence_conflict,
    pile_evidence_conflict,
)


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def get_setting(conn, key: str, default: str = "") -> str:
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def current_band_price(conn, rate_plan_id: int) -> float:
    plan = conn.execute("SELECT * FROM rate_plans WHERE id=?", (rate_plan_id,)).fetchone()
    if not plan:
        return 0.0
    hour = datetime.now().hour
    if 8 <= hour < 11 or 18 <= hour < 21:
        return float(plan["peak_price"])
    if 23 <= hour or hour < 7:
        return float(plan["valley_price"])
    return float(plan["flat_price"])


def is_day_sealed(conn, day: str | None = None) -> bool:
    d = day or today()
    r = conn.execute("SELECT sealed FROM day_seals WHERE business_day=?", (d,)).fetchone()
    return bool(r and r["sealed"])


def _gap_rows(conn) -> list[dict]:
    """开充缺口：桩/枪态与占用钉、会话行对不上的残留（只读暴露，不改写事实）。

    架构下正常写入不允许出现缺口；这里专门捞历史脏写或外部旁路留下的撕裂，
    供总览与桩位页识破，绝不借读路径假装修完。
    """
    out: list[dict] = []
    piles = conn.execute(
        f"""SELECT p.id, p.name, p.status,
                   {PILE_OPEN_SESSIONS_SQL} AS open_sessions,
                   {PILE_BUSY_GUNS_SQL} AS busy_guns
            FROM piles p ORDER BY p.id"""
    ).fetchall()
    for p in piles:
        if pile_evidence_conflict(p["status"], p["open_sessions"], p["busy_guns"]):
            out.append(
                {
                    "对象": "桩位",
                    "编号": p["id"],
                    "名称": p["name"],
                    "态": p["status"],
                    "在充会话数": p["open_sessions"],
                    "占用枪数": p["busy_guns"],
                    "说明": "桩态与占用钉/会话行不一致",
                }
            )
    guns = conn.execute(
        f"""SELECT g.id, g.name, g.status, p.name AS pile_name,
                   {GUN_OPEN_SESSIONS_SQL} AS open_sessions
            FROM guns g JOIN piles p ON p.id=g.pile_id ORDER BY g.id"""
    ).fetchall()
    for g in guns:
        if gun_evidence_conflict(g["status"], g["open_sessions"]):
            out.append(
                {
                    "对象": "枪线",
                    "编号": g["id"],
                    "名称": f"{g['pile_name']}/{g['name']}",
                    "态": g["status"],
                    "在充会话数": g["open_sessions"],
                    "占用枪数": 0,
                    "说明": "枪态与在充会话不一致",
                }
            )
    return out


def overview(conn) -> dict:
    piles = rows(conn.execute("SELECT status, COUNT(*) AS n FROM piles GROUP BY status").fetchall())
    sess = rows(conn.execute("SELECT status, COUNT(*) AS n FROM sessions GROUP BY status").fetchall())
    pile_map = {p["status"]: p["n"] for p in piles}
    sess_map = {s["status"]: s["n"] for s in sess}
    open_kwh = conn.execute(
        "SELECT COALESCE(SUM(kwh),0) AS k FROM sessions WHERE status IN ('充电中','结算中')"
    ).fetchone()["k"]
    today_amount = conn.execute(
        "SELECT COALESCE(SUM(amount),0) AS a FROM sessions WHERE business_day=? AND status='已关账'",
        (today(),),
    ).fetchone()["a"]

    # —— 异常占用必须可暴露，不得假装桩空闲可办 ——
    abnormal_open_n = conn.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE abnormal_open=1 AND status IN ('充电中','结算中')"
    ).fetchone()["n"]
    abnormal_buckets = rows(
        conn.execute(
            """SELECT p.status AS pile_status, COUNT(*) AS n
               FROM sessions se JOIN piles p ON p.id=se.pile_id
               WHERE se.abnormal_open=1 AND se.status IN ('充电中','结算中')
               GROUP BY p.status"""
        ).fetchall()
    )
    audit_today = rows(
        conn.execute(
            """SELECT result, COUNT(*) AS n FROM open_audits
               WHERE business_day=? GROUP BY result""",
            (today(),),
        ).fetchall()
    )
    audit_map = {a["result"]: a["n"] for a in audit_today}

    # —— 四者同命运重放对账：任何一侧缺失都算缺口，不用总数相消掩盖 ——
    # 1) 每个在充会话对应的桩当前不得是空闲（在充即占住）
    leak_idle = conn.execute(
        """SELECT COUNT(*) AS n FROM sessions se JOIN piles p ON p.id=se.pile_id
           WHERE se.status IN ('充电中','结算中') AND p.status='空闲'"""
    ).fetchone()["n"]
    # 2) 占用态桩必须至少有一笔在充会话（无会话的占用=脏写）
    phantom_busy = conn.execute(
        """SELECT COUNT(*) AS n FROM piles p
           WHERE p.status='占用' AND NOT EXISTS(
             SELECT 1 FROM sessions se WHERE se.pile_id=p.id
             AND se.status IN ('充电中','结算中'))"""
    ).fetchone()["n"]
    # 3) 被在充会话占用的枪不得显示可用
    gun_leak = conn.execute(
        """SELECT COUNT(*) AS n FROM sessions se JOIN guns g ON g.id=se.gun_id
           WHERE se.status IN ('充电中','结算中') AND g.status='可用'"""
    ).fetchone()["n"]
    # 4) 逐笔对账：每笔「通过」审计必须能重放出同一会话；
    #    每笔仍在充的会话必须有「通过」审计（已终结的历史会话不追索审计，
    #    迁移前的老会话不应永久把对账拖红；真正有害的是「绿了却还在充却没账」）。
    audit_without_session = conn.execute(
        """SELECT COUNT(*) AS n FROM open_audits a
           WHERE a.result='通过' AND (
             a.session_id IS NULL OR NOT EXISTS(
               SELECT 1 FROM sessions se WHERE se.id=a.session_id
               AND se.slip_no=a.slip_no AND se.pile_id=a.pile_id))"""
    ).fetchone()["n"]
    session_without_audit = conn.execute(
        """SELECT COUNT(*) AS n FROM sessions se
           WHERE se.status IN ('充电中','结算中') AND NOT EXISTS(
             SELECT 1 FROM open_audits a WHERE a.result='通过'
             AND a.session_id=se.id AND a.slip_no=se.slip_no)"""
    ).fetchone()["n"]

    gaps = _gap_rows(conn)
    all_ok = (
        leak_idle == 0
        and phantom_busy == 0
        and gun_leak == 0
        and audit_without_session == 0
        and session_without_audit == 0
        and len(gaps) == 0
    )
    # —— 证据口径的「已开充」：占用钉 + 会话行 + 可重放审计三者同时成立才算真开充 ——
    # 撕裂的会话不计入，缺口未清前总览不得亮已开充（禁止成功灯与空闲灯各报各的）。
    charging = conn.execute(
        """SELECT COUNT(*) AS n FROM sessions se
           JOIN piles p ON p.id=se.pile_id
           JOIN guns g ON g.id=se.gun_id
           WHERE se.status='充电中' AND p.status<>'空闲' AND g.status<>'可用'
             AND EXISTS(
               SELECT 1 FROM open_audits a WHERE a.result='通过'
               AND a.session_id=se.id AND a.slip_no=se.slip_no)"""
    ).fetchone()["n"]
    charging_torn = conn.execute(
        """SELECT COUNT(*) AS n FROM sessions se
           JOIN piles p ON p.id=se.pile_id
           JOIN guns g ON g.id=se.gun_id
           WHERE se.status='充电中' AND (
             p.status='空闲' OR g.status='可用'
             OR NOT EXISTS(
               SELECT 1 FROM open_audits a WHERE a.result='通过'
               AND a.session_id=se.id AND a.slip_no=se.slip_no))"""
    ).fetchone()["n"]
    # 空闲灯口径：状态空闲且无在充会话（证据口径，不是裸 status）
    idle_lamps = conn.execute(
        f"SELECT COUNT(*) AS n FROM piles p WHERE {PILE_IDLE_LAMP_SQL}"
    ).fetchone()["n"]

    return {
        "桩位空闲": pile_map.get("空闲", 0),
        "桩位占用": pile_map.get("占用", 0),
        "桩位故障": pile_map.get("故障", 0),
        "桩位维护": pile_map.get("维护", 0),
        # 证据口径：空闲灯与已开充必须成对，撕裂计数非 0 即说明视图对不上
        "桩位空闲灯亮": idle_lamps,
        "已开充(证据完整)": charging,
        "已开充证据撕裂": charging_torn,
        "充电中": sess_map.get("充电中", 0),
        "结算中": sess_map.get("结算中", 0),
        "已关账": sess_map.get("已关账", 0),
        "已超时": sess_map.get("已超时", 0),
        "已中断": sess_map.get("已中断", 0),
        "在充电量": round(float(open_kwh), 3),
        "本日关账金额": round(float(today_amount), 2),
        "营业日": today(),
        "充日已封印": is_day_sealed(conn),
        # 异常占用暴露（不做挡板，只暴露）
        "异常占用在充数": abnormal_open_n,
        "异常占用分桶": {b["pile_status"]: b["n"] for b in abnormal_buckets},
        "本日开充通过": audit_map.get("通过", 0),
        "本日开充拒绝": audit_map.get("拒绝", 0),
        # 开充缺口：历史脏写/旁路残留，必须可被识破，不得借读路径伪装已开充
        "开充缺口": gaps,
        "缺口数": len(gaps),
        "对账": {
            "在充会话占空闲桩数": leak_idle,
            "无会话占用桩数": phantom_busy,
            "在充枪显示可用数": gun_leak,
            "审计通过无会话数": audit_without_session,
            "会话无通过审计数": session_without_audit,
            "桩枪证据撕裂数": len(gaps),
            "全部一致": all_ok,
        },
    }


def list_sites(conn):
    return rows(conn.execute("SELECT * FROM sites ORDER BY id").fetchall())


def list_piles(conn, site_id: int | None = None):
    select = (
        "SELECT p.*, s.name AS site_name, r.name AS rate_name, "
        f"{PILE_OPENABLE_SQL} AS openable, "
        f"{PILE_IDLE_LAMP_SQL} AS idle_lamp, "
        f"{PILE_OPEN_SESSIONS_SQL} AS open_sessions, "
        f"{PILE_BUSY_GUNS_SQL} AS busy_guns, "
        f"COALESCE(s.active, 0) AS site_active "
        "FROM piles p JOIN sites s ON s.id=p.site_id "
        "JOIN rate_plans r ON r.id=p.rate_plan_id "
    )
    if site_id:
        rs = conn.execute(select + "WHERE p.site_id=? ORDER BY p.id", (site_id,)).fetchall()
    else:
        rs = conn.execute(select + "ORDER BY p.id").fetchall()
    out = rows(rs)
    for p in out:
        p["openable"] = bool(p["openable"])
        p["idle_lamp"] = bool(p["idle_lamp"])
        p["evidence_conflict"] = pile_evidence_conflict(
            p["status"], p["open_sessions"], p["busy_guns"]
        )
    return out


def list_guns(conn, pile_id: int | None = None):
    select = (
        "SELECT g.*, p.name AS pile_name, "
        f"{GUN_OPENABLE_SQL} AS openable, "
        f"{GUN_IDLE_LAMP_SQL} AS idle_lamp, "
        f"{GUN_OPEN_SESSIONS_SQL} AS open_sessions "
        "FROM guns g JOIN piles p ON p.id=g.pile_id "
    )
    if pile_id:
        rs = conn.execute(select + "WHERE g.pile_id=? ORDER BY g.id", (pile_id,)).fetchall()
    else:
        rs = conn.execute(select + "ORDER BY g.id").fetchall()
    out = rows(rs)
    for g in out:
        g["openable"] = bool(g["openable"])
        g["idle_lamp"] = bool(g["idle_lamp"])
        g["evidence_conflict"] = gun_evidence_conflict(g["status"], g["open_sessions"])
    return out


def list_rates(conn):
    return rows(conn.execute("SELECT * FROM rate_plans ORDER BY id").fetchall())


_SESSION_SELECT = (
    """SELECT se.*, p.name AS pile_name, g.name AS gun_name, s.name AS site_name,
              p.status AS pile_status, g.status AS gun_status
       FROM sessions se
       JOIN piles p ON p.id=se.pile_id
       JOIN guns g ON g.id=se.gun_id
       JOIN sites s ON s.id=se.site_id
    """
)


def _decorate_session(row) -> dict:
    d = row_dict(row)
    # 在充会话的占用证据是否仍钉住：充电中却桩空闲/枪可用即撕裂
    if d["status"] in ("充电中", "结算中"):
        d["evidence_conflict"] = d["pile_status"] == "空闲" or d["gun_status"] == "可用"
    else:
        d["evidence_conflict"] = False
    return d


def list_sessions(conn, status: str | None = None):
    if status:
        rs = conn.execute(
            _SESSION_SELECT + "WHERE se.status=? ORDER BY se.id DESC", (status,)
        ).fetchall()
    else:
        rs = conn.execute(_SESSION_SELECT + "ORDER BY se.id DESC LIMIT 200").fetchall()
    return [_decorate_session(r) for r in rs]


def session_detail(conn, session_id: int):
    se = conn.execute(
        """SELECT se.*, p.name AS pile_name, g.name AS gun_name, s.name AS site_name,
                  r.name AS rate_name, p.status AS pile_status, g.status AS gun_status
           FROM sessions se
           JOIN piles p ON p.id=se.pile_id
           JOIN guns g ON g.id=se.gun_id
           JOIN sites s ON s.id=se.site_id
           JOIN rate_plans r ON r.id=se.rate_plan_id
           WHERE se.id=?""",
        (session_id,),
    ).fetchone()
    if not se:
        return None
    pulses = rows(
        conn.execute(
            "SELECT * FROM pulses WHERE session_id=? ORDER BY id", (session_id,)
        ).fetchall()
    )
    events = rows(
        conn.execute(
            "SELECT * FROM session_events WHERE session_id=? ORDER BY id", (session_id,)
        ).fetchall()
    )
    d = _decorate_session(se)
    d["脉冲"] = pulses
    d["事件"] = events
    return d


def list_open_audits(conn, business_day: str | None = None):
    """开充审计流水（只追加表）：通过与拒绝尝试同册可核。"""
    base = (
        """SELECT a.*, p.name AS pile_name,
                  (SELECT se.slip_no FROM sessions se WHERE se.id=a.session_id) AS live_slip
           FROM open_audits a LEFT JOIN piles p ON p.id=a.pile_id
        """
    )
    if business_day:
        rs = conn.execute(
            base + "WHERE a.business_day=? ORDER BY a.id DESC LIMIT 300", (business_day,)
        ).fetchall()
    else:
        rs = conn.execute(base + "ORDER BY a.id DESC LIMIT 300").fetchall()
    out = rows(rs)
    for a in out:
        # 通过行必须能重放出同一会话；重放不出即可见缺口
        a["replayable"] = (
            a["result"] != "通过"
            or (a["session_id"] is not None and a["live_slip"] == a["slip_no"])
        )
    return out
