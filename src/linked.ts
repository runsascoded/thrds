export interface Section {
  title: string;
  summary: string;
  body: string;
}

export interface LinkedThread {
  summaryPrefix: string;
  sections: Section[];
  summarySuffix?: string;
}

export interface LinkedSyncResult {
  threadId: string;
  summaryIds: string[];
  detailIds: string[];
  /** section title → first detail message ID */
  sectionDetailIds: Record<string, string>;
}

export type BulletFn = (section: Section, url: string) => string;

/** Default bullet format: bold linked title (Markdown). */
export const defaultBullet: BulletFn = (section, url) =>
  `- [**${section.title}**](${url}) — ${section.summary}`;

/** Split a body into messages on paragraph (then line) boundaries. */
export function splitBody(body: string, limit: number): string[] {
  if (body.length <= limit) return [body];
  const paragraphs = body.split("\n\n");
  const messages: string[] = [];
  let current = "";
  for (const para of paragraphs) {
    const candidate = current ? `${current}\n\n${para}` : para;
    if (candidate.length > limit) {
      if (current) messages.push(current);
      if (para.length > limit) {
        const lines = para.split("\n");
        current = "";
        for (const line of lines) {
          const c = current ? `${current}\n${line}` : line;
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

/**
 * Build detail messages for each section.
 *
 * Returns `[messages, sectionStarts]` where `sectionStarts[i]` is the
 * index (0-based within details) where section `i` begins.
 */
export function buildDetailMessages(
  sections: Section[],
  limit: number,
): [string[], Record<number, number>] {
  const messages: string[] = [];
  const sectionStarts: Record<number, number> = {};
  for (let i = 0; i < sections.length; i++) {
    sectionStarts[i] = messages.length;
    const parts = splitBody(sections[i]!.body, limit);
    messages.push(...parts);
  }
  return [messages, sectionStarts];
}

/**
 * Build summary messages with section bullets and links.
 *
 * Greedy-packs bullets into messages respecting the char limit.
 * `sectionUrls[i]` is the link URL for section `i` (placeholder or real).
 */
export function buildSummaryMessages(
  linked: LinkedThread,
  sectionUrls: string[],
  limit: number,
  bulletFn: BulletFn = defaultBullet,
): string[] {
  const bullets = linked.sections.map((s, i) => bulletFn(s, sectionUrls[i]!));
  const messages: string[] = [];
  let current = linked.summaryPrefix;

  for (const bullet of bullets) {
    const candidate = current ? `${current}\n${bullet}` : bullet;
    if (candidate.length > limit) {
      if (current) messages.push(current);
      current = bullet;
    } else {
      current = candidate;
    }
  }

  if (linked.summarySuffix) {
    const candidate = current
      ? `${current}\n${linked.summarySuffix}`
      : linked.summarySuffix;
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
