from pathlib import Path

import pytest

from path_sync._internal.models import (
    DEFAULT_EXCLUDE_DIRS,
    Destination,
    HeaderConfig,
    PathMapping,
    PRDefaults,
    SrcConfig,
    SyncMetadata,
    SyncMode,
    parse_sync_metadata,
    pr_already_synced,
)
from path_sync._internal.yaml_utils import load_yaml_model


def _dest(name: str, include_groups: list[str] | None = None) -> Destination:
    return Destination(
        name=name,
        dest_path_relative=f"code/{name}",
        include_groups=include_groups or [],
    )


def _config(
    path_groups: dict[str, list[PathMapping]] | None = None,
    destinations: list[Destination] | None = None,
) -> SrcConfig:
    return SrcConfig(
        name="test",
        paths=[PathMapping(src_path="shared.txt")],
        path_groups=path_groups or {},
        destinations=destinations or [],
    )


def test_resolve_paths_no_groups():
    config = _config()
    dest = _dest("a")
    assert config.resolve_paths(dest) is config.paths


def test_resolve_paths_with_groups():
    region_paths = [PathMapping(src_path="regions/dev.txt"), PathMapping(src_path="regions/prod.txt")]
    config = _config(
        path_groups={"regions": region_paths},
        destinations=[_dest("a", include_groups=["regions"]), _dest("b")],
    )
    a_paths = config.resolve_paths(config.destinations[0])
    assert len(a_paths) == 3
    assert a_paths[0].src_path == "shared.txt"
    assert a_paths[1].src_path == "regions/dev.txt"

    b_paths = config.resolve_paths(config.destinations[1])
    assert b_paths is config.paths


def test_resolve_paths_multiple_groups():
    region_paths = [PathMapping(src_path="regions/dev.txt")]
    extra_paths = [PathMapping(src_path="extras/lint.cfg")]
    config = _config(
        path_groups={"regions": region_paths, "extras": extra_paths},
        destinations=[_dest("a", include_groups=["regions", "extras"])],
    )
    paths = config.resolve_paths(config.destinations[0])
    assert len(paths) == 3
    assert [p.src_path for p in paths] == ["shared.txt", "regions/dev.txt", "extras/lint.cfg"]


def test_validation_rejects_unknown_group():
    with pytest.raises(ValueError, match="unknown path_group 'missing'"):
        _config(destinations=[_dest("a", include_groups=["missing"])])


def test_validation_rejects_duplicate_group():
    with pytest.raises(ValueError, match="duplicate include_group 'regions'"):
        _config(
            path_groups={"regions": [PathMapping(src_path="r.txt")]},
            destinations=[_dest("a", include_groups=["regions", "regions"])],
        )


def _load(tmp_path: Path, config_yaml: str) -> SrcConfig:
    config_path = tmp_path / "test.src.yaml"
    config_path.write_text(config_yaml)
    return load_yaml_model(config_path, SrcConfig)


@pytest.mark.parametrize(
    ("field", "default"),
    [
        pytest.param("paths", [], id="paths"),
        pytest.param("path_groups", {}, id="path_groups"),
        pytest.param("destinations", [], id="destinations"),
        pytest.param("header_config", HeaderConfig(), id="header_config"),
        pytest.param("pr_defaults", PRDefaults(), id="pr_defaults"),
    ],
)
def test_null_src_config_field_loads_as_default(tmp_path: Path, field: str, default: object):
    config = _load(tmp_path, f"name: t\n{field}:\n")
    assert getattr(config, field) == default


@pytest.mark.parametrize(
    ("field", "default"),
    [
        pytest.param("include_groups", [], id="include_groups"),
        pytest.param("skip_sections", {}, id="skip_sections"),
        pytest.param("skip_file_patterns", set(), id="skip_file_patterns"),
    ],
)
def test_null_destination_field_loads_as_default(tmp_path: Path, field: str, default: object):
    config = _load(tmp_path, f"name: t\ndestinations:\n  - name: d\n    dest_path_relative: ../d\n    {field}:\n")
    assert getattr(config.destinations[0], field) == default


