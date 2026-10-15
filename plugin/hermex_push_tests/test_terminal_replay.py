"""Reattach history is framed and ordered before live output, against Hermes's real registry/PTY."""
import asyncio
import os
import json
import time

import pytest

from hermex_push import terminal
from hermex_push.terminal import REPLAY_END, REPLAY_START


class RecordingSocket:
    """The client's socket, recording frames in send order. It can hold the first byte send
    (the history) open, which is when the drain task's live output would overtake it."""

    def __init__(self, hold_history=False):
        self.frames = []
        self.hold = asyncio.Event() if hold_history else None
        self.holding = asyncio.Event()

    async def send_text(self, text):
        self.frames.append(("text", text))

    async def send_bytes(self, data):
        if self.hold is not None and not self.holding.is_set():
            self.holding.set()
            await self.hold.wait()
        self.frames.append(("bytes", data))

    async def close(self, code=1000):
        self.frames.append(("close", code))

    def output(self):
        return b"".join(value for kind, value in self.frames if kind == "bytes")


async def cat_session(tmp_path):
    """A real keep-alive PTY running cat, which prints nothing until it is sent input."""
    from hermes_cli.pty_bridge import PtyBridge
    from hermes_cli.pty_session import PtySessionRegistry
    registry = PtySessionRegistry(ttl=60, max_sessions=1, buffer_cap=4096, read_timeout=0.05)
    session, _ = await registry.attach_or_spawn(
        "chat", spawn=lambda: PtyBridge.spawn(["/bin/cat"], cwd=str(tmp_path), env={"PATH": os.defpath}))
    session.detach(None)
    session.buffer = terminal.OutputHistory(session.buffer)
    return registry, session


async def until(condition, what):
    # Real PTY output arrives on the session's drain thread; bound the wait instead of hanging.
    deadline = time.monotonic() + 10
    while not condition():
        assert time.monotonic() < deadline, f"Timed out waiting for {what}"
        await asyncio.sleep(0.01)


@pytest.fixture
def pty():
    pytest.importorskip("hermes_cli.pty_session")
    pytest.importorskip("hermes_cli.pty_bridge")


def test_an_attach_without_history_still_sends_both_frames(pty, tmp_path):
    async def scenario():
        registry, session = await cat_session(tmp_path)
        try:
            ws = RecordingSocket()
            assert session.buffer.snapshot() == b""
            assert await terminal.Viewer(ws).attach(session)
            return ws.frames
        finally:
            await registry.close_all()

    assert asyncio.run(scenario()) == [("text", REPLAY_START), ("text", REPLAY_END)]


def attach_while_live_output_arrives(tmp_path, framed):
    async def scenario():
        registry, session = await cat_session(tmp_path)
        try:
            await session.bridge.write(b"history\n")
            await until(lambda: session.buffer.snapshot().count(b"history") == 2, "the echoed history")
            ws = RecordingSocket(hold_history=True)
            viewer = terminal.Viewer(ws) if framed else ws
            attach = asyncio.create_task(viewer.attach(session) if framed else session.attach(ws))
            await ws.holding.wait()  # The history send is in flight.
            await session.bridge.write(b"live\n")
            # Once the drain task buffered it, it has already handed the chunk to the socket.
            await until(lambda: b"live" in session.buffer.snapshot(), "live output")
            ws.hold.set()
            assert await attach
            await until(lambda: ws.output().count(b"live") == 2, "live output on the socket")
            return ws.frames
        finally:
            await registry.close_all()

    return asyncio.run(scenario())


@pytest.mark.xfail(strict=False, reason="Upstream may serialize replay in a later release")
def test_without_the_viewer_live_output_overtakes_the_history(pty, tmp_path):
    """Proves the race the framing exists for is real at the tested Hermes pin."""
    frames = attach_while_live_output_arrives(tmp_path, framed=False)
    first_live = next(i for i, (_, value) in enumerate(frames) if b"live" in value)
    history = next(i for i, (_, value) in enumerate(frames) if b"history" in value)
    assert first_live < history


