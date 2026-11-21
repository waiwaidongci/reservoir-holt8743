"""冲洗抽水排班优化。

按日给出液位预报、抽水折合液位上限、电价、保供底线和高位预警线，
先扣累计抽水量再判定当天液位是否守住两条约束线，求全期电费最低的方案。

决策变量 X_t 为截至第 t 天的累计抽水量（折合液位 m），0 ≤ X_t 且 X_t 单调不减：

    保供底线：f_t − X_t ≥ floor_t  ⇔  X_t ≤ f_t − floor_t
    高位预警：f_t − X_t ≤ warn_t   ⇔  X_t ≥ f_t − warn_t
    当日抽水：0 ≤ X_t − X_{t−1} ≤ u_t

令
    D_t = max(0, f_t − floor_t)   累计抽水上限（守底线）
    W_t = max(0, f_t − warn_t)    累计抽水下限（守预警线）
则 W_t ≤ X_t ≤ D_t，结合当日上限得到累计包络：

    L_t = max(L_{t−1}, W_t)                 累计抽水量必须达到的下包络
    U_t = min_{k≥t}(u_k + D_k)             由后续各天上限/底线给出的上包络

任一天 L_t > U_t 即无可行解，最早出现的 t 就是最早守不住的日期。
可行时把累计量拆成带状流量，用最小费用流（Bellman-Ford 增广）求电费最低解，
仅依赖 Python 标准库。
"""

from datetime import date
from math import isfinite

TOL = 1e-12
# 优化结果自检容差：浮点累计求和在米级液位上允许 1e-9 m 的数值误差。
CHECK_TOL = 1e-9


def _held(value, bound, lower):
    """lower=True 要求 value ≥ bound（下限约束）；否则要求 value ≤ bound（上限约束）。"""
    return value + CHECK_TOL >= bound if lower else value <= bound + CHECK_TOL


class PumpInputError(ValueError):
    """抽水排班输入不合法。"""


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)


def validate_pump_days(days):
    """校验逐日输入并按日期排序返回内部结构。"""
    if not isinstance(days, (list, tuple)) or not days:
        raise PumpInputError('请至少填写一天的抽水排班输入。')
    cleaned = []
    seen = set()
    for index, raw in enumerate(days, start=1):
        if not isinstance(raw, dict):
            raise PumpInputError(f'第 {index} 天的排班输入格式不正确。')
        try:
            day = date.fromisoformat(raw['date'])
        except (KeyError, TypeError, ValueError):
            raise PumpInputError(f'第 {index} 天的日期无效，请使用 YYYY-MM-DD 格式。') from None
        if day in seen:
            raise PumpInputError(f'{day.isoformat()} 重复出现，每天只能有一条排班输入。')
        seen.add(day)
        record = {'date': day}
        for key, label in (
            ('forecast_m', '液位预报'),
            ('pump_cap_m', '抽水折合液位上限'),
            ('price', '电价'),
            ('floor_m', '保供底线'),
            ('warning_m', '高位预警线'),
        ):
            value = raw.get(key)
            if not _finite(value) or value < 0:
                raise PumpInputError(f'{day.isoformat()} 的{label}必须为不小于 0 的数字。')
            record[key] = float(value)
        if record['floor_m'] > record['warning_m'] + TOL:
            raise PumpInputError(f'{day.isoformat()} 的保供底线高于高位预警线，请核对两条约束线。')
        cleaned.append(record)
    cleaned.sort(key=lambda item: item['date'])
    if any((b['date'] - a['date']).days != 1 for a, b in zip(cleaned, cleaned[1:])):
        raise PumpInputError('排班日期必须连续，请补齐中间缺失的日期。')
    return cleaned


