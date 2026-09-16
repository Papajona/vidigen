"""Production provider registry.

Providers are registered by capability so the Agent can select among real adapters
without knowing provider-specific credentials or endpoints.
"""
from dataclasses import dataclass
import os
from typing import Any, Callable

@dataclass(frozen=True)
class ProviderSpec:
    key: str
    capability: str
    enabled_env: str | None = None
    cost_key: str | None = None
    adapter_factory: Callable[[], Any] | None = None

class ProviderRegistry:
    def __init__(self, specs: list[ProviderSpec] | None = None):
        self._specs = list(specs or [])
    def register(self, spec: ProviderSpec) -> None:
        self._specs = [s for s in self._specs if s.key != spec.key]
        self._specs.append(spec)
    def list(self, capability: str | None = None) -> list[dict]:
        rows=[]
        for s in self._specs:
            configured = True if not s.enabled_env else bool(os.getenv(s.enabled_env,''))
            if capability and s.capability != capability: continue
            rows.append({'key':s.key,'capability':s.capability,'configured':configured,'cost_key':s.cost_key})
        return rows
    def choose(self, capability: str, preferred: str = 'auto') -> ProviderSpec:
        candidates=[s for s in self._specs if s.capability == capability and (not s.enabled_env or os.getenv(s.enabled_env,''))]
        if preferred != 'auto':
            for s in candidates:
                if s.key == preferred: return s
            raise RuntimeError(f'{preferred} is not configured for {capability}.')
        if not candidates: raise RuntimeError(f'No configured provider is available for {capability}.')
        return candidates[0]
