from __future__ import annotations

import time

from pydantic import ConfigDict, Field, model_validator

from amrita_core.types.base import BaseModel, DirtyAwareBaseModel
from amrita_core.types.billing import BillingRecord
from amrita_core.types.message import CONTENT_LIST_TYPE_ITEM
from amrita_core.types.response import UniResponseUsage

SUMMARY_RETENTION = 7 * 24 * 60 * 60


class SummaryPart(BaseModel):
    source_at: float
    text: str


class MemoryModel(DirtyAwareBaseModel):
    dirty_exclude__: tuple[str, ...] = ("model_config",)
    model_config = ConfigDict(extra="allow")
    messages: list[CONTENT_LIST_TYPE_ITEM] = Field(default_factory=list)
    time: float = Field(default_factory=time.time, description="Timestamp")
    abstract: str = Field(default="", description="Summary")
    summary_parts: list[SummaryPart] = Field(default_factory=list)
    usage: UniResponseUsage | None = Field(
        default=None,
        description="Usage of the most recent provider request over this context",
    )
    billing: list[BillingRecord] = Field(
        default_factory=list,
        description="Per-request billing records accumulated over this context",
    )

    @model_validator(mode="before")
    @classmethod
    def date_legacy_history(cls, value):
        if not isinstance(value, dict) or "summary_parts" in value:
            return value
        value = dict(value)

        source_at = value.get("time") or 0
        abstract = value.get("abstract")
        value["summary_parts"] = (
            [{"source_at": source_at, "text": abstract}] if abstract else []
        )
        if source_at and isinstance(value.get("messages"), list):
            value["messages"] = [
                {**message, "summary_source_at": source_at}
                if isinstance(message, dict) and message.get("role") == "user"
                and "summary_source_at" not in message
                else message
                for message in value["messages"]
            ]
        return value

    def live_summary_parts(self, now: float | None = None) -> list[SummaryPart]:
        cutoff = (time.time() if now is None else now) - SUMMARY_RETENTION
        return [part for part in self.summary_parts if part.source_at >= cutoff]

    def visible_summary(self, now: float | None = None) -> str:
        return "\n\n".join(part.text for part in self.live_summary_parts(now))

    def replace_summary(self, parts: list[SummaryPart]) -> None:
        self.summary_parts = parts
        self.abstract = "\n\n".join(part.text for part in parts)

    def expire_summary(self) -> None:
        live = self.live_summary_parts()
        if len(live) != len(self.summary_parts) or (self.abstract and not live):
            self.replace_summary(live)
