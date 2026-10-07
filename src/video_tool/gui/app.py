"""Video Tool GUI.

A polished CustomTkinter interface over the shared Basic download pipeline.

The GUI deliberately contains no download logic of its own. It delegates
source fetching, caching, YouTube handling, and download counting to the
shared Video Tool modules.

Downloads run in a background thread so the window remains responsive.
"""

from __future__ import annotations

import logging
import queue
import random
import shutil
import threading
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk

from ..clip_source import fetch_clip
from ..core import load_download_count, save_download_count
from ..errors import explain
from ..notify import Notifier
from ..youtube import DownloadOptions
from .bundled_deps import ensure_all
from .toast import ToastHost
from ..versions.common_cli import clean_url

# ---------------------------------------------------------------------------
# Appearance
# ---------------------------------------------------------------------------

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

BASE_FONT_SIZE = 26

WINDOW_WIDTH = 720
WINDOW_HEIGHT = 720

MILESTONES = {
    10,
    25,
    50,
    100,
    250,
    500,
    1000,
}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)


class VideoToolGUI(ctk.CTk):
    """Main Video Tool graphical interface."""

    def __init__(self) -> None:
        super().__init__()

        self.title("Video Tool")
        self.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}")
        self.minsize(640, 650)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Make the CustomTkinter default font more readable.
        try:
            ctk.ThemeManager.theme["CTkFont"]["size"] = BASE_FONT_SIZE
        except Exception:
            pass

        self._ui_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self._download_thread: threading.Thread | None = None
        self._closing = False

        self._download_count = load_download_count()
        self._ffmpeg_path: str | None = None

        self._build_ui()
        self._update_counter()

        # Toasts show messages over the window. Any thread may send one
        # through self.notify; it is drawn on the main thread.
        self.toasts = ToastHost(self)
        self.notify = Notifier(self._notify_from_any_thread)

        self.after(100, self._poll_queue)
        self.after(100, self._check_dependencies)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        """Build the complete GUI."""

        outer = ctk.CTkFrame(
            self,
            fg_color="transparent",
        )
        outer.pack(
            fill="both",
            expand=True,
            padx=24,
            pady=20,
        )

        # --------------------------------------------------------------
        # Header
        # --------------------------------------------------------------

        header = ctk.CTkFrame(
            outer,
            fg_color="transparent",
        )
        header.pack(
            fill="x",
            pady=(0, 18),
        )

        # Left side of header.
        header_text = ctk.CTkFrame(
            header,
            fg_color="transparent",
        )
        header_text.pack(
            side="left",
            fill="x",
            expand=True,
        )

        title = ctk.CTkLabel(
            header_text,
            text="Video Tool",
            font=ctk.CTkFont(
                family="Segoe UI",
                size=32,
                weight="bold",
            ),
        )
        title.pack(anchor="w")

        subtitle = ctk.CTkLabel(
            header_text,
            text="Download YouTube videos and audio with ease.",
            font=ctk.CTkFont(
                family="Segoe UI",
                size=15,
            ),
            text_color=("gray45", "gray70"),
        )
        subtitle.pack(
            anchor="w",
            pady=(3, 0),
        )

        # Help intentionally remains at the TOP RIGHT.
        self.help_button = ctk.CTkButton(
            header,
            text="?  Help",
            width=100,
            height=38,
            corner_radius=9,
            fg_color="gray30",
            hover_color="gray25",
            font=ctk.CTkFont(
                family="Segoe UI",
                size=14,
                weight="bold",
            ),
            command=self._show_help,
        )
        self.help_button.pack(
            side="right",
            anchor="n",
            padx=(12, 0),
        )

        # --------------------------------------------------------------
        # URL card
        # --------------------------------------------------------------

        url_card = ctk.CTkFrame(
            outer,
            corner_radius=14,
        )
        url_card.pack(
            fill="x",
            pady=(0, 12),
        )

        url_label = ctk.CTkLabel(
            url_card,
            text="YouTube URL",
            font=ctk.CTkFont(
                family="Segoe UI",
                size=15,
                weight="bold",
            ),
        )
        url_label.pack(
            anchor="w",
            padx=18,
            pady=(16, 7),
        )

        self.url_text = ctk.CTkTextbox(
            url_card,
            height=72,
            corner_radius=10,
            font=ctk.CTkFont(
                family="Segoe UI",
                size=16,
            ),
            wrap="word",
        )
        self.url_text.pack(
            fill="x",
            padx=18,
            pady=(0, 17),
        )

        # --------------------------------------------------------------
        # Options card
        # --------------------------------------------------------------

        options_card = ctk.CTkFrame(
            outer,
            corner_radius=14,
        )
        options_card.pack(
            fill="x",
            pady=(0, 12),
        )

        options_title = ctk.CTkLabel(
            options_card,
            text="Download options",
            font=ctk.CTkFont(
                family="Segoe UI",
                size=15,
                weight="bold",
            ),
        )
        options_title.pack(
            anchor="w",
            padx=18,
            pady=(16, 12),
        )

        options_row = ctk.CTkFrame(
            options_card,
            fg_color="transparent",
        )
        options_row.pack(
            fill="x",
            padx=18,
            pady=(0, 17),
        )

        # Format
        format_frame = ctk.CTkFrame(
            options_row,
            fg_color="transparent",
        )
        format_frame.pack(
            side="left",
            fill="x",
            expand=True,
            padx=(0, 8),
        )

        format_label = ctk.CTkLabel(
            format_frame,
            text="Format",
            font=ctk.CTkFont(
                family="Segoe UI",
                size=14,
            ),
        )
        format_label.pack(
            anchor="w",
            pady=(0, 6),
        )

        self.format_var = ctk.StringVar(
            value="MP4"
        )

        self.format_menu = ctk.CTkOptionMenu(
            format_frame,
            variable=self.format_var,
            values=["MP4", "MP3"],
            height=40,
            corner_radius=9,
            font=ctk.CTkFont(
                family="Segoe UI",
                size=16,
            ),
        )
        self.format_menu.pack(
            fill="x"
        )

        # Quality
        quality_frame = ctk.CTkFrame(
            options_row,
            fg_color="transparent",
        )
        quality_frame.pack(
            side="left",
            fill="x",
            expand=True,
            padx=(8, 0),
        )

        quality_label = ctk.CTkLabel(
            quality_frame,
            text="Quality",
            font=ctk.CTkFont(
                family="Segoe UI",
                size=14,
            ),
        )
        quality_label.pack(
            anchor="w",
            pady=(0, 6),
        )

        self.quality_var = ctk.StringVar(
            value="Best"
        )

        self.quality_menu = ctk.CTkOptionMenu(
            quality_frame,
            variable=self.quality_var,
            values=[
                "Best",
                "1080p",
                "720p",
                "480p",
                "360p",
            ],
            height=40,
            corner_radius=9,
            font=ctk.CTkFont(
                family="Segoe UI",
                size=16,
            ),
        )
        self.quality_menu.pack(
            fill="x"
        )

        # --------------------------------------------------------------
        # Output card
        # --------------------------------------------------------------

        output_card = ctk.CTkFrame(
            outer,
            corner_radius=14,
        )
        output_card.pack(
            fill="x",
            pady=(0, 14),
        )

        output_label = ctk.CTkLabel(
            output_card,
            text="Save location",
            font=ctk.CTkFont(
                family="Segoe UI",
                size=15,
                weight="bold",
            ),
        )
        output_label.pack(
            anchor="w",
            padx=18,
            pady=(16, 8),
        )

        output_row = ctk.CTkFrame(
            output_card,
            fg_color="transparent",
        )
        output_row.pack(
            fill="x",
            padx=18,
            pady=(0, 17),
        )

        default_output = str(
            Path.home() / "Downloads" / "Video-Tool"
        )

        self.output_var = ctk.StringVar(
            value=default_output
        )

        self.output_entry = ctk.CTkEntry(
            output_row,
            textvariable=self.output_var,
            height=40,
            corner_radius=9,
            font=ctk.CTkFont(
                family="Segoe UI",
                size=15,
            ),
        )
        self.output_entry.pack(
            side="left",
            fill="x",
            expand=True,
            padx=(0, 8),
        )

        self.browse_button = ctk.CTkButton(
            output_row,
            text="Browse",
            width=100,
            height=40,
            corner_radius=9,
            font=ctk.CTkFont(
                family="Segoe UI",
                size=15,
                weight="bold",
            ),
            command=self._browse_output,
        )
        self.browse_button.pack(
            side="right"
        )

        # --------------------------------------------------------------
        # Download button
        # --------------------------------------------------------------

        self.download_button = ctk.CTkButton(
            outer,
            text="Download",
            height=50,
            corner_radius=11,
            font=ctk.CTkFont(
                family="Segoe UI",
                size=19,
                weight="bold",
            ),
            command=self._start_download,
        )
        self.download_button.pack(
            fill="x",
            pady=(0, 10),
        )

        # --------------------------------------------------------------
        # Progress
        # --------------------------------------------------------------

        self.progress = ctk.CTkProgressBar(
            outer,
            height=10,
            corner_radius=5,
        )
        self.progress.pack(
            fill="x",
            pady=(0, 8),
        )
        self.progress.set(0)

        # --------------------------------------------------------------
        # Status
        # --------------------------------------------------------------

        self.status_var = ctk.StringVar(
            value="Ready to download."
        )

        self.status_label = ctk.CTkLabel(
            outer,
            textvariable=self.status_var,
            font=ctk.CTkFont(
                family="Segoe UI",
                size=14,
            ),
            text_color=("gray35", "gray70"),
        )
        self.status_label.pack(
            pady=(0, 14)
        )

        # --------------------------------------------------------------
        # Download history
        #
        # This is intentionally the LOWEST section on the page.
        # Help remains in the header above.
        # --------------------------------------------------------------

        history_card = ctk.CTkFrame(
            outer,
            corner_radius=14,
        )
        history_card.pack(
            fill="x",
            pady=(0, 0),
        )

        history_left = ctk.CTkFrame(
            history_card,
            fg_color="transparent",
        )
        history_left.pack(
            side="left",
            fill="x",
            expand=True,
            padx=18,
            pady=13,
        )

        history_title = ctk.CTkLabel(
            history_left,
            text="Download history",
            font=ctk.CTkFont(
                family="Segoe UI",
                size=14,
                weight="bold",
            ),
        )
        history_title.pack(
            anchor="w"
        )

        history_subtitle = ctk.CTkLabel(
            history_left,
            text="Your completed downloads on this device",
            font=ctk.CTkFont(
                family="Segoe UI",
                size=12,
            ),
            text_color=("gray50", "gray65"),
        )
        history_subtitle.pack(
            anchor="w",
            pady=(2, 0),
        )

        self.counter_var = ctk.StringVar(
            value="0 downloads"
        )

        self.counter_label = ctk.CTkLabel(
            history_card,
            textvariable=self.counter_var,
            font=ctk.CTkFont(
                family="Segoe UI",
                size=18,
                weight="bold",
            ),
        )
        self.counter_label.pack(
            side="right",
            padx=18,
            pady=13,
        )

    # ------------------------------------------------------------------
    # Dependency checks
    # ------------------------------------------------------------------

    def _check_dependencies(self) -> None:
        """Set up the helper tools without freezing the window."""

        self._set_busy(True)
        self.status_var.set("Checking the tools Video Tool needs...")

        threading.Thread(
            target=self._dependency_worker,
            daemon=True,
        ).start()

    def _dependency_worker(self) -> None:
        """Run the slow tool setup away from the main thread."""

        def progress(message: str) -> None:
            self._ui_queue.put(("status", message))

        try:
            result = ensure_all(progress)
        except Exception:
            logger.exception("Dependency check failed")
            result = None

        self._ui_queue.put(("deps_done", result))

    def _finish_dependency_check(self, result: object) -> None:
        """Use what the setup found, and say so if FFmpeg is missing."""

        self._set_busy(False)

        if isinstance(result, dict) and result.get("ffmpeg"):
            self._ffmpeg_path = str(result["ffmpeg"])

        if not self._ffmpeg_path:
            self._ffmpeg_path = shutil.which("ffmpeg")

        if result is None:
            self.status_var.set("Ready to download.")
            self.toasts.show(
                "warning",
                "Could not check the tools\n"
                "Downloads may still work. If one fails, restart "
                "Video Tool.",
            )
        elif self._ffmpeg_path:
            self.status_var.set("Ready to download.")
        else:
            self.status_var.set(
                "FFmpeg was not found. Some formats may not work."
            )
            self.toasts.show(
                "warning",
                "FFmpeg is missing\n"
                "MP3 and some videos need it.\n"
                "Install FFmpeg, then restart Video Tool.",
            )

    # ------------------------------------------------------------------
    # Output folder
    # ------------------------------------------------------------------

    def _browse_output(self) -> None:
        """Let the user choose an output directory."""

        folder = filedialog.askdirectory(
            title="Choose download folder"
        )

        if folder:
            self.output_var.set(folder)

    # ------------------------------------------------------------------
    # Help
    # ------------------------------------------------------------------

    def _show_help(self) -> None:
        """Display simple instructions for first-time users."""

        help_window = ctk.CTkToplevel(self)

        help_window.title(
            "How to use Video Tool"
        )
        help_window.geometry(
            "500x430"
        )
        help_window.resizable(
            False,
            False,
        )

        help_window.transient(self)
        help_window.grab_set()

        frame = ctk.CTkFrame(
            help_window,
            fg_color="transparent",
        )
        frame.pack(
            fill="both",
            expand=True,
            padx=28,
            pady=25,
        )

        title = ctk.CTkLabel(
            frame,
            text="How to use Video Tool",
            font=ctk.CTkFont(
                family="Segoe UI",
                size=25,
                weight="bold",
            ),
        )
        title.pack(
            anchor="w",
            pady=(0, 18),
        )

        instructions = (
            "1. Copy a YouTube video URL.\n\n"
            "2. Paste the URL into the YouTube URL box.\n\n"
            "3. Choose the format you want:\n"
            "   • MP4 for video\n"
            "   • MP3 for audio\n\n"
            "4. Choose your preferred quality.\n\n"
            "5. Choose where you want to save the file.\n\n"
            "6. Click Download and wait for it to finish.\n\n"
            "Your download history is kept on this device."
        )

        body = ctk.CTkLabel(
            frame,
            text=instructions,
            justify="left",
            anchor="w",
            font=ctk.CTkFont(
                family="Segoe UI",
                size=15,
            ),
        )
        body.pack(
            fill="both",
            expand=True,
            anchor="w",
        )

        close_button = ctk.CTkButton(
            frame,
            text="Close",
            width=110,
            height=40,
            corner_radius=9,
            font=ctk.CTkFont(
                family="Segoe UI",
                size=15,
                weight="bold",
            ),
            command=help_window.destroy,
        )
        close_button.pack(
            anchor="e",
            pady=(15, 0),
        )

    # ------------------------------------------------------------------
    # Download handling
    # ------------------------------------------------------------------

    def _start_download(self) -> None:
        """Validate the form and start the background download."""

        if (
            self._download_thread
            and self._download_thread.is_alive()
        ):
            return

        raw_urls = self.url_text.get(
            "1.0",
            "end",
        ).strip()

        if not raw_urls:
            self.toasts.show(
                "warning",
                "Paste a link first\n"
                "Add at least one YouTube link, one per line.",
            )
            self.url_text.focus_set()
            return

        urls = [
            clean_url(line)
            for line in raw_urls.splitlines()
            if clean_url(line)
        ]

        output_dir = self.output_var.get().strip()

        if not output_dir:
            self.toasts.show(
                "warning",
                "Choose a folder\n"
                "Pick where the downloads should be saved.",
            )
            return

        format_type = self.format_var.get()
        quality = self.quality_var.get()

        try:
            Path(output_dir).expanduser().mkdir(
                parents=True,
                exist_ok=True,
            )
        except OSError as exc:
            logger.warning(
                "Cannot use output folder %s", output_dir, exc_info=True
            )
            self.notify.report(explain(exc))
            return

        options = DownloadOptions(
            output_dir=output_dir,
            format_type=format_type,
            quality=quality,
            ffmpeg_location=self._ffmpeg_path,
        )

        self._set_busy(True)

        self.progress.set(0)

        self.status_var.set(
            f"Starting {len(urls)} download"
            f"{'' if len(urls) == 1 else 's'}..."
        )

        # Capture all Tkinter values before entering the worker thread.
        self._download_thread = threading.Thread(
            target=self._run_downloads,
            args=(
                urls,
                options,
                Path(output_dir),
            ),
            daemon=True,
        )
        self._download_thread.start()

    def _run_downloads(
        self,
        urls: list[str],
        options: DownloadOptions,
        output_dir: Path,
    ) -> None:
        """Perform downloads away from the Tkinter main thread."""

        successful = 0

        for index, url in enumerate(urls, 1):
            if self._closing:
                return

            self._ui_queue.put(
                (
                    "status",
                    f"Downloading {index}/{len(urls)}...",
                )
            )

            try:
                path, downloaded = fetch_clip(
                    url,
                    output_dir,
                    None,
                    None,
                    options,
                    full_download=True,
                    keep_original=True,
                )

                successful += 1

                self._ui_queue.put(
                    (
                        "success",
                        str(path),
                    )
                )

                # With several links the summary at the end says enough.
                if len(urls) == 1 and downloaded:
                    self.notify.success(
                        f"Download finished\n{Path(path).name}"
                    )
                elif len(urls) == 1:
                    self.notify.info(
                        "Already on your computer\n"
                        f"Used the saved copy of {Path(path).name}."
                    )

                # Count real downloads only, as Basic and Pro do. A file
                # reused from the cache is not a new download.
                # Counter updates happen on the Tkinter main thread.
                if downloaded:
                    self._ui_queue.put(
                        (
                            "download_success",
                            None,
                        )
                    )

            except Exception as exc:
                logger.exception(
                    "Download failed for %s",
                    url,
                )

                info = explain(exc)

                # With several links, say which one failed.
                if len(urls) > 1:
                    short = url if len(url) <= 50 else url[:47] + "..."
                    info = replace(
                        info, hint="\n".join(filter(None, (info.hint, short)))
                    )

                self.notify.report(info)

            progress_value = index / len(urls)

            self._ui_queue.put(
                (
                    "progress",
                    progress_value,
                )
            )

        self._ui_queue.put(
            (
                "complete",
                (
                    successful,
                    len(urls),
                ),
            )
        )

    def _notify_from_any_thread(self, level: str, message: str) -> None:
        """Receive a notification from any thread.

        The message goes through the queue so the toast is drawn on the
        main thread.
        """

        self._ui_queue.put(("toast", (level, message)))

    # ------------------------------------------------------------------
    # Queue / UI updates
    # ------------------------------------------------------------------

    def _poll_queue(self) -> None:
        """Process messages from the background worker on the UI thread."""

        if self._closing:
            return

        try:
            while True:
                kind, payload = (
                    self._ui_queue.get_nowait()
                )

                if kind == "status":
                    self.status_var.set(
                        str(payload)
                    )

                elif kind == "progress":
                    self.progress.set(
                        float(payload)
                    )

                elif kind == "success":
                    self.status_var.set(
                        f"Saved: {payload}"
                    )

                elif kind == "download_success":
                    self._record_download()

                elif kind == "toast":
                    level, text = payload
                    self.toasts.show(level, text)

                elif kind == "deps_done":
                    self._finish_dependency_check(payload)

                elif kind == "complete":
                    successful, total = payload

                    self._set_busy(False)

                    if total > 1 and successful == total:
                        self.toasts.show(
                            "success",
                            f"All {total} downloads finished",
                        )
                    elif total > 1 and successful > 0:
                        self.toasts.show(
                            "warning",
                            f"{successful} of {total} downloads finished\n"
                            "The others did not work. See the messages above.",
                        )

                    if successful == total:
                        self.progress.set(1)

                        self.status_var.set(
                            f"Completed {successful}/{total} download"
                            f"{'' if total == 1 else 's'}."
                        )

                    elif successful > 0:
                        self.progress.set(
                            successful / total
                        )

                        self.status_var.set(
                            f"Completed {successful}/{total} downloads."
                        )

                    else:
                        self.progress.set(0)

                        self.status_var.set(
                            "No downloads were completed."
                        )

        except queue.Empty:
            pass

        self.after(
            100,
            self._poll_queue,
        )

    # ------------------------------------------------------------------
    # Download counter
    # ------------------------------------------------------------------

    def _record_download(self) -> None:
        """Record one completed download."""

        self._download_count += 1

        try:
            save_download_count(
                self._download_count
            )
        except Exception:
            logger.exception(
                "Could not save download count"
            )

        self._update_counter()

        if self._download_count in MILESTONES:
            self._show_milestone(
                self._download_count
            )

    def _update_counter(self) -> None:
        """Update the visible download history."""

        count = self._download_count

        if count == 1:
            text = "1 download"
        else:
            text = f"{count} downloads"

        self.counter_var.set(text)

    # ------------------------------------------------------------------
    # Milestone / confetti
    # ------------------------------------------------------------------

    def _show_milestone(
        self,
        count: int,
    ) -> None:
        """Celebrate selected download milestones."""

        window = ctk.CTkToplevel(self)

        window.title("Milestone!")
        window.geometry("430x330")
        window.resizable(False, False)

        window.transient(self)
        window.grab_set()

        title = ctk.CTkLabel(
            window,
            text="🎉 Milestone!",
            font=ctk.CTkFont(
                family="Segoe UI",
                size=28,
                weight="bold",
            ),
        )
        title.pack(
            pady=(20, 4)
        )

        message = ctk.CTkLabel(
            window,
            text=f"You've completed {count} downloads!",
            font=ctk.CTkFont(
                family="Segoe UI",
                size=16,
            ),
        )
        message.pack(
            pady=(0, 12)
        )

        canvas = ctk.CTkCanvas(
            window,
            width=370,
            height=150,
            highlightthickness=0,
            bg="#212121",
        )
        canvas.pack(
            pady=(0, 10)
        )

        pieces: list[dict[str, object]] = []

        for _ in range(45):
            x = random.randint(10, 360)
            y = random.randint(-140, 0)
            size = random.randint(4, 9)

            item = canvas.create_rectangle(
                x,
                y,
                x + size,
                y + size,
                outline="",
                fill=random.choice(
                    [
                        "#4CAF50",
                        "#2196F3",
                        "#FFC107",
                        "#E91E63",
                        "#9C27B0",
                    ]
                ),
            )

            pieces.append(
                {
                    "id": item,
                    "speed": random.uniform(
                        2.0,
                        5.0,
                    ),
                    "drift": random.uniform(
                        -1.2,
                        1.2,
                    ),
                }
            )

        def animate() -> None:
            if not window.winfo_exists():
                return

            for piece in pieces:
                item_id = int(
                    piece["id"]
                )
                speed = float(
                    piece["speed"]
                )
                drift = float(
                    piece["drift"]
                )

                canvas.move(
                    item_id,
                    drift,
                    speed,
                )

                coords = canvas.coords(
                    item_id
                )

                if coords and coords[1] > 150:
                    x = random.randint(
                        10,
                        360,
                    )
                    size = random.randint(
                        4,
                        9,
                    )

                    canvas.coords(
                        item_id,
                        x,
                        -10,
                        x + size,
                        0,
                    )

            window.after(
                35,
                animate,
            )

        animate()

        close_button = ctk.CTkButton(
            window,
            text="Continue",
            width=120,
            height=40,
            corner_radius=9,
            font=ctk.CTkFont(
                family="Segoe UI",
                size=15,
                weight="bold",
            ),
            command=window.destroy,
        )
        close_button.pack()

    # ------------------------------------------------------------------
    # Busy state
    # ------------------------------------------------------------------

    def _set_busy(
        self,
        busy: bool,
    ) -> None:
        """Enable/disable controls during downloads."""

        state = (
            "disabled"
            if busy
            else "normal"
        )

        self.download_button.configure(
            state=state
        )

        self.browse_button.configure(
            state=state
        )

        self.format_menu.configure(
            state=state
        )

        self.quality_menu.configure(
            state=state
        )

        self.output_entry.configure(
            state=state
        )

        self.help_button.configure(
            state=state
        )

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def _on_close(self) -> None:
        """Close the application."""

        self._closing = True
        self.destroy()


def main() -> int:
    """Launch the Video Tool GUI."""

    app = VideoToolGUI()
    app.mainloop()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())