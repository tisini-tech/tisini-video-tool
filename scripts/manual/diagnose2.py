import requests
import re
import socket

FILE_ID = "1EWEvbuphFefjwdktAGi8TYK8nsKK-OY0"
url = f"https://drive.google.com/uc?export=download&id={FILE_ID}"

print("=" * 60)
print("DIAGNOSTIC: Google Drive Response")
print("=" * 60)
print(f"URL: {url}")
print()

# Set socket timeout globally so nothing hangs
socket.setdefaulttimeout(10)

session = requests.Session()
session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
})

try:
    print("Sending GET request (timeout=10s)...")
    resp = session.get(url, allow_redirects=True, timeout=10)
    
    print(f"Status Code: {resp.status_code}")
    print(f"Final URL: {resp.url[:100]}")
    print(f"Content-Type: {resp.headers.get('Content-Type', 'N/A')}")
    print(f"Content-Length: {resp.headers.get('Content-Length', 'N/A')}")
    print()
    
    print("Cookies:")
    cookies = dict(resp.cookies)
    if cookies:
        for k, v in cookies.items():
            print(f"  {k}: {v[:60]}...")
    else:
        print("  (none)")
    print()
    
    # Check if we got HTML
    ct = resp.headers.get('Content-Type', '')
    if 'text/html' in ct:
        print("Got HTML page. Looking for confirm token...")
        html = resp.text
        
        # Show first 2000 chars
        print("\n--- First 2000 chars of HTML ---")
        print(html[:2000])
        print("--- End preview ---")
        print()
        
        # Try all patterns
        patterns = [
            (r'<input[^>]*name=["\']confirm["\'][^>]*value=["\']([a-zA-Z0-9_-]+)["\']', "input confirm value"),
            (r'name=["\']confirm["\']\s+value=["\']([a-zA-Z0-9_-]+)["\']', "confirm attribute"),
            (r'confirm=([a-zA-Z0-9_-]+)', "confirm= in URL"),
            (r'download_warning_[0-9a-f]+=([0-9a-f]+)', "download_warning cookie"),
            (r'action="[^"]*confirm=([a-zA-Z0-9_-]+)[^"]*"', "form action confirm"),
            (r'href="(/uc\?[^"]*confirm=([a-zA-Z0-9_-]+)[^"]*)"', "href confirm"),
        ]
        
        found = False
        for pattern, name in patterns:
            match = re.search(pattern, html)
            if match:
                print(f"✅ FOUND token via '{name}': {match.group(1)[:50]}...")
                found = True
                break
        
        if not found:
            print("❌ No confirm token found with any pattern")
            # Show any 'confirm' occurrences
            confirms = re.findall(r'.{0,50}confirm.{0,50}', html)
            if confirms:
                print("\nAll 'confirm' occurrences in HTML:")
                for c in confirms[:5]:
                    print(f"  ...{c}...")
    
    elif 'video' in ct or 'octet-stream' in ct:
        print("✅ Got video data directly!")
    else:
        print(f"? Got unexpected content type: {ct}")

except requests.exceptions.Timeout:
    print("❌ REQUEST TIMED OUT")
except requests.exceptions.ConnectionError as e:
    print(f"❌ CONNECTION ERROR: {e}")
except Exception as e:
    print(f"❌ ERROR: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()

print()
print("=" * 60)