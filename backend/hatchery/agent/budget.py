"""One agent's daily token budget, shared by all of its threads.

Ported from agentmesh `agent/budget.py`. Threads ask the supervisor before every
model call (pre-call admission) and report usage after it returns, so a busy thread
can overshoot by at most one admitted call.
"""

import dataclasses

import rotor

DAY = 86400


@rotor.state
class Budget:
    ceiling: int = 0  # tokens per UTC day, from configuration
    day: int = 0  # UTC day number the counters belong to
    spent: int = 0
    granted: int = 0
    grants: list[str] = dataclasses.field(default_factory=list)  # grant ids already applied

    @property
    def limit(self) -> int:
        return self.ceiling + self.granted

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.spent)

    @property
    def exhausted(self) -> bool:
        return self.spent >= self.limit

    def next_day_at(self) -> float:
        return float((self.day + 1) * DAY)

    def roll(self, now: float) -> bool:
        """Move to the current UTC day, resetting counters. True when the day changed."""
        day = int(now // DAY)
        if day == self.day:
            return False
        self.day, self.spent, self.granted = day, 0, 0
        self.grants.clear()
        return True

    def spend(self, tokens: int) -> None:
        self.spent += max(0, tokens)

    def grant(self, grant_id: str, amount: int) -> bool:
        """Apply a grant once; a repeated id is ignored."""
        if not grant_id or grant_id in self.grants or amount <= 0:
            return False
        self.grants.append(grant_id)
        self.granted += amount
        return True

    def view(self) -> dict[str, object]:
        return {
            "day": self.day,
            "spent": self.spent,
            "granted": self.granted,
            "limit": self.limit,
            "remaining": self.remaining,
            "exhausted": self.exhausted,
            "resets_at": self.next_day_at(),
        }
