"""A simple, modern GUI over the shared download logic in video_tool.youtube.

Runs downloads on a background thread so the window never freezes, and
uses ensure_all() at startup to make sure a working yt-dlp and ffmpeg are
present before letting the user click Download.

Built on CustomTkinter rather than plain ttk for a more modern look --
everything else (threading, queue-based UI updates, bundled_deps wiring)
is unchanged from the ttk version.
"""
from __future__ import annotations

import queue
import shutil
import threading
from pathlib import Path

import customtkinter as ctk

from ..youtube import DownloadOptions, download_url
from .bundled_deps import ensure_all

DEFAULT_OUTPUT_DIR = str(Path.home() / "Downloads" / "video-tool")

# Bump this to scale every label/button font at once. Widgets that don't
# take an explicit font (the URL box, entry fields, dropdown values) use
# ctk's own default, set separately just below.
BASE_FONT_SIZE = 26

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")
ctk.ThemeManager.theme["CTkFont"]["size"] = BASE_FONT_SIZE


class VideoToolGUI:
    def __init__(self, root: ctk.CTk):
        self.root = root
        self.root.title("Video Tool")
        self.root.geometry("560x460")
        self.root.minsize(480, 420)

        self._ui_queue: queue.Queue[tuple] = queue.Queue()
        self._ffmpeg_path: str | None = None
        self._ytdlp_ready = False

        self._build_widgets()
        self.root.after(100, self._poll_queue)

        self._set_status("Checking for updates...")
        threading.Thread(target=self._startup_check, daemon=True).start()

    # ---- layout --------------------------------------------------------

    def _build_widgets(self):
        pad = {"padx": 24}

        ctk.CTkLabel(self.root, text="Video Tool",
                     font=ctk.CTkFont(size=BASE_FONT_SIZE + 6, weight="bold")).pack(
            anchor="w", pady=(24, 2), **pad)
        ctk.CTkLabel(self.root, text="Paste one or more YouTube URLs, one per line",
                     text_color="gray60",
                     font=ctk.CTkFont(size=BASE_FONT_SIZE - 1)).pack(
            anchor="w", pady=(0, 12), **pad)

        self.url_text = ctk.CTkTextbox(self.root, height=110, corner_radius=10)
        self.url_text.pack(fill="x", **pad)

        options_row = ctk.CTkFrame(self.root, fg_color="transparent")
        options_row.pack(fill="x", pady=16, **pad)

        format_col = ctk.CTkFrame(options_row, fg_color="transparent")
        format_col.pack(side="left")
        ctk.CTkLabel(format_col, text="Format", text_color="gray60",
                     font=ctk.CTkFont(size=BASE_FONT_SIZE - 1)).pack(anchor="w")
        self.format_var = ctk.StringVar(value="MP4")
        ctk.CTkOptionMenu(format_col, variable=self.format_var,
                          values=["MP4", "MP3"], width=110).pack(
            anchor="w", pady=(4, 0))

        quality_col = ctk.CTkFrame(options_row, fg_color="transparent")
        quality_col.pack(side="left", padx=(20, 0))
        ctk.CTkLabel(quality_col, text="Quality", text_color="gray60",
                     font=ctk.CTkFont(size=BASE_FONT_SIZE - 1)).pack(anchor="w")
        self.quality_var = ctk.StringVar(value="Best")
        ctk.CTkOptionMenu(quality_col, variable=self.quality_var,
                          values=["Best", "1080p", "720p", "480p", "360p"],
                          width=110).pack(anchor="w", pady=(4, 0))

        out_col = ctk.CTkFrame(self.root, fg_color="transparent")
        out_col.pack(fill="x", **pad)
        ctk.CTkLabel(out_col, text="Save to", font=ctk.CTkFont(size=BASE_FONT_SIZE - 1),
                     text_color="gray60").pack(anchor="w")
        picker_row = ctk.CTkFrame(out_col, fg_color="transparent")
        picker_row.pack(fill="x", pady=(4, 0))
        self.output_var = ctk.StringVar(value=DEFAULT_OUTPUT_DIR)
        ctk.CTkEntry(picker_row, textvariable=self.output_var, corner_radius=8).pack(
            side="left", fill="x", expand=True)
        ctk.CTkButton(picker_row, text="Browse", width=80, corner_radius=8,
                      fg_color="gray30", hover_color="gray25",
                      command=self._browse_folder).pack(side="left", padx=(8, 0))

        self.download_btn = ctk.CTkButton(
            self.root, text="Download", height=42, corner_radius=10,
            font=ctk.CTkFont(size=BASE_FONT_SIZE + 2, weight="bold"),
            command=self._start_download, state="disabled")
        self.download_btn.pack(pady=(20, 10), **pad)

        self.progress = ctk.CTkProgressBar(self.root, corner_radius=6)
        self.progress.set(0)
        self.progress.pack(fill="x", **pad)

        self.status_var = ctk.StringVar(value="")
        ctk.CTkLabel(self.root, textvariable=self.status_var, text_color="gray60",
                     font=ctk.CTkFont(size=BASE_FONT_SIZE - 1)).pack(
            anchor="w", pady=(8, 16), **pad)

    def _browse_folder(self):
        from tkinter import filedialog
        chosen = filedialog.askdirectory(
            initialdir=self.output_var.get() or str(Path.home()))
        if chosen:
            self.output_var.set(chosen)

    # ---- background work / thread-safe UI updates ---------------------

    def _set_status(self, text: str):
        self.status_var.set(text)

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self._ui_queue.get_nowait()
                if kind == "status":
                    self.status_var.set(payload)
                elif kind == "progress":
                    self.progress.set(max(0.0, min(1.0, payload / 100)))
                elif kind == "ready":
                    self._ytdlp_ready = True
                    self._ffmpeg_path = payload
                    self.download_btn.configure(state="normal")
                    self.status_var.set("Ready.")
                elif kind == "not_ready":
                    self.status_var.set(payload)
                elif kind == "done":
                    self.download_btn.configure(state="normal")
                    self.status_var.set(payload)
                    self.progress.set(0)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _startup_check(self):
        def report(msg: str):
            self._ui_queue.put(("status", msg))

        result = ensure_all(progress_callback=report)
        ytdlp_ok = result["ytdlp"] is not None
        ffmpeg_ok = result["ffmpeg"] is not None or self._system_ffmpeg_present()

        if ytdlp_ok and ffmpeg_ok:
            ffmpeg_path = str(result["ffmpeg"]) if result["ffmpeg"] else None
            self._ui_queue.put(("ready", ffmpeg_path))
        else:
            missing = []
            if not ytdlp_ok:
                missing.append("yt-dlp")
            if not ffmpeg_ok:
                missing.append("ffmpeg")
            self._ui_queue.put((
                "not_ready",
                f"Could not set up: {', '.join(missing)}. Check your internet "
                f"connection and restart.",
            ))

    def _system_ffmpeg_present(self) -> bool:
        return shutil.which("ffmpeg") is not None

    def _start_download(self):
        raw_lines = self.url_text.get("1.0", "end").splitlines()
        urls = [u.strip() for u in raw_lines if u.strip()]
        if not urls:
            self._ui_queue.put(("status", "Paste at least one URL first."))
            return

        self.download_btn.configure(state="disabled")
        self._ui_queue.put(("status", f"Starting {len(urls)} download(s)..."))
        threading.Thread(target=self._run_downloads, args=(urls,), daemon=True).start()

    def _run_downloads(self, urls: list[str]):
        def hook(d: dict):
            if d.get("status") == "downloading":
                pct_str = d.get("_percent_str", "0%").strip().rstrip("%")
                try:
                    self._ui_queue.put(("progress", float(pct_str)))
                except ValueError:
                    pass
            elif d.get("status") == "finished":
                self._ui_queue.put(("progress", 100))

        options = DownloadOptions(
            output_dir=self.output_var.get() or DEFAULT_OUTPUT_DIR,
            format_type=self.format_var.get(),
            quality=self.quality_var.get(),
            progress_hook=hook,
            ffmpeg_location=self._ffmpeg_path,
        )

        succeeded, failed = 0, 0
        for i, url in enumerate(urls, 1):
            self._ui_queue.put(("status", f"Downloading {i}/{len(urls)}..."))
            try:
                download_url(url, options)
                succeeded += 1
            except Exception as e:
                failed += 1
                self._ui_queue.put(("status", f"Failed: {url} ({e})"))

        summary = f"Done: {succeeded} succeeded"
        if failed:
            summary += f", {failed} failed"
        self._ui_queue.put(("done", summary))


def main():
    root = ctk.CTk()
    VideoToolGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
