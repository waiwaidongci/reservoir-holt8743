"""Chronological reservoir forecasts with separated selection and holdout."""

from datetime import date, timedelta
from math import isclose, sqrt
from statistics import fmean

from .spec import SPEC


_DAY = timedelta(days=1)
_MIN_TRAINING = 3
_MIN_ROLLING = 2
_ALERT_TOLERANCE_M = 1e-12
_MODELS = {"holt": "Holt 趋势", "level_only": "仅水平"}
# Prefer the simpler model when the errors are equal.
_SELECTION_ORDER = ("level_only", "holt")
_PREDICTIONS = (("holt", "Holt 趋势（m）"), ("level_only", "仅水平（m）"))
_EVALUATION = (("date", "实测日期"), ("step", "提前天数"), ("actual", "实测（m）")) + _PREDICTIONS


def validate_rows(rows):
    """Validate a single pool's daily readings and return them in date order."""
    readings = {}
    reservoir_id = None
    for index, row in enumerate(rows, start=1):
        try:
            pool = row["reservoir_id"].strip()
            day = date.fromisoformat(row["reading_date"])
            level = row["level_m"]
            if not pool or len(pool) > 40 or isinstance(level, bool) or not isinstance(level, (int, float)):
                raise ValueError
            if not 0 <= level <= 20:
                raise ValueError
        except (AttributeError, KeyError, TypeError, ValueError):
            raise ValueError(f"第 {index} 条晨检读数无效，请检查水池编号、日期和 0–20 m 液位。") from None
        if reservoir_id is not None and pool != reservoir_id:
            raise ValueError("同一批次只能预测一座水池，请按水池编号拆分 CSV。")
        if day in readings:
            raise ValueError(f"{day} 存在重复晨检日期，请保留当天唯一的有效读数。")
        reservoir_id = pool
        readings[day] = float(level)
    if not readings:
        raise ValueError("缺少晨检读数，请先导入单池日液位台账。")
    return reservoir_id, sorted(readings.items())


def _options(params):
    fields = {field["key"]: field for field in SPEC["params"]}
    if set(params) - fields.keys():
        raise ValueError("存在未知预测参数，请重新打开预测设置。")
    values = {}
    for key, field in fields.items():
        value = params.get(key, field["default"])
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not field["min"] <= value <= field["max"]
                or (field["type"] == "int" and not isinstance(value, int))):
            raise ValueError(f'{field["label"]}必须为 {field["min"]}–{field["max"]} 范围内的有效值。')
        values[key] = value
    return values


def _recent_segment(readings):
    start = 0
    gaps = []
    for index, ((previous, _), (current, _)) in enumerate(zip(readings, readings[1:]), start=1):
        missing = (current - previous).days - 1
        if missing:
            gaps.append({"start": (previous + _DAY).isoformat(), "end": (current - _DAY).isoformat(), "days": missing})
            start = index
    return readings[start:], gaps


class _Models:
    def __init__(self, first, second, alpha, beta):
        self.alpha = alpha
        self.beta = beta
        self.holt_level = second
        self.trend = second - first
        self.level_only = first + alpha * (second - first)

    def predict(self, step):
        return {"holt": max(0.0, self.holt_level + step * self.trend), "level_only": self.level_only}

    def update(self, actual):
        previous_level = self.holt_level
        projected_level = previous_level + self.trend
        self.holt_level = projected_level + self.alpha * (actual - projected_level)
        self.trend += self.beta * (self.holt_level - previous_level - self.trend)
        self.level_only += self.alpha * (actual - self.level_only)


def _evaluation_row(models, reading, step):
    day, actual = reading
    return {"date": day.isoformat(), "step": step, "actual": actual, **models.predict(step)}


def _errors(rows, model):
    errors = [abs(row[model] - row["actual"]) for row in rows]
    return fmean(errors), sqrt(fmean(error * error for error in errors))


def _table(title, columns, rows):
    return {"title": title, "columns": [{"key": key, "label": label} for key, label in columns], "rows": rows}


def _chart(title, points):
    return {"title": title, "kind": "line", "series": [{"label": day, "value": value} for day, value in points]}


