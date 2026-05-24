export type ActionType = "SKIP" | "EDIT" | "POST" | "DELETE";

export class EditRateLimited extends Error {
  constructor(message?: string) {
    super(message ?? "edit rate limited");
    this.name = "EditRateLimited";
  }
}

export class OrphanedRepliesError extends Error {
  constructor(
    public readonly messageId: string,
    public readonly replyCount: number,
  ) {
    super(
      `Refusing to delete message ${messageId}: it has ${replyCount} ` +
        `thread replies that would be orphaned. Pass orphansOk=true to delete anyway.`,
    );
    this.name = "OrphanedRepliesError";
  }
}

export interface Action {
  type: ActionType;
  index: number;
  messageId?: string;
  content?: string;
  priorContent?: string;
}

export interface Thread {
  messages: string[];
}

export interface Message {
  id: string;
  content: string;
  editable?: boolean;
}

export interface SyncResult {
  threadId: string;
  messageIds: string[];
  actions: Action[];
}

export interface SyncOptions {
  dryRun?: boolean;
  pace?: number;
  jitter?: number;
  suppressEmbeds?: boolean;
  suppressUnfurls?: boolean;
}

export interface ThreadClient {
  list(threadId: string): Promise<Message[]>;
  post(content: string, threadId?: string): Promise<Message>;
  edit(messageId: string, content: string): Promise<Message>;
  delete(messageId: string): Promise<void>;
}

const sleep = (ms: number): Promise<void> =>
  new Promise((resolve) => setTimeout(resolve, ms));

export async function sync(
  client: ThreadClient,
  desired: Thread,
  threadId?: string,
  options: SyncOptions = {},
): Promise<SyncResult> {
  const { dryRun = false, pace = 0, jitter = 0 } = options;
  const actions: Action[] = [];
  const messageIds: string[] = [];
  let mutated = false;

  const doPace = async (): Promise<void> => {
    if (mutated && pace > 0) {
      const delay = pace + Math.random() * jitter;
      await sleep(delay * 1000);
    }
    mutated = true;
  };

  const allExisting: Message[] =
    threadId !== undefined ? await client.list(threadId) : [];
  const existing = allExisting.filter((m) => m.editable !== false);

  const M = desired.messages.length;
  const N = existing.length;

  // Phase 1: delete editable extras from the end (backwards, OP last)
  if (M < N) {
    for (let i = N - 1; i >= M; i--) {
      const msg = existing[i]!;
      actions.push({
        type: "DELETE",
        index: i,
        messageId: msg.id,
        priorContent: msg.content,
      });
      if (!dryRun) {
        await doPace();
        await client.delete(msg.id);
      }
    }
  }

  // Phase 2: edit overlapping messages (skip unchanged)
  const overlap = Math.min(M, N);
  let repostFrom: number | null = null;
  for (let i = 0; i < overlap; i++) {
    const ex = existing[i]!;
    const want = desired.messages[i]!;
    if (ex.content === want) {
      actions.push({
        type: "SKIP",
        index: i,
        messageId: ex.id,
        content: want,
      });
      messageIds.push(ex.id);
      continue;
    }
    actions.push({
      type: "EDIT",
      index: i,
      messageId: ex.id,
      content: want,
      priorContent: ex.content,
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

  // Phase 2b: delete+repost fallback when an edit rate-limited
  if (repostFrom !== null) {
    for (let j = overlap - 1; j >= repostFrom; j--) {
      const msg = existing[j]!;
      actions.push({
        type: "DELETE",
        index: j,
        messageId: msg.id,
        priorContent: msg.content,
      });
      await doPace();
      await client.delete(msg.id);
    }
    for (let j = repostFrom; j < M; j++) {
      const want = desired.messages[j]!;
      actions.push({ type: "POST", index: j, content: want });
      await doPace();
      const result = await client.post(want, threadId);
      messageIds.push(result.id);
    }
    return {
      threadId: threadId ?? "",
      messageIds,
      actions,
    };
  }

  // Phase 3: post new messages at the end (creating thread if needed)
  let finalThreadId = threadId;
  if (M > N) {
    let start = N;
    if (threadId === undefined && N === 0) {
      const want = desired.messages[0]!;
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
      const want = desired.messages[i]!;
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
    actions,
  };
}
