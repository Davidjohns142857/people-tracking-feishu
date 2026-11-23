import json

from people_tracking_feishu import cli, validation_profile


def test_control_page_init_is_independent_of_production_runtime(tmp_path, monkeypatch, capsys):
    def unexpected_live_state(_):
        raise AssertionError("validation must not open production runtime")

    monkeypatch.setattr(cli, "_runtime_paths", unexpected_live_state)
    root = tmp_path / "control"
    assert cli.main([
        "validation-profile", "init", "--root", str(root),
        "--subject-name", "Explicit Test Person", "--json",
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["data"]["test_only"] is True
    assert result["data"]["remote_registration_verified"] is False
    assert "Explicit Test Person" in (root / "public/index.html").read_text()
    assert not (root / "validation.sqlite3").exists()


def test_failed_control_check_is_machine_detectable(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        validation_profile, "evaluate_profile",
        lambda *a, **kw: {"failed": 1, "pending": 0, "evidence_source": "remote"},
    )
    assert cli.main([
        "validation-profile", "check", "--root", str(tmp_path),
        "--evidence-source", "remote", "--json",
    ]) == 3
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False
    assert result["error"]["type"] == "ControlledProfileValidationFailed"
    assert result["data"]["failed"] == 1


def test_missing_host_is_an_explicit_blocker(tmp_path, capsys):
    root = tmp_path / "control"
    validation_profile.init_profile(root, subject_name="Explicit Test Person")
    assert cli.main([
        "validation-profile", "tick", "--root", str(root),
        "--evidence-source", "local", "--json",
    ]) == 2
    result = json.loads(capsys.readouterr().err)
    assert result["ok"] is False
    assert not (root / "injections.jsonl").exists()


def test_remote_evidence_import_does_not_run_live_tracker(tmp_path, monkeypatch, capsys):
    captured = {}

    def import_receipts(root, evidence):
        captured.update(root=root, evidence=evidence)
        return {"imported": 1, "independently_verified": False}

    monkeypatch.setattr(validation_profile, "import_remote_observations", import_receipts)
    source = tmp_path / "receipts.json"
    source.write_text(json.dumps([{"observation_id": "obs-receipt"}]))
    assert cli.main([
        "validation-profile", "import-observations", "--root", str(tmp_path / "control"),
        "--input", str(source), "--json",
    ]) == 0
    assert captured["evidence"] == [{"observation_id": "obs-receipt"}]
    assert json.loads(capsys.readouterr().out)["data"]["independently_verified"] is False


def test_check_without_remote_evidence_does_not_report_success(tmp_path, capsys):
    root = tmp_path / "control"
    validation_profile.init_profile(root, subject_name="Explicit Test Person")
    assert cli.main([
        "validation-profile", "check", "--root", str(root),
        "--evidence-source", "remote", "--json",
    ]) == 4
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False
    assert result["data"]["status"] == "blocked"
    assert result["data"]["remote_registration_verified"] is False
