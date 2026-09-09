"""Video Tool Auto-Compiler.

Compiles player clips from CSV timestamps. Source resolution, trimming and
merging are kept separate from the CLI so the compiler can also be imported.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

try:
    import yt_dlp  # noqa: F401
    HAS_YTDLP = True
except ImportError:
    HAS_YTDLP = False

from ..core import (
    add_to_cache,
    extract_video_id,
    find_ffmpeg,
    get_cached_path,
    is_valid_youtube_url,
    load_cache,
    merge_clips,
    read_csv_entries,
    resolve_cached_file,
    trim_clip,
)
from ..csv_prep import get_compiler_csv, write_manifests
from ..version_check import check_dependencies
from ..youtube import DownloadOptions, download_url

# === CONFIG ===
DEFAULT_OUTPUT_FOLDER = os.path.join(os.path.expanduser('~'), 'Downloads', 'video-tool', 'Compilations')
DEFAULT_DOWNLOAD_FOLDER = os.path.join(os.path.expanduser('~'), 'Downloads', 'video-tool')
PRO_CONFIG_FILE = "video_tool_pro_config.json"


def _load_pro_cookie_browser():
    """Load cookie_browser setting from Video Tool Pro config if available."""
    try:
        if os.path.exists(PRO_CONFIG_FILE):
            with open(PRO_CONFIG_FILE, encoding='utf-8') as f:
                cfg = json.load(f)
                return cfg.get('cookie_browser')
    except Exception:
        pass
    return None


# === SOURCE TYPE DETECTION ===
def detect_source_type(match_id):
    """
    Detect what type of source match_id refers to.
    Returns: ('local', path) or ('youtube', url) or ('cache_key', key)
    """
    if not match_id:
        return ('unknown', None)
    
    match_id = match_id.strip()
    
    # 1. Check if it is a valid local file path (absolute or relative)
    if os.path.isfile(match_id):
        return ('local', os.path.abspath(match_id))
    
    # 2. Check if it is a relative path in current directory
    if os.path.isfile(os.path.join(os.getcwd(), match_id)):
        return ('local', os.path.abspath(os.path.join(os.getcwd(), match_id)))
    
    # 3. Check if it is a relative path with .mp4 extension
    mp4_path = match_id if match_id.endswith('.mp4') else match_id + '.mp4'
    if os.path.isfile(mp4_path):
        return ('local', os.path.abspath(mp4_path))
    if os.path.isfile(os.path.join(os.getcwd(), mp4_path)):
        return ('local', os.path.abspath(os.path.join(os.getcwd(), mp4_path)))
    
    # 4. Check if it is a YouTube URL
    if is_valid_youtube_url(match_id):
        return ('youtube', match_id)
    
    # 5. Treat as cache key
    return ('cache_key', match_id)


# === DOWNLOAD MISSING (deduplicated) ===
_download_locks = {}
_download_results = {}


def _get_download_lock(video_id):
    if video_id not in _download_locks:
        _download_locks[video_id] = threading.Lock()
    return _download_locks[video_id]


def download_video(
    video_id,
    url,
    folder,
    format_type="MP4",
    quality="Best",
    cookie_browser=None,
    cookie_file=None,
    bypass_no_auth=True,
    remote_components=None,
):
    """Download one missing compiler source through the shared downloader."""
    if not HAS_YTDLP:
        print("yt-dlp not installed. Cannot download missing videos.")
        return None

    if video_id in _download_results:
        cached = _download_results[video_id]
        if cached and os.path.exists(cached):
            return cached

    lock = _get_download_lock(video_id)
    with lock:
        if video_id in _download_results:
            cached = _download_results[video_id]
            if cached and os.path.exists(cached):
                return cached

        try:
            options = DownloadOptions(
                output_dir=folder,
                format_type=format_type,
                quality=quality,
                cookie_browser=cookie_browser,
                cookie_file=cookie_file,
                bypass_no_auth=bypass_no_auth,
                remote_components=(
                    remote_components if remote_components is not None
                    else ["ejs:github"]
                ),
            )
            result = download_url(url, options)

            if result:
                _download_results[video_id] = result
                return result

        except Exception as exc:
            message = str(exc)
            if "Sign in to confirm" in message or "confirm you're not a bot" in message:
                print("  YouTube bot-check blocked this download.")
                if not cookie_browser and not cookie_file:
                    print("      Bypass mode failed. YouTube requires authentication for this video.")
                print("      Try: select a browser with an active YouTube login,")
                print("      Or: export cookies.txt from your browser and select it below.")
            else:
                print(f"  Download failed for {video_id}: {message}")

        _download_results[video_id] = None
        return None


# === MAIN COMPILER ===
class AutoCompiler:
    def __init__(self, csv_path, output_folder=None,
                 resolution="1080p", bitrate_kbps=8000, fps=30,
                 download_missing=True, quality='Best', dry_run=False,
                 keep_temp=False, jobs=1, cookie_browser=None, cookie_file=None,
                 bypass_no_auth=True, crf=18, preset="slow"):
        # Checked at most once a day; never blocks a run if GitHub is
        # unreachable. See version_check.py for why yt-dlp auto-upgrades
        # but ffmpeg only gets a warning.
        check_dependencies()

        # Raw tagger exports get converted here; already-compiler-ready
        # CSVs pass through untouched. See csv_prep.py.
        self.csv_path = get_compiler_csv(csv_path)
        self.output_folder = output_folder or DEFAULT_OUTPUT_FOLDER
        self.resolution = resolution
        self.bitrate_kbps = bitrate_kbps
        self.fps = fps
        # Missing YouTube sources are downloaded automatically.
        # Local files and cached copies are always checked first.
        self.download_missing = True
        self.quality = quality
        self.dry_run = dry_run
        self.keep_temp = keep_temp
        self.jobs = max(1, jobs)
        self.bypass_no_auth = bypass_no_auth
        self.crf = crf
        self.preset = preset

        self.cache = load_cache()
        self.ffmpeg_path = find_ffmpeg()
        self.temp_clips = []
        self.stats = {'trimmed': 0, 'failed': 0, 'downloaded': 0,
                      'local': 0, 'cached': 0}
        self.cookie_browser = cookie_browser
        self.cookie_file = cookie_file

        self.download_folder = os.path.dirname(self.output_folder)
        if self.download_folder == self.output_folder:
            self.download_folder = DEFAULT_DOWNLOAD_FOLDER

        os.makedirs(self.output_folder, exist_ok=True)
        os.makedirs(self.download_folder, exist_ok=True)

    def ensure_video_available(self, match_id):
        """
        Unified video resolver. Handles all source types automatically:
        1. Local file paths (absolute or relative)
        2. Cached videos
        3. YouTube URLs (download if needed)
        4. Cache keys
        
        Returns the resolved video path or None.
        """
        if not match_id:
            return None
        
        match_id = match_id.strip()
        
        # === STEP 1: Check if it is a local file ===
        source_type, resolved_path = detect_source_type(match_id)
        
        if source_type == 'local':
            print(f'  Local file: {os.path.basename(resolved_path)}')
            self.stats['local'] += 1
            return resolved_path
        
        # === STEP 2: Resolve local/cache copies before any network access ===
        if source_type == 'youtube':
            cached = resolve_cached_file(
                match_id,
                'MP4',
                cache=self.cache,
                directories=[self.download_folder],
            )
        else:
            lookup_key = match_id
            cached = get_cached_path(self.cache, lookup_key, 'MP4')
            if not cached and source_type == 'cache_key' and not match_id.endswith('.mp4'):
                cached = get_cached_path(self.cache, match_id + '.mp4', 'MP4')

        if cached:
            print(f'  Cached/local: {os.path.basename(cached)}')
            self.stats['cached'] += 1
            return cached

        # === STEP 3: Download missing YouTube sources automatically ===
        # Cache/local resolution above always happens first, so a network
        # request is made only when no usable local copy exists.
        if source_type == 'youtube' and self.download_missing and HAS_YTDLP:
            video_id = extract_video_id(match_id)
            print(f'  Downloading: {video_id}')
            downloaded = download_video(
                video_id, match_id, self.download_folder, 'MP4',
                self.quality, self.cookie_browser, self.cookie_file,
                bypass_no_auth=self.bypass_no_auth
            )
            if downloaded:
                add_to_cache(self.cache, video_id, 'MP4', downloaded)
                self.stats['downloaded'] += 1
                return downloaded

        # === STEP 5: Nothing worked ===
        if source_type == 'youtube' and not HAS_YTDLP:
            print("  yt-dlp not installed. Cannot download YouTube video.")
        elif source_type == 'youtube':
            print(
                "  ERROR: Video is not available locally or in cache, "
                "and the source could not be downloaded."
            )
            self.stats['failed'] += 1
        elif source_type == 'cache_key':
            print(f"  Cache key '{match_id}' not found and not a valid file/URL.")
        else:
            print(f"  Could not resolve source: {match_id}")
        
        return None

    def generate_clip(self, entry):
        """Generate a single trimmed clip. Returns clip path or None."""
        match_id = entry['match_id']
        video_path = self.ensure_video_available(match_id)

        if not video_path:
            print(f'  No video available for: {match_id}')
            return None

        temp_folder = os.path.join(self.output_folder, '.temp')
        os.makedirs(temp_folder, exist_ok=True)

        safe_player = re.sub(r'[^\w\s-]', '', entry['player']).strip().replace(' ', '_')
        safe_action = re.sub(r'[^\w\s-]', '', entry['action']).strip().replace(' ', '_')
        start_sec = int(entry['start'])
        clip_name = f"{start_sec:05d}_{safe_player}_{safe_action}_{entry['start_str']}-{entry['end_str']}.mp4"
        clip_path = os.path.join(temp_folder, clip_name)

        if self.dry_run:
            print(f"  [DRY-RUN] Would trim: {entry['player']} | {entry['action']} | {entry['start_str']} -> {entry['end_str']}")
            return clip_path

        print(f"  Trimming: {entry['player']} | {entry['action']} | {entry['start_str']} -> {entry['end_str']}")

        try:
            trim_clip(
                self.ffmpeg_path,
                video_path,
                clip_path,
                entry['start'],
                entry['end'],
                resolution=self.resolution,
                bitrate_kbps=self.bitrate_kbps,
                fps=self.fps,
                crf=self.crf,
                preset=self.preset
            )
            self.temp_clips.append(clip_path)
            self.stats['trimmed'] += 1
            return clip_path
        except Exception as e:
            print(f'  Failed: {e}')
            self.stats['failed'] += 1
            return None

    def compile_player(self, player_name, entries):
        """Compile all clips for a specific player into one video."""
        print(f"\n{'='*50}")
        print(f'Compiling: {player_name}')
        print(f"{'='*50}")

        entries = sorted(entries, key=lambda x: x['start'])

        clips = []
        if self.jobs > 1 and not self.dry_run:
            with ThreadPoolExecutor(max_workers=self.jobs) as executor:
                future_to_entry = {executor.submit(self.generate_clip, e): e for e in entries}
                for future in as_completed(future_to_entry):
                    result = future.result()
                    if result:
                        clips.append(result)
            clips = sorted(clips)
        else:
            for entry in entries:
                clip = self.generate_clip(entry)
                if clip:
                    clips.append(clip)

        if not clips:
            print(f'No clips generated for {player_name}')
            return None

        safe_name = re.sub(r'[^\w\s-]', '', player_name).strip().replace(' ', '_')
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        output_name = f'{safe_name}_Compilation_{timestamp}.mp4'
        output_path = os.path.join(self.output_folder, output_name)

        if self.dry_run:
            print(f'\n[DRY-RUN] Would merge {len(clips)} clips into:\n  {output_path}')
            return output_path

        print(f'\nMerging {len(clips)} clips...')

        try:
            merge_clips(
                self.ffmpeg_path,
                clips,
                output_path,
                re_encode=False,
                crf=self.crf,
                preset=self.preset,
            )
            if not os.path.exists(output_path) or os.path.getsize(output_path) < 1000:
                raise RuntimeError("Merge completed without producing a valid output file")
            size_mb = os.path.getsize(output_path) / (1024 * 1024)
            print(f'Done! {output_path}')
            print(f'Size: {size_mb:.1f} MB')
            return output_path
        except Exception as e:
            print(f'Merge failed: {e}')
            return None

    def run(self):
        """Main entry point."""
        print(f'Reading: {self.csv_path}')
        entries = read_csv_entries(self.csv_path)

        if not entries:
            print("No valid entries found in CSV.")
            return {}

        print(f'Found {len(entries)} clip entries')

        by_player = {}
        for e in entries:
            by_player.setdefault(e['player'], []).append(e)

        print(f'Players: {', '.join(by_player.keys())}')

        results = {}
        for player, player_entries in by_player.items():
            output = self.compile_player(player, player_entries)
            if output:
                results[player] = output

        if not self.keep_temp:
            print('\nCleaning up temp files...')
            for clip in self.temp_clips:
                try:
                    if os.path.exists(clip):
                        os.remove(clip)
                except Exception:
                    pass
            temp_folder = os.path.join(self.output_folder, '.temp')
            try:
                if os.path.exists(temp_folder) and not os.listdir(temp_folder):
                    os.rmdir(temp_folder)
            except Exception:
                pass
        else:
            print(f'\nKept temp files in: {os.path.join(self.output_folder, ".temp")}')

        print(f"\n{'='*50}")
        print('SUMMARY')
        print(f"{'='*50}")
        for player, path in results.items():
            if self.dry_run:
                print(f"  [DRY-RUN] {player}: {path}")
            elif os.path.exists(path):
                size = os.path.getsize(path) / (1024 * 1024)
                print(f"  {player}: {size:.1f} MB -> {path}")
            else:
                print(f"  {player}: file not found at {path}")
        
        print(f"\n  Trimmed: {self.stats['trimmed']}  |  Failed: {self.stats['failed']}")
        print(f"  Local: {self.stats['local']}  |  Cached: {self.stats['cached']}  |  Downloaded: {self.stats['downloaded']}")

        # Group finished compilations by player into the manifest files the
        # app reads from, so they're ready without any extra step.
        if results and not self.dry_run:
            raw_match_id = entries[0]['match_id']
            match_id = extract_video_id(raw_match_id) or raw_match_id
            # Manifests live next to whatever output folder was actually
            # used this run, not a fixed default, so a manifest and the
            # files it points to are always found together.
            manifest_root = os.path.join(
                os.path.dirname(self.output_folder), 'manifests'
            )
            write_manifests(
                match_id, raw_match_id, results, manifest_root=manifest_root
            )

        return results


# === CLI INTERFACE ===
def main():
    parser = argparse.ArgumentParser(
        description='Video Tool Auto-Compiler v3.0 -- unified CSV for YouTube, local files, and cached videos',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
UNIFIED CSV FORMAT:
  The match_id column accepts ANY of these:
    YouTube URL:     https://www.youtube.com/watch?v=ABC123
    Local path:      /home/lab/videos/match1.mp4  (or just match1.mp4)
    Cache key:       ABC123  (previously downloaded YouTube video ID)

  All sources work in the SAME CSV. The compiler auto-detects the type.

Examples:
  # Local/cache sources are used first; missing YouTube sources download automatically.
  python -m video_tool.versions.auto_compiler tests/cape_verde.csv

  # Maximum quality (visually lossless, larger files)
  python -m src.auto_compiler tests/cape_verde.csv --crf 16 --preset slower

  # The local youtube_cookies.txt file is used automatically when present.
  python -m video_tool.versions.auto_compiler tests/cape_verde.csv
        """
    )
    parser.add_argument('csv', help='Path to CSV file with timestamps')
    parser.add_argument('-o', '--output', default=DEFAULT_OUTPUT_FOLDER,
                        help='Output folder (default: ~/Downloads/Video Tool/Compilations)')
    parser.add_argument('-r', '--resolution', default='1080p',
                        choices=['1080p', '720p', '480p', '360p'],
                        help='Output resolution')
    parser.add_argument('-b', '--bitrate', type=int, default=8000,
                        help='Target video bitrate in kbps (default: 8000)')
    parser.add_argument('-f', '--fps', type=int, default=30,
                        help='Output frame rate (default: 30)')
    parser.add_argument('-p', '--player', default=None,
                        help='Compile only this specific player')
    parser.add_argument('-q', '--quality', default='Best',
                        choices=['Best', '1080p', '720p', '480p', '360p'],
                        help='Download quality when video needs to be fetched')
    parser.add_argument('-n', '--dry-run', action='store_true',
                        help='Preview the plan without running ffmpeg')
    parser.add_argument('--keep-temp', action='store_true',
                        help='Preserve temporary trimmed clips')
    parser.add_argument('-j', '--jobs', type=int, default=1,
                        help='Parallel trim jobs (default: 1)')
    parser.add_argument('--crf', type=int, default=18,
                        choices=range(0, 51),
                        help='Quality: 0=lossless, 16=great, 18=visually lossless, 23=good, 28=smaller (default: 18)')
    parser.add_argument('--preset', default='slow',
                        choices=['ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow', 'slower', 'veryslow'],
                        help='Encoding speed/quality tradeoff (default: slow)')
    parser.add_argument('--cookie-browser', default=None,
                        choices=['chrome', 'firefox', 'edge', 'safari', 'brave', 'opera', 'none'],
                        help='Browser to extract cookies from for YouTube auth')
    parser.add_argument('--cookie-file', default=None,
                        help='Path to a Netscape cookies.txt file for YouTube auth')
    parser.add_argument('--no-bypass', action='store_true',
                        help='Disable no-auth bypass mode (default: bypass enabled)')

    args = parser.parse_args()

    cookie_browser = args.cookie_browser or _load_pro_cookie_browser()
    if cookie_browser and cookie_browser.lower() == 'none':
        cookie_browser = None

    compiler = AutoCompiler(
        csv_path=args.csv,
        output_folder=args.output,
        resolution=args.resolution,
        bitrate_kbps=args.bitrate,
        fps=args.fps,
        # Missing sources are downloaded automatically after cache/local checks.
        download_missing=True,
        quality=args.quality,
        dry_run=args.dry_run,
        keep_temp=args.keep_temp,
        jobs=args.jobs,
        cookie_browser=cookie_browser,
        cookie_file=args.cookie_file,
        bypass_no_auth=not args.no_bypass,
        crf=args.crf,
        preset=args.preset
    )

    if args.player:
        entries = read_csv_entries(args.csv)
        player_entries = [e for e in entries if e['player'].lower() == args.player.lower()]
        if not player_entries:
            print(f"Player '{args.player}' not found in CSV.")
            return
        compiler.compile_player(args.player, player_entries)
    else:
        compiler.run()


if __name__ == "__main__":
    main()