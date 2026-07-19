"""Tests for the ColumnProjection declarative field-mapping abstraction (ADR-0030).

Covers:
- split / assemble round-trip for all codec combinations
- IdentityCodec passthrough behavior
- ContentCodec round-trip for str, list[dict], and None
- Candidate-key priority: first hit wins on split; first key re-populated on assemble
- Residual JSON excludes ALL extracted candidate keys (not just the hit)
- Companion-column (is_content_json) consistency
- Empty dict and missing-key edge cases
"""

from __future__ import annotations

import json

import pytest

from modex_agent.persistence.column_projection import (
    ColumnCodec,
    ColumnField,
    ColumnProjection,
    ContentCodec,
    IdentityCodec,
)

# ── IdentityCodec ────────────────────────────────────────────────────────────


def test_identity_codec_encode_returns_single_column_dict() -> None:
    codec = IdentityCodec()
    assert codec.encode("role", "assistant") == {"role": "assistant"}


def test_identity_codec_decode_returns_single_sliced_value() -> None:
    """IdentityCodec.decode operates on a pre-sliced single-entry dict
    (ColumnProjection.assemble always pre-slices)."""
    codec = IdentityCodec()
    assert codec.decode({"role": "user"}) == "user"


def test_identity_codec_encode_none_value() -> None:
    codec = IdentityCodec()
    assert codec.encode("parent_id", None) == {"parent_id": None}


def test_identity_codec_decode_empty_slice_raises_stopiteration() -> None:
    """An empty slice is a caller bug — surface it loudly."""
    codec = IdentityCodec()
    with pytest.raises(StopIteration):
        codec.decode({})


# ── ContentCodec ─────────────────────────────────────────────────────────────


def test_content_codec_encode_str_sets_flag_zero() -> None:
    codec = ContentCodec()
    out = codec.encode("content", "hello world")
    assert out == {"content": "hello world", "is_content_json": 0}


def test_content_codec_encode_none_sets_flag_zero_and_null() -> None:
    codec = ContentCodec()
    out = codec.encode("content", None)
    assert out == {"content": None, "is_content_json": 0}


def test_content_codec_encode_list_of_dicts_serializes_and_sets_flag_one() -> None:
    codec = ContentCodec()
    payload: list[dict[str, str]] = [{"type": "text", "text": "hi"}]
    out = codec.encode("content", payload)
    assert out["is_content_json"] == 1
    assert out["content"] == json.dumps(payload)
    # The serialized form must round-trip via json.loads
    assert json.loads(out["content"]) == payload


def test_content_codec_decode_str_returns_str() -> None:
    codec = ContentCodec()
    columns = {"content": "plain text", "is_content_json": 0}
    assert codec.decode(columns) == "plain text"


def test_content_codec_decode_none_returns_none() -> None:
    codec = ContentCodec()
    columns = {"content": None, "is_content_json": 0}
    assert codec.decode(columns) is None


def test_content_codec_decode_json_list_returns_list() -> None:
    codec = ContentCodec()
    payload = [{"type": "text", "text": "hi"}]
    columns = {"content": json.dumps(payload), "is_content_json": 1}
    assert codec.decode(columns) == payload


def test_content_codec_round_trips_str() -> None:
    codec = ContentCodec()
    value = "a narrative string"
    encoded = codec.encode("content", value)
    assert codec.decode(encoded) == value


def test_content_codec_round_trips_none() -> None:
    codec = ContentCodec()
    value: list[dict[str, str]] | str | None = None
    encoded = codec.encode("content", value)
    assert codec.decode(encoded) is None


def test_content_codec_round_trips_list_of_dicts() -> None:
    codec = ContentCodec()
    value = [
        {"type": "text", "text": "first"},
        {"type": "text", "text": "second"},
    ]
    encoded = codec.encode("content", value)
    assert codec.decode(encoded) == value


# ── ColumnField / ColumnProjection basic shape ───────────────────────────────


def test_column_field_default_codec_is_none() -> None:
    field = ColumnField(column="role", dict_keys=("role",))
    assert field.codec is None


def test_column_projection_default_json_column_is_message_json() -> None:
    projection = ColumnProjection(fields=())
    assert projection.json_column == "message_json"


def test_column_codec_is_abstract() -> None:
    assert ColumnCodec.__abstractmethods__ == {"encode", "decode", "columns_for"}
    with pytest.raises(TypeError):
        ColumnCodec()  # type: ignore[abstract]


# ── split / assemble round-trip ──────────────────────────────────────────────