def analyze(rows, normalized_params):
    reservoir_id, readings = validate_rows(rows)
    options = _options(normalized_params)
    recent, gaps = _recent_segment(readings)
    holdout_days = options["holdout_days"]
    horizon = options["forecast_days"]
    threshold = options["alert_level_m"]
    required = _MIN_TRAINING + _MIN_ROLLING + holdout_days
    if len(recent) < required:
        start = recent[0][0]
        raise ValueError(f"从 {start} 起仅有 {len(recent)} 条连续日读数，至少需要 {required} 条才能完成滚动验证和留出评估。请补录缺测或缩短留出期。")
    last_day, last_level = recent[-1]
    if (date.max - last_day).days < horizon:
        raise ValueError("预报日期超出日历范围，请缩短未来预报天数。")

    selection_rows = recent[:-holdout_days]
    holdout_rows = recent[-holdout_days:]
    models = _Models(recent[0][1], recent[1][1], options["alpha"], options["beta"])
    for _, actual in selection_rows[2:_MIN_TRAINING]:
        models.update(actual)
    rolling = []
    for reading in selection_rows[_MIN_TRAINING:]:
        rolling.append(_evaluation_row(models, reading, 1))
        models.update(reading[1])
    rolling_scores = {model: _errors(rolling, model) for model in _MODELS}
    winner = min(_SELECTION_ORDER, key=lambda model: rolling_scores[model][0])
    holdout = [_evaluation_row(models, reading, step) for step, reading in enumerate(holdout_rows, start=1)]
    holdout_scores = {model: _errors(holdout, model) for model in _MODELS}
    for _, actual in holdout_rows:
        models.update(actual)

    forecasts = []
    for step in range(1, horizon + 1):
        day = (last_day + step * _DAY).isoformat()
        estimates = models.predict(step)
        selected = estimates[winner]
        reaches_alert = selected >= threshold or isclose(selected, threshold, rel_tol=0, abs_tol=_ALERT_TOLERANCE_M)
        forecasts.append({"date": day, "step": step, **estimates, "selected": selected, "reaches_alert": reaches_alert})
    if last_level >= threshold:
        trigger = last_day.isoformat()
        headline = f"末日实测已达预警线 · {trigger}"
    else:
        trigger = next((row["date"] for row in forecasts if row["reaches_alert"]), None)
        headline = f"预计 {trigger} 首次达到高位预警线" if trigger else f"未来 {horizon} 天预计未触线"

    comparison = [
        {"model": label, "rolling_mae": rolling_scores[model][0], "rolling_rmse": rolling_scores[model][1],
         "holdout_mae": holdout_scores[model][0], "holdout_rmse": holdout_scores[model][1], "selected": model == winner}
        for model, label in _MODELS.items()
    ]
    tables = [
        _table("逐日预报", (("date", "预报日期"), ("step", "提前天数")) + _PREDICTIONS
               + (("selected", "采用预报（m）"), ("reaches_alert", "达到预警线")), forecasts),
        _table("模型误差对照", (("model", "模型"), ("rolling_mae", "滚动 MAE（m）"),
               ("rolling_rmse", "滚动 RMSE（m）"), ("holdout_mae", "留出 MAE（m）"),
               ("holdout_rmse", "留出 RMSE（m）"), ("selected", "采用")), comparison),
        _table("滚动单步验证", _EVALUATION, rolling),
        _table("固定起点留出评估", _EVALUATION, holdout),
        _table("日读数缺测区间", (("start", "缺测首日"), ("end", "缺测末日"), ("days", "缺测天数")), gaps),
    ]
    return {
        "state": "warning" if trigger or gaps else "ok",
        "headline": headline,
        "note": f"{reservoir_id} 使用 {recent[0][0]} 至 {last_day} 的 {len(recent)} 条连续晨检读数，预报采用{_MODELS[winner]}模型。",
        "metrics": [
            {"label": "末日实测（m）", "value": last_level, "detail": last_day.isoformat()},
            {"label": "预警线（m）", "value": threshold, "detail": "达到预警线即计入"},
            {"label": "首次触线日期", "value": trigger or "窗口内未触线", "detail": "末日实测" if last_level >= threshold else "采用模型预报"},
            {"label": "滚动验证天数", "value": len(rolling), "detail": f'{rolling[0]["date"]} 至 {rolling[-1]["date"]}'},
            {"label": "留出评估天数", "value": holdout_days, "detail": f'{holdout[0]["date"]} 至 {holdout[-1]["date"]}'},
            {"label": "缺测天数", "value": sum(gap["days"] for gap in gaps), "detail": f"共 {len(gaps)} 个缺测区间"},
        ],
        "tables": tables,
        "charts": [
            _chart("连续段实测液位（m，最近 30 天）", ((day.isoformat(), level) for day, level in recent[-30:])),
            _chart(f"{_MODELS[winner]}逐日预报（m）", ((row["date"], row["selected"]) for row in forecasts)),
        ],
        "sections": [
            {"title": "评估时间边界", "text": f"模型仅按留出前的滚动 MAE 选择。留出预测统一以 {selection_rows[-1][0]} 为起点。留出评估结束后，将这些实测纳入未来预报。"},
            {"title": "缺测处理", "text": "缺测中断日序列。两模型均使用最后一段连续日读数，缺测区间见下表。"},
        ],
    }
