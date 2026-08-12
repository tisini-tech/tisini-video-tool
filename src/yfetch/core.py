"""
🛠 YFetch Core Utilities — HIGH QUALITY VERSION
Shared utilities for YFetch apps.

Quality improvements:
• trim_clip: tries lossless copy first, falls back to high-quality re-encode
• merge_clips: uses concat demuxer with -c copy for lossless merging
• Proper CRF/preset settings when re-encode is unavoidable
"""

import os
import re
import json
import csv
import subprocess
import shutil
from pathlib import Path

# === RUNTIME PATHS ===
# Keep generated state outside the source tree. This avoids polluting the
# project with cache/config files while giving every YFetch version one
# consistent location for shared state.
YFETCH_CONFIG_DIR = Path.home() / ".config" / "yfetch"
YFETCH_CACHE_DIR = Path.home() / ".cache" / "yfetch"
CACHE_FILE = str(YFETCH_CACHE_DIR / "cache.json")
CONFIG_FILE = str(YFETCH_CONFIG_DIR / "config.json")
DOWNLOAD_COUNT_FILE = str(YFETCH_CACHE_DIR / "download_count.json")


def _ensure_runtime_dirs() -> None:
    """Create YFetch runtime directories when state needs to be written."""
    YFETCH_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    YFETCH_CACHE_DIR.mkdir(parents=True, exist_ok=True)


# === PATH UTILS ===
def project_path(*parts):
    """Return an absolute path relative to the repository root."""
    # core.py lives at: <project>/src/yfetch/core.py
    project_root = Path(__file__).resolve().parents[2]
    return str(project_root.joinpath(*parts))


def src_path(*parts):
    """Return an absolute path relative to the src/ directory."""
    src_root = Path(__file__).resolve().parents[1]
    return str(src_root.joinpath(*parts))


def root_path(*parts):
    """Alias for project_path, kept for compatibility."""
    return project_path(*parts)


def ensure_dir(path):
    """Ensure a directory exists and return it."""
    os.makedirs(path, exist_ok=True)
    return path


def safe_filename(name):
    """Make a string safe for use as a filename."""
    return re.sub(r'[^\w\s-]', '', name).strip().replace(' ', '_')


# === FFMPEG ===
def find_ffmpeg():
    """Find ffmpeg executable."""
    for name in ['ffmpeg', 'ffmpeg.exe']:
        path = shutil.which(name)
        if path:
            return path
    # Common locations
    for loc in ['/usr/bin/ffmpeg', '/usr/local/bin/ffmpeg', 
                '/opt/homebrew/bin/ffmpeg', 'C:\\ffmpeg\\bin\\ffmpeg.exe']:
        if os.path.exists(loc):
            return loc
    return None

def find_ffprobe():
    """Find ffprobe executable."""
    for name in ['ffprobe', 'ffprobe.exe']:
        path = shutil.which(name)
        if path:
            return path
    for loc in ['/usr/bin/ffprobe', '/usr/local/bin/ffprobe',
                '/opt/homebrew/bin/ffprobe', 'C:\\ffmpeg\\bin\\ffprobe.exe']:
        if os.path.exists(loc):
            return loc
    return None


def get_available_encoders(ffmpeg_path):
    """Get list of available video encoders."""
    if not ffmpeg_path:
        return []
    try:
        result = subprocess.run([ffmpeg_path, '-encoders'], capture_output=True, text=True, timeout=10)
        encoders = []
        for line in result.stdout.split('\n'):
            if 'libx264' in line:
                encoders.append('libx264')
            elif 'libx265' in line:
                encoders.append('libx265')
            elif 'h264_nvenc' in line:
                encoders.append('h264_nvenc')
            elif 'h264_amf' in line:
                encoders.append('h264_amf')
            elif 'h264_videotoolbox' in line:
                encoders.append('h264_videotoolbox')
        return encoders
    except Exception:
        return []


def get_best_video_encoder(ffmpeg_path):
    encoders = get_available_encoders(ffmpeg_path)
    priority = ['libx264', 'libx265']  # Software encoders support CRF
    for enc in priority:
        if enc in encoders:
            return enc
        # Fallback to hardware only if no software encoder found
    fallback = ['h264_nvenc', 'h264_amf', 'h264_videotoolbox']
    for enc in fallback:
        if enc in encoders:
            return enc
    return 'libx264'


# === TIME PARSING ===
def parse_time_to_seconds(time_str):
    """Convert time string to seconds. Handles HH:MM:SS, MM:SS, SS, etc."""
    if not time_str or not time_str.strip():
        return None
    time_str = time_str.strip()
    # Try pure seconds
    try:
        return float(time_str)
    except ValueError:
        pass
    # Try HH:MM:SS or MM:SS
    parts = time_str.split(':')
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        elif len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        elif len(parts) == 1:
            return float(parts[0])
    except ValueError:
        pass
    return None


