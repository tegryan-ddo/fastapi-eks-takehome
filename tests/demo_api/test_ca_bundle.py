"""The Dockerfile copies the public RDS CA bundle, so a clean checkout must
contain it and it must match the checksum recorded in certs/README.md."""

import hashlib
import re
from pathlib import Path

CERTS = Path(__file__).resolve().parents[2] / "examples" / "demo-api" / "certs"


def test_rds_ca_bundle_is_committed_and_matches_its_recorded_checksum():
    bundle = CERTS / "rds-global-bundle.pem"
    assert bundle.is_file(), f"{bundle} is missing; the image build copies it (see certs/README.md)"

    text = bundle.read_text()
    assert text.count("-----BEGIN CERTIFICATE-----") >= 50, "expected the full RDS global bundle"
    assert "PRIVATE KEY" not in text, "the bundle must contain certificates only"

    recorded = re.search(r"\b([0-9a-f]{64})\b", (CERTS / "README.md").read_text())
    assert recorded, "certs/README.md must record the bundle's sha256"
    actual = hashlib.sha256(bundle.read_bytes()).hexdigest()
    assert actual == recorded.group(1), f"bundle sha256 {actual} differs from certs/README.md; update both together"
