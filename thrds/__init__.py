from .core import Action, ActionType, EditRateLimited, Message, OrphanedRepliesError, SyncOptions, SyncResult, Thread, sync
from .discord import DiscordClient
from .doc import Doc, DocMessage, DocSyncResult, DocThread, Frontmatter
from .linked import LinkedSyncResult, LinkedThread, Section
from .protocol import ThreadClient
from .slack import RecoveredSession, SlackClient
from .state import SessionState

__all__ = [
    "Action",
    "ActionType",
    "DiscordClient",
    "Doc",
    "DocMessage",
    "DocSyncResult",
    "DocThread",
    "EditRateLimited",
    "Frontmatter",
    "LinkedSyncResult",
    "LinkedThread",
    "Message",
    "OrphanedRepliesError",
    "RecoveredSession",
    "Section",
    "SessionState",
    "SlackClient",
    "SyncOptions",
    "SyncResult",
    "Thread",
    "ThreadClient",
    "sync",
]

# BskyClient requires atproto; import lazily
try:
    from .bsky import BskyClient
    __all__.append("BskyClient")
except ImportError:
    pass
