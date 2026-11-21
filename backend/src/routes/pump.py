from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config.database import connection
from src.config.settings import ARTIFACTS
from src.constants.enums import Role
from src.controllers.auth import allow, current
from src.models.identity import User
from src.models.jobs import PumpPlan
from src.schemas.requests import PumpPlanRequest
from src.services import pump as service
from src.services.audit import record
from src.utils import views
from src.utils.exports import write_pump_report
from src.utils.wakeup import wake

router = APIRouter(prefix='/pump-plans')


def require_plan(db: Session, plan_id: int, lock=False):
    item = db.get(PumpPlan, plan_id, with_for_update=lock)
    if not item:
        raise HTTPException(404, '找不到该抽水排班方案')
    return item


@router.get('')
def index(db: Session = Depends(connection), actor: User = Depends(current)):
    return [views.pump_plan(item) for item in service.list_plans(db)]


@router.get('/active')
def active(db: Session = Depends(connection), actor: User = Depends(current)):
    item = service.active_plan(db)
    return views.pump_plan(item, detail=True) if item else None


@router.post('', status_code=202)
def create(body: PumpPlanRequest, db: Session = Depends(connection),
           actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    payload = body.model_dump()
    try:
        plan = service.create_plan(db, payload, actor.username)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    db.commit()
    wake()
    return views.pump_plan(plan, detail=True)


@router.get('/{plan_id}')
def detail(plan_id: int, db: Session = Depends(connection), actor: User = Depends(current)):
    return views.pump_plan(require_plan(db, plan_id), detail=True)


@router.post('/{plan_id}/activate')
def activate(plan_id: int, db: Session = Depends(connection),
             actor: User = Depends(allow(Role.ADMIN))):
    plan = require_plan(db, plan_id, lock=True)
    if plan.status != 'complete':
        raise HTTPException(409, '只有计算完成（含无解结论）的最新方案可以切换为当前方案')
    newer = db.scalar(select(PumpPlan.id).where(PumpPlan.generation > plan.generation).limit(1))
    if newer is not None:
        raise HTTPException(409, '已存在更新代次的排班方案，旧方案不能顶回当前方案')
    for old in db.scalars(select(PumpPlan).where(PumpPlan.is_active.is_(True)).with_for_update()):
        old.is_active = False
    plan.is_active = True
    record(db, actor.username, 'pump.activated_manually', plan.id, generation=plan.generation)
    db.commit()
    return views.pump_plan(plan, detail=True)


@router.get('/{plan_id}/export')
def export(plan_id: int, db: Session = Depends(connection), actor: User = Depends(current)):
    plan = require_plan(db, plan_id)
    if not plan.result:
        raise HTTPException(409, '此排班尚无可下载结果')
    name = write_pump_report(plan.id, plan.attempt, plan.result)
    path = ARTIFACTS / name
    if not path.is_file():
        raise HTTPException(410, '导出文件已不可用，请重新发起排班')
    return FileResponse(path, media_type='text/csv',
                        filename=f'pump-schedule-{plan.id}.csv')
