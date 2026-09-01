"""Position-feed adapter implementations.

Each adapter in this package conforms to the PositionFeedAdapter protocol
defined in `nexafreight.adapters.protocols`.

Adapters are NOT imported eagerly here to avoid import-time side effects,
network connections, or settings resolution.

Usage:
    from nexafreight.adapters.feed.aisstream import AISStreamAdapter
    from nexafreight.adapters.feed.mock import MockFeedAdapter
    from nexafreight.adapters.feed.replay_ais import ReplayFeedAdapter
"""
