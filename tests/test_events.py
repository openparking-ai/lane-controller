from lane_controller.events import EventQueue, LaneEvent


class AcceptingTransport:
    def __init__(self):
        self.batches = []

    def send(self, events):
        self.batches.append(events)
        return True


class RefusingTransport:
    def send(self, events):
        return False


def test_a_successful_flush_clears_the_queue():
    transport = AcceptingTransport()
    queue = EventQueue(transport)
    queue.record("decision", "lane-1", outcome="allow")

    assert queue.flush() == 1
    assert queue.pending == 0
    assert len(transport.batches[0]) == 1


def test_a_refused_flush_keeps_everything():
    """The lane is offline. Nothing may be dropped on the assumption it arrived."""
    queue = EventQueue(RefusingTransport())
    queue.record("decision", "lane-1", outcome="allow")

    assert queue.flush() == 0
    assert queue.pending == 1


def test_overflow_is_counted_rather_than_silent():
    queue = EventQueue(max_events=2)
    for i in range(5):
        queue.record("decision", "lane-1", n=i)

    assert queue.pending == 2
    assert queue.dropped == 3, "a gap in the record must be measured, not invisible"


def test_events_serialise():
    event = LaneEvent(kind="vended", lane_id="lane-1", at=1.0, detail={"plate": "SIM-0001"})
    assert event.as_dict()["detail"]["plate"] == "SIM-0001"
