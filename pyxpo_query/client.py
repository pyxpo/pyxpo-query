"""The query cache: one `Query` per key, shared by every screen that asks for it.

    client = QueryClient(stale_time=60, retry=1)
    posts = await client.fetch_query(["posts", project_id], lambda: api.list_posts(project_id))
    client.set_query_data(["post", "p1"], lambda old: {**old, "title": "New"})
    client.invalidate_queries(["posts"])          # every key that starts with "posts" refetches if on screen

Times are in seconds. A query is fresh for `stale_time` after it loads; an unused query is dropped from the
cache `gc_time` after its last screen goes away.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

from pyxpo.module_api import spawn

Key = Sequence[Any]
Retry = Union[bool, int, Callable[[int, BaseException], bool]]


def hash_key(key: Key) -> str:
    """A stable string for a key: dicts hash the same whatever their key order."""
    if isinstance(key, (str, bytes)) or not isinstance(key, (list, tuple)):
        raise TypeError("a query key is a list or tuple, got %r" % (key,))
    return json.dumps(list(key), sort_keys=True, default=repr, separators=(",", ":"))


def _part_matches(want: Any, have: Any) -> bool:
    if isinstance(want, dict) and isinstance(have, dict):
        return all(k in have and _part_matches(v, have[k]) for k, v in want.items())
    return want == have


def matches_key(key: Key, prefix: Optional[Key], exact: bool = False) -> bool:
    """Whether `key` starts with `prefix`. A dict in `prefix` matches any dict holding those entries."""
    if prefix is None:
        return True
    if exact:
        return hash_key(key) == hash_key(prefix)
    if len(prefix) > len(key):
        return False
    return all(_part_matches(w, h) for w, h in zip(prefix, key))


def _now() -> float:
    return time.monotonic()


def _running_loop() -> Optional[asyncio.AbstractEventLoop]:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


async def _call(fn: Callable[..., Any], *args: Any) -> Any:
    """Calls a sync or async function and awaits what it hands back."""
    value = fn(*args)
    if inspect.isawaitable(value):
        value = await value
    return value


class _Manager:
    """A flag the app keeps current (focused, online). Listeners hear each change to True or False."""

    def __init__(self) -> None:
        self._value = True
        self._listeners: List[Callable[[bool], None]] = []

    def _set(self, value: bool) -> None:
        value = bool(value)
        if value == self._value:
            return
        self._value = value
        for fn in list(self._listeners):
            fn(value)

    def subscribe(self, fn: Callable[[bool], None]) -> Callable[[], None]:
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn) if fn in self._listeners else None


class FocusManager(_Manager):
    """Tell it when the app comes to the foreground: stale queries on screen then refetch."""

    def set_focused(self, focused: bool) -> None:
        self._set(focused)

    def is_focused(self) -> bool:
        return self._value


class OnlineManager(_Manager):
    """Tell it when the network comes and goes. Offline, a fetch waits (`fetch_status == "paused"`)."""

    def set_online(self, online: bool) -> None:
        self._set(online)

    def is_online(self) -> bool:
        return self._value


focus_manager = FocusManager()
online_manager = OnlineManager()


class QueryState:
    """What the cache knows about one key. Replaced, never changed, so screens can compare by identity."""

    __slots__ = ("status", "fetch_status", "data", "error", "data_updated_at", "error_updated_at",
                 "failure_count", "is_invalidated")

    def __init__(self, status: str = "pending", fetch_status: str = "idle", data: Any = None,
                 error: Optional[BaseException] = None, data_updated_at: float = 0.0, error_updated_at: float = 0.0,
                 failure_count: int = 0, is_invalidated: bool = False) -> None:
        self.status = status
        self.fetch_status = fetch_status
        self.data = data
        self.error = error
        self.data_updated_at = data_updated_at
        self.error_updated_at = error_updated_at
        self.failure_count = failure_count
        self.is_invalidated = is_invalidated

    def replace(self, **changes: Any) -> "QueryState":
        values = {name: getattr(self, name) for name in self.__slots__}
        values.update(changes)
        return QueryState(**values)

    def __repr__(self) -> str:
        return "QueryState(status=%r, fetch_status=%r, data=%r, error=%r)" % (
            self.status, self.fetch_status, self.data, self.error)


class Query:
    """One cached key: its state, the screens watching it, and the fetch in flight (shared by all of them)."""

    def __init__(self, client: "QueryClient", key: Key) -> None:
        self.client = client
        self.key = list(key)
        self.hash = hash_key(key)
        self.state = QueryState()
        self.fn: Optional[Callable[..., Any]] = None
        self.options: Dict[str, Any] = {}
        self.observers: List[Any] = []
        self._listeners: List[Callable[[], None]] = []
        self._task: Optional[Any] = None
        self._run_id = 0
        self._outcome: Any = None
        self._gc: Optional[asyncio.TimerHandle] = None
        self.fetch_meta: Any = None

    # --- watching -------------------------------------------------------------------

    def subscribe(self, fn: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn) if fn in self._listeners else None

    def _set(self, state: QueryState) -> None:
        self.state = state
        for fn in list(self._listeners):
            fn()
        self.client._notify(self)

    def add_observer(self, observer: Any) -> None:
        if self._gc is not None:
            self._gc.cancel()
            self._gc = None
        self.observers.append(observer)

    def remove_observer(self, observer: Any) -> None:
        if observer in self.observers:
            self.observers.remove(observer)
        if not self.observers:
            self._schedule_gc()

    def _schedule_gc(self) -> None:
        gc_time = self.options.get("gc_time", self.client.gc_time)
        loop = _running_loop()
        if loop is None or gc_time is None or gc_time == float("inf"):
            return
        self._gc = loop.call_later(gc_time, self._collect)

    def _collect(self) -> None:
        self._gc = None
        if not self.observers and self.state.fetch_status == "idle":
            self.client._remove(self)

    # --- freshness ------------------------------------------------------------------

    def is_stale(self, stale_time: Optional[float] = None) -> bool:
        s = self.state
        if s.status != "success" or s.is_invalidated:
            return True
        if stale_time is None:
            stale_time = self.options.get("stale_time", self.client.stale_time)
        return _now() - s.data_updated_at >= stale_time

    @property
    def is_active(self) -> bool:
        return any(getattr(o, "enabled", True) for o in self.observers)

    # --- fetching -------------------------------------------------------------------

    def fetch(self, fn: Optional[Callable[..., Any]] = None, options: Optional[Dict[str, Any]] = None,
              cancel_running: bool = False, one_off: Optional[Callable[..., Any]] = None,
              meta: Any = None) -> Optional[Any]:
        """Starts a fetch, or joins the one already running. Returns its task (None without an event loop).

        `one_off` runs instead of the query function this time only (an infinite query's next page); `meta`
        says why the fetch runs and stays on `fetch_meta` until it ends."""
        if fn is not None:
            self.fn = fn
        if options:
            self.options = {**self.options, **options}
        run_fn = one_off or self.fn
        if run_fn is None:
            raise RuntimeError("query %r has no query function" % (self.key,))
        if self._task is not None and not self._task.done():
            if not cancel_running:
                return self._task
            self._task.cancel()
        self._run_id += 1
        run_id = self._run_id
        self.fetch_meta = meta
        self._set(self.state.replace(fetch_status="fetching" if online_manager.is_online() else "paused",
                                     failure_count=0 if self.state.status != "error" else self.state.failure_count))
        self._task = spawn(self._run(run_id, run_fn))
        if self._task is None:  # no loop: nothing can run, so the query stays as it was
            self._set(self.state.replace(fetch_status="idle"))
        return self._task

    async def _run(self, run_id: int, fn: Callable[..., Any]) -> None:
        retry = self.options.get("retry", self.client.retry)
        delay = self.options.get("retry_delay", self.client.retry_delay)
        failures = 0
        try:
            while True:
                await self._wait_online(run_id)
                try:
                    data = await _call(fn)
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001
                    failures += 1
                    if run_id != self._run_id:
                        return
                    if _should_retry(retry, failures - 1, error):
                        self._set(self.state.replace(failure_count=failures))
                        wait = delay(failures - 1, error) if callable(delay) else delay
                        if wait:
                            await asyncio.sleep(wait)
                        continue
                    self._outcome = error
                    self._set(self.state.replace(status="error", fetch_status="idle", error=error,
                                                 error_updated_at=_now(), failure_count=failures))
                    return
                if run_id != self._run_id:
                    return
                self._outcome = None
                self._set(self.state.replace(status="success", fetch_status="idle", data=data, error=None,
                                             data_updated_at=_now(), failure_count=0, is_invalidated=False))
                return
        except asyncio.CancelledError:
            if run_id == self._run_id:
                self._outcome = asyncio.CancelledError()
                self._set(self.state.replace(fetch_status="idle"))
            raise

    async def _wait_online(self, run_id: int) -> None:
        if online_manager.is_online():
            return
        if run_id == self._run_id and self.state.fetch_status != "paused":
            self._set(self.state.replace(fetch_status="paused"))
        back = asyncio.get_running_loop().create_future()
        off = online_manager.subscribe(lambda on: on and not back.done() and back.set_result(None))
        try:
            await back
        finally:
            off()
        if run_id == self._run_id:
            self._set(self.state.replace(fetch_status="fetching"))

    async def result(self, task: Any) -> Any:
        """The data a fetch task produced, or its error raised."""
        if task is None:
            raise RuntimeError("fetching needs a running asyncio loop")
        await asyncio.shield(task)
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self.state.data

    def cancel(self) -> None:
        if self._task is not None and not self._task.done():
            self._run_id += 1
            self._task.cancel()
            self._set(self.state.replace(fetch_status="idle"))

    def set_data(self, data: Any, updated_at: Optional[float] = None) -> None:
        self._set(self.state.replace(status="success", data=data, error=None, is_invalidated=False,
                                     data_updated_at=_now() if updated_at is None else updated_at))

    def invalidate(self) -> None:
        if not self.state.is_invalidated:
            self._set(self.state.replace(is_invalidated=True))

    def __repr__(self) -> str:
        return "Query(%r, %r)" % (self.key, self.state)


def _should_retry(retry: Retry, failures: int, error: BaseException) -> bool:
    if callable(retry):
        return bool(retry(failures, error))
    if retry is True:
        return True
    return failures < int(retry or 0)


def default_retry_delay(failures: int, _error: BaseException) -> float:
    """1, 2, 4 ... seconds, at most 30."""
    return min(2.0 ** failures, 30.0)


class QueryClient:
    """The cache every `use_query` reads. Make one per app and hand it to `QueryClientProvider`."""

    def __init__(self, *, stale_time: float = 0.0, gc_time: float = 300.0, retry: Retry = 3,
                 retry_delay: Union[float, Callable[[int, BaseException], float]] = default_retry_delay,
                 refetch_on_window_focus: bool = True, refetch_on_reconnect: bool = True,
                 mutation_retry: Retry = 0) -> None:
        self.stale_time = stale_time
        self.gc_time = gc_time
        self.retry = retry
        self.retry_delay = retry_delay
        self.refetch_on_window_focus = refetch_on_window_focus
        self.refetch_on_reconnect = refetch_on_reconnect
        self.mutation_retry = mutation_retry
        self._queries: Dict[str, Query] = {}
        self._listeners: List[Callable[[Query], None]] = []
        self._mutations = 0
        self._mounts = 0
        self._unsubscribe: List[Callable[[], None]] = []

    # --- the cache ------------------------------------------------------------------

    def build(self, key: Key) -> Query:
        """The query for `key`, created empty the first time."""
        h = hash_key(key)
        q = self._queries.get(h)
        if q is None:
            q = self._queries[h] = Query(self, key)
        return q

    def find(self, key: Key) -> Optional[Query]:
        return self._queries.get(hash_key(key))

    def find_all(self, key: Optional[Key] = None, *, exact: bool = False,
                 predicate: Optional[Callable[[Query], bool]] = None, type: str = "all") -> List[Query]:
        """Queries whose key starts with `key`. `type`: "all", "active" (on screen) or "inactive"."""
        out = []
        for q in list(self._queries.values()):
            if not matches_key(q.key, key, exact):
                continue
            if type == "active" and not q.is_active or type == "inactive" and q.is_active:
                continue
            if predicate is not None and not predicate(q):
                continue
            out.append(q)
        return out

    def subscribe(self, fn: Callable[[Query], None]) -> Callable[[], None]:
        """Hears every change to any query."""
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn) if fn in self._listeners else None

    def _notify(self, query: Query) -> None:
        for fn in list(self._listeners):
            fn(query)

    def _remove(self, query: Query) -> None:
        if self._queries.get(query.hash) is query:
            del self._queries[query.hash]
            query.cancel()

    # --- reading and writing data ---------------------------------------------------

    def get_query_data(self, key: Key) -> Any:
        q = self.find(key)
        return None if q is None else q.state.data

    def get_query_state(self, key: Key) -> Optional[QueryState]:
        q = self.find(key)
        return None if q is None else q.state

    def set_query_data(self, key: Key, updater: Any) -> Any:
        """Writes data for `key`. `updater` is the new value or a function of the old one; None from it skips."""
        q = self.build(key)
        data = updater(q.state.data) if callable(updater) else updater
        if data is None and callable(updater):
            return None
        q.set_data(data)
        return data

    def set_queries_data(self, key: Key, updater: Callable[[Any], Any]) -> None:
        """`set_query_data` on every cached key starting with `key` that has data."""
        for q in self.find_all(key):
            if q.state.status == "success":
                data = updater(q.state.data)
                if data is not None:
                    q.set_data(data)

    async def fetch_query(self, key: Key, fn: Callable[..., Any], *, stale_time: float = 0.0,
                          **options: Any) -> Any:
        """The data for `key`: from the cache when fresher than `stale_time`, else fetched (once, however many ask)."""
        q = self.build(key)
        if options:
            q.options = {**q.options, **options}
        if not q.is_stale(stale_time) and q.state.fetch_status == "idle":
            return q.state.data
        return await q.result(q.fetch(fn))

    async def prefetch_query(self, key: Key, fn: Callable[..., Any], **options: Any) -> None:
        """`fetch_query` that keeps errors to itself: warm the cache before a screen opens."""
        try:
            await self.fetch_query(key, fn, **options)
        except Exception:  # noqa: BLE001
            pass

    async def ensure_query_data(self, key: Key, fn: Callable[..., Any], **options: Any) -> Any:
        """Cached data however old, else fetched."""
        q = self.find(key)
        if q is not None and q.state.status == "success":
            return q.state.data
        return await self.fetch_query(key, fn, **options)

    # --- invalidating ---------------------------------------------------------------

    def invalidate_queries(self, key: Optional[Key] = None, *, exact: bool = False,
                           predicate: Optional[Callable[[Query], bool]] = None,
                           refetch_type: str = "active") -> List[Any]:
        """Marks matching queries stale, then refetches those of `refetch_type` ("active", "all" or "none").
        Returns the refetch tasks, so `await asyncio.gather(*tasks)` waits for fresh data."""
        tasks = []
        for q in self.find_all(key, exact=exact, predicate=predicate):
            q.invalidate()
            if refetch_type == "all" or refetch_type == "active" and q.is_active:
                if q.fn is not None:
                    tasks.append(q.fetch(cancel_running=True))
        return [t for t in tasks if t is not None]

    def refetch_queries(self, key: Optional[Key] = None, *, exact: bool = False, type: str = "all") -> List[Any]:
        tasks = [q.fetch(cancel_running=True) for q in self.find_all(key, exact=exact, type=type) if q.fn]
        return [t for t in tasks if t is not None]

    def cancel_queries(self, key: Optional[Key] = None, *, exact: bool = False) -> None:
        """Stops matching fetches in flight. Their data stays; a late answer is dropped."""
        for q in self.find_all(key, exact=exact):
            q.cancel()

    def remove_queries(self, key: Optional[Key] = None, *, exact: bool = False) -> None:
        """Drops matching queries from the cache. Screens still showing them fetch again on their next render."""
        for q in self.find_all(key, exact=exact):
            self._remove(q)
            q._set(QueryState())

    def reset_queries(self, key: Optional[Key] = None, *, exact: bool = False) -> List[Any]:
        """Back to empty, then active ones refetch."""
        tasks = []
        for q in self.find_all(key, exact=exact):
            q.cancel()
            q._set(QueryState())
            if q.is_active and q.fn is not None:
                tasks.append(q.fetch())
        return [t for t in tasks if t is not None]

    def clear(self) -> None:
        """Empties the cache: sign-out."""
        for q in list(self._queries.values()):
            self._remove(q)
            q._set(QueryState())

    def is_fetching(self, key: Optional[Key] = None) -> int:
        return sum(1 for q in self.find_all(key) if q.state.fetch_status == "fetching")

    def is_mutating(self) -> int:
        return self._mutations

    # --- focus and network ----------------------------------------------------------

    def _refetch_stale(self, flag: str) -> None:
        for q in list(self._queries.values()):
            for o in q.observers:
                if getattr(o, "enabled", True) and o.option(flag) and q.is_stale(o.option("stale_time")):
                    q.fetch()
                    break

    def _on_focus(self, focused: bool) -> None:
        if focused:
            self._refetch_stale("refetch_on_window_focus")

    def _on_online(self, online: bool) -> None:
        if online:
            self._refetch_stale("refetch_on_reconnect")

    def mount(self) -> Callable[[], None]:
        """Starts refetching on focus and reconnect; `QueryClientProvider` calls it. Returns the matching undo."""
        self._mounts += 1
        if self._mounts == 1:
            self._unsubscribe = [focus_manager.subscribe(self._on_focus), online_manager.subscribe(self._on_online)]

        def undo() -> None:
            self._mounts -= 1
            if self._mounts == 0:
                self.unmount()

        return undo

    def unmount(self) -> None:
        """Stops listening to focus and network changes, however many providers mounted this client."""
        self._mounts = 0
        for off in self._unsubscribe:
            off()
        self._unsubscribe = []

    def __repr__(self) -> str:
        return "QueryClient(%d queries)" % len(self._queries)