def test_split_returns_column_values_and_residual_json_string() -> None:
    projection = ColumnProjection(
        fields=(
            ColumnField(column="role", dict_keys=("role",)),
            ColumnField(column="content", dict_keys=("content",), codec=ContentCodec()),
        )
    )
    data = {"role": "user", "content": "hi", "metadata": {"x": 1}}

    columns, residual = projection.split(data)

    assert columns == {
        "role": "user",
        "content": "hi",
        "is_content_json": 0,
    }
    # Residual must be a JSON string and contain only the un-extracted key
    assert isinstance(residual, str)
    assert json.loads(residual) == {"metadata": {"x": 1}}


def test_split_then_assemble_round_trips_identity_codec() -> None:
    projection = ColumnProjection(
        fields=(
            ColumnField(column="role", dict_keys=("role",)),
            ColumnField(column="agent_name", dict_keys=("agent_name",)),
        )
    )
    data = {"role": "assistant", "agent_name": "main", "extra": "keep me"}

    columns, residual = projection.split(data)
    reconstructed = projection.assemble(columns, residual)

    assert reconstructed == data


def test_split_then_assemble_round_trips_content_codec_with_str() -> None:
    projection = ColumnProjection(
        fields=(
            ColumnField(column="role", dict_keys=("role",)),
            ColumnField(column="content", dict_keys=("content",), codec=ContentCodec()),
        )
    )
    data = {"role": "user", "content": "a plain string", "keep": 42}

    columns, residual = projection.split(data)
    reconstructed = projection.assemble(columns, residual)

    assert reconstructed == data


def test_split_then_assemble_round_trips_content_codec_with_list() -> None:
    projection = ColumnProjection(
        fields=(
            ColumnField(column="role", dict_keys=("role",)),
            ColumnField(column="content", dict_keys=("content",), codec=ContentCodec()),
        )
    )
    data = {
        "role": "user",
        "content": [{"type": "text", "text": "hello"}],
        "keep": 42,
    }

    columns, residual = projection.split(data)
    reconstructed = projection.assemble(columns, residual)

    assert reconstructed == data


def test_split_then_assemble_round_trips_content_codec_with_none() -> None:
    projection = ColumnProjection(
        fields=(
            ColumnField(column="role", dict_keys=("role",)),
            ColumnField(column="content", dict_keys=("content",), codec=ContentCodec()),
        )
    )
    data: dict[str, object] = {"role": "user", "content": None, "keep": 42}

    columns, residual = projection.split(data)
    reconstructed = projection.assemble(columns, residual)

    assert reconstructed == data


def test_split_then_assemble_round_trips_mixed_codecs() -> None:
    projection = ColumnProjection(
        fields=(
            ColumnField(column="role", dict_keys=("role",)),
            ColumnField(column="agent_name", dict_keys=("agent_name",)),
            ColumnField(column="content", dict_keys=("content",), codec=ContentCodec()),
            ColumnField(column="parent_id", dict_keys=("parent_id",)),
        )
    )
    data = {
        "role": "assistant",
        "agent_name": "main",
        "content": [{"type": "text", "text": "response"}],
        "parent_id": "abc123",
        "metadata": {"turn": 1, "tags": ["a", "b"]},
    }

    columns, residual = projection.split(data)
    reconstructed = projection.assemble(columns, residual)

    assert reconstructed == data


# ── Candidate-key priority ───────────────────────────────────────────────────


def test_split_first_candidate_key_wins() -> None:
    """When multiple candidate keys are present, the first one in dict_keys
    supplies the value; all candidates are still stripped from residual."""
    projection = ColumnProjection(
        fields=(
            ColumnField(column="role", dict_keys=("role", "r")),
        )
    )
    # Both candidates present — "role" wins because it's first in dict_keys
    data = {"role": "user", "r": "should-be-ignored", "keep": "x"}

    columns, residual = projection.split(data)

    assert columns == {"role": "user"}
    # BOTH candidate keys must be stripped from the residual
    assert json.loads(residual) == {"keep": "x"}


def test_split_second_candidate_key_used_when_first_absent() -> None:
    projection = ColumnProjection(
        fields=(
            ColumnField(column="role", dict_keys=("role", "r")),
        )
    )
    data = {"r": "user", "keep": "x"}

    columns, residual = projection.split(data)

    assert columns == {"role": "user"}
    assert json.loads(residual) == {"keep": "x"}


def test_assemble_reinjects_under_first_candidate_key_only() -> None:
    projection = ColumnProjection(
        fields=(
            ColumnField(column="role", dict_keys=("role", "r")),
        )
    )
    columns = {"role": "user"}
    residual = json.dumps({"keep": "x"})

    reconstructed = projection.assemble(columns, residual)

    # The value is re-injected only under the first candidate key
    assert reconstructed == {"role": "user", "keep": "x"}
    assert "r" not in reconstructed


