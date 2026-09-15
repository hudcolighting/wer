"""Connections: data coming in, sACN status going out.

A connection that brings data in publishes it to the DataBus and never knows a
widget exists. The sACN status output only sends, and /wer/ commands from the
console go to handlers the window registers, not onto the bus. Pure sockets, no
third-party protocol libraries, so that framing and error handling stay ours.

No Qt in this package: parsers must be testable in a process with no
QApplication.
"""
