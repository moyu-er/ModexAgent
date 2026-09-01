"""Phase-guard / evidence-fusion / interrupt tests for the persistent pair.

Companion to ``test_persistent_bash.py`` (real-protocol suite): these pin
the interactive-contract behaviors — the two-phase guard (bash vs
bash_input), the ``[hint: ...]`` advisory suffix on stdin-wait returns,
OR-fused stdin-wait evidence (probe miss must not starve the content
fallback), foreign-marker stripping, and ``^C`` interrupt translation.

Same real-pexpect discipline, but every test is time-boxed: the kernel
probe interval is monkeypatched down to 0.3s and commands are short, so
the whole file runs in seconds. Skipped off POSIX.
"""

from __future__ import annotations

import asyncio
import importlib.util
import shutil
import sys
from time import monotonic

import pytest

import modex_agent.tools.terminal._persistent_session as session_mod
from modex_agent.tools.terminal._foreground_probe import (
    stdin_probe_available as _probe_available,
)
from modex_agent.tools.terminal._persistent_session import PersistentShellSession
from modex_agent.tools.terminal.persistent_bash import BashInputTool, PersistentBashTool

_HAS_PERSISTENT_BASH = (
    sys.platform != "win32"
    and shutil.which("bash") is not None
    and importlib.util.find_spec("pexpect") is not None
)

pytestmark = pytest.mark.skipif(
    not _HAS_PERSISTENT_BASH, reason="persistent bash requires POSIX pexpect + /bin/bash"
)


@pytest.fixture(autouse=True)
def _fast_probe_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    """Probe cadence 3s → 0.3s: stdin-wait verdicts arrive in test time."""
    monkeypatch.setattr(session_mod, "_STDIN_PROBE_INTERVAL_S", 0.3)


# ── phase guard: bash while WAITING is rejected, session stays consistent ──


async def test_bash_rejected_while_waiting_and_input_still_completes():
    """THE user-report regression: a second bash call over a stdin-waiting
    command must be REJECTED (its wrapper would be eaten as input); the
    pending transaction stays answerable via bash_input afterwards."""
    tool = PersistentBashTool(timeout_seconds=10)
    bash_input = BashInputTool(tool.manager)
    try:
        out = await tool.execute(command="read -p 'Password: ' X; echo got=$X")
        assert "Password:" in out
        busy = await tool.execute(command="echo hi")
        assert busy.startswith("[Error]")
        assert "bash_input" in busy
        resumed = await bash_input.execute(line="yes")
        assert resumed.strip() == "got=yes"
        assert await tool.execute(command="echo healthy") == "healthy"
    finally:
        await tool.close()


async def test_bash_input_rejected_when_idle():
    """IDLE bash_input keeps its clear error (no pending transaction)."""
    tool = PersistentBashTool(timeout_seconds=10)
    try:
        assert "[Error]" in await BashInputTool(tool.manager).execute(line="x")
    finally:
        await tool.close()


# ── [hint: ...] advisory suffix on stdin-wait returns ──


async def test_waiting_return_carries_hint_suffix():
    """The stdin-wait return keeps the RAW prompt output and appends one
    advisory line suggesting bash_input — advisory, not a verdict."""
    tool = PersistentBashTool(timeout_seconds=10)
    try:
        started = monotonic()
        out = await tool.execute(command="read -p 'Continue? ' X; echo got=$X")
        assert "Continue?" in out
        assert out.rstrip().endswith("]")
        assert "[hint:" in out
        assert "bash_input" in out
        assert monotonic() - started < 5.0
    finally:
        await tool.close()


async def test_hint_separated_from_output_by_blank_line():
    """The advisory is never glued to the tool's own output: exactly one
    blank line separates the hint from the body (user-reported readability)."""
    tool = PersistentBashTool(timeout_seconds=10)
    bash_input = BashInputTool(tool.manager)
    try:
        out = await tool.execute(command="read -p 'Password: ' X; echo got=$X")
        assert "Password: \n\n[hint:" in out
        assert (await bash_input.execute(line="pw")).strip() == "got=pw"
    finally:
        await tool.close()


async def test_completed_command_has_no_hint():
    """Normal marker completion never carries the advisory."""
    tool = PersistentBashTool(timeout_seconds=10)
    try:
        out = await tool.execute(command="echo done")
        assert out == "done"
        assert "[hint:" not in out
    finally:
        await tool.close()


