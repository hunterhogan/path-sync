from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from git import Repo
from typer.testing import CliRunner

from path_sync._internal import git_ops
from path_sync._internal.cmd_copy import (
    CopyOptions,
    _cleanup_orphans,
    _close_stale_pr,
    _print_dest_only_hint,
    _skip_already_synced,
    _sync_path,
)
from path_sync._internal.header import add_header, has_header
from path_sync._internal.models import (
    CommitConfig,
    Destination,
    OnFailStrategy,
    PathMapping,
    SrcConfig,
    SyncMode,
    VerifyConfig,
    VerifyStep,
    resolve_config_path,
)
from path_sync._internal.repo_utils import ensure_repo
from path_sync._internal.typer_app import app
from path_sync._internal.verify import VerifyStatus, run_verify_steps
from path_sync._internal.yaml_utils import dump_yaml_model

CONFIG_NAME = "test-config"
runner = CliRunner()


def _make_dest(**kwargs) -> Destination:
    defaults = {"name": "test", "dest_path_relative": "."}
    return Destination.model_validate(defaults | kwargs)


def test_sync_single_file(tmp_path):
    src_root = tmp_path / "src"
    dest_root = tmp_path / "dest"
    src_root.mkdir()
    dest_root.mkdir()

    (src_root / "file.py").write_text("content")

    mapping = PathMapping(src_path="file.py", dest_path="out.py")
    changes, synced = _sync_path(mapping, src_root, dest_root, _make_dest(), CONFIG_NAME, False, False)

    assert changes == 1
    assert dest_root / "out.py" in synced
    result = (dest_root / "out.py").read_text()
    assert has_header(result)
    assert f"path-sync copy -n {CONFIG_NAME}" in result


def test_sync_skips_opted_out_file(tmp_path):
    src_root = tmp_path / "src"
    dest_root = tmp_path / "dest"
    src_root.mkdir()
    dest_root.mkdir()

    (src_root / "file.py").write_text("new content")
    (dest_root / "file.py").write_text("local content without header")

    mapping = PathMapping(src_path="file.py")
    changes, _ = _sync_path(mapping, src_root, dest_root, _make_dest(), CONFIG_NAME, False, False)

    assert changes == 0
    assert (dest_root / "file.py").read_text() == "local content without header"


def test_force_overwrite_adds_header_when_content_matches(tmp_path):
    src_root = tmp_path / "src"
    dest_root = tmp_path / "dest"
    src_root.mkdir()
    dest_root.mkdir()

    content = "same content"
    (src_root / "file.py").write_text(content)
    (dest_root / "file.py").write_text(content)  # No header, same content

    mapping = PathMapping(src_path="file.py")
    changes, _ = _sync_path(mapping, src_root, dest_root, _make_dest(), CONFIG_NAME, False, True)

    assert changes == 1
    result = (dest_root / "file.py").read_text()
    assert has_header(result)
    assert content in result


def test_cleanup_orphans(tmp_path):
    dest_root = tmp_path / "dest"
    dest_root.mkdir()

    # File with matching config header - will be orphaned
    orphan = dest_root / "orphan.py"
    orphan.write_text(add_header("orphan content", orphan, CONFIG_NAME))

    # File with different config - should not be deleted
    other = dest_root / "other.py"
    other.write_text(add_header("other content", other, "other-config"))

    synced: set = set()  # No files synced
    deleted = _cleanup_orphans(dest_root, CONFIG_NAME, synced, dry_run=False)

    assert deleted == 1
    assert not orphan.exists()
    assert other.exists()


def test_sync_with_sections_replaces_managed(tmp_path):
    src_root = tmp_path / "src"
    dest_root = tmp_path / "dest"
    src_root.mkdir()
    dest_root.mkdir()

    src_content = """\
# === DO_NOT_EDIT: path-sync standard ===
new recipe
# === OK_EDIT: path-sync standard ==="""
    (src_root / "file.sh").write_text(src_content)

    dest_file = dest_root / "file.sh"
    dest_content = add_header(
        """\
# === DO_NOT_EDIT: path-sync standard ===
old recipe
# === OK_EDIT: path-sync standard ===
# my custom stuff""",
        dest_file,
        CONFIG_NAME,
    )
    dest_file.write_text(dest_content)

    mapping = PathMapping(src_path="file.sh")
    changes, _ = _sync_path(mapping, src_root, dest_root, _make_dest(), CONFIG_NAME, False, False)

    assert changes == 1
    result = (dest_root / "file.sh").read_text()
    assert "new recipe" in result
    assert "old recipe" not in result
    assert "# my custom stuff" in result


