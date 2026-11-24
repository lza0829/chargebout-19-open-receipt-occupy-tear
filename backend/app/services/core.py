from __future__ import annotations
from datetime import datetime, timedelta
from app.services import open_surface

from app.db import row_dict, rows
from app.services.predicate import GUN_OPENABLE_SQL, PILE_OPENABLE_SQL


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

    # —— 架构 B 对账区：异常占用必须可暴露，不得假装桩空闲可办 ——
    # 异常开充在充会话（非空闲/非可用枪上开出且仍未结）
    abnormal_open_n = conn.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE abnormal_open=1 AND status IN ('充电中','结算中')"
    ).fetchone()["n"]
    # 逐桩态拆分异常占用桶（臂内分类计数口径）
    abnormal_buckets = rows(
        conn.execute(
            """SELECT p.status AS pile_status, COUNT(*) AS n
               FROM sessions se JOIN piles p ON p.id=se.pile_id
               WHERE se.abnormal_open=1 AND se.status IN ('充电中','结算中')
               GROUP BY p.status"""
        ).fetchall()
    )
    # 审计流水：当日通过/拒绝次数（拒绝尝试永远不计入充电中）
    audit_today = rows(
        conn.execute(
            """SELECT result, COUNT(*) AS n FROM open_audits
               WHERE business_day=? GROUP BY result""",
            (today(),),
        ).fetchall()
    )
    audit_map = {a["result"]: a["n"] for a in audit_today}

    # —— 跨视图不变量：写侧同一谓词可重放的对账，全部应为真 ——
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
    # 4) 审计「通过」数与累计已落会话数一致（同刷保证，失败回滚则两边都无）
    audit_pass_total = conn.execute(
        "SELECT COUNT(*) AS n FROM open_audits WHERE result='通过'"
    ).fetchone()["n"]
    session_total = conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]

    return {
        "桩位空闲": pile_map.get("空闲", 0),
        "桩位占用": pile_map.get("占用", 0),
        "桩位故障": pile_map.get("故障", 0),
        "桩位维护": pile_map.get("维护", 0),
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
        "对账": {
            "在充会话占空闲桩数": leak_idle,
            "无会话占用桩数": phantom_busy,
            "在充枪显示可用数": gun_leak,
            "审计通过会话差": audit_pass_total - session_total,
            "全部一致": leak_idle == 0 and phantom_busy == 0 and gun_leak == 0
            and audit_pass_total == session_total,
        },
    }


def list_sites(conn):
    return rows(conn.execute("SELECT * FROM sites ORDER BY id").fetchall())


def list_piles(conn, site_id: int | None = None):
    select = (
        "SELECT p.*, s.name AS site_name, r.name AS rate_name, "
        f"{PILE_OPENABLE_SQL} AS openable "
        "FROM piles p JOIN sites s ON s.id=p.site_id "
        "JOIN rate_plans r ON r.id=p.rate_plan_id "
    )
    if site_id:
        rs = conn.execute(select + "WHERE p.site_id=? ORDER BY p.id", (site_id,)).fetchall()
    else:
        rs = conn.execute(select + "ORDER BY p.id").fetchall()
    return rows(rs)


def list_guns(conn, pile_id: int | None = None):
    if pile_id:
        return rows(
            conn.execute(
                "SELECT g.*, p.name AS pile_name, "
                f"{GUN_OPENABLE_SQL} AS openable "
                "FROM guns g JOIN piles p ON p.id=g.pile_id WHERE g.pile_id=? ORDER BY g.id",
                (pile_id,),
            ).fetchall()
        )
    return rows(
        conn.execute(
            "SELECT g.*, p.name AS pile_name, "
            f"{GUN_OPENABLE_SQL} AS openable "
            "FROM guns g JOIN piles p ON p.id=g.pile_id ORDER BY g.id"
        ).fetchall()
    )


def list_rates(conn):
    return rows(conn.execute("SELECT * FROM rate_plans ORDER BY id").fetchall())


def list_sessions(conn, status: str | None = None):
    if status:
        rs = conn.execute(
            """SELECT se.*, p.name AS pile_name, g.name AS gun_name, s.name AS site_name
               FROM sessions se
               JOIN piles p ON p.id=se.pile_id
               JOIN guns g ON g.id=se.gun_id
               JOIN sites s ON s.id=se.site_id
               WHERE se.status=? ORDER BY se.id DESC""",
            (status,),
        ).fetchall()
    else:
        rs = conn.execute(
            """SELECT se.*, p.name AS pile_name, g.name AS gun_name, s.name AS site_name
               FROM sessions se
               JOIN piles p ON p.id=se.pile_id
               JOIN guns g ON g.id=se.gun_id
               JOIN sites s ON s.id=se.site_id
               ORDER BY se.id DESC LIMIT 200"""
        ).fetchall()
    return rows(rs)


def session_detail(conn, session_id: int):
    se = conn.execute(
        """SELECT se.*, p.name AS pile_name, g.name AS gun_name, s.name AS site_name, r.name AS rate_name
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
    d = row_dict(se)
    d["脉冲"] = pulses
    d["事件"] = events
    return d


def list_open_audits(conn, business_day: str | None = None):
    """开充审计流水（只追加表）：通过与拒绝尝试同册可核。"""
    if business_day:
        rs = conn.execute(
            """SELECT a.*, p.name AS pile_name
               FROM open_audits a LEFT JOIN piles p ON p.id=a.pile_id
               WHERE a.business_day=? ORDER BY a.id DESC LIMIT 300""",
            (business_day,),
        ).fetchall()
    else:
        rs = conn.execute(
            """SELECT a.*, p.name AS pile_name
               FROM open_audits a LEFT JOIN piles p ON p.id=a.pile_id
               ORDER BY a.id DESC LIMIT 300"""
        ).fetchall()
    return rows(rs)

_overview_impl = overview


def overview(conn):
    return open_surface.overlay_overview(_overview_impl(conn), conn)