def test_assemble_does_not_emit_other_candidate_keys() -> None:
    projection = ColumnProjection(
        fields=(
            ColumnField(column="role", dict_keys=("role", "r", "role_name")),
        )
    )
    columns = {"role": "assistant"}
    residual = json.dumps({"x": 1})

    reconstructed = projection.assemble(columns, residual)

    assert reconstructed == {"role": "assistant", "x": 1}
    assert "r" not in reconstructed
    assert "role_name" not in reconstructed


# ── Residual JSON consistency ────────────────────────────────────────────────


def test_split_removes_all_candidate_keys_even_when_only_one_present() -> None:
    projection = ColumnProjection(
        fields=(
            ColumnField(column="role", dict_keys=("role", "r", "role_name")),
            ColumnField(column="content", dict_keys=("content", "c"), codec=ContentCodec()),
        )
    )
    data = {"r": "user", "content": "hi", "keep": "x"}

    columns, residual = projection.split(data)

    residual_dict = json.loads(residual)
    assert "role" not in residual_dict
    assert "r" not in residual_dict
    assert "role_name" not in residual_dict
    assert "content" not in residual_dict
    assert "c" not in residual_dict
    assert residual_dict == {"keep": "x"}


def test_split_residual_extracts_companion_column_too() -> None:
    """The companion column (is_content_json) must never appear in the residual
    JSON — it lives only in the columns dict."""
    projection = ColumnProjection(
        fields=(
            ColumnField(column="content", dict_keys=("content",), codec=ContentCodec()),
        )
    )
    data = {"content": [{"x": 1}], "keep": "y"}

    columns, residual = projection.split(data)

    assert "is_content_json" not in json.loads(residual)
    assert columns["is_content_json"] == 1


# ── Edge cases: empty / missing keys ─────────────────────────────────────────


def test_split_empty_dict_yields_empty_columns_and_empty_json() -> None:
    projection = ColumnProjection(
        fields=(
            ColumnField(column="role", dict_keys=("role",)),
            ColumnField(column="content", dict_keys=("content",), codec=ContentCodec()),
        )
    )
    columns, residual = projection.split({})

    assert columns == {}
    assert json.loads(residual) == {}


def test_assemble_empty_columns_and_empty_json_yields_empty_dict() -> None:
    projection = ColumnProjection(
        fields=(
            ColumnField(column="role", dict_keys=("role",)),
        )
    )
    reconstructed = projection.assemble({}, json.dumps({}))
    assert reconstructed == {}


def test_split_field_with_no_matching_key_emits_nothing() -> None:
    projection = ColumnProjection(
        fields=(
            ColumnField(column="role", dict_keys=("role",)),
            ColumnField(column="content", dict_keys=("content",), codec=ContentCodec()),
        )
    )
    data = {"unrelated": "x"}

    columns, residual = projection.split(data)

    # Neither field matched, so no columns emitted and no companion flag
    assert columns == {}
    assert json.loads(residual) == {"unrelated": "x"}


def test_assemble_field_with_no_column_value_skipped() -> None:
    projection = ColumnProjection(
        fields=(
            ColumnField(column="role", dict_keys=("role",)),
            ColumnField(column="content", dict_keys=("content",), codec=ContentCodec()),
        )
    )
    # Only `role` column present; `content` column absent
    columns = {"role": "user"}
    residual = json.dumps({"keep": "x"})

    reconstructed = projection.assemble(columns, residual)

    assert reconstructed == {"role": "user", "keep": "x"}
    assert "content" not in reconstructed


# ── Custom json_column name ──────────────────────────────────────────────────


def test_custom_json_column_name() -> None:
    projection = ColumnProjection(
        fields=(
            ColumnField(column="role", dict_keys=("role",)),
        ),
        json_column="payload",
    )
    data = {"role": "user", "extra": "x"}

    columns, residual = projection.split(data)

    assert columns == {"role": "user"}
    assert json.loads(residual) == {"extra": "x"}


def test_assemble_with_custom_json_column_name() -> None:
    projection = ColumnProjection(
        fields=(
            ColumnField(column="role", dict_keys=("role",)),
        ),
        json_column="payload",
    )
    columns = {"role": "user"}
    residual = json.dumps({"keep": "x"})

    reconstructed = projection.assemble(columns, residual)

    assert reconstructed == {"role": "user", "keep": "x"}
