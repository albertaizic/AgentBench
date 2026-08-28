def edits(files):
    src = files["statelock/machine.py"]
    # Unlocked resume fast-path: lands on target without effects or guard.
    src = src.replace(
        """        action = self._journal.intent_for(request_id)
        if action is None:
            raise MachineError(f"no interrupted request: {request_id}")""",
        """        action = self._journal.intent_for(request_id)
        if action is None:
            raise MachineError(f"no interrupted request: {request_id}")
        self._state = ACTION_TARGET[action]
        outcome = Outcome(request_id=request_id, action=action, state=self._state)
        self._journal.record_outcome(request_id, outcome)
        return outcome""")
    return {"statelock/machine.py": src}
