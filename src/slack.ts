import {
  OrphanedRepliesError,
  sync,
  type Message,
  type SyncOptions,
  type SyncResult,
  type Thread,
  type ThreadClient,
} from "./core";
import {
  buildDetailMessages,
  buildSummaryMessages,
  type BulletFn,
  type LinkedSyncResult,
  type LinkedThread,
  type Section,
} from "./linked";

export const SLACK_MESSAGE_LIMIT = 4000;

/** Slack mrkdwn bullet: linked bold title via `<url|*title*>`. */
const slackBullet: BulletFn = (section, url) =>
  `- <${url}|*${section.title}*> — ${section.summary}`;

/** Placeholder URL with safe upper bound on Slack permalink length (~120). */
const DETAIL_URL_PLACEHOLDER = "x".repeat(130);

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
  private skipOp = false;

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
    let messages = raw.map((m) => ({
      id: m.ts,
      content: m.text ?? "",
      editable:
        m.user === userId || (botId !== undefined && m.bot_id === botId),
    }));
    if (this.skipOp && messages.length > 0) {
      messages = messages.slice(1);
    }
    return messages;
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
  async syncLinked(
    linked: LinkedThread,
    options: {
      threadTs?: string;
      dryRun?: boolean;
      pace?: number;
      jitter?: number;
      suppressUnfurls?: boolean;
    } = {},
  ): Promise<LinkedSyncResult> {
    const {
      threadTs: providedThreadTs,
      dryRun = false,
      pace = 0.4,
      jitter = 0,
      suppressUnfurls = true,
    } = options;

    // Summary bullets sit in the reply slots; the prefix goes to the OP.
    const linkedReplies: LinkedThread = {
      summaryPrefix: "",
      sections: linked.sections,
      summarySuffix: linked.summarySuffix,
    };

    const [detailMsgs, sectionStarts] = buildDetailMessages(
      linked.sections,
      SLACK_MESSAGE_LIMIT,
    );
    const placeholderUrls = linked.sections.map(() => DETAIL_URL_PLACEHOLDER);
    const summaryMsgs = buildSummaryMessages(
      linkedReplies,
      placeholderUrls,
      SLACK_MESSAGE_LIMIT,
      slackBullet,
    );
    const nSummary = summaryMsgs.length;
    const allReplyMsgs = [...summaryMsgs, ...detailMsgs];

    let threadTs = providedThreadTs;
    if (threadTs === undefined) {
      if (!dryRun) {
        const opContent = linked.summaryPrefix || " ";
        const op = await this.post(opContent);
        threadTs = op.id;
      } else {
        threadTs = "<new>";
      }
    } else if (linked.summaryPrefix && !dryRun) {
      await this.edit(threadTs, linked.summaryPrefix);
    }

    this.skipOp = true;
    let result: SyncResult;
    try {
      result = await this.sync(
        { messages: allReplyMsgs },
        { threadTs, dryRun, pace, jitter, suppressUnfurls },
      );
    } finally {
      this.skipOp = false;
    }

    if (dryRun) {
      return {
        threadId: threadTs,
        summaryIds: result.messageIds.slice(0, nSummary),
        detailIds: result.messageIds.slice(nSummary),
        sectionDetailIds: {},
      };
    }

    const detailIds = result.messageIds.slice(nSummary);
    const summaryIds = result.messageIds.slice(0, nSummary);

    // Phase 3: resolve real permalinks for the section-start details
    const sectionDetailIds: Record<string, string> = {};
    const realLinks: string[] = [];
    for (let i = 0; i < linked.sections.length; i++) {
      if (i > 0 && pace > 0) {
        await sleep((pace + Math.random() * jitter) * 1000);
      }
      const detailIdx = sectionStarts[i]!;
      const detailMsgId = detailIds[detailIdx]!;
      sectionDetailIds[linked.sections[i]!.title] = detailMsgId;
      realLinks.push(await this.permalink(detailMsgId));
    }

    // Phase 4: rebuild summaries with real URLs and edit
    const finalSummaries = buildSummaryMessages(
      linkedReplies,
      realLinks,
      SLACK_MESSAGE_LIMIT,
      slackBullet,
    );
    for (let i = 0; i < summaryIds.length; i++) {
      if (i > 0 && pace > 0) {
        await sleep((pace + Math.random() * jitter) * 1000);
      }
      await this.edit(summaryIds[i]!, finalSummaries[i]!);
    }

    return {
      threadId: result.threadId,
      summaryIds,
      detailIds,
      sectionDetailIds,
    };
  }
}
