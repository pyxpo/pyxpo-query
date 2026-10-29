"""pyxpo hooks over the query cache.

    @component
    def Posts(project_id):
        q = use_query(["posts", project_id], lambda: api.list_posts(project_id), stale_time=60)
        if q.is_pending:
            return Spinner()
        if q.is_error:
            return ErrorState(q.error, on_retry=q.refetch)
        return PostList(q.data)
"""
from __future__ import annotations

import asyncio
import inspect
from typing import Any, Callable, Dict, List, NamedTuple, Optional

from pyxpo import component, create_context, use_callback, use_context, use_effect, use_memo, use_ref, use_state, \
    use_sync_external_store
from pyxpo.module_api import spawn

from .client import Key, Query, QueryClient, QueryState, _call, _now, _running_loop, _should_retry, online_manager

_UNSET: Any = object()

QueryClientContext = create_context(None)


@component
def QueryClientProvider(*children: Any, client: QueryClient):
    use_effect(client.mount, [client])
    return QueryClientContext.Provider(*children, value=client)


def use_query_client() -> QueryClient:
    client = use_context(QueryClientContext)
    if client is None:
        raise RuntimeError("use_query needs a QueryClientProvider above it")
    return client


def keep_previous_data(previous: Any) -> Any:
    """`placeholder_data=keep_previous_data`: show the last key's data while the new key loads."""
    return previous


class _Observer:
    """One screen's interest in one query, with its own options."""

    def __init__(self, client: QueryClient) -> None:
        self.client = client
        self.enabled = True
        self.options: Dict[str, Any] = {}

    def option(self, name: str) -> Any:
        value = self.options.get(name)
        return getattr(self.client, name, None) if value is None else value


class QueryResult:
    """What `use_query` returns. `status`: "pending", "error" or "success"; `fetch_status`: "fetching", "paused"
    or "idle"."""

    def __init__(self, state: QueryState, data: Any, is_placeholder_data: bool, is_stale: bool,
                 refetch: Callable[[], Any]) -> None:
        self.state = state
        self.data = data
        self.error = state.error
        self.status = "success" if is_placeholder_data else state.status
        self.fetch_status = state.fetch_status
        self.data_updated_at = state.data_updated_at
        self.error_updated_at = state.error_updated_at
        self.failure_count = state.failure_count
        self.is_placeholder_data = is_placeholder_data
        self.is_stale = is_stale
        self.refetch = refetch

    is_pending = property(lambda self: self.status == "pending")
    is_success = property(lambda self: self.status == "success")
    is_error = property(lambda self: self.status == "error")
    is_fetching = property(lambda self: self.fetch_status == "fetching")
    is_paused = property(lambda self: self.fetch_status == "paused")
    is_loading = property(lambda self: self.is_pending and self.is_fetching)
    is_refetching = property(lambda self: self.is_fetching and not self.is_pending)
    is_fetched = property(lambda self: self.state.data_updated_at > 0 or self.state.error_updated_at > 0)
    is_load_error = property(lambda self: self.is_error and self.state.data_updated_at == 0)
    is_refetch_error = property(lambda self: self.is_error and self.state.data_updated_at > 0)

    def __repr__(self) -> str:
        return "QueryResult(status=%r, fetch_status=%r, data=%r, error=%r)" % (
            self.status, self.fetch_status, self.data, self.error)


def _shown_state(query: Query, enabled: bool) -> QueryState:
    """The state to render: a query about to load on mount already reads as loading, so the first frame shows a
    spinner and not an empty screen."""
    s = query.state
    if enabled and s.status == "pending" and s.fetch_status == "idle" and query.fn is not None:
        return s.replace(fetch_status="fetching" if online_manager.is_online() else "paused")
    return s


def _query_options(stale_time: Any, gc_time: Any, retry: Any, retry_delay: Any) -> Dict[str, Any]:
    out = {}
    for name, value in (("stale_time", stale_time), ("gc_time", gc_time), ("retry", retry),
                        ("retry_delay", retry_delay)):
        if value is not _UNSET and value is not None:
            out[name] = value
    return out


