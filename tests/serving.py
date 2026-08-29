"""Run a server on an ephemeral port for the length of a test.

One helper, used for our lane and for the third-party stub, so the two are
reached exactly the same way: over a socket, by URL, with no in-process
shortcut for either. A test that read ours by calling `LaneService` directly
and the stub over HTTP would be comparing two different things.
"""

from __future__ import annotations

import contextlib
import threading


@contextlib.contextmanager
def serving(server):
    """Yield the base URL of `server`, running in a thread, then shut it down."""
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
