"""Compatibility facade for Vidigen's provider registry.

The executable registry lives in gateway.providers. This module is retained so older
imports do not silently diverge from the production routing path.
"""
from gateway.providers import (
    PROVIDERS,
    ProviderError,
    configured_providers,
    provider_inventory,
)


class ProviderRegistry:
    def list(self, capability=None):
        return provider_inventory() if capability is None else [
            row for row in provider_inventory()
            if capability in (row.get("capabilities") or [])
        ]

    def choose(self, capability, preferred="auto"):
        candidates=configured_providers(capability)
        if preferred != "auto":
            if preferred not in candidates:
                raise ProviderError(f"{preferred} is not configured for {capability}.")
            return PROVIDERS[preferred]
        if not candidates:
            raise ProviderError(f"No configured provider is available for {capability}.")
        return PROVIDERS[candidates[0]]

    def register(self, spec):
        raise RuntimeError(
            "Runtime registration is configuration-driven. Add the provider manifest to "
            "gateway/providers.json or VIDIGEN_PROVIDER_CONFIG_JSON and restart the gateway."
        )
