# Load Balancing

A load balancer sits between clients and a pool of backend servers and decides which
server handles each request. The goal is to spread load evenly, avoid sending traffic
to dead or overloaded instances, and let you scale horizontally by adding boxes instead
of buying a bigger one. Almost every system at scale has one in front of the app tier,
and often a second layer in front of the database or cache tier.

## L4 vs L7

Layer 4 load balancers work at the TCP/UDP level. They look at IP and port, not at the
request content, and just forward packets. Fast, cheap, protocol-agnostic, but blind to
what's inside the request. AWS NLB and raw IPVS setups are L4.

Layer 7 load balancers terminate the connection and read the actual HTTP request — path,
headers, cookies, method. That lets them route `/api/*` to one service and `/static/*` to
another, do TLS termination, add retry logic, and inspect payloads for smarter decisions.
Costs more CPU per request because you're doing a full parse. Nginx, HAProxy, Envoy, and
AWS ALB operate at L7.

Most production stacks use both: an L4 balancer (or the cloud provider's edge network) in
front to absorb raw connection volume, then L7 for anything content-aware.

## Algorithms

- Round robin: requests go to servers in rotation. Simple, works fine when all servers and
  all requests are roughly equal cost.
- Weighted round robin: same idea but bigger servers get a higher share.
- Least connections: send the request to whichever backend currently has the fewest open
  connections. Better than round robin when request cost varies a lot.
- Least response time: like least connections but factors in observed latency too.
- IP hash / consistent hashing: derive the backend from a hash of the client IP or a request
  key, so the same client (or key) keeps landing on the same backend. Needed for sticky
  sessions and for cache-friendly routing. See consistent-hashing.md for the rebalancing
  problem this solves.
- Random with two choices: pick two backends at random, send to whichever has less load.
  Surprisingly close to optimal least-connections behavior with much less coordination
  overhead — this is what a lot of large-scale systems actually use internally.

## Health checks

The load balancer polls each backend (HTTP `/health`, TCP connect, or an active probe) on
an interval, and pulls a backend out of rotation if it fails N consecutive checks. Too
aggressive and you flap servers out over a GC pause; too lax and you keep sending traffic
to a box that's already dead. A typical setup: 5-second interval, 2-3 consecutive failures
to mark unhealthy, 1-2 consecutive successes to bring it back.

Passive health checks (watching real traffic for errors/timeouts) catch failures faster
than active polling but need care not to overreact to a single blip.

## Sticky sessions

If a server holds session state in memory, the load balancer needs to keep sending the
same client to the same server (via a cookie or IP hash). This works but couples clients
to specific servers, which hurts rebalancing and makes deploys/failovers messier. The
better long-term fix is to move session state out to a shared store (Redis, a database) so
any backend can serve any request, and the load balancer stays free to route however it wants.

## Failure and scaling

A single load balancer is a single point of failure, so production setups run at least two,
either active-passive with a floating IP or active-active behind DNS/anycast. Cloud LBs
(ALB, GCLB) hide this by being managed as a horizontally-scaled service themselves.

Global load balancing (routing a user to the nearest region) is a separate layer done via
GeoDNS or anycast, sitting above the per-region load balancers described here.
