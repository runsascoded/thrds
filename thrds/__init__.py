from .core import Action, ActionType, EditRateLimited, Message, OrphanedRepliesError, SyncOptions, SyncResult, Thread, sync
from .discord import DiscordClient
from .linked import LinkedSyncResult, LinkedThread, Section
from .protocol import ThreadClient
from .slack import SlackClient

__all__ = [
    "Action",
    "ActionType",
    "DiscordClient",
    "EditRateLimited",
    "LinkedSyncResult",
    "LinkedThread",
    "Message",
    "OrphanedRepliesError",
    "Section",
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
