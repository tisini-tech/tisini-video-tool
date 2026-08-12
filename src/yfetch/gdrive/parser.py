"""
GDrive Parser Module for YFetch
Handles Google Drive shared video links
"""
import re
import requests
import urllib.parse
from dataclasses import dataclass
from typing import Optional, Tuple
import json


@dataclass
class GDriveVideoInfo:
    """Parsed Google Drive video metadata"""
    file_id: str
    title: Optional[str] = None
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None
    duration_sec: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    direct_url: Optional[str] = None
    is_streamable: bool = False
    confirm_token: Optional[str] = None


class GDriveError(Exception):
    """Google Drive parsing/download error"""
    pass


class GDriveURLParser:
    """Parse Google Drive share URLs and extract direct download links"""

    # Regex patterns for various Drive URL formats
    PATTERNS = [
        r'drive\.google\.com/file/d/([a-zA-Z0-9_-]+)',           # /file/d/FILE_ID/view
        r'drive\.google\.com/open\?id=([a-zA-Z0-9_-]+)',       # /open?id=FILE_ID
        r'googledrive\.com/host/([a-zA-Z0-9_-]+)',               # deprecated hosting
        r'drive\.google\.com/uc\?.*?id=([a-zA-Z0-9_-]+)',      # /uc?id=FILE_ID
    ]

    # Direct download base URL
    DOWNLOAD_BASE = "https://drive.google.com/uc"

    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self.session = requests.Session()
        # Mimic a real browser to avoid blocks
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
        })

    def extract_file_id(self, url: str) -> Optional[str]:
        """Extract the file ID from any Google Drive URL format"""
        for pattern in self.PATTERNS:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        return None

    def is_valid_drive_url(self, url: str) -> bool:
        """Check if URL is a valid Google Drive share link"""
        return self.extract_file_id(url) is not None

    def _build_direct_url(self, file_id: str, confirm_token: Optional[str] = None) -> str:
        """Build the direct download URL"""
        params = {'export': 'download', 'id': file_id}
        if confirm_token:
            params['confirm'] = confirm_token
        return f"{self.DOWNLOAD_BASE}?{urllib.parse.urlencode(params)}"

    def _fetch_confirm_token(self, file_id: str) -> Tuple[Optional[str], dict]:
        """
        Fetch the confirmation token for large files.
        Google Drive shows a virus scan warning for files > 100MB or un-scannable files.
        Returns (token, response_headers)
        """
        url = self._build_direct_url(file_id)
        
        try:
            # First request - get the warning page or file
            resp = self.session.get(url, timeout=self.timeout, allow_redirects=True)
            headers = dict(resp.headers)
            content_type = headers.get('Content-Type', '')
            
            # If we got video data directly, no confirmation needed
            if 'video' in content_type or 'octet-stream' in content_type:
                return None, headers
            
            # If it's not HTML, something else is wrong
            if 'text/html' not in content_type:
                return None, headers
            
            # Read the full HTML
            html = resp.text
            
            # === Method 1: Check for warning cookies (most reliable for large files) ===
            for cookie_name, cookie_value in resp.cookies.items():
                if 'download_warning' in cookie_name:
                    return cookie_value, headers
            
            # === Method 2: Extract confirm token from form/input in HTML ===
            # Pattern: <input type="hidden" name="confirm" value="TOKEN">
            patterns = [
                r'<input[^>]*name=["\']confirm["\'][^>]*value=["\']([a-zA-Z0-9_-]+)["\']',
                r'<input[^>]*value=["\']([a-zA-Z0-9_-]+)["\'][^>]*name=["\']confirm["\']',
                r'name=["\']confirm["\']\s+value=["\']([a-zA-Z0-9_-]+)["\']',
                r'confirm=([a-zA-Z0-9_-]+)',  # in URLs
                r'download_warning_[0-9a-f]+=([0-9a-f]+)',  # cookie format in HTML
                r'action="[^"]*confirm=([a-zA-Z0-9_-]+)[^"]*"',  # form action URL
            ]
            for pattern in patterns:
                match = re.search(pattern, html)
                if match:
                    return match.group(1), headers
            
            # === Method 3: Look for "download" link in the warning page ===
            # Google sometimes embeds the token in a download button href
            href_match = re.search(
                r'href="(/uc\?[^"]*confirm=([a-zA-Z0-9_-]+)[^"]*)"',
                html
            )
            if href_match:
                return href_match.group(2), headers
            
            # === Method 4: Look for the form action with uc endpoint ===
            form_match = re.search(
                r'<form[^>]*action="(/uc[^"]*)"[^>]*>(.*?)</form>',
                html,
                re.DOTALL
            )
            if form_match:
                form_html = form_match.group(2)
                input_match = re.search(
                    r'<input[^>]*name=["\']confirm["\'][^>]*value=["\']([^"\']+)["\']',
                    form_html
                )
                if input_match:
                    return input_match.group(1), headers
            
            return None, headers
            
        except Exception as e:
            raise GDriveError(f"Failed to fetch confirmation token: {e}")

    def get_video_info(self, url: str) -> GDriveVideoInfo:
        """
        Get video metadata and direct download URL from a Drive share link.
        Handles both small files (direct download) and large files (confirmation).
        """
        file_id = self.extract_file_id(url)
        if not file_id:
            raise GDriveError(f"Could not extract file ID from URL: {url}")
        
        info = GDriveVideoInfo(file_id=file_id)
        
        # Step 1: Get confirmation token if needed
        confirm_token, headers = self._fetch_confirm_token(file_id)
        info.confirm_token = confirm_token
        
        # Step 2: Build the direct URL
        info.direct_url = self._build_direct_url(file_id, confirm_token)
        
        # Step 3: Verify the direct URL actually works
        try:
            # Use GET with stream=True for the actual check (HEAD sometimes fails)
            probe_resp = self.session.get(
                info.direct_url,
                stream=True,
                timeout=self.timeout,
                allow_redirects=True
            )
            probe_ct = probe_resp.headers.get('Content-Type', '')
            
            # If GET returns HTML, the confirm token didn't work
            if 'text/html' in probe_ct:
                # Read a bit to check for error messages
                chunk = next(probe_resp.iter_content(4096), b'').decode('utf-8', errors='ignore')
                lower_chunk = chunk.lower()
                
                if 'quota' in lower_chunk or 'too many users' in lower_chunk:
                    raise GDriveError("Google Drive quota exceeded. Too many downloads recently.")
                if 'virus' in lower_chunk:
                    raise GDriveError("Virus scan warning could not be bypassed. Token may be invalid.")
                if 'sign in' in lower_chunk or 'login' in lower_chunk:
                    raise GDriveError("File requires login. Not publicly shared.")
                
                raise GDriveError(
                    "Direct URL returns HTML instead of video. "
                    "File may not be publicly accessible or requires confirmation."
                )
            
            info.mime_type = probe_ct
            content_length = probe_resp.headers.get('Content-Length')
            if content_length:
                info.size_bytes = int(content_length)
            
            # Check if it's actually a video
            if 'video' in probe_ct or 'octet-stream' in probe_ct:
                info.is_streamable = True
            
            # Try to get filename from Content-Disposition
            cd = probe_resp.headers.get('Content-Disposition', '')
            filename_match = re.search(r'filename\*?=[\'"]?([^\'";]+)', cd)
            if filename_match:
                info.title = urllib.parse.unquote(filename_match.group(1))
            
            # Close the stream since we only wanted to check headers
            probe_resp.close()
            
        except GDriveError:
            raise
        except Exception as e:
            # Non-fatal: we still have the direct URL, but mark as not streamable
            info.is_streamable = False
        
        return info

    def get_stream_url(self, url: str) -> str:
        """Get a streamable URL (for ffmpeg/players)"""
        info = self.get_video_info(url)
        if not info.is_streamable and not info.direct_url:
            raise GDriveError("File is not streamable or accessible")
        return info.direct_url


# Convenience function
def is_drive_url(url: str) -> bool:
    """Check if a URL is a Google Drive link"""
    return GDriveURLParser().is_valid_drive_url(url)


def parse_drive_url(url: str) -> GDriveVideoInfo:
    """Quick parse a Drive URL"""
    parser = GDriveURLParser()
    return parser.get_video_info(url)


def get_drive_stream_url(url: str) -> str:
    """Get streamable URL from Drive link"""
    return GDriveURLParser().get_stream_url(url)