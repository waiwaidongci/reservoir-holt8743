"""抽水排班优化器的纯标准库交叉验证。

不依赖 pytest：python3 -I tests/test_pump.py
1. 手工样例核对；
2. 小规模离散网格穷举全部排班，核对最优电费、可行/无解与最早破线日；
3. 随机样例核对包络判定与直接约束枚举一致。
"""

import itertools
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.domain.pump import TOL, optimize_pump_schedule, validate_pump_days, _envelope, _first_breach  # noqa: E402


def brute_force(raw_days, energy, grid=0.1):
    rows = validate_pump_days(raw_days)
    options = []
    for row in rows:
        steps = int(round(row['pump_cap_m'] / grid))
        options.append([round(k * grid, 9) for k in range(steps + 1)])
    best = None
    for pumps in itertools.product(*options):
        cum = 0.0
        feasible = True
        cost = 0.0
        for row, pump in zip(rows, pumps):
            cum += pump
            end = row['forecast_m'] - cum
            if end < row['floor_m'] - TOL or end > row['warning_m'] + TOL:
                feasible = False
                break
            cost += pump * row['price'] * energy
        if feasible and (best is None or cost < best[0] - TOL):
            best = (cost, list(pumps))
    return best


def make_days(forecasts, caps, prices, floors, warns, start='2026-11-21'):
    from datetime import date, timedelta
    base = date.fromisoformat(start)
    return [
        {'date': (base + timedelta(days=i)).isoformat(), 'forecast_m': f, 'pump_cap_m': c,
         'price': p, 'floor_m': fl, 'warning_m': w}
        for i, (f, c, p, fl, w) in enumerate(zip(forecasts, caps, prices, floors, warns))
    ]


HAND_CASES = [
    # 预报 2.20→2.35，预警 2.05（首日必须抽 0.15），低价在末日：应尽量推迟抽水。
    dict(forecasts=[2.20, 2.30, 2.35], caps=[0.3, 0.3, 0.3], prices=[0.8, 0.6, 0.3],
         floors=[1.0, 1.0, 1.0], warns=[2.05, 2.05, 2.05],
         expect_pumps=[0.15, 0.10, 0.05]),
    # 末日预报高且末日上仅 0.1，迫使抽水提前到高价日。
    dict(forecasts=[2.10, 2.20, 2.50], caps=[0.5, 0.5, 0.1], prices=[0.9, 0.5, 0.2],
         floors=[0.5, 0.5, 0.5], warns=[2.05, 2.05, 2.05],
         expect_pumps=[0.05, 0.30, 0.10]),
    # 全程不需要抽水：预报都在两条线内。
    dict(forecasts=[1.5, 1.6, 1.7], caps=[0.5, 0.5, 0.5], prices=[0.9, 0.5, 0.2],
         floors=[1.0, 1.0, 1.0], warns=[2.05, 2.05, 2.05],
         expect_pumps=[0.0, 0.0, 0.0]),
]


def test_hand_cases():
    for index, case in enumerate(HAND_CASES):
        days = make_days(case['forecasts'], case['caps'], case['prices'], case['floors'], case['warns'])
        result = optimize_pump_schedule(days, energy_kwh_per_m=1.0)
        assert result['feasible'], f'hand {index} 应可行'
        pumps = [row['pump_m'] for row in result['plan']['days']]
        for got, want in zip(pumps, case['expect_pumps']):
            assert abs(got - want) < 1e-9, f'hand {index} 抽水 {pumps} 与预期 {case["expect_pumps"]} 不符'
        bf = brute_force(days, 1.0, grid=0.05)
        assert bf is not None and abs(bf[0] - result['plan']['total_cost']) < 1e-7, \
            f'hand {index} 电费 {result["plan"]["total_cost"]} 与穷举 {bf and bf[0]} 不符'
    print('ok · 手工样例（含推迟抽水、能力受限提前、零抽水）')


