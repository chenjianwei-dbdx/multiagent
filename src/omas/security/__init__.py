"""Security boundary objects: the unified model gateway (P2, v1.1 §6).

The gateway is the single policy chokepoint every model endpoint config and
every outgoing model request must pass — before any provider client is built
and before any network byte leaves the process.
"""

from omas.domain.errors import ModelGatewayError

from .model_gateway import (
    CLOUD_PROVIDERS,
    EXTERNAL_TELEMETRY_DEFAULT_OFF,
    ModelEndpointConfig,
    ModelGateway,
    PolicyDecision,
    record_call,
)

__all__ = [
    "CLOUD_PROVIDERS",
    "EXTERNAL_TELEMETRY_DEFAULT_OFF",
    "ModelEndpointConfig",
    "ModelGateway",
    "ModelGatewayError",
    "PolicyDecision",
    "record_call",
]
