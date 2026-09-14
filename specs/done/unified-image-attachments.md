# Spec: unified image attachments (`Image`), one surface for Slack + Discord

## Why

Attachments diverged into two shapes that are *isomorphic from the author's POV* ("put this picture in the message"):

- **Slack** — a trailing `![alt](url)` line in the message content, lifted to a Block Kit *image block* (`specs/done/editable-image-blocks.md`); refresh in place by editing the URL, `{bust}` for byte-changes under a stable URL.
- **Discord** — a programmatic `Msg.files=[Path]` (landed in `specs/done/discord-webhook-attachments.md`), uploaded as a real multipart *attachment* via the webhook; refresh by re-uploading on edit.

So the same intent has two authoring surfaces (a doc `![]()` vs a `files=` kwarg) and two platform-specific mechanisms. This unifies them behind one `Image` type reachable from **both** a doc and the library, honored by **both** platforms.

`Msg.files` landed this session and nothing consumes it yet (mgu still pins a pre-`files` `thrds`), so there is **no BC cost** to reshaping it — this is the last cheap moment before it ossifies.

## The one real asymmetry

A **hosted URL is the universal currency; local bytes are not.**

- Give a **URL** → both platforms (Slack references the block URL; Discord `GET`s the URL to get bytes, then uploads — so the attachment stays native, not a hot-linked embed).
- Give **local bytes** → Discord uploads natively; **Slack has no native path without first hosting them.**

