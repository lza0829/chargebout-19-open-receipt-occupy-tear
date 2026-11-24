"""开充资格谓词——全局唯一口径，读写路径共用，禁止各写一份判断。

架构（全局唯一，禁止混用）：可告警仍开充 + 同命运原子重放。
- 谓词为真 = 桩空闲且枪可用 = 「正常开充」。
- 谓词为假不代表硬拒绝，而是「异常开充」：会话照落、占用照推，
  但异常事实写进会话册与开充审计并在总览暴露，不得静默抹掉。
- 成功灯成立的条件（四者同命运，缺一整笔回滚）：
  ① 空闲谓词结果（正常开充须破空闲；异常开充须留下真实开前态快照）
  ② 桩/枪占用钉（piles.status / guns.status 的条件 UPDATE）
  ③ 充电中会话行
  ④ 开充审计通过行
  四者由同一个 BEGIN IMMEDIATE 事务写入，失败整笔回到开充前快照。
- 读路径的空闲灯也在此重放：桩/枪空闲灯 = 状态空闲/可用 且 无在充会话，
  前端不得另写 `status==='空闲'` 之类的判断。
"""
from __future__ import annotations

# 在充会话状态（占用钉成立的证据）
OPEN_SESSION_STATUSES = ("充电中", "结算中")
OPEN_SESSION_STATUS_SQL = "('充电中','结算中')"

# 桩位「可正常开充」：状态空闲且站点启用
PILE_OPENABLE_SQL = "(p.status = '空闲' AND COALESCE(s.active, 0) = 1)"
# 枪线「可正常开充」：枪本身可用
GUN_OPENABLE_SQL = "g.status = '可用'"

# 写侧条件更新子句（与上面读侧谓词逐字同口径）
PILE_TAKE_CLAUSE = "status='占用' WHERE id=? AND status='空闲'"
GUN_TAKE_CLAUSE = "status='占用' WHERE id=? AND status='可用'"

# 占用证据：桩/枪上仍在充的会话数
PILE_OPEN_SESSIONS_SQL = (
    f"(SELECT COUNT(*) FROM sessions se WHERE se.pile_id=p.id "
    f"AND se.status IN {OPEN_SESSION_STATUS_SQL})"
)
GUN_OPEN_SESSIONS_SQL = (
    f"(SELECT COUNT(*) FROM sessions se WHERE se.gun_id=g.id "
    f"AND se.status IN {OPEN_SESSION_STATUS_SQL})"
)
PILE_BUSY_GUNS_SQL = (
    "(SELECT COUNT(*) FROM guns g2 WHERE g2.pile_id=p.id AND g2.status='占用')"
)

# 空闲灯：状态空闲/可用 且 没有在充会话、且桩上无占用枪（与写侧占用钉同一证据）
PILE_IDLE_LAMP_SQL = (
    f"(p.status='空闲' AND {PILE_OPEN_SESSIONS_SQL}=0 AND {PILE_BUSY_GUNS_SQL}=0)"
)
GUN_IDLE_LAMP_SQL = f"(g.status='可用' AND {GUN_OPEN_SESSIONS_SQL}=0)"

# 异常占用的桩位态（非空闲仍被开出会话）
NON_IDLE_PILE_STATES = ("占用", "故障", "维护")
NON_AVAILABLE_GUN_STATES = ("占用", "故障")


def pile_openable(pile_row, site_row) -> bool:
    """读侧对象级重放：与 PILE_OPENABLE_SQL 完全等价。"""
    if pile_row is None:
        return False
    if pile_row["status"] != "空闲":
        return False
    return bool(site_row and site_row["active"])


def gun_openable(gun_row) -> bool:
    """读侧对象级重放：与 GUN_OPENABLE_SQL 完全等价。"""
    return gun_row is not None and gun_row["status"] == "可用"


def pile_evidence_conflict(status: str, open_sessions: int, busy_guns: int) -> bool:
    """桩位页证据撕裂：状态与占用钉/会话行对不上。

    正常态：空闲 ⇔ 无在充会话且无占用枪；占用 ⇒ 至少一笔在充会话。
    异常开充允许「故障/维护 + 在充会话」，不算撕裂。
    """
    if status == "空闲":
        return open_sessions > 0 or busy_guns > 0
    if status == "占用":
        return open_sessions == 0
    return False


def gun_evidence_conflict(status: str, open_sessions: int) -> bool:
    if status == "可用":
        return open_sessions > 0
    if status == "占用":
        return open_sessions == 0
    return False


def classify_open(pile_status: str, gun_status: str) -> tuple[bool, str]:
    """返回 (是否异常开充, 异常原因)。正常开充时原因为空串。

    口径：桩非空闲即异常；桩空闲但枪不可用亦异常（枪线占用态联动）。
    """
    reasons = []
    if pile_status != "空闲":
        reasons.append(f"桩位非空闲({pile_status})")
    if gun_status != "可用":
        reasons.append(f"枪线非可用({gun_status})")
    return (bool(reasons), "；".join(reasons))
