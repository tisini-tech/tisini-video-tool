from yfetch.gdrive.trimmer import GDriveTrimmer

# ═══════════════════════════════════════════════════════════════════
# STEP 5: TRIM TESTS
# ═══════════════════════════════════════════════════════════════════

TEST_URL = "https://drive.google.com/file/d/1IoTfwAaEYL3UH6ImbNH1JNW69zL6_l4F/view?usp=sharing"

print("=" * 60)
print("STEP 5: TRIM TESTS")
print("=" * 60)

trimmer = GDriveTrimmer(output_dir="./downloads")

# Test 5A: Fast trim
print("\n--- Test 5A: Fast Trim (0s to 10s) ---")
result = trimmer.trim(TEST_URL, start_time=0, end_time=10,
                      mode='fast', output_filename="test_fast.mp4")
if result.success:
    print(f"✅ FAST TRIM OK")
    print(f"   Size: {result.output_size_mb:.2f} MB")
    print(f"   Stream copy: {result.used_stream_copy}")
    print(f"   Duration: {result.trimmed_duration:.1f}s")
else:
    print(f"❌ Failed: {result.error_message}")

# Test 5B: Accurate trim
print("\n--- Test 5B: Accurate Trim (30s to 45s) ---")
result = trimmer.trim(TEST_URL, start_time=30, end_time=45,
                      mode='accurate', output_filename="test_accurate.mp4", crf=18)
if result.success:
    print(f"✅ ACCURATE TRIM OK")
    print(f"   Size: {result.output_size_mb:.2f} MB")
    print(f"   Stream copy: {result.used_stream_copy}")
    print(f"   Duration: {result.trimmed_duration:.1f}s")
else:
    print(f"❌ Failed: {result.error_message}")

# Test 5C: Auto mode
print("\n--- Test 5C: Auto Trim (60s to 90s) ---")
result = trimmer.trim(TEST_URL, start_time=60, end_time=90,
                      mode='auto', output_filename="test_auto.mp4")
if result.success:
    print(f"✅ AUTO TRIM OK")
    print(f"   Size: {result.output_size_mb:.2f} MB")
    print(f"   Stream copy: {result.used_stream_copy}")
    print(f"   Duration: {result.trimmed_duration:.1f}s")
    if result.used_stream_copy:
        print("   → Used stream copy (fast, no quality loss)")
    else:
        print("   → Used re-encode (frame-accurate)")
else:
    print(f"❌ Failed: {result.error_message}")

print("\n✅ ALL TRIM TESTS COMPLETE!")
