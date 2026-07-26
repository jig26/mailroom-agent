import os
import sys

# Let `app.*` resolve when this file is imported as the Vercel function entry.
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.main import app  # noqa: E402,F401  (Vercel looks for `app` here)
