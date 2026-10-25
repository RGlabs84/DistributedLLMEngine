"""
The Coordinator's pooled client for calls into its own llama-server engines.

A client per request cost about 3 ms (spikes near 30 ms) on the production host before a
single byte reached the engine. The pool must be reused within an event loop and never
handed to a different one, where its connections would be unusable.
"""

import asyncio

from engine import coordinator


def test_engine_client_is_reused_within_a_loop_and_replaced_across_loops(monkeypatch):
    monkeypatch.setattr(coordinator, "_engine_http", None)

    async def borrow_twice():
        async with coordinator.engine_client() as first:
            pass
        async with coordinator.engine_client() as second:
            pass
        # Leaving the block hands the client back; it must stay open for the next request.
        assert not first.is_closed
        return first, second

    first, second = asyncio.run(borrow_twice())
    assert first is second

    other, _ = asyncio.run(borrow_twice())
    assert other is not first

    for client in (first, other):
        asyncio.run(client.aclose())


def test_a_closed_client_is_replaced(monkeypatch):
    monkeypatch.setattr(coordinator, "_engine_http", None)

    async def scenario():
        async with coordinator.engine_client() as first:
            pass
        await first.aclose()
        async with coordinator.engine_client() as second:
            pass
        return first, second

    first, second = asyncio.run(scenario())
    assert second is not first
    assert not second.is_closed
    asyncio.run(second.aclose())


def test_idle_connections_expire_before_llama_servers_keep_alive(monkeypatch):
    """llama-server (cpp-httplib) closes idle keep-alive sockets after 5 s."""
    monkeypatch.setattr(coordinator, "_engine_http", None)

    async def scenario():
        async with coordinator.engine_client() as client:
            return client

    client = asyncio.run(scenario())
    assert client._transport._pool._keepalive_expiry < 5.0
    asyncio.run(client.aclose())
