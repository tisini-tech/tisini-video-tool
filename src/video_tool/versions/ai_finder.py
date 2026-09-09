"""
Video Tool AI Finder
Watch a video, find the parts that match a plain-English description,
and write them out as a CSV in the same shape auto_compiler.py expects:

    player,match_id,action,start_time,end_time

Feed that CSV straight into auto_compiler.py -- no changes needed there.

Requirements:
    pip install google-genai
    export GEMINI_API_KEY=your_key_here

Examples:
    # Local file, one description
    python -m src.ai_finder "match.mp4" "goalkeeper saves" -l "Keeper Saves"

    # YouTube link, downloads first, then scans
    python -m src.ai_finder "https://youtu.be/XXXX" "every time a lion appears" \\
        -l "Lion Sightings" --download-missing

    # Long video: split into 15-minute chunks before scanning (recommended
    # for anything over ~30 minutes -- keeps each AI call fast and accurate)
    python -m src.ai_finder "match.mp4" "goalkeeper saves" -l "Keeper Saves" \\
        --chunk-minutes 15

    # Scan, then immediately compile the result
    python -m src.ai_finder "match.mp4" "goalkeeper saves" -l "Keeper Saves" --compile
"""

import argparse
import csv
import json
import os
import re
import time

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # falls back to whatever is already set in the environment

import subprocess

from ..core import extract_video_id, find_ffmpeg, find_ffprobe, seconds_to_timestamp
from .auto_compiler import AutoCompiler, detect_source_type, download_video

try:
    from google import genai
    from google.genai import types as genai_types
    HAS_GENAI = True
except ImportError:
    HAS_GENAI = False


DEFAULT_MODEL = "gemini-3.6-flash"
DEFAULT_DOWNLOAD_FOLDER = os.path.join(os.path.expanduser('~'), 'Downloads', 'Video-Tool')


# === VIDEO LENGTH ===
def get_duration_seconds(video_path):
    """Read a video's length with ffprobe."""
    ffprobe = find_ffprobe()
    if not ffprobe:
        return None
    try:
        cmd = [ffprobe, '-v', 'error', '-show_entries', 'format=duration',
               '-of', 'json', video_path]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        data = json.loads(result.stdout)
        return float(data.get('format', {}).get('duration', 0)) or None
    except Exception:
        return None


# === SPLIT INTO CHUNKS ===
def split_into_chunks(ffmpeg_path, video_path, chunk_seconds, work_folder):
    """
    Cut a long video into fixed-length pieces with a fast stream copy.
    Returns a list of (chunk_path, offset_seconds).
    """
    duration = get_duration_seconds(video_path)
    if not duration or duration <= chunk_seconds:
        return [(video_path, 0)]

    os.makedirs(work_folder, exist_ok=True)
    chunks = []
    offset = 0
    idx = 0
    base = os.path.splitext(os.path.basename(video_path))[0]

    while offset < duration:
        chunk_path = os.path.join(work_folder, f"{base}_chunk{idx:03d}.mp4")
        cmd = [
            ffmpeg_path, '-y',
            '-ss', str(offset),
            '-i', video_path,
            '-t', str(chunk_seconds),
            '-c', 'copy',
            '-avoid_negative_ts', 'make_zero',
            chunk_path
        ]
        print(f"  Cutting chunk {idx}: {seconds_to_timestamp(offset)} onward...")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode == 0 and os.path.exists(chunk_path) and os.path.getsize(chunk_path) > 1000:
            chunks.append((chunk_path, offset))
        else:
            print(f"  Warning: chunk {idx} failed, skipping")
        offset += chunk_seconds
        idx += 1

    return chunks


# === GEMINI CALL ===
PROMPT_TEMPLATE = """You are watching a video clip. Find every moment that matches this description:

"{description}"

Return ONLY a JSON array, nothing else. Each item must have this exact shape:
[{{"start": "MM:SS", "end": "MM:SS"}}]

Rules:
- Use MM:SS or HH:MM:SS format for times, matching where the moment happens in THIS clip.
- Each entry should cover just the moment itself, not extra padding before or after.
- If nothing in this clip matches, return an empty array: []
- Do not include any text, explanation, or markdown -- only the JSON array.
"""


