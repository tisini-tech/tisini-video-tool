@echo off
REM Packaging is intentionally deferred while YFetch is CLI-first.
REM Install PyInstaller and add explicit entry-point builds here later.
python -m pip install pyinstaller
python -m PyInstaller --onefile --name YFetch src\yfetch\__main__.py
