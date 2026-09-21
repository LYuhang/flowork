"""Top-level package metadata.

Keep this module intentionally light. Python executes package ``__init__``
before ``python -m vibecanvas_api.sandbox_entry``; importing API managers or
agent modules here pulls optional host dependencies into the sandbox worker
before it can even start.
"""

__version__ = "0.3.0"

__all__ = [
    "__version__",
    "WorkflowRepo",
    "ChatRepo",
    "ExecutionRepo",
    "config",
    "AgentConfig",
    "StorageConfig",
    "init_stores",
]


def __getattr__(name: str):
    if name == "WorkflowRepo":
        from .storage.workflow_repo import WorkflowRepo
        return WorkflowRepo
    if name == "ChatRepo":
        from .storage.chat_repo import ChatRepo
        return ChatRepo
    if name == "ExecutionRepo":
        from .storage.execution_repo import ExecutionRepo
        return ExecutionRepo
    if name in {"config", "AgentConfig", "StorageConfig"}:
        from .config import AgentConfig, StorageConfig, config
        return {
            "config": config,
            "AgentConfig": AgentConfig,
            "StorageConfig": StorageConfig,
        }[name]
    if name == "init_stores":
        from .context import init_stores
        return init_stores
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
