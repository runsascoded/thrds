import { describe, it, expect } from "vitest";
import { SlackClient } from "../src/slack";
import type { LinkedThread } from "../src/linked";

interface MockState {
  authResponse: { user_id: string; bot_id?: string };
  // For conversations.replies: per-thread message lists, in posting order.
  // The OP is index 0 in each list.
  thread: { ts: string; messages: Array<{ ts: string; text: string }> } | null;
  postedMessages: Array<{ content: string; threadTs?: string; ts: string }>;
  edits: Array<{ ts: string; text: string }>;
  permalinks: Record<string, string>;
  tsCounter: number;
}

function makeClient(initialThreadTs?: string): {
  client: SlackClient;
  state: MockState;
} {
  const state: MockState = {
    authResponse: { user_id: "U_BOT", bot_id: "B_BOT" },
    thread:
      initialThreadTs !== undefined
        ? {
            ts: initialThreadTs,
            messages: [{ ts: initialThreadTs, text: "" }],
          }
        : null,
    postedMessages: [],
    edits: [],
    permalinks: {},
    tsCounter: 1000,
  };

  const nextTs = (): string => {
    state.tsCounter += 1;
    return `${state.tsCounter}.0`;
  };

  const fetchImpl: typeof fetch = async (input, init = {} as any) => {
    const url = String(input);
    const method = (init.method ?? "GET") as string;
    const endpoint = url
      .split("?")[0]!
      .replace(/^https:\/\/slack\.com\/api\//, "");
    const body: any =
      typeof init.body === "string" ? JSON.parse(init.body) : null;

    if (endpoint === "auth.test") {
      return new Response(JSON.stringify({ ok: true, ...state.authResponse }));
    }

    if (endpoint === "conversations.replies") {
      // URL params for GET — extract `ts` from query string
      const qs = new URLSearchParams(url.split("?")[1] ?? "");
      const ts = qs.get("ts");
      if (state.thread && state.thread.ts === ts) {
        // Mark messages with bot_id so editable detection sees them as ours
        const msgs = state.thread.messages.map((m) => ({
          ...m,
          user: null,
          bot_id: state.authResponse.bot_id,
        }));
        return new Response(JSON.stringify({ ok: true, messages: msgs }));
      }
      return new Response(JSON.stringify({ ok: true, messages: [] }));
    }

    if (endpoint === "chat.postMessage") {
      const ts = nextTs();
      const threadTs: string | undefined = body.thread_ts;
      const text: string = body.text;
      state.postedMessages.push({ content: text, threadTs, ts });
      if (threadTs === undefined) {
        // New thread (this is the OP)
        state.thread = { ts, messages: [{ ts, text }] };
      } else {
        if (state.thread === null || state.thread.ts !== threadTs) {
          throw new Error(`unknown thread ${threadTs}`);
        }
        state.thread.messages.push({ ts, text });
      }
      return new Response(JSON.stringify({ ok: true, ts }));
    }

    if (endpoint === "chat.update") {
      const ts: string = body.ts;
      const text: string = body.text;
      state.edits.push({ ts, text });
      if (state.thread !== null) {
        const i = state.thread.messages.findIndex((m) => m.ts === ts);
        if (i >= 0) state.thread.messages[i] = { ts, text };
      }
      return new Response(JSON.stringify({ ok: true, ts }));
    }

    if (endpoint === "chat.getPermalink") {
      const qs = new URLSearchParams(url.split("?")[1] ?? "");
      const messageTs = qs.get("message_ts")!;
      const permalink = `https://slack.com/archives/C123/p${messageTs.replace(".", "")}`;
      state.permalinks[messageTs] = permalink;
      return new Response(JSON.stringify({ ok: true, permalink }));
    }

    if (endpoint === "chat.delete") {
      return new Response(JSON.stringify({ ok: true }));
    }

    throw new Error(`unexpected endpoint: ${endpoint}`);
  };

  const client = new SlackClient({
    token: "xoxb-fake",
    channel: "C123",
    fetch: fetchImpl,
  });
  return { client, state };
}

describe("SlackClient.syncLinked — new thread", () => {
  it("posts OP, summary, details with placeholders then edits summary with real permalinks", async () => {
    const { client, state } = makeClient();
    const linked: LinkedThread = {
      summaryPrefix: "# Weekly",
      sections: [
        {
          title: "Topic A",
          summary: "3 items",
          body: "Detail about A",
        },
        {
          title: "Topic B",
          summary: "5 items",
          body: "Detail about B",
        },
      ],
    };

    const result = await client.syncLinked(linked, { pace: 0 });

    // OP first, then summary, then 2 details — 4 posts total.
    expect(state.postedMessages.length).toBe(4);
    const op = state.postedMessages[0]!;
    expect(op.content).toBe("# Weekly");
    expect(op.threadTs).toBeUndefined();

    const summary = state.postedMessages[1]!;
    expect(summary.threadTs).toBe(op.ts);
    // Summary posted with placeholder URLs
    expect(summary.content).toContain("xxxxxxxxxx");
    expect(summary.content).not.toContain("https://slack.com");

    const detailA = state.postedMessages[2]!;
    const detailB = state.postedMessages[3]!;
    expect(detailA.content).toBe("Detail about A");
    expect(detailB.content).toBe("Detail about B");
    expect(detailA.threadTs).toBe(op.ts);
    expect(detailB.threadTs).toBe(op.ts);

    // Then summary edited with real permalinks (1 edit)
    expect(state.edits.length).toBe(1);
    expect(state.edits[0]!.ts).toBe(summary.ts);
    expect(state.edits[0]!.text).toContain("https://slack.com/archives/C123/p");
    expect(state.edits[0]!.text).not.toContain("xxxxxxxxxx");
    // Both section permalinks present
    expect(state.edits[0]!.text).toContain(
      `https://slack.com/archives/C123/p${detailA.ts.replace(".", "")}`,
    );
    expect(state.edits[0]!.text).toContain(
      `https://slack.com/archives/C123/p${detailB.ts.replace(".", "")}`,
    );

    expect(result.threadId).toBe(op.ts);
    expect(result.summaryIds).toEqual([summary.ts]);
    expect(result.detailIds).toEqual([detailA.ts, detailB.ts]);
    expect(result.sectionDetailIds).toEqual({
      "Topic A": detailA.ts,
      "Topic B": detailB.ts,
    });
  });
});

describe("SlackClient.syncLinked — existing thread", () => {
  it("edits the OP with summaryPrefix, syncs replies, leaves OP intact in replies sync", async () => {
    const { client, state } = makeClient("100.0");
    const linked: LinkedThread = {
      summaryPrefix: "# Updated",
      sections: [
        {
          title: "Solo",
          summary: "summary text",
          body: "Detail body",
        },
      ],
    };

    const result = await client.syncLinked(linked, {
      threadTs: "100.0",
      pace: 0,
    });

    // OP edit + summary final edit
    const opEdit = state.edits.find((e) => e.ts === "100.0");
    expect(opEdit).toBeDefined();
    expect(opEdit!.text).toBe("# Updated");

    // 2 posts: 1 summary + 1 detail (all into thread 100.0)
    expect(state.postedMessages.length).toBe(2);
    expect(state.postedMessages.every((p) => p.threadTs === "100.0")).toBe(
      true,
    );

    const [summary, detail] = state.postedMessages;
    expect(detail!.content).toBe("Detail body");

    // Summary edit with real link (excluding OP edit)
    const summaryEdits = state.edits.filter((e) => e.ts === summary!.ts);
    expect(summaryEdits.length).toBe(1);
    expect(summaryEdits[0]!.text).toContain(
      `https://slack.com/archives/C123/p${detail!.ts.replace(".", "")}`,
    );

    expect(result.threadId).toBe("100.0");
    expect(result.sectionDetailIds).toEqual({ Solo: detail!.ts });
  });
});

describe("SlackClient.syncLinked — dry run", () => {
  it("makes no API calls and returns a sentinel thread id when creating", async () => {
    const { client, state } = makeClient();
    const linked: LinkedThread = {
      summaryPrefix: "# Dry",
      sections: [
        { title: "A", summary: "s", body: "b" },
      ],
    };
    const result = await client.syncLinked(linked, { dryRun: true, pace: 0 });
    expect(state.postedMessages).toEqual([]);
    expect(state.edits).toEqual([]);
    expect(result.threadId).toBe("<new>");
    expect(result.sectionDetailIds).toEqual({});
  });
});
