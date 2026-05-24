import {
  OrphanedRepliesError,
  sync,
  type Message,
  type SyncOptions,
  type SyncResult,
  type Thread,
  type ThreadClient,
} from "./core";

export const SLACK_MESSAGE_LIMIT = 4000;

export interface SlackClientOptions {
  token: string;
  channel: string;
  username?: string;
  iconEmoji?: string;
  fetch?: typeof fetch;
}

export interface SlackPostOptions {
  threadId?: string;
}

export interface SlackDeleteOptions {
  orphansOk?: boolean;
}

export interface SlackSyncOptions extends SyncOptions {
  threadTs?: string;
  suppressUnfurls?: boolean;
}

interface SlackResponse {
  ok: boolean;
  error?: string;
  [key: string]: unknown;
}

const sleep = (ms: number): Promise<void> =>
  new Promise((resolve) => setTimeout(resolve, ms));

export class SlackClient implements ThreadClient {
  readonly token: string;
  readonly channel: string;
  readonly username?: string;
  readonly iconEmoji?: string;

  private readonly fetchImpl: typeof fetch;
  private _botIds: { userId: string; botId?: string } | null = null;
  private suppressUnfurls = true;

  constructor(options: SlackClientOptions) {
    this.token = options.token;
    this.channel = options.channel;
    this.username = options.username;
    this.iconEmoji = options.iconEmoji;
    this.fetchImpl = options.fetch ?? globalThis.fetch;
  }

  protected async request(
    endpoint: string,
    data?: Record<string, unknown>,
    method: "GET" | "POST" = "POST",
  ): Promise<SlackResponse> {
    const maxRetries = 3;
    let attempt = 0;
    while (true) {
      let url = `https://slack.com/api/${endpoint}`;
      const headers: Record<string, string> = {
        Authorization: `Bearer ${this.token}`,
      };
      let body: string | undefined;
      if (method === "GET" && data) {
        const params = new URLSearchParams();
        for (const [k, v] of Object.entries(data)) {
          if (v !== undefined && v !== null) params.set(k, String(v));
        }
        url = `${url}?${params.toString()}`;
      } else if (data) {
        headers["Content-Type"] = "application/json; charset=utf-8";
        body = JSON.stringify(data);
      }

      const response = await this.fetchImpl(url, { method, headers, body });
      if (response.status === 429 && attempt < maxRetries) {
        const retryAfter = parseInt(
          response.headers.get("Retry-After") ?? "1",
          10,
        );
        await sleep(retryAfter * 1000);
        attempt++;
        continue;
      }
      const text = await response.text();
      if (!response.ok) {
        throw new Error(`Slack API error: ${response.status} ${text}`);
      }
      const result = JSON.parse(text) as SlackResponse;
      if (!result.ok) {
        throw new Error(`Slack API error: ${result.error ?? text}`);
      }
      return result;
    }
  }

  /**
   * Lazily resolve the authenticated bot's `user_id` and `bot_id`.
   *
   * Slack returns `user: null` on `bot_message` events — the bot's identity
   * lives in `bot_id`. Matching only `user_id` mis-labels our own posts as
   * non-editable, which makes `sync()` re-post the whole thread every run.
   */
  async botIds(): Promise<{ userId: string; botId?: string }> {
    if (this._botIds === null) {
      const result = await this.request("auth.test", undefined, "POST");
      this._botIds = {
        userId: result["user_id"] as string,
        botId: (result["bot_id"] as string | undefined) ?? undefined,
      };
    }
    return this._botIds;
  }

  async list(threadId: string): Promise<Message[]> {
    const result = await this.request(
      "conversations.replies",
      { channel: this.channel, ts: threadId },
      "GET",
    );
    const { userId, botId } = await this.botIds();
    const raw = (result["messages"] ?? []) as Array<{
      ts: string;
      text?: string;
      user?: string | null;
      bot_id?: string;
    }>;
    return raw.map((m) => ({
      id: m.ts,
      content: m.text ?? "",
      editable:
        m.user === userId || (botId !== undefined && m.bot_id === botId),
    }));
  }

  async post(content: string, threadId?: string): Promise<Message> {
    const data: Record<string, unknown> = {
      channel: this.channel,
      text: content,
      unfurl_links: !this.suppressUnfurls,
      unfurl_media: !this.suppressUnfurls,
    };
    if (this.username !== undefined) data["username"] = this.username;
    if (this.iconEmoji !== undefined) data["icon_emoji"] = this.iconEmoji;
    if (threadId !== undefined) data["thread_ts"] = threadId;
    const result = await this.request("chat.postMessage", data);
    return { id: result["ts"] as string, content };
  }

  async edit(messageId: string, content: string): Promise<Message> {
    await this.request("chat.update", {
      channel: this.channel,
      ts: messageId,
      text: content,
      unfurl_links: !this.suppressUnfurls,
      unfurl_media: !this.suppressUnfurls,
    });
    return { id: messageId, content };
  }

  async delete(
    messageId: string,
    options: SlackDeleteOptions = {},
  ): Promise<void> {
    if (!options.orphansOk) {
      const result = await this.request(
        "conversations.replies",
        { channel: this.channel, ts: messageId },
        "GET",
      );
      const replies = (result["messages"] ?? []) as unknown[];
      if (replies.length > 1) {
        throw new OrphanedRepliesError(messageId, replies.length - 1);
      }
    }
    await this.request("chat.delete", {
      channel: this.channel,
      ts: messageId,
    });
  }

  async permalink(messageTs: string): Promise<string> {
    const result = await this.request(
      "chat.getPermalink",
      { channel: this.channel, message_ts: messageTs },
      "GET",
    );
    return result["permalink"] as string;
  }

  async sync(
    thread: Thread,
    options: SlackSyncOptions = {},
  ): Promise<SyncResult> {
    const { threadTs, suppressUnfurls = true, ...rest } = options;
    const prior = this.suppressUnfurls;
    this.suppressUnfurls = suppressUnfurls;
    try {
      return await sync(this, thread, threadTs, rest);
    } finally {
      this.suppressUnfurls = prior;
    }
  }
}