# ── OR-fused evidence: probe miss must not starve content detection ──


async def test_probe_miss_still_caught_by_content(monkeypatch: pytest.MonkeyPatch):
    """The Linux /dev/tty blind spot (ssh/sudo read from a non-zero tty fd
    the probe cannot see): probe reports available-but-not-waiting, and the
    keyword content evidence still surfaces the prompt."""
    monkeypatch.setattr(session_mod, "stdin_probe_available", lambda: True)

    async def _probe_miss(self: PersistentShellSession) -> bool:
        return False

    monkeypatch.setattr(PersistentShellSession, "_probe_stdin_wait", _probe_miss)
    tool = PersistentBashTool(timeout_seconds=10)
    bash_input = BashInputTool(tool.manager)
    try:
        out = await tool.execute(command="read -p 'Password: ' X; echo got=$X")
        assert "Password:" in out
        assert "[hint:" in out
        assert (await bash_input.execute(line="pw")).strip() == "got=pw"
    finally:
        await tool.close()


async def test_probe_misreport_suppressed_while_streaming(monkeypatch: pytest.MonkeyPatch):
    """The ssh-select misreport shape (probe ALWAYS claims stdin-wait):
    while output streams denser than the settle window the verdict is
    suppressed and the command completes via its marker."""
    monkeypatch.setattr(session_mod, "stdin_probe_available", lambda: True)

    async def _probe_misreports(self: PersistentShellSession) -> bool:
        return True

    monkeypatch.setattr(PersistentShellSession, "_probe_stdin_wait", _probe_misreports)
    tool = PersistentBashTool(timeout_seconds=10)
    try:
        out = await tool.execute(command="for i in $(seq 1 15); do echo l$i; sleep 0.1; done")
        assert out.splitlines()[-1] == "l15"
        assert "[hint:" not in out
    finally:
        await tool.close()


# ── prompt-shape evidence: non-keyword prompts (REPL / remote shells) ──


_FAKE_SSH = __file__.rsplit("/", 1)[0] + "/_fake_ssh_prompt.py"


async def test_ssh_shaped_program_full_interactive_flow():
    """THE round-2 user report: an ssh-shaped program (password read from
    /dev/tty, then a BRACKETED remote prompt ``[root@host ~]#``) must be
    answerable end-to-end — password accepted, banner returned promptly,
    commands served, exit closing the transaction back to IDLE."""
    tool = PersistentBashTool(timeout_seconds=8)
    bash_input = BashInputTool(tool.manager)
    try:
        started = monotonic()
        out = await tool.execute(command=f"python3 {_FAKE_SSH} pw")
        assert "password:" in out
        assert "[hint:" in out
        resumed = await bash_input.execute(line="pw")
        elapsed = monotonic() - started
        assert "Welcome" in resumed
        assert "[hint:" in resumed
        assert elapsed < 7.0
        echoed = await bash_input.execute(line="hostname")
        assert "ECHO:hostname" in echoed
        assert "[hint:" in echoed
        await bash_input.execute(line="exit")
        assert await tool.execute(command="echo back") == "back"
    finally:
        await tool.close()


async def test_prompt_shape_detected_without_keywords(monkeypatch: pytest.MonkeyPatch):
    """A foreign prompt-shaped trailing line (fake REPL ``fake>``) with no
    keyword anywhere: during a pending transaction our prompt is the
    controlled __MODEX_PS1__ token, so any other prompt-shaped line is a
    foreground program's — the shape detector must surface it."""
    monkeypatch.setattr(session_mod, "stdin_probe_available", lambda: False)
    tool = PersistentBashTool(timeout_seconds=10)
    bash_input = BashInputTool(tool.manager)
    try:
        out = await tool.execute(command="printf 'fake> '; read X; echo got=$X")
        assert "fake>" in out
        assert "[hint:" in out
        assert (await bash_input.execute(line="hi")).strip() == "got=hi"
    finally:
        await tool.close()


