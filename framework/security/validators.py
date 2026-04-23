"""Tool validators for security checking.

This module provides validators that check tools and their arguments
for potentially dangerous operations before execution.
"""

import asyncio
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


class ValidationStatus(Enum):
    """Status of a validation check."""
    VALID = "valid"           # Tool/arguments are safe
    INVALID = "invalid"       # Tool/arguments are dangerous (blocked)
    SUSPICIOUS = "suspicious" # Tool/arguments need approval


class RiskLevel(Enum):
    """Risk level classification for operations."""
    LOW = 1      # Minimal risk, safe operations
    MEDIUM = 2   # Moderate risk, potentially dangerous
    HIGH = 3     # High risk, likely dangerous
    CRITICAL = 4 # Critical risk, definitely dangerous


class DefaultAction(Enum):
    """Default action when validation requires decision."""
    ALLOW = "allow"  # Automatically approve
    ASK = "ask"      # Require explicit approval
    DENY = "deny"    # Automatically reject


@dataclass
class ValidationResult:
    """Result of a tool validation check.
    
    Attributes:
        status: The validation status (VALID, INVALID, SUSPICIOUS)
        reason: Human-readable explanation of the result
        risk_level: The risk level classification
    """
    status: ValidationStatus
    reason: str
    risk_level: RiskLevel
    
    @classmethod
    def valid(cls, reason: str = "Validation passed") -> "ValidationResult":
        """Create a valid result."""
        return cls(
            status=ValidationStatus.VALID,
            reason=reason,
            risk_level=RiskLevel.LOW
        )
    
    @classmethod
    def invalid(cls, reason: str, risk_level: RiskLevel = RiskLevel.CRITICAL) -> "ValidationResult":
        """Create an invalid result."""
        return cls(
            status=ValidationStatus.INVALID,
            reason=reason,
            risk_level=risk_level
        )
    
    @classmethod
    def suspicious(cls, reason: str, risk_level: RiskLevel = RiskLevel.MEDIUM) -> "ValidationResult":
        """Create a suspicious result."""
        return cls(
            status=ValidationStatus.SUSPICIOUS,
            reason=reason,
            risk_level=risk_level
        )


