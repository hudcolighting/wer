"""Qt application chrome.

Everything in this package runs on the main thread and may touch widgets.
Nothing outside it may. Workers communicate inward via Qt signals only.
"""
