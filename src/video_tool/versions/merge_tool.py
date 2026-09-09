"""Command-line video merge tool.
merge-strategy: stream-copy first when no CRF is requested, then re-encode.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile


def merge_videos(output_path: str, video_paths: list[str], ffmpeg_path: str, crf: int | None = None):
    """Merge videos using the original copy-then-reencode strategy."""
    if len(video_paths) < 2:
        raise ValueError("Need at least 2 videos")

    list_lines = []
    for path in video_paths:
        abs_path = os.path.abspath(path).replace("\\", "/").replace("'", "'\\''")
        list_lines.append(f"file '{abs_path}'")

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as handle:
        handle.write("\n".join(list_lines))
        list_file = handle.name

    try:
        if crf is None:
            cmd = [
                ffmpeg_path, "-y", "-f", "concat", "-safe", "0",
                "-i", list_file, "-c", "copy",
                "-movflags", "+faststart", output_path,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                return True, "copy"

        use_crf = crf if crf is not None else 23
        cmd = [
            ffmpeg_path, "-y", "-f", "concat", "-safe", "0",
            "-i", list_file,
            "-c:v", "libx264", "-preset", "fast", "-crf", str(use_crf),
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", "-pix_fmt", "yuv420p",
            output_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(result.stderr[:1000])

        return True, f"crf{use_crf}"
    finally:
        try:
            os.unlink(list_file)
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Merge video clips with FFmpeg.")
    parser.add_argument("-o", "--output", required=True)
    parser.add_argument("--crf", type=int, default=None)
    parser.add_argument("videos", nargs="+")
    args = parser.parse_args(argv)

    from ..core import find_ffmpeg

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        parser.error("FFmpeg was not found.")

    ok, mode = merge_videos(args.output, args.videos, ffmpeg, args.crf)
    print(f"Merged {len(args.videos)} videos using {mode}: {args.output}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