def test_sync_with_sections_skip(tmp_path):
    src_root = tmp_path / "src"
    dest_root = tmp_path / "dest"
    src_root.mkdir()
    dest_root.mkdir()

    src_content = """\
# === DO_NOT_EDIT: path-sync standard ===
source
# === OK_EDIT: path-sync standard ==="""
    (src_root / "file.sh").write_text(src_content)

    dest_file = dest_root / "file.sh"
    dest_content = add_header(
        """\
# === DO_NOT_EDIT: path-sync standard ===
keep this
# === OK_EDIT: path-sync standard ===""",
        dest_file,
        CONFIG_NAME,
    )
    dest_file.write_text(dest_content)

    dest = _make_dest(skip_sections={"file.sh": ["standard"]})
    mapping = PathMapping(src_path="file.sh")
    changes, _ = _sync_path(mapping, src_root, dest_root, dest, CONFIG_NAME, False, False)

    assert changes == 0
    assert "keep this" in (dest_root / "file.sh").read_text()


def test_ensure_repo_dry_run_errors_if_missing(tmp_path):
    dest = _make_dest()
    dest_root = tmp_path / "missing_repo"
    with pytest.raises(ValueError, match="dry-run"):
        ensure_repo(dest, dest_root, dry_run=True)


def test_copy_options_defaults():
    opts = CopyOptions()
    assert not opts.dry_run
    assert not opts.force_overwrite
    assert not opts.no_checkout
    assert not opts.skip_commit
    assert not opts.no_prompt
    assert not opts.no_pr


def test_source_with_header_no_duplicate(tmp_path):
    """Source files with headers should not get double headers in destination."""
    src_root = tmp_path / "src"
    dest_root = tmp_path / "dest"
    src_root.mkdir()
    dest_root.mkdir()

    # Source file already has a header (e.g., template repo uses path-sync itself)
    src_file = src_root / "file.py"
    src_content_with_header = add_header("content", src_file, "original-config")
    src_file.write_text(src_content_with_header)

    mapping = PathMapping(src_path="file.py")
    changes, synced = _sync_path(mapping, src_root, dest_root, _make_dest(), CONFIG_NAME, False, False)

    assert changes == 1
    assert dest_root / "file.py" in synced
    result = (dest_root / "file.py").read_text()

    # Should have exactly one header line with CONFIG_NAME, not two headers
    header_count = result.count("path-sync copy -n")
    assert header_count == 1, f"Expected 1 header, got {header_count}. Content:\n{result}"
    assert f"path-sync copy -n {CONFIG_NAME}" in result
    assert "original-config" not in result


def test_file_mode_replace_no_header(tmp_path):
    src_root = tmp_path / "src"
    dest_root = tmp_path / "dest"
    src_root.mkdir()
    dest_root.mkdir()

    (src_root / "LICENSE").write_text("MIT License")
    (dest_root / "LICENSE").write_text("old license")

    mapping = PathMapping(src_path="LICENSE", sync_mode=SyncMode.REPLACE)
    changes, _ = _sync_path(mapping, src_root, dest_root, _make_dest(), CONFIG_NAME, False, False)

    assert changes == 1
    result = (dest_root / "LICENSE").read_text()
    assert result == "MIT License"
    assert not has_header(result)


def test_file_mode_replace_skips_unchanged(tmp_path):
    src_root = tmp_path / "src"
    dest_root = tmp_path / "dest"
    src_root.mkdir()
    dest_root.mkdir()

    (src_root / "LICENSE").write_text("MIT License")
    (dest_root / "LICENSE").write_text("MIT License")

    mapping = PathMapping(src_path="LICENSE", sync_mode=SyncMode.REPLACE)
    changes, _ = _sync_path(mapping, src_root, dest_root, _make_dest(), CONFIG_NAME, False, False)

    assert changes == 0


