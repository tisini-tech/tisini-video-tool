from video_tool.gdrive.parser import GDriveURLParser

# ═══════════════════════════════════════════════════════════════════
# STEP 2: DIRECT URL ACCESS TEST
# ═══════════════════════════════════════════════════════════════════

TEST_URL = "https://drive.google.com/file/d/1EWEvbuphFefjwdktAGi8TYK8nsKK-OY0/view?usp=sharing"

parser = GDriveURLParser()

print("=" * 60)
print("STEP 2: DIRECT URL ACCESS TEST")
print("=" * 60)

try:
    info = parser.get_video_info(TEST_URL)
    print(f"\n✓ Direct URL obtained: {info.direct_url[:70]}...")

    if info.size_bytes:
        print(f"✓ File size: {info.size_bytes / (1024*1024):.1f} MB")
    else:
        print("✓ File size: Unknown (will be determined during download)")

    print(f"✓ MIME type: {info.mime_type or 'Unknown'}")
    print(f"✓ Is streamable: {info.is_streamable}")
    print(f"✓ Title: {info.title or 'Unknown'}")

    if info.confirm_token:
        print("\n⚠️  Virus scan confirmation required")
        print(f"   Token: {info.confirm_token[:30]}...")
        print("   (This is normal for files > 100MB)")
    else:
        print("\n✓ No confirmation needed (small file)")

    print("\n✅ STEP 2 PASSED: Direct URL is accessible!")
    print("\nNext step: Run test_probe.py")

except Exception as e:
    print(f"\n❌ FAILED: {e}")
    print("\nPossible causes:")
    print("  • File is not publicly shared (open in incognito to verify)")
    print("  • URL is malformed")
    print("  • Google is rate-limiting")
    print("  • Network issue")
