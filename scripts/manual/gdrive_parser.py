from video_tool.gdrive.parser import GDriveURLParser, is_drive_url

# ═══════════════════════════════════════════════════════════════════
# STEP 1: URL PARSING TEST
# ═══════════════════════════════════════════════════════════════════

# REPLACE THIS with your actual Google Drive share link
TEST_URL = "https://drive.google.com/file/d/1EWEvbuphFefjwdktAGi8TYK8nsKK-OY0/view?usp=sharing"

parser = GDriveURLParser()

print("=" * 60)
print("STEP 1: URL PARSING TEST")
print("=" * 60)
print(f"\nInput URL: {TEST_URL}")

# Test 1: Is it recognized as Drive?
is_gd = is_drive_url(TEST_URL)
print(f"\n✓ Is Drive URL: {is_gd}")
if not is_gd:
    print("  ❌ FAILED: URL not recognized as Google Drive")
    print("  Make sure your URL looks like: drive.google.com/file/d/.../view")
    exit(1)

# Test 2: Extract file ID
file_id = parser.extract_file_id(TEST_URL)
print(f"✓ File ID: {file_id}")
if not file_id:
    print("  ❌ FAILED: Could not extract file ID")
    exit(1)

# Test 3: Build direct URL
direct = parser._build_direct_url(file_id)
print(f"✓ Direct URL: {direct}")

print("\n✅ STEP 1 PASSED: URL parsing works!")
print("\nNext step: Run test_direct_url.py")
