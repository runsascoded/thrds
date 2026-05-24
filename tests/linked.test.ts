import { describe, it, expect } from "vitest";
import {
  buildDetailMessages,
  buildSummaryMessages,
  splitBody,
  type LinkedThread,
  type Section,
} from "../src/linked";

describe("splitBody", () => {
  it("returns the input unchanged when under limit", () => {
    expect(splitBody("short text", 100)).toEqual(["short text"]);
  });

  it("splits on paragraph boundaries", () => {
    const body = "Paragraph 1\n\nParagraph 2\n\nParagraph 3";
    expect(splitBody(body, 30)).toEqual([
      "Paragraph 1\n\nParagraph 2",
      "Paragraph 3",
    ]);
  });

  it("hard-splits an oversized paragraph on line boundaries", () => {
    expect(splitBody("Line 1\nLine 2\nLine 3", 14)).toEqual([
      "Line 1\nLine 2",
      "Line 3",
    ]);
  });
});

describe("buildDetailMessages", () => {
  it("emits one message per section when under limit", () => {
    const sections: Section[] = [
      { title: "A", summary: "a", body: "Detail A" },
      {
        title: "B",
        summary: "b",
        body: "Detail B part 1\n\nDetail B part 2",
      },
    ];
    const [msgs, starts] = buildDetailMessages(sections, 100);
    expect(msgs).toEqual([
      "Detail A",
      "Detail B part 1\n\nDetail B part 2",
    ]);
    expect(starts).toEqual({ 0: 0, 1: 1 });
  });

  it("splits oversize bodies and tracks section starts", () => {
    const sections: Section[] = [
      { title: "A", summary: "a", body: "Short A" },
      { title: "B", summary: "b", body: "Part 1\n\nPart 2" },
    ];
    const [msgs, starts] = buildDetailMessages(sections, 10);
    expect(msgs).toEqual(["Short A", "Part 1", "Part 2"]);
    expect(starts).toEqual({ 0: 0, 1: 1 });
  });
});

describe("buildSummaryMessages", () => {
  const linked = (overrides: Partial<LinkedThread> = {}): LinkedThread => ({
    summaryPrefix: "# Digest",
    sections: [
      { title: "A", summary: "stuff", body: "" },
      { title: "B", summary: "things", body: "" },
    ],
    ...overrides,
  });

  it("packs bullets into a single message under limit", () => {
    const urls = ["http://link-a", "http://link-b"];
    const msgs = buildSummaryMessages(linked(), urls, 200);
    expect(msgs.length).toBe(1);
    expect(msgs[0]).toBe(
      "# Digest\n- [**A**](http://link-a) — stuff\n- [**B**](http://link-b) — things",
    );
  });

  it("splits across multiple messages when limit forces it", () => {
    const urls = ["http://link-a", "http://link-b"];
    const msgs = buildSummaryMessages(linked(), urls, 60);
    expect(msgs.length).toBe(2);
    expect(msgs[0]).toBe("# Digest\n- [**A**](http://link-a) — stuff");
    expect(msgs[1]).toBe("- [**B**](http://link-b) — things");
  });

  it("appends summary_suffix to the last message when it fits", () => {
    const urls = ["http://link"];
    const msgs = buildSummaryMessages(
      linked({
        sections: [{ title: "A", summary: "stuff", body: "" }],
        summarySuffix: "_footer_",
      }),
      urls,
      200,
    );
    expect(msgs).toEqual([
      "# Digest\n- [**A**](http://link) — stuff\n_footer_",
    ]);
  });

  it("omits leading newline when summaryPrefix is empty", () => {
    const urls = ["http://link-a", "http://link-b"];
    const msgs = buildSummaryMessages(
      linked({ summaryPrefix: "" }),
      urls,
      200,
    );
    expect(msgs).toEqual([
      "- [**A**](http://link-a) — stuff\n- [**B**](http://link-b) — things",
    ]);
  });

  it("uses a custom bulletFn (e.g. Slack mrkdwn)", () => {
    const slackBullet = (s: Section, url: string): string =>
      `- <${url}|*${s.title}*> — ${s.summary}`;
    const urls = ["http://link-a", "http://link-b"];
    const msgs = buildSummaryMessages(linked(), urls, 200, slackBullet);
    expect(msgs).toEqual([
      "# Digest\n- <http://link-a|*A*> — stuff\n- <http://link-b|*B*> — things",
    ]);
  });
});
