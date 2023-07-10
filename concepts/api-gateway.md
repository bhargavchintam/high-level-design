# API Gateway

An API gateway is the single entry point that sits in front of a collection of backend
services (often microservices) and handles the cross-cutting concerns so individual services
don't each have to reimplement them: authentication, rate limiting, request routing, request/
response transformation, and often TLS termination and logging.

## What it replaces

Without a gateway, a client talking to 15 microservices needs to know 15 hostnames, handle
auth against each independently, and each service needs to duplicate rate limiting, auth
validation, and logging. The gateway centralizes that: the client talks to one host, the
gateway authenticates the request once, then routes it to whichever backend service owns that
path — `/users/*` to the user service, `/orders/*` to the order service.

## Core responsibilities

- Routing: path-based or header-based dispatch to the right backend service.
- Authentication/authorization: validate a JWT or session token once at the edge, so
  downstream services can trust an already-validated identity (often passed along as a header
  or inside the token) instead of each re-implementing auth.
- Rate limiting: enforce per-client quotas before a request reaches any backend — see
  rate-limiting.md.
- Request/response transformation: adapt an external-facing API shape to whatever the
  internal service actually expects (useful when internal services evolve faster than the
  public contract should).
- Aggregation: some gateways support composing a response from multiple backend calls into
  one client-facing response (a lightweight BFF — backend-for-frontend — pattern), trading
  gateway complexity for fewer client round trips.
- Circuit breaking: stop forwarding requests to a backend that's clearly failing, and fail
  fast instead of piling up timeouts — protects the gateway itself from being taken down by a
  single misbehaving downstream service.

## Cost of centralizing this

Every request now has an extra hop, and the gateway becomes a shared dependency — if it's
down, everything behind it is unreachable regardless of whether those services are healthy.
That means the gateway itself needs to be more heavily invested in for availability than any
single backend service: horizontally scaled, deployed across zones, with aggressive health
checking of what it's routing to.

The gateway can also become a deployment bottleneck if routing config, auth logic, and
transformation rules live in one team-owned codebase that every other team has to go through
to ship a change — this is a common reason large orgs split gateway concerns (e.g. auth as a
separate edge layer, rate limiting as its own service) rather than putting everything in one
monolithic gateway.

## Gateway vs load balancer

A load balancer distributes traffic across replicas of the same service based on connection
count or round robin — it doesn't know or care what the request means. A gateway routes based
on the content of the request (path, method, headers) to different services, and adds
application-aware logic (auth, transformation, rate limiting) on top. In practice a gateway
usually sits in front of a set of per-service load balancers: the gateway decides which
service, the load balancer decides which instance of that service.

## Real implementations

Kong, AWS API Gateway, Envoy (often as the data plane under Istio), and Nginx-based custom
gateways are the common choices. AWS API Gateway is fully managed and pairs naturally with
Lambda for serverless backends; Kong and Envoy are self-hosted and give more control over
custom plugins and latency, at the cost of operating them yourself.