async def test_suffix_shaped_output_advisory_fires_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
):
    """The gpt2-codegolf shape (tb21-all-v6) under the shared detector's
    documented Layer-2 contract: a slow compute-then-print command whose
    data lines end in ')' now surfaces the soft stdin-wait advisory at the
    settle window — even when the kernel probe is available and says
    nothing is blocked on stdin. prompt.py's Layer 2 (the prompt-shape
    suffix layer documented in terminal/AGENTS.md's "keyword ∪
    prompt-shape" evidence fusion) fires on the trailing ')' before the
    probe-gated weak path is consulted, so a probe veto now governs only
    the session-local shell shapes. The false positive is BY-DESIGN
    tolerable: the hint is soft-worded and the transaction stays
    recoverable — bash_input(^C) closes it and the next bash call just
    works (no deadlock, no lost session)."""
    monkeypatch.setattr(session_mod, "stdin_probe_available", lambda: True)

    async def _probe_says_not_waiting(self: PersistentShellSession) -> bool:
        return False

    monkeypatch.setattr(PersistentShellSession, "_probe_stdin_wait", _probe_says_not_waiting)
    tool = PersistentBashTool(timeout_seconds=10)
    bash_input = BashInputTool(tool.manager)
    try:
        out = await tool.execute(
            command="echo 'wpe [85056000..85842432): (-0.0007, 0.1227)'; sleep 3; echo tail"
        )
        assert "[hint:" in out
        resumed = await bash_input.execute(line="^C")
        assert "[hint:" not in resumed
        assert await tool.execute(command="echo healthy") == "healthy"
    finally:
        await tool.close()


async def test_weak_suffix_still_fires_when_probe_confirms(
    monkeypatch: pytest.MonkeyPatch,
):
    """A deliberately non-keyword suffix prompt still fires when the kernel
    probe positively confirms the stdin wait."""
    monkeypatch.setattr(session_mod, "stdin_probe_available", lambda: True)

    async def _probe_confirms(self: PersistentShellSession) -> bool:
        return True

    monkeypatch.setattr(PersistentShellSession, "_probe_stdin_wait", _probe_confirms)
    tool = PersistentBashTool(timeout_seconds=10)
    bash_input = BashInputTool(tool.manager)
    try:
        out = await tool.execute(command="read -p 'name: ' X; echo got=$X")
        assert "name:" in out
        assert "[hint:" in out
        assert (await bash_input.execute(line="y")).strip() == "got=y"
    finally:
        await tool.close()


def test_foreign_prompt_shape_matcher_unit():
    """The shape matcher: real prompt forms pass, output lines don't."""
    for line in ("root@aliyun:~#", "$", ">>>", "mysql>", "%", "user@box$"):
        assert session_mod._looks_like_foreign_prompt(line + "\n") is True
    for line in ("stuck?", "Continue?", "[A]llow?", "name:", "Enter choice)"):
        assert session_mod._looks_like_foreign_prompt(line + "\n") is True
    for line in (
        "[root@fakehost ~]#",
        "user@host:~ $",
        "mysql [(none)]>",
        "root@box:~# \x1b[?2004h",
        "[root@fakehost ~]# \x1b[?2004h",
    ):
        assert session_mod._looks_like_foreign_prompt(line) is True
    for line in ("total 12", "line2", "done", "progress 42%", "", "[WARNING]", "[OK]"):
        assert session_mod._looks_like_foreign_prompt(line + "\n") is False


# ── ^C interrupt translation ──


async def test_interrupt_line_closes_transaction_via_marker():
    """bash_input('^C') sends SIGINT: the foreground command dies, the
    wrapper's END marker still fires, and the transaction closes back to
    IDLE — a stuck session now has an escape hatch."""
    tool = PersistentBashTool(timeout_seconds=10)
    bash_input = BashInputTool(tool.manager)
    try:
        out = await tool.execute(command="read -p 'stuck? ' X; echo got=$X")
        assert "[hint:" in out
        resumed = await bash_input.execute(line="^C")
        # bash's read dies on SIGINT; the wrapper continues and completes.
        assert "__MODEX" not in resumed
        assert "[hint:" not in resumed
        assert await tool.execute(command="echo back") == "back"
    finally:
        await tool.close()


