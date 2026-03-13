# Bootstrap script for PyInstaller
# This properly loads the ddgs package and calls the CLI entry point

import sys
import os

# Add the current directory to path to find the package
if getattr(sys, 'frozen', False):
    # Running as compiled executable
    os.chdir(sys._MEIPASS)
    sys.path.insert(0, sys._MEIPASS)

# Now import and call the entry point
from ddgs.cli import safe_entry_point

if __name__ == '__main__':
    safe_entry_point()