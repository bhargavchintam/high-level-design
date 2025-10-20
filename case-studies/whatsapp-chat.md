# WhatsApp-Style Chat System

## Requirements

### Functional
- One-to-one messaging, delivered in real time when both users are online.
- Messages queue and deliver when a recipient comes back online (offline delivery).
- Delivery receipts: sent, delivered, read.
- Group chats (up to a few hundred members).
- Media messages (images, video, voice notes).
- End-to-end encryption (mentioned for completeness; the crypto itself is out of scope for
  the system design, but it constrains the architecture — the server cannot read message
  content, only route encrypted blobs).

### Non-functional
- Low latency delivery when both parties are online (under a second, ideally much less).
- Messages must not be lost, even if a client is offline for days.
- Massive concurrent connection count — hundreds of millions of devices holding an open
  connection simultaneously.
- Ordering: messages within a single conversation must arrive in the order they were sent.

## Capacity Estimation

2B users, 100B messages sent per day (WhatsApp-scale, real published figure is in this
range). That's 100B / 86,400 ≈ 1.16M messages/sec average, ~3.5M/sec at peak.

Concurrent connections: assume 500M users online simultaneously at peak (a fraction of 2B
total, but still enormous) — each holds one persistent WebSocket/TCP connection. A single
well-tuned server can hold roughly 1-2M concurrent idle connections (this is what drives
using an event-loop architecture like Erlang/BEAM, which WhatsApp actually used, rather than
a thread-per-connection model). At 1M connections/server, that's ~500 connection-handling
servers just to hold the sockets open, before any message-routing load.

Storage: average message ~100 bytes (text) — media is stored/transferred separately via
object storage and CDN, referenced by pointer, not stored inline. 100B messages/day × 100
bytes ≈ 10 TB/day of message metadata if every message were retained server-side. In
practice, delivered messages are deleted from the server shortly after delivery (this is a
key design choice — see deep dive), so steady-state server storage is dominated by
temporarily-undelivered messages for offline users, not by total message history.

## API Design

Primarily a persistent connection protocol (WebSocket or a custom binary protocol over TCP),
not REST — request/response doesn't fit a real-time bidirectional chat well. Message shape,
roughly:

```
# client -> server, over the open connection
SEND { to: user_id, client_msg_id, ciphertext, timestamp }

# server -> client (recipient online)
DELIVER { from: user_id, msg_id, ciphertext, timestamp }

# server -> client (sender), delivery receipts
ACK { client_msg_id, status: sent|delivered|read }

# on reconnect after being offline
SYNC { since: last_seen_msg_id } -> [ queued messages ]
```

## Data Model

```
messages (per-recipient inbox, sharded by recipient user_id)
  msg_id (PK)
  from_user_id
  to_user_id
  ciphertext
  sent_at
  status: sent | delivered | read
  # deleted from this table once delivered and acked, not kept indefinitely

conversations
  conversation_id, participant_ids[], last_message_at   (for chat list UI)

user_presence
  user_id -> { online: bool, last_seen, connected_server_id }
```

The `messages` table functions as a per-user queue more than a permanent log — once a message
is delivered and acknowledged, it's removed from server storage. This is a deliberate privacy
and storage-cost decision, not an oversight.

## High-Level Architecture

```
 Sender device ──WS──► Connection Server A
                              │
                        (looks up recipient's
                         connected server via
                         presence service)
                              │
                     recipient online? ──yes──► forward over that
                              │                  connection server's
                              │                  socket to recipient
                              no
                              │
                              ▼
                     write to recipient's
                     offline message queue
                     (persisted store)
                              │
                     on recipient reconnect:
                     connection server pulls
                     queued messages, delivers,
                     client acks, server deletes
```

Each device holds a persistent connection to one of many connection servers. A presence
service (backed by something like Redis) tracks which connection server each online user is
attached to, so a message from user A can be routed to the specific server holding user B's
socket — this routing indirection is the core of the whole system.

## Deep Dives

**Connection server architecture.** Holding hundreds of millions of concurrent idle
connections rules out a thread-per-connection model (thread stacks alone would exhaust memory
at that scale). An event-loop model where one process handles many thousands of connections
using async I/O is required — this is the reason WhatsApp famously ran on Erlang/BEAM
(lightweight processes, built-in message passing, designed for exactly this workload). The
generalizable point for any language: this tier needs to be built on an async/event-driven
I/O model, not a blocking one.

**Presence and routing.** Every connection server registers, in a shared presence store, which
users are connected to it and updates on connect/disconnect. When server A needs to deliver a
message to a user connected to server B, it looks up B in the presence store and forwards the
message to B over internal server-to-server infrastructure, and B pushes it down the socket to
the recipient. This presence store is a hot path on every cross-server message and needs to be
fast and horizontally scaled (sharded by user_id) — a slow presence lookup adds latency to
every single message.

**Offline delivery and ordering.** If the recipient isn't connected anywhere, the message goes
into a persisted per-recipient queue instead. On reconnect, the client syncs from its last
known message ID and the server drains the queue in order. Ordering within a single
conversation is preserved by using a monotonically increasing ID per conversation (or per
sender-recipient pair) rather than relying on wall-clock timestamps, which can be skewed
across servers.

**Delivery receipts (sent/delivered/read).** Three separate acks moving in the opposite
direction of the message: "sent" as soon as the server accepts the message from the sender,
"delivered" once the recipient's client acks receipt, "read" once the recipient's client acks
that the user actually viewed it. Each of these is its own small message flowing back to the
sender's connection server (found via the same presence lookup), and if the sender is offline
when a receipt arrives, the receipt itself queues the same way a message would.

**Group chats.** A message to a group of N members is fanned out to N individual deliveries
(or N offline-queue writes) rather than being a single shared broadcast object, because
delivery and read state is inherently per-recipient. This caps practical group size (WhatsApp
limits groups to a few hundred members) since a group message otherwise becomes its own
mini fan-out problem, similar in shape to the celebrity fan-out issue in feed systems, just at
a bounded and much smaller scale.

## Bottlenecks & Scaling

Connection count is the primary scaling axis, addressed by horizontally adding connection
servers and keeping each one lean (event-loop, minimal per-connection memory). The presence
store is the second-order bottleneck since every cross-server message touches it — sharded by
user_id and run with replication so a shard loss doesn't blackout message routing for the
users on it. Offline message storage needs to handle a write spike whenever a large group of
users comes back online simultaneously (e.g. after a regional network outage) — a queue-based
draining process rather than pushing all queued messages synchronously on reconnect avoids
overwhelming a single connection server at that moment.
