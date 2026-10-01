"""Plan validator tests run against the real weekly-report contract.

The fixtures dir (tests/fixtures) is deliberately not a package, so it is put
on sys.path here exactly like tests/unit/templates does.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from omas.domain.template import TemplateContract
from omas.templates import EXTRACTOR_VERSION, build_contract

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
if str(_FIXTURES) not in sys.path:
    sys.path.insert(0, str(_FIXTURES))


@pytest.fixture(scope="session")
def contract() -> TemplateContract:
    """Extract the real contract from the fixture template + sidecar."""
    from weekly_report import build_weekly_report_template

    fixture = build_weekly_report_template()
    return build_contract(
        docx_bytes=fixture.docx_bytes,
        sidecar=fixture.sidecar,
        styles_spec=fixture.styles_spec,
        static_map=fixture.static_map,
        template_id="weekly-report",
        version=1,
        extractor_version=EXTRACTOR_VERSION,
    )
