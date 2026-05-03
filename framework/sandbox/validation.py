"""Code validation module for sandbox execution."""

from dataclasses import dataclass


@dataclass
class ValidationResult:
    is_valid: bool
    error_message: str | None = None
    line_number: int | None = None


def validate_code(code: str) -> ValidationResult:
    """
    Validate Python code syntax using compile().
    
    Args:
        code: Python code string to validate
        
    Returns:
        ValidationResult with is_valid=True if code compiles successfully,
        otherwise is_valid=False with error details.
    """
    try:
        compile(code, '<sandbox>', 'exec')
        return ValidationResult(is_valid=True)
    except SyntaxError as e:
        return ValidationResult(
            is_valid=False,
            error_message=str(e),
            line_number=e.lineno,
        )
    except Exception as e:
        return ValidationResult(
            is_valid=False,
            error_message=str(e),
            line_number=None,
        )
