"""Boundary for user identity (Boswas ID). Interfaces only.

Boswas ID is intentionally not implemented: there is no identity provider,
SSO, OAuth/OIDC server, user directory or password synchronisation in
Boswas OS. Devices keep their local administrator account.

These types exist so that Boswas ID can later be attached (Control Plane
administrator authentication, optional association of devices with users)
without changing the device identity model:

    device identity (certificate)  !=  user identity (AuthenticatedPrincipal)

Nothing in the device agent may derive a device identity from a user, or
treat a user identity as device authentication.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    """A user authenticated by an identity provider (future Boswas ID)."""

    subject: str                     # stable, opaque identifier from the provider
    issuer: str                      # identity provider that authenticated it
    display_name: str | None = None
    groups: tuple[str, ...] = ()


@dataclass(frozen=True)
class IdentityContext:
    """Who (if anyone) is behind a request, next to which device it came from."""

    principal: AuthenticatedPrincipal | None
    device_id: str | None
    method: str                      # e.g. "none", later "boswas-id-oidc"

    @property
    def authenticated(self) -> bool:
        return self.principal is not None


class IdentityProvider(ABC):
    """Pluggable user identity source. Boswas ID will implement this."""

    name: str = "provider"

    @abstractmethod
    def current_context(self, device_id: str | None) -> IdentityContext:
        """Identity context for the current request or session."""


class NoIdentityProvider(IdentityProvider):
    """The only provider in this release: no user identity is ever asserted."""

    name = "none"

    def current_context(self, device_id: str | None) -> IdentityContext:
        return IdentityContext(principal=None, device_id=device_id, method="none")
