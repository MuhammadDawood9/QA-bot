"""Start the desktop app from the project folder, from anywhere (double-click, or the start-at-login
entry): the project folder is put on the import path first, so the working folder doesn't matter."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.app.main import main  # noqa: E402

sys.exit(main(sys.argv))