Decision (2026-09-14): **URL-first.** A path-only image on Slack **raises** with a clear "give a URL / host it" message. Hosting local bytes on Slack via its own `files.upload` flow is **deferred** — see [Deferred](#deferred-lazy-upload-on-slack).

## Model

`thrds.core.Image`:

```python
@dataclass
class Image:
    url: str | None = None       # hosted URL — cross-platform currency
    path: Path | str | None = None  # local bytes — Discord-native; Slack raises
    alt: str = ""
    bust: bool = False           # cache-bust a stable URL on edit (Slack only)
```

- Validation: at least one of `url` / `path` (both allowed; a byte-uploader prefers `path`, a referencer prefers `url`). `bust` with no `url` is meaningless → error.
- `Msg.images: Sequence[Image] = ()` **replaces** `Msg.files`.
- `ThreadClient.post` / `edit` take `images: Sequence[Image] = ()` (replacing the `files` kwarg added this session). Empty is byte-identical to pre-feature everywhere.

## Two authoring surfaces, one model

1. **Doc**: a trailing `![alt](target){bust?}` line — `target` is a URL (has a scheme) or a local path (resolved relative to the doc file). Parsed to `Image` at resolve time (below).
2. **Programmatic**: `Msg(content, images=[Image(...)])`.

Both funnel to `Msg.images`; every client consumes only `Msg.images`.

### Resolve-layer lifting (the key move)

`md.resolve_messages` (and the Slack/Discord doc-push paths that call it) lift the trailing image run out of each message's content into `Msg.images`, leaving `content` clean:

```
DocMessage("text\n![plot](https://…/p.png){bust}")  →  Msg("text", images=[Image(url="https://…/p.png", alt="plot", bust=True)])
DocMessage("text\n![plot](./p.png)")                 →  Msg("text", images=[Image(path="<docdir>/p.png", alt="plot")])
```

Why lift at resolve, not per-client: `discord push` must know a message has an image **before** routing, because the Discord **bot** can't upload — an image forces the **webhook** transport (exactly like a per-sender override). Detecting that requires the image on the `Msg`, not buried in content. Lifting once at resolve also makes the doc genuinely platform-agnostic.

Classification: `urllib.parse.urlsplit(target).scheme` non-empty ⇒ URL; else a path (resolved against the doc's directory). `{bust}` only valid with a URL.

## Per-client resolution

- **Slack** (`post`/`edit`, `images=`): each `Image` → an `ImageRef(alt, url, bust)` → existing `_lift_image_blocks`. **`path`-only raises** (URL-first). Direct string callers keep working: `post("text\n![](url)")` still content-lifts via `split_trailing_images` (unchanged) — content-lift and `images=` both build blocks, so both surfaces converge on the same wire payload. (The resolve layer now hands Slack clean content + `images=`, producing the identical block payload it produced from content-embedded images before.)
- **Discord webhook** (`post`/`edit`, `images=`): each `Image` → a local file path — `path` used directly; `url` fetched (`GET`) into the scratch dir — then the existing `_files_form` multipart upload. The public `files=` param is **replaced** by `images=`; local-file upload is now `images=[Image(path=p)]`. `bust` is ignored (a re-upload always refreshes).
- **Discord bot** (`post`/`edit`): non-empty `images` **raises** — attachments need the webhook transport (as senders do). `DiscordHybridClient` already routes a sender-or-attachment message to the webhook; the routing predicate reads `Msg.images` too.
- **Bluesky**: non-empty `images` **raises** (embeds not wired).

## CLI / routing

- `discord push`: `needs_webhook = any(Msg with a sender OR images)` (was sender-only). An image-bearing doc without a webhook env errors the same way a per-sender doc does.
- Slack `push`/`promote`: unchanged in behavior — resolve now feeds `images=` instead of content-embedded images, same wire result.

## Tests

- `Image` validation (needs url or path; `bust` needs url).
- Resolve lifts trailing `![]()` (url and local-path forms, `{bust}`) into `Msg.images`, strips content; a mid-message image is **not** lifted (trailing-only, per `editable-image-blocks.md`).
- Slack `post`/`edit` with `images=[Image(url=…)]` build the same blocks as the content-embedded form (assert identical payload); `Image(path=…)` raises.
- Discord webhook `post`/`edit` with `images=[Image(path=…)]` → exact multipart form; `Image(url=…)` fetches then uploads (stub the fetch); the hybrid routes an image OP through the webhook.
- Discord bot / Bluesky raise on non-empty `images`.
- `discord push` of a doc with a `![](url)` OP routes through the webhook.
- A Slack doc-push regression check: an existing `![](url)` doc produces the same `chat.postMessage` payload as before the resolve-layer move.

## Implemented (2026-09-14)

Everything above except the Deferred section. Notes on where the design met reality:

- **`core.Image`** (`url`/`path`/`alt`/`bust`, validated) and **`Msg.images`** replace `Msg.files`; `ThreadClient.post`/`edit` carry `images`. Exported as `thrds.Image`.
- **Resolve lift** (`md.resolve_messages(..., lift_images=, base_dir=)`): off by default; `discord push` passes `lift_images=True, base_dir=<doc dir>`. Reuses `imageblock.split_trailing_images` (so emoji-image lines and mid-message images are untouched), classifying each target as URL (has a scheme) or local path.
- **Slack folds, not blocks-from-a-side-channel.** The design said "Slack builds blocks from `images=`"; reality: Slack's image round-trip lives in *content* (read-back reconstructs `![](url)` lines), so building blocks from a separate channel would make every re-push see a phantom diff. Instead `Msg.images` **fold into content** as trailing `![alt](url)` lines (`_fold_images_into_content`, URL-first raise) — in `SlackClient.sync` (before the reconcile diff, so it stays idempotent) and in `post`/`edit` (for direct callers). Same wire payload, idempotent converge. This is why Slack keeps `lift_images=False`: it *wants* images in content.
- **Discord webhook** resolves `Image`s to local paths (`_image_paths`: `path` direct, `url` fetched to a temp dir named by the URL basename, cleaned up after) and uploads via the existing `_files_form` multipart. Bot/Bluesky raise on non-empty `images`.
- **Idempotency**: Slack via the content round-trip (a no-op converge is a no-op). Discord keeps clean content on both the desired (lifted) and live (read-back) sides, so an image-bearing message SKIPs unless its text changed — the documented trade (a bytes-only change with unchanged text won't refresh).
- **Tests**: `test_attachments.py` (`Image` validation, `Msg.images` plumbing, the platform raises), `test_doc_sender.py` (resolve lift: url/path/bust/mid-message/sender), `test_image_blocks.py` (Slack `images=` folds to the identical content payload; path-only raises), `test_discord_webhook.py` (path + url-fetch multipart), `test_discord_hybrid.py` (attachment OP routing + refresh), `test_discord_cli.py` (`discord push` of a doc-image OP). Full suite green (1492 passed).

## Lazy upload on Slack — done (was deferred)

Implemented in `specs/slack-lazy-image-upload.md`: a **path** image on Slack uploads via the external-upload flow and references the file by id (`slack_file`, no public URL — the historically-finicky permalink/public-visibility path turned out to be unnecessary), content-addressed so a re-push uploads iff the bytes change. So `path` images now work on **both** platforms; the URL-first framing survives only as "a hosted URL is the cross-platform currency," not as a Slack limitation.
