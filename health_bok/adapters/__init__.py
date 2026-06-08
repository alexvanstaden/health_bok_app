"""Real adapters for the three ports.

Each module here imports its own third-party SDK. They are imported by the
entrypoint (`health_bok.main`) when running for real — never by the job, the
ports, or the tests, which stay SDK-free.
"""
