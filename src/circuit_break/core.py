"""Circuit breaker with half-open probing.

The breaker has three states:

  CLOSED  — calls pass through. Failures are counted.
  OPEN    — calls are rejected immediately with CircuitOpenError.
  HALF_OPEN — a limited number of probe calls are allowed through. If they
              succeed, the breaker closes. If any fails, it re-opens.

The transition from OPEN to HALF_OPEN happens lazily: the first call after
`recovery_timeout` seconds have elapsed (as reported by the injected `clock`)
triggers the move to HALF_OPEN and itself becomes the first probe.

Design choices:

  * `clock` is a callable returning a float (seconds). Defaulting to
    time.monotonic keeps production use simple; tests inject a fake so they
    never depend on wall-clock time.

  * `max_half_open_probes` is the number of successful probes required to
    close the breaker. A failed probe re-opens immediately. If a second call
    arrives while a probe is in-flight, it is rejected — we never run more
    than one probe at a time. This keeps the half-open window predictable.

  * Success resets the failure count to zero. This is the standard
    "recent failures" interpretation: a single success after a long string
    of failures means the downstream service recovered.

  * Exceptions listed in `expected_exceptions` are treated as success — they
    are propagated to the caller but do not count as failures. Everything
    else is a failure.
"""

import time
import functools
from enum import Enum


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """Raised when a call is rejected because the circuit is open.

    The original callable was never invoked.
    """


class CircuitBreaker:
    def __init__(
        self,
        failure_threshold=5,
        recovery_timeout=30.0,
        max_half_open_probes=1,
        expected_exceptions=(),
        clock=time.monotonic,
    ):
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if recovery_timeout < 0:
            raise ValueError("recovery_timeout must be >= 0")
        if max_half_open_probes < 1:
            raise ValueError("max_half_open_probes must be >= 1")

        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.max_half_open_probes = max_half_open_probes
        self.expected_exceptions = tuple(expected_exceptions)
        self._clock = clock

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._opened_at = None
        self._half_open_successes = 0
        self._probe_in_flight = False

    @property
    def state(self):
        """Current logical state.

        If the breaker is OPEN but enough time has elapsed, this returns
        HALF_OPEN without mutating internal state — the actual transition
        happens on the next call. This lets observers see "the breaker is
        ready to probe" without triggering a probe.
        """
        if self._state is CircuitState.OPEN and self._has_timed_out():
            return CircuitState.HALF_OPEN
        return self._state

    def _has_timed_out(self):
        return self._clock() - self._opened_at >= self.recovery_timeout

    def _before_call(self):
        """Decide whether this call may proceed and update state.

        Returns True if the call should run, False if it should be rejected.
        """
        if self._state is CircuitState.CLOSED:
            return True

        if self._state is CircuitState.OPEN:
            if self._has_timed_out():
                self._state = CircuitState.HALF_OPEN
                self._half_open_successes = 0
                self._probe_in_flight = True
                return True
            return False

        # HALF_OPEN
        if self._probe_in_flight:
            return False
        self._probe_in_flight = True
        return True

    def _on_success(self):
        if self._state is CircuitState.HALF_OPEN:
            self._half_open_successes += 1
            self._probe_in_flight = False
            if self._half_open_successes >= self.max_half_open_probes:
                self._state = CircuitState.CLOSED
                self._failure_count = 0
        else:
            self._failure_count = 0

    def _on_failure(self):
        if self._state is CircuitState.HALF_OPEN:
            self._state = CircuitState.OPEN
            self._opened_at = self._clock()
            self._half_open_successes = 0
            self._probe_in_flight = False
        else:
            self._failure_count += 1
            if self._failure_count >= self.failure_threshold:
                self._state = CircuitState.OPEN
                self._opened_at = self._clock()

    def call(self, func, *args, **kwargs):
        if not self._before_call():
            raise CircuitOpenError("circuit is open")
        try:
            result = func(*args, **kwargs)
        except self.expected_exceptions:
            self._on_success()
            raise
        except Exception:
            self._on_failure()
            raise
        self._on_success()
        return result

    def __call__(self, func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            return self.call(func, *args, **kwargs)
        return wrapper
