"""Request shapes."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .linkcheck import canonical_url


class BasicJob(BaseModel):
    # A request with a field not listed here is refused.
    model_config = ConfigDict(extra="forbid")

    tool: Literal["basic"]
    url: str = Field(max_length=2048)
    format: Literal["MP4", "MP3"] = "MP4"
    quality: Literal["Best", "1080p", "720p", "480p", "360p"] = "Best"

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        return canonical_url(value)

    def params(self) -> dict:
        return {
            "url": self.url,
            "format": self.format,
            "quality": self.quality,
        }
