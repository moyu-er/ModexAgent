"""Docker platform detection utilities."""

import sys
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

# Try to import docker, but don't fail if not available
try:
    import docker
    from docker.errors import DockerException
    DOCKER_AVAILABLE = True
except ImportError:
    docker = None
    DockerException = Exception
    DOCKER_AVAILABLE = False


def check_docker_available() -> bool:
    """Check if Docker is available and the daemon is running.
    
    Returns:
        True if Docker is available and responding, False otherwise.
    """
    if not DOCKER_AVAILABLE:
        logger.debug("Docker Python package not installed")
        return False
    
    try:
        client = docker.from_env()
        client.ping()
        return True
    except DockerException as e:
        logger.debug(f"Docker not available: {e}")
        return False
    except Exception as e:
        logger.debug(f"Unexpected error checking Docker: {e}")
        return False


def get_docker_info() -> Optional[Dict[str, Any]]:
    """Get Docker system information.
    
    Returns:
        Docker info dict or None if Docker is not available.
    """
    if not DOCKER_AVAILABLE:
        return None
    
    try:
        client = docker.from_env()
        return client.info()
    except Exception as e:
        logger.debug(f"Failed to get Docker info: {e}")
        return None


def check_windows_linux_containers() -> bool:
    """Check if Windows Docker is configured for Linux containers.
    
    Returns:
        True if Docker supports Linux containers, False otherwise.
        On non-Windows platforms, always returns True if Docker is available.
    """
    if sys.platform != "win32":
        # On non-Windows platforms, we assume Linux containers are supported
        return check_docker_available()
    
    # On Windows, check the OSType
    info = get_docker_info()
    if info is None:
        return False
    
    os_type = info.get("OSType", "").lower()
    if os_type == "linux":
        return True
    elif os_type == "windows":
        logger.warning(
            "Docker Desktop is configured for Windows containers. "
            "Linux containers are required for this sandbox."
        )
        return False
    else:
        logger.warning(f"Unknown Docker OSType: {os_type}")
        return False


def get_docker_version() -> Optional[str]:
    """Get Docker version string.
    
    Returns:
        Docker version string or None if not available.
    """
    if not DOCKER_AVAILABLE:
        return None
    
    try:
        client = docker.from_env()
        version_info = client.version()
        return version_info.get("Version")
    except Exception as e:
        logger.debug(f"Failed to get Docker version: {e}")
        return None


def check_docker_version_requirement(min_version: str = "20.10") -> bool:
    """Check if Docker version meets minimum requirement.
    
    Args:
        min_version: Minimum required version string (e.g., "20.10").
        
    Returns:
        True if version meets requirement or can't be determined, False otherwise.
    """
    version = get_docker_version()
    if version is None:
        # If we can't get version, assume it's OK
        return True
    
    try:
        # Simple version comparison (major.minor)
        current_parts = version.split(".")[:2]
        min_parts = min_version.split(".")[:2]
        
        current_major = int(current_parts[0])
        current_minor = int(current_parts[1]) if len(current_parts) > 1 else 0
        
        min_major = int(min_parts[0])
        min_minor = int(min_parts[1]) if len(min_parts) > 1 else 0
        
        if current_major > min_major:
            return True
        elif current_major == min_major:
            return current_minor >= min_minor
        else:
            return False
    except (ValueError, IndexError) as e:
        logger.warning(f"Failed to parse Docker version '{version}': {e}")
        return True


class DockerPlatformChecker:
    """Helper class to check Docker platform compatibility."""
    
    def __init__(self):
        self._available: Optional[bool] = None
        self._info: Optional[Dict[str, Any]] = None
    
    def is_available(self) -> bool:
        """Check if Docker is available."""
        if self._available is None:
            self._available = check_docker_available()
        return self._available
    
    def supports_linux_containers(self) -> bool:
        """Check if Linux containers are supported."""
        if not self.is_available():
            return False
        return check_windows_linux_containers()
    
    def get_info(self) -> Optional[Dict[str, Any]]:
        """Get Docker info, caching the result."""
        if self._info is None:
            self._info = get_docker_info()
        return self._info
    
    def get_status_report(self) -> Dict[str, Any]:
        """Get a comprehensive status report.
        
        Returns:
            Dict with availability, platform, version, and recommendations.
        """
        report = {
            "available": False,
            "supports_linux_containers": False,
            "version": None,
            "platform": sys.platform,
            "error": None,
            "recommendation": None,
        }
        
        if not DOCKER_AVAILABLE:
            report["error"] = "Docker Python package not installed"
            report["recommendation"] = "Install docker package: pip install docker"
            return report
        
        if not self.is_available():
            report["error"] = "Docker daemon not running or not accessible"
            report["recommendation"] = "Start Docker Desktop or Docker service"
            return report
        
        report["available"] = True
        report["version"] = get_docker_version()
        
        if sys.platform == "win32":
            if self.supports_linux_containers():
                report["supports_linux_containers"] = True
            else:
                report["error"] = "Docker Desktop configured for Windows containers"
                report["recommendation"] = (
                    "Switch Docker Desktop to Linux containers: "
                    "Settings > General > Use the WSL 2 based engine"
                )
        else:
            report["supports_linux_containers"] = True
        
        return report


# Global checker instance for reuse
_docker_checker: Optional[DockerPlatformChecker] = None


def get_docker_checker() -> DockerPlatformChecker:
    """Get the global Docker platform checker instance."""
    global _docker_checker
    if _docker_checker is None:
        _docker_checker = DockerPlatformChecker()
    return _docker_checker
