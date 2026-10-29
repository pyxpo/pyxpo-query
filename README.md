# pyxpo-query

[![CI](https://github.com/pyxpo/pyxpo-query/actions/workflows/ci.yml/badge.svg)](https://github.com/pyxpo/pyxpo-query/actions/workflows/ci.yml) [![PyPI](https://img.shields.io/pypi/v/pyxpo-query)](https://pypi.org/project/pyxpo-query/) [![Python](https://img.shields.io/pypi/pyversions/pyxpo-query)](https://pypi.org/project/pyxpo-query/) [![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

A server-state cache for [pyxpo](https://github.com/pyxpo/pyxpo) apps, written in pure Python. Screens ask for
data by key; the cache loads it once, shares it between every screen that asks, keeps it while it is fresh,
refetches it when it goes stale, when the app comes back to the foreground and when the network returns, and
retries failed loads. Writes go through mutations, which can update the cache at once and then invalidate what
they changed. If you know [TanStack Query](https://tanstack.com/query) (React Query), you know this API.

```python
from pyxpo import Text, component
from pyxpo_query import use_query


@component
def Posts():
    q = use_query(["posts"], api.list_posts, stale_time=60)
    if q.is_loading:
        return Text("Loading…")
    if q.is_error:
        return Text(str(q.error))
    return Text(", ".join(p["title"] for p in q.data))
```

## Features

- **Cached queries:** one load per key, shared by every screen showing it. Two screens asking at once share one
  request.
- **Freshness:** `stale_time` says how long data counts as fresh; stale data shows at once and refreshes in the
  background.
- **Refetch on focus, reconnect, mount and interval**, each one switchable per query.
- **Retries** with exponential back-off, or your own rule (`retry=lambda count, error: ...`).
- **Offline aware:** loads pause while offline and resume when the network returns.
- **Infinite lists:** `use_infinite_query` loads page after page by cursor; a refetch reloads the pages in order.
- **Mutations:** `use_mutation` with `on_mutate` for optimistic updates, `on_error` to roll back, and
  `on_success`/`on_settled` to invalidate.
- **Cache control:** `invalidate_queries`, `set_query_data`, `remove_queries`, `prefetch_query`,
  `ensure_query_data` and key-prefix matching.
- **Garbage collection:** unused data leaves the cache after `gc_time`.
- **Sync or async functions:** query and mutation functions may be either.
- **No native code and no dependencies** besides pyxpo. Works the same on Android, iOS, web and desktop.
- **Typed:** ships `py.typed`.

## Contents

| If you are... | Start here |
| --- | --- |
| Loading data on a screen | [Quick start](#quick-start) → [Queries](#queries) |
| Deciding when data reloads | [Freshness and refetching](#freshness-and-refetching) |
| Building a feed that loads more on scroll | [Infinite lists](#infinite-lists) |
| Saving changes | [Mutations](#mutations) |
| Wiring app focus and the network | [Focus and network](#focus-and-network) |
| Looking for exact signatures | [API reference](#api-reference) |
| Stuck | [Testing](#testing) · [Troubleshooting](#troubleshooting) · [FAQ](#faq) |

## Platform support

| Target | What works |
| --- | --- |
| Android, API 24+ | Everything. |
| iOS 15+ | Everything. |
| Web page and desktop window (`pyxpo run web` / `pyxpo run desktop`) | Everything. |

Requires Python 3.9+ and `pyxpo>=0.1.0a2`.

## Install

```sh
pip install pyxpo-query
```

Add the module to your app's `pyproject.toml`, so the build bundles its Python files:

```toml
[tool.pyxpo]
modules = ["query"]
```

The module has no native code and no config plugin. Rebuild once after adding it.

## Quick start

Make one client for the app and put it above every screen:

```python
from pyxpo import component
from pyxpo_query import QueryClient, QueryClientProvider

client = QueryClient(stale_time=60, retry=1)


@component
def App():
    return QueryClientProvider(Router(), client=client)
```

Then read data anywhere below it:

```python
from pyxpo_query import use_query


@component
def Post(post_id):
    q = use_query(["post", post_id], lambda: api.get_post(post_id))
    ...
```

`api.get_post` may be a plain function or a coroutine function. The key is a list; anything JSON-like works in
it, and dicts match regardless of order.

## Queries

`use_query(key, fn, **options)` returns a `QueryResult`:

| Field | Meaning |
| --- | --- |
| `data`, `error` | The last data, and the last error (`None` after a success). |
| `status` | `"pending"` (no data yet), `"error"` or `"success"`. |
| `fetch_status` | `"fetching"`, `"paused"` (offline) or `"idle"`. |
| `is_loading` | First load in flight: show a spinner. |
| `is_refetching` | A background reload of data already shown. |
| `is_pending`, `is_success`, `is_error`, `is_fetching`, `is_paused`, `is_stale` | Shorthands. |
| `is_load_error`, `is_refetch_error` | The error came with no data yet, or on top of older data. |
| `is_placeholder_data` | `data` is the placeholder, not this key's data. |
| `refetch()` | Reloads now; returns the task, so `await q.refetch()` works. |

Options:

| Option | Default | Meaning |
| --- | --- | --- |
| `enabled` | `True` | `False` waits (a dependent query: `enabled=user_id is not None`). |
| `stale_time` | client's (`0`) | Seconds data stays fresh. |
| `gc_time` | client's (`300`) | Seconds unused data stays cached. |
| `retry`, `retry_delay` | client's (`3`, back-off) | Count, `True`, or `fn(count, error) -> bool`; seconds or `fn(count, error)`. |
| `select` | `None` | Maps data for this screen only. |
| `initial_data` | `None` | Data to start with, counted as loaded now. |
| `placeholder_data` | `None` | Shown while loading, not cached. `keep_previous_data` keeps the last key's data while a new page loads. |
| `refetch_on_mount` | `True` | `False`, `True` (when stale) or `"always"`. |
| `refetch_on_window_focus`, `refetch_on_reconnect` | client's (`True`) | Reload stale data on these events. |
| `refetch_interval` | `None` | Seconds between polls while mounted. |

## Freshness and refetching

Data is fresh for `stale_time` seconds after it loads. Fresh data never reloads by itself. Stale data still
shows at once, and reloads in the background when a screen mounts it, when the app returns to the foreground,
and when the network comes back. `invalidate_queries(key)` marks data stale and reloads what is on screen.

```python
client.invalidate_queries(["posts"])                      # every key starting with "posts"
client.invalidate_queries(["posts", project_id], exact=True)
client.invalidate_queries(predicate=lambda q: q.key[0] in ("home", "insights"))
```

## Infinite lists

```python
from pyxpo_query import use_infinite_query


@component
def Feed():
    q = use_infinite_query(
        ["feed"],
        lambda cursor: api.list_posts(cursor=cursor),
        initial_page_param=None,
        get_next_page_param=lambda last: last["pagination"]["nextCursor"] if last["pagination"]["hasNext"] else None,
    )
    return FlatList(data=q.items, on_end_reached=lambda: q.has_next_page and q.fetch_next_page(), ...)
```

`q.data` is `InfiniteData(pages, page_params)`. `q.items` joins every page's list (pages that are lists, or have
an `items` field). `get_next_page_param` gets `(last_page, pages, last_param, params)`; take as many as you need.
`max_pages` keeps only the newest pages. `is_fetching_next_page` tells a footer spinner from a full reload.

## Mutations

```python
from pyxpo_query import use_mutation, use_query_client


@component
def RenameButton(post):
    client = use_query_client()

    def optimistic(title):
        previous = client.get_query_data(["post", post["id"]])
        client.set_query_data(["post", post["id"]], lambda old: {**old, "title": title})
        return previous                                   # handed to on_error as `context`

    rename = use_mutation(
        lambda title: api.rename(post["id"], title),
        on_mutate=optimistic,
        on_error=lambda error, title, previous: client.set_query_data(["post", post["id"]], previous),
        on_settled=lambda *_: client.invalidate_queries(["posts"]),
    )
    return Button(title="Saving…" if rename.is_pending else "Rename", on_press=lambda: rename.mutate("New title"))
```

`mutate(variables)` runs in the background and keeps errors in `rename.error`. `await mutate_async(variables)`
returns the data or raises. Mutations do not retry unless you pass `retry`. `reset()` goes back to `"idle"`.

## Focus and network

The client reloads stale data when `focus_manager` reports focus and `online_manager` reports the network. Wire
them once at app start:

```python
from pyxpo import AppState
from pyxpo_query import focus_manager, online_manager

AppState.addEventListener("change", lambda state: focus_manager.set_focused(state == "active"))
network.add_listener(lambda online: online_manager.set_online(online))   # from your network module
```

## Production checklist

- Make one `QueryClient` per app, outside components.
- Set a `stale_time` (30 to 60 seconds suits most screens); `0` reloads on every mount.
- Retry only errors that may pass: network errors and 5xx, not 4xx.
- Put every input of the query function in its key, or screens will share the wrong data.
- After a write, invalidate the lists it changes, not the whole cache.
- On sign-out, call `client.clear()` so the next user never sees old data.

## Testing

Render under a `QueryClientProvider` with a fresh client per test and the mock renderer on a live loop:

```python
import asyncio

from pyxpo import LocalChannel, component
from pyxpo.app import App
from pyxpo.renderers.mock import MockRenderer
from pyxpo_query import QueryClient, QueryClientProvider


def test_posts():
    async def main():
        ch = LocalChannel()
        r = MockRenderer(ch)
        client = QueryClient(retry=0)

        @component
        def Root():
            return QueryClientProvider(Posts(), client=client)

        App(Root, ch, asyncio.get_running_loop()).start()
        await asyncio.sleep(0.05)
        client.unmount()
        return r.texts()

    assert asyncio.run(main()) == ["First post"]
```

`retry=0` makes failures show at once. `client.fetch_query` and `set_query_data` also work in plain async tests.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `use_query needs a QueryClientProvider above it` | Wrap the app root in `QueryClientProvider`. |
| Two screens show each other's data | Their keys are equal; add the missing inputs to the key. |
| Data never reloads | `stale_time` is long or `enabled` is `False`. Invalidate after writes. |
| A spinner shows on every visit | `stale_time` is `0` and there is no data in the cache yet; it is removed after `gc_time` with no screen using it. |
| Errors take seconds to show | Retries with back-off; pass `retry=0` for that query or test. |

## FAQ

**Is it React Query?** It follows its design and names (snake_case), not its code. Devtools, suspense,
persisters and hydration are not there.

**Where does the fetch run?** On the app's asyncio loop. Async functions run there; sync functions run there
too, so keep them quick or make them async.

**Can I read the cache outside components?** Yes: `client.get_query_data`, `await client.fetch_query(...)`.

## API reference

| Name | Purpose |
| --- | --- |
| `QueryClient(*, stale_time=0, gc_time=300, retry=3, retry_delay=..., refetch_on_window_focus=True, refetch_on_reconnect=True, mutation_retry=0)` | The cache. |
| `.get_query_data(key)`, `.get_query_state(key)` | Read. |
| `.set_query_data(key, value_or_updater)`, `.set_queries_data(prefix, updater)` | Write; an updater returning `None` writes nothing. |
| `await .fetch_query(key, fn, stale_time=0)`, `.prefetch_query`, `.ensure_query_data` | Load outside components. |
| `.invalidate_queries(key=None, exact=False, predicate=None, refetch_type="active")` | Mark stale and reload; returns tasks. |
| `.refetch_queries`, `.cancel_queries`, `.remove_queries`, `.reset_queries`, `.clear()` | Cache control. |
| `.is_fetching(key=None)`, `.is_mutating()` | Counts. |
| `.mount()`, `.unmount()` | Listen to focus and network; the provider calls `mount`. |
| `QueryClientProvider(*children, client)`, `use_query_client()` | Context. |
| `use_query(key, fn, **options) -> QueryResult` | See [Queries](#queries). |
| `use_infinite_query(key, fn, *, initial_page_param, get_next_page_param, ...) -> InfiniteQueryResult` | See [Infinite lists](#infinite-lists). |
| `use_mutation(fn, *, on_mutate, on_success, on_error, on_settled, retry, retry_delay) -> MutationResult` | See [Mutations](#mutations). |
| `use_is_fetching(key=None) -> int` | Number of loads in flight. |
| `keep_previous_data` | A `placeholder_data` value. |
| `focus_manager`, `online_manager` | `.set_focused(bool)`, `.set_online(bool)`, `.subscribe(fn)`. |
| `hash_key(key)`, `matches_key(key, prefix, exact=False)` | Key helpers. |

## License

MIT. See [LICENSE](LICENSE).