@pytest.mark.parametrize(
    ("field", "default"),
    [
        pytest.param("dest_path", "", id="dest_path"),
        pytest.param("sync_mode", SyncMode.SYNC, id="sync_mode"),
        pytest.param("exclude_dirs", DEFAULT_EXCLUDE_DIRS, id="exclude_dirs"),
        pytest.param("exclude_file_patterns", set(), id="exclude_file_patterns"),
    ],
)
def test_null_path_mapping_field_loads_as_default(tmp_path: Path, field: str, default: object):
    config = _load(tmp_path, f"name: t\npaths:\n  - src_path: a.txt\n    {field}:\n")
    assert getattr(config.paths[0], field) == default


@pytest.mark.parametrize(
    "config_yaml",
    [
        pytest.param("name:\n", id="src_config_name"),
        pytest.param("name: t\ndestinations:\n  - name: d\n    dest_path_relative:\n", id="dest_path_relative"),
        pytest.param("name: t\npaths:\n  - src_path:\n", id="src_path"),
    ],
)
def test_null_required_field_still_errors(tmp_path: Path, config_yaml: str):
    with pytest.raises(ValueError, match="Input should be a valid string"):
        _load(tmp_path, config_yaml)


def test_parse_sync_metadata_from_default_template():
    body = "<!-- path-sync: sha=abcd1234 ts=2026-02-12T10:00:00+00:00 -->\nSynced from ..."
    result = parse_sync_metadata(body)
    assert result == SyncMetadata(sha="abcd1234", ts="2026-02-12T10:00:00+00:00")


def test_parse_sync_metadata_missing():
    assert parse_sync_metadata("Just a regular PR body") is None
    assert parse_sync_metadata("") is None


def test_format_body_includes_metadata():
    pr = PRDefaults()
    body = pr.format_body(
        src_repo_url="https://github.com/org/repo",
        src_sha="abcdef1234567890",
        sync_log="synced 3 files",
        dest_name="my-dest",
        src_commit_ts="2026-02-12T10:00:00+00:00",
    )
    meta = parse_sync_metadata(body)
    assert meta == SyncMetadata(sha="abcdef12", ts="2026-02-12T10:00:00+00:00")
    assert "Synced from [repo]" in body


def test_pr_already_synced_newer_skips():
    body = "<!-- path-sync: sha=abcd1234 ts=2026-02-12T10:00:00+00:00 -->\nSynced from ..."
    result = pr_already_synced(body, "2026-02-12T09:00:00+00:00")
    assert result == SyncMetadata(sha="abcd1234", ts="2026-02-12T10:00:00+00:00")


def test_pr_already_synced_equal_skips():
    body = "<!-- path-sync: sha=abcd1234 ts=2026-02-12T10:00:00+00:00 -->\nSynced from ..."
    assert pr_already_synced(body, "2026-02-12T10:00:00+00:00")


def test_pr_already_synced_older_proceeds():
    body = "<!-- path-sync: sha=abcd1234 ts=2026-02-12T09:00:00+00:00 -->\nSynced from ..."
    assert not pr_already_synced(body, "2026-02-12T10:00:00+00:00")


def test_pr_already_synced_no_body():
    assert not pr_already_synced(None, "2026-02-12T10:00:00+00:00")
    assert not pr_already_synced("", "2026-02-12T10:00:00+00:00")


def test_pr_already_synced_no_metadata():
    assert not pr_already_synced("Just a regular PR body", "2026-02-12T10:00:00+00:00")


def test_format_body_roundtrip_custom_template():
    pr = PRDefaults(body_template="<!-- path-sync: sha={src_sha_short} ts={src_commit_ts} -->\nCustom {dest_name}")
    body = pr.format_body(
        src_repo_url="https://github.com/org/repo",
        src_sha="abcdef1234567890",
        sync_log="",
        dest_name="test",
        src_commit_ts="2026-01-01T00:00:00+00:00",
    )
    meta = parse_sync_metadata(body)
    assert meta
    assert meta.ts == "2026-01-01T00:00:00+00:00"