async def test_interrupt_variants_translated():
    """ctrl+c / raw \x03 land on the same path as ^C."""
    tool = PersistentBashTool(timeout_seconds=10)
    bash_input = BashInputTool(tool.manager)
    try:
        out = await tool.execute(command="read -p 'q1: ' A; echo a=$A")
        assert "[hint:" in out
        resumed = await bash_input.execute(line="ctrl+c")
        assert "[hint:" not in resumed
        out2 = await tool.execute(command="read -p 'q2: ' B; echo b=$B")
        assert "[hint:" in out2
        resumed2 = await bash_input.execute(line="\x03")
        assert "[hint:" not in resumed2
        assert await tool.execute(command="echo ok") == "ok"
    finally:
        await tool.close()


# ── WAITING-kind: interactive-shell passthrough (the round-4 report) ──


_FAKE_SSH_SHELL = __file__.rsplit("/", 1)[0] + "/_fake_ssh_shell.py"
_FAKE_APT_HOOK = __file__.rsplit("/", 1)[0] + "/_fake_apt_hook.py"


@pytest.mark.skipif(
    not _probe_available(), reason="apt-transient suppression needs the Linux probe"
)
async def test_transient_raw_juggle_completes_without_takeover():
    """THE tb21-all-v8 regression: apt/dpkg's debconf frontend flips the
    tty raw for ~0.7s at configure time while apt-get itself (not reading
    stdin) still owns the foreground. Mode evidence alone (CHILD_RAW +
    quiet + prior output) misread that transient as an interactive
    takeover; the early WAITING return then let the NEXT command's wrapper
    die inside the still-running apt, and the 480s deadline kill interrupted
    dpkg mid-configure — poisoning the verifier's own apt. A raw-mode child
    that the kernel probe shows is NOT blocked reading terminal input must
    keep collecting until its END marker."""
    tool = PersistentBashTool(timeout_seconds=10)
    try:
        out = await tool.execute(
            command=f"echo unpacking; python3 {_FAKE_APT_HOOK} 1.2; echo after-hook"
        )
        assert "unpacking" in out
        assert "after-hook" in out
        assert "[hint:" not in out
        assert await tool.execute(command="echo healthy") == "healthy"
    finally:
        await tool.close()


async def test_takeover_still_fires_for_stdin_reading_raw_child():
    """The suppression must not swallow REAL takeovers: a child that sets
    raw mode and blocks reading stdin (ssh/REPL/pager shape) still returns
    the shell-kind WAITING hint promptly."""
    tool = PersistentBashTool(timeout_seconds=8)
    bash_input = BashInputTool(tool.manager)
    try:
        started = monotonic()
        out = await tool.execute(
            command='python3 -c "import sys, tty; tty.setraw(0); sys.stdin.read(1)"'
        )
        elapsed = monotonic() - started
        assert "[hint:" in out
        assert elapsed < 5.0
        resumed = await bash_input.execute(line="q")
        assert "[hint:" not in resumed
        assert await tool.execute(command="echo healthy") == "healthy"
    finally:
        await tool.close()


async def test_bash_passes_through_after_remote_login():
    """THE round-4 report: bash(ssh) → bash_input(password) logs in (the
    banner proves both tools share the terminal) → a NEW bash call must
    PASS THROUGH to the remote shell (its wrapper executes remotely and
    closes via its own markers) — not be rejected as 'waiting for input'.
    An interactive SHELL is not an input prompt."""
    tool = PersistentBashTool(timeout_seconds=10)
    bash_input = BashInputTool(tool.manager)
    try:
        out = await tool.execute(command=f"python3 {_FAKE_SSH_SHELL} pw")
        assert "password:" in out
        assert "[hint:" in out
        resumed = await bash_input.execute(line="pw")
        assert "Welcome" in resumed
        assert "[hint:" in resumed
        assert "interactive shell" in resumed  # shell-kind hint, not "waiting for input"
        assert "waiting for interactive terminal input" not in resumed
        remote = await tool.execute(command="echo remote-run")
        assert remote == "remote-run"
        remote2 = await tool.execute(command="cd / && basename $PWD")
        assert remote2 == "/"
        await tool.execute(command="exit")
        assert await tool.execute(command="echo local-ok") == "local-ok"
    finally:
        await tool.close()


