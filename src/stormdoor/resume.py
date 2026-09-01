"""Letting a dropped stream be picked up where it left off.

Every SSE frame this gateway sends already carries an ``id:``. That id was put
there in week one as the anchor for exactly this: a client whose connection dies
mid-answer reconnects, says which id it last saw, and gets the rest rather than
the whole thing again or nothing at all.

To make that possible the frames of a live stream are kept in memory as they go
out, keyed by the request id the caller already has in a response header. A
reconnect to ``GET /v1/stream/{request_id}`` with a ``Last-Event-ID`` replays the
frames after that id. If the original stream finished, the client gets the tail
it missed. If it is still in flight, the client gets what has been produced so
far; it can reconnect again for more.

Three honest bounds, because an unbounded in-memory buffer is a memory leak with
a nice name:

* **Per stream, a frame cap.** Past it the oldest frames are dropped and the
  buffer records how far it was forced to forget. A resume asking for an id below
  that line is told to restart rather than handed a stream with a hole in it.
* **Across streams, a count cap.** The oldest whole stream is evicted first.
* **A TTL.** A buffer nobody came back for is swept. The clock is monotonic
  because this state never leaves the process, so a restart simply empties it,
  which is the correct behaviour: after a restart there is nothing to resume.

None of this is shared across replicas. Behind more than one gateway a client
must reconnect to the instance that served it, which the README states.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field


@dataclass(slots=True)
class BufferedStream:
    key_id: str
    start_id: int = 0                       # event id of frames[0]
    frames: list[str] = field(default_factory=list)
    done: bool = False
    forgot_before: int = 0                  # highest id dropped by the frame cap, +1
    created_at: float = 0.0
    last_at: float = 0.0

    @property
    def next_id(self) -> int:
        return self.start_id + len(self.frames)


@dataclass(frozen=True, slots=True)
class Replay:
    frames: list[str]
    done: bool
    # True when the requested resume point is older than the buffer still holds,
    # so the caller cannot be given a gap-free continuation and should restart.
    too_far_behind: bool


class StreamBuffer:
    """In-memory, bounded, per-request frame store. Not shared across replicas."""

    def __init__(self, *, max_streams: int = 256, max_frames: int = 512,
                 ttl_s: float = 300.0):
        self._streams: OrderedDict[str, BufferedStream] = OrderedDict()
        self._max_streams = max_streams
        self._max_frames = max_frames
        self._ttl_s = ttl_s

    def _now(self) -> float:
        return time.monotonic()

    def open(self, request_id: str, key_id: str) -> None:
        self._sweep()
        now = self._now()
        self._streams[request_id] = BufferedStream(key_id=key_id, created_at=now, last_at=now)
        self._streams.move_to_end(request_id)
        while len(self._streams) > self._max_streams:
            self._streams.popitem(last=False)  # evict the oldest whole stream

    def append(self, request_id: str, frame: str) -> None:
        s = self._streams.get(request_id)
        if s is None:
            return
        s.frames.append(frame)
        s.last_at = self._now()
        # Enforce the per-stream cap by forgetting the oldest frames. start_id and
        # forgot_before move together so a later resume knows the buffer can no
        # longer offer a gap-free continuation from before this point.
        overflow = len(s.frames) - self._max_frames
        if overflow > 0:
            del s.frames[:overflow]
            s.start_id += overflow
            s.forgot_before = s.start_id

    def mark_done(self, request_id: str) -> None:
        s = self._streams.get(request_id)
        if s is not None:
            s.done = True
            s.last_at = self._now()

    def replay(self, request_id: str, *, after_id: int, key_id: str) -> Replay | None:
        """Frames after ``after_id`` for a stream owned by ``key_id``.

        Returns None if the request id is unknown (expired or never existed) or is
        owned by a different key, which are indistinguishable to the caller on
        purpose: one key must not be able to probe for another's stream ids.
        """
        s = self._streams.get(request_id)
        if s is None or s.key_id != key_id:
            return None
        self._streams.move_to_end(request_id)
        # after_id is the last id the client saw. It wants everything strictly
        # after it. If that lands before what the buffer still holds, there is a
        # gap it cannot fill.
        if after_id + 1 < s.forgot_before:
            return Replay(frames=[], done=s.done, too_far_behind=True)
        first = max(after_id + 1, s.start_id)
        offset = first - s.start_id
        return Replay(frames=s.frames[offset:], done=s.done, too_far_behind=False)

    def _sweep(self) -> None:
        cutoff = self._now() - self._ttl_s
        stale = [rid for rid, s in self._streams.items() if s.last_at < cutoff]
        for rid in stale:
            del self._streams[rid]

    def size(self) -> int:
        return len(self._streams)
