"""抽水排班的代次管理、租约与一次性切换。"""

import logging
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.constants.enums import PumpStatus
from src.domain.pump import PumpInputError, optimize_pump_schedule
from src.domain.spec import SPEC
from src.models.jobs import PumpPlan, Run
from src.services.audit import record
from src.utils.time import now

log = logging.getLogger(__name__)
_LEASE_MINUTES = 3
PUMP_RULE_REVISION = SPEC['pump']['rule_revision']


def _check_source_run(db: Session, source_run_id, days):
    """若指定来源预测运行，校验逐日预报与运行结果快照一致。"""
    if not source_run_id:
        return
    run = db.get(Run, source_run_id)
    if not run:
        raise PumpInputError('找不到作为预报来源的预测运行，请重新选择。')
    if not run.result:
        raise PumpInputError('来源预测运行尚无预报结果，请等待其计算完成。')
    forecast_table = next((table for table in run.result['tables']
                           if table['title'] == '逐日预报'), None)
    if forecast_table is None:
        raise PumpInputError('来源预测运行缺少逐日预报，无法核对液位预报。')
    forecasts = {row['date']: row['selected'] for row in forecast_table['rows']}
    for day in days:
        if day['date'] not in forecasts:
            raise PumpInputError(f'{day["date"]} 不在来源运行的逐日预报范围内，请调整排班日期。')
        if abs(forecasts[day['date']] - day['forecast_m']) > 1e-9:
            raise PumpInputError(
                f'{day["date"]} 的液位预报与来源运行快照不一致，重算期间预报以发起时快照为准。')


def create_plan(db: Session, payload: dict, author: str):
    """登记一次排班计算，分配递增代次；不影响当前 active 方案。"""
    days = [day if isinstance(day, dict) else day.model_dump() for day in payload['days']]
    _check_source_run(db, payload.get('source_run_id'), days)
    # 领域层做完整数值与连续性校验，提前拒绝非法输入。
    optimize_snapshot_inputs(days, payload['energy_kwh_per_m'])
    generation = (db.scalar(select(func.coalesce(func.max(PumpPlan.generation), 0))) or 0) + 1
    plan = PumpPlan(
        name=payload['name'], generation=generation, rule_revision=PUMP_RULE_REVISION,
        source_run_id=payload.get('source_run_id'), status=PumpStatus.QUEUED,
        inputs={'energy_kwh_per_m': payload['energy_kwh_per_m'], 'days': days},
        author=author,
    )
    db.add(plan)
    db.flush()
    record(db, author, 'pump.queued', plan.id, generation=generation,
           rule_revision=PUMP_RULE_REVISION, days=len(days))
    return plan


def optimize_snapshot_inputs(days, energy_kwh_per_m):
    """纯计算入口：供服务层预检与 worker 复用，返回领域结果字典。"""
    return optimize_pump_schedule(days, energy_kwh_per_m=energy_kwh_per_m,
                                  rule_revision=PUMP_RULE_REVISION)


def claim_pump(db: Session):
    """领取一个排队或租约过期的排班计算，行级锁防止多 worker 重复执行。"""
    stamp = now()
    plan = db.scalar(
        select(PumpPlan)
        .where(PumpPlan.status.in_((PumpStatus.QUEUED, PumpStatus.RUNNING)))
        .order_by(PumpPlan.id)
        .limit(1)
        .with_for_update(skip_locked=True))
    if plan is None:
        return None
    if plan.status == PumpStatus.RUNNING and plan.lease_until and plan.lease_until > stamp:
        return None
    if plan.attempt >= 3:
        plan.status = PumpStatus.FAILED
        plan.error = '后台进程多次中断，请发起新的排班计算'
        plan.finished_at = stamp
        record(db, 'worker', 'pump.failed', plan.id, reason=plan.error)
        return None
    plan.status = PumpStatus.RUNNING
    plan.attempt += 1
    plan.lease_until = stamp + timedelta(minutes=_LEASE_MINUTES)
    return plan


def finish_pump(db: Session, plan: PumpPlan):
    """执行计算并按代次决定激活或标记取代，调用时 plan 已在当前事务内锁定。"""
    stamp = now()
    try:
        result = optimize_snapshot_inputs(plan.inputs['days'], plan.inputs['energy_kwh_per_m'])
        failure = None
    except PumpInputError as error:
        result, failure = None, str(error)[:500]
    except Exception:
        log.exception('Pump plan %s failed', plan.id)
        result, failure = None, '排班计算失败，请检查逐日输入后重试'

    attempt_snapshot = plan.attempt
    locked = db.get(PumpPlan, plan.id, with_for_update=True)
    if locked is None or locked.status != PumpStatus.RUNNING or locked.attempt != attempt_snapshot:
        return  # 已被其他流程处理，本次结果丢弃。
    latest_generation = db.scalar(select(func.coalesce(func.max(PumpPlan.generation), 0))) or 0
    locked.lease_until = None
    locked.finished_at = stamp
    if failure:
        locked.status = PumpStatus.FAILED
        locked.error = failure
        record(db, 'worker', 'pump.failed', locked.id, generation=locked.generation, reason=failure)
        return
    locked.result = result
    locked.error = None
    if locked.generation != latest_generation:
        # 较早发起的计算晚完成： newer 代次已存在，绝不能顶回当前方案。
        locked.status = PumpStatus.SUPERSEDED
        record(db, 'worker', 'pump.superseded', locked.id, generation=locked.generation,
               latest_generation=latest_generation)
        return
    # 最新代次完成：一次性切换——先下线旧 active，再激活本方案。
    previous = db.scalars(select(PumpPlan).where(PumpPlan.is_active.is_(True)).with_for_update())
    for old in previous:
        old.is_active = False
    locked.status = PumpStatus.COMPLETE
    locked.is_active = True
    record(db, 'worker', 'pump.activated', locked.id, generation=locked.generation,
           feasible=result['feasible'])


def list_plans(db: Session):
    return db.scalars(select(PumpPlan).order_by(PumpPlan.id.desc()).limit(100)).all()


def active_plan(db: Session):
    return db.scalar(select(PumpPlan).where(PumpPlan.is_active.is_(True)).limit(1))
