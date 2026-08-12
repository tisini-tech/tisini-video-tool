"""
YFetch Unified Downloader
Supports both YouTube and Google Drive sources with quality-preserving trim
"""
import re
from dataclasses import dataclass
from typing import Optional, Literal
from pathlib import Path

# Import our modules
from ..gdrive.parser import is_drive_url, parse_drive_url, GDriveError
from ..gdrive.trimmer import GDriveTrimmer, TrimResult


@dataclass
class DownloadRequest:
    """User download request"""
    url: str
    start_time: Optional[float] = None  # None = download full
    end_time: Optional[float] = None
    output_name: Optional[str] = None
    quality: str = "best"  # best, 1080p, 720p, etc.
    mode: Literal['accurate', 'fast', 'auto'] = 'auto'


@dataclass
class SourceInfo:
    """Detected source information"""
    source_type: Literal['youtube', 'gdrive', 'unknown']
    is_valid: bool
    file_id: Optional[str] = None
    video_id: Optional[str] = None
    title_hint: Optional[str] = None


class YFetchUnified:
    """
    Unified downloader for YouTube and Google Drive.

    Usage:
        fetch = YFetchUnified()

        # Download from Google Drive
        result = fetch.download("https://drive.google.com/file/d/ABC123/view", 
                                start=30, end=60)

        # Download from YouTube
        result = fetch.download("https://youtube.com/watch?v=XYZ789",
                                start=120, end=150, quality="1080p")
    """

    YT_PATTERNS = [
        r'(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)([a-zA-Z0-9_-]+)',
        r'youtube\.com/shorts/([a-zA-Z0-9_-]+)',
    ]

    def __init__(self, output_dir: str = "./downloads"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.gdrive_trimmer = GDriveTrimmer(output_dir=output_dir)

    def detect_source(self, url: str) -> SourceInfo:
        """Detect if URL is YouTube, Google Drive, or unknown"""
        # Check Google Drive
        if is_drive_url(url):
            parser = parse_drive_url(url)
            return SourceInfo(
                source_type='gdrive',
                is_valid=True,
                file_id=parser.file_id,
                title_hint=parser.title
            )

        # Check YouTube
        for pattern in self.YT_PATTERNS:
            match = re.search(pattern, url)
            if match:
                return SourceInfo(
                    source_type='youtube',
                    is_valid=True,
                    video_id=match.group(1)
                )

        return SourceInfo(source_type='unknown', is_valid=False)

    def download(self, 
                 url: str,
                 start_time: Optional[float] = None,
                 end_time: Optional[float] = None,
                 output_name: Optional[str] = None,
                 quality: str = "best",
                 mode: Literal['accurate', 'fast', 'auto'] = 'auto',
                 crf: int = 18) -> TrimResult:
        """
        Download/trim video from URL (YouTube or Google Drive).

        Args:
            url: Video URL (YouTube or Google Drive)
            start_time: Start trim time in seconds (None = from beginning)
            end_time: End trim time in seconds (None = to end)
            output_name: Custom output filename
            quality: Target quality (best, 1080p, 720p, 480p)
            mode: Trim mode - 'accurate' (re-encode), 'fast' (stream copy), 'auto'
            crf: Quality setting for re-encode (18 = visually lossless)

        Returns:
            TrimResult with success status and file info
        """
        source = self.detect_source(url)

        if not source.is_valid:
            return TrimResult(
                success=False,
                error_message=f"Unsupported URL format. Supported: YouTube, Google Drive"
            )

        # Handle Google Drive
        if source.source_type == 'gdrive':
            if start_time is not None and end_time is not None:
                return self.gdrive_trimmer.trim(
                    drive_url=url,
                    start_time=start_time,
                    end_time=end_time,
                    output_filename=output_name,
                    mode=mode,
                    crf=crf
                )
            else:
                return self.gdrive_trimmer.download_full(
                    drive_url=url,
                    output_filename=output_name,
                    use_stream_copy=(mode != 'accurate')
                )

        # Handle YouTube (placeholder - integrate with your existing yt-dlp code)
        elif source.source_type == 'youtube':
            return self._download_youtube(
                url=url,
                start_time=start_time,
                end_time=end_time,
                output_name=output_name,
                quality=quality,
                mode=mode,
                crf=crf
            )

        return TrimResult(success=False, error_message="Unknown error")

    def _download_youtube(self, **kwargs) -> TrimResult:
        """Placeholder for YouTube download - integrate with your existing code"""
        # This should call your existing YFetch Basic or YFetch Pro download logic
        return TrimResult(
            success=False,
            error_message="YouTube download not implemented in unified module. Use your existing YFetch app."
        )


# Quick test function
def test_url(url: str) -> dict:
    """Test a URL and return what we detected"""
    fetch = YFetchUnified()
    source = fetch.detect_source(url)
    return {
        'type': source.source_type,
        'valid': source.is_valid,
        'id': source.file_id or source.video_id,
        'title_hint': source.title_hint
    }


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Inspect a YouTube or Google Drive URL.")
    parser.add_argument("url")
    args = parser.parse_args(argv)

    result = test_url(args.url)
    print(json.dumps(result, indent=2))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
