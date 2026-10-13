"""Request shapes."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ..versions.common_cli import clean_url, is_valid_youtube_url


class BasicJob(BaseModel):
    tool: Literal["basic"]
    url: str
    format: Literal["MP4", "MP3"] = "MP4"
    quality: str = Field("Best", pattern=r"^[A-Za-z0-9 ]{1,12}$")

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        value = clean_url(value)

        if not is_valid_youtube_url(value):
            raise ValueError("This is not a YouTube link.")

        return value

    def params(self) -> dict:
        return {
            "url": self.url,
            "format": self.format,
            "quality": self.quality,
        }
