"""eos.connected must reflect whether data is arriving, not whether a socket
is open.

The failure this guards is deliberately the quiet one. A console that dies is
loud -- there is a board operator sitting at it who knows within seconds. A
network blip while the desk is perfectly fine is silent: the operator keeps
calling cues, nobody has any reason to look at Wer, and the recording quietly
claims the last cue it heard for the rest of the act.
"""

from __future__ import annotations

import time

from wer.connections.base import ConnectionState
from wer.connections.eos import EosConnection, EosSettings
from wer.core.databus import DataBus


def _live_connection(stale_after: float = 0.2):
    bus = DataBus()
    connection = EosConnection("eos", bus, EosSettings())
    connection.stale_after = stale_after
    connection._set_state(ConnectionState.LIVE)
    connection.status.note_packet(1)
    connection.publish("eos.connected", True)
    return connection, bus


def test_connected_goes_false_when_the_desk_stops_talking() -> None:
    connection, bus = _live_connection()
    assert bus.value("eos.connected") is True

    time.sleep(0.35)
    connection.refresh_staleness()

    assert connection.status.state is ConnectionState.STALE
    assert bus.value("eos.connected") is False


def test_connected_comes_back_when_the_desk_does() -> None:
    connection, bus = _live_connection()
    time.sleep(0.35)
    connection.refresh_staleness()
    assert bus.value("eos.connected") is False

    connection.status.note_packet(1)
    connection.refresh_staleness()

    assert connection.status.state is ConnectionState.LIVE
    assert bus.value("eos.connected") is True


def test_a_healthy_connection_is_left_alone() -> None:
    """refresh_staleness runs twice a second from the UI. It must not churn
    the bus, or every subscriber repaints for nothing."""
    connection, bus = _live_connection(stale_after=30.0)
    changes = []
    bus.subscribe("eos.connected", lambda entry: changes.append(entry.value))

    for _ in range(10):
        connection.refresh_staleness()

    assert connection.status.state is ConnectionState.LIVE
    assert changes == [], "published on a connection that had not changed state"


def test_nothing_is_published_before_a_connection_exists() -> None:
    """A connection that was never live has nothing to say about staleness."""
    bus = DataBus()
    connection = EosConnection("eos", bus, EosSettings())
    connection.refresh_staleness()
    assert bus.value("eos.connected") is None
