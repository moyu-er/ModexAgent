"""Contract tests for shared Markdown YAML frontmatter parsing."""

import yaml

from modex_agent.utils.frontmatter import parse_frontmatter


def test_parses_bom_prefixed_frontmatter_and_normalizes_newlines() -> None:
    frontmatter, body = parse_frontmatter(
        "\ufeff---\r\nname: weather\rdescription: Forecasts\r\n---\r\n# Weather\r\nBody\r\n"
    )

    assert frontmatter == {"name": "weather", "description": "Forecasts"}
    assert body == "# Weather\nBody\n"


def test_non_mapping_yaml_returns_empty_mapping_and_body() -> None:
    frontmatter, body = parse_frontmatter("---\n- one\n- two\n---\nBody\n")

    assert frontmatter == {}
    assert body == "Body\n"


def test_malformed_yaml_returns_empty_mapping_and_body() -> None:
    frontmatter, body = parse_frontmatter("---\nname: [unterminated\n---\nBody\n")

    assert frontmatter == {}
    assert body == "Body\n"


def test_unclosed_frontmatter_returns_empty_mapping_and_normalized_original() -> None:
    text = "---\r\nname: weather\r\nBody\r\n"

    frontmatter, body = parse_frontmatter(text)

    assert frontmatter == {}
    assert body == "---\nname: weather\nBody\n"


def test_opening_fence_must_be_exact() -> None:
    text = "----\nname: weather\n---\nBody\n"

    frontmatter, body = parse_frontmatter(text)

    assert frontmatter == {}
    assert body == text


def test_yaml_boolean_resolution_uses_yaml_12_tokens() -> None:
    frontmatter, _ = parse_frontmatter(
        "---\nexplicit: true\nlegacy-token: yes\nquoted: \"true\"\n---\nBody\n"
    )

    assert frontmatter == {
        "explicit": True,
        "legacy-token": "yes",
        "quoted": "true",
    }


def test_yaml_12_loader_does_not_mutate_pyyaml_safe_loader() -> None:
    assert yaml.safe_load("legacy: yes\n") == {"legacy": True}


def test_empty_frontmatter_returns_empty_mapping_and_body() -> None:
    frontmatter, body = parse_frontmatter("---\n---\nBody\n")

    assert frontmatter == {}
    assert body == "Body\n"