def test_file_mode_scaffold_creates_new(tmp_path):
    src_root = tmp_path / "src"
    dest_root = tmp_path / "dest"
    src_root.mkdir()
    dest_root.mkdir()

    (src_root / ".gitignore").write_text("*.pyc")

    mapping = PathMapping(src_path=".gitignore", sync_mode=SyncMode.SCAFFOLD)
    changes, _ = _sync_path(mapping, src_root, dest_root, _make_dest(), CONFIG_NAME, False, False)

    assert changes == 1
    result = (dest_root / ".gitignore").read_text()
    assert result == "*.pyc"
    assert not has_header(result)


def test_file_mode_scaffold_skips_existing(tmp_path):
    src_root = tmp_path / "src"
    dest_root = tmp_path / "dest"
    src_root.mkdir()
    dest_root.mkdir()

    (src_root / ".gitignore").write_text("new content")
    (dest_root / ".gitignore").write_text("user customized")

    mapping = PathMapping(src_path=".gitignore", sync_mode=SyncMode.SCAFFOLD)
    changes, _ = _sync_path(mapping, src_root, dest_root, _make_dest(), CONFIG_NAME, False, False)

    assert changes == 0
    assert (dest_root / ".gitignore").read_text() == "user customized"


def test_sync_new_file_respects_skip_sections(tmp_path):
    """New file should not include skipped sections."""
    src_root = tmp_path / "src"
    dest_root = tmp_path / "dest"
    src_root.mkdir()
    dest_root.mkdir()

    src_content = """\
# === DO_NOT_EDIT: path-sync included ===
keep this
# === OK_EDIT: path-sync included ===
# === DO_NOT_EDIT: path-sync skipped ===
skip this
# === OK_EDIT: path-sync skipped ==="""
    (src_root / "file.sh").write_text(src_content)

    dest = _make_dest(skip_sections={"file.sh": ["skipped"]})
    mapping = PathMapping(src_path="file.sh")
    changes, _ = _sync_path(mapping, src_root, dest_root, dest, CONFIG_NAME, False, False)

    assert changes == 1
    result = (dest_root / "file.sh").read_text()
    assert "keep this" in result
    assert "skipped" not in result
    assert "skip this" not in result


MODULE = run_verify_steps.__module__


def test_verify_steps_empty_returns_passed():
    mock_repo = MagicMock(spec=Repo)
    verify = VerifyConfig(steps=[])
    result = run_verify_steps(mock_repo, Path("/tmp"), verify)
    assert result.status == VerifyStatus.PASSED
    assert not result.failures


def test_verify_steps_all_pass(tmp_path: Path):
    mock_repo = MagicMock(spec=Repo)
    verify = VerifyConfig(steps=[VerifyStep(run="echo hello"), VerifyStep(run="true")])
    result = run_verify_steps(mock_repo, tmp_path, verify)
    assert result.status == VerifyStatus.PASSED
    assert not result.failures


def test_verify_step_fails_with_skip_strategy(tmp_path: Path):
    mock_repo = MagicMock(spec=Repo)
    verify = VerifyConfig(
        on_fail=OnFailStrategy.SKIP,
        steps=[VerifyStep(run="false")],
    )
    result = run_verify_steps(mock_repo, tmp_path, verify)
    assert result.status == VerifyStatus.SKIPPED
    assert len(result.failures) == 1
    assert result.failures[0].on_fail == OnFailStrategy.SKIP


def test_verify_step_fails_with_fail_strategy(tmp_path: Path):
    mock_repo = MagicMock(spec=Repo)
    verify = VerifyConfig(
        on_fail=OnFailStrategy.FAIL,
        steps=[VerifyStep(run="false")],
    )
    result = run_verify_steps(mock_repo, tmp_path, verify)
    assert result.status == VerifyStatus.FAILED
    assert len(result.failures) == 1
    assert result.failures[0].on_fail == OnFailStrategy.FAIL


def test_verify_step_fails_with_warn_strategy_continues(tmp_path: Path):
    mock_repo = MagicMock(spec=Repo)
    verify = VerifyConfig(
        on_fail=OnFailStrategy.WARN,
        steps=[VerifyStep(run="false"), VerifyStep(run="echo after-warn")],
    )
    result = run_verify_steps(mock_repo, tmp_path, verify)
    assert result.status == VerifyStatus.WARN
    assert len(result.failures) == 1
    assert result.failures[0].on_fail == OnFailStrategy.WARN