async def test_bash_recovers_from_misclassified_wait():
    """Mid-banner misclassification self-heals: the WAITING kind was frozen
    as prompt (a banner burst boundary that looked like an input prompt),
    the remote shell is really at its own prompt — a new bash call must
    reclassify from the buffered tail and PASS THROUGH, not deadlock on
    the guidance rejection (the round-5 live report)."""
    tool = PersistentBashTool(timeout_seconds=10)
    bash_input = BashInputTool(tool.manager)
    try:
        await tool.execute(command=f"python3 {_FAKE_SSH_SHELL} pw")
        await bash_input.execute(line="pw")  # WAITING, shell-kind
        session = tool.manager.session_for(None)
        session._waiting_shell = False  # noqa: SLF001 — simulate the misclassification
        remote = await tool.execute(command="echo recovered")
        assert remote == "recovered"
        await tool.execute(command="exit")
    finally:
        await tool.close()


async def test_bash_still_rejected_on_password_prompt():
    """A PASSWORD wait (keyword evidence) is a genuine input prompt — the
    wrapper would be eaten as the answer. Rejection with guidance stays."""
    tool = PersistentBashTool(timeout_seconds=10)
    bash_input = BashInputTool(tool.manager)
    try:
        out = await tool.execute(command="read -sp 'Password: ' X; echo ok=$X")
        assert "[hint:" in out
        busy = await tool.execute(command="echo nope")
        assert busy.startswith("[Error]")
        assert "bash_input" in busy
        assert (await bash_input.execute(line="pw")).strip() == "ok=pw"
    finally:
        await tool.close()


# ── silence is NOT evidence: silent commands wait for the deadline ──


async def test_silent_running_command_waits_for_deadline():
    """THE gpt2-codegolf kill chain, structurally: a silently-running
    foreground command (``curl -s`` / a big compile — the kernel probe is
    consulted and says not-waiting, output is quiet). Output silence is
    NOT stdin-wait evidence (by design: its tool layer never returns to
    the agent on silence): the call holds to the command deadline and
    returns the timeout message — never the settle hint, never a WAITING
    phase that blocks the next bash call."""
    tool = PersistentBashTool(timeout_seconds=16)
    try:
        started = monotonic()
        out = await tool.execute(command="sleep 30")
        elapsed = monotonic() - started
        assert "timed out after 16 seconds" in out
        assert "[hint:" not in out
        assert elapsed >= 15.5  # silence alone never early-returns
        # the deadline path terminated the session — no WAITING residue
        assert await tool.execute(command="echo fresh") == "fresh"
    finally:
        await tool.close()


async def test_probeless_silence_waits_for_deadline(monkeypatch: pytest.MonkeyPatch):
    """Same contract with the kernel probe UNAVAILABLE (the macOS
    content-fallback path): a fully silent ``read -s`` — zero output, no
    keywords, no probe. Silence is not evidence on ANY platform: the
    deadline is the only exit, the session resets, and the next call
    spawns fresh."""
    monkeypatch.setattr(session_mod, "stdin_probe_available", lambda: False)
    tool = PersistentBashTool(timeout_seconds=16)
    try:
        out = await tool.execute(command="read -s X; echo got=$X")
        assert "timed out after 16 seconds" in out
        assert "[hint:" not in out
        assert await tool.execute(command="echo fresh") == "fresh"
    finally:
        await tool.close()


# ── foreign-marker stripping (orphan `__DONE__/__MODEX_*` pollution) ──


async def test_foreign_markers_stripped_from_result():
    """THE `__DONE_xxx__=130` residue regression: marker-shaped lines a
    command PRINTS ITSELF (or an abandoned transaction left behind) never
    reach the model — only our own paired markers bound the output."""
    tool = PersistentBashTool(timeout_seconds=10)
    try:
        out = await tool.execute(
            command="echo before; echo '__DONE_deadbeef__=130'; "
            "echo '__MODEX_START_c0ffee42__'; echo '__MODEX_END_c0ffee42:7'; echo after"
        )
        assert "before" in out
        assert "after" in out
        assert "__DONE" not in out
        assert "__MODEX" not in out
    finally:
        await tool.close()


# ── cancellation hygiene ──


