"""
🎬 YFetch Auto-Compiler v2.5
Automatically generate player compilations from a CSV of timestamps.

New in v2.5:
• HIGH QUALITY output — lossless trim/merge when possible
• Proper CRF 18 + slow preset for re-encodes
• Higher default bitrate (8000 kbps)
• One unified command for cached and new videos
"""

import os
import sys
import re
import shutil
import argparse
import threading
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

try:
    from .core import (
        find_ffmpeg,
        parse_time_to_seconds,
        seconds_to_timestamp,
        load_cache,
        save_cache,
        get_cached_path,
        add_to_cache,
        load_config,
        trim_clip,
        merge_clips,
        read_csv_entries,
        is_valid_youtube_url,
        extract_video_id,
    )
except ImportError:
    from core import (
        find_ffmpeg,
        parse_time_to_seconds,
        seconds_to_timestamp,
        load_cache,
        save_cache,
        get_cached_path,
        add_to_cache,
        load_config,
        trim_clip,
        merge_clips,
        read_csv_entries,
        is_valid_youtube_url,
        extract_video_id,
    )

try:
    import yt_dlp
    HAS_YTDLP = True
except ImportError:
    HAS_YTDLP = False


# === CONFIG ===
DEFAULT_OUTPUT_FOLDER = os.path.join(os.path.expanduser('~'), 'Downloads', 'YFetch', 'Compilations')
DEFAULT_DOWNLOAD_FOLDER = os.path.join(os.path.expanduser('~'), 'Downloads', 'YFetch')
PRO_CONFIG_FILE = "yfetch_pro_config.json"


def _load_pro_cookie_browser():
    """Load cookie_browser setting from YFetch Pro config if available."""
    try:
        if os.path.exists(PRO_CONFIG_FILE):
            with open(PRO_CONFIG_FILE, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
                return cfg.get('cookie_browser')
    except Exception:
        pass
    return None


# === DOWNLOAD MISSING (deduplicated) ===
_download_locks = {}
_download_results = {}


def _get_download_lock(video_id):
    if video_id not in _download_locks:
        _download_locks[video_id] = threading.Lock()
    return _download_locks[video_id]


def download_video(video_id, url, folder, format_type='MP4', quality='Best',
                   cookie_browser=None, cookie_file=None, bypass_no_auth=True):
    if not HAS_YTDLP:
        print("❌ yt-dlp not installed. Cannot download missing videos.")
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

        os.makedirs(folder, exist_ok=True)

        quality_filter = ""
        if quality == '1080p': quality_filter = "[height<=1080]"
        elif quality == '720p': quality_filter = "[height<=720]"
        elif quality == '480p': quality_filter = "[height<=480]"
        elif quality == '360p': quality_filter = "[height<=360]"

        opts = {
            'outtmpl': f'{folder}/%(title)s.%(ext)s',
            'retries': 10,
            'fragment_retries': 10,
            'ignoreerrors': False,
            'noplaylist': True,
            'quiet': True,
            'no_warnings': True,
        }

        has_auth = False
        if cookie_file and os.path.exists(cookie_file):
            opts['cookies'] = [cookie_file]
            print(f"  🔐 Using cookie file: {cookie_file}")
            has_auth = True
        elif cookie_browser and cookie_browser != 'none':
            opts['cookiesfrombrowser'] = (cookie_browser,)
            print(f"  🔐 Using browser cookies: {cookie_browser}")
            has_auth = True
        else:
            print(f"  🌐 No auth provided — attempting without cookies...")

        if not has_auth and bypass_no_auth:
            print(f"  🛡️  Enabling no-auth bypass mode...")
            opts['extractor_args'] = {
                'youtube': {
                    'player_client': 'android',
                    'player_skip': 'webpage,configs,js',
                }
            }

        if format_type == 'MP3':
            opts['format'] = 'bestaudio/best'
            opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]
        else:
            if quality == 'Best':
                opts['format'] = f"bv*+ba/best{quality_filter}"
            else:
                opts['format'] = f"bestvideo{quality_filter}+bestaudio{quality_filter}/best{quality_filter}"
            opts['merge_output_format'] = 'mp4'

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
                if format_type == 'MP3':
                    base = os.path.splitext(filename)[0]
                    mp3_path = base + '.mp3'
                    if os.path.exists(mp3_path):
                        _download_results[video_id] = mp3_path
                        return mp3_path
                expected = filename if os.path.exists(filename) else None
                if not expected:
                    files = [(f, os.path.getmtime(os.path.join(folder, f)))
                             for f in os.listdir(folder) if os.path.isfile(os.path.join(folder, f))]
                    if files:
                        files.sort(key=lambda x: x[1], reverse=True)
                        expected = os.path.join(folder, files[0][0])
                _download_results[video_id] = expected
                return expected
        except Exception as e:
            err_msg = str(e)
            if "Sign in to confirm" in err_msg or "confirm you’re not a bot" in err_msg:
                print(f"  ⚠️  YouTube bot-check blocked this download.")
                if not has_auth:
                    print(f"      → Bypass mode failed. YouTube requires authentication for this video.")
                print(f"      → Try: select a browser with an active YouTube login,")
                print(f"      → Or: export cookies.txt from your browser and select it below.")
            else:
                print(f"  ❌ Download failed for {video_id}: {err_msg}")
            _download_results[video_id] = None
            return None


