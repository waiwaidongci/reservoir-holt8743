from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field
from src.constants.enums import Review

class Request(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')

class Credentials(Request):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)

class Upload(Request):
    name: str = Field(min_length=1, max_length=120)
    filename: str = Field(min_length=1, max_length=160)
    csv_text: str = Field(min_length=1, max_length=2_000_000)

class RunRequest(Request):
    dataset_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=120)
    params: dict = Field(default_factory=dict)

class ReviewRequest(Request):
    review: Review
    note: str = Field(min_length=1, max_length=500)
    decision: str = Field(default='', max_length=120)

class ScheduleRequest(RunRequest):
    recipient: str = Field(min_length=1, max_length=100)
    first_at: datetime
    repeat_daily: bool = False

class PumpDayRequest(Request):
    date: str = Field(min_length=8, max_length=10)
    forecast_m: float = Field(ge=0, le=20)
    pump_cap_m: float = Field(ge=0, le=20)
    price: float = Field(ge=0, le=100)
    floor_m: float = Field(ge=0, le=20)
    warning_m: float = Field(ge=0, le=20)

class PumpPlanRequest(Request):
    name: str = Field(min_length=1, max_length=120)
    source_run_id: int | None = Field(default=None, gt=0)
    energy_kwh_per_m: float = Field(default=1.0, ge=0, le=100)
    days: list[PumpDayRequest] = Field(min_length=1, max_length=30)
