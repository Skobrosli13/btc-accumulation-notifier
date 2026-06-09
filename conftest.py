"""Ensures the project root is importable so tests can `import app` regardless
of where pytest is invoked from."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

from app import scoring  # noqa: E402


@pytest.fixture(autouse=True)
def _force_fixed_thresholds():
    """Run unit tests against the fixed-threshold fallback by default: deterministic
    and immune to a committed calibration.json. Percentile tests inject their own
    via scoring.set_calibration({...})."""
    scoring.set_calibration({})
    yield
    scoring.set_calibration(None)