# === MAIN COMPILER ===
class AutoCompiler:
    def __init__(self, csv_path, output_folder=None,
                 resolution="1080p", bitrate_kbps=8000, fps=30,
                 download_missing=False, quality='Best', dry_run=False,
                 keep_temp=False, jobs=1, cookie_browser=None, cookie_file=None,
                 bypass_no_auth=True, crf=18, preset="slow"):
        self.csv_path = csv_path
        self.output_folder = output_folder or DEFAULT_OUTPUT_FOLDER
        self.resolution = resolution
        self.bitrate_kbps = bitrate_kbps
        self.fps = fps
        self.download_missing = download_missing
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
        self.stats = {'trimmed': 0, 'failed': 0, 'downloaded': 0}
        self.cookie_browser = cookie_browser
        self.cookie_file = cookie_file

        self.download_folder = os.path.dirname(self.output_folder)
        if self.download_folder == self.output_folder:
            self.download_folder = DEFAULT_DOWNLOAD_FOLDER

        os.makedirs(self.output_folder, exist_ok=True)
        os.makedirs(self.download_folder, exist_ok=True)

    def ensure_video_available(self, match_id):
        lookup_key = extract_video_id(match_id) if is_valid_youtube_url(match_id) else match_id

        cached = get_cached_path(self.cache, lookup_key, 'MP4')
        if cached:
            print(f"  💾 Using cached video: {os.path.basename(cached)}")
            return cached

        if self.download_missing and is_valid_youtube_url(match_id):
            print(f"  📥 Downloading missing video: {lookup_key}")
            downloaded = download_video(
                lookup_key, match_id, self.download_folder, 'MP4',
                self.quality, self.cookie_browser, self.cookie_file,
                bypass_no_auth=self.bypass_no_auth
            )
            if downloaded:
                add_to_cache(self.cache, lookup_key, 'MP4', downloaded)
                self.stats['downloaded'] += 1
                return downloaded
        return None

    def generate_clip(self, entry):
        """Generate a single trimmed clip. Returns clip path or None."""
        match_id = entry['match_id']
        video_path = self.ensure_video_available(match_id)

        if not video_path:
            print(f"  ❌ No video available for match: {match_id}")
            return None

        temp_folder = os.path.join(self.output_folder, '.temp')
        os.makedirs(temp_folder, exist_ok=True)

        safe_player = re.sub(r'[^\w\s-]', '', entry['player']).strip().replace(' ', '_')
        safe_action = re.sub(r'[^\w\s-]', '', entry['action']).strip().replace(' ', '_')
        start_sec = int(entry['start'])
        clip_name = f"{start_sec:05d}_{safe_player}_{safe_action}_{entry['start_str']}-{entry['end_str']}.mp4"
        clip_path = os.path.join(temp_folder, clip_name)

        if self.dry_run:
            print(f"  [DRY-RUN] Would trim: {entry['player']} | {entry['action']} | {entry['start_str']} → {entry['end_str']}")
            return clip_path

        print(f"  ✂️  {entry['player']} | {entry['action']} | {entry['start_str']} → {entry['end_str']}")

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
            print(f"  ❌ Failed: {e}")
            self.stats['failed'] += 1
            return None

    def compile_player(self, player_name, entries):
        """Compile all clips for a specific player into one video."""
        print(f"\n{'='*50}")
        print(f"🎬 Compiling: {player_name}")
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
            print(f"❌ No clips generated for {player_name}")
            return None

        safe_name = re.sub(r'[^\w\s-]', '', player_name).strip().replace(' ', '_')
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_name = f"{safe_name}_Compilation_{timestamp}.mp4"
        output_path = os.path.join(self.output_folder, output_name)

        if self.dry_run:
            print(f"\n[DRY-RUN] Would merge {len(clips)} clips into:\n  {output_path}")
            return output_path

        print(f"\n🔗 Merging {len(clips)} clips...")

        try:
            merge_clips(
                self.ffmpeg_path,
                clips,
                output_path,
                re_encode=False,
                crf=self.crf,
                preset=self.preset
            )
            size_mb = os.path.getsize(output_path) / (1024 * 1024)
            print(f"✅ Done! {output_path}")
            print(f"📦 Size: {size_mb:.1f} MB")
            return output_path
        except Exception as e:
            print(f"❌ Merge failed: {e}")
            return None

    def run(self):
        """Main entry point."""
        print(f"📖 Reading: {self.csv_path}")
        entries = read_csv_entries(self.csv_path)

        if not entries:
            print("❌ No valid entries found in CSV.")
            return {}

        print(f"✅ Found {len(entries)} clip entries")

        by_player = {}
        for e in entries:
            by_player.setdefault(e['player'], []).append(e)

        print(f"👥 Players: {', '.join(by_player.keys())}")

        results = {}
        for player, player_entries in by_player.items():
            output = self.compile_player(player, player_entries)
            if output:
                results[player] = output

        if not self.keep_temp:
            print(f"\n🧹 Cleaning up temp files...")
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
            print(f"\n💾 Kept temp files in: {os.path.join(self.output_folder, '.temp')}")

        print(f"\n{'='*50}")
        print("📊 SUMMARY")
        print(f"{'='*50}")
        for player, path in results.items():
            if self.dry_run:
                print(f"  [DRY-RUN] {player}: {path}")
            elif os.path.exists(path):
                size = os.path.getsize(path) / (1024 * 1024)
                print(f"  ✅ {player}: {size:.1f} MB → {path}")
            else:
                print(f"  ⚠️  {player}: file not found at {path}")
        print(f"\n  Trimmed: {self.stats['trimmed']}  |  Failed: {self.stats['failed']}  |  Downloaded: {self.stats['downloaded']}")

        return results


