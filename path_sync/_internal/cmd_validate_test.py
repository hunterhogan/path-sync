from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from git import Repo
from typer.testing import CliRunner

from path_sync._internal.cmd_validate import validate_no_changes
from path_sync._internal.header import get_header_line
from path_sync._internal.typer_app import app
from path_sync._internal.validation import validate_no_unauthorized_changes

MODULE = validate_no_changes.__module__
HEADER = get_header_line(Path("test.py"), "test-config")
runner = CliRunner()


def test_validate_no_changes_passes_branch_to_validation(tmp_path: Path):
    mock_repo = MagicMock()
    mock_repo.active_branch.name = "feature-branch"

    with (
        patch(f"{MODULE}.find_repo_root", return_value=tmp_path),
        patch(f"{MODULE}.git_ops") as git_ops,
        patch(f"{MODULE}.{validate_no_unauthorized_changes.__name__}") as validate_fn,
    ):
        git_ops.get_repo.return_value = mock_repo
        validate_fn.return_value = []

        validate_no_changes(
            branch="develop",
            skip_sections_opt="",
            src_root_opt=str(tmp_path),
        )

        validate_fn.assert_called_once()
        call_kw = validate_fn.call_args
        assert call_kw[0][0] == tmp_path
        assert call_kw[0][1] == "develop"


def test_validate_no_changes_passes_remote_to_validation(tmp_path: Path):
    mock_repo = MagicMock()
    mock_repo.active_branch.name = "feature-branch"

    with (
        patch(f"{MODULE}.find_repo_root", return_value=tmp_path),
        patch(f"{MODULE}.git_ops") as git_ops,
        patch(f"{MODULE}.{validate_no_unauthorized_changes.__name__}") as validate_fn,
    ):
        git_ops.get_repo.return_value = mock_repo
        validate_fn.return_value = []

        validate_no_changes(
            branch="main",
            remote="upstream",
            skip_sections_opt="",
            src_root_opt=str(tmp_path),
        )

        validate_fn.assert_called_once()
        assert validate_fn.call_args[0][3] == "upstream"


def test_validate_no_changes_skips_when_on_sync_branch(tmp_path: Path):
    mock_repo = MagicMock()
    mock_repo.active_branch.name = "sync/python-template"

    with (
        patch(f"{MODULE}.find_repo_root", return_value=tmp_path),
        patch(f"{MODULE}.git_ops") as git_ops,
        patch(f"{MODULE}.{validate_no_unauthorized_changes.__name__}") as validate_fn,
    ):
        git_ops.get_repo.return_value = mock_repo

        validate_no_changes(
            branch="main",
            skip_sections_opt="",
            src_root_opt=str(tmp_path),
        )

        validate_fn.assert_not_called()


def test_validate_no_changes_skips_when_on_base_branch(tmp_path: Path):
    mock_repo = MagicMock()
    mock_repo.active_branch.name = "main"

    with (
        patch(f"{MODULE}.find_repo_root", return_value=tmp_path),
        patch(f"{MODULE}.git_ops") as git_ops,
        patch(f"{MODULE}.{validate_no_unauthorized_changes.__name__}") as validate_fn,
    ):
        git_ops.get_repo.return_value = mock_repo

        validate_no_changes(
            branch="main",
            skip_sections_opt="",
            src_root_opt=str(tmp_path),
        )

        validate_fn.assert_not_called()


def _setup_repo_without_origin(repo_path: Path, content: str) -> None:
    """Commit a synced file as the baseline of upstream/main (there is no origin), then switch to a feature branch."""
    repo = Repo(repo_path)
    (repo_path / "test.py").write_text(f"{HEADER}\n{content}\n")
    repo.index.add(["test.py"])
    repo.index.commit("baseline")
    repo.create_remote("upstream", "https://example.com/repo.git")
    repo.git.update_ref("refs/remotes/upstream/main", repo.head.commit.hexsha)
    repo.git.checkout("-b", "feature")


def test_validate_no_changes_cli_remote_option_unchanged_passes(tmp_repo, monkeypatch):
    monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
    _setup_repo_without_origin(tmp_repo, "original content")

    result = runner.invoke(app, ["validate-no-changes", "--remote", "upstream", "--src-root", str(tmp_repo)])

    assert result.exit_code == 0, result.output


def test_validate_no_changes_cli_remote_option_modified_fails(tmp_repo, monkeypatch):
    monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
    _setup_repo_without_origin(tmp_repo, "original content")
    (tmp_repo / "test.py").write_text(f"{HEADER}\nmodified content\n")

    result = runner.invoke(app, ["validate-no-changes", "--remote", "upstream", "--src-root", str(tmp_repo)])

    assert result.exit_code == 1, result.output