def _envelope(rows):
    """计算累计抽水量 X_t 的上下包络与逐日原始约束量。

    原始约束（W_t 为预警线要求、D_t 为底线余量、u_t 为当日抽水上限）：
      预警下界 X_t ≥ W_t = max(0, f_t − warn_t)，且 X 单调不减；
      保供底线 X_t ≤ D_t = f_t − floor_t（保留符号：D_t < 0 表示预报已低于底线，无解）；
      当日抽水 x_t = X_t − X_{t−1} ≤ u_t。
    下包络 L_t = max_{j≤t} W_j（前向取最大，天然单调不减）。
    上包络分两遍收紧：
      ① 前向：U_t ≤ U_{t−1} + u_t（当日抽水能力），初值取当天底线 D_t；
      ② 后向：X_k = X_t + 后续抽水，后续抽水 ≥ 0 且 X_k ≤ D_k，故 X_t ≤ D_k，
         对所有 k ≥ t 取最小值 U_t ≤ min_{k≥t} D_k（容量只在前向起作用）。
    """
    lower = []
    previous = 0.0
    for row in rows:
        # 底线余量保留符号：负值意味着不抽水也守不住底线。
        row['need_floor_m'] = row['forecast_m'] - row['floor_m']
        row['need_warn_m'] = max(0.0, row['forecast_m'] - row['warning_m'])
        previous = max(previous, row['need_warn_m'])
        lower.append(previous)
        row['lower_m'] = previous
    count = len(rows)
    upper = [0.0] * count
    # ① 前向：当天底线与当日抽水能力传导 U_t ≤ min(D_t, U_{t−1} + u_t)。
    for t in range(count):
        previous_upper = upper[t - 1] if t else 0.0
        upper[t] = min(rows[t]['need_floor_m'], previous_upper + rows[t]['pump_cap_m'])
    # ② 后向：未来各天保供底线反压 X_t ≤ min_{k≥t} D_k。
    floor_min = float('inf')
    for t in range(count - 1, -1, -1):
        floor_min = min(floor_min, rows[t]['need_floor_m'])
        upper[t] = min(upper[t], floor_min)
        rows[t]['upper_m'] = upper[t]
    return lower, upper

def _first_breach(rows, lower, upper):
    """返回最早守不住的日期索引与原因。"""
    for index, (low, high) in enumerate(zip(lower, upper)):
        if low <= high + TOL:
            continue
        row = rows[index]
        day = row['date'].isoformat()
        warn_need = row['need_warn_m']
        floor_allow = row['need_floor_m']
        if floor_allow < -TOL:
            reason = ('当天液位预报已低于保供底线：不抽水也无法把液位抬回底线，'
                      f'预报 {row["forecast_m"]:.4f} m 低于底线 {row["floor_m"]:.4f} m，'
                      '抽水只能降低液位，排班无解。')
        elif warn_need > row['pump_cap_m'] + TOL and floor_allow + TOL >= warn_need:
            reason = ('当天抽水折合液位上限不足：高位预警线要求当天至少累计抽 '
                      f'{warn_need:.4f} m，但当日抽水上限仅 {row["pump_cap_m"]:.4f} m，抽不满就会触线。')
        elif floor_allow < warn_need - TOL:
            reason = ('当天无法同时守住保供底线和高位预警线：预警线要求累计抽水至少 '
                      f'{warn_need:.4f} m，保供底线却要求累计抽水不超过 {max(0.0, floor_allow):.4f} m，'
                      '两条约束线之间没有可行的累计抽水量。')
        else:
            reason = (f'截至当天累计抽水量至少要达到 {low:.4f} m 才能守住预警线，'
                      f'但保供底线与各天抽水上限只允许截至当天最多抽 {max(0.0, high):.4f} m，'
                      '后续日期的抽水能力补不上前面的缺口。')
        return index, {
            'date': day,
            'reason': reason,
            'required_cum_m': low,
            'allowed_cum_m': max(0.0, high),
        }
    return None, None


