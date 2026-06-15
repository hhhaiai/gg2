"""Side-car latency probe worker.

Runs in its own process; cycles through accounts and records ``last_latency_ms``
+ ``last_probe_at`` to the shared repository.  The API server reads these
columns via its periodic sync loop to drive the ``fast`` selector strategy.
"""
