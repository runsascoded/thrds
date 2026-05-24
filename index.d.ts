type ActionType = "SKIP" | "EDIT" | "POST" | "DELETE";
declare class EditRateLimited extends Error {
    constructor(message?: string);
}
declare class OrphanedRepliesError extends Error {
    readonly messageId: string;
    readonly replyCount: number;
    constructor(messageId: string, replyCount: number);
}
interface Action {
    type: ActionType;
    index: number;
    messageId?: string;
    content?: string;
    priorContent?: string;
}
interface Thread {
    messages: string[];
}
interface Message {
    id: string;
    content: string;
    editable?: boolean;
}
interface SyncResult {
    threadId: string;
    messageIds: string[];
    actions: Action[];
}
interface SyncOptions {
    dryRun?: boolean;
    pace?: number;
    jitter?: number;
    suppressEmbeds?: boolean;
    suppressUnfurls?: boolean;
}
interface ThreadClient {
    list(threadId: string): Promise<Message[]>;
    post(content: string, threadId?: string): Promise<Message>;
    edit(messageId: string, content: string): Promise<Message>;
    delete(messageId: string): Promise<void>;
}
declare function sync(client: ThreadClient, desired: Thread, threadId?: string, options?: SyncOptions): Promise<SyncResult>;

interface Section {
    title: string;
    summary: string;
    body: string;
}
interface LinkedThread {
    summaryPrefix: string;
    sections: Section[];
    summarySuffix?: string;
}
interface LinkedSyncResult {
    threadId: string;
    summaryIds: string[];
    detailIds: string[];
    /** section title → first detail message ID */
    sectionDetailIds: Record<string, string>;
}
type BulletFn = (section: Section, url: string) => string;
/** Default bullet format: bold linked title (Markdown). */
declare const defaultBullet: BulletFn;
/** Split a body into messages on paragraph (then line) boundaries. */
declare function splitBody(body: string, limit: number): string[];
/**
 * Build detail messages for each section.
 *
 * Returns `[messages, sectionStarts]` where `sectionStarts[i]` is the
 * index (0-based within details) where section `i` begins.
 */
declare function buildDetailMessages(sections: Section[], limit: number): [string[], Record<number, number>];
/**
 * Build summary messages with section bullets and links.
 *
 * Greedy-packs bullets into messages respecting the char limit.
 * `sectionUrls[i]` is the link URL for section `i` (placeholder or real).
 */
declare function buildSummaryMessages(linked: LinkedThread, sectionUrls: string[], limit: number, bulletFn?: BulletFn): string[];

declare const SLACK_MESSAGE_LIMIT = 4000;
interface SlackClientOptions {
    token: string;
    channel: string;
    username?: string;
    iconEmoji?: string;
    fetch?: typeof fetch;
}
interface SlackPostOptions {
    threadId?: string;
}
interface SlackDeleteOptions {
    orphansOk?: boolean;
}
interface SlackSyncOptions extends SyncOptions {
    threadTs?: string;
    suppressUnfurls?: boolean;
}
interface SlackResponse {
    ok: boolean;
    error?: string;
    [key: string]: unknown;
}
declare class SlackClient implements ThreadClient {
    readonly token: string;
    readonly channel: string;
    readonly username?: string;
    readonly iconEmoji?: string;
    private readonly fetchImpl;
    private _botIds;
    private suppressUnfurls;
    private skipOp;
    constructor(options: SlackClientOptions);
    protected request(endpoint: string, data?: Record<string, unknown>, method?: "GET" | "POST"): Promise<SlackResponse>;
    /**
     * Lazily resolve the authenticated bot's `user_id` and `bot_id`.
     *
     * Slack returns `user: null` on `bot_message` events — the bot's identity
     * lives in `bot_id`. Matching only `user_id` mis-labels our own posts as
     * non-editable, which makes `sync()` re-post the whole thread every run.
     */
    botIds(): Promise<{
        userId: string;
        botId?: string;
    }>;
    list(threadId: string): Promise<Message[]>;
    post(content: string, threadId?: string): Promise<Message>;
    edit(messageId: string, content: string): Promise<Message>;
    delete(messageId: string, options?: SlackDeleteOptions): Promise<void>;
    permalink(messageTs: string): Promise<string>;
    sync(thread: Thread, options?: SlackSyncOptions): Promise<SyncResult>;
    /**
     * Sync a linked summary thread.
     *
     * In Slack the thread parent (OP) is the first message in
     * `conversations.replies`. This method manages the OP separately
     * (setting it to `summaryPrefix`) and syncs bullets + details as thread
     * replies. Two phases: post all messages with placeholder URLs first
     * (to obtain real message ids), then edit summaries with real
     * permalinks.
     */
    syncLinked(linked: LinkedThread, options?: {
        threadTs?: string;
        dryRun?: boolean;
        pace?: number;
        jitter?: number;
        suppressUnfurls?: boolean;
    }): Promise<LinkedSyncResult>;
}

export { type Action, type ActionType, type BulletFn, EditRateLimited, type LinkedSyncResult, type LinkedThread, type Message, OrphanedRepliesError, SLACK_MESSAGE_LIMIT, type Section, SlackClient, type SlackClientOptions, type SlackDeleteOptions, type SlackPostOptions, type SlackSyncOptions, type SyncOptions, type SyncResult, type Thread, type ThreadClient, buildDetailMessages, buildSummaryMessages, defaultBullet, splitBody, sync };
