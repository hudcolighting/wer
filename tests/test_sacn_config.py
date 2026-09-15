"""The sACN section of the show file.

No Qt.
"""

from __future__ import annotations

import json

import pytest

from wer.core.showfile import SacnConfig, ShowFile, load_show, save_show


def test_a_new_show_is_off_on_universe_101_address_1() -> None:
    """Hudson's defaults, 13 Sep 2026."""
    config = SacnConfig()
    assert (config.enabled, config.universe, config.address) == (False, 101, 1)
    assert (config.priority, config.per_address_priority, config.adapter) == (100, True, "")


def test_the_settings_come_back_as_they_were_saved(tmp_path) -> None:
    show = ShowFile()
    show.sacn = SacnConfig(
        enabled=True, universe=5, address=7, priority=90,
        per_address_priority=False, adapter="{wired}", adapter_name="Ethernet 2",
        source_name="Booth Wer", cid="6f1c2d9e-8a4b-4c3d-9e2f-1a2b3c4d5e6f",
    )
    path = tmp_path / "show.wer"
    save_show(show, path)
    assert load_show(path).sacn == show.sacn


def test_a_show_file_from_before_loads_with_sacn_off(tmp_path) -> None:
    path = tmp_path / "before.wer"
    save_show(ShowFile(), path)
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["sacn"]
    path.write_text(json.dumps(data), encoding="utf-8")
    assert load_show(path).sacn == SacnConfig()


@pytest.mark.parametrize(("field", "stored", "loaded"), [
    ("universe", 70000, 101),
    ("universe", 0, 101),
    ("universe", "5", 5),
    ("universe", "five", 101),
    ("universe", None, 101),
    ("universe", True, 101),
    ("address", 513, 1),
    ("address", 0, 1),
    ("address", -1, 1),
    ("priority", 0, 100),
    ("priority", 201, 100),
    ("priority", None, 100),
    ("enabled", "yes", False),
    ("per_address_priority", None, True),
    ("adapter", 5, ""),
    ("cid", "not a uuid", ""),
])
def test_a_hand_edited_value_that_cannot_be_sent_falls_back_to_its_default(
    tmp_path, field: str, stored, loaded
) -> None:
    """Back to the default, not clamped: clamping 70000 to 63999 would send on
    a universe nobody picked, where the default is at least the one Wer starts
    on everywhere."""
    path = tmp_path / "edited.wer"
    save_show(ShowFile(), path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["sacn"][field] = stored
    path.write_text(json.dumps(data), encoding="utf-8")
    assert getattr(load_show(path).sacn, field) == loaded
