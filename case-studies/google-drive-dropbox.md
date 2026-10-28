# Google Drive / Dropbox-Style File Sync

## Requirements

### Functional
- Upload/download files, organize into folders.
- Sync changes across multiple devices for the same user automatically.
- Share files/folders with other users, with permission levels.
- Version history — recover an earlier version of a file.
- Efficient sync for large files with small changes (don't re-upload the whole file for a
  one-line edit).

### Non-functional
- Storage efficiency at scale — don't store N full duplicate copies of the same bytes if
  possible (dedup).
- Sync should be fast for small edits and shouldn't saturate a user's bandwidth for every
  minor change.
- Conflict handling when the same file is edited offline on two devices.
- Strong durability — this is a product where losing a user's file is close to worst-case.

## Capacity Estimation

100M users, average 5 GB stored per user → 500 PB total stored, before any dedup savings.
Real-world dedup and delta-encoding (below) meaningfully reduce this, but 500 PB raw is the
right order of magnitude to reason about — this is squarely an object-storage problem, not a
database problem.

Assume each user makes ~20 file changes/day (edits, new files, deletes) that need to sync.
100M × 20 = 2B sync events/day → 2B / 86,400 ≈ 23,000 sync events/sec average, ~70,000/sec
peak.

Average file size varies hugely, but the more important number for sync efficiency is average
change size — if delta sync (below) uploads only the changed blocks of a file rather than the
whole file, a typical edit might transfer a few KB to a few MB instead of the full file size,
which is the difference between a sync system that's usable on a mobile connection and one
that isn't.

## API Design

```
POST /v1/files                      upload (new file or new version)
GET  /v1/files/{id}                 download
GET  /v1/files/{id}/versions        list version history
POST /v1/files/{id}/share           { user_id, permission }
GET  /v1/sync/changes?since=cursor  -> delta of changes since last sync (per device)
```

The sync endpoint is the one that matters most architecturally — each client device tracks a
cursor/version marker and asks "what changed since I last synced," rather than the server
pushing every change to every device unconditionally.

## Data Model

```
files
  file_id (PK), owner_id, name, folder_id, size, mime_type
  current_version_id

file_versions
  version_id (PK), file_id, created_at, size
  block_list: [block_hash, block_hash, ...]   (ordered list of content-addressed blocks)

blocks (content-addressed, deduplicated storage)
  block_hash (PK) -> object storage location, reference_count

permissions
  file_id, user_id, level (read|write|owner)

sync_log (per user, per device)
  device_id -> last_synced_cursor
```

The `blocks` table is what enables both delta sync and cross-user deduplication: a file is
represented as an ordered list of content-hashed chunks, and if two users upload the same
chunk (identical file, or a shared chunk between two versions), it's stored once and
reference-counted rather than duplicated.

## High-Level Architecture

```
 client ──► chunk file into blocks (e.g. 4MB, or variable-size via
             content-defined chunking) ──► hash each block

 for each block:
   client ──HEAD block_hash──► Metadata Service: "do you already have this block?"
                                      │
                          yes ────────┴──── no
                          │                  │
                    skip upload        upload block to Object Storage,
                    (just reference     Metadata Service records it
                     the existing
                     block)

 client ──► commit new file_version { ordered block_hash list } ──► Metadata Service

 other devices for the same user/share:
   poll or get pushed a sync notification ──► GET /sync/changes?since=cursor
                                          ──► fetch only new/changed blocks
                                          ──► reassemble file locally
```

## Deep Dives

**Chunking and delta sync.** Instead of treating a file as one opaque blob, split it into
content-addressed chunks (hashed with something like SHA-256) so that when a file changes, only
the chunks that actually changed need to be re-uploaded and re-downloaded — everything else is
already present and just gets referenced by hash. Content-defined chunking (chunk boundaries
determined by the content itself, e.g. via a rolling hash, rather than fixed byte offsets) is
what makes this robust to insertions: inserting a byte at the start of a naively fixed-offset-
chunked file would shift every subsequent chunk boundary and defeat dedup entirely; content-
defined chunking keeps chunk boundaries stable around the actual edit.

**Deduplication.** Because blocks are content-addressed, identical content uploaded by
different users (a common shared template file, a popular PDF) or different versions of the
same file (unchanged chunks across edits) is stored exactly once in object storage, with a
reference count tracking how many file_versions point at it. A block is only actually deleted
from storage once its reference count hits zero. At scale, this measurably reduces total
storage relative to the naive 500 PB estimate above, though how much depends heavily on how
correlated user content actually is.

**Conflict resolution.** Two devices editing the same file while offline, then both coming
back online, is the hard case. The system detects this by version vectors or a simple
"expected parent version" check on commit — if a device tries to commit a new version whose
parent isn't the current server-side version, that's a conflict, not a normal update. The
common resolution (what Dropbox actually does) is not to attempt an automatic merge of
arbitrary file content — instead, keep both versions and surface it to the user as a
"conflicted copy" file, letting them manually reconcile. Automatic merging is only safe for
structured, mergeable formats (which is a much narrower, format-specific problem, e.g. what
Google Docs does with operational transforms on its own document format — not something a
general file-sync layer can do for an arbitrary binary file).

**Sync notification.** Rather than every device polling constantly, a lightweight
long-poll or push-notification channel tells other devices "something changed for this
account," which triggers them to call the changes endpoint and pull the actual delta. This
keeps idle devices cheap (a held-open connection or occasional heartbeat) while making sync
propagation fast (near-real-time) when something actually changes — the same push-vs-poll
tradeoff that shows up in [case-studies/notification-system.md](notification-system.md).

## Bottlenecks & Scaling

Metadata operations (checking block existence, committing new versions, listing changes) are
comparatively small and fast, and scale by sharding the metadata store by user_id or file_id.
The real scaling and cost driver is object storage volume and dedup effectiveness — a chunking
strategy with a good dedup ratio changes the storage bill by potentially double-digit
percentages at this scale, which is why real systems (Dropbox's own published engineering
writeups are the reference here) invest heavily in block-level chunking rather than treating
file sync as "just re-upload the whole file on every save."