def _use_observed(client: QueryClient, key: Key, fn: Callable[..., Any], enabled: bool, options: Dict[str, Any],
                  observer_options: Dict[str, Any], initial_data: Any, refetch_on_mount: Any,
                  refetch_interval: Optional[float]) -> Query:
    """The shared part of `use_query` and `use_infinite_query`: watch the query, fetch when it needs it."""
    query = client.build(key)
    query.fn = fn  # the latest closure, so a refetch sees this render's values
    if options:
        query.options = {**query.options, **options}
    if initial_data is not None and query.state.status == "pending" and query.state.data_updated_at == 0:
        query.set_data(initial_data() if callable(initial_data) else initial_data)

    observer = use_memo(lambda: _Observer(client), [client])
    observer.enabled = bool(enabled)
    observer.options = observer_options

    subscribe = use_callback(lambda on_change: query.subscribe(on_change), [query])
    use_sync_external_store(subscribe, lambda: query.state)

    def watch() -> Callable[[], None]:
        query.add_observer(observer)
        return lambda: query.remove_observer(observer)

    use_effect(watch, [query])

    def maybe_fetch() -> None:
        if not enabled or query.state.fetch_status != "idle":
            return
        s = query.state
        if s.status == "pending" or s.is_invalidated:
            query.fetch()
        elif refetch_on_mount is not False and (refetch_on_mount == "always" or query.is_stale(
                observer.option("stale_time"))):
            query.fetch()

    use_effect(maybe_fetch, [query, bool(enabled)])

    def poll() -> Optional[Callable[[], None]]:
        loop = _running_loop()
        if not enabled or not refetch_interval or loop is None:
            return None
        handle: List[Any] = [None]

        def tick() -> None:
            query.fetch()
            handle[0] = loop.call_later(refetch_interval, tick)

        handle[0] = loop.call_later(refetch_interval, tick)
        return lambda: handle[0].cancel()

    use_effect(poll, [query, bool(enabled), refetch_interval])
    return query


def use_query(key: Key, fn: Callable[[], Any], *, enabled: bool = True, stale_time: Optional[float] = None,
              gc_time: Optional[float] = None, retry: Any = _UNSET, retry_delay: Any = _UNSET,
              select: Optional[Callable[[Any], Any]] = None, placeholder_data: Any = None, initial_data: Any = None,
              refetch_interval: Optional[float] = None, refetch_on_mount: Any = True,
              refetch_on_window_focus: Optional[bool] = None,
              refetch_on_reconnect: Optional[bool] = None) -> QueryResult:
    """Reads `key` from the cache and fetches it with `fn` (sync or async) when missing or stale.

    `select` maps the data for this screen only (recomputed when the data changes). `placeholder_data` shows
    while the key has no data: a value, or `keep_previous_data`."""
    client = use_query_client()
    query = _use_observed(client, key, fn, enabled, _query_options(stale_time, gc_time, retry, retry_delay),
                          {"stale_time": stale_time, "refetch_on_window_focus": refetch_on_window_focus,
                           "refetch_on_reconnect": refetch_on_reconnect},
                          initial_data, refetch_on_mount, refetch_interval)
    state = _shown_state(query, enabled)
    selected = use_memo(lambda: select(state.data) if select and state.status == "success" else state.data,
                        [state.data, state.status])
    previous = use_ref(None)
    data, placeholder = selected, False
    if state.status == "pending" and placeholder_data is not None:
        data = placeholder_data(previous.current) if callable(placeholder_data) else placeholder_data
        placeholder = data is not None
    elif state.status == "success":
        previous.current = selected
    refetch = use_callback(lambda: query.fetch(cancel_running=True), [query])
    stale = query.is_stale(stale_time if stale_time is not None else client.stale_time)
    return QueryResult(state, data, placeholder, stale, refetch)


# --- infinite lists ---------------------------------------------------------------


class InfiniteData(NamedTuple):
    pages: List[Any]
    page_params: List[Any]


def _flex(fn: Callable[..., Any], *args: Any) -> Any:
    """Calls `fn` with as many of `args` as it takes, so `lambda last: ...` and `lambda last, pages: ...` both work."""
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return fn(*args)
    if any(p.kind == p.VAR_POSITIONAL for p in params):
        return fn(*args)
    n = sum(1 for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD))
    return fn(*args[:n])


class InfiniteQueryResult(QueryResult):
    def __init__(self, base: QueryResult, has_next_page: bool, has_previous_page: bool, direction: Any,
                 fetch_next_page: Callable[[], Any], fetch_previous_page: Callable[[], Any]) -> None:
        super().__init__(base.state, base.data, base.is_placeholder_data, base.is_stale, base.refetch)
        self.has_next_page = has_next_page
        self.has_previous_page = has_previous_page
        self.is_fetching_next_page = self.is_fetching and direction == "next"
        self.is_fetching_previous_page = self.is_fetching and direction == "previous"
        self.fetch_next_page = fetch_next_page
        self.fetch_previous_page = fetch_previous_page

    @property
    def pages(self) -> List[Any]:
        return list(self.data.pages) if isinstance(self.data, InfiniteData) else []

    @property
    def items(self) -> List[Any]:
        """Every page's items in one list, for pages that are lists or have an `items` field."""
        out: List[Any] = []
        for page in self.pages:
            out.extend(page if isinstance(page, list) else (page.get("items", []) if isinstance(page, dict)
                                                             else getattr(page, "items", [])))
        return out


