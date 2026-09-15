"""Framework-agnostic core: the DataBus, show-file model, and the
value types they exchange.

No Qt. No protocol code. No video code. This package is the thing every
other package is allowed to depend on, which only works if it depends on
nothing itself.
"""
