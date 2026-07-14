"""Stable application exception hierarchy."""


class SentinelError(Exception):
    """Base exception for expected platform failures."""


class ConfigurationError(SentinelError):
    """Raised when required application configuration is invalid."""


class DependencyUnavailableError(SentinelError):
    """Raised when a required external dependency is unavailable."""