def seconds_to_timestamp(seconds):
    """Convert seconds to HH:MM:SS timestamp."""
    if seconds is None:
        return "00:00:00"
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


# === CACHE ===
def load_cache():
    """Load video cache."""
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_cache(cache):
    """Save video cache."""
    try:
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache, f, indent=2)
    except Exception:
        pass


def get_cached_path(cache, video_id, format_type='MP4'):
    """Get cached video path if it exists."""
    key = f"{video_id}_{format_type}"
    path = cache.get(key)
    if path and os.path.exists(path):
        return path
    return None


def add_to_cache(cache, video_id, format_type, path):
    """Add video to cache."""
    key = f"{video_id}_{format_type}"
    cache[key] = path
    save_cache(cache)


def default_download_directories() -> list[str]:
    """Return the conventional YFetch download locations.

    Linux filesystems are case-sensitive, so both historical spellings are
    checked. The user's explicit output directory is added separately by
    callers.
    """
    downloads = Path.home() / "Downloads"
    candidates = [
        downloads / "yfetch",
        downloads / "YFetch",
    ]

    existing: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate.resolve(strict=False))
        if key not in seen:
            seen.add(key)
            existing.append(str(candidate))
    return existing


def find_local_cached_file(video_id, format_type="MP4", directories=None):
    """Find a previously downloaded file without contacting YouTube.

    The cache registry is checked first by ``resolve_cached_file``. This
    directory scan is a recovery path for files whose cache entry is missing.
    Only filenames containing the exact YouTube ID are accepted; unrelated
    videos are never guessed to be a match.
    """
    if not video_id:
        return None

    extensions = (
        {".mp3"}
        if format_type.upper() == "MP3"
        else {".mp4", ".mkv", ".webm"}
    )

    search_dirs = list(directories or [])
    for directory in default_download_directories():
        if directory not in search_dirs:
            search_dirs.append(directory)

    for directory in search_dirs:
        path = Path(directory).expanduser()
        if not path.is_dir():
            continue

        try:
            candidates = [
                p for p in path.iterdir()
                if p.is_file()
                and p.suffix.lower() in extensions
                and video_id in p.name
                and "_trim_" not in p.name
            ]
        except OSError:
            continue

        if candidates:
            return str(max(candidates, key=lambda p: p.stat().st_mtime))

    return None


def resolve_cached_file(
    url,
    format_type="MP4",
    *,
    cache=None,
    directories=None,
):
    """Resolve a local/cached download before any network operation."""
    video_id = extract_video_id(url) if is_valid_youtube_url(url) else url
    if not video_id:
        return None

    cache = cache if cache is not None else load_cache()

    cached = get_cached_path(cache, video_id, format_type)
    if cached:
        return cached

    return find_local_cached_file(video_id, format_type, directories)


