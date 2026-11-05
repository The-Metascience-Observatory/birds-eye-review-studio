"""Thread-safe per-query coverage, separate from the crash-resume cache."""
import threading


class QueryLedger:
    def __init__(self):
        self.lock = threading.Lock()
        self.local = threading.local()
        self.results = {}

    def begin(self, key, skipped=False):
        self.local.key = key
        with self.lock:
            self.results[key] = "skipped" if skipped else "incomplete"

    def fail(self, reason):
        key = getattr(self.local, "key", "source")
        with self.lock:
            self.results[key] = "failed: " + str(reason)

    def complete(self, key):
        with self.lock:
            if str(self.results.get(key, "")).startswith("failed:"):
                return False
            self.results[key] = "complete"
            return True

    @property
    def incomplete(self):
        return {key: value for key, value in self.results.items()
                if value not in ("complete", "skipped")}
