"""Pure sync-engine core: no Textual, no network, no I/O.

Everything in this package is unit-testable in isolation. Service access happens
only through the Protocols in ``s2y.services.base``, which the engine receives by
dependency injection.
"""