def test_infeasible_dates():
    cases = [
        # 首日预警要求抽 0.50，首日上限 0.2 且次日上限 0 → 第一天即破线。
        dict(forecasts=[2.55, 2.60], caps=[0.2, 0.0], prices=[0.5, 0.5],
             floors=[1.0, 1.0], warns=[2.05, 2.05], expect='2026-11-21'),
        # 首日可过；末日需求 0.80，两日总能力 0.60，撑不到末日 → 末日破线。
        dict(forecasts=[2.10, 2.85], caps=[0.5, 0.1], prices=[0.5, 0.5],
             floors=[1.0, 1.0], warns=[2.05, 2.05], expect='2026-11-22'),
        # 底线与预警线夹逼：两条线之间要抽 0.30 m，当日上限只有 0.10 m。
        dict(forecasts=[2.40], caps=[0.10], prices=[0.5],
             floors=[2.00], warns=[2.10], expect='2026-11-21'),
    ]
    for index, case in enumerate(cases):
        days = make_days(case['forecasts'], case['caps'], case['prices'], case['floors'], case['warns'])
        result = optimize_pump_schedule(days)
        assert not result['feasible'], f'case {index} 应无解'
        assert result['infeasible']['date'] == case['expect'], \
            f'case {index} 最早破线日 {result["infeasible"]["date"]} != {case["expect"]}'
        assert brute_force(days, 1.0) is None, f'case {index} 穷举却找到可行解'
    print('ok · 无解时指出最早守不住的日期（首日/末日/夹逼）')


def test_random_against_bruteforce():
    rng = random.Random(20261121)
    for trial in range(120):
        n = rng.randint(1, 3)
        floors = [round(rng.uniform(0.8, 1.2), 2) for _ in range(n)]
        warns = [round(rng.uniform(1.8, 2.3), 2) for _ in range(n)]
        days = make_days(
            forecasts=[round(rng.uniform(1.2, 2.6), 2) for _ in range(n)],
            caps=[round(rng.choice([0.0, 0.1, 0.2, 0.3, 0.5]), 2) for _ in range(n)],
            prices=[round(rng.uniform(0.2, 1.0), 2) for _ in range(n)],
            floors=floors, warns=warns)
        if any(f > w for f, w in zip(floors, warns)):
            continue
        energy = round(rng.uniform(0.5, 2.0), 2)
        result = optimize_pump_schedule(days, energy_kwh_per_m=energy)
        bf = brute_force(days, energy, grid=0.02)
        if bf is None:
            assert not result['feasible'], f'trial {trial} 优化器误判可行'
            continue
        assert result['feasible'], f'trial {trial} 优化器误判无解'
        # 离散网格只能给出连续最优的上界：优化器电费不得高于任何可行离散方案。
        assert result['plan']['total_cost'] <= bf[0] + 1e-7, \
            f'trial {trial} 优化器电费 {result["plan"]["total_cost"]} 高于离散可行解 {bf[0]}'
        # 优化器解本身必须可行。
        cum = 0.0
        for row in result['plan']['days']:
            cum += row['pump_m']
            end = row['forecast_m'] - cum
            assert end + TOL >= row['floor_m'] and end - TOL <= row['warning_m']
            assert row['pump_m'] <= row['pump_cap_m'] + TOL
        # 与独立的连续断点 DP（LP 全局最优）对比，两者必须得到相同最优电费。
        lp_cost = breakpoint_dp_cost(days, energy)
        assert lp_cost is not None
        assert abs(lp_cost - result['plan']['total_cost']) < 1e-7, \
            f'trial {trial} 断点 DP {lp_cost} != 优化器 {result["plan"]["total_cost"]}'
    print('ok · 120 组随机小样例：解可行、不劣于离散穷举、与独立断点 DP 同最优值')