async def test_cancelled_command_recovers_session_via_on_cancel():
    """ADR-0048 D6: cancelling a run_command preserves the session; the
    tool's on_cancel hook interrupts the foreground command and drains it.
    The shell, its cwd, and its env survive — the next call reuses them."""
    tool = PersistentBashTool(timeout_seconds=10)
    try:
        await tool.execute(command="cd /tmp")
        task = asyncio.create_task(tool.execute(command="sleep 8"))
        await asyncio.sleep(0.4)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        await tool.on_cancel()

        session = tool.session
        assert session._phase is session_mod._Phase.IDLE  # noqa: SLF001
        assert session._proc is not None  # noqa: SLF001
        assert session._proc.isalive()  # noqa: SLF001
        assert await tool.execute(command="pwd") == "/tmp"
    finally:
        await tool.close()


# ── ordering invariant: session deadline strictly below executor deadline ──


def test_session_deadline_strictly_below_executor_default():
    """Layered-timeout ordering: the session's own graceful timeout path
    (partial output + reset notice) must be reachable BEFORE the executor's
    blind cancel — 480 < DefaultValues.TOOL_TIMEOUT_SECONDS (540). The
    bot's old hardcoded 400 inverted this and made every interactive hang
    a silent `<tool_timeout>` with all partial output destroyed."""
    from modex_agent.core.constants import DefaultValues

    assert session_mod._DEFAULT_TIMEOUT_SECONDS < DefaultValues.TOOL_TIMEOUT_SECONDS


async def test_raw_takeover_returns_promptly_zsh_prompt():
    tool = PersistentBashTool(timeout_seconds=8)
    bash_input = BashInputTool(tool.manager)
    try:
        started = monotonic()
        step1 = await tool.execute(command=f'python3 {_FAKE_SSH_SHELL} pw "user@server ~ % "')
        assert "password:" in step1
        assert "[hint:" in step1

        resumed = await bash_input.execute(line="pw")
        elapsed = monotonic() - started
        assert elapsed < 3.5
        assert "Welcome" in resumed
        assert "interactive shell" in resumed

        session = tool.manager.session_for(None)
        assert session._phase is session_mod._Phase.WAITING
        assert session._pending is not None
        assert session._proc is not None
        assert session._proc.isalive()

        assert await tool.execute(command="echo remote-run") == "remote-run"
        await tool.execute(command="exit")
        assert await tool.execute(command="echo local-ok") == "local-ok"
    finally:
        await tool.close()


async def test_raw_takeover_returns_promptly_fish_prompt():
    tool = PersistentBashTool(timeout_seconds=8)
    bash_input = BashInputTool(tool.manager)
    try:
        started = monotonic()
        step1 = await tool.execute(command=f'python3 {_FAKE_SSH_SHELL} pw "root@server ~> "')
        assert "password:" in step1
        assert "[hint:" in step1

        resumed = await bash_input.execute(line="pw")
        elapsed = monotonic() - started
        assert elapsed < 3.5
        assert "Welcome" in resumed
        assert "interactive shell" in resumed

        session = tool.manager.session_for(None)
        assert session._phase is session_mod._Phase.WAITING
        assert session._pending is not None
        assert session._proc is not None
        assert session._proc.isalive()

        assert await tool.execute(command="echo remote-run") == "remote-run"
        await tool.execute(command="exit")
        assert await tool.execute(command="echo local-ok") == "local-ok"
    finally:
        await tool.close()


async def test_pipeline_completion_not_truncated():
    tool = PersistentBashTool(timeout_seconds=8)
    try:
        output = await tool.execute(command="(echo one; sleep 2; echo two) | grep -v nomatch")

        assert "one" in output
        assert "two" in output
    finally:
        await tool.close()


async def test_local_nested_shell_passthrough():
    tool = PersistentBashTool(timeout_seconds=8)
    try:
        started = monotonic()
        output = await tool.execute(command="bash --noprofile --norc -i")
        elapsed = monotonic() - started
        assert elapsed < 3.5
        assert "interactive shell" in output

        assert await tool.execute(command="echo nested-run") == "nested-run"
        await tool.execute(command="exit")
        assert await tool.execute(command="echo local-ok") == "local-ok"
    finally:
        await tool.close()


