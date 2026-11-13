# Run as scripts, the stand modules import each other as `e2e.large_installation.*` from the checkout.
import sys
from pathlib import Path

REPO_ROOT = str(Path(__file__).resolve().parents[2])
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
