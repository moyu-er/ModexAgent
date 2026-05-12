from framework.ioc.merge import deep_merge


class TestDeepMerge:
    def test_scalar_override(self) -> None:
        assert deep_merge({"a": 1}, {"a": 2}) == {"a": 2}

    def test_nested_dict_merge(self) -> None:
        base: dict[str, object] = {"a": {"b": 1, "c": 2}}  # type: ignore[dict-item]
        override: dict[str, object] = {"a": {"b": 10}}  # type: ignore[dict-item]
        assert deep_merge(base, override) == {"a": {"b": 10, "c": 2}}

    def test_none_clears_key(self) -> None:
        base: dict[str, object] = {"a": 1, "b": 2}
        override: dict[str, object] = {"a": None}
        assert deep_merge(base, override) == {"b": 2}

    def test_list_is_replaced_not_merged(self) -> None:
        base: dict[str, object] = {"items": [1, 2, 3]}
        override: dict[str, object] = {"items": [4]}
        assert deep_merge(base, override) == {"items": [4]}

    def test_override_none_returns_base_copy(self) -> None:
        base: dict[str, object] = {"a": 1}
        assert deep_merge(base, None) == {"a": 1}
        assert deep_merge(base, None) is not base

    def test_override_adds_new_key(self) -> None:
        base: dict[str, object] = {"a": 1}
        override: dict[str, object] = {"b": 2}
        assert deep_merge(base, override) == {"a": 1, "b": 2}
