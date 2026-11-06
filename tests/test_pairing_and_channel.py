import time

import nachalo
import pytest
from conftest import run_in_thread

from nachalo_hermes import channel as channel_mod
from nachalo_hermes import runtime
from nachalo_hermes.encoding import msg2_topic, msg3_topic, rendezvous_topic
from nachalo_hermes.listener import Listener
from nachalo_hermes.pairing import InitiatorPairing, PairingError, ResponderPairing


def deliver(relay, side, peer, since="0"):
    """Run every frame for ``peer`` through ``side``'s receive path once (no listener thread)."""
    topics = channel_mod.recv_topics(side.host, side.store, peer)
    out = []
    for msg in [m for t in topics for m in relay.poll(t, since)]:
        out.append(channel_mod.process_frame(side.host, side.store, peer, msg.data))
    return out


def test_pairing_registers_the_same_peer_on_both_sides(pair, alice, bob):
    paired_a, paired_b = pair()
    assert paired_a.peer.peer_id == paired_b.peer.peer_id
    assert paired_a.safety_words == paired_b.safety_words
    assert len(paired_a.safety_words.split()) == 4
    assert paired_a.peer.name == "Bob" and paired_b.peer.name == "Alice"
    assert [p.peer_id for p in alice.store.peers()] == [paired_a.peer.peer_id]
    assert alice.host.store_key() is not None


def test_a_draft_reaches_the_peer_inbox_and_nowhere_else(pair, relay, alice, bob):
    paired_a, paired_b = pair()
    draft = alice.store.add_draft(paired_a.peer.peer_id, "Can your agent review my PR?", "ask", None)
    published_before = len(relay.published)
    result = channel_mod.send_draft(alice.host, alice.store, runtime.transport_for(alice.host), draft)
    assert result.published == 1
    frames = relay.published[published_before:]
    assert all(len(m.data) in (512, 2048) for m in frames)
    assert all(b"review" not in m.data for m in frames)

    received = deliver(relay, bob, paired_b.peer)
    assert [r.kind for r in received if r] == ["data"]
    rows = bob.store.inbox()
    assert [(r["type"], r["text"]) for r in rows] == [("ask", "Can your agent review my PR?")]
    assert alice.store.draft(draft)["sent_at"] is not None
    with pytest.raises(channel_mod.SendError):
        channel_mod.send_draft(alice.host, alice.store, runtime.transport_for(alice.host), draft)

    # Delivering the same frames again files nothing new.
    assert all(r is None for r in deliver(relay, bob, paired_b.peer))
    assert len(bob.store.inbox()) == 1


def test_long_messages_split_and_reassemble(pair, relay, alice, bob):
    paired_a, paired_b = pair()
    text = 'línea con acentos y "comillas"\n' * 120
    alice.host._settings["max_message_chars"] = 10_000
    bob.host._settings["max_message_chars"] = 10_000
    draft = alice.store.add_draft(paired_a.peer.peer_id, text, "note", None)
    result = channel_mod.send_draft(alice.host, alice.store, runtime.transport_for(alice.host), draft)
    assert result.frames > 1
    deliver(relay, bob, paired_b.peer)
    assert bob.store.inbox()[0]["text"] == text


def test_acks_and_replies_flow_both_ways(pair, relay, alice, bob):
    paired_a, paired_b = pair()
    t_a, t_b = runtime.transport_for(alice.host), runtime.transport_for(bob.host)
    d1 = alice.store.add_draft(paired_a.peer.peer_id, "hi bob", "note", None)
    channel_mod.send_draft(alice.host, alice.store, t_a, d1)
    deliver(relay, bob, paired_b.peer)
    assert channel_mod.send_acks(bob.host, bob.store, t_b, paired_b.peer)
    d2 = bob.store.add_draft(paired_b.peer.peer_id, "hi alice", "answer", d1)
    channel_mod.send_draft(bob.host, bob.store, t_b, d2)
    kinds = [r.kind for r in deliver(relay, alice, paired_a.peer) if r]
    assert kinds == ["ack", "data"]
    assert alice.store.conn.execute("SELECT state FROM outbox WHERE id = ?", (d1,)).fetchone()[0] == "acked"
    assert alice.store.inbox()[0]["reply_to"] == d1


def test_revoking_closes_the_channel_on_both_sides(pair, relay, alice, bob):
    paired_a, paired_b = pair()
    assert channel_mod.revoke(alice.host, alice.store, runtime.transport_for(alice.host), paired_a.peer)
    assert [r.kind for r in deliver(relay, bob, paired_b.peer) if r] == ["close"]
    assert bob.store.find_peer(paired_b.peer.peer_id).status == "revoked_by_peer"
    assert alice.store.peers() == []
    assert alice.store.sealed_state(alice.store.conn, paired_a.peer.peer_id) is None


