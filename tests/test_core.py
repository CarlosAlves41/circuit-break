import unittest

from circuit_break import CircuitBreaker, CircuitState, CircuitOpenError


class FakeClock:
    def __init__(self, start=0.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class TestClosedState(unittest.TestCase):
    def test_passes_through_on_success(self):
        cb = CircuitBreaker(failure_threshold=3, clock=FakeClock())
        self.assertEqual(cb.call(lambda: 42), 42)
        self.assertEqual(cb.state, CircuitState.CLOSED)

    def test_counts_failures_but_stays_closed_below_threshold(self):
        cb = CircuitBreaker(failure_threshold=3, clock=FakeClock())
        for _ in range(2):
            with self.assertRaises(ValueError):
                cb.call(lambda: (_ for _ in ()).throw(ValueError("boom")))
        self.assertEqual(cb.state, CircuitState.CLOSED)

    def test_opens_at_threshold(self):
        cb = CircuitBreaker(failure_threshold=3, clock=FakeClock())
        for _ in range(3):
            with self.assertRaises(ValueError):
                cb.call(lambda: (_ for _ in ()).throw(ValueError("boom")))
        self.assertEqual(cb.state, CircuitState.OPEN)

    def test_success_resets_failure_count(self):
        cb = CircuitBreaker(failure_threshold=3, clock=FakeClock())
        for _ in range(2):
            with self.assertRaises(ValueError):
                cb.call(lambda: (_ for _ in ()).throw(ValueError("boom")))
        cb.call(lambda: "ok")
        # Two more failures should not open it, because count was reset.
        for _ in range(2):
            with self.assertRaises(ValueError):
                cb.call(lambda: (_ for _ in ()).throw(ValueError("boom")))
        self.assertEqual(cb.state, CircuitState.CLOSED)


class TestExpectedExceptions(unittest.TestCase):
    def test_expected_exception_does_not_count_as_failure(self):
        cb = CircuitBreaker(
            failure_threshold=2,
            expected_exceptions=(KeyError,),
            clock=FakeClock(),
        )
        for _ in range(5):
            with self.assertRaises(KeyError):
                cb.call(lambda: (_ for _ in ()).throw(KeyError("nope")))
        self.assertEqual(cb.state, CircuitState.CLOSED)

    def test_expected_exception_is_still_raised(self):
        cb = CircuitBreaker(
            failure_threshold=2,
            expected_exceptions=(KeyError,),
            clock=FakeClock(),
        )
        with self.assertRaises(KeyError):
            cb.call(lambda: (_ for _ in ()).throw(KeyError("nope")))


class TestOpenState(unittest.TestCase):
    def test_rejects_without_calling_func(self):
        cb = CircuitBreaker(failure_threshold=1, clock=FakeClock())
        calls = []
        with self.assertRaises(ValueError):
            cb.call(lambda: (_ for _ in ()).throw(ValueError("boom")))
        with self.assertRaises(CircuitOpenError):
            cb.call(lambda: calls.append(1))
        self.assertEqual(calls, [])

    def test_state_shows_half_open_after_timeout_without_mutating(self):
        clock = FakeClock()
        cb = CircuitBreaker(
            failure_threshold=1, recovery_timeout=10.0, clock=clock
        )
        with self.assertRaises(ValueError):
            cb.call(lambda: (_ for _ in ()).throw(ValueError("boom")))
        self.assertEqual(cb.state, CircuitState.OPEN)
        clock.advance(10.0)
        # state property should reflect readiness without changing internals
        self.assertEqual(cb.state, CircuitState.HALF_OPEN)


class TestHalfOpen(unittest.TestCase):
    def test_successful_probe_closes_circuit(self):
        clock = FakeClock()
        cb = CircuitBreaker(
            failure_threshold=1, recovery_timeout=10.0, clock=clock
        )
        with self.assertRaises(ValueError):
            cb.call(lambda: (_ for _ in ()).throw(ValueError("boom")))
        clock.advance(10.0)
        # This call is the probe.
        self.assertEqual(cb.call(lambda: "ok"), "ok")
        self.assertEqual(cb.state, CircuitState.CLOSED)

    def test_failed_probe_reopens(self):
        clock = FakeClock()
        cb = CircuitBreaker(
            failure_threshold=1, recovery_timeout=10.0, clock=clock
        )
        with self.assertRaises(ValueError):
            cb.call(lambda: (_ for _ in ()).throw(ValueError("boom")))
        clock.advance(10.0)
        with self.assertRaises(ValueError):
            cb.call(lambda: (_ for _ in ()).throw(ValueError("still broken")))
        self.assertEqual(cb.state, CircuitState.OPEN)

    def test_concurrent_call_during_probe_is_rejected(self):
        clock = FakeClock()
        cb = CircuitBreaker(
            failure_threshold=1, recovery_timeout=10.0, clock=clock
        )
        with self.assertRaises(ValueError):
            cb.call(lambda: (_ for _ in ()).throw(ValueError("boom")))
        clock.advance(10.0)

        accepted = []

        def first_probe():
            accepted.append("ran")
            return "ok"

        # Simulate a probe in flight by calling _before_call manually,
        # which sets _probe_in_flight = True.
        self.assertTrue(cb._before_call())
        # A second call while the probe is in flight should be rejected.
        with self.assertRaises(CircuitOpenError):
            cb.call(lambda: accepted.append("should not run"))
        self.assertEqual(accepted, [])

    def test_multiple_probes_required_to_close(self):
        clock = FakeClock()
        cb = CircuitBreaker(
            failure_threshold=1,
            recovery_timeout=10.0,
            max_half_open_probes=3,
            clock=clock,
        )
        with self.assertRaises(ValueError):
            cb.call(lambda: (_ for _ in ()).throw(ValueError("boom")))
        clock.advance(10.0)

        self.assertEqual(cb.call(lambda: "ok1"), "ok1")
        self.assertEqual(cb.state, CircuitState.HALF_OPEN)
        self.assertEqual(cb.call(lambda: "ok2"), "ok2")
        self.assertEqual(cb.state, CircuitState.HALF_OPEN)
        self.assertEqual(cb.call(lambda: "ok3"), "ok3")
        self.assertEqual(cb.state, CircuitState.CLOSED)

    def test_failure_during_multi_probe_reopens_immediately(self):
        clock = FakeClock()
        cb = CircuitBreaker(
            failure_threshold=1,
            recovery_timeout=10.0,
            max_half_open_probes=3,
            clock=clock,
        )
        with self.assertRaises(ValueError):
            cb.call(lambda: (_ for _ in ()).throw(ValueError("boom")))
        clock.advance(10.0)

        self.assertEqual(cb.call(lambda: "ok1"), "ok1")
        with self.assertRaises(ValueError):
            cb.call(lambda: (_ for _ in ()).throw(ValueError("nope")))
        self.assertEqual(cb.state, CircuitState.OPEN)


class TestDecorator(unittest.TestCase):
    def test_decorator_wraps_function(self):
        cb = CircuitBreaker(failure_threshold=2, clock=FakeClock())

        @cb
        def add(a, b):
            return a + b

        self.assertEqual(add(2, 3), 5)
        self.assertEqual(add(10, 20), 30)
        self.assertEqual(cb.state, CircuitState.CLOSED)

    def test_decorator_propagates_failures(self):
        cb = CircuitBreaker(failure_threshold=2, clock=FakeClock())

        @cb
        def fail():
            raise RuntimeError("nope")

        with self.assertRaises(RuntimeError):
            fail()
        with self.assertRaises(RuntimeError):
            fail()
        self.assertEqual(cb.state, CircuitState.OPEN)


class TestValidation(unittest.TestCase):
    def test_rejects_zero_failure_threshold(self):
        with self.assertRaises(ValueError):
            CircuitBreaker(failure_threshold=0, clock=FakeClock())

    def test_rejects_negative_recovery_timeout(self):
        with self.assertRaises(ValueError):
            CircuitBreaker(recovery_timeout=-1, clock=FakeClock())

    def test_rejects_zero_max_half_open_probes(self):
        with self.assertRaises(ValueError):
            CircuitBreaker(max_half_open_probes=0, clock=FakeClock())


class TestRecoveryCycle(unittest.TestCase):
    def test_full_cycle_close_open_half_open_close(self):
        clock = FakeClock()
        cb = CircuitBreaker(
            failure_threshold=2, recovery_timeout=5.0, clock=clock
        )

        # Close -> Open
        for _ in range(2):
            with self.assertRaises(ValueError):
                cb.call(lambda: (_ for _ in ()).throw(ValueError("boom")))
        self.assertEqual(cb.state, CircuitState.OPEN)

        # Open -> HalfOpen -> Open (probe fails)
        clock.advance(5.0)
        with self.assertRaises(ValueError):
            cb.call(lambda: (_ for _ in ()).throw(ValueError("still bad")))
        self.assertEqual(cb.state, CircuitState.OPEN)

        # Open -> HalfOpen -> Close (probe succeeds)
        clock.advance(5.0)
        self.assertEqual(cb.call(lambda: "recovered"), "recovered")
        self.assertEqual(cb.state, CircuitState.CLOSED)

        # Confirm it stays closed under load
        for _ in range(10):
            cb.call(lambda: "ok")
        self.assertEqual(cb.state, CircuitState.CLOSED)


if __name__ == "__main__":
    unittest.main()
