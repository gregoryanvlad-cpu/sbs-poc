"""Secondary bot: content "player" gateway.

This bot is intentionally minimal:
- validates subscription in the shared DB
- resolves deep-link tokens from content_requests
- only returns direct links for allowed domains
"""
