"""Host terminal: shell, folder and environment
logic, and the two routes in ``routes.py``.

- ``shell``: which shell, its argv, the allowlisted environment, the spawn through Hermes's
  ``PtyBridge`` and the resize-frame pattern.
- ``folder``: the chat's folder (``sessions.cwd`` in the profile's ``state.db``, read-only) or
  Hermes's default working folder.
- ``service``: the keep-alive registry (Hermes's ``PtySessionRegistry``), the login gate lookup
  and the two route handlers.
"""

from __future__ import annotations
