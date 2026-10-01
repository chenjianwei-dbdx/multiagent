"""Make tests/fixtures importable (the fixtures dir is deliberately not a
package — only weekly_report.py lives there)."""

from __future__ import annotations

import sys
from pathlib import Path

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
if str(_FIXTURES) not in sys.path:
    sys.path.insert(0, str(_FIXTURES))
