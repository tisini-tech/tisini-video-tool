import requests
import re
import sys

FILE_ID = "1IoTfwAaEYL3UH6ImbNH1JNW69zL6_l4F"
URL = f"https://drive.google.com/uc?export=download&id={FILE_ID}"

print("=" * 60)
print("DIAGNOSTIC: Testing Google Drive response")
print("=" * 60)
print(f"URL: {URL}")
print()

try:
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    })
    
    print("Step 1: Sending GET request (timeout=15s)...")
    resp = session.get(URL, allow_redirects=True, timeout=15)
    
    print(f"Status Code: {resp.status_code}")
    print(f"Final URL: {resp.url[:80]}...")
    print(f"Content-Type: {resp.headers.get('Content-Type', 'N/A')}")
    print(f"Content-Length: {resp.headers.get('Content-Length', 'N/A')}")
    print()
    
    print("Cookies received:")
    for name, value in resp.cookies.items():
        print(f"  {name}: {value[:50]}...")
    
    print()
    print("--- First 1000 characters of response ---")
    text = resp.text[:1000]
    print(text)
    print("--- End preview ---")
    print()
    
    # Check for common error patterns
    lower_text = text.lower()
    if "quota" in lower_text:
        print("❌ DETECTED: Google Drive quota exceeded")
    elif "virus" in lower_text:
        print("⚠️ DETECTED: Virus scan warning page")
    elif "too many users" in lower_text:
        print("❌ DETECTED: Too many users have viewed/downloaded this file")
    elif "sign in" in lower_text or "login" in lower_text:
        print("❌ DETECTED: File requires login (not publicly shared)")
    elif resp.headers.get('Content-Type', '').startswith('video'):
        print("✅ DETECTED: Video data returned directly!")
    else:
        print("? Unknown response type")
        
except requests.exceptions.Timeout:
    print("❌ REQUEST TIMED OUT after 15 seconds")
    print("   Google may be blocking or throttling your IP.")
except Exception as e:
    print(f"❌ ERROR: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()

print()
print("=" * 60)