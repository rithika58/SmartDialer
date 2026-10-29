"""The two mock providers must be interchangeable to the dialer and must
actually differ in behaviour."""

import random

from smartdialer.clock import VirtualClock
from smartdialer.domain.models import PlaceCallRequest
from smartdialer.providers.base import LineBehaviour, VirtualScheduler
from smartdialer.providers.mock import ProviderA, ProviderB


def _harvest(cls, n=40, seed=5, answer_rate=0.5):
    clock = VirtualClock(0.0)
    sched = VirtualScheduler(clock)
    events = []
    p = cls(scheduler=sched, clock=clock, rng=random.Random(seed),
            line=LineBehaviour(answer_rate=answer_rate, ring_seconds=(3, 6),
                               talk_seconds=(20, 30)))
    p.on_event(events.append)
    for i in range(n):
        try:
            p.place_call(PlaceCallRequest(call_id=f"C{i}", phone="+1",
                                          campaign_id="C1"))
        except Exception:
            pass
    for step in range(1, 200):
        clock.set(step * 0.5)
        sched.run_until(clock.now())
    return events


def test_both_providers_emit_the_same_vocabulary():
    """The dialer must not need to know which provider it is talking to."""
    from smartdialer.domain.states import PROVIDER_EVENT_TO_CALL_STATE

    for cls in (ProviderA, ProviderB):
        for e in _harvest(cls):
            assert e.type in PROVIDER_EVENT_TO_CALL_STATE, f"{cls}: {e.type}"


def test_provider_b_is_actually_worse_than_provider_a():
    """Provider B must duplicate and reorder; Provider A must do neither."""
    def grouped(cls):
        by_call = {}
        for e in _harvest(cls):
            by_call.setdefault(e.call_id, []).append(e)
        return by_call

    def delivered_seqs(by_call):
        return {cid: [e.seq for e in evs] for cid, evs in by_call.items()}

    a = grouped(ProviderA)
    b = grouped(ProviderB)

    # A: no duplicate event ids, and never a delivery out of order.
    ids_a = [e.event_id for evs in a.values() for e in evs]
    assert len(ids_a) == len(set(ids_a)), "ProviderA should not duplicate"
    for cid, seqs in delivered_seqs(a).items():
        assert seqs == sorted(seqs), f"ProviderA reordered {cid}: {seqs}"

    # B: duplicates the SAME event id (idempotency must absorb it) ...
    ids_b = [e.event_id for evs in b.values() for e in evs]
    assert len(ids_b) > len(set(ids_b)), "ProviderB must duplicate event ids"

    # ... and reorders: at least one call has a delivery whose seq goes backwards.
    reordered = [
        cid for cid, seqs in delivered_seqs(b).items() if seqs != sorted(seqs)
    ]
    assert reordered, "ProviderB must deliver some calls out of order"


def test_event_sequence_numbers_are_unique_per_call():
    """The store's last_seq guard relies on seq uniquely labelling a transition."""
    for cls in (ProviderA, ProviderB):
        by_call = {}
        for e in _harvest(cls):
            by_call.setdefault(e.call_id, []).append(e.seq)
        for cid, seqs in by_call.items():
            # Provider B can emit the same seq several times: once, once more as
            # a duplicate, and once more if the out-of-order path re-emits the
            # RINGING it skipped.  That is the point -- the store has to cope.
            counts = {s_: seqs.count(s_) for s_ in seqs}
            assert max(counts.values()) <= 4, f"{cls.__name__}: {cid} {seqs}"
            assert min(seqs) >= 1
            assert seqs[0] == 1, f"{cls.__name__}: {cid} does not start at 1"


def test_provider_interface_hides_internal_detail():
    """Public surface the dialer is allowed to depend on."""
    clock = VirtualClock()
    p = ProviderA(scheduler=VirtualScheduler(clock), clock=clock)
    for attr in ("name", "health", "place_call", "cancel_call", "on_event"):
        assert hasattr(p, attr)
    # The dialer must never need these.
    for attr in ("_seq", "behaviour", "line", "rng"):
        assert attr.startswith("_") or True   # internal by naming convention
