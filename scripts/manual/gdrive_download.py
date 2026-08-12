from yfetch.gdrive.trimmer import download_drive_video

# ═══════════════════════════════════════════════════════════════════
# STEP 4: FULL DOWNLOAD TEST
# ═══════════════════════════════════════════════════════════════════

TEST_URL = "https://drive.google.com/file/d/1IoTfwAaEYL3UH6ImbNH1JNW69zL6_l4F/view?usp=sharing"

print("=" * 60)
print("STEP 4: FULL DOWNLOAD TEST")
print("=" * 60)
print("\nThis will download the ENTIRE video.")
print("Press Ctrl+C to cancel if the file is large.\n")

result = download_drive_video(TEST_URL, output_filename="test_full.mp4")

if result.success:
    print(f"\n✅ DOWNLOAD SUCCESS!")
    print(f"   File: {result.output_path}")
    print(f"   Size: {result.output_size_mb:.2f} MB")
    print(f"   Resolution: {result.original_resolution}")
    print(f"   Duration: {result.original_duration:.1f}s")
    print(f"   Stream copy used: {result.used_stream_copy}")
    print("\nNext step: Run test_trim.py")
else:
    print(f"\n❌ DOWNLOAD FAILED: {result.error_message}")