def test_verify_step_with_commit_stages_and_commits(tmp_path: Path):
    mock_repo = MagicMock(spec=Repo)
    verify = VerifyConfig(
        steps=[
            VerifyStep(
                run="echo format",
                commit=CommitConfig(message="style: format", add_paths=[".", "!.venv"]),
            )
        ]
    )
    git_ops_module = git_ops.stage_and_commit.__module__
    with patch(f"{git_ops_module}.{git_ops.stage_and_commit.__name__}") as mock_stage:
        result = run_verify_steps(mock_repo, tmp_path, verify)
        assert result.status == VerifyStatus.PASSED
        mock_stage.assert_called_once_with(mock_repo, [".", "!.venv"], "style: format")


def test_verify_per_step_on_fail_overrides_verify_level(tmp_path: Path):
    mock_repo = MagicMock(spec=Repo)
    verify = VerifyConfig(
        on_fail=OnFailStrategy.FAIL,
        steps=[VerifyStep(run="false", on_fail=OnFailStrategy.WARN)],
    )
    result = run_verify_steps(mock_repo, tmp_path, verify)
    assert result.status == VerifyStatus.WARN
    assert len(result.failures) == 1
    assert result.failures[0].on_fail == OnFailStrategy.WARN


def test_wrap_synced_files_wraps_content_in_section(tmp_path):
    src_root = tmp_path / "src"
    dest_root = tmp_path / "dest"
    src_root.mkdir()
    dest_root.mkdir()

    (src_root / "file.py").write_text("def hello(): pass")

    mapping = PathMapping(src_path="file.py")
    changes, _ = _sync_path(
        mapping, src_root, dest_root, _make_dest(), CONFIG_NAME, False, False, wrap_synced_files=True
    )

    assert changes == 1
    result = (dest_root / "file.py").read_text()
    assert "# === DO_NOT_EDIT: path-sync synced ===" in result
    assert "def hello(): pass" in result
    assert "# === OK_EDIT: path-sync synced ===" in result


def test_wrap_per_path_override_disables(tmp_path):
    src_root = tmp_path / "src"
    dest_root = tmp_path / "dest"
    src_root.mkdir()
    dest_root.mkdir()

    (src_root / "file.py").write_text("content")

    mapping = PathMapping(src_path="file.py", wrap=False)
    changes, _ = _sync_path(
        mapping, src_root, dest_root, _make_dest(), CONFIG_NAME, False, False, wrap_synced_files=True
    )

    assert changes == 1
    result = (dest_root / "file.py").read_text()
    assert "DO_NOT_EDIT" not in result


def test_wrapped_file_preserves_dest_content_around_section(tmp_path):
    src_root = tmp_path / "src"
    dest_root = tmp_path / "dest"
    src_root.mkdir()
    dest_root.mkdir()

    (src_root / "file.py").write_text("managed content v2")

    dest_file = dest_root / "file.py"
    dest_content = add_header(
        """\
# before section
# === DO_NOT_EDIT: path-sync synced ===
managed content v1
# === OK_EDIT: path-sync synced ===
# after section""",
        dest_file,
        CONFIG_NAME,
    )
    dest_file.write_text(dest_content)

    mapping = PathMapping(src_path="file.py")
    changes, _ = _sync_path(
        mapping, src_root, dest_root, _make_dest(), CONFIG_NAME, False, False, wrap_synced_files=True
    )

    assert changes == 1
    result = (dest_root / "file.py").read_text()
    assert "# before section" in result
    assert "managed content v2" in result
    assert "managed content v1" not in result
    assert "# after section" in result


def test_file_with_existing_sections_not_double_wrapped(tmp_path):
    src_root = tmp_path / "src"
    dest_root = tmp_path / "dest"
    src_root.mkdir()
    dest_root.mkdir()

    src_content = """\
# === DO_NOT_EDIT: path-sync standard ===
already has sections
# === OK_EDIT: path-sync standard ==="""
    (src_root / "file.sh").write_text(src_content)

    mapping = PathMapping(src_path="file.sh")
    changes, _ = _sync_path(
        mapping, src_root, dest_root, _make_dest(), CONFIG_NAME, False, False, wrap_synced_files=True
    )

    assert changes == 1
    result = (dest_root / "file.sh").read_text()
    assert result.count("DO_NOT_EDIT") == 1
    assert "synced" not in result


