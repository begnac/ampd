# ampd - Asynchronous MPD client library

Communicate with a Music Player Daemon server using asyncio.

Connection is established by a client object.
Requests (MPD commands) are generated by executor objects, and are awaited for in asyncio coroutines.
Each client has a root executor, and sub-executors can be constructed for grouping related requests.

## Tasks

An AMPD task is an asyncio coroutine decorated by `@ampd.task`.
The decorator ensures that the task is immediately scheduled for execution, and
that cancellation (e.g., on close()ing the executor) is considered normal termination:

```
@ampd.task
async def task_example():
    ...
    reply = await executor.request1(a, b)
    ...
    reply = await executor.request2()
    ...
```

The request can be:

a. An [MPD command](https://www.musicpd.org/doc/html/protocol.html#command-reference) (other than `idle` or `noidle`).
   Returns when the server's reply arrives:

```
    await executor.play(5)
    reply = await executor.status()
```

b. Idle request:

```
    reply = await executor.idle(event_mask, timeout=None)
```

This emulates MPD's `idle` command, with some improvements.
The timeout is given in seconds.
Possible event flags are:

- `Event.<SUBSYSTEM>` (in uppercase) or `ANY` to match any subsystem.
- `Event.CONNECT` - client is connected to server.
- `Event.IDLE` - client is idle.

Returns the mask of events which actually occurred, or ``Event.TIMEOUT`` if timeout occurred.

c. Command list: `executor.command_list(iterable)`.
