"""Agent error types."""


class AgentError(Exception):
    """Base class. ``code`` is a stable machine-readable reason."""

    code = "AGENT_ERROR"

    def __init__(self, message: str = "", *, code: str | None = None):
        super().__init__(message)
        if code:
            self.code = code


class NotEnrolled(AgentError):
    """The device has no Control Plane enrollment; it works offline."""

    code = "NOT_ENROLLED"


class UnsupportedCommand(AgentError):
    """A command type or format this agent does not implement."""

    code = "UNSUPPORTED_COMMAND"


class InvalidCommand(UnsupportedCommand):
    """A known command type whose payload or envelope is invalid."""

    code = "INVALID_COMMAND"


class CommandExpired(AgentError):
    """A command whose expiry time has passed. Never executed."""

    code = "COMMAND_EXPIRED"


class CommandRefused(AgentError):
    """A valid command that local policy or device state does not allow."""

    code = "COMMAND_REFUSED"


class PolicyVerificationError(AgentError):
    """A policy document's signature or integrity check failed. Never apply it."""

    code = "POLICY_INVALID"


class PrivacyViolation(AgentError):
    """A payload contains a field outside its privacy allowlist."""

    code = "PRIVACY_VIOLATION"


class DeviceConfigError(AgentError):
    """/etc/boswas/device.conf content is invalid or would contain a secret."""

    code = "CONFIG_INVALID"


class IdentityError(AgentError):
    """The persisted device identity is missing or damaged."""

    code = "IDENTITY_INVALID"


class ConnectivityError(AgentError):
    """The Control Plane could not be reached (network, DNS, TLS, timeout)."""

    code = "CONTROL_PLANE_UNREACHABLE"


class ControlPlaneRejected(AgentError):
    """The Control Plane answered and refused the request."""

    code = "CONTROL_PLANE_REJECTED"

    def __init__(self, message: str = "", *, code: str | None = None, status: int = 0):
        super().__init__(message, code=code)
        self.status = status


class CredentialError(AgentError):
    """Device key or certificate problem."""

    code = "CREDENTIAL_ERROR"


class SessionUnavailable(AgentError):
    """No user session agent is available to run an application command."""

    code = "NO_USER_SESSION"


class BackendError(AgentError):
    """boswas-winapp refused or failed an operation (``reason`` is its reason)."""

    code = "BACKEND_ERROR"

    def __init__(self, message: str = "", *, reason: str = "failed", exit_code: int = 1):
        super().__init__(message, code=reason.upper().replace("-", "_"))
        self.reason = reason
        self.exit_code = exit_code
