Wait, poll, predicate
=====================

This page is the test-author's polling toolkit: how to block until hardware
reaches a state, how to express that state as a value comparison or a
predicate, how to sample a register many times for statistical checks, and
what happens when the wait times out. Every register and field exposes the
same small surface; the goal is that the most common test idiom — "wait until
this bit is set" — is one method call, not a hand-rolled loop.

Why this matters
----------------

From the design sketch:

   ``wait_for`` is the single most common test idiom; it deserves dedicated
   ergonomics.

A naive polling loop in user code is easy to get subtly wrong: missing a
timeout, never sleeping, never logging the last value seen, never
back-pressuring a busy bus. The methods on this page handle all of that
uniformly, so test code reads as a statement of intent ("wait for tx_ready")
and not as bus plumbing.

The contract:

- **Default-bounded.** Every wait takes a ``timeout=`` and raises a
  descriptive error if it expires.
- **Returns the value seen.** A successful wait returns the matching read so
  callers don't immediately read again.
- **Sampling is built in.** ``read(n=...)`` and ``histogram(n=...)`` cover
  debouncing and glitch-detection without a hand-rolled loop.
- **Sync first, async parallel.** Every wait has an ``await_for``/``aio*``
  dual usable from ``asyncio`` code.

``wait_for`` on a single field
------------------------------

The shortest path: wait until a field equals a value.

.. code-block:: python

   # Block until tx_ready becomes True, or raise after 1.0 s.
   soc.uart.status.tx_ready.wait_for(True, timeout=1.0)

   # Tune the polling cadence and add jitter to avoid lock-step
   # interference with hardware events.
   soc.uart.status.tx_ready.wait_for(
       True,
       timeout=1.0,
       period=0.001,
       jitter=True,
   )

Arguments:

- ``value`` (positional): the target. For a 1-bit field, ``True`` / ``False``.
  For an enum field, the enum member (``BaudRate.BAUD_115200``) or its int
  value. Type-checked against the field's encoding.
- ``timeout`` (seconds): hard upper bound on the wait. Required in practice;
  the implementation raises if you call ``wait_for`` with no timeout to keep
  test runs from hanging.
- ``period`` (seconds, default master-dependent): nominal time between polls.
  Smaller is more responsive, larger is gentler on the bus.
- ``jitter`` (bool, default ``False``): perturb each sleep by a small random
  factor. Useful when the polled signal is itself periodic and a fixed period
  could alias.

On success, ``wait_for`` returns the matching ``FieldValue`` (the value the
field had on the read that satisfied the comparison). On timeout, it raises
``WaitTimeoutError`` (see below).

``wait_until`` on a register predicate
--------------------------------------

When the wait condition involves more than one field of the same register —
"tx_ready set **and** error clear" — use ``wait_until`` on the register, not
``wait_for`` on a field. The predicate receives a fresh ``RegisterValue`` per
poll, so it sees a coherent snapshot for that one bus read:

.. code-block:: python

   # Each poll is one bus read; the predicate sees a coherent register value.
   soc.uart.status.wait_until(
       lambda v: v.tx_ready and not v.error,
       timeout=1.0,
   )

The predicate's argument is a ``RegisterValue``; field accessors
(``v.tx_ready``, ``v.error``) return the same typed values you'd get from
``soc.uart.status.read()``. Returning truthy ends the wait; returning falsy
re-polls.

``wait_until`` accepts the same ``timeout``, ``period``, and ``jitter``
arguments as ``wait_for``. On success it returns the ``RegisterValue`` that
satisfied the predicate.

Use ``wait_for`` when the condition is "field equals value"; use
``wait_until`` when the condition is anything more complex, even if it's
still over a single field. ``wait_until`` is also the right tool for
inequality checks (``v.count > 16``) and for waits that depend on
external state (``lambda v: v.tx_ready and feature_enabled``).

IRQ shortcut
------------

For interrupt sources, the polling pattern is so common that the
:doc:`interrupts` group exposes its own ``wait``:

