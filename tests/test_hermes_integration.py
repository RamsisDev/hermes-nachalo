"""Against a real Hermes install: the Desktop REST routes, HermesHost, plugin_db and .env secrets.

Run with Hermes's interpreter, e.g. ``HERMES_PYTHON=/path/to/hermes/.venv/bin/python make test-hermes``.
"""

import importlib.util
import sys
import time

import pytest
from conftest import ROOT, run_in_thread

pytest.importorskip("hermes_constants")
pytest.importorskip("fastapi")


@pytest.fixture
def api(tmp_path, monkeypatch, relay):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    (tmp_path / "hermes-home").mkdir()
    for name in [n for n in sys.modules if n.startswith("hermes_nachalo_dashboard_core")]:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_nachalo", ROOT / "dashboard" / "plugin_api.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    core_runtime = sys.modules["hermes_nachalo_dashboard_core.runtime"]
    from nachalo_hermes.transport.memory import MemoryTransport

    core_runtime.set_transport_factory(lambda host, url=None: MemoryTransport(relay))
    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/nachalo")
    yield TestClient(app), tmp_path / "hermes-home"
    core_runtime.set_transport_factory(None)


def wait_state(client, session, wanted, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = client.get(f"/api/plugins/nachalo/pair/{session}").json()
        if state["state"] in wanted:
            return state
        time.sleep(0.05)
    raise AssertionError(f"pairing never reached {wanted}: {state}")


def test_desktop_pairs_and_sends_through_hermes(api, relay, bob):
    from nachalo_hermes import channel as channel_mod
    from nachalo_hermes import runtime
    from nachalo_hermes.pairing import ResponderPairing

    client, home = api
    assert client.get("/api/plugins/nachalo/status").json() == {"peers": 0, "held": 0, "drafts": 0}

    started = client.post("/api/plugins/nachalo/pair/start", json={"name": "Alice · desktop"}).json()
    assert len(started["words"]) == 6 and started["state"] == "waiting"
    assert "words" not in client.get(f"/api/plugins/nachalo/pair/{started['session']}").json()

    b = ResponderPairing(runtime.transport_for(bob.host), runtime.own_payload(bob.host), " ".join(started["words"]))
    thread, result = run_in_thread(b.wait_for_initiator)
    wait_state(client, started["session"], {"verifying"})
    thread.join(10)
    wrong = client.post(f"/api/plugins/nachalo/pair/{started['session']}/verify", json={"words": "a b c d"}).json()
    assert wrong["match"] is False
    ok = client.post(f"/api/plugins/nachalo/pair/{started['session']}/verify", json={"words": result["value"]}).json()
    assert ok["match"] is True and ok["peer"]["name"] == "Bob"
    paired_b = b.confirm(bob.host, bob.store, "https://relay.test")

    env = (home / ".env").read_text()
    assert "NACHALO_STORE_KEY=" in env
    assert (home / "plugin-data" / "nachalo" / "data.db").exists()
    for path in home.rglob("*"):
        if path.is_file():
            assert " ".join(started["words"]).encode() not in path.read_bytes()

    peers = client.get("/api/plugins/nachalo/peers").json()["peers"]
    assert [p["name"] for p in peers] == ["Bob"]
    assert (
        client.get(f"/api/plugins/nachalo/peers/{peers[0]['peer_id']}/fingerprint").json()["safety_words"]
        == result["value"].split()
    )

    core_store = sys.modules["hermes_nachalo_dashboard_core.store"]
    core_host = sys.modules["hermes_nachalo_dashboard_core.host"]
    store = core_store.Store(core_host.get_host())
    draft = store.add_draft(peers[0]["peer_id"], "From Desktop, with a click", "note", None)
    store.close()
    drafts = client.get("/api/plugins/nachalo/drafts").json()["drafts"]
    assert drafts[0]["text"] == "From Desktop, with a click" and drafts[0]["leak_warning"] is None
    sent = client.post(f"/api/plugins/nachalo/drafts/{draft}/send").json()
    assert sent == {"sent": True, "peer": "Bob", "frames": 1, "published": 1}
    assert client.post(f"/api/plugins/nachalo/drafts/{draft}/send").status_code == 409

    for topic in channel_mod.recv_topics(bob.host, bob.store, paired_b.peer):
        for msg in relay.poll(topic, "all"):
            channel_mod.process_frame(bob.host, bob.store, paired_b.peer, msg.data)
    assert [r["text"] for r in bob.store.inbox()] == ["From Desktop, with a click"]


def test_a_store_key_saved_by_another_process_is_found(tmp_path, monkeypatch):
    """The gateway loaded its env before a CLI pairing wrote the key to the profile's .env."""
    from nachalo_hermes.host import STORE_KEY_ENV, HermesHost

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv(STORE_KEY_ENV, raising=False)
    host = HermesHost()
    assert host.store_key() is None
    (home / ".env").write_text(f"{STORE_KEY_ENV}={'ab' * 32}\n")
    assert host.store_key() == bytes.fromhex("ab" * 32)


def test_desktop_join_and_reject_discards_everything(api, relay, alice):
    from nachalo_hermes import runtime
    from nachalo_hermes.pairing import InitiatorPairing

    client, home = api
    a = InitiatorPairing(runtime.transport_for(alice.host), runtime.own_payload(alice.host))
    words = a.start()
    thread, _ = run_in_thread(a.wait_for_peer)
    joined = client.post("/api/plugins/nachalo/join/start", json={"words": words.upper()}).json()
    state = wait_state(client, joined["session"], {"verifying"})
    assert len(state["safety_words"].split()) == 4
    thread.join(10)
    rejected = client.post(f"/api/plugins/nachalo/pair/{joined['session']}/confirm", json={"match": False}).json()
    assert rejected["state"] == "mismatch"
    assert client.get("/api/plugins/nachalo/peers").json()["peers"] == []
    assert not (home / ".env").exists() or "NACHALO_STORE_KEY" not in (home / ".env").read_text()
    a.abort()
