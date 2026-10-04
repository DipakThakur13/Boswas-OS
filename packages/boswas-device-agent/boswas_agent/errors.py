"""Agent error types."""


class AgentError(Exception):
    pass


class NotEnrolled(AgentError):
    """The device has no Control Plane enrollment; it works offline."""


class UnsupportedCommand(AgentError):
    """A command type or format this agent does not implement."""


class PolicyVerificationError(AgentError):
    """A policy document's signature or integrity check failed. Never apply it."""


class PrivacyViolation(AgentError):
    """A payload contains a field outside its privacy allowlist."""


class DeviceConfigError(AgentError):
    """/etc/boswas/device.conf content is invalid or would contain a secret."""
