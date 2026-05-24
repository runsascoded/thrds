import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { sync } from "../src/core";
import type { Message, Thread, ThreadClient } from "../src/core";

interface FixtureAction {
  type: string;
  index: number;
  message_id?: string;
  content?: string;
  prior_content?: string;
}

interface FixtureMessage {
  id: string;
  content: string;
  editable?: boolean;
}

interface FixtureCase {
  name: string;
  description?: string;
  thread_id: string | null;
  existing: FixtureMessage[];
  desired: string[];
  expected_actions: FixtureAction[];
  expected_message_ids?: string[];
  expected_final?: FixtureMessage[];
  expected_final_contents?: string[];
}

const fixturePath = resolve(__dirname, "fixtures", "sync.json");
const cases: FixtureCase[] = JSON.parse(
  readFileSync(fixturePath, "utf-8"),
).cases;

class FixtureClient implements ThreadClient {
  threads: Map<string, Message[]>;
  private newIdCounter = 0;

  constructor(initial: Map<string, Message[]>) {
    this.threads = initial;
  }

  private nextId(): string {
    const id = `n${this.newIdCounter}`;
    this.newIdCounter++;
    return id;
  }

  async list(threadId: string): Promise<Message[]> {
    return [...(this.threads.get(threadId) ?? [])];
  }

  async post(content: string, threadId?: string): Promise<Message> {
    const msg: Message = { id: this.nextId(), content, editable: true };
    if (threadId === undefined) {
      this.threads.set(msg.id, [msg]);
    } else {
      const replies = this.threads.get(threadId) ?? [];
      replies.push(msg);
      this.threads.set(threadId, replies);
    }
    return msg;
  }

  async edit(messageId: string, content: string): Promise<Message> {
    for (const msgs of this.threads.values()) {
      const i = msgs.findIndex((m) => m.id === messageId);
      if (i >= 0) {
        const old = msgs[i]!;
        if (old.editable === false) {
          throw new Error(`edit() called on non-editable ${messageId}`);
        }
        const updated: Message = { id: messageId, content, editable: true };
        msgs[i] = updated;
        return updated;
      }
    }
    throw new Error(`Message ${messageId} not found`);
  }

  async delete(messageId: string): Promise<void> {
    for (const msgs of this.threads.values()) {
      const i = msgs.findIndex((m) => m.id === messageId);
      if (i >= 0) {
        const old = msgs[i]!;
        if (old.editable === false) {
          throw new Error(`delete() called on non-editable ${messageId}`);
        }
        msgs.splice(i, 1);
        return;
      }
    }
    throw new Error(`Message ${messageId} not found`);
  }
}

describe("sync fixtures", () => {
  for (const c of cases) {
    it(c.name, async () => {
      const initialThreads = new Map<string, Message[]>();
      if (c.thread_id !== null) {
        initialThreads.set(
          c.thread_id,
          c.existing.map((m) => ({
            id: m.id,
            content: m.content,
            editable: m.editable ?? true,
          })),
        );
      }
      const client = new FixtureClient(initialThreads);
      const desired: Thread = { messages: c.desired };
      const result = await sync(client, desired, c.thread_id ?? undefined);

      expect(result.actions.length).toBe(c.expected_actions.length);
      for (let i = 0; i < c.expected_actions.length; i++) {
        const actual = result.actions[i]!;
        const expected = c.expected_actions[i]!;
        expect(actual.type).toBe(expected.type);
        expect(actual.index).toBe(expected.index);
        if (expected.message_id !== undefined) {
          expect(actual.messageId).toBe(expected.message_id);
        }
        if (expected.content !== undefined) {
          expect(actual.content).toBe(expected.content);
        }
        if (expected.prior_content !== undefined) {
          expect(actual.priorContent).toBe(expected.prior_content);
        }
      }

      if (c.expected_message_ids !== undefined) {
        expect(result.messageIds).toEqual(c.expected_message_ids);
      }

      const finalMsgs = client.threads.get(result.threadId) ?? [];
      if (c.expected_final !== undefined) {
        const actualFinal = finalMsgs.map((m) => ({
          id: m.id,
          content: m.content,
          editable: m.editable ?? true,
        }));
        expect(actualFinal).toEqual(c.expected_final);
      } else if (c.expected_final_contents !== undefined) {
        expect(finalMsgs.map((m) => m.content)).toEqual(
          c.expected_final_contents,
        );
      }
    });
  }
});

describe("fixture file shape", () => {
  it("has unique case names", () => {
    const names = cases.map((c) => c.name);
    expect(new Set(names).size).toBe(names.length);
  });

  it("uses valid action types", () => {
    const valid = new Set(["SKIP", "EDIT", "POST", "DELETE"]);
    for (const c of cases) {
      for (const a of c.expected_actions) {
        expect(valid.has(a.type)).toBe(true);
      }
    }
  });
});
