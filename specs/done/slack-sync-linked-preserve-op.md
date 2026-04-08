# Spec: `sync_linked` on Slack must not overwrite the thread OP

## Problem

In Slack, the thread parent (OP) IS the first message returned by `conversations.replies`. `sync_linked` treats it as the first sync target and overwrites it with S bullet content, destroying the XS headline.

In Discord, the OP is a channel message outside the thread — the thread has a system message at index 0 that's already filtered out. So Discord doesn't have this issue.

## Fix

`SlackClient.sync_linked` should skip the OP (index 0) when syncing thread replies. The OP content is managed separately by the caller (or via `summary_prefix` if provided).

When `summary_prefix` is non-empty, `sync_linked` should edit the OP to `summary_prefix` content. When empty, leave the OP unchanged.

Thread replies (S bullets + M details) start at index 1.

## Current behavior

`sync_linked` calls `sync()` which calls `list_messages()` returning all messages including OP. The desired messages list doesn't include the OP, so the OP gets edited to the first desired message (S bullets), and everything shifts.

## Also needed (done)

Slack bold linked titles: `<url|*Title*>` format — implemented in `linked-title-format` spec.