def test_sync_preserves_dest_trailing_when_adding_new_sections(tmp_path):
    """When source has new sections not in dest, dest's trailing content is preserved."""
    src_root = tmp_path / "src"
    dest_root = tmp_path / "dest"
    src_root.mkdir()
    dest_root.mkdir()

    src_content = """\
# === DO_NOT_EDIT: path-sync core ===
core content
# === OK_EDIT: path-sync core ===

# === DO_NOT_EDIT: path-sync checks ===
checks content
# === OK_EDIT: path-sync checks ===
# source trailing
src-recipe:
    echo source
"""
    (src_root / "justfile").write_text(src_content)

    dest_content = add_header(
        """\
# === DO_NOT_EDIT: path-sync standard ===
old standard
# === OK_EDIT: path-sync standard ===
# dest trailing
dest-recipe:
    echo dest
""",
        dest_root / "justfile",
        CONFIG_NAME,
    )
    (dest_root / "justfile").write_text(dest_content)

    mapping = PathMapping(src_path="justfile")
    changes, _ = _sync_path(mapping, src_root, dest_root, _make_dest(), CONFIG_NAME, False, False)

    assert changes == 1
    result = (dest_root / "justfile").read_text()
    assert "core content" in result
    assert "checks content" in result
    assert "old standard" not in result
    assert "dest trailing" in result
    assert "dest-recipe" in result
    assert "source trailing" not in result
    assert "src-recipe" not in result


COPY_MODULE = _close_stale_pr.__module__


def _make_src_config(**kwargs) -> SrcConfig:
    defaults = {"name": "test", "destinations": []}
    return SrcConfig.model_validate(defaults | kwargs)


def test_close_stale_pr_skipped_when_keep_pr_on_no_changes(tmp_path: Path):
    opts = CopyOptions()
    config = _make_src_config(keep_pr_on_no_changes=True)
    with patch(f"{COPY_MODULE}.git_ops") as mock_git:
        _close_stale_pr(tmp_path, "sync/test", opts, config)
        mock_git.has_open_pr.assert_not_called()


def test_close_stale_pr_skipped_when_skip_commit(tmp_path: Path):
    opts = CopyOptions(skip_commit=True)
    config = _make_src_config()
    with patch(f"{COPY_MODULE}.git_ops") as mock_git:
        _close_stale_pr(tmp_path, "sync/test", opts, config)
        mock_git.has_open_pr.assert_not_called()


def test_close_stale_pr_runs_by_default(tmp_path: Path):
    opts = CopyOptions()
    config = _make_src_config()
    with patch(f"{COPY_MODULE}.git_ops") as mock_git:
        mock_git.has_open_pr.return_value = True
        _close_stale_pr(tmp_path, "sync/test", opts, config)
        mock_git.close_pr.assert_called_once()


def test_skip_already_synced_bypassed_when_force_resync(tmp_path: Path):
    config = _make_src_config(force_resync=True)
    opts = CopyOptions()
    with patch(f"{COPY_MODULE}.git_ops") as mock_git:
        result = _skip_already_synced("dest", tmp_path, "sync/test", "2026-01-01T00:00:00", opts, config)
        assert not result
        mock_git.get_pr_body.assert_not_called()


def test_skip_already_synced_checks_by_default(tmp_path: Path):
    config = _make_src_config()
    opts = CopyOptions()
    body = "<!-- path-sync: sha=abc12345 ts=2099-01-01T00:00:00+00:00 -->"
    with patch(f"{COPY_MODULE}.git_ops") as mock_git:
        mock_git.get_pr_body.return_value = body
        result = _skip_already_synced("dest", tmp_path, "sync/test", "2026-01-01T00:00:00", opts, config)
        assert result


def _hint_setup(tmp_path):
    src_root = tmp_path / "src"
    dest_root = tmp_path / "dest"
    src_root.mkdir()
    dest_root.mkdir()
    Repo.init(src_root)
    Repo.init(dest_root)
    config = SrcConfig(name=CONFIG_NAME, paths=[PathMapping(src_path=".cursor", sync_mode=SyncMode.REPLACE)])
    dest = _make_dest(name="lz", dest_path_relative=".")
    (src_root / ".cursor/rules/foo.mdc").parent.mkdir(parents=True)
    (src_root / ".cursor/rules/foo.mdc").write_text("foo")
    (dest_root / ".cursor/rules/foo.mdc").parent.mkdir(parents=True)
    (dest_root / ".cursor/rules/foo.mdc").write_text("foo")
    return src_root, dest_root, config, dest