@pytest.mark.skipif(
    not _probe_available(), reason="zero-output raw stdin waits require the Linux probe"
)
async def test_raw_nonshell_takeover_hint_probe_path(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(session_mod, "_STDIN_PROBE_INTERVAL_S", 3.0)
    tool = PersistentBashTool(timeout_seconds=8)
    bash_input = BashInputTool(tool.manager)
    try:
        started = monotonic()
        output = await tool.execute(
            command='python3 -c "import sys,tty,termios; tty.setraw(0); sys.stdin.read(1)"'
        )
        elapsed = monotonic() - started

        assert output.startswith("[no output]\n\n[hint:")
        assert 2.5 <= elapsed < 6.0

        resumed = await bash_input.execute(line="q")
        assert "[hint:" not in resumed
        assert tool.manager.session_for(None)._phase is session_mod._Phase.IDLE
    finally:
        await tool.close()


@pytest.mark.skipif(
    _probe_available(), reason="probe-less zero-output contract runs off Linux"
)
async def test_raw_nonshell_takeover_hint_gated_on_probeless():
    tool = PersistentBashTool(timeout_seconds=3)
    try:
        output = await tool.execute(
            command='python3 -c "import sys,tty,termios; tty.setraw(0); sys.stdin.read(1)"'
        )

        assert "timed out after 3 seconds" in output
        assert "[hint:" not in output
        assert await tool.execute(command="echo fresh") == "fresh"
    finally:
        await tool.close()


async def test_interrupt_byte_forwarded_under_takeover():
    tool = PersistentBashTool(timeout_seconds=8)
    bash_input = BashInputTool(tool.manager)
    try:
        await tool.execute(command=f'python3 {_FAKE_SSH_SHELL} pw "user@server ~ % "')
        await bash_input.execute(line="pw")

        await bash_input.execute(line="^C")
        session = tool.manager.session_for(None)
        assert session._proc is not None
        assert session._proc.isalive()
        assert await tool.execute(command="echo after-ctrlc") == "after-ctrlc"
    finally:
        await tool.close()


async def test_printed_ps1_token_not_treated_as_completion(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(session_mod, "_strip_ps1", lambda text: text)
    tool = PersistentBashTool(timeout_seconds=10)
    try:
        started = monotonic()
        out = await tool.execute(command="echo '__MODEX_PS1__'; sleep 3")
        elapsed = monotonic() - started

        assert elapsed >= 3.0
        assert "[hint:" not in out
        assert "__MODEX_PS1__" in out
        assert await tool.execute(command="echo healthy") == "healthy"
    finally:
        await tool.close()


async def test_probe_unavailable_keeps_legacy_ps1_behavior(
    monkeypatch: pytest.MonkeyPatch,
):
    real_terminal_state = PersistentShellSession._terminal_state
    monkeypatch.setattr(PersistentShellSession, "_terminal_state", lambda self: None)
    tool = PersistentBashTool(timeout_seconds=10)
    try:
        started = monotonic()
        await tool.execute(command="echo '__MODEX_PS1__'; sleep 3")
        elapsed = monotonic() - started

        assert elapsed < 2.5
        session = tool.manager.session_for(None)
        deadline = monotonic() + 5.0
        while real_terminal_state(session) is not session_mod._TerminalSignal.SHELL_READLINE:
            assert monotonic() < deadline
            await asyncio.sleep(0.025)
        assert await tool.execute(command="echo healthy") == "healthy"
    finally:
        await tool.close()


async def test_probe_hit_classifies_kind_by_kernel_state(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(session_mod, "stdin_probe_available", lambda: True)

    async def _probe(self: PersistentShellSession) -> bool:
        return True

    monkeypatch.setattr(PersistentShellSession, "_probe_stdin_wait", _probe)
    tool = PersistentBashTool(timeout_seconds=10)
    bash_input = BashInputTool(tool.manager)
    try:
        canonical = await tool.execute(command="read -p 'Password: ' X; echo got=$X")
        assert "answer it with bash_input" in canonical
        assert "interactive shell" not in canonical
        assert (await bash_input.execute(line="pw")).strip() == "got=pw"

        password = await tool.execute(command=f"python3 {_FAKE_SSH_SHELL} pw")
        assert "password:" in password
        takeover = await bash_input.execute(line="pw")
        assert "interactive shell" in takeover
        assert await tool.execute(command="echo kind-ok") == "kind-ok"
        await tool.execute(command="exit")
        assert await tool.execute(command="echo local-ok") == "local-ok"
    finally:
        await tool.close()