# === CLI INTERFACE ===
def main():
    parser = argparse.ArgumentParser(
        description='YFetch Auto-Compiler — high quality video compilations',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Default — high quality, auto-download if needed
  python -m src.auto_compiler tests/cape_verde.csv

  # Maximum quality (visually lossless, larger files)
  python -m src.auto_compiler tests/cape_verde.csv --crf 16 --preset slower

  # Smaller files (slightly lower quality)
  python -m src.auto_compiler tests/cape_verde.csv --crf 23 --preset medium

  # With auth for blocked videos
  python -m src.auto_compiler tests/cape_verde.csv --cookie-browser chrome
        """
    )
    parser.add_argument('csv', help='Path to CSV file with timestamps')
    parser.add_argument('-o', '--output', default=DEFAULT_OUTPUT_FOLDER,
                        help='Output folder (default: ~/Downloads/YFetch/Compilations)')
    parser.add_argument('-r', '--resolution', default='1080p',
                        choices=['1080p', '720p', '480p', '360p'],
                        help='Output resolution')
    parser.add_argument('-b', '--bitrate', type=int, default=8000,
                        help='Target video bitrate in kbps (default: 8000)')
    parser.add_argument('-f', '--fps', type=int, default=30,
                        help='Output frame rate (default: 30)')
    parser.add_argument('-p', '--player', default=None,
                        help='Compile only this specific player')
    parser.add_argument('--no-download', action='store_true',
                        help='Skip downloading — fail if video is not in cache')
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
        download_missing=not args.no_download,
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
            print(f"❌ Player '{args.player}' not found in CSV.")
            return
        compiler.compile_player(args.player, player_entries)
    else:
        compiler.run()


if __name__ == "__main__":
    main()
