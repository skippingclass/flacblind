"""The ABX session state machine.

A session owns the *test logic* only: how many rounds, where the lossless
sample is hidden, what the listener answered, and the running statistics.
It knows nothing about Qt, files or widgets, which makes the whole game loop
unit testable::

    session = ABXSession(TestConfig(rounds=10, seed=7))
    round_ = session.next_round()      # -> ABXRound(lossless_key=SampleKey.B, ...)
    session.vote(SampleKey.B)          # -> RoundRecord(correct=True, ...)
    session.stats()                    # -> SessionStats(p_value=..., ...)
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterator

from .errors import SessionError
from .models import RoundRecord, SampleKey, SessionReport, SessionStats
from .stats import analyze, fmt_p_value, verdict

__all__ = [
    "TestConfig",
    "ABXRound",
    "ABXSession",
    "RoundState",
    "ENDLESS",
]

#: Sentinel for "play until the listener stops".
ENDLESS = 0


class RoundState(str, Enum):
    """Where a round is in its life cycle."""

    PROMPT = "prompt"       # fresh round, nothing played yet
    LISTENING = "listening"  # at least one sample played
    VOTED = "voted"          # vote cast, reveal pending
    REVEALED = "revealed"    # truth shown to the user


@dataclass(frozen=True, slots=True)
class TestConfig:
    """Session level settings.

    ``rounds=0`` selects endless mode; ``seed`` makes the A/B assignment
    reproducible (handy for a fair re-test with a colleague).
    """

    rounds: int = 10
    seed: int | None = None
    reveal_after_vote: bool = True
    #: Refuse votes until both samples have been played at least once.
    require_both_heard: bool = False
    #: Force at least ``min_plays`` playbacks per round before enabling a vote.
    min_plays: int = 0

    @property
    def endless(self) -> bool:
        return self.rounds <= 0

    @property
    def rounds_text(self) -> str:
        return "∞" if self.endless else str(self.rounds)


@dataclass(slots=True)
class ABXRound:
    """A single round, including the hidden answer."""

    index: int
    lossless_key: SampleKey
    state: RoundState = RoundState.PROMPT
    played: set[SampleKey] = field(default_factory=set)
    switches: int = 0
    listen_sec: float = 0.0
    _started: float = field(default_factory=time.monotonic, repr=False)
    _last_switch: float = field(default_factory=time.monotonic, repr=False)

    @property
    def lossy_key(self) -> SampleKey:
        return self.lossless_key.other

    @property
    def number(self) -> str:
        return f"{self.index}"

    def mark_played(self, key: SampleKey) -> None:
        """Record a playback of ``key`` (used for the 'did you listen?' rules)."""
        if key not in self.played:
            self.switches += 1
        self.played.add(key)
        self._last_switch = time.monotonic()
        if self.state is RoundState.PROMPT:
            self.state = RoundState.LISTENING

    def tick_listen_time(self) -> None:
        if self.state in (RoundState.PROMPT, RoundState.LISTENING):
            self.listen_sec += max(0.0, time.monotonic() - self._last_switch)

    def can_vote(self, cfg: TestConfig) -> tuple[bool, str]:
        """Whether a vote is allowed right now, plus the reason if not."""
        if self.state in (RoundState.VOTED, RoundState.REVEALED):
            return False, "already-voted"
        if cfg.require_both_heard and len(self.played) < 2:
            return False, "need-both"
        if cfg.min_plays > 0 and self.switches < cfg.min_plays:
            return False, "need-plays"
        return True, ""

    def copy(self) -> "ABXRound":
        return ABXRound(
            index=self.index,
            lossless_key=self.lossless_key,
            state=self.state,
            played=set(self.played),
            switches=self.switches,
            listen_sec=self.listen_sec,
        )


class ABXSession:
    """Drives the test: rounds, votes, statistics."""

    def __init__(self, config: TestConfig | None = None, **kwargs: object) -> None:
        self.config = config or TestConfig(**kwargs)  # type: ignore[arg-type]
        self._rng = random.Random(self.config.seed)
        self._records: list[RoundRecord] = []
        self._round: ABXRound | None = None
        self._finished = False

    # ------------------------------------------------------------------ state
    @property
    def records(self) -> tuple[RoundRecord, ...]:
        return tuple(self._records)

    @property
    def round_number(self) -> int:
        return len(self._records) + 1

    @property
    def current(self) -> ABXRound | None:
        return self._round

    @property
    def finished(self) -> bool:
        if self.config.endless:
            return self._finished
        return len(self._records) >= self.config.rounds

    @property
    def total_rounds(self) -> int | None:
        return None if self.config.endless else self.config.rounds

    def start(self) -> ABXRound:
        """Begin the first round (or restart after a reset)."""
        if self._round is None:
            self._round = self._new_round()
        return self._round

    def restart(self, config: TestConfig | None = None) -> ABXRound:
        if config is not None:
            self.config = config
            self._rng = random.Random(config.seed)
        self._records.clear()
        self._round = None
        self._finished = False
        return self.start()

    def _new_round(self) -> ABXRound:
        """Draw the hidden answer for the next round.

        A pure coin flip, except that a third identical assignment in a row is
        flipped: long runs of "A is always the lossless one" make an otherwise
        fair test look rigged to the listener.
        """
        index = self.round_number
        candidate = self._rng.choice((SampleKey.A, SampleKey.B))
        if len(self._records) >= 2:
            last, before = self._records[-1].lossless_key, self._records[-2].lossless_key
            if last is before and candidate is last:
                candidate = candidate.other
        return ABXRound(index=index, lossless_key=candidate)

    # ------------------------------------------------------------- round flow
    def mark_played(self, key: SampleKey) -> None:
        if self._round is None:
            self.start()
        assert self._round is not None
        self._round.mark_played(key)

    def note_switch(self) -> None:
        if self._round is not None:
            self._round.switches += 1

    def can_vote(self) -> tuple[bool, str]:
        if self._round is None:
            return False, "no-round"
        return self._round.can_vote(self.config)

    def vote(self, guess: SampleKey) -> RoundRecord:
        """Cast a vote and reveal the answer of the current round."""
        if self._round is None:
            raise SessionError("There is no active round to vote on.")
        allowed, reason = self._round.can_vote(self.config)
        if not allowed and reason not in ("already-voted",):
            raise SessionError(_vote_error(reason))
        guess = SampleKey(guess)
        if self._round.state in (RoundState.VOTED, RoundState.REVEALED):
            raise SessionError("This round has already been answered.")
        self._round.tick_listen_time()
        record = RoundRecord(
            index=self._round.index,
            guess=guess,
            correct=guess is self._round.lossless_key,
            lossless_key=self._round.lossless_key,
            played_sec=round(self._round.listen_sec, 2),
            switched=self._round.switches > 1,
        )
        self._records.append(record)
        self._round.state = RoundState.REVEALED if self.config.reveal_after_vote else RoundState.VOTED
        if not self.config.endless and len(self._records) >= self.config.rounds:
            self._finished = True
        return record

    def reveal(self) -> ABXRound | None:
        """Mark the current round as revealed without casting a vote."""
        if self._round is not None:
            self._round.state = RoundState.REVEALED
        return self._round

    def skip_round(self) -> RoundRecord | None:
        """Abandon the current round and draw a new one (no vote recorded)."""
        if self._round is None or self._round.state in (RoundState.VOTED, RoundState.REVEALED):
            return None
        self._round.state = RoundState.REVEALED
        self._round = self._new_round()
        return None

    def next_round(self) -> ABXRound:
        """Advance to the following round, or raise if the session is over."""
        if self._round is not None and self._round.state in (RoundState.PROMPT, RoundState.LISTENING):
            raise SessionError("Finish the current round before starting a new one.")
        if self.finished:
            raise SessionError("The session is already finished.")
        self._round = self._new_round()
        return self._round

    def end_early(self) -> None:
        """Leave endless mode and freeze the statistics."""
        self._finished = True

    # ------------------------------------------------------------- statistics
    def stats(self) -> SessionStats:
        n = len(self._records)
        k = sum(1 for record in self._records if record.correct)
        result = analyze(n, k)
        return SessionStats(
            rounds=result.rounds,
            correct=result.correct,
            p_value=result.p_value,
            p_value_two_sided=result.p_value_two_sided,
            ci_low=result.ci_low,
            ci_high=result.ci_high,
            needed_for_significance=result.needed_for_significance,
        )

    def verdict(self):
        n = len(self._records)
        k = sum(1 for record in self._records if record.correct)
        return verdict(n, k)

    def report(self, *, segment: str = "", sample_rate: int = 0) -> SessionReport:
        return SessionReport(
            records=self.records,
            stats=self.stats(),
            endless=self.config.endless,
            segment=segment,
            sample_rate=sample_rate,
        )

    def summary_line(self) -> str:
        stats = self.stats()
        if stats.rounds == 0:
            return "No rounds played yet."
        return (
            f"{stats.correct}/{stats.rounds} correct · "
            f"p = {fmt_p_value(stats.p_value)} · {self.verdict().label}"
        )

    def __iter__(self) -> Iterator[RoundRecord]:  # pragma: no cover - convenience
        return iter(self._records)

    def __len__(self) -> int:
        return len(self._records)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ABXSession(rounds={self.config.rounds_text}, played={len(self._records)})"


def _vote_error(reason: str) -> str:
    return {
        "need-both": "Play both samples before voting on this round.",
        "need-plays": "Listen to the samples a few times before voting.",
    }.get(reason, "A vote is not possible right now.")
