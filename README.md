# circuit-break

A circuit breaker with half-open probing for Python. Standard library only.

```python
from circuit_break import CircuitBreaker, CircuitOpenError

cb = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)

@cb
def fetch_user(user_id):
    return {"id": user_id, "name": "Ada"}

try:
    user = fetch_user(42)
except CircuitOpenError:
    # downstream is failing; call was not attempted
    user = None
```

## Why

When a downstream service fails, hammering it with retries slows recovery and
wastes resources. A circuit breaker stops calling after enough consecutive
failures, then periodically probes with a single request to see if the service
has recovered.

The trade-off here is simplicity over feature richness. There is no per-host
bucketing, no sliding window of partial failures, no async support, and no
fallback function. If you need those, use a larger library. This one is small
enough to read in full and audit.

## Behaviour

States: `CLOSED` → `OPEN` → `HALF_OPEN` → `CLOSED` (or back to `OPEN`).

- In `CLOSED`, calls pass through. Failures increment a counter. A single
  success resets the counter to zero.
- At `failure_threshold` consecutive failures, the breaker opens.
- In `OPEN`, calls are rejected with `CircuitOpenError` without invoking the
  wrapped function.
- After `recovery_timeout` seconds, the breaker reports `HALF_OPEN` and allows
  one probe call through. If the probe succeeds, the breaker closes. If it
  fails, the breaker re-opens and the timeout starts again.
- With `max_half_open_probes > 1`, that many consecutive successful probes are
  required to close. A single failed probe at any point re-opens immediately.
- Exceptions in `expected_exceptions` are propagated to the caller but do not
  count as failures.

## The awkward edge

While a probe is in-flight, any additional call is rejected with
`CircuitOpenError` — even though the breaker is in `HALF_OPEN`. This is
deliberate: running multiple probes at once defeats the purpose of probing
gently. If you need parallel probes, this library is not the right tool.

The `state` property may report `HALF_OPEN` before any call is made, because
the timeout has elapsed. The internal state only transitions on the next
actual call. Observing `HALF_OPEN` does not consume the probe.

## Time

The breaker takes a `clock` callable (default `time.monotonic`). Inject a fake
in tests so behaviour is deterministic and never depends on wall-clock time.

## Design notes

The window stores values eagerly rather than keeping running aggregates. Running
sums drift with floating point over long streams, and recomputing from a small
buffer is cheap enough that the drift is not worth the speed.

