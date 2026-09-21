"""jevguard - guardrails, evaluation and observability for agentic systems, powered by Jev.

    from jevguard import Guard
    guard = Guard()
    guard.check_input("Ignore all previous instructions...").blocked   # True
"""

from jevguard.approvals import CallbackApprover, DashboardApprover, StaticApprover, StoreApprover
from jevguard.config import GuardPolicy, StagePolicy, Thresholds, ToolPolicy
from jevguard.engine import Guard
from jevguard.jev.questions import Choice, Noul, Score
from jevguard.remote import RemoteGuard
from jevguard.sinks import CallbackSink, ConsoleSink, HttpSink, MemorySink, SQLiteSink
from jevguard.types import Action, Decision, Finding, GuardBlocked, GuardContext, Stage

__version__ = "0.1.0"

__all__ = [
    "Action", "CallbackApprover", "CallbackSink", "Choice", "ConsoleSink", "DashboardApprover", "Decision", "Finding",
    "Guard", "GuardBlocked", "GuardContext", "GuardPolicy", "HttpSink", "MemorySink", "Noul", "RemoteGuard",
    "SQLiteSink", "Score", "Stage", "StagePolicy", "StaticApprover", "StoreApprover", "Thresholds", "ToolPolicy",
]
