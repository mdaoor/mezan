"""Job handlers. Importing this package registers them with mizan.core.jobs."""
from mizan.workers import ingest, infer, load, report  # noqa: F401
