"""抽水排班代次/激活/防顶回语义测试（无第三方依赖，用 stub 替换数据库层）。

运行：python3 -I tests/test_pump_service.py
"""

import sys
import types
from pathlib import Path


def _install_stubs():
    class _Chain:
        def where(self, *a, **k):
            return self

        def with_for_update(self, **k):
            return self

        def order_by(self, *a, **k):
            return self

        def limit(self, *a, **k):
            return self

    class _Col:
        def is_(self, value):
            return value

    for name in ('sqlalchemy', 'sqlalchemy.orm', 'sqlalchemy.exc'):
        sys.modules[name] = types.ModuleType(name)
    sys.modules['sqlalchemy'].select = lambda *a, **k: _Chain()
    sys.modules['sqlalchemy'].func = types.SimpleNamespace(
        coalesce=lambda *a: a, max=lambda x: x)
    sys.modules['sqlalchemy.orm'].Mapped = object
    sys.modules['sqlalchemy.orm'].mapped_column = lambda *a, **k: None
    sys.modules['sqlalchemy.orm'].Session = object
    for name in ('src.config', 'src.config.database', 'src.config.settings', 'src.models',
                 'src.models.jobs', 'src.services.audit', 'src.utils', 'src.utils.time'):
        sys.modules[name] = types.ModuleType(name)
    sys.modules['src.config.database'].Base = type('Base', (), {})
    sys.modules['src.config.database'].Session = object

    class _PumpPlan:
        generation = None
        is_active = _Col()

    sys.modules['src.models.jobs'].PumpPlan = _PumpPlan
    sys.modules['src.models.jobs'].Run = type('Run', (), {})
    sys.modules['src.services.audit'].record = lambda *a, **k: None
    sys.modules['src.utils.time'].now = lambda: 1
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


_install_stubs()

from src.constants.enums import PumpStatus  # noqa: E402
from src.services import pump as service  # noqa: E402

_DAYS = [
    {'date': '2026-11-21', 'forecast_m': 2.2, 'pump_cap_m': 0.3, 'price': 0.8,
     'floor_m': 1.0, 'warning_m': 2.05},
    {'date': '2026-11-22', 'forecast_m': 2.3, 'pump_cap_m': 0.3, 'price': 0.6,
     'floor_m': 1.0, 'warning_m': 2.05},
]


class FakePlan:
    def __init__(self, plan_id, generation, status=PumpStatus.RUNNING, attempt=1, active=False):
        self.id = plan_id
        self.generation = generation
        self.status = status
        self.attempt = attempt
        self.is_active = active
        self.lease_until = 1
        self.finished_at = None
        self.result = None
        self.error = None
        self.rule_revision = 'rules-2026-11-21'
        self.inputs = {'energy_kwh_per_m': 1.0, 'days': _DAYS}


class FakeDB:
    def __init__(self, plans, max_generation):
        self.plans = plans
        self.max_generation = max_generation

    def get(self, model, plan_id, with_for_update=False):
        return next((plan for plan in self.plans if plan.id == plan_id), None)

    def scalar(self, query):
        return self.max_generation

    def scalars(self, query):
        return [plan for plan in self.plans if plan.is_active]

    def add(self, obj):
        pass


def test_stale_generation_cannot_replace_active():
    old = FakePlan(1, 1)
    current = FakePlan(2, 2, status=PumpStatus.COMPLETE, active=True)
    service.finish_pump(FakeDB([old, current], 2), old)
    assert old.status == PumpStatus.SUPERSEDED and old.is_active is False
    assert current.is_active is True and current.status == PumpStatus.COMPLETE


def test_latest_generation_switches_atomically():
    previous = FakePlan(1, 1, status=PumpStatus.COMPLETE, active=True)
    latest = FakePlan(2, 2)
    service.finish_pump(FakeDB([previous, latest], 2), latest)
    assert latest.status == PumpStatus.COMPLETE and latest.is_active is True
    assert previous.is_active is False


def test_infeasible_result_activates_with_breach_date():
    plan = FakePlan(3, 3)
    plan.inputs = {'energy_kwh_per_m': 1.0, 'days': [
        {'date': '2026-11-21', 'forecast_m': 2.55, 'pump_cap_m': 0.2, 'price': 0.5,
         'floor_m': 1.0, 'warning_m': 2.05},
        {'date': '2026-11-22', 'forecast_m': 2.6, 'pump_cap_m': 0.0, 'price': 0.5,
         'floor_m': 1.0, 'warning_m': 2.05},
    ]}
    service.finish_pump(FakeDB([plan], 3), plan)
    assert plan.status == PumpStatus.COMPLETE and plan.is_active is True
    assert plan.result['feasible'] is False
    assert plan.result['infeasible']['date'] == '2026-11-21'


def test_invalid_input_fails_without_activation():
    plan = FakePlan(4, 4)
    plan.inputs = {'energy_kwh_per_m': 1.0, 'days': [
        {'date': '2026-11-21', 'forecast_m': -1, 'pump_cap_m': 0.2, 'price': 0.5,
         'floor_m': 1.0, 'warning_m': 2.05},
    ]}
    service.finish_pump(FakeDB([plan], 4), plan)
    assert plan.status == PumpStatus.FAILED and plan.is_active is False and plan.error


if __name__ == '__main__':
    test_stale_generation_cannot_replace_active()
    print('ok · 较早发起晚完成的旧代次被标记 superseded，顶不回当前方案')
    test_latest_generation_switches_atomically()
    print('ok · 最新代次完成后一次性切换，旧方案下线')
    test_infeasible_result_activates_with_breach_date()
    print('ok · 无解时记录最早守不住日期并作为最新结论激活')
    test_invalid_input_fails_without_activation()
    print('ok · 非法输入标记 FAILED 且不影响当前方案')
    print('代次/激活/防顶回语义全部正确')
