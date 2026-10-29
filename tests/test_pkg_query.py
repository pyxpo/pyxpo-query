"""pyxpo-query: the cache, fetch sharing, retries, freshness, invalidation, infinite lists, mutations and hooks."""
import asyncio

import pytest
from pyxpo import LocalChannel, Text, View, component, use_state
from pyxpo.app import App
from pyxpo.renderers.mock import MockRenderer

from pyxpo_query import (InfiniteData, QueryClient, QueryClientProvider, focus_manager, hash_key, keep_previous_data,
                         matches_key, online_manager, use_infinite_query, use_is_fetching, use_mutation, use_query)


@pytest.fixture(autouse=True)
def managers():
    online_manager.set_online(True)
    focus_manager.set_focused(True)
    yield
    online_manager.set_online(True)
    focus_manager.set_focused(True)


def run(root, client, seconds=0.05, then=None):
    """Mounts `root` under a provider on a live loop; `then(renderer)` runs as a coroutine after `seconds`."""
    async def main():
        ch = LocalChannel()
        r = MockRenderer(ch)

        @component
        def Root():
            return QueryClientProvider(root(), client=client)

        App(Root, ch, asyncio.get_running_loop()).start()
        await asyncio.sleep(seconds)
        if then is not None:
            await then(r)
        client.unmount()
        return r

    return asyncio.run(main())


# --- keys and cache -----------------------------------------------------------------


def test_keys():
    assert hash_key(["posts", {"b": 1, "a": 2}]) == hash_key(("posts", {"a": 2, "b": 1}))
    assert hash_key(["posts", 1]) != hash_key(["posts", "1"])
    with pytest.raises(TypeError):
        hash_key("posts")
    assert matches_key(["posts", "p1", {"status": "draft", "q": ""}], ["posts"])
    assert matches_key(["posts", "p1", {"status": "draft", "q": ""}], ["posts", "p1", {"status": "draft"}])
    assert not matches_key(["posts", "p1"], ["posts", "p2"])
    assert not matches_key(["posts", "p1"], ["posts"], exact=True)
    assert matches_key(["posts"], None)


def test_set_and_get_data_without_loop():
    c = QueryClient()
    assert c.get_query_data(["me"]) is None
    c.set_query_data(["me"], {"name": "Ana"})
    assert c.get_query_data(["me"]) == {"name": "Ana"}
    c.set_query_data(["me"], lambda old: {**old, "name": "Bo"})
    assert c.get_query_state(["me"]).status == "success" and c.get_query_data(["me"])["name"] == "Bo"
    c.set_query_data(["me"], lambda old: None)  # an updater returning None leaves the data alone
    assert c.get_query_data(["me"])["name"] == "Bo"
    c.set_query_data(["posts", 1], [1])
    c.set_query_data(["posts", 2], [2])
    c.set_queries_data(["posts"], lambda old: old + [0])
    assert c.get_query_data(["posts", 2]) == [2, 0]
    c.remove_queries(["posts"])
    assert c.find(["posts", 1]) is None and c.find(["me"]) is not None
    c.clear()
    assert c.find_all() == []


def test_fetch_query_shares_and_caches():
    calls = []

    async def load():
        calls.append(1)
        await asyncio.sleep(0.01)
        return "data"

    async def main():
        c = QueryClient()
        a, b = await asyncio.gather(c.fetch_query(["k"], load), c.fetch_query(["k"], load))
        cached = await c.fetch_query(["k"], load, stale_time=60)
        fresh = await c.ensure_query_data(["k"], load)
        return a, b, cached, fresh

    assert asyncio.run(main()) == ("data",) * 4
    assert len(calls) == 1


def test_fetch_query_raises_after_retries():
    tries = []

    def boom():
        tries.append(1)
        raise ValueError("no")

    async def main():
        c = QueryClient(retry=2, retry_delay=0)
        with pytest.raises(ValueError):
            await c.fetch_query(["k"], boom)
        return c.get_query_state(["k"])

    state = asyncio.run(main())
    assert len(tries) == 3 and state.status == "error" and state.failure_count == 3


