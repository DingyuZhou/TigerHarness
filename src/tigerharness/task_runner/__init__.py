"""tigerharness.task_runner — fire-and-forget iterative task execution.

Drives one persona through a fixed number of resume-based Claude turns,
with periodic /compact. Runs detached in the background so it survives
session exit / SSH disconnect.
"""
