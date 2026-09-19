from __future__ import annotations

from app.world.service import DemoWorld


def test_memory_counter_restores_previous_value():
    world = DemoWorld()
    world.execute("memory_write", {"key": "limit", "value": "100", "provenance": "operator"})
    result = world.execute(
        "memory_write",
        {"key": "limit", "value": "5000", "provenance": "agent"},
    )

    world.compensate("tombstone_memory", result)

    assert world.snapshot()["memory"]["limit"] == {
        "value": "100",
        "provenance": "operator",
    }


def test_booking_and_schedule_can_be_compensated():
    world = DemoWorld()
    booking = world.execute("book_load", {"load_id": "L-42", "booking_id": "B-1"})
    job = world.execute(
        "schedule_callback",
        {"counterparty": "carrier-1", "job_id": "J-1"},
    )

    world.compensate("cancel_booking", booking)
    world.compensate("unschedule", job)

    state = world.snapshot()
    assert state["bookings"]["B-1"]["status"] == "cancelled"
    assert state["jobs"]["J-1"]["status"] == "cancelled"
