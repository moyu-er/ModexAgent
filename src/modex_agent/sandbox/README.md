# Sandbox Module

Secure code execution module for framework.

## Quick Start

```python
from modex_agent.sandbox import get_default_sandbox, SandboxConfig
import asyncio


async def main():
    sandbox = get_default_sandbox()

    config = SandboxConfig(allowed_dirs=["/tmp"])
    result = await sandbox.execute("print('Hello!')", config=config)

    if result.success:
        print(result.stdout)


asyncio.run(main())
```

## New Enum-Based API

```python
from modex_agent.sandbox import SandboxType, get_sandbox, get_local_sandbox, get_cloud_sandbox

sandbox = get_local_sandbox()
cloud = get_cloud_sandbox()
sandbox = get_sandbox(SandboxType.SUBPROCESS)
sandbox = get_sandbox("subprocess")
```

## SandboxType Enum

```python
from modex_agent.sandbox import SandboxType

for st in SandboxType:
    print(f"{st.value}: {st.display_name}")
    print(f"  Requires: {st.requires}")
```

| Type | Display | Requires |
|------|---------|----------|
| subprocess | Subprocess (Local) | None |
| landlock | Landlock (Linux Filesystem) | Linux 5.13+ with landlock package |
| docker | Docker (Container) | Docker daemon running |
| e2b | E2B (Cloud) | E2B_API_KEY environment variable |

## Configuration

```python
config = SandboxConfig(
    allowed_dirs=["/workspace", "/tmp"],
    max_file_size_mb=100,
    max_execution_time_seconds=60,
    workspace_dir="/tmp/sandbox",
    copy_images_to="/output",
    enable_network=False,
)
```

## Environment Variables

- `E2B_API_KEY` - E2B cloud sandbox API key
- `DOCKER_SOCKET` - Docker socket path
## Current Runtime Status

Sandbox adapters are used by tools under the ReAct tool execution path. Runtime
control and approval boundaries are documented in `docs/current-runtime.md`.
