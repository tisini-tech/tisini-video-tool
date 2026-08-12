import subprocess
import json
from yfetch.gdrive.parser import get_drive_stream_url

# ═══════════════════════════════════════════════════════════════════
# STEP 3: VIDEO PROBE TEST
# ═══════════════════════════════════════════════════════════════════

TEST_URL = "https://drive.google.com/file/d/1EWEvbuphFefjwdktAGi8TYK8nsKK-OY0/view?usp=sharing"

print("=" * 60)
print("STEP 3: VIDEO PROBE TEST")
print("=" * 60)

try:
    stream_url = get_drive_stream_url(TEST_URL)
    print(f"\nStream URL: {stream_url[:60]}...")

    cmd = [
        'ffprobe', '-v', 'error',
        '-show_entries', 'format=duration,bit_rate:stream=width,height,codec_name,r_frame_rate',
        '-of', 'json',
        stream_url
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"\n❌ ffprobe failed:")
        print(f"   {result.stderr[:300]}")
        exit(1)

    data = json.loads(result.stdout)
    fmt = data['format']
    video = [s for s in data['streams'] if s['codec_type'] == 'video'][0]

    print(f"\n✓ Duration: {float(fmt['duration']):.1f} seconds")
    print(f"✓ Resolution: {video['width']}x{video['height']}")
    print(f"✓ Codec: {video['codec_name']}")
    print(f"✓ Frame rate: {video['r_frame_rate']}")
    print(f"✓ Bitrate: {int(fmt['bit_rate'])/1000:.0f} kbps")

    print("\n✅ STEP 3 PASSED: ffmpeg can read the video!")
    print("\nNext step: Run test_download.py")

except Exception as e:
    print(f"\n❌ FAILED: {e}")