class ToolValidator(ABC):
    """Abstract base class for tool validators.
    
    Validators check tools and their arguments for potentially dangerous
    operations before execution. They return a ValidationResult indicating
    whether the operation is safe, dangerous, or suspicious.
    
    Example:
        class MyValidator(ToolValidator):
            async def validate(self, tool_name: str, arguments: Dict[str, Any]) -> ValidationResult:
                if tool_name == "bash":
                    command = arguments.get("command", "")
                    if "rm -rf /" in command:
                        return ValidationResult.invalid("Dangerous command detected")
                return ValidationResult.valid()
            
            @property
            def name(self) -> str:
                return "my_validator"
    """
    
    @abstractmethod
    async def validate(self, tool_name: str, arguments: Dict[str, Any]) -> ValidationResult:
        """Validate a tool invocation.
        
        Args:
            tool_name: The name of the tool being invoked
            arguments: The arguments passed to the tool
            
        Returns:
            ValidationResult indicating the validation status
        """
        pass
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Return the validator name for logging/debugging."""
        pass


class CommandValidator(ToolValidator):
    """Validator for command-line tools.
    
    Checks command arguments for dangerous patterns like:
    - Filesystem destruction (rm -rf /, format, etc.)
    - Privilege escalation (sudo, su)
    - Remote code execution (curl | sh)
    
    Example:
        validator = CommandValidator(
            dangerous_patterns=[r"rm\\s+-rf\\s+/"],
            blocked_commands=["format C:"]
        )
        result = await validator.validate("bash", {"command": "rm -rf /"})
        # result.status == ValidationStatus.INVALID
    """
    
    # Default dangerous patterns for command validation (BLOCKED - cannot be overridden)
    DEFAULT_DANGEROUS_PATTERNS = [
        # Filesystem destruction - these are irreversible
        r"rm\s+-rf\s+/$",           # rm -rf / (exact match)
        r"rm\s+-rf\s+/\s+$",        # rm -rf / (with trailing space)
        r"format\s+C:",             # Windows format
        r"format\s+/FS",            # Windows format with options
        r"mkfs\.\w+",               # Any mkfs command
        r"dd\s+if=\S+\s+of=/dev/\w+",  # dd to device

        # System destruction
        r":\(\)\s*\{\s*:\|:\s*&\s*\};\s*:",  # Fork bomb
        r"shutdown\s+-h\s+now",
        r"init\s+0",
        r"poweroff",
    ]

    # Default suspicious patterns (require approval - user can decide)
    DEFAULT_SUSPICIOUS_PATTERNS = [
        # Destructive operations (but not system-wide)
        r"rm\s+-rf\s+",             # rm -rf (but not /)
        r"rm\s+-r\s+",
        r"chmod\s+-R\s+",
        r"chown\s+-R\s+",
        r">\s*/etc/",
        r">\s*/usr/",
        r">\s*/var/",

        # Privilege escalation (should ask user)
        r"sudo\s+",
        r"su\s+-",

        # Remote code execution (should ask user)
        r"curl\s+.*\|\s*sh",
        r"curl\s+.*\|\s*bash",
        r"wget\s+.*\|\s*sh",
        r"wget\s+.*\|\s*bash",

        # Potentially dangerous operations
        r"pip\s+install\s+.*--user",
        r"docker\s+run\s+.*--privileged",
        r"mount\s+",
        r"umount\s+",
    ]
    
    def __init__(
        self,
        dangerous_patterns: Optional[List[str]] = None,
        suspicious_patterns: Optional[List[str]] = None,
        blocked_commands: Optional[List[str]] = None,
        command_key: str = "command"
    ):
        """Initialize command validator.
        
        Args:
            dangerous_patterns: Regex patterns that are considered dangerous (blocked)
            suspicious_patterns: Regex patterns that are considered suspicious (need approval)
            blocked_commands: Exact command strings that are blocked
            command_key: The key in arguments dict that contains the command
        """
        self.dangerous_patterns = dangerous_patterns or self.DEFAULT_DANGEROUS_PATTERNS
        self.suspicious_patterns = suspicious_patterns or self.DEFAULT_SUSPICIOUS_PATTERNS
        self.blocked_commands = blocked_commands or []
        self.command_key = command_key
        
        # Compile patterns for efficiency
        self._dangerous_regexes = [
            re.compile(p, re.IGNORECASE) for p in self.dangerous_patterns
        ]
        self._suspicious_regexes = [
            re.compile(p, re.IGNORECASE) for p in self.suspicious_patterns
        ]
    
    async def validate(self, tool_name: str, arguments: Dict[str, Any]) -> ValidationResult:
        """Validate a command invocation."""
        command = arguments.get(self.command_key, "")
        if not command:
            return ValidationResult.valid("No command to validate")
        
        # Check blocked commands first (exact match)
        for blocked in self.blocked_commands:
            if blocked in command:
                return ValidationResult.invalid(
                    f"Command contains blocked pattern: {blocked}",
                    RiskLevel.CRITICAL
                )
        
        # Check dangerous patterns
        for pattern in self._dangerous_regexes:
            if pattern.search(command):
                return ValidationResult.invalid(
                    f"Matched dangerous pattern: {pattern.pattern}",
                    RiskLevel.CRITICAL
                )
        
        # Check suspicious patterns
        for pattern in self._suspicious_regexes:
            if pattern.search(command):
                return ValidationResult.suspicious(
                    f"Matched suspicious pattern: {pattern.pattern}",
                    RiskLevel.HIGH
                )
        
        return ValidationResult.valid("Command looks safe")
    
    @property
    def name(self) -> str:
        return "command_validator"


class FilePathValidator(ToolValidator):
    """Validator for file path arguments.
    
    Checks file paths for security issues like:
    - Directory traversal (../)
    - Access outside allowed directories
    - Absolute paths (when not allowed)
    
    Example:
        validator = FilePathValidator(
            allowed_paths=["/tmp", "/home/user"],
            allow_absolute=False
        )
        result = await validator.validate("read_file", {"path": "../../../etc/passwd"})
        # result.status == ValidationStatus.SUSPICIOUS
    """
    
    def __init__(
        self,
        allowed_paths: Optional[List[str]] = None,
        allow_absolute: bool = True,
        path_key: str = "path",
        block_traversal: bool = True
    ):
        """Initialize file path validator.
        
        Args:
            allowed_paths: List of allowed base paths (None means allow all)
            allow_absolute: Whether to allow absolute paths
            path_key: The key in arguments dict that contains the path
            block_traversal: Whether to block directory traversal attempts
        """
        self.allowed_paths = allowed_paths
        self.allow_absolute = allow_absolute
        self.path_key = path_key
        self.block_traversal = block_traversal
    
    async def validate(self, tool_name: str, arguments: Dict[str, Any]) -> ValidationResult:
        """Validate a file path argument."""
        path = arguments.get(self.path_key, "")
        if not path:
            return ValidationResult.valid("No path to validate")

        # Use pathlib for proper path handling
        try:
            input_path = Path(path)

            # Check directory traversal using proper path resolution
            if self.block_traversal:
                # Check for '..' in path components (catches all traversal attempts)
                if ".." in input_path.parts:
                    return ValidationResult.suspicious(
                        "Potential path traversal detected (contains '..')",
                        RiskLevel.HIGH
                    )

            # Check absolute paths
            if not self.allow_absolute and input_path.is_absolute():
                return ValidationResult.suspicious(
                    "Absolute paths are not allowed",
                    RiskLevel.MEDIUM
                )

            # Check allowed paths using proper path resolution
            if self.allowed_paths is not None:
                # Resolve the path to its absolute form for comparison
                try:
                    # For security, we check if the resolved path is within allowed directories
                    resolved_path = input_path.resolve()

                    is_allowed = any(
                        self._is_path_within_allowed(resolved_path, Path(allowed).resolve())
                        for allowed in self.allowed_paths
                    )

                    if not is_allowed:
                        return ValidationResult.suspicious(
                            f"Path outside allowed directories: {self.allowed_paths}",
                            RiskLevel.MEDIUM
                        )
                except (OSError, ValueError) as e:
                    # Path resolution failed (e.g., non-existent path with ..)
                    return ValidationResult.suspicious(
                        f"Invalid path: {e}",
                        RiskLevel.HIGH
                    )

            return ValidationResult.valid("Path looks safe")
        except Exception as e:
            return ValidationResult.suspicious(
                f"Path validation error: {e}",
                RiskLevel.MEDIUM
            )

    def _is_path_within_allowed(self, path: Path, allowed_base: Path) -> bool:
        """Check if a path is within an allowed base directory.

        Uses proper path comparison to prevent traversal bypasses.

        Args:
            path: The resolved path to check
            allowed_base: The resolved allowed base path

        Returns:
            True if path is within or equal to allowed_base
        """
        try:
            # Use relative_to which raises ValueError if path is not within allowed_base
            path.relative_to(allowed_base)
            return True
        except ValueError:
            return False
    
    @property
    def name(self) -> str:
        return "file_path_validator"


class CompositeValidator(ToolValidator):
    """Validator that chains multiple validators together.
    
    Executes validators in sequence and aggregates results:
    - If any validator returns INVALID, immediately return INVALID
    - If any validator returns SUSPICIOUS, return the highest risk level
    - If all validators return VALID, return VALID
    
    Example:
        validator = CompositeValidator([
            CommandValidator(),
            FilePathValidator(allowed_paths=["/tmp"]),
        ])
        result = await validator.validate("bash", {"command": "ls /tmp"})
        # Both validators must pass
    """
    
    def __init__(self, validators: List[ToolValidator]):
        """Initialize composite validator.
        
        Args:
            validators: List of validators to chain
        """
        self.validators = validators
    
    async def validate(self, tool_name: str, arguments: Dict[str, Any]) -> ValidationResult:
        """Validate using all validators in sequence."""
        suspicious_results = []
        
        for validator in self.validators:
            result = await validator.validate(tool_name, arguments)
            
            # Short-circuit on INVALID
            if result.status == ValidationStatus.INVALID:
                return result
            
            # Collect SUSPICIOUS results
            if result.status == ValidationStatus.SUSPICIOUS:
                suspicious_results.append(result)
        
        # If any SUSPICIOUS, return the highest risk one
        if suspicious_results:
            # Sort by risk level (highest first)
            suspicious_results.sort(key=lambda r: r.risk_level.value, reverse=True)
            return suspicious_results[0]
        
        return ValidationResult.valid("All validators passed")
    
    @property
    def name(self) -> str:
        return f"composite({', '.join(v.name for v in self.validators)})"


class FunctionValidator(ToolValidator):
    """Validator using custom validation functions.

    Allows users to provide custom validation logic via functions.
    Functions can be sync or async, and receive (tool_name, arguments).

    Example:
        # Simple sync validator
        def my_validator(tool_name, arguments):
            if arguments.get("count", 0) > 100:
                return ValidationResult.suspicious("Count too high", RiskLevel.MEDIUM)
            return ValidationResult.valid("OK")

        validator = FunctionValidator(my_validator)

        # Async validator
        async def async_validator(tool_name, arguments):
            # async check...
            return ValidationResult.valid("OK")

        validator = FunctionValidator(async_validator)

        # Multiple validators
        validator = FunctionValidator([validator1, validator2, validator3])
    """

    def __init__(
        self,
        validators: Optional[
            List[Callable[[str, Dict[str, Any]], Any]]
        ] = None,
    ):
        """Initialize with validation functions.

        Args:
            validators: Single function or list of validation functions.
                       Each function receives (tool_name, arguments) and returns
                       ValidationResult or dict with status/reason/risk_level.
        """
        if validators is None:
            self.validators = []
        elif callable(validators) and not isinstance(validators, list):
            self.validators = [validators]
        else:
            self.validators = validators

    async def validate(self, tool_name: str, arguments: Dict[str, Any]) -> ValidationResult:
        """Validate using custom functions."""
        for validator in self.validators:
            try:
                # Check if function is async
                if asyncio.iscoroutinefunction(validator):
                    result = await validator(tool_name, arguments)
                else:
                    result = validator(tool_name, arguments)

                # Handle different return types
                if isinstance(result, ValidationResult):
                    if result.status != ValidationStatus.VALID:
                        return result
                elif isinstance(result, dict):
                    status = result.get("status", ValidationStatus.VALID)
                    reason = result.get("reason", "")
                    risk_level = result.get("risk_level", RiskLevel.LOW)

                    if status == ValidationStatus.INVALID:
                        return ValidationResult.invalid(reason, risk_level)
                    elif status == ValidationStatus.SUSPICIOUS:
                        return ValidationResult.suspicious(reason, risk_level)
                elif result is False:
                    return ValidationResult.invalid("Validation failed", RiskLevel.HIGH)

            except Exception as e:
                return ValidationResult.invalid(
                    f"Validation error: {e}", RiskLevel.CRITICAL
                )

        return ValidationResult.valid("All function validations passed")

    @property
    def name(self) -> str:
        return f"function_validator({len(self.validators)} validators)"


class ParameterValidator(ToolValidator):
    """Validator for specific tool parameters with value constraints.

    Validates that specific parameters of a tool meet certain constraints.
    Supports multiple constraint types: range checks, enum values, regex patterns,
    and custom validation functions.

    Example:
        # Validate specific tool parameters
        validator = ParameterValidator({
            "database_query": {
                "timeout": {"max": 60, "min": 1},  # timeout must be 1-60
                "query": {"pattern": r"^SELECT", "flags": re.IGNORECASE},  # must start with SELECT
            },
            "file_write": {
                "size": {"max": 1024 * 1024},  # max 1MB
                "encoding": {"enum": ["utf-8", "ascii", "latin-1"]},  # must be one of these
            },
        })

        # With custom validation functions
        validator = ParameterValidator({
            "send_email": {
                "to": {"custom": lambda v: "@" in v or (ValidationStatus.INVALID, "Invalid email")},
            },
        })
    """

    def __init__(
        self,
        tool_constraints: Dict[str, Dict[str, Dict[str, Any]]],
    ):
        """Initialize with tool-specific parameter constraints.

        Args:
            tool_constraints: Dict mapping tool_name to parameter constraints.
                Each parameter constraint can have:
                - "min"/"max": Numeric range constraints
                - "enum": List of allowed values
                - "pattern": Regex pattern to match (with optional "flags")
                - "custom": Function that validates the value
        """
        self.tool_constraints = tool_constraints

    async def validate(self, tool_name: str, arguments: Dict[str, Any]) -> ValidationResult:
        """Validate tool arguments against constraints."""
        # If no constraints for this tool, skip validation
        if tool_name not in self.tool_constraints:
            return ValidationResult.valid(f"No constraints for tool: {tool_name}")

        param_constraints = self.tool_constraints[tool_name]

        for param_name, constraints in param_constraints.items():
            value = arguments.get(param_name)

            # Skip if parameter not provided (optional parameters)
            if value is None:
                continue

            # Check min/max constraints
            if "min" in constraints or "max" in constraints:
                result = self._validate_range(value, constraints, param_name)
                if result:
                    return result

            # Check enum constraints
            if "enum" in constraints:
                result = self._validate_enum(value, constraints, param_name)
                if result:
                    return result

            # Check pattern constraints
            if "pattern" in constraints:
                result = self._validate_pattern(value, constraints, param_name)
                if result:
                    return result

            # Check custom validation
            if "custom" in constraints:
                result = await self._validate_custom(value, constraints, param_name, arguments)
                if result:
                    return result

        return ValidationResult.valid("All parameter constraints satisfied")

    def _validate_range(
        self, value: Any, constraints: Dict[str, Any], param_name: str
    ) -> Optional[ValidationResult]:
        """Validate numeric range constraints."""
        try:
            num_value = float(value)
            min_val = constraints.get("min")
            max_val = constraints.get("max")

            if min_val is not None and num_value < min_val:
                return ValidationResult.suspicious(
                    f"Parameter '{param_name}' ({value}) is below minimum ({min_val})",
                    RiskLevel.MEDIUM,
                )
            if max_val is not None and num_value > max_val:
                return ValidationResult.suspicious(
                    f"Parameter '{param_name}' ({value}) exceeds maximum ({max_val})",
                    RiskLevel.MEDIUM,
                )
        except (TypeError, ValueError):
            return ValidationResult.invalid(
                f"Parameter '{param_name}' must be numeric", RiskLevel.HIGH
            )
        return None

    def _validate_enum(
        self, value: Any, constraints: Dict[str, Any], param_name: str
    ) -> Optional[ValidationResult]:
        """Validate enum constraints."""
        allowed = constraints["enum"]
        if value not in allowed:
            return ValidationResult.suspicious(
                f"Parameter '{param_name}' ({value}) must be one of: {allowed}",
                RiskLevel.MEDIUM,
            )
        return None

    def _validate_pattern(
        self, value: Any, constraints: Dict[str, Any], param_name: str
    ) -> Optional[ValidationResult]:
        """Validate regex pattern constraints."""
        pattern = constraints["pattern"]
        flags = constraints.get("flags", 0)

        if not isinstance(value, str):
            return ValidationResult.invalid(
                f"Parameter '{param_name}' must be a string", RiskLevel.HIGH
            )

        if not re.search(pattern, value, flags):
            return ValidationResult.suspicious(
                f"Parameter '{param_name}' does not match required pattern: {pattern}",
                RiskLevel.MEDIUM,
            )
        return None

    async def _validate_custom(
        self, value: Any, constraints: Dict[str, Any], param_name: str, all_args: Dict[str, Any]
    ) -> Optional[ValidationResult]:
        """Validate using custom function."""
        custom_fn = constraints["custom"]

        try:
            if asyncio.iscoroutinefunction(custom_fn):
                result = await custom_fn(value, all_args)
            else:
                result = custom_fn(value, all_args)

            # Handle different return types
            if isinstance(result, ValidationResult):
                if result.status != ValidationStatus.VALID:
                    return result
            elif isinstance(result, tuple) and len(result) >= 2:
                status, reason = result[0], result[1]
                risk = result[2] if len(result) > 2 else RiskLevel.MEDIUM
                if status == ValidationStatus.INVALID:
                    return ValidationResult.invalid(reason, risk)
                elif status == ValidationStatus.SUSPICIOUS:
                    return ValidationResult.suspicious(reason, risk)
            elif result is False:
                return ValidationResult.suspicious(
                    f"Parameter '{param_name}' failed custom validation", RiskLevel.MEDIUM
                )
        except Exception as e:
            return ValidationResult.invalid(
                f"Custom validation error for '{param_name}': {e}", RiskLevel.HIGH
            )
        return None

    @property
    def name(self) -> str:
        tool_count = len(self.tool_constraints)
        return f"parameter_validator({tool_count} tools)"
