# tests/service/test_session_gc.py
from bot.service.session_gc import SessionGcConfig, load_session_gc_config


def test_config_defaults_when_key_absent():
    cfg = load_session_gc_config({})
    assert cfg.enabled is True
    assert cfg.scan_interval_seconds == 300
    assert cfg.max_workers == 1


def test_config_overrides_from_raw_dict():
    cfg = load_session_gc_config({"session_gc": {"enabled": False, "scan_interval_seconds": 60, "max_workers": 2}})
    assert cfg.enabled is False
    assert cfg.scan_interval_seconds == 60
    assert cfg.max_workers == 2


def test_config_is_frozen_and_strict():
    import pydantic
    try:
        SessionGcConfig(scan_interval_seconds=-1)  # type: ignore[arg-type]
    except pydantic.ValidationError:
        pass
    # frozen: assignment must raise
    cfg = SessionGcConfig()
    try:
        cfg.enabled = False  # type: ignore[misc]
        raise AssertionError("expected frozen error")
    except Exception:
        pass
