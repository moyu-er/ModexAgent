"""Approval handlers for command security.

This module provides various implementations of the ApprovalHandler
abstract base class for different approval scenarios.
"""

import logging
import re
from abc import ABC, abstractmethod
from datetime import datetime

logger = logging.getLogger(__name__)


class ApprovalHandler(ABC):
    """Abstract base class for command approval handlers.

    This class defines the interface for different approval mechanisms:
    - ConsoleApprovalHandler: Interactive command line approval
    - ConfigBasedApprovalHandler: Configuration-based automatic approval
    - APIBasedApprovalHandler: External API-based approval
    - CompositeApprovalHandler: Chain multiple handlers

    Users can also implement custom handlers for specific needs (e.g., Slack, Email).
    """

    @abstractmethod
    async def approve(self, command: str, reason: str) -> bool:
        """Approve or deny a command execution.

        Args:
            command: The command to be executed
            reason: Why this command needs approval

        Returns:
            True to approve, False to deny
        """
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the handler name for logging/debugging."""
        pass


class ConsoleApprovalHandler(ApprovalHandler):
    """Default handler using console input for approval.

    Suitable for interactive CLI applications where user can review
    and approve/deny commands in real-time.

    Example:
        handler = ConsoleApprovalHandler()
        approved = await handler.approve("rm -rf /tmp", "Matched ask pattern")
    """

    async def approve(self, command: str, reason: str) -> bool:
        print(f"\n{'='*60}")
        print("🔒 Security Approval Required")
        print(f"{'='*60}")
        print(f"Command: {command}")
        print(f"Reason:  {reason}")
        print(f"{'='*60}")

        try:
            response = input("Approve execution? (y/n): ").strip().lower()
            return response in ('y', 'yes', '是', '确认', 'approve')
        except EOFError:
            # Handle non-interactive environments
            logger.warning("Console input not available, denying command")
            return False

    @property
    def name(self) -> str:
        return "console"


class ConfigBasedApprovalHandler(ApprovalHandler):
    """Handler that makes decisions based on configuration patterns only.

    No user interaction required, suitable for:
    - Automated/CI environments
    - Batch processing
    - Scenarios requiring deterministic behavior

    Example:
        handler = ConfigBasedApprovalHandler(
            auto_approve_patterns=[r"^git\\s+status"],
            auto_deny_patterns=[r"^rm\\s+-rf\\s+/"],
            default_action=False
        )
    """

    def __init__(
        self,
        auto_approve_patterns: list[str] | None = None,
        auto_deny_patterns: list[str] | None = None,
        default_action: bool = False
    ):
        """Initialize with configuration patterns.

        Args:
            auto_approve_patterns: Regex patterns that auto-approve
            auto_deny_patterns: Regex patterns that auto-deny
            default_action: Default decision if no pattern matches
        """
        self.auto_approve_patterns = auto_approve_patterns or []
        self.auto_deny_patterns = auto_deny_patterns or []
        self.default_action = default_action

        # Compile patterns for efficiency
        self._approve_regexes = [
            re.compile(p, re.IGNORECASE) for p in self.auto_approve_patterns
        ]
        self._deny_regexes = [
            re.compile(p, re.IGNORECASE) for p in self.auto_deny_patterns
        ]

    async def approve(self, command: str, reason: str) -> bool:
        # Check deny patterns first (higher priority)
        for pattern in self._deny_regexes:
            if pattern.search(command):
                logger.debug(f"Config handler denied command (pattern: {pattern.pattern})")
                return False

        # Check approve patterns
        for pattern in self._approve_regexes:
            if pattern.search(command):
                logger.debug(f"Config handler approved command (pattern: {pattern.pattern})")
                return True

        # Return default action
        logger.debug(f"Config handler using default action: {self.default_action}")
        return self.default_action

    @property
    def name(self) -> str:
        return "config"


class APIBasedApprovalHandler(ApprovalHandler):
    """Handler that calls external API for approval.

    Suitable for enterprise environments with centralized approval systems,
    audit trails, or multi-user workflows.

    Example:
        handler = APIBasedApprovalHandler(
            endpoint="https://approval.company.com/api/approve",
            api_key="secret-key",
            timeout=30.0
        )
    """

    def __init__(
        self,
        endpoint: str,
        api_key: str | None = None,
        timeout: float = 30.0,
        headers: dict[str, str] | None = None
    ):
        """Initialize API-based handler.

        Args:
            endpoint: API endpoint URL
            api_key: Optional API key for authentication
            timeout: Request timeout in seconds
            headers: Additional HTTP headers
        """
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout = timeout
        self.headers = headers or {}

    async def approve(self, command: str, reason: str) -> bool:
        try:
            import aiohttp
        except ImportError:
            logger.error("aiohttp not installed, cannot use APIBasedApprovalHandler")
            return False

        payload = {
            "command": command,
            "reason": reason,
            "timestamp": datetime.now().isoformat(),
            "handler": "sandbox_approval"
        }

        headers = self.headers.copy()
        headers["Content-Type"] = "application/json"
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.endpoint,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=self.timeout)
                ) as response:
                    if response.status != 200:
                        logger.warning(f"Approval API returned status {response.status}")
                        return False

                    result = await response.json()
                    approved = result.get("approved", False)

                    if approved:
                        logger.info(f"API approved command: {command}")
                    else:
                        logger.info(f"API denied command: {command}")

                    return approved

        except Exception as e:
            logger.error(f"API approval request failed: {e}")
            return False

    @property
    def name(self) -> str:
        return "api"


class CompositeApprovalHandler(ApprovalHandler):
    """Handler that chains multiple handlers.

    Executes handlers in order, requiring ALL handlers to approve.
    If any handler denies, the command is immediately rejected.
    Useful for combining multiple approval strategies where every
    strategy must agree.

    Example:
        handler = CompositeApprovalHandler([
            ConfigBasedApprovalHandler(...),  # Check config first
            ConsoleApprovalHandler(),          # Then ask user
        ])
    """

    def __init__(self, handlers: list[ApprovalHandler]):
        """Initialize with list of handlers.

        Args:
            handlers: List of approval handlers to chain
        """
        self.handlers = handlers

    async def approve(self, command: str, reason: str) -> bool:
        for handler in self.handlers:
            logger.debug(f"Trying approval handler: {handler.name}")
            result = await handler.approve(command, reason)
            # If any handler denies, deny immediately
            if not result:
                logger.info(f"Handler '{handler.name}' denied command: {command}")
                return False
        # All handlers approved
        return True

    @property
    def name(self) -> str:
        return f"composite({', '.join(h.name for h in self.handlers)})"


class LoggingApprovalHandler(ApprovalHandler):
    """Handler that logs approval requests but always approves.

    Useful for auditing and monitoring without blocking execution.
    Can be combined with other handlers using CompositeApprovalHandler.

    Example:
        handler = CompositeApprovalHandler([
            LoggingApprovalHandler(),  # Log all requests
            ConsoleApprovalHandler(),  # Then ask user
        ])
    """

    def __init__(self, log_level: int = logging.INFO):
        """Initialize logging handler.

        Args:
            log_level: Logging level for approval messages
        """
        self.log_level = log_level

    async def approve(self, command: str, reason: str) -> bool:
        logger.log(self.log_level, f"[APPROVAL REQUEST] Command: {command}, Reason: {reason}")
        return True  # Always approve, just log

    @property
    def name(self) -> str:
        return "logging"
