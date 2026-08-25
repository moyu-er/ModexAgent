"""Regression: the EXACT remote banner + prompt bytes from the round-2 report.

bash_input(password) succeeded (banner captured), yet _collect hung the full
480s session deadline — no stdin-wait evidence fired on the trailing remote
prompt ``root@iZbp14inxlh1m59lnehzkjZ:~# ``. These pin the detectors against
the verbatim strings.
"""

from __future__ import annotations

from modex_agent.tools.terminal._persistent_session import _looks_like_foreign_prompt
from modex_agent.tools.terminal.prompt import is_waiting_for_input

_BANNER = (
    "\r\nWelcome to Ubuntu 22.04.5 LTS (GNU/Linux 5.15.0-186-generic x86_64)\r\n"
    "\r\n * Documentation:  https://help.ubuntu.com/management/\r\n"
    "\r\nSystem load:  0.63               Processes:             143\r\n"
    "\r\nWelcome to Alibaba Cloud Elastic Compute Service !\r\n"
    "\r\n"
    "\r\n"
)

_REMOTE_PROMPT = "root@iZbp14inxlh1m59lnehzkjZ:~# "

# Modern bash (4.4+) readline enables bracketed paste right after the
# prompt; colored prompts add SGR runs — both land IN the prompt line.
_BRACKETED_PASTE = "\x1b[?2004h"
_COLORED_PROMPT = "\x1b[01;32mroot@iZbp14inxlh1m59lnehzkjZ\x1b[00m:\x1b[01;34m~\x1b[00m# "


def test_real_remote_prompt_detected_after_banner() -> None:
    assert _looks_like_foreign_prompt(_BANNER + _REMOTE_PROMPT) is True


def test_real_remote_prompt_with_bracketed_paste() -> None:
    """The round-2 hang: remote bash enables bracketed paste after the
    prompt — the escape bytes sit in the trailing line and must not blind
    the shape detector (the 480s-hang root cause)."""
    assert _looks_like_foreign_prompt(_BANNER + _REMOTE_PROMPT + _BRACKETED_PASTE) is True


def test_real_colored_remote_prompt_detected() -> None:
    """SGR-colored remote prompts (default on many cloud images) carry
    escape runs inside the prompt — still detected after stripping."""
    assert _looks_like_foreign_prompt(_BANNER + _COLORED_PROMPT + _BRACKETED_PASTE) is True


def test_real_remote_prompt_with_crlf_variants() -> None:
    assert _looks_like_foreign_prompt(_BANNER + _REMOTE_PROMPT + "\r\n") is True
    assert _looks_like_foreign_prompt(_BANNER + "\r" + _REMOTE_PROMPT) is True


def test_real_banner_alone_not_detected() -> None:
    assert _looks_like_foreign_prompt(_BANNER) is False
    assert is_waiting_for_input(_BANNER + _REMOTE_PROMPT) is False
