export type {
  Action,
  ActionType,
  Message,
  SyncOptions,
  SyncResult,
  Thread,
  ThreadClient,
} from "./core";
export { EditRateLimited, OrphanedRepliesError, sync } from "./core";

export type {
  SlackClientOptions,
  SlackDeleteOptions,
  SlackPostOptions,
  SlackSyncOptions,
} from "./slack";
export { SLACK_MESSAGE_LIMIT, SlackClient } from "./slack";

export type {
  BulletFn,
  LinkedSyncResult,
  LinkedThread,
  Section,
} from "./linked";
export {
  buildDetailMessages,
  buildSummaryMessages,
  defaultBullet,
  splitBody,
} from "./linked";