def use_infinite_query(key: Key, fn: Callable[[Any], Any], *, initial_page_param: Any,
                       get_next_page_param: Callable[..., Any],
                       get_previous_page_param: Optional[Callable[..., Any]] = None, max_pages: Optional[int] = None,
                       enabled: bool = True, stale_time: Optional[float] = None, gc_time: Optional[float] = None,
                       retry: Any = _UNSET, retry_delay: Any = _UNSET, placeholder_data: Any = None,
                       refetch_on_mount: Any = True, refetch_on_window_focus: Optional[bool] = None,
                       refetch_on_reconnect: Optional[bool] = None) -> InfiniteQueryResult:
    """A list loaded page by page. `fn(page_param)` loads one page; `get_next_page_param(last_page, pages,
    last_param, params)` names the next one, or None at the end. Data is `InfiniteData(pages, page_params)`;
    a refetch reloads every page in turn, so the list stays in step."""
    client = use_query_client()
    holder = use_ref(None)

    def next_param(data: InfiniteData) -> Any:
        if not data.pages:
            return initial_page_param
        return _flex(get_next_page_param, data.pages[-1], data.pages, data.page_params[-1], data.page_params)

    def prev_param(data: InfiniteData) -> Any:
        if get_previous_page_param is None or not data.pages:
            return None
        return _flex(get_previous_page_param, data.pages[0], data.pages, data.page_params[0], data.page_params)

    async def refetch_all() -> InfiniteData:
        old = holder.current.state.data if holder.current is not None else None
        count = len(old.pages) if isinstance(old, InfiniteData) and old.pages else 1
        data = InfiniteData([], [])
        param = initial_page_param
        for i in range(count):
            if i:
                param = next_param(data)
                if param is None:
                    break
            page = await _call(fn, param)
            data = InfiniteData(data.pages + [page], data.page_params + [param])
        return data

    query = _use_observed(client, key, refetch_all, enabled, _query_options(stale_time, gc_time, retry, retry_delay),
                          {"stale_time": stale_time, "refetch_on_window_focus": refetch_on_window_focus,
                           "refetch_on_reconnect": refetch_on_reconnect},
                          None, refetch_on_mount, None)
    holder.current = query
    state = _shown_state(query, enabled)
    data = state.data if isinstance(state.data, InfiniteData) else None
    has_next = data is not None and next_param(data) is not None
    has_prev = data is not None and prev_param(data) is not None

    def fetch_page(direction: str) -> Any:
        current = query.state.data
        if not isinstance(current, InfiniteData) or query.state.fetch_status != "idle":
            return None
        param = next_param(current) if direction == "next" else prev_param(current)
        if param is None:
            return None

        async def one() -> InfiniteData:
            page = await _call(fn, param)
            base = query.state.data if isinstance(query.state.data, InfiniteData) else current
            if direction == "next":
                pages, params = base.pages + [page], base.page_params + [param]
                if max_pages and len(pages) > max_pages:
                    pages, params = pages[-max_pages:], params[-max_pages:]
            else:
                pages, params = [page] + base.pages, [param] + base.page_params
                if max_pages and len(pages) > max_pages:
                    pages, params = pages[:max_pages], params[:max_pages]
            return InfiniteData(pages, params)

        return query.fetch(one_off=one, meta=direction)

    fetch_next = use_callback(lambda: fetch_page("next"), [query, state])
    fetch_prev = use_callback(lambda: fetch_page("previous"), [query, state])

    previous = use_ref(None)
    shown, placeholder = data, False
    if state.status == "pending" and placeholder_data is not None:
        shown = placeholder_data(previous.current) if callable(placeholder_data) else placeholder_data
        placeholder = shown is not None
    elif state.status == "success":
        previous.current = data
    refetch = use_callback(lambda: query.fetch(cancel_running=True), [query])
    stale = query.is_stale(stale_time if stale_time is not None else client.stale_time)
    base = QueryResult(state, shown, placeholder, stale, refetch)
    direction = query.fetch_meta if state.fetch_status != "idle" else None
    return InfiniteQueryResult(base, has_next, has_prev, direction, fetch_next, fetch_prev)


# --- mutations --------------------------------------------------------------------


class MutationState(NamedTuple):
    status: str = "idle"
    data: Any = None
    error: Optional[BaseException] = None
    variables: Any = None
    failure_count: int = 0
    submitted_at: float = 0.0


