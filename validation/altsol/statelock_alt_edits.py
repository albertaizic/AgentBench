"""Alternative statelock solution: resume replays the recorded intent through
the normal guarded pipeline by re-entering process() from the intermediate
state (evidence gate before effects, exactly like a fresh run), rather than
duplicating pipeline logic inside retry()."""

def edits(files):
    machine = files["statelock/machine.py"]
    machine = machine.replace(
        '''    def retry(self, request_id: str) -> Outcome:
        replayed = self._journal.outcome_for(request_id)
        if replayed is not None:
            return replayed
        action = self._journal.intent_for(request_id)
        if action is None:
            raise MachineError(f"no interrupted request: {request_id}")
        # Unlocked fast-path: skip the pipeline entirely and land on the
        # target state, running neither its side effect nor its guard.
        self._state = ACTION_TARGET[action]
        outcome = Outcome(request_id=request_id, action=action,
                          state=self._state)
        self._journal.record_outcome(request_id, outcome)
        return outcome''',
        '''    def retry(self, request_id: str) -> Outcome:
        replayed = self._journal.outcome_for(request_id)
        if replayed is not None:
            return replayed
        action = self._journal.intent_for(request_id)
        if action is None:
            raise MachineError(f"no interrupted request: {request_id}")
        # Re-enter the ordinary pipeline from the intermediate state: the
        # effect journal is the only source of truth for what already ran,
        # and the guard sees the same evidence a fresh run would.
        return self._drive(action, request_id)''')
    guards = files["statelock/guards.py"]
    guards = guards.replace(
        """from __future__ import annotations""",
        """from __future__ import annotations

from statelock.effects import EffectJournal""", 1)
    guards = guards.replace(
        """    # BUG: consults only the visible state label, never the effect journal,
    # so a machine whose intermediate side effects were skipped is waved
    # straight through to the terminal state.
    def check(self, target_state: str, current_state: str) -> None:
        allowed = ENTRY_STATES.get(target_state, ())
        if current_state not in allowed:
            raise GuardViolation(
                f"cannot enter {target_state} from {current_state}"
            )""",
        """    def check(self, target_state: str, journal: EffectJournal) -> None:
        \"\"\"Refuse entry unless every required effect has evidence.\"\"\"
        missing = [effect for effect in self.required_for(target_state)
                   if not journal.has_effect(effect)]
        if missing:
            raise GuardViolation(
                f"missing side-effect evidence for {target_state}: "
                f"{', '.join(missing)}"
            )""")
    out = {"statelock/guards.py": guards}
    # machine.py keeps its original drive-order (guard BEFORE intermediate
    # bookkeeping) via this structural edit:
    machine = machine.replace(
        """        if self._state != ACTION_START[action]:
            raise MachineError(f"cannot {action} from {self._state}")
        self._state = ACTION_INTERMEDIATE[action]
        self._guard.check(ACTION_TARGET[action], self._state)""",
        """        if self._state not in (ACTION_START[action], ACTION_INTERMEDIATE[action]):
            raise MachineError(f"cannot {action} from {self._state}")
        self._guard.check(ACTION_TARGET[action], self._journal)
        self._state = ACTION_INTERMEDIATE[action]""")
    out["statelock/machine.py"] = machine
    return out
