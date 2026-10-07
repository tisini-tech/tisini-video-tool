"""
Google Drive video adapter for Video Tool.

This module is intentionally thin.

Responsibilities:
    - Resolve a Google Drive URL into a direct stream URL.
    - Delegate video processing to the shared core utilities.
    - Preserve the existing public GDrive trimming/download API.

FFmpeg processing, encoder selection, validation, and fallback behavior
belong to video_tool.core.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..core import (
    VideoToolError,
    check_clip_length,
    download_remote,
    find_ffmpeg,
    trim_remote,
)
from ..errors import plain_message
from .parser import GDriveError, get_drive_stream_url


@dataclass
class TrimResult:
    """Result of a Google Drive trim/download operation."""

    success: bool
    output_path: str | None = None
    error_message: str | None = None
    original_duration: float | None = None
    trimmed_duration: float | None = None
    original_resolution: str | None = None
    output_size_mb: float | None = None
    used_stream_copy: bool = False


class GDriveTrimmer:
    """Thin Google Drive adapter around the shared FFmpeg core."""

    def __init__(
        self,
        output_dir: str = "./downloads",
        temp_dir: str | None = None,
        ffmpeg_path: str | None = None,
        ffprobe_path: str | None = None,
    ):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.temp_dir = (
            Path(temp_dir)
            if temp_dir
            else Path.cwd()
        )

        self.ffmpeg = ffmpeg_path or find_ffmpeg()
        self.ffprobe = ffprobe_path

        if not self.ffmpeg:
            raise VideoToolError(
                "FFmpeg was not found. Install FFmpeg and make sure it "
                "is available on your PATH."
            )

    def _stream_url(self, drive_url: str) -> str:
        """Resolve a Google Drive URL to a direct media stream URL."""

        try:
            return get_drive_stream_url(drive_url)
        except GDriveError:
            raise
        except Exception as exc:
            raise GDriveError(
                "Video Tool could not open this Google Drive video. "
                "Check that the link works and the file is shared.",
                detail=str(exc),
            ) from exc

    def trim(
        self,
        drive_url: str,
        start_time: float,
        end_time: float,
        output_filename: str | None = None,
        mode: Literal["accurate", "fast", "auto"] = "auto",
        video_codec: str | None = None,
        crf: int = 18,
        preset: str = "slow",
        include_audio: bool = True,
    ) -> TrimResult:
        """
        Trim a Google Drive video directly from its remote stream.

        The actual FFmpeg processing is delegated to core.trim_remote().

        mode:
            accurate -> force re-encoding
            fast     -> allow stream-copy first
            auto     -> allow stream-copy first

        If stream-copy fails validation, core.trim_remote() handles the
        re-encode fallback automatically.
        """

        result = TrimResult(success=False)

        try:
            if start_time < 0:
                start_time = 0.0

            if end_time <= start_time:
                raise GDriveError(
                    f"Invalid time range: {start_time} to {end_time}"
                )

            stream_url = self._stream_url(drive_url)

            if not output_filename:
                output_filename = (
                    f"gdrive_trim_{start_time:.1f}_{end_time:.1f}.mp4"
                )

            output_path = self.output_dir / output_filename

            force_reencode = mode == "accurate"

            trim_remote(
                self.ffmpeg,
                stream_url,
                str(output_path),
                start_time,
                end_time,
                resolution=None,
                bitrate_kbps=8000,
                fps=None,
                crf=crf,
                preset=preset,
                encoder=video_codec,
                caption_filter=None,
                include_audio=include_audio,
                re_encode=force_reencode,
            )

            if not output_path.exists():
                raise GDriveError(
                    "FFmpeg completed but the output file was not created."
                )

            wanted = end_time - start_time

            # A video output must always contain video.
            # Audio is required only when include_audio=True.
            kinds = (
                ("video", "audio")
                if include_audio
                else ("video",)
            )

            problems = check_clip_length(
                str(output_path),
                wanted,
                kinds=kinds,
            )

            if problems:
                try:
                    output_path.unlink()
                except OSError:
                    pass

                # Normally core.trim_remote() already performs the
                # stream-copy -> re-encode fallback. This is an additional
                # safety net for an output that still fails duration
                # validation at this adapter level.
                if not force_reencode:
                    trim_remote(
                        self.ffmpeg,
                        stream_url,
                        str(output_path),
                        start_time,
                        end_time,
                        resolution=None,
                        bitrate_kbps=8000,
                        fps=None,
                        crf=crf,
                        preset=preset,
                        encoder=video_codec,
                        caption_filter=None,
                        include_audio=include_audio,
                        re_encode=True,
                    )

                    problems = check_clip_length(
                        str(output_path),
                        wanted,
                        kinds=kinds,
                    )

                if problems:
                    try:
                        output_path.unlink()
                    except OSError:
                        pass

                    raise GDriveError(
                        "The trimmed Google Drive clip is shorter than "
                        f"requested ({wanted:.2f}s). "
                        + "; ".join(problems)
                    )

            result.success = True
            result.output_path = str(output_path)
            result.trimmed_duration = wanted
            result.output_size_mb = (
                output_path.stat().st_size / (1024 * 1024)
            )

            # This reflects the requested processing mode.
            # core.trim_remote() may transparently fall back to re-encoding
            # if stream-copy is unsuitable.
            result.used_stream_copy = not force_reencode

        except Exception as exc:
            result.error_message = plain_message(exc)

        return result

    def download_full(
        self,
        drive_url: str,
        output_filename: str | None = None,
        use_stream_copy: bool = True,
    ) -> TrimResult:
        """
        Download a complete Google Drive video through its direct stream.

        core.download_remote() owns the stream-copy/re-encode fallback.
        """

        result = TrimResult(success=False)

        try:
            stream_url = self._stream_url(drive_url)

            if not output_filename:
                output_filename = "gdrive_video.mp4"

            output_path = self.output_dir / output_filename

            download_remote(
                self.ffmpeg,
                stream_url,
                str(output_path),
                re_encode=not use_stream_copy,
                crf=18,
                preset="slow",
                encoder=None,
                include_audio=True,
            )

            if not output_path.exists():
                raise GDriveError(
                    "FFmpeg completed but the output file was not created."
                )

            result.success = True
            result.output_path = str(output_path)
            result.output_size_mb = (
                output_path.stat().st_size / (1024 * 1024)
            )

            # This represents the requested mode. The shared core may have
            # silently fallen back to re-encoding if stream-copy failed.
            result.used_stream_copy = use_stream_copy

        except Exception as exc:
            result.error_message = plain_message(exc)

        return result


def trim_drive_video(
    drive_url: str,
    start: float,
    end: float,
    **kwargs,
) -> TrimResult:
    """Convenience wrapper for trimming a Google Drive video."""

    trimmer = GDriveTrimmer()

    return trimmer.trim(
        drive_url,
        start,
        end,
        **kwargs,
    )


def download_drive_video(
    drive_url: str,
    **kwargs,
) -> TrimResult:
    """Convenience wrapper for downloading a Google Drive video."""

    trimmer = GDriveTrimmer()

    return trimmer.download_full(
        drive_url,
        **kwargs,
    )