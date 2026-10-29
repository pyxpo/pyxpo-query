"""pyxpo-query: a server-state cache for pyxpo apps.

Queries are cached by key, shared between screens, refetched when stale, on focus and on reconnect, and retried
on failure. Mutations write and then invalidate what they changed.
"""
from .client import (FocusManager, OnlineManager, Query, QueryClient, QueryState, default_retry_delay, focus_manager,
                     hash_key, matches_key, online_manager)
from .hooks import (InfiniteData, InfiniteQueryResult, MutationResult, MutationState, QueryClientContext,
                    QueryClientProvider, QueryResult, keep_previous_data, use_infinite_query, use_is_fetching,
                    use_mutation, use_query, use_query_client)

__version__ = "0.1.0a1"

__all__ = [
    "FocusManager", "InfiniteData", "InfiniteQueryResult", "MutationResult", "MutationState", "OnlineManager",
    "Query", "QueryClient", "QueryClientContext", "QueryClientProvider", "QueryResult", "QueryState",
    "default_retry_delay", "focus_manager", "hash_key", "keep_previous_data", "matches_key", "online_manager",
    "use_infinite_query", "use_is_fetching", "use_mutation", "use_query", "use_query_client",
]
