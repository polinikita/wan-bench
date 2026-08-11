"""Record coarse benchmark phase timings."""

from __future__ import annotations

import contextlib
import time


def since(start: float) -> int:
    """Whole seconds elapsed since a `time.monotonic()` reading."""
    return round(time.monotonic() - start)


class StepLog:
    """Record and print named phase durations."""

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.steps: list[tuple[str, float]] = []

    @contextlib.contextmanager
    def step(self, name: str):
        start = time.monotonic()
        try:
            yield
        finally:
            seconds = time.monotonic() - start
            self.steps.append((name, seconds))
            print(f"{self.prefix}: {name} took {round(seconds)}s")

    def total(self) -> float:
        return sum(seconds for _, seconds in self.steps)

    def summary(self) -> str:
        """Return an aligned phase summary."""
        total = self.total()
        header = f"{self.prefix}: timeline (total {round(total)}s):"
        if not self.steps:
            return header
        width = max(len(name) for name, _ in self.steps)
        lines = [header]
        for name, seconds in self.steps:
            pct = (seconds / total * 100) if total > 0 else 0.0
            lines.append(f"  {name:<{width}}  {round(seconds):>5}s  {pct:5.1f}%")
        return "\n".join(lines)
