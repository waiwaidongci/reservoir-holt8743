"""澄湾会展园 RW-02 的固定晨检台账。"""


_LEVELS = (0.83, 0.87, 0.89, None, 0.94, 1.02, 1.06, 1.13, 1.23, 1.25,
           1.35, 1.37, 1.44, 1.51, 1.59, 1.65, 1.72, 1.76, 1.83, 1.91)


def demo_rows() -> list[dict]:
    return [
        {"reservoir_id": "RW-02", "reading_date": f"2026-11-{day:02d}", "level_m": level}
        for day, level in enumerate(_LEVELS, start=1)
        if level is not None
    ]