def test_copy_hint_with_dest_extra(capsys, tmp_path):
    src_root, dest_root, config, dest = _hint_setup(tmp_path)
    (dest_root / ".cursor/rules/bar.mdc").write_text("extra")
    _print_dest_only_hint(config, dest, src_root, dest_root)
    err = capsys.readouterr().err
    assert "1 dest-only file." in err
    assert f"path-sync pull -n {CONFIG_NAME} -d lz --dest-only" in err
    assert f"path-sync prune -n {CONFIG_NAME} -d lz" in err
    assert (dest_root / ".cursor/rules/bar.mdc").exists()


def test_copy_hint_absent_without_extra(capsys, tmp_path):
    src_root, dest_root, config, dest = _hint_setup(tmp_path)
    _print_dest_only_hint(config, dest, src_root, dest_root)
    assert capsys.readouterr().err == ""


def test_copy_hint_with_skip_orphan_cleanup(capsys, tmp_path):
    src_root, dest_root, config, dest = _hint_setup(tmp_path)
    (dest_root / ".cursor/rules/bar.mdc").write_text("extra")
    _print_dest_only_hint(config, dest, src_root, dest_root)
    assert "dest-only" in capsys.readouterr().err


def test_copy_hint_absent_for_opted_out_only(capsys, tmp_path):
    src_root = tmp_path / "src"
    dest_root = tmp_path / "dest"
    src_root.mkdir()
    dest_root.mkdir()
    Repo.init(src_root)
    Repo.init(dest_root)
    config = SrcConfig(name=CONFIG_NAME, paths=[PathMapping(src_path="justfile")])
    dest = _make_dest(name="lz", dest_path_relative=".")
    (dest_root / "justfile").write_text("opted out")
    _print_dest_only_hint(config, dest, src_root, dest_root)
    assert capsys.readouterr().err == ""


def _write_config(src_root: Path, config: SrcConfig) -> None:
    config_path = resolve_config_path(src_root, config.name)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(dump_yaml_model(config))


def _init_dest(tmp_path: Path, name: str) -> Path:
    dest_root = tmp_path / name
    dest_root.mkdir()
    Repo.init(dest_root)
    return dest_root


def _invoke_copy(src_root: Path):
    args = ["copy", "-n", CONFIG_NAME, "--src-root", str(src_root)]
    return runner.invoke(app, [*args, "--local", "--no-checkout", "--no-prompt", "--detailed-exit-code"])


TABLES_SRC = """\
# === DO_NOT_EDIT: path-sync tables ===
[alpha]
a = 1
# === OK_EDIT: path-sync tables ===

# === DO_NOT_EDIT: path-sync tables ===
[beta]
b = 2
# === OK_EDIT: path-sync tables ===

# === DO_NOT_EDIT: path-sync tables ===
[gamma]
c = 3
# === OK_EDIT: path-sync tables ===
"""

TABLES_DEST_OWN_CONTENT = """\
# dest preamble
# === DO_NOT_EDIT: path-sync tables ===
[alpha]
a = 0
# === OK_EDIT: path-sync tables ===
# dest between parts
# === DO_NOT_EDIT: path-sync tables ===
[beta]
b = 0
# === OK_EDIT: path-sync tables ===

# === DO_NOT_EDIT: path-sync tables ===
[gamma]
c = 0
# === OK_EDIT: path-sync tables ===
# dest trailer
"""

TABLES_DEST_OWN_CONTENT_SYNCED = """\
# dest preamble
# === DO_NOT_EDIT: path-sync tables ===
[alpha]
a = 1
# === OK_EDIT: path-sync tables ===
# dest between parts
# === DO_NOT_EDIT: path-sync tables ===
[beta]
b = 2
# === OK_EDIT: path-sync tables ===

# === DO_NOT_EDIT: path-sync tables ===
[gamma]
c = 3
# === OK_EDIT: path-sync tables ===
# dest trailer
"""


