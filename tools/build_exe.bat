@echo off
REM Packaging is intentionally deferred while Video Tool is CLI-first.
REM Install PyInstaller and add explicit entry-point builds here later.
python -m pip install pyinstaller
python -m PyInstaller --onefile --name video-tool src\video_tool\__main__.py
