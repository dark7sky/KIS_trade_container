import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_env_example_has_no_values():
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            assert line.endswith("="), "Example values must remain blank"


def test_git_ignores_secret_and_runtime_files():
    result = subprocess.run(
        ["git", "check-ignore", "--stdin", "-z"],
        input=b".env\0.env.production\0data/state.sqlite3\0runtime/cache\0",
        capture_output=True,
        cwd=ROOT,
        check=True,
    )
    assert result.stdout.count(b"\0") == 4
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], capture_output=True, cwd=ROOT, check=True
    ).stdout
    assert b".env" not in tracked.rstrip(b"\0").split(b"\0")


def test_git_guard_handles_nested_files():
    spec = importlib.util.spec_from_file_location("guard", ROOT / "scripts/check_git_secrets.py")
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    for name in (
        ".env",
        "nested/.env.production",
        "data/trading.sqlite3",
        "runtime/tokens.json",
        "keys/server.key",
    ):
        assert guard.forbidden(name)
    for name in (".env.example", "src/kis_mcp/app.py", "tests/compose.test.env"):
        assert not guard.forbidden(name)


def test_docker_context_is_allowlist():
    lines = (ROOT / ".dockerignore").read_text().splitlines()
    assert lines[0] == "**"
    assert all("env" not in line for line in lines if line.startswith("!"))


def test_guard_blocks_forced_env_in_index_and_history(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".env").write_text("TEST_ONLY=fake\n", encoding="utf-8")
    subprocess.run(["git", "add", "-f", ".env"], cwd=tmp_path, check=True)
    guard = [sys.executable, str(ROOT / "scripts/check_git_secrets.py")]
    result = subprocess.run(guard + ["--staged"], cwd=tmp_path, capture_output=True)
    assert result.returncode == 1
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=tmp_path,
        check=True,
    )
    result = subprocess.run(guard + ["--history", "HEAD"], cwd=tmp_path, capture_output=True)
    assert result.returncode == 1
