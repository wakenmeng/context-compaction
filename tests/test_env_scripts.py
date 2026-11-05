import json

import pytest

import check_env
import gold_eval
from context_compaction import credentials

SECRET = "sk-test-0123456789abcdef"


@pytest.fixture
def healthy(monkeypatch, tmp_path):
    monkeypatch.setattr(credentials, "DOTENV", tmp_path / "no.env")  # never read the developer's .env
    monkeypatch.setenv("CC_CREDENTIALS_DIR", str(tmp_path / "creds"))
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    monkeypatch.setattr(check_env, "check_packages", lambda: [(True, "pkgs ok")])
    monkeypatch.setattr(check_env, "check_dataset", lambda: (True, "dataset ok"))
    monkeypatch.setattr(check_env, "check_docker", lambda: [(True, "docker ok")])
    monkeypatch.setattr(check_env.pins, "PYTHON", check_env.sys.version_info[:2])


def test_all_ok_exits_zero(healthy, capsys):
    check_env.main()
    assert "all checks passed" in capsys.readouterr().out


@pytest.mark.parametrize(
    "breakage",
    [
        lambda m: m.delenv("OPENAI_API_KEY"),
        lambda m: m.setattr(check_env.pins, "PYTHON", (3, 9)),
        lambda m: m.setattr(check_env, "check_packages", lambda: [(False, "swebench missing")]),
        lambda m: m.setattr(check_env, "check_dataset", lambda: (False, "sha mismatch")),
    ],
)
def test_any_failure_exits_non_zero_even_if_docker_passes(healthy, monkeypatch, breakage):
    breakage(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        check_env.main()
    assert "failed" in str(exc.value)


def test_secret_never_printed(healthy, capsys):
    check_env.main()
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err


def test_package_versions_are_compared(monkeypatch):
    monkeypatch.setattr(check_env.importlib.metadata, "version", lambda name: "0.0.1")
    assert all(not ok for ok, _ in check_env.check_packages())


def test_gold_summary_lists_unresolved(tmp_path):
    report = tmp_path / "gold.gold-smoke.json"
    report.write_text(json.dumps({"resolved_ids": ["a", "b"]}))
    assert gold_eval.summarize(report, ["a", "b", "c"]) == {
        "requested": 3,
        "resolved": 2,
        "not_resolved": ["c"],
    }


def test_gold_selects_sets(tmp_path):
    config = tmp_path / "instances.json"
    config.write_text(json.dumps({"smoke": {"ids": ["s"]}, "formal": {"ids": ["f1", "f2"]}}))
    assert gold_eval.selected_ids("smoke", config) == ["s"]
    assert gold_eval.selected_ids("all", config) == ["s", "f1", "f2"]


def test_key_loaded_from_file_is_not_printed(healthy, monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("OPENAI_API_KEY")
    creds = tmp_path / "creds"
    creds.mkdir()
    (creds / "openai.txt").write_text(SECRET + "\n")
    check_env.main()
    out = capsys.readouterr()
    assert "loaded from" in out.out
    assert SECRET not in out.out + out.err
    assert check_env.os.environ["OPENAI_API_KEY"] == SECRET


def test_environment_key_wins_over_file(tmp_path):
    (tmp_path / "openai.txt").write_text("from-file")
    env = {"CC_CREDENTIALS_DIR": str(tmp_path), "OPENAI_API_KEY": "from-env"}
    assert credentials.load(env, dotenv=False) == {"OPENAI_API_KEY": "env"}
    assert env["OPENAI_API_KEY"] == "from-env"


def test_empty_key_file_counts_as_missing(tmp_path):
    (tmp_path / "openai.txt").write_text("\n")
    env = {"CC_CREDENTIALS_DIR": str(tmp_path)}
    assert credentials.load(env, dotenv=False) == {"OPENAI_API_KEY": None}
    assert "OPENAI_API_KEY" not in env


class FakeProc:
    def __init__(self, code, stdout, stderr):
        self.returncode, self.stdout, self.stderr = code, stdout, stderr


def test_probe_reports_python_not_pull_progress(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd[1] == "version":
            return FakeProc(0, "amd64\n", "")
        return FakeProc(0, "Python 3.9.20\n", "Status: Downloaded newer image for x\n")

    monkeypatch.setattr(check_env.subprocess, "run", fake_run)
    assert check_env.check_docker()[-1] == (True, "amd64 probe image: Python 3.9.20")


def test_probe_failure_shows_stderr(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd[1] == "version":
            return FakeProc(0, "arm64\n", "")
        return FakeProc(125, "", "exec format error\n")

    monkeypatch.setattr(check_env.subprocess, "run", fake_run)
    assert check_env.check_docker()[-1] == (False, "amd64 probe image: exec format error")


def test_no_default_credentials_directory(tmp_path):
    env = {}
    assert credentials.load(env, dotenv=tmp_path / "missing.env") == {"OPENAI_API_KEY": None}
    assert credentials.credentials_dir(env) is None


def test_dotenv_fills_unset_values_only(tmp_path):
    keydir = tmp_path / "keys"
    keydir.mkdir()
    (keydir / "openai.txt").write_text(SECRET + "\n")
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"# local settings\nexport CC_CREDENTIALS_DIR='{keydir}'\nCC_ZONE=us-east1-b\n")
    env = {"CC_ZONE": "europe-west1-b"}
    assert credentials.load(env, dotenv=dotenv) == {"OPENAI_API_KEY": "file"}
    assert env["CC_ZONE"] == "europe-west1-b" and env["OPENAI_API_KEY"] == SECRET