def test_live_output_during_attach_follows_the_replay_end(pty, tmp_path):
    frames = attach_while_live_output_arrives(tmp_path, framed=True)
    assert json.loads(frames[0][1])["unseen"] > 0
    end = frames.index(("text", REPLAY_END))
    history = b"".join(value for kind, value in frames[1:end])
    live = b"".join(value for kind, value in frames[end + 1:])
    assert all(kind == "bytes" for kind, _ in frames[1:end] + frames[end + 1:])
    assert history.count(b"history") == 2 and b"live" not in history
    assert live.count(b"live") == 2 and b"history" not in live


def test_replay_identifies_only_output_no_viewer_received(pty, tmp_path):
    async def scenario():
        registry, session = await cat_session(tmp_path)
        try:
            await session.bridge.write(b"startup\n")
            await until(lambda: b"startup" in session.buffer.snapshot(), "startup output")
            first = RecordingSocket()
            viewer = terminal.Viewer(first)
            assert await viewer.attach(session)
            assert json.loads(first.frames[0][1])["unseen"] == len(first.output())
            session.detach(viewer)
            await session.bridge.write(b"detached\n")
            await until(lambda: b"detached" in session.buffer.snapshot(), "detached output")
            second = RecordingSocket()
            assert await terminal.Viewer(second).attach(session)
            unseen = json.loads(second.frames[0][1])["unseen"]
            assert second.output()[-unseen:].count(b"detached") >= 1
            assert b"startup" not in second.output()[-unseen:]
            third = RecordingSocket()
            assert await terminal.Viewer(third).attach(session)
            assert json.loads(third.frames[0][1])["unseen"] == 0
        finally:
            await registry.close_all()
    asyncio.run(scenario())


def test_close_does_not_wait_for_a_stalled_history_send(pty, tmp_path):
    async def scenario():
        registry, session = await cat_session(tmp_path)
        try:
            session.buffer.append(b"history")
            old = RecordingSocket(hold_history=True)
            viewer = terminal.Viewer(old)
            attach = asyncio.create_task(viewer.attach(session))
            await asyncio.wait_for(old.holding.wait(), 1)
            await asyncio.wait_for(viewer.close(4409), 1)
            assert ("close", 4409) in old.frames
            old.hold.set()
            await attach
        finally:
            await registry.close_all()
    asyncio.run(scenario())


def test_replay_deadline_is_per_send_not_whole_history(pty, tmp_path, monkeypatch):
    clock = 0

    class SlowSocket(RecordingSocket):
        async def send_text(self, text):
            nonlocal clock
            clock += 0.75
            await super().send_text(text)

        async def send_bytes(self, data):
            nonlocal clock
            clock += 0.75
            await super().send_bytes(data)

    async def deadline(operation, timeout):
        started = clock
        result = await operation
        if clock - started > timeout:
            raise asyncio.TimeoutError
        return result

    async def scenario():
        registry, session = await cat_session(tmp_path)
        try:
            session.buffer.append(b"history")
            socket = SlowSocket()
            assert await terminal.Viewer(socket).attach(session)
            assert socket.output() == b"history"
            assert clock > terminal.SEND_TIMEOUT_S
        finally:
            await registry.close_all()

    monkeypatch.setattr(terminal, "SEND_TIMEOUT_S", 1)
    monkeypatch.setattr(asyncio, "wait_for", deadline)
    asyncio.run(scenario())


@pytest.mark.parametrize("error", [ConnectionError("send failed"), asyncio.TimeoutError()])
def test_live_send_failure_closes_the_viewer(pty, tmp_path, error):
    class BrokenSocket(RecordingSocket):
        async def send_bytes(self, data):
            raise error

    async def scenario():
        registry, session = await cat_session(tmp_path)
        try:
            socket = BrokenSocket()
            assert await terminal.Viewer(socket).attach(session)
            await session.bridge.write(b"live" + bytes([10]))
            await until(lambda: session._ws is None, "failed viewer detach")
            assert ("close", 1011) in socket.frames
        finally:
            await registry.close_all()
    asyncio.run(scenario())