def test_a_man_in_the_middle_who_knows_the_words_is_caught(relay, alice, bob):
    """A relay that also learned the words runs one handshake with each side (AC-07)."""
    from nachalo_hermes.payload import Payload, encode

    a = InitiatorPairing(runtime.transport_for(alice.host), runtime.own_payload(alice.host))
    words = a.start()

    def inv():
        return nachalo.Invitation.live(words, b"hermes-nachalo/1", nachalo.Config(nachalo.Profile.INTERACTIVE))

    msg1_a = relay.poll(rendezvous_topic(inv().rendezvous()), "all")[0].data
    mallory_payload = encode(Payload(name="Bob", relay="https://relay.test"))

    # Mallory answers Alice as "Bob" ...
    m_listener = nachalo.Listener(inv())
    to_alice = m_listener.respond(msg1_a, mallory_payload, nachalo.Clock(now=0, now_unix=int(time.time())))
    # ... and from now on swallows everything on that rendezvous so the real Bob only sees hers.
    relay.tamper = lambda topic, data: [] if topic == rendezvous_topic(inv().rendezvous()) else [(topic, data)]
    relay._messages = [m for m in relay._messages if m.topic != rendezvous_topic(inv().rendezvous())]
    m_init = nachalo.Initiator.start(inv(), mallory_payload, nachalo.Clock(now=0, now_unix=int(time.time())))
    relay.inject(rendezvous_topic(inv().rendezvous()), m_init.msg1())
    relay.inject(msg2_topic(msg1_a), to_alice.msg2)

    b = ResponderPairing(runtime.transport_for(bob.host), runtime.own_payload(bob.host), words)
    thread, result = run_in_thread(b.wait_for_initiator)
    a.wait_for_peer()
    # Mallory completes with Bob: takes his msg2 and sends msg3.
    deadline = time.time() + 5
    while time.time() < deadline and not relay.poll(msg2_topic(m_init.msg1()), "all"):
        time.sleep(0.05)
    msg2_b = relay.poll(msg2_topic(m_init.msg1()), "all")[0].data
    fin = m_init.finish(msg2_b, nachalo.Clock(now=0, now_unix=int(time.time())))
    relay.inject(msg3_topic(msg2_b), fin.msg3)
    thread.join(10)
    bob_safety = result["value"]

    assert a.verify(bob_safety) is False  # Alice types what Bob reads: no match
    a.mismatch()
    b.mismatch()
    assert alice.store.peers() == [] and bob.store.peers() == []
    assert alice.host.store_key() is None and bob.host.store_key() is None


def test_wrong_words_find_nobody(relay, alice, bob, monkeypatch):
    from nachalo_hermes import pairing

    a = InitiatorPairing(runtime.transport_for(alice.host), runtime.own_payload(alice.host))
    a.start()
    b = ResponderPairing(
        runtime.transport_for(bob.host), runtime.own_payload(bob.host), "abandon ability able about above absent"
    )
    b.deadline = pairing.monotonic_counting_sleep() + 1
    with pytest.raises(PairingError) as err:
        b.wait_for_initiator()
    assert err.value.code == "expired"
    with pytest.raises(PairingError) as err:
        ResponderPairing(runtime.transport_for(bob.host), b"x", "not six words")
    assert err.value.code == "bad_words"
    a.abort()


def test_the_listener_files_messages_and_sends_acks(pair, relay, alice, bob):
    paired_a, paired_b = pair()
    listener = Listener(bob.host, recheck=0.2, ack_every=0.2)
    listener.start()
    try:
        draft = alice.store.add_draft(paired_a.peer.peer_id, "via listener", "note", None)
        channel_mod.send_draft(alice.host, alice.store, runtime.transport_for(alice.host), draft)
        deadline = time.time() + 5
        while time.time() < deadline and not bob.store.inbox():
            time.sleep(0.05)
        assert bob.store.inbox()[0]["text"] == "via listener"
        assert ("inbox.count", {"held": 1}) in bob.host.events
        while time.time() < deadline and not deliver(relay, alice, paired_a.peer):
            time.sleep(0.05)
        assert alice.store.conn.execute("SELECT state FROM outbox").fetchone()[0] == "acked"
    finally:
        listener.close()


def test_only_one_listener_owns_a_profile(pair, bob):
    pair()
    first, second = Listener(bob.host, recheck=0.1), Listener(bob.host, recheck=0.1)
    first.start()
    time.sleep(0.3)
    from nachalo_hermes.store import Store

    assert first._lease(Store(bob.host)) is True
    assert second._lease(Store(bob.host)) is False
    first.close()


def test_writing_to_the_database_cannot_make_anything_send(pair, relay, alice):
    """An agent script that edits plugin_db (AC-17): forged outbox rows and drafts send nothing."""
    paired_a, _ = pair()
    before = len(relay.published)
    alice.store.conn.execute(
        "INSERT INTO outbox VALUES ('X',1,?,0,'forged',?, 'data','queued',0,0,NULL)",
        (paired_a.peer.peer_id, b"exfiltrated secret".ljust(512, b"\0")),
    )
    alice.store.add_draft(paired_a.peer.peer_id, "the agent wrote this", "note", None)
    alice.store.conn.execute("UPDATE drafts SET sent_at = NULL")
    assert channel_mod.retry_outbox(alice.host, alice.store, runtime.transport_for) == 0
    listener = Listener(alice.host, recheck=0.1, ack_every=0.1)
    listener.start()
    time.sleep(0.6)
    listener.close()
    assert len(relay.published) == before
    events = [r["event"] for r in alice.store.conn.execute("SELECT event FROM audit")]
    assert "outbox_forged" in events
