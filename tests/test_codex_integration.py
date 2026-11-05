"""可选真实协议验收：CODEX_TEST_BINARY=/path/to/codex pytest -q tests/test_codex_integration.py。"""

import asyncio
import json
import os
import sqlite3

import pytest

from rename.adapters.codex import CodexAdapter


@pytest.mark.skipif(not os.environ.get("CODEX_TEST_BINARY"), reason="需指定真实 Codex 二进制")
def test_app_server_reads_persisted_name(tmp_path, monkeypatch):
    # 使用独立数据目录，不继承用户配置、登录凭据或真实聊天；不发起模型请求。
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))

    async def run():
        async def start():
            return await asyncio.create_subprocess_exec(
                os.environ["CODEX_TEST_BINARY"], "app-server", "--stdio",
                cwd=tmp_path, env=os.environ.copy(),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )

        async def request(proc, request_id, method, params):
            proc.stdin.write((json.dumps({
                "id": request_id, "method": method, "params": params,
            }) + "\n").encode())
            await proc.stdin.drain()
            # 通知可穿插在响应之间，等待整个请求的上限由外层控制。
            while True:
                line = await proc.stdout.readline()
                assert line, "Codex 在响应前退出"
                response = json.loads(line)
                if response.get("id") == request_id:
                    assert "error" not in response, response.get("error")
                    return response["result"]

        async def call(proc, request_id, method, params):
            return await asyncio.wait_for(request(proc, request_id, method, params), 20)

        async def initialize(proc):
            await call(proc, 1, "initialize", {
                "clientInfo": {"name": "rename_test", "version": "1.0.0"},
                "capabilities": {"experimentalApi": True},
            })

        async def stop(proc):
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 5)
                except TimeoutError:
                    # 即使验收失败，也必须回收隔离服务，避免测试残留后台进程。
                    proc.kill()
                    await proc.wait()

        proc = await start()
        try:
            await initialize(proc)
            result = await call(proc, 2, "thread/start", {
                "cwd": str(tmp_path), "modelProvider": "openai",
                "persistExtendedHistory": True,
            })
            tid = result["thread"]["id"]
            await call(proc, 3, "thread/name/set", {"threadId": tid, "name": "Original name"})
            adapter = CodexAdapter()
            session = next(s for s in adapter.discover(0) if s.id == tid)
            # 先复现旧实现的失败：SQLite 写入成功，官方读取仍返回旧名称。
            with sqlite3.connect(session.meta["db"]) as con:
                con.execute("UPDATE threads SET title=? WHERE id=?", ("Legacy write", tid))
            result = await call(proc, 4, "thread/read", {"threadId": tid})
            assert result["thread"]["name"] == "Original name"
            adapter.set_title(session, "修复后的显示名称")
            result = await call(proc, 5, "thread/read", {"threadId": tid})
            assert result["thread"]["name"] == "修复后的显示名称"
        finally:
            await stop(proc)

        proc = await start()
        try:
            await initialize(proc)
            result = await call(proc, 2, "thread/read", {"threadId": tid})
            assert result["thread"]["name"] == "修复后的显示名称"
        finally:
            await stop(proc)

    asyncio.run(run())
