from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from app.db import db, db_immediate
from app.services import core
from app.services import sessions as svc
from app.services.sessions import OpenRejected, OpenTorn

router = APIRouter()


class OpenIn(BaseModel):
    桩位编号: int
    枪线编号: int | None = None


class PulseIn(BaseModel):
    电量: float = Field(gt=0)
    功率: float = 0


class AbortIn(BaseModel):
    原因: str = "急停"


@router.get("/会话")
def list_sessions(状态: str | None = None):
    with db() as conn:
        return core.list_sessions(conn, 状态)


@router.get("/会话/{session_id}")
def get_session(session_id: int):
    with db() as conn:
        d = core.session_detail(conn, session_id)
        if not d:
            raise HTTPException(404, detail="会话不存在")
        return d


@router.post("/开会话")
def open_session(body: OpenIn):
    # 写侧唯一入口：立即事务串行化并发开充；成功四者同命运，失败整笔回滚。
    try:
        with db_immediate() as conn:
            return svc.open_session(conn, body.桩位编号, body.枪线编号)
    except OpenRejected as e:
        # 硬拒绝：主事务未留任何会话/占用痕迹；用独立短事务补登审计，保证拒绝可核。
        with db() as audit_conn:
            svc.insert_open_audit(audit_conn, e.audit)
        raise HTTPException(400, detail=e.detail)
    except OpenTorn as e:
        # 四者同命运校验失败：主事务已整笔回滚，绝不返回成功回执。
        raise HTTPException(409, detail=f"开充未能同刷，已整笔回滚：{e}") from e


@router.get("/开充审计")
def list_open_audits(营业日: str | None = None):
    with db() as conn:
        return core.list_open_audits(conn, 营业日)


@router.post("/会话/{session_id}/脉冲")
def add_pulse(session_id: int, body: PulseIn):
    with db_immediate() as conn:
        return svc.add_pulse(conn, session_id, body.电量, body.功率)


@router.post("/会话/{session_id}/结算")
def settle(session_id: int):
    with db_immediate() as conn:
        return svc.settle(conn, session_id)


@router.post("/会话/{session_id}/超时")
def timeout(session_id: int):
    with db_immediate() as conn:
        return svc.mark_timeout(conn, session_id)


@router.post("/会话/{session_id}/中断")
def abort(session_id: int, body: AbortIn | None = None):
    with db_immediate() as conn:
        reason = body.原因 if body else "急停"
        return svc.abort_session(conn, session_id, reason)


def _surface_router_sessions() -> dict:
    return {"panel": "sessions", "ready": True}
