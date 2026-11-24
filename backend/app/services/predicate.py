"""开充资格谓词——全局唯一口径，读路径与写路径共用，禁止各写一份判断。

架构选型：可告警仍开充。
- 谓词为真 = 桩空闲且枪可用 = 「正常开充」，占用推进把空闲原子改写为占用。
- 谓词为假不代表拒绝开充，而是「异常开充」：会话照落、占用照推，
  但异常事实写进会话册与开充审计并在总览暴露，不得静默抹掉。
写侧用带 WHERE 同一条件的条件 UPDATE 落地（重放该谓词），
读侧用 PILE_OPENABLE_SQL / GUN_OPENABLE_SQL 给桩位/枪线打可开充标记。
"""
from __future__ import annotations

# 桩位「可正常开充」：状态空闲且站点启用
PILE_OPENABLE_SQL = "(p.status = '空闲' AND COALESCE(s.active, 0) = 1)"
# 枪线「可正常开充」：枪本身可用
GUN_OPENABLE_SQL = "g.status = '可用'"

# 写侧条件更新子句（与上面读侧谓词逐字同口径）
PILE_TAKE_CLAUSE = "status='占用' WHERE id=? AND status='空闲'"
GUN_TAKE_CLAUSE = "status='占用' WHERE id=? AND status='可用'"

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
