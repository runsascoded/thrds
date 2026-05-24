// src/core.ts
var EditRateLimited = class extends Error {
  constructor(message) {
    super(message ?? "edit rate limited");
    this.name = "EditRateLimited";
  }
};
var OrphanedRepliesError = class extends Error {
  constructor(messageId, replyCount) {
    super(
      `Refusing to delete message ${messageId}: it has ${replyCount} thread replies that would be orphaned. Pass orphansOk=true to delete anyway.`
    );
    this.messageId = messageId;
    this.replyCount = replyCount;
    this.name = "OrphanedRepliesError";
  }
  messageId;
  replyCount;
};
var sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function sync(client, desired, threadId, options = {}) {
  const { dryRun = false, pace = 0, jitter = 0 } = options;
  const actions = [];
  const messageIds = [];
  let mutated = false;
  const doPace = async () => {
    if (mutated && pace > 0) {
      const delay = pace + Math.random() * jitter;
      await sleep(delay * 1e3);
    }
    mutated = true;
  };
  const allExisting = threadId !== void 0 ? await client.list(threadId) : [];
  const existing = allExisting.filter((m) => m.editable !== false);
  const M = desired.messages.length;
  const N = existing.length;
  if (M < N) {
    for (let i = N - 1; i >= M; i--) {
      const msg = existing[i];
      actions.push({
        type: "DELETE",
        index: i,
        messageId: msg.id,
        priorContent: msg.content
      });
      if (!dryRun) {
        await doPace();
        await client.delete(msg.id);
      }
    }
  }
  const overlap = Math.min(M, N);
  let repostFrom = null;
  for (let i = 0; i < overlap; i++) {
    const ex = existing[i];
    const want = desired.messages[i];
    if (ex.content === want) {
      actions.push({
        type: "SKIP",
        index: i,
        messageId: ex.id,
        content: want
      });
      messageIds.push(ex.id);
      continue;
    }
    actions.push({
      type: "EDIT",
      index: i,
      messageId: ex.id,
      content: want,
      priorContent: ex.content
    });
    if (dryRun) {
      messageIds.push(ex.id);
      continue;
    }
    try {
      await doPace();
      const result = await client.edit(ex.id, want);
      messageIds.push(result.id);
    } catch (e) {
      if (e instanceof EditRateLimited) {
        repostFrom = i;
        break;
      }
      throw e;
    }
  }
  if (repostFrom !== null) {
    for (let j = overlap - 1; j >= repostFrom; j--) {
      const msg = existing[j];
      actions.push({
        type: "DELETE",
        index: j,
        messageId: msg.id,
        priorContent: msg.content
      });
      await doPace();
      await client.delete(msg.id);
    }
    for (let j = repostFrom; j < M; j++) {
      const want = desired.messages[j];
      actions.push({ type: "POST", index: j, content: want });
      await doPace();
      const result = await client.post(want, threadId);
      messageIds.push(result.id);
    }
    return {
      threadId: threadId ?? "",
      messageIds,
      actions
    };
  }
  let finalThreadId = threadId;
  if (M > N) {
    let start = N;
    if (threadId === void 0 && N === 0) {
      const want = desired.messages[0];
      actions.push({ type: "POST", index: 0, content: want });
      if (dryRun) {
        messageIds.push("<new>");
        finalThreadId = "<new>";
      } else {
        await doPace();
        const result = await client.post(want);
        finalThreadId = result.id;
        messageIds.push(result.id);
      }
      start = 1;
    }
    for (let i = start; i < M; i++) {
      const want = desired.messages[i];
      actions.push({ type: "POST", index: i, content: want });
      if (dryRun) {
        messageIds.push("<new>");
      } else {
        await doPace();
        const result = await client.post(want, finalThreadId);
        messageIds.push(result.id);
      }
    }
  }
  return {
    threadId: finalThreadId ?? "",
    messageIds,
    actions
  };
}