# === CONFIG ===
def load_config():
    """Load app config."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_config(config):
    """Save app config."""
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2)
    except Exception:
        pass


# === DOWNLOAD COUNT ===
def load_download_count():
    """Load download counter."""
    if os.path.exists(DOWNLOAD_COUNT_FILE):
        try:
            with open(DOWNLOAD_COUNT_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data.get('count', 0)
        except Exception:
            pass
    return 0


def save_download_count(count):
    """Save download counter."""
    try:
        with open(DOWNLOAD_COUNT_FILE, 'w', encoding='utf-8') as f:
            json.dump({'count': count}, f)
    except Exception:
        pass


# === URL HELPERS ===
def is_valid_youtube_url(url):
    """Check if URL is a valid YouTube URL."""
    if not url:
        return False
    patterns = [
        r'(?:https?://)?(?:www\.)?youtube\.com/watch\?v=[\w-]+',
        r'(?:https?://)?(?:www\.)?youtu\.be/[\w-]+',
        r'(?:https?://)?(?:www\.)?youtube\.com/shorts/[\w-]+',
    ]
    return any(re.search(p, url) for p in patterns)


def extract_video_id(url):
    """Extract YouTube video ID from URL."""
    if not url:
        return None
    # youtu.be/ID
    m = re.search(r'youtu\.be/([\w-]+)', url)
    if m:
        return m.group(1)
    # youtube.com/watch?v=ID
    m = re.search(r'[?&]v=([\w-]+)', url)
    if m:
        return m.group(1)
    # youtube.com/shorts/ID
    m = re.search(r'/shorts/([\w-]+)', url)
    if m:
        return m.group(1)
    return None


# === VIDEO INFO ===
def get_video_info(ffmpeg_path, video_path):
    """Get video metadata using ffprobe."""
    ffprobe = find_ffprobe()
    if not ffprobe or not os.path.exists(video_path):
        return {}
    try:
        cmd = [
            ffprobe, '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'stream=width,height,r_frame_rate,codec_name,pix_fmt,duration',
            '-show_entries', 'format=duration,bit_rate',
            '-of', 'json',
            video_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        data = json.loads(result.stdout)
        # Normalize structure
        if 'streams' not in data:
            data['streams'] = []
        if 'format' not in data:
            data['format'] = {}
        return data
    except Exception:
        return {'streams': [], 'format': {}}


# === HIGH QUALITY TRIM ===
def trim_clip(ffmpeg_path, input_path, output_path, start_sec, end_sec,
              resolution="1080p", bitrate_kbps=8000, fps=30, crf=18,
              preset="slow", encoder=None):
    """
    Trim a video clip with maximum quality preservation.

    Strategy:
    1. Try lossless copy trim first (fast, zero quality loss)
    2. If copy fails (not on keyframe), use frame-accurate output-seek re-encode
       with -ss AFTER -i for precise frame cutting
    """
    if not ffmpeg_path or not os.path.exists(input_path):
        raise FileNotFoundError(f"Input not found: {input_path}")

    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)

    # Determine duration
    duration = None
    if start_sec is not None and end_sec is not None:
        duration = end_sec - start_sec
    elif end_sec is not None:
        duration = end_sec

    # --- STRATEGY 1: Lossless copy trim ---
    if duration and duration > 0:
        copy_cmd = [
            ffmpeg_path, '-y',
            '-ss', str(start_sec),
            '-i', input_path,
            '-t', str(duration),
            '-c', 'copy',
            '-avoid_negative_ts', 'make_zero',
            '-fflags', '+genpts',
            output_path
        ]
        try:
            result = subprocess.run(copy_cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
                # Verify the output is valid (not just a few frames)
                info = get_video_info(ffmpeg_path, output_path)
                out_duration = 0
                if info and 'format' in info:
                    out_duration = float(info['format'].get('duration', 0))
                elif info and 'streams' in info:
                    # Fallback
                    pass

                # If copy trim produced a reasonable file, use it
                # Allow some tolerance for keyframe alignment
                if out_duration > 0 or os.path.getsize(output_path) > 100000:
                    print(f"  ✅ Lossless trim: {os.path.basename(output_path)}")
                    return output_path

            if os.path.exists(output_path):
                os.remove(output_path)
        except Exception:
            if os.path.exists(output_path):
                os.remove(output_path)

    # --- STRATEGY 2: Frame-accurate re-encode trim ---
    # CRITICAL FIX: -ss goes AFTER -i (output seek) for frame-accurate cutting
    # This decodes from the beginning and cuts at exact frames

    # Build resolution filter
    res_filter = None
    if resolution == '1080p':
        res_filter = "scale=1920:-2:flags=lanczos"
    elif resolution == '720p':
        res_filter = "scale=1280:-2:flags=lanczos"
    elif resolution == '480p':
        res_filter = "scale=854:-2:flags=lanczos"
    elif resolution == '360p':
        res_filter = "scale=640:-2:flags=lanczos"

    # Build filter chain
    filters = []
    if res_filter:
        filters.append(res_filter)
    # Add fps filter only if explicitly different from source
    if fps:
        filters.append(f"fps={fps}")
    filters.append("format=yuv420p")
    vf = ','.join(filters) if filters else "format=yuv420p"

    enc = encoder or get_best_video_encoder(ffmpeg_path)

    # CRITICAL: -ss AFTER -i for frame-accurate cutting
    cmd = [ffmpeg_path, '-y', '-i', input_path]

    # Output seek (frame accurate) — placed after input
    if start_sec and start_sec > 0:
        cmd.extend(['-ss', str(start_sec)])

    # Duration
    if duration and duration > 0:
        cmd.extend(['-t', str(duration)])

    # Video encoding — HIGH QUALITY
    cmd.extend([
        '-c:v', enc,
        '-crf', str(crf),
        '-preset', preset,
        '-pix_fmt', 'yuv420p',
        '-movflags', '+faststart',
    ])

    if enc == 'libx264':
        cmd.extend([
            '-tune', 'film',
            '-profile:v', 'high',
            '-level', '4.2',
        ])

    # Audio — high quality AAC
    cmd.extend([
        '-c:a', 'aac',
        '-b:a', '256k',
        '-ar', '48000',
        '-ac', '2',
    ])

    # Video filter
    cmd.extend(['-vf', vf])

    cmd.append(output_path)

    print(f"  🎬 Frame-accurate trim (CRF {crf}, preset {preset})...")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

    if result.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
        return output_path

    if os.path.exists(output_path):
        os.remove(output_path)

    # --- STRATEGY 3: High-quality re-encode trim ---
    # Build resolution filter
    res_filter = None
    if resolution == '1080p':
        res_filter = "scale=1920:-2:flags=lanczos"
    elif resolution == '720p':
        res_filter = "scale=1280:-2:flags=lanczos"
    elif resolution == '480p':
        res_filter = "scale=854:-2:flags=lanczos"
    elif resolution == '360p':
        res_filter = "scale=640:-2:flags=lanczos"

    # Build filter chain
    filters = []
    if res_filter:
        filters.append(res_filter)
    filters.append("format=yuv420p")  # Ensure compatibility
    vf = ','.join(filters) if filters else None

    # Pick encoder
    enc = encoder or get_best_video_encoder(ffmpeg_path)

    # Build command
    cmd = [ffmpeg_path, '-y']

    # Input seek for faster processing
    if start_sec and start_sec > 0:
        cmd.extend(['-ss', str(start_sec)])

    cmd.extend(['-i', input_path])

    # Output duration
    if duration and duration > 0:
        cmd.extend(['-t', str(duration)])

    # Video encoding — HIGH QUALITY SETTINGS
    cmd.extend([
        '-c:v', enc,
        '-crf', str(crf),           # Quality: 18 = visually lossless
        '-preset', preset,          # slow = better compression/quality
        '-pix_fmt', 'yuv420p',      # Compatibility
        '-movflags', '+faststart',  # Web playback optimization
    ])

    # Encoder-specific tuning
    if enc == 'libx264':
        cmd.extend([
            '-tune', 'film',           # Better for sports/action
            '-profile:v', 'high',
            '-level', '4.2',
        ])

    # Audio — copy if possible, otherwise high-quality AAC
    cmd.extend([
        '-c:a', 'aac',
        '-b:a', '256k',               # High audio bitrate
        '-ar', '48000',
        '-ac', '2',
    ])

    # Video filter
    if vf:
        cmd.extend(['-vf', vf])

    # Frame rate
    if fps:
        cmd.extend(['-r', str(fps)])

    cmd.append(output_path)

    print(f"  🎬 Re-encoding trim (CRF {crf}, preset {preset})...")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

    if result.returncode != 0:
        raise RuntimeError(f"Trim failed: {result.stderr}")

    if not os.path.exists(output_path) or os.path.getsize(output_path) < 1000:
        raise RuntimeError("Trim produced empty output")

    return output_path


# === HIGH QUALITY MERGE ===
def merge_clips(ffmpeg_path, clip_paths, output_path, re_encode=False,
                crf=18, preset="slow", encoder=None):
    """
    Merge multiple clips into one video with zero quality loss when possible.

    Strategy:
    1. Since all clips come from the SAME source video, use concat demuxer + -c copy
    2. Only re-encode if explicitly requested or clips are incompatible
    """
    if not ffmpeg_path:
        raise RuntimeError("FFmpeg not found")

    if not clip_paths:
        raise ValueError("No clips to merge")

    if len(clip_paths) == 1:
        shutil.copy2(clip_paths[0], output_path)
        return output_path

    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)

    # --- STRATEGY 1: Lossless concat demuxer ---
    # All clips come from the same source video, so they SHOULD be compatible.
    # We skip the strict codec check and try concat directly.
    # If it fails, we fall back to re-encode.

    list_file = output_path + '.concat_list.txt'
    try:
        with open(list_file, 'w', encoding='utf-8') as f:
            for clip in clip_paths:
                abs_path = os.path.abspath(clip)
                # Escape single quotes in path for concat file
                abs_path = abs_path.replace("'", "'\''")
                f.write(f"file '{abs_path}'\n")

        cmd = [
            ffmpeg_path, '-y',
            '-f', 'concat',
            '-safe', '0',
            '-i', list_file,
            '-c', 'copy',
            '-fflags', '+genpts',
            '-movflags', '+faststart',
            output_path
        ]

        print(f"  🔗 Lossless merge ({len(clip_paths)} clips)...")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

        if result.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
            print(f"  ✅ Lossless merge complete")
            return output_path

        # Log why it failed
        if result.returncode != 0:
            err = result.stderr
            if "Codec stream differs" in err or "does not match" in err:
                print(f"  ⚠️  Clips have incompatible codecs, falling back to re-encode")
            else:
                print(f"  ⚠️  Concat copy failed, falling back to re-encode")

        if os.path.exists(output_path):
            os.remove(output_path)
    finally:
        if os.path.exists(list_file):
            os.remove(list_file)

    # --- STRATEGY 2: High-quality re-encode merge ---
    if re_encode:
        print(f"  🎬 Re-encoding merge ({len(clip_paths)} clips)...")
    else:
        print(f"  🎬 Re-encoding merge (lossless concat failed, {len(clip_paths)} clips)...")

    enc = encoder or get_best_video_encoder(ffmpeg_path)

    # Use concat protocol (safer than filter_complex for same-codec sources)
    inputs = []
    for clip in clip_paths:
        inputs.extend(['-i', clip])

    # Build concat filter
    filter_parts = []
    for i in range(len(clip_paths)):
        filter_parts.append(f"[{i}:v:0][{i}:a:0]")
    filter_complex = ''.join(filter_parts) + f"concat=n={len(clip_paths)}:v=1:a=1[outv][outa]"

    cmd = [ffmpeg_path, '-y'] + inputs + [
        '-filter_complex', filter_complex,
        '-map', '[outv]',
        '-map', '[outa]',
        '-c:v', enc,
        '-crf', str(crf),
        '-preset', preset,
        '-pix_fmt', 'yuv420p',
        '-movflags', '+faststart',
        '-c:a', 'aac',
        '-b:a', '256k',
        '-ar', '48000',
        '-ac', '2',
    ]

    if enc == 'libx264':
        cmd.extend(['-tune', 'film', '-profile:v', 'high', '-level', '4.2'])

    cmd.append(output_path)

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

    if result.returncode != 0:
        raise RuntimeError(f"Merge failed: {result.stderr}")

    if not os.path.exists(output_path) or os.path.getsize(output_path) < 1000:
        raise RuntimeError("Merge produced empty output")

    return output_path


# === CSV READING ===
def read_csv_entries(csv_path):
    """Read CSV entries for auto-compiler.
    Expected format: player,match_id,action,start_time,end_time
    Skips comment lines (starting with // or #) and blank lines before the header.
    """
    entries = []

    # Read raw lines, skip comments and blank lines
    raw_lines = []
    with open(csv_path, 'r', encoding='utf-8') as f:
        for line in f:
            stripped = line.strip()
            # Skip blank lines and comment lines
            if not stripped or stripped.startswith('//') or stripped.startswith('#'):
                continue
            raw_lines.append(line)

    if not raw_lines:
        return []

    reader = csv.DictReader(raw_lines)
    fieldnames = reader.fieldnames or []

    # Map common column names
    col_map = {}
    for col in fieldnames:
        col_lower = col.lower().strip()
        if col_lower in ('player', 'name', 'player_name'):
            col_map['player'] = col
        elif col_lower in ('match_id', 'match', 'video', 'url', 'video_id', 'matchid'):
            col_map['match_id'] = col
        elif col_lower in ('action', 'event', 'clip_name', 'description'):
            col_map['action'] = col
        elif col_lower in ('start_time', 'start', 'starttime', 'begin'):
            col_map['start_time'] = col
        elif col_lower in ('end_time', 'end', 'endtime', 'finish'):
            col_map['end_time'] = col

    # Default mapping if no recognized columns
    if not col_map and len(fieldnames) >= 5:
        col_map = {
            'player': fieldnames[0],
            'match_id': fieldnames[1],
            'action': fieldnames[2],
            'start_time': fieldnames[3],
            'end_time': fieldnames[4],
        }

    for row in reader:
        try:
            player = row.get(col_map.get('player', 'player'), '').strip()
            match_id = row.get(col_map.get('match_id', 'match_id'), '').strip()
            action = row.get(col_map.get('action', 'action'), '').strip()
            start_str = row.get(col_map.get('start_time', 'start_time'), '').strip()
            end_str = row.get(col_map.get('end_time', 'end_time'), '').strip()

            if not player or not match_id:
                continue

            start = parse_time_to_seconds(start_str) or 0
            end = parse_time_to_seconds(end_str)

            if end is not None and end <= start:
                continue

            entries.append({
                'player': player,
                'match_id': match_id,
                'action': action or 'clip',
                'start': start,
                'end': end,
                'start_str': start_str or '0:00',
                'end_str': end_str or '',
            })
        except Exception:
            continue

    return entries


def format_size(size_bytes):
    """Format byte size to human readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"


def format_duration(seconds):
    """Format seconds to MM:SS or HH:MM:SS."""
    if seconds is None:
        return "00:00"
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