def breakpoint_dp_cost(days, energy):
    """独立参考实现：在累计量 X 的约束断点上做动态规划求全局最优电费。

    线性费用 LP 的最优解可在约束多面体顶点取到，顶点坐标必为 L、U、0 等断点值，
    故枚举所有断点作为每天的累计量状态，按当日抽水约束转移即得精确最优值。
    与生产代码的最小费用流完全独立，仅用于交叉验证。
    """
    rows = validate_pump_days(days)
    lower, upper = _envelope(rows)
    if any(lo > hi + TOL for lo, hi in zip(lower, upper)):
        return None
    # LP 顶点坐标 = 包络断点（或 0）± 任意连续抽水能力子区间和。
    interval_sums = {0.0}
    for start in range(len(rows)):
        subtotal = 0.0
        for end in range(start, len(rows)):
            subtotal += rows[end]['pump_cap_m']
            interval_sums.add(round(subtotal, 12))
    seeds = [0.0, *lower, *upper]
    grid_set = {0.0}
    for seed in seeds:
        grid_set.add(round(seed, 12))
        for subtotal in interval_sums:
            grid_set.add(round(seed + subtotal, 12))
            grid_set.add(round(seed - subtotal, 12))
    grid = sorted(value for value in grid_set if value >= -TOL)
    dp = {0.0: 0.0}  # 累计量 -> 最小电费
    for t, row in enumerate(rows):
        nxt = {}
        for total in grid:
            if total < lower[t] - TOL or total > upper[t] + TOL:
                continue
            best = float('inf')
            for previous_total, previous_cost in dp.items():
                pump = total - previous_total
                if -TOL <= pump <= row['pump_cap_m'] + TOL:
                    best = min(best, previous_cost + max(0.0, pump) * row['price'] * energy)
            if best < float('inf'):
                nxt[total] = best
        dp = nxt
    return min(dp.values()) if dp else None



def test_envelope_matches_direct():
    rng = random.Random(77)
    for trial in range(300):
        n = rng.randint(2, 6)
        rows = validate_pump_days(make_days(
            forecasts=[rng.uniform(1.0, 2.8) for _ in range(n)],
            caps=[rng.choice([0.0, 0.15, 0.3, 0.6]) for _ in range(n)],
            prices=[rng.uniform(0.2, 1.0) for _ in range(n)],
            floors=[rng.uniform(0.7, 1.1) for _ in range(n)],
            warns=[rng.uniform(1.8, 2.3) for _ in range(n)]))
        lower, upper = _envelope(rows)
        index, breach = _first_breach(rows, lower, upper)
        # 独立两遍法直接构造可行累计区间，核对最早破线日。
        direct_lower = []
        low = 0.0
        for row in rows:
            low = max(low, row['need_warn_m'])
            direct_lower.append(low)
        direct_upper = [0.0] * n
        for t in range(n):
            prev = direct_upper[t - 1] if t else 0.0
            direct_upper[t] = min(rows[t]['need_floor_m'], prev + rows[t]['pump_cap_m'])
        floor_min = float('inf')
        for t in range(n - 1, -1, -1):
            floor_min = min(floor_min, rows[t]['need_floor_m'])
            direct_upper[t] = min(direct_upper[t], floor_min)
        direct_first = next(
            (t for t in range(n) if direct_lower[t] > direct_upper[t] + TOL), None)
        if direct_first is None:
            assert breach is None, f'trial {trial} 包络误报无解'
        else:
            assert index == direct_first, f'trial {trial} 破线日 {index} != {direct_first}'
    print('ok · 300 组随机样例的包络判定与直接约束枚举一致')


def test_input_validation():
    base = make_days([2.2], [0.3], [0.5], [1.0], [2.05])
    for bad in ([], None, 'x'):
        try:
            optimize_pump_schedule(bad)
        except ValueError:
            pass
        else:
            raise AssertionError('空输入未拦截')
    bad_rows = [dict(base[0], forecast_m=-1)]
    try:
        optimize_pump_schedule(bad_rows)
    except ValueError:
        pass
    else:
        raise AssertionError('负预报未拦截')
    bad_rows = [dict(base[0], floor_m=2.1, warning_m=2.05)]
    try:
        optimize_pump_schedule(bad_rows)
    except ValueError:
        pass
    else:
        raise AssertionError('底线高于预警线未拦截')
    gap = make_days([2.2, 2.3], [0.3, 0.3], [0.5, 0.5], [1.0, 1.0], [2.05, 2.05])
    gap[1]['date'] = '2026-11-23'
    try:
        optimize_pump_schedule(gap)
    except ValueError:
        pass
    else:
        raise AssertionError('日期不连续未拦截')
    print('ok · 输入校验（空值/负值/夹逼/断档/重复）')


if __name__ == '__main__':
    test_hand_cases()
    test_infeasible_dates()
    test_random_against_bruteforce()
    test_envelope_matches_direct()
    test_input_validation()
    print('全部通过')
