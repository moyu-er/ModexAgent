from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path

from framework.workspace.models import CdError, CdResult, WorkspaceSwitchCallback
from framework.workspace.parse import parse_user_path

logger = logging.getLogger(__name__)


class WorkspaceContext(ABC):
    """工作空间切换上下文的抽象基类。

    cd 和 exit 共用同一套切换机制 _switch()。
    所有受影响的子系统通过注册回调接入，新增子系统无需修改切换逻辑。

    活跃检查：
      - 通过注入的 active_checker: Callable[[], bool] 实现。
      - 框架不定义"活跃"语义，业务层构造 lambda 并注入。
      - None 时跳过活跃检查。

    切换流程（校验 → 回调 → os.chdir → 持久化 → 状态更新）：
      1. 校验：路径合法性、活跃 agent 检查。
      2. 回调：通知所有注册回调（按注册顺序执行）。
      3. OS 切换：os.chdir() — 所有 subprocess/file/新 terminal 自动生效。
      4. 持久化：cwd.json 写入/清除。
      5. 状态更新：更新 _current。
    """

    @property
    @abstractmethod
    def home(self) -> Path:
        """原始项目路径（启动时记录，不可变）。"""

    @property
    @abstractmethod
    def current(self) -> Path:
        """当前工作路径。"""

    @property
    @abstractmethod
    def data_dir(self) -> Path:
        """当前数据目录 = current / {MODEX_DATA_DIR}。"""

    @property
    @abstractmethod
    def is_home(self) -> bool:
        """是否在原始路径（未 cd 或已 exit）。"""

    @abstractmethod
    def register_callback(self, callback: WorkspaceSwitchCallback) -> None:
        """注册切换回调。回调需实现 WorkspaceSwitchCallback Protocol。"""

    @abstractmethod
    async def cd(self, target: str) -> CdResult:
        """切换到目标路径。target 为用户原始输入。"""

    @abstractmethod
    async def exit(self) -> CdResult:
        """回到原始 home 路径。"""

    @abstractmethod
    async def restore(self) -> CdResult | None:
        """启动时恢复上次持久化的工作路径。返回 None 表示无需恢复。"""


class DefaultWorkspaceContext(WorkspaceContext):
    """默认实现：os.chdir() + 回调通知 + cwd.json 持久化。

    Args:
        home: 原始项目路径（startup directory）。
        active_checker: 可选活跃 agent 检查器。Callable[[], bool]，
            True 表示有 agent 在运行，应拒绝 cd。
    """

    def __init__(
        self,
        home: Path,
        *,
        active_checker: Callable[[], bool] | None = None,
    ) -> None:
        self._home = home.resolve()
        self._current = self._home
        self._callbacks: list[WorkspaceSwitchCallback] = []
        self._active_checker = active_checker
        self._lock = asyncio.Lock()

    # -- properties ----------------------------------------------------------

    @property
    def home(self) -> Path:
        return self._home

    @property
    def current(self) -> Path:
        return self._current

    @property
    def data_dir(self) -> Path:
        return self._current / self._dir_name

    @property
    def is_home(self) -> bool:
        return self._current == self._home

    @property
    def _dir_name(self) -> str:
        """数据子目录名（每次读取环境变量以支持测试）。"""
        return os.environ.get("MODEX_DATA_DIR", ".modex")

    @property
    def _cwd_path(self) -> Path:
        """cwd.json 持久化文件路径。"""
        return self._home / self._dir_name / "cwd.json"

    # -- public API ----------------------------------------------------------

    def register_callback(self, callback: WorkspaceSwitchCallback) -> None:
        self._callbacks.append(callback)

    async def cd(self, target: str) -> CdResult:
        try:
            resolved = parse_user_path(target, base=self._current)
        except ValueError:
            return self._fail("cd: invalid path", CdError.INVALID_PATH)
        return await self._switch(resolved)

    async def exit(self) -> CdResult:
        if self.is_home:
            return self._fail("exit: already at home", CdError.ALREADY_HOME)
        return await self._switch(self._home, _prefix="exit")

    async def restore(self) -> CdResult | None:
        """启动时恢复上次持久化的工作路径。"""
        if not self._cwd_path.exists():
            return None
        try:
            data = json.loads(self._cwd_path.read_text(encoding="utf-8"))
            target = Path(data["path"])
        except (json.JSONDecodeError, KeyError, OSError):
            logger.warning("Failed to read cwd.json, skipping restore")
            return None
        if not target.is_dir():
            logger.warning("Restored path %s no longer exists, skipping", target)
            return None
        result = await self._switch(target)
        if result.success:
            logger.info("Restored workspace to %s", target)
        return result

    # -- internal ------------------------------------------------------------

    def _fail(self, notice: str, error: CdError) -> CdResult:
        return CdResult(
            success=False,
            current_path=self._current,
            original_path=self._home,
            notice=notice,
            error=error,
        )

    async def _switch(self, target: Path, _prefix: str = "cd") -> CdResult:
        """整个 check→callback→chdir→persist 序列在 Lock 下互斥执行。"""
        async with self._lock:
            return await self._switch_locked(target, _prefix)

    async def _switch_locked(self, target: Path, _prefix: str = "cd") -> CdResult:
        """Lock-protected inner implementation of _switch."""

        # Check 1: idempotent — already there is a success
        if target == self._current:
            return CdResult(
                success=True,
                current_path=self._current,
                original_path=self._home,
                notice=f"already at: {self._current}",
            )

        # Check 2: target validity
        if not target.exists():
            return self._fail(
                f"{_prefix}: path not found: '{target}'", CdError.PATH_NOT_FOUND,
            )
        if not target.is_dir():
            return self._fail(
                f"{_prefix}: not a directory: '{target}'", CdError.NOT_A_DIRECTORY,
            )

        # Check 3: agent idle (fast CPU check, before I/O)
        if self._active_checker is not None and self._active_checker():
            return self._fail(
                f"{_prefix}: agents are busy, try again later", CdError.AGENTS_BUSY,
            )

        # Check 4: writable
        new_data_dir = target / self._dir_name
        try:
            new_data_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return self._fail(
                f"{_prefix}: permission denied: '{target}'",
                CdError.PERMISSION_DENIED,
            )

        # Execute: callbacks
        old_data_dir = self.data_dir
        try:
            for cb in self._callbacks:
                await cb.on_workspace_switch(old_data_dir, new_data_dir)
        except Exception:
            logger.exception("Callback failed during workspace switch, not switching")
            return self._fail(
                f"{_prefix}: internal error, reverted", CdError.CALLBACK_ERROR,
            )

        # Execute: OS chdir
        os.chdir(target)

        # Persist
        if target == self._home:
            with contextlib.suppress(OSError):
                self._cwd_path.unlink(missing_ok=True)
        else:
            # Parent (.modex/) is guaranteed to exist from Check 4 above
            self._cwd_path.write_text(
                json.dumps({"path": str(target)}, ensure_ascii=False),
                encoding="utf-8",
            )

        # State update
        self._current = target

        notice = (
            f"switched to: {target}"
            if target != self._home
            else f"returned to home: {target}"
        )
        return CdResult(
            success=True,
            current_path=target,
            original_path=self._home,
            notice=notice,
        )
