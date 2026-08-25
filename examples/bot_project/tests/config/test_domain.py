from __future__ import annotations

from pathlib import Path
from typing import Annotated

from bot.config.domain import (
    ConfigDomain,
    DomainFlavor,
    FieldDescriptor,
    FieldType,
    RestartMarker,
    Secret,
    SecretMask,
    atomic_write,
    describe,
    get_domain,
    mask,
    merge,
    register_domain,
)
from pydantic import BaseModel, ConfigDict, ValidationError


class Inner(BaseModel):
    note: str
    token: Annotated[str, Secret()]


class Outer(BaseModel):
    name: str
    secret: Annotated[str, Secret()]
    inner: Inner
    inners: list[Inner]
    flag: bool = False


def test_mask_replaces_secret_fields_with_secretmask() -> None:
    data = {
        "name": "n",
        "secret": "abcd1234",
        "inner": {"note": "x", "token": "tok12345"},
        "inners": [{"note": "a", "token": "t1"}, {"note": "b", "token": ""}],
        "flag": True,
    }
    out = mask(Outer, data)
    assert out["name"] == "n"
    assert isinstance(out["secret"], SecretMask)
    assert out["secret"] == SecretMask(has_value=True, hint="••••••••1234")
    assert out["inner"]["token"] == SecretMask(has_value=True, hint="••••••••2345")
    assert out["inners"][0]["token"].has_value is True
    assert out["inners"][1]["token"] == SecretMask(has_value=False, hint="")
    assert out["flag"] is True


def test_merge_secret_write_semantics() -> None:
    current = {
        "name": "n",
        "secret": "orig",
        "inner": {"note": "x", "token": "tok"},
        "inners": [],
        "flag": False,
    }

    # omitted → keep
    kept = merge(Outer, current, {"name": "n2"})
    assert kept["secret"] == "orig"
    assert kept["inner"]["token"] == "tok"
    assert kept["name"] == "n2"

    # echoed SecretMask-like dict {has_value,hint} → keep current
    echoed = merge(Outer, current, {"secret": {"has_value": True, "hint": "x"}})
    assert echoed["secret"] == "orig"

    # explicit {value: ...} → overwrite
    overwritten = merge(Outer, current, {"secret": {"value": "new"}})
    assert overwritten["secret"] == "new"

    # {set: False} → clear
    cleared = merge(Outer, current, {"secret": {"set": False}})
    assert cleared["secret"] == ""


def test_describe_returns_field_descriptors_with_enum_types() -> None:
    desc = describe(Outer)
    assert isinstance(desc, list) and isinstance(desc[0], FieldDescriptor)
    by_name = {d.name: d for d in desc}
    assert by_name["secret"].type is FieldType.SECRET
    assert by_name["flag"].type is FieldType.BOOLEAN
    assert by_name["name"].type is FieldType.STRING
    assert by_name["inner"].type is FieldType.OBJECT
    assert by_name["inners"].type is FieldType.LIST


def test_singleton_read_masks_and_write_marks_restart(tmp_path: Path) -> None:
    yml = tmp_path / "sing.yml"
    yml.write_text("name: hello\nkey: topsecret\n", encoding="utf-8")

    class S(BaseModel):
        name: str
        key: Annotated[str, Secret()]

    dom = ConfigDomain(
        name="sing",
        label="Sing",
        yaml_path=yml,
        flavor=DomainFlavor.SINGLETON,
        root_schema=S,
    )
    values, schema, restart_required = dom.read()
    assert values["name"] == "hello"
    assert isinstance(values["key"], SecretMask) and values["key"].has_value
    assert any(f.name == "key" and f.type is FieldType.SECRET for f in schema)
    assert restart_required is False

    dom.write({"name": "world", "key": {"value": "newsecret"}})
    values, _, restart_required = dom.read()
    assert values["name"] == "world"
    assert restart_required is True


def test_atomic_write_always_advances_mtime(tmp_path: Path) -> None:
    """Regression (Windows clock-tick race): back-to-back writes can land in
    the same ~15.6ms timer tick, leaving the mtime unchanged — the restart
    marker then missed real writes intermittently. ``atomic_write`` must
    guarantee the mtime strictly advances every write."""
    yml = tmp_path / "tick.yml"
    yml.write_text("a: 0\n", encoding="utf-8")
    marker = RestartMarker()
    marker.capture(yml)

    for i in range(1, 4):
        atomic_write(yml, f"a: {i}\n")
        assert marker.is_modified(yml), f"write #{i} did not advance the mtime"
        marker.capture(yml)


def test_singleton_validation_error_does_not_touch_disk(tmp_path: Path) -> None:
    yml = tmp_path / "strict.yml"
    original = "name: keep\nkey: orig\n"
    yml.write_text(original, encoding="utf-8")

    class Strict(BaseModel):
        model_config = ConfigDict(frozen=True, extra="forbid")
        name: str
        key: Annotated[str, Secret()]

    dom = ConfigDomain(
        name="strict",
        label="Strict",
        yaml_path=yml,
        flavor=DomainFlavor.SINGLETON,
        root_schema=Strict,
    )

    try:
        dom.write({"name": "ok", "bogus": "rejected"})
    except ValidationError:
        pass
    else:  # pragma: no cover - the write must raise
        raise AssertionError("expected ValidationError for unknown field")

    assert yml.read_text(encoding="utf-8") == original


def test_registry_assembles_sections_and_masks_secrets(tmp_path: Path) -> None:
    yml = tmp_path / "reg.yml"
    yml.write_text(
        'primary:\n  name: main\n  token: tok-main\nsecondary:\n  name: sec\n  token: ""\n',
        encoding="utf-8",
    )

    class Channel(BaseModel):
        name: str
        token: Annotated[str, Secret()]

    dom = ConfigDomain(
        name="channels",
        label="Channels",
        yaml_path=yml,
        flavor=DomainFlavor.REGISTRY,
    )
    dom.register_kind("primary", Channel, label="Primary")
    dom.register_kind("secondary", Channel, label="Secondary")

    sections, restart_required = dom.read_registry()
    assert restart_required is False
    primary = sections["primary"]
    assert primary.label == "Primary"
    assert primary.values["name"] == "main"
    assert isinstance(primary.values["token"], SecretMask) and primary.values["token"].has_value
    assert isinstance(sections["secondary"].values["token"], SecretMask)
    assert sections["secondary"].values["token"].has_value is False
    assert any(f.name == "token" and f.type is FieldType.SECRET for f in primary.fields)

    # missing optional kind section tolerated
    dom.register_kind("optional", Channel, label="Optional")
    sections, _ = dom.read_registry()
    assert sections["optional"].values == {}


def test_register_domain_and_get_domain_round_trip() -> None:
    dom = ConfigDomain(
        name="rd",
        label="RD",
        yaml_path=Path("ignored.yml"),
        flavor=DomainFlavor.SINGLETON,
    )
    register_domain(dom)
    assert get_domain("rd") is dom
    assert get_domain("nope") is None
