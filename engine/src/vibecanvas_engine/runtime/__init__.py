"""Memory-owned workflow execution and its host control protocol.

No database or API imports belong here. Host adapters persist the public events
and authenticate control requests; losing this runtime loses its live executions.
"""