.. code-block:: python

   # Block until tx_done is pending, or raise after 1.0 s.
   soc.uart.interrupts.tx_done.wait(timeout=1.0)

This is the same as
``soc.uart.status.tx_done_int.wait_for(True, timeout=1.0)`` for chips where
the interrupt state register is the natural place to wait, but it reads
better and respects the per-RDL clear/acknowledge semantics. See
:doc:`interrupts` for the full interrupt surface (``enable``, ``clear``,
``fire``, group operations).

Sampling
--------

Two patterns come up so often that they live next to ``wait_for``: capturing
N reads of the same register, and bucketing them into a histogram.

.. code-block:: python

   # 100 fresh bus reads of soc.adc.sample, returned as a NumPy array.
   samples = soc.adc.sample.read(n=100)
   samples.shape          # (100,)
   samples.mean()

   # 1000 reads, bucketed by value.
   from collections import Counter
   hist = soc.adc.sample.histogram(n=1000)
   hist.most_common(5)

A note on shape polymorphism: ``soc.adc.sample.read()`` (no ``n``) returns a
single decoded value, the same shape as any other field read. Passing
``n=...`` switches to a NumPy array of length ``n``. The cutover is the
keyword argument, not the operation.

``read(n=...)`` is also valid on register and memory views; the array's dtype
matches the register width or the memory's word width.

Sampling is intentionally separate from ``wait_for``. Use ``wait_for`` to
synchronize ("I want a known state before I read"); use ``read(n=...)`` to
characterize ("I want N samples of whatever the hardware is doing").

Async equivalents
-----------------

Every blocking wait has a coroutine dual. The methods are renamed with an
``await_`` or ``aio`` prefix so the call site reads correctly under
``await``:

.. code-block:: python

   # asyncio test
   await soc.uart.status.tx_ready.await_for(True, timeout=1.0)
   await soc.uart.status.await_until(
       lambda v: v.tx_ready and not v.error,
       timeout=1.0,
   )
   await soc.uart.interrupts.tx_done.aiowait(timeout=1.0)

The argument shapes match their sync counterparts. Returns and exceptions are
the same. Use the async forms whenever you're already inside an ``async def``
test or a notebook running an event loop, so a slow hardware response doesn't
block the loop.

The complementary surface (``aread`` / ``awrite`` / ``amodify`` on every
node) is described in the bus & masters documentation; everything on this
page about timeouts, predicates, and sampling applies identically there.

Timeout error
-------------

On timeout, every wait raises ``WaitTimeoutError`` with a descriptive message
that names the wait target, the expected condition, the last value seen, and
how long the wait ran:

.. code-block:: text

   WaitTimeoutError: soc.uart.status.tx_ready did not reach True within 1.000s
     last value seen : False
     expected        : True
     polled          : 1024 reads, period=0.001s

For predicate waits, the message includes a short repr of the failing
``RegisterValue`` so post-mortem doesn't require a re-run:

.. code-block:: text

   WaitTimeoutError: soc.uart.status predicate not satisfied within 1.000s
     last value seen : UartStatus(0x00000004)  tx_ready=0 error=1
     polled          : 1024 reads, period=0.001s

For deeper post-mortem, every wait accepts a ``capture=True`` flag that
attaches the full list of sampled ``RegisterValue`` (or ``FieldValue``)
objects to the exception under ``.samples``:

.. code-block:: python

   try:
       soc.uart.status.wait_until(
           lambda v: v.tx_ready and not v.error,
           timeout=1.0,
           capture=True,
       )
   except WaitTimeoutError as e:
       # Replay the trace for the bug report.
       for v in e.samples:
           print(v.hex(), v.error, v.tx_ready)

``capture`` is opt-in because the sample list can grow long for tight polling
loops; the default error message is descriptive enough for most failures.

See also
--------

- :doc:`interrupts` — interrupt sources, group operations, and the
  ``wait``/``aiowait`` shortcuts.
- :doc:`observers` — read/write hooks, including how to capture sampled
  values from a wait without ``capture=True`` (an observer sees every read
  the wait performs, with no opt-in needed).
