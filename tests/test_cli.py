import json

from papervault.cli import main


def test_cli_dryrun_stdout_json_and_progress_stderr(env, pdf, monkeypatch, capsys):
    v, _, _ = env
    monkeypatch.setattr("papervault.cli.Vault", lambda state: v)
    assert main(["--home", str(v.state.home), "import", str(pdf), "--dry-run", "--json"]) == 0
    out = capsys.readouterr()
    assert json.loads(out.out)["counts"] == {"accepted": 1}
    assert "[1]" in out.err


def test_cli_rejects_directory_wide_doi(env, pdf, monkeypatch, capsys):
    v, _, _ = env
    monkeypatch.setattr("papervault.cli.Vault", lambda state: v)
    assert (
        main(["--home", str(v.state.home), "import", str(pdf.parent), "--doi", "10.1234/example", "--json"])
        == 2
    )
    assert json.loads(capsys.readouterr().out)["exit_code"] == 2


def test_cli_redacts_unexpected_exception(tmp_path, monkeypatch, capsys):
    def fail(state):
        raise RuntimeError("Authorization: Bearer SECRET_TOKEN")

    monkeypatch.setattr("papervault.cli.Vault", fail)
    assert main(["--home", str(tmp_path), "doctor", "--json"]) == 4
    out = capsys.readouterr()
    assert "SECRET_TOKEN" not in out.err + out.out