// src/linked.ts
var defaultBullet = (section, url) => `- [**${section.title}**](${url}) \u2014 ${section.summary}`;
function splitBody(body, limit) {
  if (body.length <= limit) return [body];
  const paragraphs = body.split("\n\n");
  const messages = [];
  let current = "";
  for (const para of paragraphs) {
    const candidate = current ? `${current}

${para}` : para;
    if (candidate.length > limit) {
      if (current) messages.push(current);
      if (para.length > limit) {
        const lines = para.split("\n");
        current = "";
        for (const line of lines) {
          const c = current ? `${current}
${line}` : line;
          if (c.length > limit) {
            if (current) messages.push(current);
            current = line;
          } else {
            current = c;
          }
        }
      } else {
        current = para;
      }
    } else {
      current = candidate;
    }
  }
  if (current) messages.push(current);
  return messages;
}
function buildDetailMessages(sections, limit) {
  const messages = [];
  const sectionStarts = {};
  for (let i = 0; i < sections.length; i++) {
    sectionStarts[i] = messages.length;
    const parts = splitBody(sections[i].body, limit);
    messages.push(...parts);
  }
  return [messages, sectionStarts];
}
function buildSummaryMessages(linked, sectionUrls, limit, bulletFn = defaultBullet) {
  const bullets = linked.sections.map((s, i) => bulletFn(s, sectionUrls[i]));
  const messages = [];
  let current = linked.summaryPrefix;
  for (const bullet of bullets) {
    const candidate = current ? `${current}
${bullet}` : bullet;
    if (candidate.length > limit) {
      if (current) messages.push(current);
      current = bullet;
    } else {
      current = candidate;
    }
  }
  if (linked.summarySuffix) {
    const candidate = current ? `${current}
${linked.summarySuffix}` : linked.summarySuffix;
    if (candidate.length > limit) {
      if (current) messages.push(current);
      current = linked.summarySuffix;
    } else {
      current = candidate;
    }
  }
  if (current) messages.push(current);
  return messages;
}

