
"""Google Drive source parsing and direct-stream URL resolution.

This module handles Google Drive shared video links.

Responsibilities:

    - Parse supported Google Drive URL formats.
    - Extract Drive file IDs.
    - Resolve confirmation tokens for large files when required.
    - Build direct download/stream URLs.
    - Verify that the resolved URL is accessible.
    - Return lightweight metadata about the remote file.

Actual media processing remains in the core processing module.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass

import requests

from ..errors import VideoToolError

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class GDriveVideoInfo:
    """Parsed Google Drive video metadata."""

    file_id: str
    title: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    duration_sec: float | None = None
    width: int | None = None
    height: int | None = None
    direct_url: str | None = None
    is_streamable: bool = False
    confirm_token: str | None = None


class GDriveError(VideoToolError):
    """Google Drive parsing or access error.

    Messages are written for people who are not technical.
    """


# ---------------------------------------------------------------------------
# Google Drive URL parser
# ---------------------------------------------------------------------------


class GDriveURLParser:
    """Parse Google Drive URLs and resolve direct stream URLs."""

    # Supported Google Drive URL formats.
    PATTERNS = (
        re.compile(
            r"drive\.google\.com/file/d/([A-Za-z0-9_-]+)",
            re.IGNORECASE,
        ),
        re.compile(
            r"drive\.google\.com/open\?id=([A-Za-z0-9_-]+)",
            re.IGNORECASE,
        ),
        re.compile(
            r"googledrive\.com/host/([A-Za-z0-9_-]+)",
            re.IGNORECASE,
        ),
        re.compile(
            r"drive\.google\.com/uc\?(?:[^#]*&)?id=([A-Za-z0-9_-]+)",
            re.IGNORECASE,
        ),
    )

    # Direct download endpoint.
    DOWNLOAD_BASE = "https://drive.google.com/uc"

    # Filename from Content-Disposition.
    _FILENAME_RE = re.compile(
        r"""filename\*?=(?:UTF-8''|["']?)([^"';\r\n]+)""",
        re.IGNORECASE,
    )

    # Common confirmation-token patterns returned by Google Drive.
    _CONFIRM_PATTERNS = (
        re.compile(
            r"""<input[^>]*name=["']confirm["'][^>]*value=["']([A-Za-z0-9_-]+)["']""",
            re.IGNORECASE,
        ),
        re.compile(
            r"""<input[^>]*value=["']([A-Za-z0-9_-]+)["'][^>]*name=["']confirm["']""",
            re.IGNORECASE,
        ),
        re.compile(
            r"""name=["']confirm["']\s+value=["']([A-Za-z0-9_-]+)["']""",
            re.IGNORECASE,
        ),
        re.compile(
            r"""confirm=([A-Za-z0-9_-]+)""",
            re.IGNORECASE,
        ),
        re.compile(
            r"""download_warning_[0-9a-f]+=([0-9a-f]+)""",
            re.IGNORECASE,
        ),
        re.compile(
            r"""action="[^"]*confirm=([A-Za-z0-9_-]+)[^"]*""" ,
            re.IGNORECASE,
        ),
    )

    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self.session = requests.Session()

        # Browser-like headers help avoid unnecessary Drive blocking.
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": (
                    "text/html,application/xhtml+xml,"
                    "application/xml;q=0.9,image/webp,*/*;q=0.8"
                ),
                "Accept-Language": "en-US,en;q=0.5",
            }
        )

    # ------------------------------------------------------------------
    # URL parsing
    # ------------------------------------------------------------------

    def extract_file_id(self, url: str) -> str | None:
        """Extract the Drive file ID from a supported URL."""

        for pattern in self.PATTERNS:
            match = pattern.search(url)

            if match:
                return match.group(1)

        return None

    def is_valid_drive_url(self, url: str) -> bool:
        """Return True when ``url`` contains a supported Drive file ID."""

        return self.extract_file_id(url) is not None

    # ------------------------------------------------------------------
    # Direct URL construction
    # ------------------------------------------------------------------

    def _build_direct_url(
        self,
        file_id: str,
        confirm_token: str | None = None,
    ) -> str:
        """Build a Google Drive direct download URL."""

        params = {
            "export": "download",
            "id": file_id,
        }

        if confirm_token:
            params["confirm"] = confirm_token

        query = urllib.parse.urlencode(params)

        return f"{self.DOWNLOAD_BASE}?{query}"

    # ------------------------------------------------------------------
    # Confirmation handling
    # ------------------------------------------------------------------

    def _fetch_confirm_token(
        self,
        file_id: str,
    ) -> tuple[str | None, dict[str, str]]:
        """Fetch a confirmation token when Drive requires one.

        Google Drive may show a confirmation page for large or
        unscannable files.

        Returns:
            ``(token, response_headers)``.
        """

        url = self._build_direct_url(file_id)

        try:
            response = self.session.get(
                url,
                timeout=self.timeout,
                allow_redirects=True,
            )

            headers = dict(response.headers)
            content_type = headers.get("Content-Type", "").lower()

            # A direct media response means no confirmation is required.
            if "video" in content_type or "octet-stream" in content_type:
                return None, headers

            # Anything other than HTML cannot be interpreted as a
            # confirmation page.
            if "text/html" not in content_type:
                return None, headers

            html = response.text

            # ----------------------------------------------------------
            # Method 1: confirmation cookie
            # ----------------------------------------------------------

            for cookie_name, cookie_value in response.cookies.items():
                if "download_warning" in cookie_name:
                    return cookie_value, headers

            # ----------------------------------------------------------
            # Method 2: hidden form/input fields and URLs
            # ----------------------------------------------------------

            for pattern in self._CONFIRM_PATTERNS:
                match = pattern.search(html)

                if match:
                    return match.group(1), headers

            # ----------------------------------------------------------
            # Method 3: download link containing the token
            # ----------------------------------------------------------

            href_match = re.search(
                r"""href="(/uc\?[^"]*confirm=([A-Za-z0-9_-]+)[^"]*)""" ,
                html,
                re.IGNORECASE,
            )

            if href_match:
                return href_match.group(2), headers

            # ----------------------------------------------------------
            # Method 4: form action containing confirmation data
            # ----------------------------------------------------------

            form_match = re.search(
                r"""<form[^>]*action="(/uc[^"]*)"[^>]*>(.*?)</form>""",
                html,
                re.IGNORECASE | re.DOTALL,
            )

            if form_match:
                form_html = form_match.group(2)

                input_match = re.search(
                    r"""<input[^>]*name=["']confirm["'][^>]*value=["']([^"']+)["']""",
                    form_html,
                    re.IGNORECASE,
                )

                if input_match:
                    return input_match.group(1), headers

            return None, headers

        except requests.RequestException as exc:
            raise GDriveError(
                "Google Drive did not let Video Tool start the "
                "download. Try again in a while.",
                detail=str(exc),
            ) from exc

    # ------------------------------------------------------------------
    # Metadata / stream resolution
    # ------------------------------------------------------------------

    def get_video_info(self, url: str) -> GDriveVideoInfo:
        """Resolve a Drive URL into a usable direct stream URL.

        Handles both ordinary files and files requiring a confirmation
        token before download.
        """

        file_id = self.extract_file_id(url)

        if not file_id:
            raise GDriveError(
                f"This does not look like a Google Drive video link: {url}"
            )

        info = GDriveVideoInfo(file_id=file_id)

        # Step 1: determine whether Drive requires a confirmation token.
        confirm_token, _headers = self._fetch_confirm_token(file_id)

        info.confirm_token = confirm_token

        # Step 2: build the direct URL.
        info.direct_url = self._build_direct_url(
            file_id,
            confirm_token,
        )

        # Step 3: verify that the URL actually resolves to media.
        probe_response = None

        try:
            probe_response = self.session.get(
                info.direct_url,
                stream=True,
                timeout=self.timeout,
                allow_redirects=True,
            )

            content_type = probe_response.headers.get(
                "Content-Type",
                "",
            )

            content_type_lower = content_type.lower()

            # HTML usually means that the file is inaccessible,
            # confirmation failed, or Google returned an error page.
            if "text/html" in content_type_lower:
                chunk = next(
                    probe_response.iter_content(4096),
                    b"",
                )

                lower_chunk = chunk.decode(
                    "utf-8",
                    errors="ignore",
                ).lower()

                if "quota" in lower_chunk or "too many users" in lower_chunk:
                    raise GDriveError(
                        "Google Drive has paused downloads of this file "
                        "because too many people downloaded it. "
                        "Try again tomorrow."
                    )

                if "virus" in lower_chunk:
                    raise GDriveError(
                        "Google Drive showed a safety warning that Video "
                        "Tool could not get past. Try again later."
                    )

                if (
                    "sign in" in lower_chunk
                    or "login" in lower_chunk
                ):
                    raise GDriveError(
                        "This Google Drive file is not shared with "
                        "everyone who has the link. Ask the owner to "
                        "change the sharing setting."
                    )

                raise GDriveError(
                    "Google Drive sent a web page instead of the video. "
                    "The file may not be shared with everyone who has "
                    "the link."
                )

            info.mime_type = content_type

            content_length = probe_response.headers.get(
                "Content-Length"
            )

            if content_length:
                try:
                    info.size_bytes = int(content_length)
                except ValueError:
                    info.size_bytes = None

            # A video or generic binary response is usable by the
            # downstream media-processing pipeline.
            if (
                "video" in content_type_lower
                or "octet-stream" in content_type_lower
            ):
                info.is_streamable = True

            # Try to recover the original filename.
            content_disposition = probe_response.headers.get(
                "Content-Disposition",
                "",
            )

            filename_match = self._FILENAME_RE.search(
                content_disposition
            )

            if filename_match:
                info.title = urllib.parse.unquote(
                    filename_match.group(1)
                )

        except GDriveError:
            raise

        except requests.RequestException:
            # The direct URL has still been constructed. Treat a failed
            # verification as non-fatal so callers can decide whether
            # to attempt the stream themselves.
            info.is_streamable = False

        finally:
            if probe_response is not None:
                probe_response.close()

        return info

    def get_stream_url(self, url: str) -> str:
        """Return a resolved Google Drive URL suitable for media tools."""

        info = self.get_video_info(url)

        if not info.direct_url:
            raise GDriveError(
                "Google Drive will not let Video Tool open this file. "
                "Check that it is shared with everyone who has the link."
            )

        return info.direct_url


# ---------------------------------------------------------------------------
# Convenience functions
# ---------------------------------------------------------------------------


def is_drive_url(url: str) -> bool:
    """Return True when ``url`` is a supported Google Drive URL."""

    return GDriveURLParser().is_valid_drive_url(url)


def parse_drive_url(url: str) -> GDriveVideoInfo:
    """Parse a Drive URL and return its resolved video information."""

    parser = GDriveURLParser()

    return parser.get_video_info(url)


def get_drive_stream_url(url: str) -> str:
    """Return a resolved stream URL from a Google Drive link."""

    return GDriveURLParser().get_stream_url(url)