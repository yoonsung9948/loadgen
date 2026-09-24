from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, PositiveInt, HttpUrl

from typing import Annotated, Literal, Any

class TargetConfig(BaseModel):
    url: HttpUrl
    method: str = "GET"

class RequestConfig(BaseModel):
    headers: dict[str, str] = Field(default_factory=dict)
    json_body: dict[str, Any] | None = Field(default=None, alias="json")

class BaseLoadConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    timeout: float = Field(gt=0)
    duration_seconds: float = Field(gt=0)
    max_in_flight: PositiveInt


class ConstantConfig(BaseLoadConfig):
    pattern: Literal["constant"]
    requests_per_second: float = Field(gt=0)


class RampConfig(BaseLoadConfig):
    pattern: Literal["ramp"]
    requests_per_second: float = Field(ge=0)
    slope: float


class BurstConfig(BaseLoadConfig):
    pattern: Literal["burst"]
    requests_per_second: float = Field(ge=0)  # baseline
    peak_rps: float = Field(gt=0)
    center_seconds: float = Field(ge=0)
    width_seconds: float = Field(gt=0)

class GammaConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    shape: float = Field(gt=0)
    seed: int | None = Field(default=None, ge=0)

LoadConfig = Annotated[
    ConstantConfig | RampConfig | BurstConfig ,
    Field(discriminator="pattern"),
]

class Config(BaseModel):
    target: TargetConfig
    load: LoadConfig
    request: RequestConfig
    arrival_distribution: GammaConfig | None = None


def load_yaml(path: str | Path) -> Config:
    with Path(path).open() as f:
        raw = yaml.safe_load(f)

    return Config.model_validate(raw)