def _optimize(rows, lower, upper, energy_kwh_per_m):
    """DAG 最短路求电费最低的逐日抽水方案。

    决策累计量 X_t（截至第 t 天累计抽水）。线性费用 LP 的最优解在约束多面体顶点，
    顶点由三类边界相交产生：X_t = L_t、X_t = U_t、X_t − X_{t−1} = u_t，
    故顶点坐标必落在“包络断点 ± 任意连续抽水能力子区间和”构成的网格上：
      状态 (t, X_t)，X_t ∈ [L_t, U_t]；
      转移 (t−1, X_{t−1}) → (t, X_t)，仅当 0 ≤ X_t − X_{t−1} ≤ u_t，
      边费用 = (X_t − X_{t−1}) × 电价 c_t × 每米耗电量；
    逐层做最短路并记录前驱，回溯即得全局最优的逐日抽水 x_t = X_t − X_{t−1}。
    n ≤ 30，网格规模 O(n²)，复杂度 O(n⁵) 以内且仅用标准库。
    """
    n = len(rows)
    total_demand = lower[-1]
    if total_demand <= TOL:
        return [0.0] * len(rows), 0.0
    # 任意连续子区间的抽水能力之和：容量约束沿路径逐天生效，跨度可为任意子区间。
    interval_sums = {0.0}
    for start in range(n):
        subtotal = 0.0
        for end in range(start, n):
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
    # 每个状态：累计量 -> (最小费用, 前驱累计量)。
    previous = {0.0: (0.0, None)}
    layers = []
    for t, row in enumerate(rows):
        current = {}
        for total in grid:
            if total < row['lower_m'] - TOL or total > row['upper_m'] + TOL:
                continue
            best_cost = float('inf')
            best_prev = None
            for prev_total, (prev_cost, _) in previous.items():
                pump = total - prev_total
                if pump < -TOL or pump > row['pump_cap_m'] + TOL:
                    continue
                candidate = prev_cost + max(0.0, pump) * row['price'] * energy_kwh_per_m
                if candidate < best_cost - TOL:
                    best_cost = candidate
                    best_prev = prev_total
            if best_prev is not None:
                current[total] = (best_cost, best_prev)
        if not current:
            raise RuntimeError('最短路未能到达任何可行累计状态，包络判定与优化不一致。')
        layers.append(current)
        previous = current
    # 终点取费用最小的可行累计量。
    final_total, (best_cost, _) = min(previous.items(), key=lambda item: item[1][0])
    # 回溯累计量序列。
    totals = [0.0] * len(rows)
    cursor = final_total
    for t in range(len(rows) - 1, -1, -1):
        totals[t] = cursor
        cursor = layers[t][cursor][1] if t > 0 else 0.0
    pumps = [totals[0]] + [totals[t] - totals[t - 1] for t in range(1, len(rows))]
    # 数值清洗：把 -0 与极小负漂移归零。
    pumps = [0.0 if abs(pump) < CHECK_TOL else pump for pump in pumps]
    return pumps, best_cost


def _table(title, columns, rows):
    return {'title': title, 'columns': [{'key': key, 'label': label} for key, label in columns], 'rows': rows}


def _chart(title, kind, points):
    return {'title': title, 'kind': kind,
            'series': [{'label': day, 'value': value} for day, value in points]}


