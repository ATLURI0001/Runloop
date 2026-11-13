import asyncio
import pytest

from agent.utils.ttl_cache import cached, clear, invalidate, set_cached


def test_ttl_cache_validation():
    with pytest.raises(ValueError):
        set_cached("k1", "v1", -1)

    with pytest.raises(ValueError):
        set_cached("k1", "v1", float("nan"))

    with pytest.raises(ValueError):
        set_cached("k1", "v1", True)

    async def dummy():
        return "val"

    with pytest.raises(ValueError):
        asyncio.run(cached("k1", -5, dummy))


def test_ttl_cache_set_get_and_clear():
    async def run():
        clear()
        set_cached("key_a", "hello", 60)
        calls = 0

        async def loader():
            nonlocal calls
            calls += 1
            return "loaded"

        res = await cached("key_a", 60, loader)
        assert res == "hello"
        assert calls == 0

        invalidate("key_a")
        res2 = await cached("key_a", 60, loader)
        assert res2 == "loaded"
        assert calls == 1

        clear()

    asyncio.run(run())