def test_retry_predicate():
    tries = []

    def boom():
        tries.append(1)
        raise KeyError("x")

    async def main():
        c = QueryClient(retry=lambda n, e: n < 5 and not isinstance(e, KeyError), retry_delay=0)
        with pytest.raises(KeyError):
            await c.fetch_query(["k"], boom)

    asyncio.run(main())
    assert len(tries) == 1


# --- use_query ----------------------------------------------------------------------


def test_use_query_loads_and_shares():
    calls = []

    async def load():
        calls.append(1)
        await asyncio.sleep(0.01)
        return ["a", "b"]

    @component
    def List_():
        q = use_query(["posts"], load)
        return Text("loading" if q.is_loading else ",".join(q.data))

    @component
    def Screen():
        return View(List_(), List_())

    first = []

    async def check(r):
        first.append(r.texts())
        await asyncio.sleep(0.05)

    client = QueryClient(retry=0)
    r = run(Screen, client, 0.001, check)
    assert first[0] == ["loading", "loading"]
    assert r.texts() == ["a,b", "a,b"] and len(calls) == 1


def test_use_query_error_and_refetch():
    state = {"fail": True}
    seen = []

    def load():
        if state["fail"]:
            raise RuntimeError("down")
        return "ok"

    @component
    def Screen():
        q = use_query(["x"], load)
        seen.append(q)
        return Text(str(q.error) if q.is_error else (q.data or "…"))

    async def retry(r):
        assert r.texts() == ["down"] and seen[-1].is_load_error
        state["fail"] = False
        await seen[-1].refetch()
        await asyncio.sleep(0.01)

    r = run(Screen, QueryClient(retry=0), 0.05, retry)
    assert r.texts() == ["ok"] and seen[-1].is_success and seen[-1].error is None


def test_disabled_query_waits():
    calls = []

    @component
    def Screen():
        on, set_on = use_state(False)
        q = use_query(["x"], lambda: calls.append(1) or "v", enabled=on)
        Screen.flip = lambda: set_on(True)
        return Text("%s:%s" % (q.status, q.data))

    async def flip(r):
        assert r.texts() == ["pending:None"] and calls == []
        Screen.flip()
        await asyncio.sleep(0.02)

    r = run(Screen, QueryClient(), 0.02, flip)
    assert r.texts() == ["success:v"] and calls == [1]


def test_select_initial_and_placeholder():
    @component
    def Screen():
        q = use_query(["n"], lambda: 3, select=lambda n: n * 10)
        i = use_query(["i"], lambda: "fetched", initial_data="seed", stale_time=60)
        return Text("%s|%s" % (q.data, i.data))

    r = run(Screen, QueryClient())
    assert r.texts() == ["30|seed"]


def test_keep_previous_data_between_keys():
    seen = []

    async def load(page):
        await asyncio.sleep(0.02)
        return "page%d" % page

    @component
    def Screen():
        page, set_page = use_state(1)
        q = use_query(["p", page], lambda: load(page), placeholder_data=keep_previous_data)
        Screen.next = lambda: set_page(2)
        seen.append((q.data, q.is_placeholder_data))
        return Text(q.data or "-")

    async def go(r):
        Screen.next()
        await asyncio.sleep(0.005)
        mid = r.texts()
        await asyncio.sleep(0.05)
        assert mid == ["page1"]

    r = run(Screen, QueryClient(), 0.05, go)
    assert r.texts() == ["page2"] and ("page1", True) in seen


def test_stale_time_and_refetch_on_mount():
    calls = []

    @component
    def Reader():
        q = use_query(["k"], lambda: calls.append(1) or len(calls), stale_time=60)
        return Text(str(q.data))

    @component
    def Screen():
        shown, set_shown = use_state(True)
        Screen.toggle = lambda: set_shown(lambda s: not s)
        return View(Reader() if shown else Text("gone"))

    async def remount(r):
        Screen.toggle()
        await asyncio.sleep(0.01)
        Screen.toggle()
        await asyncio.sleep(0.02)

    r = run(Screen, QueryClient(), 0.02, remount)
    assert r.texts() == ["1"] and calls == [1]  # fresh for 60 s: no second fetch