def _ascii_safe_copy(video_path):
    """
    Gemini's upload call puts the filename in an HTTP header, and headers
    can't hold non-ASCII characters. Video titles with emoji, curly quotes,
    or full-width punctuation (common in YouTube titles) crash the upload.
    If the name isn't plain ASCII, make a same-content copy under a safe
    name and upload that instead. Returns (path_to_upload, is_temp_copy).
    """
    basename = os.path.basename(video_path)
    try:
        basename.encode('ascii')
        return video_path, False
    except UnicodeEncodeError:
        pass

    ext = os.path.splitext(video_path)[1] or '.mp4'
    safe_dir = os.path.join(os.path.dirname(video_path) or '.', '.ai_finder_upload')
    os.makedirs(safe_dir, exist_ok=True)
    safe_path = os.path.join(safe_dir, f"upload_{abs(hash(video_path)) % 100000}{ext}")

    try:
        os.link(video_path, safe_path)  # instant, no extra disk space, same filesystem
    except OSError:
        import shutil
        shutil.copy2(video_path, safe_path)  # falls back if link isn't possible (e.g. different drive)

    return safe_path, True


def ask_gemini_for_moments(client, video_path, description, model=DEFAULT_MODEL):
    """Upload a video chunk to Gemini and ask it to find matching moments."""
    print(f"  Uploading {os.path.basename(video_path)}...")
    upload_path, is_temp = _ascii_safe_copy(video_path)
    upload_start = time.time()
    uploaded = client.files.upload(file=upload_path)
    if is_temp:
        os.remove(upload_path)
    print(f"  Upload sent in {time.time() - upload_start:.0f}s. Waiting for Gemini to process the video...")

    # Wait for Gemini to finish processing the upload
    waited = 0
    while uploaded.state.name == "PROCESSING":
        time.sleep(3)
        waited += 3
        print(f"  Still processing... ({waited}s)")
        uploaded = client.files.get(name=uploaded.name)

    if uploaded.state.name == "FAILED":
        print(f"  Upload processing failed for {video_path}")
        return []

    print("  Processing done. Asking the model to find matching moments...")
    prompt = PROMPT_TEMPLATE.format(description=description)

    response = client.models.generate_content(
        model=model,
        contents=[uploaded, prompt],
        config=genai_types.GenerateContentConfig(
            response_mime_type="application/json",
        ),
    )

    # Clean up the uploaded file on Gemini's side once we're done with it
    try:
        client.files.delete(name=uploaded.name)
    except Exception:
        pass

    text = (response.text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        moments = json.loads(text)
        if isinstance(moments, list):
            return moments
    except json.JSONDecodeError:
        print(f"  Could not parse model response: {text[:200]}")
    return []


def time_str_to_seconds(t):
    parts = [float(p) for p in str(t).split(':')]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0]


