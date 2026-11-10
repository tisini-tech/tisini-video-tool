"""Request shapes."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .linkcheck import canonical_url
from .uploads import UPLOAD_ID

# A merge fetches every source before it joins them, so the count is capped.
MAX_MERGE_CLIPS = 6

Quality = Literal["Best", "1080p", "720p", "480p", "360p"]


class BasicJob(BaseModel):
    # A request with a field not listed here is refused.
    model_config = ConfigDict(extra="forbid")

    tool: Literal["basic"]
    url: str = Field(max_length=2048)
    format: Literal["MP4", "MP3"] = "MP4"
    quality: Quality = "Best"

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


class UrlSource(BaseModel):
    """A YouTube link to download and merge."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(max_length=2048)

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        return canonical_url(value)


class UploadSource(BaseModel):
    """A file the user handed over earlier through POST /v1/uploads."""

    model_config = ConfigDict(extra="forbid")

    upload: str = Field(pattern=UPLOAD_ID)


Source = UrlSource | UploadSource


class MergeJob(BaseModel):
    """Two or more videos in, one joined MP4 out. The order is kept.

    Send ``sources`` to mix YouTube links and uploaded files. Send
    ``urls`` when every video is a YouTube link. Not both.
    """

    model_config = ConfigDict(extra="forbid")

    tool: Literal["merge"]
    urls: (
        list[Annotated[str, Field(max_length=2048)]] | None
    ) = Field(default=None, min_length=2, max_length=MAX_MERGE_CLIPS)
    sources: list[Source] | None = Field(
        default=None, min_length=2, max_length=MAX_MERGE_CLIPS
    )
    quality: Quality = "Best"

    @field_validator("urls")
    @classmethod
    def _check_urls(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None

        return [canonical_url(value) for value in values]

    @model_validator(mode="after")
    def _one_list(self) -> "MergeJob":
        if (self.urls is None) == (self.sources is None):
            raise ValueError("Send either urls or sources, not both.")

        return self

    def params(self) -> dict:
        if self.urls is not None:
            sources = [{"url": url} for url in self.urls]
        else:
            sources = [
                {"url": s.url} if isinstance(s, UrlSource)
                else {"upload": s.upload}
                for s in self.sources
            ]

        return {
            "sources": sources,
            "quality": self.quality,
        }


# The "tool" field says which shape a request has.
JobRequest = Annotated[BasicJob | MergeJob, Field(discriminator="tool")]