def test_invalidate_refetches_active():
    calls = []

    @component
    def Screen():
        q = use_query(["posts", "p1"], lambda: calls.append(1) or len(calls))
        return Text(str(q.data))

    client = QueryClient()

    async def invalidate(r):
        assert r.texts() == ["1"]
        tasks = client.invalidate_queries(["posts"])
        await asyncio.gather(*tasks)
        await asyncio.sleep(0.01)

    r = run(Screen, client, 0.02, invalidate)
    assert r.texts() == ["2"]


def test_set_query_data_rerenders():
    client = QueryClient()

    @component
    def Screen():
        q = use_query(["me"], lambda: {"name": "Ana"}, stale_time=60)
        return Text(q.data["name"] if q.data else "-")

    async def edit(r):
        client.set_query_data(["me"], lambda old: {**old, "name": "Bo"})
        await asyncio.sleep(0.01)

    r = run(Screen, client, 0.02, edit)
    assert r.texts() == ["Bo"]


def test_refetch_on_focus_and_reconnect():
    calls = []

    @component
    def Screen():
        q = use_query(["k"], lambda: calls.append(1) or len(calls))
        return Text(str(q.data))

    async def events(r):
        focus_manager.set_focused(False)
        focus_manager.set_focused(True)
        await asyncio.sleep(0.01)
        online_manager.set_online(False)
        online_manager.set_online(True)
        await asyncio.sleep(0.01)

    r = run(Screen, QueryClient(), 0.02, events)
    assert r.texts() == ["3"]


def test_offline_pauses_until_online():
    seen = []

    @component
    def Screen():
        q = use_query(["k"], lambda: "v")
        seen.append(q.fetch_status)
        return Text("%s/%s" % (q.fetch_status, q.data))

    async def back(r):
        assert r.texts() == ["paused/None"]
        online_manager.set_online(True)
        await asyncio.sleep(0.02)

    online_manager.set_online(False)
    r = run(Screen, QueryClient(), 0.02, back)
    assert r.texts() == ["idle/v"]


def test_refetch_interval():
    calls = []

    @component
    def Screen():
        q = use_query(["k"], lambda: calls.append(1) or len(calls), refetch_interval=0.02)
        return Text(str(q.data))

    run(Screen, QueryClient(), 0.09)
    assert len(calls) >= 3


def test_is_fetching_counter():
    async def slow():
        await asyncio.sleep(0.03)
        return 1

    @component
    def Screen():
        use_query(["a"], slow)
        use_query(["b"], slow)
        return Text(str(use_is_fetching()))

    mid = []

    async def wait(r):
        mid.append(r.texts())
        await asyncio.sleep(0.05)

    r = run(Screen, QueryClient(), 0.01, wait)
    assert mid[0] == ["2"] and r.texts() == ["0"]


def test_missing_provider():
    errors = []

    @component
    def Bad():
        try:
            use_query(["x"], lambda: 1)
        except RuntimeError as e:
            errors.append(str(e))
        return Text("x")

    ch = LocalChannel()
    MockRenderer(ch)
    App(Bad, ch, None).start()
    assert errors and "QueryClientProvider" in errors[0]


def test_gc_removes_unused_queries():
    client = QueryClient(gc_time=0.01)

    @component
    def Reader():
        use_query(["k"], lambda: 1)
        return Text("r")

    @component
    def Screen():
        shown, set_shown = use_state(True)
        Screen.hide = lambda: set_shown(False)
        return View(Reader() if shown else Text("gone"))

    async def hide(r):
        Screen.hide()
        await asyncio.sleep(0.05)

    run(Screen, client, 0.02, hide)
    assert client.find(["k"]) is None


# --- infinite -----------------------------------------------------------------------


def _pages(cursor):
    start = cursor or 0
    nxt = start + 2 if start + 2 < 6 else None
    return {"items": [start, start + 1], "next": nxt}


