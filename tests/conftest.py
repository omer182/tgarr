import os
import sys
import tempfile
from pathlib import Path

# app.config reads its paths from the environment at import time: point it at a throwaway folder
os.environ.setdefault("CONFIG_DIR", tempfile.mkdtemp(prefix="tgarr-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
