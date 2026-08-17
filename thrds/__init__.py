from .core import Action, ActionType, EditRateLimited, Message, Msg, OrphanedRepliesError, SenderChangeForbidden, SenderChangePolicy, SyncOptions, SyncResult, Thread, sync
from .discord import DiscordClient
from .doc import Doc, DocMessage, DocSyncResult, DocThread, Frontmatter
from .linked import LinkedSyncResult, LinkedThread, Section
from .lint import BskyLinter, DiscordLinter, LintIssue, LintReport
from .protocol import ThreadClient
from .slack import RecoveredSession, ScanCapReached, SlackClient
from .state import SessionState, ThreadEntry, ThreadTarget
from .threadfile import ThreadFile, thread_files, thread_filename

__all__ = [
    "Action",
    "ActionType",
    "BskyLinter",
    "DiscordClient",
    "DiscordLinter",
    "Doc",
    "DocMessage",
    "DocSyncResult",
    "DocThread",
    "EditRateLimited",
    "Frontmatter",
    "LinkedSyncResult",
    "LinkedThread",
    "LintIssue",
    "LintReport",
    "Message",
    "Msg",
    "OrphanedRepliesError",
    "RecoveredSession",
    "ScanCapReached",
    "Section",
    "SenderChangeForbidden",
    "SenderChangePolicy",
    "SessionState",
    "SlackClient",
    "SyncOptions",
    "SyncResult",
    "Thread",
    "ThreadClient",
    "ThreadEntry",
    "ThreadFile",
    "ThreadTarget",
    "sync",
    "thread_filename",
    "thread_files",
]

# BskyClient requires atproto; import lazily
try:
    from .bsky import BskyClient
    __all__.append("BskyClient")
except ImportError:
    pass