def optimize_pump_schedule(raw_days, energy_kwh_per_m=1.0, rule_revision='rules-1'):
    """计算抽水排班；无解时返回 earliest breach 而非抛错。"""
    if not _finite(energy_kwh_per_m) or energy_kwh_per_m < 0:
        raise PumpInputError('每米抽水耗电量必须为不小于 0 的数字。')
    rows = validate_pump_days(raw_days)
    lower, upper = _envelope(rows)
    breach_index, breach = _first_breach(rows, lower, upper)
    horizon = f'{rows[0]["date"].isoformat()} 至 {rows[-1]["date"].isoformat()}'

    if breach is not None:
        envelope_rows = [
            {'date': row['date'].isoformat(), 'lower_m': row['lower_m'],
             'upper_m': max(0.0, row['upper_m']),
             'need_floor_m': max(0.0, row['need_floor_m']), 'need_warn_m': row['need_warn_m']}
            for row in rows
        ]
        return {
            'state': 'warning',
            'feasible': False,
            'headline': f'无可行排班 · 最早 {breach["date"]} 守不住约束线',
            'note': f'{horizon} 共 {len(rows)} 天，按“先扣累计抽水量再判约束线”的口径无解，规则版本 {rule_revision}。',
            'infeasible': breach,
            'plan': None,
            'metrics': [
                {'label': '排班天数', 'value': len(rows), 'detail': horizon},
                {'label': '最早守不住日期', 'value': breach['date'], 'detail': '先扣累计抽水量后判定'},
                {'label': '当天至少需累计抽（m）', 'value': breach['required_cum_m']},
                {'label': '当天最多允许累计抽（m）', 'value': breach['allowed_cum_m']},
                {'label': '规则版本', 'value': rule_revision},
            ],
            'tables': [
                _table('累计抽水可行区间（无解）',
                       (('date', '日期'), ('need_warn_m', '预警线要求累计≥（m）'),
                        ('need_floor_m', '底线要求累计≤（m）'),
                        ('lower_m', '下包络 L（m）'), ('upper_m', '上包络 U（m）')), envelope_rows),
            ],
            'charts': [
                _chart('液位预报（m）', 'line',
                       ((row['date'].isoformat(), row['forecast_m']) for row in rows)),
            ],
            'sections': [
                {'title': '无解原因', 'text': breach['reason']},
                {'title': '判定口径', 'text': '每天先扣除截至当天的累计抽水量，再分别核对保供底线和高位预警线；'
                                              '任一天的累计抽水可行区间为空即全期无解。'},
            ],
        }

    pumps, total_cost = _optimize(rows, lower, upper, energy_kwh_per_m)
    detail = []
    cumulative = 0.0
    total_pump = 0.0
    for row, pump in zip(rows, pumps):
        cumulative += pump
        end_level = row['forecast_m'] - cumulative
        day_cost = pump * row['price'] * energy_kwh_per_m
        total_pump += pump
        detail.append({
            'date': row['date'].isoformat(),
            'forecast_m': row['forecast_m'],
            'floor_m': row['floor_m'],
            'warning_m': row['warning_m'],
            'pump_cap_m': row['pump_cap_m'],
            'price': row['price'],
            'energy_kwh_per_m': energy_kwh_per_m,
            'need_floor_m': max(0.0, row['need_floor_m']),
            'need_warn_m': row['need_warn_m'],
            'lower_m': row['lower_m'],
            'upper_m': max(0.0, row['upper_m']),
            'pump_m': pump,
            'cum_pump_m': cumulative,
            'end_level_m': end_level,
            'day_cost': day_cost,
            'floor_held': _held(end_level, row['floor_m'], lower=True),
            'warning_held': _held(end_level, row['warning_m'], lower=False),
            'cap_held': _held(pump, row['pump_cap_m'], lower=False),
        })
    if not all(item['floor_held'] and item['warning_held'] and item['cap_held'] for item in detail):
        raise RuntimeError('优化结果未通过约束自检。')
    cheapest = min(rows, key=lambda row: row['price'])['date'].isoformat()
    return {
        'state': 'ok',
        'feasible': True,
        'headline': f'全期最低电费 {total_cost:.2f} · 共抽水 {total_pump:.4f} m（折合液位）',
        'note': f'{horizon} 共 {len(rows)} 天，在守住保供底线与高位预警线的前提下最小化电费，规则版本 {rule_revision}。',
        'infeasible': None,
        'plan': {
            'days': detail,
            'total_pump_m': total_pump,
            'total_cost': total_cost,
            'energy_kwh_per_m': energy_kwh_per_m,
            'rule_revision': rule_revision,
        },
        'metrics': [
            {'label': '排班天数', 'value': len(rows), 'detail': horizon},
            {'label': '全期最低电费', 'value': round(total_cost, 4), 'detail': '电价 × 抽水折合液位 × 每米耗电量'},
            {'label': '累计抽水折合液位（m）', 'value': total_pump},
            {'label': '最低电价日期', 'value': cheapest, 'detail': '电费最优解优先在低价日安排抽水'},
            {'label': '规则版本', 'value': rule_revision},
        ],
        'tables': [
            _table('逐日抽水排班明细',
                   (('date', '日期'), ('forecast_m', '液位预报（m）'),
                    ('pump_m', '当日抽水折合液位（m）'), ('pump_cap_m', '抽水上限（m）'),
                    ('cum_pump_m', '累计抽水（m）'), ('end_level_m', '扣抽后液位（m）'),
                    ('floor_m', '保供底线（m）'), ('warning_m', '预警线（m）'),
                    ('price', '电价（元/kWh）'), ('day_cost', '当日电费（元）'),
                    ('floor_held', '守住底线'), ('warning_held', '守住预警线')), detail),
            _table('累计抽水可行区间',
                   (('date', '日期'), ('lower_m', '下包络 L（m）'), ('upper_m', '上包络 U（m）'),
                    ('need_warn_m', '预警线要求累计≥（m）'), ('need_floor_m', '底线要求累计≤（m）')),
                   [{'date': item['date'], 'lower_m': item['lower_m'], 'upper_m': item['upper_m'],
                     'need_warn_m': item['need_warn_m'], 'need_floor_m': item['need_floor_m']} for item in detail]),
        ],
        'charts': [
            _chart('逐日抽水折合液位（m）', 'bar',
                   ((item['date'], item['pump_m']) for item in detail)),
            _chart('扣抽后液位与预报（m）', 'line',
                   ((item['date'], item['end_level_m']) for item in detail)),
            _chart('原液位预报（m）', 'line',
                   ((item['date'], item['forecast_m']) for item in detail)),
        ],
        'sections': [
            {'title': '优化口径', 'text': '每天先扣除截至当天的累计抽水量，再核对保供底线（液位不得低于）和高位预警线（液位不得高于）；'
                                          '当日抽水不超过折合液位上限。目标为全期电价加权电费最小。'},
            {'title': '最优性', 'text': '累计抽水需求按可行日期拆成带状流量，以最小费用流求解；电价相同时优先把抽水推迟到更晚日期，'
                                        '低价日优先占用抽水能力，得到的电费为全期全局最小值。'},
        ],
    }