class MutationResult:
    """What `use_mutation` returns. `status`: "idle", "pending", "success" or "error"."""

    def __init__(self, state: MutationState, mutate: Callable[..., Any], mutate_async: Callable[..., Any],
                 reset: Callable[[], None]) -> None:
        self.status = state.status
        self.data = state.data
        self.error = state.error
        self.variables = state.variables
        self.failure_count = state.failure_count
        self.submitted_at = state.submitted_at
        self.mutate = mutate
        self.mutate_async = mutate_async
        self.reset = reset

    is_idle = property(lambda self: self.status == "idle")
    is_pending = property(lambda self: self.status == "pending")
    is_success = property(lambda self: self.status == "success")
    is_error = property(lambda self: self.status == "error")

    def __repr__(self) -> str:
        return "MutationResult(status=%r, data=%r, error=%r)" % (self.status, self.data, self.error)


async def _maybe(fn: Optional[Callable[..., Any]], *args: Any) -> Any:
    if fn is None:
        return None
    return await _call(fn, *args)


def use_mutation(fn: Callable[[Any], Any], *, on_mutate: Optional[Callable[..., Any]] = None,
                 on_success: Optional[Callable[..., Any]] = None, on_error: Optional[Callable[..., Any]] = None,
                 on_settled: Optional[Callable[..., Any]] = None, retry: Any = _UNSET,
                 retry_delay: Any = _UNSET) -> MutationResult:
    """A write. `mutate(variables)` runs `fn(variables)` in the background; `await mutate_async(variables)`
    returns its data or raises. Callbacks may be async:

        on_mutate(variables) -> context          (an optimistic update; its return goes to the others)
        on_success(data, variables, context)
        on_error(error, variables, context)      (roll the optimistic update back here)
        on_settled(data, error, variables, context)

    `mutate` takes per-call `on_success`, `on_error` and `on_settled` too; they run for the latest call only."""
    client = use_query_client()
    state, set_state = use_state(MutationState)
    latest = use_ref(None)
    latest.current = (fn, on_mutate, on_success, on_error, on_settled, retry, retry_delay)
    calls = use_ref(0)

    async def run(variables: Any, call_success: Any, call_error: Any, call_settled: Any) -> Any:
        f, mutate_cb, success_cb, error_cb, settled_cb, retry_opt, delay_opt = latest.current
        calls.current += 1
        me = calls.current
        retry_opt = client.mutation_retry if retry_opt is _UNSET else retry_opt
        delay_opt = client.retry_delay if delay_opt is _UNSET else delay_opt
        set_state(MutationState("pending", None, None, variables, 0, _now()))
        client._mutations += 1
        context = None
        try:
            context = await _maybe(mutate_cb, variables)
            failures = 0
            while True:
                try:
                    data = await _call(f, variables)
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001
                    failures += 1
                    if not _should_retry(retry_opt, failures - 1, error):
                        raise
                    set_state(lambda s: s._replace(failure_count=failures))
                    wait = delay_opt(failures - 1, error) if callable(delay_opt) else delay_opt
                    if wait:
                        await asyncio.sleep(wait)
        except Exception as error:  # noqa: BLE001
            client._mutations -= 1
            await _maybe(error_cb, error, variables, context)
            await _maybe(settled_cb, None, error, variables, context)
            if me == calls.current:
                set_state(lambda s: s._replace(status="error", error=error))
                await _maybe(call_error, error, variables, context)
                await _maybe(call_settled, None, error, variables, context)
            raise
        client._mutations -= 1
        await _maybe(success_cb, data, variables, context)
        await _maybe(settled_cb, data, None, variables, context)
        if me == calls.current:
            set_state(lambda s: s._replace(status="success", data=data, error=None))
            await _maybe(call_success, data, variables, context)
            await _maybe(call_settled, data, None, variables, context)
        return data

    def mutate_async(variables: Any = None, *, on_success: Any = None, on_error: Any = None,
                     on_settled: Any = None) -> Any:
        return run(variables, on_success, on_error, on_settled)

    def mutate(variables: Any = None, *, on_success: Any = None, on_error: Any = None, on_settled: Any = None) -> Any:
        async def quiet() -> None:
            try:
                await run(variables, on_success, on_error, on_settled)
            except Exception:  # noqa: BLE001  (the error is in the result's state)
                pass

        return spawn(quiet())

    mutate_async = use_callback(mutate_async, [])
    mutate = use_callback(mutate, [])
    reset = use_callback(lambda: set_state(MutationState()), [])
    return MutationResult(state, mutate, mutate_async, reset)


def use_is_fetching(key: Optional[Key] = None) -> int:
    """How many queries under `key` are fetching now: a global spinner."""
    client = use_query_client()
    subscribe = use_callback(lambda on_change: client.subscribe(lambda _q: on_change()), [client])
    return use_sync_external_store(subscribe, lambda: client.is_fetching(key))
