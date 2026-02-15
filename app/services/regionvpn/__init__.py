"""VPN-Region (VLESS + Reality) service.

This package is intentionally small and defensive:
- It must not break app boot if REGION_* env vars are missing.
- It provides a thin async SSH-based integration with Xray config.

"""

from .service import RegionVpnService

__all__ = ["RegionVpnService"]