// src/slack.ts
var SLACK_MESSAGE_LIMIT = 4e3;
var slackBullet = (section, url) => `- <${url}|*${section.title}*> \u2014 ${section.summary}`;
var DETAIL_URL_PLACEHOLDER = "x".repeat(130);
var sleep2 = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
var SlackClient = class {
  token;
  channel;
  username;
  iconEmoji;
  fetchImpl;
  _botIds = null;
  suppressUnfurls = true;
  skipOp = false;
  constructor(options) {
    this.token = options.token;
    this.channel = options.channel;
    this.username = options.username;
    this.iconEmoji = options.iconEmoji;
    this.fetchImpl = options.fetch ?? globalThis.fetch;
  }
  async request(endpoint, data, method = "POST") {
    const maxRetries = 3;
    let attempt = 0;
    while (true) {
      let url = `https://slack.com/api/${endpoint}`;
      const headers = {
        Authorization: `Bearer ${this.token}`
      };
      let body;
      if (method === "GET" && data) {
        const params = new URLSearchParams();
        for (const [k, v] of Object.entries(data)) {
          if (v !== void 0 && v !== null) params.set(k, String(v));
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
          10
        );
        await sleep2(retryAfter * 1e3);
        attempt++;
        continue;
      }
      const text = await response.text();
      if (!response.ok) {
        throw new Error(`Slack API error: ${response.status} ${text}`);
      }
      const result = JSON.parse(text);
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
  async botIds() {
    if (this._botIds === null) {
      const result = await this.request("auth.test", void 0, "POST");
      this._botIds = {
        userId: result["user_id"],
        botId: result["bot_id"] ?? void 0
      };
    }
    return this._botIds;
  }
  async list(threadId) {
    const result = await this.request(
      "conversations.replies",
      { channel: this.channel, ts: threadId },
      "GET"
    );
    const { userId, botId } = await this.botIds();
    const raw = result["messages"] ?? [];
    let messages = raw.map((m) => ({
      id: m.ts,
      content: m.text ?? "",
      editable: m.user === userId || botId !== void 0 && m.bot_id === botId
    }));
    if (this.skipOp && messages.length > 0) {
      messages = messages.slice(1);
    }
    return messages;
  }
  async post(content, threadId) {
    const data = {
      channel: this.channel,
      text: content,
      unfurl_links: !this.suppressUnfurls,
      unfurl_media: !this.suppressUnfurls
    };
    if (this.username !== void 0) data["username"] = this.username;
    if (this.iconEmoji !== void 0) data["icon_emoji"] = this.iconEmoji;
    if (threadId !== void 0) data["thread_ts"] = threadId;
    const result = await this.request("chat.postMessage", data);
    return { id: result["ts"], content };
  }
  async edit(messageId, content) {
    await this.request("chat.update", {
      channel: this.channel,
      ts: messageId,
      text: content,
      unfurl_links: !this.suppressUnfurls,
      unfurl_media: !this.suppressUnfurls
    });
    return { id: messageId, content };
  }
  async delete(messageId, options = {}) {
    if (!options.orphansOk) {
      const result = await this.request(
        "conversations.replies",
        { channel: this.channel, ts: messageId },
        "GET"
      );
      const replies = result["messages"] ?? [];
      if (replies.length > 1) {
        throw new OrphanedRepliesError(messageId, replies.length - 1);
      }
    }
    await this.request("chat.delete", {
      channel: this.channel,
      ts: messageId
    });
  }
  async permalink(messageTs) {
    const result = await this.request(
      "chat.getPermalink",
      { channel: this.channel, message_ts: messageTs },
      "GET"
    );
    return result["permalink"];
  }
  async sync(thread, options = {}) {
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
  async syncLinked(linked, options = {}) {
    const {
      threadTs: providedThreadTs,
      dryRun = false,
      pace = 0.4,
      jitter = 0,
      suppressUnfurls = true
    } = options;
    const linkedReplies = {
      summaryPrefix: "",
      sections: linked.sections,
      summarySuffix: linked.summarySuffix
    };
    const [detailMsgs, sectionStarts] = buildDetailMessages(
      linked.sections,
      SLACK_MESSAGE_LIMIT
    );
    const placeholderUrls = linked.sections.map(() => DETAIL_URL_PLACEHOLDER);
    const summaryMsgs = buildSummaryMessages(
      linkedReplies,
      placeholderUrls,
      SLACK_MESSAGE_LIMIT,
      slackBullet
    );
    const nSummary = summaryMsgs.length;
    const allReplyMsgs = [...summaryMsgs, ...detailMsgs];
    let threadTs = providedThreadTs;
    if (threadTs === void 0) {
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
    let result;
    try {
      result = await this.sync(
        { messages: allReplyMsgs },
        { threadTs, dryRun, pace, jitter, suppressUnfurls }
      );
    } finally {
      this.skipOp = false;
    }
    if (dryRun) {
      return {
        threadId: threadTs,
        summaryIds: result.messageIds.slice(0, nSummary),
        detailIds: result.messageIds.slice(nSummary),
        sectionDetailIds: {}
      };
    }
    const detailIds = result.messageIds.slice(nSummary);
    const summaryIds = result.messageIds.slice(0, nSummary);
    const sectionDetailIds = {};
    const realLinks = [];
    for (let i = 0; i < linked.sections.length; i++) {
      if (i > 0 && pace > 0) {
        await sleep2((pace + Math.random() * jitter) * 1e3);
      }
      const detailIdx = sectionStarts[i];
      const detailMsgId = detailIds[detailIdx];
      sectionDetailIds[linked.sections[i].title] = detailMsgId;
      realLinks.push(await this.permalink(detailMsgId));
    }
    const finalSummaries = buildSummaryMessages(
      linkedReplies,
      realLinks,
      SLACK_MESSAGE_LIMIT,
      slackBullet
    );
    for (let i = 0; i < summaryIds.length; i++) {
      if (i > 0 && pace > 0) {
        await sleep2((pace + Math.random() * jitter) * 1e3);
      }
      await this.edit(summaryIds[i], finalSummaries[i]);
    }
    return {
      threadId: result.threadId,
      summaryIds,
      detailIds,
      sectionDetailIds
    };
  }
};
export {
  EditRateLimited,
  OrphanedRepliesError,
  SLACK_MESSAGE_LIMIT,
  SlackClient,
  buildDetailMessages,
  buildSummaryMessages,
  defaultBullet,
  splitBody,
  sync
};
//# sourceMappingURL=index.js.map