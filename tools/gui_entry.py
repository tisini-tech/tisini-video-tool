"""PyInstaller entry point for the GUI build.

video_tool.gui.app uses relative imports (from ..youtube import ...), which
break if PyInstaller is pointed at that file directly -- it gets run as a
bare script with no parent package, the same way `python app.py` would
fail outside the package too. Pointing PyInstaller at this launcher instead
means app.py is properly imported as a package member, so its relative
imports resolve normally.
"""
from video_tool.gui.app import main

if __name__ == "__main__":
    main()