def test_infinite_query_pages_and_refetch():
    seen = []
    calls = []

    async def load(cursor):
        calls.append(cursor)
        await asyncio.sleep(0.005)
        return _pages(cursor)

    @component
    def Screen():
        q = use_infinite_query(["feed"], load, initial_page_param=None,
                               get_next_page_param=lambda last: last["next"])
        seen.append(q)
        return Text(",".join(str(i) for i in q.items))

    async def scroll(r):
        assert r.texts() == ["0,1"] and seen[-1].has_next_page
        await seen[-1].fetch_next_page()
        await asyncio.sleep(0.01)
        assert r.texts() == ["0,1,2,3"]
        await seen[-1].fetch_next_page()
        await asyncio.sleep(0.01)
        assert r.texts() == ["0,1,2,3,4,5"] and not seen[-1].has_next_page
        assert seen[-1].fetch_next_page() is None
        calls.clear()
        await seen[-1].refetch()
        await asyncio.sleep(0.01)

    r = run(Screen, QueryClient(), 0.03, scroll)
    assert r.texts() == ["0,1,2,3,4,5"] and calls == [None, 2, 4]
    data = seen[-1].data
    assert isinstance(data, InfiniteData) and data.page_params == [None, 2, 4]
    assert any(q.is_fetching_next_page for q in seen)


def test_infinite_max_pages():
    seen = []

    @component
    def Screen():
        q = use_infinite_query(["feed"], _pages, initial_page_param=None, max_pages=2,
                               get_next_page_param=lambda last, pages: last["next"])
        seen.append(q)
        return Text(",".join(str(i) for i in q.items))

    async def scroll(r):
        await seen[-1].fetch_next_page()
        await asyncio.sleep(0.01)
        await seen[-1].fetch_next_page()
        await asyncio.sleep(0.01)

    r = run(Screen, QueryClient(), 0.02, scroll)
    assert r.texts() == ["2,3,4,5"]


# --- mutations ----------------------------------------------------------------------


def test_mutation_flow_and_invalidate():
    client = QueryClient()
    events = []
    seen = []
    server = {"title": "old"}

    @component
    def Screen():
        q = use_query(["post"], lambda: dict(server))

        async def save(title):
            await asyncio.sleep(0.005)
            server["title"] = title
            return title

        def optimistic(title):
            previous = client.get_query_data(["post"])
            client.set_query_data(["post"], {"title": title})
            return previous

        m = use_mutation(save, on_mutate=optimistic,
                         on_success=lambda data, v, ctx: events.append(("ok", data, ctx["title"])),
                         on_settled=lambda *a: client.invalidate_queries(["post"]))
        seen.append(m)
        return Text("%s/%s" % (q.data["title"] if q.data else "-", m.status))

    async def act(r):
        seen[-1].mutate("new", on_success=lambda *a: events.append("call"))
        await asyncio.sleep(0.001)
        assert r.texts() == ["new/pending"]
        await asyncio.sleep(0.03)

    r = run(Screen, client, 0.02, act)
    assert r.texts() == ["new/success"]
    assert events == [("ok", "new", "old"), "call"] and client.is_mutating() == 0
    assert seen[-1].data == "new" and seen[-1].variables == "new"


def test_mutation_error_rolls_back_and_async_raises():
    client = QueryClient()
    seen = []

    @component
    def Screen():
        q = use_query(["post"], lambda: "kept", stale_time=60)

        def fail(_v):
            raise ValueError("rejected")

        def optimistic(v):
            previous = client.get_query_data(["post"])
            client.set_query_data(["post"], v)
            return previous

        m = use_mutation(fail, on_mutate=optimistic, on_error=lambda e, v, ctx: client.set_query_data(["post"], ctx))
        seen.append(m)
        return Text("%s/%s" % (q.data, m.status))

    async def act(r):
        with pytest.raises(ValueError):
            await seen[-1].mutate_async("temp")
        await asyncio.sleep(0.01)
        assert r.texts() == ["kept/error"] and str(seen[-1].error) == "rejected"
        seen[-1].reset()
        await asyncio.sleep(0.01)

    r = run(Screen, client, 0.02, act)
    assert r.texts() == ["kept/idle"]


def test_mutation_retry():
    tries = []
    seen = []

    @component
    def Screen():
        def flaky(_v):
            tries.append(1)
            if len(tries) < 3:
                raise OSError("net")
            return "done"

        m = use_mutation(flaky, retry=2, retry_delay=0)
        seen.append(m)
        return Text(m.status)

    async def act(r):
        assert await seen[-1].mutate_async() == "done"
        await asyncio.sleep(0.01)

    r = run(Screen, QueryClient(), 0.01, act)
    assert r.texts() == ["success"] and len(tries) == 3