def test_copy_twice_second_run_reports_no_changes(tmp_path, tmp_repo):
    """One section id repeated around several blocks (parts), with and without dest-owned content."""
    (tmp_repo / "settings.toml").write_text(TABLES_SRC)
    (tmp_repo / "plain.py").write_text("print('hello')\n")
    config = SrcConfig(
        name=CONFIG_NAME,
        paths=[PathMapping(src_path="settings.toml"), PathMapping(src_path="plain.py")],
        destinations=[
            _make_dest(name="fresh", dest_path_relative="../fresh"),
            _make_dest(name="custom", dest_path_relative="../custom"),
        ],
    )
    _write_config(tmp_repo, config)
    fresh_root = _init_dest(tmp_path, "fresh")
    custom_root = _init_dest(tmp_path, "custom")
    custom_settings = custom_root / "settings.toml"
    custom_settings.write_text(add_header(TABLES_DEST_OWN_CONTENT, custom_settings, CONFIG_NAME))

    first = _invoke_copy(tmp_repo)

    assert first.exit_code == 1
    fresh_settings = fresh_root / "settings.toml"
    assert fresh_settings.read_text() == add_header(TABLES_SRC, fresh_settings, CONFIG_NAME)
    assert custom_settings.read_text() == add_header(TABLES_DEST_OWN_CONTENT_SYNCED, custom_settings, CONFIG_NAME)
    synced_files = [root / name for root in (fresh_root, custom_root) for name in ("settings.toml", "plain.py")]
    assert all(has_header(path.read_text()) for path in synced_files)
    after_first = {path: path.read_bytes() for path in synced_files}

    second = _invoke_copy(tmp_repo)

    assert second.exit_code == 0
    assert {path: path.read_bytes() for path in synced_files} == after_first


def test_sync_with_sections_skips_opted_out_file(tmp_path):
    """A dest file without the header is left alone even if it has matching fences."""
    src_root = tmp_path / "src"
    dest_root = tmp_path / "dest"
    src_root.mkdir()
    dest_root.mkdir()

    src_content = """\
# === DO_NOT_EDIT: path-sync standard ===
new recipe
# === OK_EDIT: path-sync standard ==="""
    (src_root / "file.sh").write_text(src_content)

    dest_file = dest_root / "file.sh"
    dest_file.write_text(
        """\
# === DO_NOT_EDIT: path-sync standard ===
old recipe
# === OK_EDIT: path-sync standard ===
# my custom stuff"""
    )
    before = dest_file.read_bytes()

    mapping = PathMapping(src_path="file.sh")
    changes, _ = _sync_path(mapping, src_root, dest_root, _make_dest(), CONFIG_NAME, False, False)

    assert changes == 0
    assert dest_file.read_bytes() == before


def test_sync_with_sections_force_overwrite_syncs_opted_out_file(tmp_path):
    src_root = tmp_path / "src"
    dest_root = tmp_path / "dest"
    src_root.mkdir()
    dest_root.mkdir()

    src_content = """\
# === DO_NOT_EDIT: path-sync standard ===
new recipe
# === OK_EDIT: path-sync standard ==="""
    (src_root / "file.sh").write_text(src_content)

    dest_file = dest_root / "file.sh"
    dest_file.write_text(
        """\
# === DO_NOT_EDIT: path-sync standard ===
old recipe
# === OK_EDIT: path-sync standard ===
# my custom stuff"""
    )

    mapping = PathMapping(src_path="file.sh")
    changes, _ = _sync_path(mapping, src_root, dest_root, _make_dest(), CONFIG_NAME, False, True)

    assert changes == 1
    result = dest_file.read_text()
    assert has_header(result)
    assert "new recipe" in result
    assert "old recipe" not in result
    assert "# my custom stuff" in result


def test_include_groups_write_only_named_groups(tmp_path, tmp_repo):
    for name in ("common.py", "lint.py", "docs.py"):
        (tmp_repo / name).write_text(f"# {name}\n")
    config = SrcConfig(
        name=CONFIG_NAME,
        paths=[PathMapping(src_path="common.py")],
        path_groups={
            "lint": [PathMapping(src_path="lint.py")],
            "docs": [PathMapping(src_path="docs.py")],
        },
        destinations=[
            _make_dest(name="lint-only", dest_path_relative="../lint-only", include_groups=["lint"]),
            _make_dest(name="docs-only", dest_path_relative="../docs-only", include_groups=["docs"]),
            _make_dest(name="no-groups", dest_path_relative="../no-groups"),
        ],
    )
    _write_config(tmp_repo, config)
    dest_roots = {dest.name: _init_dest(tmp_path, dest.name) for dest in config.destinations}

    result = _invoke_copy(tmp_repo)

    assert result.exit_code == 1
    written = {name: sorted(path.name for path in root.glob("*.py")) for name, root in dest_roots.items()}
    assert written == {
        "lint-only": ["common.py", "lint.py"],
        "docs-only": ["common.py", "docs.py"],
        "no-groups": ["common.py"],
    }


