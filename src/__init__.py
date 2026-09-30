"""Role-managed harness for long-horizon computer-use tasks."""

from importlib.metadata import PackageNotFoundError, version as _package_version

from .types import (
    EpisodeBudget,
    EpisodeResult,
    ExecResult,
    HarnessConfig,
    ManagedRound,
    AuditReport,
)

# This local project has no published repository or issue tracker configured.
# Retain the public constants for compatibility with the bundled CLI.
HOMEPAGE = ""
ISSUES_URL = ""

try:
    __version__ = _package_version("deep-research-harness")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

__all__ = [
    "EpisodeBudget",
    "EpisodeResult",
    "ExecResult",
    "HarnessConfig",
    "ManagedRound",
    "AuditReport",
    "HOMEPAGE",
    "ISSUES_URL",
    "__version__",
]
