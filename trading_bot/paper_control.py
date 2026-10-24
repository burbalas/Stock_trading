"""A user-wide submitting lock shared by terminal and desktop entry points."""
from .journal import TradeJournal
from .runtime_paths import runtime_home


def paper_execution_lock():
    # One submitting paper operator per Windows user, including different journals.
    return TradeJournal(runtime_home() / 'locks').lock('paper-execution', timeout=0)