def test_replace_mode_overwrites_file_with_unknown_comment_syntax(tmp_path):
    src_root = tmp_path / "src"
    dest_root = tmp_path / "dest"
    src_root.mkdir()
    dest_root.mkdir()

    hook = "#!/bin/sh\nexit 0\n"
    (src_root / "pre-commit").write_text(hook)
    (dest_root / "pre-commit").write_text("#!/bin/sh\nexit 1\n")

    mapping = PathMapping(src_path="pre-commit", sync_mode=SyncMode.REPLACE)
    changes, _ = _sync_path(mapping, src_root, dest_root, _make_dest(), CONFIG_NAME, False, False)

    assert changes == 1
    assert (dest_root / "pre-commit").read_text() == hook


@pytest.mark.parametrize("filename", ["pre-commit", "snippets.code-snippets"])
def test_sync_mode_skips_file_with_unknown_comment_syntax(tmp_path, caplog, filename):
    src_root = tmp_path / "src"
    dest_root = tmp_path / "dest"
    src_root.mkdir()
    dest_root.mkdir()

    (src_root / filename).write_text("#!/bin/sh\nexit 0\n")

    mapping = PathMapping(src_path=filename)
    with caplog.at_level("WARNING"):
        changes, _ = _sync_path(mapping, src_root, dest_root, _make_dest(), CONFIG_NAME, False, False)

    assert changes == 0
    assert not (dest_root / filename).exists()
    assert "No comment config" in caplog.text


def test_sync_deletes_extra_dest_parts(tmp_path, caplog):
    src_root = tmp_path / "src"
    dest_root = tmp_path / "dest"
    src_root.mkdir()
    dest_root.mkdir()

    src_content = """\
# === DO_NOT_EDIT: path-sync tables ===
[alpha]
a = 1
# === OK_EDIT: path-sync tables ==="""
    (src_root / "settings.toml").write_text(src_content)

    dest_file = dest_root / "settings.toml"
    dest_file.write_text(
        add_header(
            """\
# dest preamble
# === DO_NOT_EDIT: path-sync tables ===
[alpha]
a = 0
# === OK_EDIT: path-sync tables ===

# === DO_NOT_EDIT: path-sync tables ===
[beta]
b = 0
# === OK_EDIT: path-sync tables ===
# dest trailer""",
            dest_file,
            CONFIG_NAME,
        )
    )

    mapping = PathMapping(src_path="settings.toml")
    with caplog.at_level("WARNING"):
        changes, _ = _sync_path(mapping, src_root, dest_root, _make_dest(), CONFIG_NAME, False, False)

    assert changes == 1
    result = dest_file.read_text()
    assert has_header(result)
    assert "a = 1" in result
    assert "a = 0" not in result
    assert "[beta]" not in result
    assert "b = 0" not in result
    assert result.count("DO_NOT_EDIT: path-sync tables") == 1
    assert "# dest preamble" in result
    assert "# dest trailer" in result
    assert "Deleting extra dest part 1 for section 'tables'" in caplog.text


def test_sync_creates_missing_dest_file_in_new_folder_with_header(tmp_path):
    src_root = tmp_path / "src"
    dest_root = tmp_path / "dest"
    (src_root / ".github/workflows").mkdir(parents=True)
    dest_root.mkdir()

    src_content = """\
# === DO_NOT_EDIT: path-sync standard ===
name: ci
# === OK_EDIT: path-sync standard ==="""
    (src_root / ".github/workflows/ci.yml").write_text(src_content)

    mapping = PathMapping(src_path=".github/workflows/ci.yml")
    changes, synced = _sync_path(mapping, src_root, dest_root, _make_dest(), CONFIG_NAME, False, False)

    dest_file = dest_root / ".github/workflows/ci.yml"
    assert changes == 1
    assert synced == {dest_file}
    assert dest_file.read_text() == add_header(src_content, dest_file, CONFIG_NAME)
    assert dest_file.read_text().startswith(f"# path-sync copy -n {CONFIG_NAME}\n")
