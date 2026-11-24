from pathlib import Path

import pytest
from git import GitCommandError, Repo

from path_sync._internal import validation
from path_sync._internal.header import get_header_line

HEADER = get_header_line(Path("test.py"), "test-config")


def _setup_baseline(repo_path, filename: str, content: str) -> None:
    """Commit content as baseline on main and create origin/main ref."""
    repo = Repo(repo_path)
    file_path = repo_path / filename
    file_path.write_text(content)
    repo.index.add([filename])
    repo.index.commit("baseline")
    repo.create_head("origin/main", repo.head.commit.hexsha)


def _setup_baseline_on_remote(repo_path, filename: str, content: str, remote: str) -> None:
    """Commit content as baseline and create <remote>/main as a remote-tracking ref, with no origin."""
    repo = Repo(repo_path)
    (repo_path / filename).write_text(content)
    repo.index.add([filename])
    repo.index.commit("baseline")
    repo.create_remote(remote, "https://example.com/repo.git")
    repo.git.update_ref(f"refs/remotes/{remote}/main", repo.head.commit.hexsha)


def test_modify_ok_edit_passes(tmp_repo):
    baseline = f"""{HEADER}
# === OK_EDIT: path-sync header ===
user content
# === DO_NOT_EDIT: path-sync standard ===
protected
# === OK_EDIT: path-sync standard ===
"""
    _setup_baseline(tmp_repo, "test.py", baseline)

    current = f"""{HEADER}
# === OK_EDIT: path-sync header ===
modified user content
# === DO_NOT_EDIT: path-sync standard ===
protected
# === OK_EDIT: path-sync standard ===
"""
    (tmp_repo / "test.py").write_text(current)

    result = validation.validate_no_unauthorized_changes(tmp_repo, "main")
    assert result == []


def test_modify_do_not_edit_fails(tmp_repo):
    baseline = f"""{HEADER}
# === DO_NOT_EDIT: path-sync standard ===
protected content
# === OK_EDIT: path-sync standard ===
"""
    _setup_baseline(tmp_repo, "test.py", baseline)

    current = f"""{HEADER}
# === DO_NOT_EDIT: path-sync standard ===
MODIFIED protected content
# === OK_EDIT: path-sync standard ===
"""
    (tmp_repo / "test.py").write_text(current)

    result = validation.validate_no_unauthorized_changes(tmp_repo, "main")
    assert result == ["test.py:standard"]


def test_skip_section_passes(tmp_repo):
    baseline = f"""{HEADER}
# === DO_NOT_EDIT: path-sync coverage ===
protected
# === OK_EDIT: path-sync coverage ===
"""
    _setup_baseline(tmp_repo, "test.py", baseline)

    current = f"""{HEADER}
# === DO_NOT_EDIT: path-sync coverage ===
MODIFIED
# === OK_EDIT: path-sync coverage ===
"""
    (tmp_repo / "test.py").write_text(current)

    result = validation.validate_no_unauthorized_changes(tmp_repo, "main", skip_sections={"test.py": {"coverage"}})
    assert result == []


def test_section_removed_warns_not_fails(tmp_repo, caplog):
    baseline = f"""{HEADER}
# === DO_NOT_EDIT: path-sync standard ===
protected
# === OK_EDIT: path-sync standard ===
"""
    _setup_baseline(tmp_repo, "test.py", baseline)

    current = f"""{HEADER}
# no sections anymore
"""
    (tmp_repo / "test.py").write_text(current)

    result = validation.validate_no_unauthorized_changes(tmp_repo, "main")
    assert result == []
    assert "Section 'standard' removed" in caplog.text


def test_no_sections_full_file_comparison(tmp_repo):
    baseline = f"{HEADER}\noriginal content\n"
    _setup_baseline(tmp_repo, "test.py", baseline)

    (tmp_repo / "test.py").write_text(f"{HEADER}\nmodified content\n")

    result = validation.validate_no_unauthorized_changes(tmp_repo, "main")
    assert result == ["test.py"]


def test_remote_other_than_origin(tmp_repo):
    _setup_baseline_on_remote(tmp_repo, "test.py", f"{HEADER}\noriginal content\n", "upstream")

    (tmp_repo / "test.py").write_text(f"{HEADER}\nmodified content\n")

    result = validation.validate_no_unauthorized_changes(tmp_repo, "main", remote="upstream")
    assert result == ["test.py"]


def test_default_remote_is_origin(tmp_repo):
    _setup_baseline_on_remote(tmp_repo, "test.py", f"{HEADER}\noriginal content\n", "upstream")

    with pytest.raises(GitCommandError, match="origin/main"):
        validation.validate_no_unauthorized_changes(tmp_repo, "main")


def test_parse_skip_sections():
    result = validation.parse_skip_sections("justfile:coverage,pyproject.toml:default")
    assert result == {"justfile": {"coverage"}, "pyproject.toml": {"default"}}

    result = validation.parse_skip_sections("path:a,path:b")
    assert result == {"path": {"a", "b"}}
