"""
GDrive Video Trimmer for YFetch
Downloads and trims Google Drive videos with quality preservation
"""
import subprocess
import os
import tempfile
import json
from pathlib import Path
from typing import Optional, Tuple, Literal
from dataclasses import dataclass

from .parser import GDriveURLParser, GDriveError, get_drive_stream_url


@dataclass
class TrimResult:
    """Result of a trim operation"""
    success: bool
    output_path: Optional[str] = None
    error_message: Optional[str] = None
    original_duration: Optional[float] = None
    trimmed_duration: Optional[float] = None
    original_resolution: Optional[str] = None
    output_size_mb: Optional[float] = None
    used_stream_copy: bool = False


class GDriveTrimmer:
    """
    Trim Google Drive videos with frame-accurate cuts.

    Strategy:
    1. For frame-accurate cuts: Re-encode at original quality (lossless approach)
    2. For keyframe-aligned cuts: Use stream copy (no quality loss, instant)
    """

    def __init__(self, output_dir: str = "./downloads", 
                 temp_dir: Optional[str] = None,
                 ffmpeg_path: str = "ffmpeg",
                 ffprobe_path: str = "ffprobe"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir = Path(temp_dir) if temp_dir else Path(tempfile.gettempdir())
        self.ffmpeg = ffmpeg_path
        self.ffprobe = ffprobe_path

    def _probe_video(self, url: str) -> dict:
        """Probe video metadata using ffprobe"""
        cmd = [
            self.ffprobe, '-v', 'error',
            '-show_entries', 'format=duration,bit_rate,size:stream=codec_name,width,height,pix_fmt,r_frame_rate,bit_rate',
            '-of', 'json',
            url
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise GDriveError(f"ffprobe failed: {result.stderr}")
        return json.loads(result.stdout)

    def _get_keyframe_times(self, url: str) -> list:
        """Get list of keyframe timestamps for smart cutting"""
        cmd = [
            self.ffprobe, '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'frame=pkt_pts_time,pict_type',
            '-of', 'json',
            url
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            return []

        data = json.loads(result.stdout)
        keyframes = []
        for frame in data.get('frames', []):
            if frame.get('pict_type') == 'I':
                try:
                    keyframes.append(float(frame.get('pkt_pts_time', 0)))
                except:
                    pass
        return keyframes

    def _find_nearest_keyframe(self, target_time: float, keyframes: list, direction: str = 'before') -> float:
        """Find nearest keyframe to target time"""
        if not keyframes:
            return target_time

        if direction == 'before':
            # Find last keyframe before target
            valid = [k for k in keyframes if k <= target_time]
            return max(valid) if valid else keyframes[0]
        else:
            # Find first keyframe after target
            valid = [k for k in keyframes if k >= target_time]
            return min(valid) if valid else keyframes[-1]

    def trim(self, 
             drive_url: str,
             start_time: float,  # seconds
             end_time: float,    # seconds
             output_filename: Optional[str] = None,
             mode: Literal['accurate', 'fast', 'auto'] = 'auto',
             video_codec: Optional[str] = None,  # None = auto-detect/copy
             crf: int = 18,  # For re-encode mode (18 = visually lossless)
             preset: str = 'slow',  # Encoding speed/quality tradeoff
             include_audio: bool = True) -> TrimResult:
        """
        Trim a Google Drive video.

        Args:
            drive_url: Google Drive share URL
            start_time: Start time in seconds (frame-accurate)
            end_time: End time in seconds
            output_filename: Output filename (auto-generated if None)
            mode: 'accurate' = re-encode for frame accuracy
                  'fast' = stream copy (keyframe-aligned only)
                  'auto' = use stream copy if cuts align with keyframes
            video_codec: Override output codec (None = match source or libx264)
            crf: Quality for re-encode (lower = better, 18-23 recommended)
            preset: Encoding preset (slow/veryslow = better quality)

        Returns:
            TrimResult with operation details
        """
        result = TrimResult(success=False)

        try:
            # Step 1: Get direct stream URL
            stream_url = get_drive_stream_url(drive_url)

            # Step 2: Probe source video
            probe = self._probe_video(stream_url)
            fmt = probe.get('format', {})
            streams = probe.get('streams', [])

            result.original_duration = float(fmt.get('duration', 0))

            video_stream = next((s for s in streams if s.get('codec_type') == 'video'), None)
            if video_stream:
                w = video_stream.get('width', '?')
                h = video_stream.get('height', '?')
                result.original_resolution = f"{w}x{h}"

            # Validate time range
            if start_time < 0:
                start_time = 0
            if end_time > result.original_duration:
                end_time = result.original_duration
            if end_time <= start_time:
                raise GDriveError(f"Invalid time range: {start_time} to {end_time}")

            result.trimmed_duration = end_time - start_time

            # Step 3: Determine cutting strategy
            use_stream_copy = False
            if mode in ('fast', 'auto'):
                keyframes = self._get_keyframe_times(stream_url)
                if keyframes:
                    nearest_start = self._find_nearest_keyframe(start_time, keyframes, 'before')
                    nearest_end = self._find_nearest_keyframe(end_time, keyframes, 'after')

                    # Allow 0.5s tolerance for auto mode
                    tolerance = 0.5 if mode == 'auto' else 0.05
                    if abs(nearest_start - start_time) <= tolerance and abs(nearest_end - end_time) <= tolerance:
                        use_stream_copy = True
                        start_time = nearest_start
                        end_time = nearest_end
                        result.trimmed_duration = end_time - start_time

            # Step 4: Build output filename
            if not output_filename:
                safe_title = f"gdrive_trim_{start_time:.1f}_{end_time:.1f}"
                ext = '.mp4'  # Default container
                output_filename = f"{safe_title}{ext}"

            output_path = self.output_dir / output_filename

            # Step 5: Build ffmpeg command
            cmd = [self.ffmpeg, '-y']

            # Input options for seeking
            if use_stream_copy:
                # Stream copy mode - fast, no quality loss
                cmd.extend(['-ss', str(start_time), '-i', stream_url, 
                           '-t', str(end_time - start_time),
                           '-c', 'copy'])
                if not include_audio:
                    cmd.extend(['-an'])
                result.used_stream_copy = True
            else:
                # Re-encode mode - frame accurate
                # Use input seeking + output seeking for accuracy
                cmd.extend(['-ss', str(start_time), '-i', stream_url,
                           '-t', str(end_time - start_time)])

                # Video encoding
                if video_codec:
                    cmd.extend(['-c:v', video_codec])
                else:
                    # Auto: try to match source codec, fallback to libx264
                    src_codec = video_stream.get('codec_name', 'h264') if video_stream else 'h264'
                    if src_codec in ('h264', 'avc1'):
                        cmd.extend(['-c:v', 'libx264', '-profile:v', 'high', '-level', '4.2'])
                    elif src_codec == 'hevc':
                        cmd.extend(['-c:v', 'libx265'])
                    elif src_codec == 'av1':
                        cmd.extend(['-c:v', 'libsvtav1'])
                    else:
                        cmd.extend(['-c:v', 'libx264'])

                cmd.extend([
                    '-crf', str(crf),
                    '-preset', preset,
                    '-pix_fmt', 'yuv420p',
                    '-movflags', '+faststart'
                ])

                # Audio
                if include_audio:
                    cmd.extend(['-c:a', 'aac', '-b:a', '192k'])
                else:
                    cmd.extend(['-an'])

                result.used_stream_copy = False

            cmd.append(str(output_path))

            # Step 6: Execute
            process = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=3600  # 1 hour max for large files
            )

            if process.returncode != 0:
                raise GDriveError(f"ffmpeg failed: {process.stderr[-500:]}")

            # Step 7: Verify output
            if output_path.exists():
                result.success = True
                result.output_path = str(output_path)
                result.output_size_mb = output_path.stat().st_size / (1024 * 1024)
            else:
                raise GDriveError("Output file was not created")

        except Exception as e:
            result.error_message = str(e)

        return result

    def download_full(self, 
                      drive_url: str,
                      output_filename: Optional[str] = None,
                      use_stream_copy: bool = True) -> TrimResult:
        """
        Download a full Google Drive video without trimming.
        Uses stream copy by default for maximum quality preservation.
        """
        return self.trim(
            drive_url=drive_url,
            start_time=0,
            end_time=999999,  # Will be clamped to actual duration
            output_filename=output_filename,
            mode='fast' if use_stream_copy else 'accurate'
        )


# Convenience functions
def trim_drive_video(drive_url: str, start: float, end: float, **kwargs) -> TrimResult:
    """Quick trim a Drive video"""
    trimmer = GDriveTrimmer()
    return trimmer.trim(drive_url, start, end, **kwargs)


def download_drive_video(drive_url: str, **kwargs) -> TrimResult:
    """Quick download a full Drive video"""
    trimmer = GDriveTrimmer()
    return trimmer.download_full(drive_url, **kwargs)
