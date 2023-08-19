# Message Queues

A message queue decouples the producer of work from the consumer that processes it. Instead
of service A calling service B directly and waiting for a response, A writes a message to a
queue and moves on; B reads from the queue on its own schedule. This buys three things:
producers and consumers can scale independently, a slow or down consumer doesn't take the
producer down with it, and bursty traffic gets smoothed out because the queue absorbs spikes
that would otherwise overload a downstream service.

## Point-to-point vs pub/sub

Point-to-point (queue semantics): each message is consumed by exactly one consumer. Multiple
consumers reading the same queue compete for messages — used for work distribution, like a
pool of workers processing image resize jobs.

Pub/sub (topic semantics): a message published to a topic is delivered to every subscriber
independently. Used when multiple, unrelated services need to react to the same event — an
order-placed event might trigger inventory update, email confirmation, and analytics, each a
separate subscriber.

Some systems support both: Kafka's consumer groups let you have pub/sub across groups and
point-to-point load balancing within a group.

## Delivery guarantees

- At-most-once: message might be lost, never redelivered. Fast, used when losing an
  occasional message is fine (metrics, logs).
- At-least-once: message is redelivered until acknowledged, so it may arrive more than once.
  The default for most systems (SQS, Kafka with manual commit) because losing messages is
  usually worse than duplicating them.
- Exactly-once: the message is processed exactly one time, no loss, no duplication. Genuinely
  hard across a network — most "exactly-once" systems actually deliver at-least-once and rely
  on the consumer being idempotent (dedup by message ID) to fake exactly-once semantics
  end-to-end. Kafka's transactional producer/consumer gets closer to true exactly-once within
  the Kafka ecosystem, but crossing into an external system (a database write) still needs an
  idempotency key.

## Ordering

Global ordering across a queue with multiple consumers is expensive to guarantee and rarely
needed. The usual compromise: partition the queue by key (e.g. by user ID or order ID), and
guarantee ordering only within a partition. All messages for the same key go to the same
partition and are processed in order; different keys can be processed out of order relative
to each other, which is fine for most use cases. Kafka partitions and SQS FIFO's message
group ID both work this way.

## Kafka vs SQS vs RabbitMQ, roughly

- Kafka: log-based, messages persist for a retention window (not deleted on read), consumers
  track their own offset. Built for high throughput and replay — multiple consumer groups can
  independently re-read the same stream. The default choice for event streaming and
  analytics pipelines.
- RabbitMQ: traditional broker with routing (exchanges, bindings), messages are removed once
  acknowledged. Strong at complex routing topologies and lower-latency task queues, weaker at
  raw throughput than Kafka.
- SQS: fully managed, simple queue semantics, no infrastructure to run. Standard queues are
  at-least-once with best-effort ordering; FIFO queues add strict ordering and exactly-once
  processing within a message group at lower throughput. The default choice when you don't
  want to operate a broker yourself.

## Backpressure and dead-letter queues

If consumers can't keep up, the queue grows — that's the point, it's the buffer. But it needs
a bound (max queue depth or max age) with alerting, or an unbounded backlog just delays the
inevitable failure instead of preventing it. Messages that fail processing repeatedly should
go to a dead-letter queue after N retries rather than blocking the main queue or retrying
forever — that isolates poison messages so one bad message doesn't stall the whole pipeline.
