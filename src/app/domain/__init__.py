"""The pure domain layer: decision logic only, no I/O.

Nothing in this package may import `sqlalchemy`, `httpx`, `google.*`, or any other I/O
library, and nothing may read configuration or the system clock. Time and zone arrive as
arguments. This is what makes the reminder math, the due-time rule, and the planner
unit-testable without a database or a network.
"""
