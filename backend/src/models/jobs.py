from datetime import datetime
from sqlalchemy import JSON, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base
from src.constants.enums import PumpStatus, Review, Status
from src.utils.time import now

class Schedule(Base):
    __tablename__ = 'schedules'

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'))
    params: Mapped[dict] = mapped_column(JSON)
    recipient: Mapped[str] = mapped_column(String(100))
    next_at: Mapped[datetime] = mapped_column(index=True)
    repeat_daily: Mapped[bool]
    enabled: Mapped[bool] = mapped_column(default=True)
    author: Mapped[str] = mapped_column(String(64))

class Run(Base):
    __tablename__ = 'runs'
    __table_args__ = (UniqueConstraint('schedule_id', 'scheduled_at'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'))
    schedule_id: Mapped[int | None] = mapped_column(ForeignKey('schedules.id'))
    scheduled_at: Mapped[datetime | None]
    name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default=Status.QUEUED, index=True)
    params: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(String(500))
    export_file: Mapped[str | None] = mapped_column(String(100))
    review: Mapped[str] = mapped_column(String(20), default=Review.PENDING)
    review_note: Mapped[str | None] = mapped_column(String(500))
    decision: Mapped[str | None] = mapped_column(String(120))
    attempt: Mapped[int] = mapped_column(default=0)
    lease_until: Mapped[datetime | None]
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)
    finished_at: Mapped[datetime | None]

class Delivery(Base):
    __tablename__ = 'deliveries'

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey('runs.id'), unique=True)
    recipient: Mapped[str] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(String(20))
    detail: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(default=now)


class PumpPlan(Base):
    """一次抽水排班计算及其版本化结果。

    规则更新后旧方案失效：每次发起计算生成递增 generation。只有最新一代的完成结果
    能成为当前方案（is_active）；较早发起、较晚完成的计算在激活时发现自己不是最新代，
    直接标记 superseded，不能顶回当前方案。重算期间旧的 active 方案照常展示。
    """
    __tablename__ = 'pump_plans'
    __table_args__ = (
        Index('ix_pump_active', 'is_active', postgresql_where='is_active'),
        Index('ix_pump_generation', 'generation'),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    generation: Mapped[int]
    rule_revision: Mapped[str] = mapped_column(String(64))
    source_run_id: Mapped[int | None] = mapped_column(ForeignKey('runs.id'))
    status: Mapped[str] = mapped_column(String(20), default=PumpStatus.QUEUED, index=True)
    # 发起时快照的逐日输入（预报、上限、电价、底线、预警线）与每米耗电量。
    inputs: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(String(500))
    is_active: Mapped[bool] = mapped_column(default=False, index=True)
    attempt: Mapped[int] = mapped_column(default=0)
    lease_until: Mapped[datetime | None]
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)
    finished_at: Mapped[datetime | None]
