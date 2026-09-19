"""PA-06: personal_assistant config domain — typed schema, persistence, isolation.

Exercises the REAL ConfigDomain persistence path (loader/dumper/atomic write)
over temp assembly roots, plus the generic ConfigController read/write surface
the REST routes use. No mocks over the domain machinery.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from bot.config.domain import DomainFlavor, get_domain
from bot.config.domains.personal_assistant import (
    PersonalAssistantConfig,
    PersonalAssistantPreferences,
    normalize_workspace_path,
)
from bot.service.config_controller import ConfigController, FieldValidationError
from pydantic import ValidationError


def _owner(tmp_path: Path) -> PersonalAssistantPreferences:
    return PersonalAssistantPreferences(tmp_path / "personal_assistant.yml")


# ── Schema ──────────────────────────────────────────────────────────────────


def test_schema_defaults_are_null_workspace_and_default_pool() -> None:
    cfg = PersonalAssistantConfig()
    assert cfg.default_workspace is None
    assert cfg.default_pool == "default"


def test_schema_rejects_unknown_keys_and_bad_types() -> None:
    with pytest.raises(ValidationError):
        PersonalAssistantConfig(default_pool=None)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        PersonalAssistantConfig(default_pool="")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        PersonalAssistantConfig(default_workspace=123)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        PersonalAssistantConfig.model_validate({"default_pool": "x", "extra": 1})


# ── Path normalization ──────────────────────────────────────────────────────


def test_normalize_workspace_path_rejects_relative_nonexistent_and_files(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="absolute"):
        normalize_workspace_path("relative/dir")
    with pytest.raises(ValueError, match="exist"):
        normalize_workspace_path(str(tmp_path / "nope"))
    file_path = tmp_path / "f.txt"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="director"):
        normalize_workspace_path(str(file_path))


def test_normalize_workspace_path_resolves_to_canonical_directory(
    tmp_path: Path,
) -> None:
    target = tmp_path / "proj"
    target.mkdir()
    normalized = normalize_workspace_path(str(target))
    assert Path(normalized) == target.resolve()
    assert Path(normalized).is_absolute()


# ── Preferences owner over real temp roots ──────────────────────────────────


def test_missing_file_reads_defaults(tmp_path: Path) -> None:
    prefs = _owner(tmp_path)
    assert prefs.read() == PersonalAssistantConfig()
    assert prefs.preferred_pool() == "default"


def test_missing_file_is_not_written_by_read(tmp_path: Path) -> None:
    prefs = _owner(tmp_path)
    prefs.read()
    assert not (tmp_path / "personal_assistant.yml").exists()


def test_save_persists_and_reads_back(tmp_path: Path) -> None:
    prefs = _owner(tmp_path)
    target = tmp_path / "ws"
    target.mkdir()
    saved = prefs.save(default_workspace=str(target), default_pool="coder")
    assert saved.default_pool == "coder"
    assert Path(saved.default_workspace or "") == target.resolve()
    # persisted as canonical absolute string
    data = yaml.safe_load((tmp_path / "personal_assistant.yml").read_text("utf-8"))
    assert data["default_pool"] == "coder"
    assert data["default_workspace"] == str(target.resolve())
    # owner sees the saved value immediately (single runtime owner)
    assert prefs.read().default_pool == "coder"


def test_save_explicit_null_workspace_unsets(tmp_path: Path) -> None:
    prefs = _owner(tmp_path)
    target = tmp_path / "ws"
    target.mkdir()
    prefs.save(default_workspace=str(target), default_pool="coder")
    prefs.save(default_workspace=None, default_pool="coder")
    assert prefs.read().default_workspace is None


def test_save_invalid_workspace_path_rejected_and_file_unchanged(
    tmp_path: Path,
) -> None:
    prefs = _owner(tmp_path)
    prefs.save(default_workspace=None, default_pool="coder")
    before = (tmp_path / "personal_assistant.yml").read_text("utf-8")
    with pytest.raises(ValidationError):
        prefs.save(default_workspace=str(tmp_path / "ghost"), default_pool="review")
    assert (tmp_path / "personal_assistant.yml").read_text("utf-8") == before
    assert prefs.read().default_pool == "coder"


def test_failed_save_leaves_runtime_view_unchanged(tmp_path: Path) -> None:
    prefs = _owner(tmp_path)
    prefs.save(default_workspace=None, default_pool="coder")
    with pytest.raises(ValidationError):
        prefs.save(default_workspace="relative", default_pool="review")
    assert prefs.preferred_pool() == "coder"


def test_malformed_yaml_preferred_pool_returns_none(tmp_path: Path) -> None:
    (tmp_path / "personal_assistant.yml").write_text(
        "default_pool: [1, 2]\n", encoding="utf-8"
    )
    prefs = _owner(tmp_path)
    with pytest.raises(ValidationError):
        prefs.read()
    # tolerant routing accessor: no silent fallback pool, explicit None
    assert prefs.preferred_pool() is None


def test_different_config_dirs_are_isolated(tmp_path: Path) -> None:
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    prefs_a = PersonalAssistantPreferences(dir_a / "personal_assistant.yml")
    prefs_b = PersonalAssistantPreferences(dir_b / "personal_assistant.yml")
    prefs_a.save(default_workspace=None, default_pool="coder")
    assert prefs_a.preferred_pool() == "coder"
    assert prefs_b.preferred_pool() == "default"
    assert not (dir_b / "personal_assistant.yml").exists()


# ── Generic ConfigController surface (the REST /api/config/{domain} path) ───


def test_domain_registered_and_singleton_flavor() -> None:
    prefs = PersonalAssistantPreferences(Path("unused-tmp-root") / "pa.yml")
    dom = prefs.domain
    assert dom is not None
    assert dom is prefs.domain
    assert dom.flavor is DomainFlavor.SINGLETON


def test_controller_read_returns_full_default_values(tmp_path: Path) -> None:
    prefs = _owner(tmp_path)
    payload = ConfigController(domains=(prefs.domain,)).read("personal_assistant")
    assert payload.domain == "personal_assistant"
    assert payload.flavor is DomainFlavor.SINGLETON
    # fresh install reports the full effective values, not an empty map
    assert payload.values == {
        "default_workspace": None,
        "default_pool": "default",
    }
    names = [f.name for f in payload.fields or []]
    assert names == ["default_workspace", "default_pool"]


def test_controller_write_persists_partial_payload(tmp_path: Path) -> None:
    prefs = _owner(tmp_path)
    payload = ConfigController(domains=(prefs.domain,)).write("personal_assistant", {"default_pool": "coder"})
    assert payload.values is not None
    assert payload.values["default_pool"] == "coder"
    assert payload.values["default_workspace"] is None  # preserved via merge
    # immediate effect: runtime default reflects the PUT without restart
    assert prefs.preferred_pool() == "coder"
    # immediate-effect domain: restart_required stays False after a write
    assert payload.restart_required is False


def test_controller_write_marked_immediate_no_restart_required(
    tmp_path: Path,
) -> None:
    """DESIGN: preference saves are immediate for new selections — unlike
    model/im config the payload must never report restart_required."""
    prefs = _owner(tmp_path)
    controller = ConfigController(domains=(prefs.domain,))
    payload = controller.write(
        "personal_assistant", {"default_pool": "coder", "default_workspace": None}
    )
    assert payload.restart_required is False
    again = controller.read("personal_assistant")
    assert again.restart_required is False


def test_controller_write_validates_workspace_path(tmp_path: Path) -> None:
    prefs = _owner(tmp_path)
    ctrl = ConfigController(domains=(prefs.domain,))
    with pytest.raises(FieldValidationError) as ei:
        ctrl.write("personal_assistant", {"default_workspace": "relative/path"})
    assert "default_workspace" in ei.value.errors
    # disk untouched
    assert prefs.preferred_pool() == "default"


def test_controller_write_rejects_unknown_fields(tmp_path: Path) -> None:
    prefs = _owner(tmp_path)
    with pytest.raises(FieldValidationError):
        ConfigController(domains=(prefs.domain,)).write("personal_assistant", {"theme": "dark"})


def test_controller_write_null_pool_is_validation_error(tmp_path: Path) -> None:
    prefs = _owner(tmp_path)
    with pytest.raises(FieldValidationError):
        ConfigController(domains=(prefs.domain,)).write("personal_assistant", {"default_pool": None})


def test_existing_model_and_im_domains_not_regressed(tmp_path: Path) -> None:
    from bot.config.domains import im as im_module  # noqa: F401 - registers on import
    from bot.config.domains import model as model_module  # noqa: F401

    assert get_domain("model") is model_module.model_domain
    assert get_domain("im") is im_module.im_domain


def test_two_controllers_keep_their_assembly_preferences(tmp_path: Path) -> None:
    first = PersonalAssistantPreferences(tmp_path / "first.yml")
    first_controller = ConfigController(domains=(first.domain,))
    second = PersonalAssistantPreferences(tmp_path / "second.yml")
    second_controller = ConfigController(domains=(second.domain,))
    first_controller.write("personal_assistant", {"default_pool": "coder"})
    assert first.preferred_pool() == "coder"
    assert second.preferred_pool() == "default"
    values = second_controller.read("personal_assistant").values
    assert values is not None and values["default_pool"] == "default"
