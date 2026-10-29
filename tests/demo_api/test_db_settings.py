"""Database settings: URL and sslmode validation, TLS verification, tunables."""

import ssl
from pathlib import Path

import pytest

from app.db import ConfigurationError, Settings

BUNDLE = Path(__file__).resolve().parents[2] / "examples" / "demo-api" / "certs" / "rds-global-bundle.pem"
URL = "postgresql://demo:dummy-password@db.example.internal:5432/demo"


def settings(**env):
    return Settings.from_env({"DATABASE_URL": URL, "DATABASE_CA_BUNDLE": str(BUNDLE), **env})


def test_verify_full_is_the_default_and_checks_the_hostname_against_the_rds_bundle():
    tls = settings().tls
    assert isinstance(tls, ssl.SSLContext)
    assert tls.check_hostname is True
    assert tls.verify_mode is ssl.CERT_REQUIRED
    certificates_in_bundle = BUNDLE.read_text().count("BEGIN CERTIFICATE")
    assert tls.cert_store_stats()["x509_ca"] == certificates_in_bundle, "only the RDS bundle may be trusted"


def test_disable_turns_tls_off_for_local_databases():
    assert settings(DATABASE_URL=URL + "?sslmode=disable").tls is False


@pytest.mark.parametrize(
    "url", ["", "demo:5432", "mysql://u:p@h:3306/db", "postgresql://u:p@h", URL + "?sslmode=require", URL + "?sslmode=prefer"]
)
def test_invalid_url_or_sslmode_is_rejected_without_echoing_the_url(url):
    with pytest.raises(ConfigurationError) as error:
        settings(DATABASE_URL=url)
    assert "dummy-password" not in str(error.value)


def test_missing_ca_bundle_is_rejected(tmp_path):
    with pytest.raises(ConfigurationError, match="DATABASE_CA_BUNDLE"):
        settings(DATABASE_CA_BUNDLE=str(tmp_path / "missing.pem"))


@pytest.mark.parametrize("port", ["abc", "99999", "0"])
def test_invalid_port_is_rejected_at_startup(port):
    with pytest.raises(ConfigurationError, match="DATABASE_URL"):
        settings(DATABASE_URL=URL.replace(":5432", f":{port}"))


def test_pool_and_timeout_defaults_and_overrides():
    defaults = settings()
    assert defaults.pool_max == 5
    assert (defaults.connect_timeout, defaults.acquire_timeout, defaults.query_timeout) == (3, 2, 5)
    tuned = settings(DATABASE_POOL_MAX="8", DATABASE_QUERY_TIMEOUT="1.5")
    assert (tuned.pool_max, tuned.query_timeout) == (8, 1.5)


@pytest.mark.parametrize("env", [{"DATABASE_POOL_MAX": "many"}, {"DATABASE_POOL_MAX": "0"}, {"DATABASE_CONNECT_TIMEOUT": "-1"}])
def test_invalid_tunables_are_rejected(env):
    with pytest.raises(ConfigurationError):
        settings(**env)