# === MAIN ===
def find_clips(video_source, description, label, output_csv,
                download_missing=False, quality='Best',
                chunk_minutes=15, model=DEFAULT_MODEL,
                cookie_browser=None, cookie_file=None, keep_chunks=False):

    if not HAS_GENAI:
        print("google-genai is not installed. Run: pip install google-genai")
        return None

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Set the GEMINI_API_KEY environment variable first.")
        return None

    client = genai.Client(api_key=api_key)
    ffmpeg_path = find_ffmpeg()
    if not ffmpeg_path:
        print("ffmpeg not found. It's needed to split long videos into chunks.")
        return None

    # === Resolve the source video to a local file ===
    source_type, resolved = detect_source_type(video_source)
    match_id_for_csv = video_source  # keep the original reference in the output CSV

    if source_type == 'local':
        local_path = resolved
    elif source_type == 'youtube':
        if not download_missing:
            print("This is a YouTube link. Pass --download-missing to fetch it first.")
            return None
        video_id = extract_video_id(video_source)
        print(f"Downloading: {video_id}")
        local_path = download_video(
            video_id, video_source, DEFAULT_DOWNLOAD_FOLDER, 'MP4',
            quality, cookie_browser, cookie_file
        )
        if not local_path:
            print("Download failed.")
            return None
    else:
        # source_type is 'cache_key' here -- detect_source_type() falls back to this
        # for anything that isn't a real file and isn't a YouTube link. Give a
        # sharper message depending on what the input looks like, so a bad path
        # doesn't look the same as a genuinely unknown source.
        looks_like_path = '/' in video_source or '\\' in video_source or video_source.lower().endswith('.mp4')
        if looks_like_path:
            print(f"No file found at: {video_source}")
            if not video_source.startswith('/') and not re.match(r'^[A-Za-z]:', video_source):
                print("  This path isn't absolute. If you meant a specific file, "
                      "start it with '/' (or check for a dropped leading slash).")
            print(f"  Checked: {os.path.abspath(video_source)}")
        else:
            print(f"Could not resolve source: {video_source}")
            print("  Not a real file, not a YouTube link, and no matching cache entry.")
        return None

    # === Split into chunks so each AI call stays fast and accurate ===
    work_folder = os.path.join(os.path.dirname(local_path) or '.', '.ai_finder_chunks')
    chunk_seconds = max(60, chunk_minutes * 60)
    chunks = split_into_chunks(ffmpeg_path, local_path, chunk_seconds, work_folder)

    print(f"\nScanning {len(chunks)} chunk(s) for: \"{description}\"\n")

    all_entries = []
    for i, (chunk_path, offset) in enumerate(chunks):
        print(f"Chunk {i + 1}/{len(chunks)} (starts at {seconds_to_timestamp(offset)}):")
        moments = ask_gemini_for_moments(client, chunk_path, description, model=model)
        print(f"  Found {len(moments)} moment(s)")

        for m in moments:
            try:
                start = time_str_to_seconds(m['start']) + offset
                end = time_str_to_seconds(m['end']) + offset
                if end <= start:
                    continue
                all_entries.append({
                    'player': label,
                    'match_id': match_id_for_csv,
                    'action': description[:40],
                    'start_time': seconds_to_timestamp(start),
                    'end_time': seconds_to_timestamp(end),
                })
            except (KeyError, ValueError, TypeError):
                print(f"  Skipping malformed entry: {m}")

    # Clean up chunk files unless asked to keep them
    if not keep_chunks and len(chunks) > 1:
        for chunk_path, _ in chunks:
            if chunk_path != local_path and os.path.exists(chunk_path):
                os.remove(chunk_path)
        try:
            os.rmdir(work_folder)
        except OSError:
            pass

    # === Write the CSV in auto_compiler's expected format ===
    os.makedirs(os.path.dirname(output_csv) or '.', exist_ok=True)
    with open(output_csv, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['player', 'match_id', 'action', 'start_time', 'end_time'])
        writer.writeheader()
        for entry in all_entries:
            writer.writerow(entry)

    print(f"\nWrote {len(all_entries)} clip(s) to {output_csv}")
    return output_csv


def main():
    parser = argparse.ArgumentParser(
        description='Video Tool AI Finder -- describe what you want, get a CSV auto_compiler.py can use',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('video', help='Local video path or YouTube URL')
    parser.add_argument('description', help='Plain-English description of what to find, e.g. "goalkeeper saves"')
    parser.add_argument('-l', '--label', default='AI_Clips',
                        help='Name for this set of clips (becomes the "player" column, and the output filename)')
    parser.add_argument('-o', '--output', default=None,
                        help='Output CSV path (default: <label>.csv)')
    parser.add_argument('--download-missing', action='store_true',
                        help='Download the video first if a YouTube URL is given')
    parser.add_argument('-q', '--quality', default='Best',
                        choices=['Best', '1080p', '720p', '480p', '360p'])
    parser.add_argument('--chunk-minutes', type=int, default=15,
                        help='Split the video into pieces this long before scanning (default: 15)')
    parser.add_argument('--model', default=DEFAULT_MODEL, help=f'Gemini model to use (default: {DEFAULT_MODEL})')
    parser.add_argument('--cookie-browser', default=None,
                        choices=['chrome', 'firefox', 'edge', 'safari', 'brave', 'opera', 'none'])
    parser.add_argument('--cookie-file', default=None)
    parser.add_argument('--keep-chunks', action='store_true', help='Keep the split video pieces after scanning')
    parser.add_argument('--compile', action='store_true',
                        help='Run auto_compiler.py on the result right after scanning')

    args = parser.parse_args()
    output_csv = args.output or f"{args.label.replace(' ', '_')}.csv"

    csv_path = find_clips(
        args.video, args.description, args.label, output_csv,
        download_missing=args.download_missing, quality=args.quality,
        chunk_minutes=args.chunk_minutes, model=args.model,
        cookie_browser=args.cookie_browser, cookie_file=args.cookie_file,
        keep_chunks=args.keep_chunks,
    )

    if csv_path and args.compile:
        print(f"\nCompiling {csv_path}...")
        compiler = AutoCompiler(csv_path=csv_path, download_missing=args.download_missing)
        compiler.run()


if __name__ == "__main__":
    main()