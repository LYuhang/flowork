"""Process-scoped services such as the object store and workflow runner.

These are infrastructure helpers used by both routes and background task
bodies. Anything that is not a Postgres repo or a route handler lives
here.
"""
