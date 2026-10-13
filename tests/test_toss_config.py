from pathlib import Path

import pytest

from kis_mcp.config import Settings, TossCredentials


@pytest.fixture
def configured_env(monkeypatch):
    fixture = Path(__file__).with_name("compose.test.env").read_text()
    for line in fixture.splitlines():
        if line and not line.startswith("#"):
            name, value = line.split("=", 1)
            monkeypatch.setenv(name, value)
    for name in ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "TOSS_ACCOUNT_SEQ"):
        monkeypatch.delenv(name, raising=False)


def test_toss_credentials_are_optional_and_private(configured_env, monkeypatch):
    assert Settings.from_env().toss_credentials is None
    monkeypatch.setenv("TOSS_CLIENT_ID", "test-client")
    monkeypatch.setenv("TOSS_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("TOSS_ACCOUNT_SEQ", "3")
    creds = Settings.from_env().toss_credentials
    assert creds.account == "3"
    assert "test-secret" not in repr(creds)
    assert creds.fingerprint != TossCredentials("other-client", "secret", "3").fingerprint
    assert creds.fingerprint == TossCredentials("test-client", "rotated-secret", "3").fingerprint


@pytest.mark.parametrize("missing", ["TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "TOSS_ACCOUNT_SEQ"])
def test_partial_toss_configuration_fails_startup(configured_env, monkeypatch, missing):
    for name, value in {
        "TOSS_CLIENT_ID": "test-client",
        "TOSS_CLIENT_SECRET": "test-secret",
        "TOSS_ACCOUNT_SEQ": "3",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(missing)
    with pytest.raises(ValueError, match="Toss requires"):
        Settings.from_env()


def test_account_sequence_rejects_header_injection(configured_env, monkeypatch):
    monkeypatch.setenv("TOSS_CLIENT_ID", "test-client")
    monkeypatch.setenv("TOSS_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("TOSS_ACCOUNT_SEQ", "3\r\nX-Injected: yes")
    with pytest.raises(ValueError, match="Toss requires"):
        Settings.from_env()
