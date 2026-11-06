import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from nachalo_hermes import host as host_mod  # noqa: E402
from nachalo_hermes import runtime  # noqa: E402
from nachalo_hermes.host import LocalHost  # noqa: E402
from nachalo_hermes.store import Store  # noqa: E402
from nachalo_hermes.transport.memory import MemoryRelay, MemoryTransport  # noqa: E402


@pytest.fixture
def relay():
    relay = MemoryRelay()
    runtime.set_transport_factory(lambda host, url=None: MemoryTransport(relay))
    yield relay
    runtime.set_transport_factory(None)


class Side:
    def __init__(self, root: Path, name: str):
        self.host = LocalHost(root / name, settings={"display_name": name, "relay_url": "https://relay.test"})
        self.store = Store(self.host)
        self.name = name

    def use(self):
        host_mod.set_host(self.host)
        return self


@pytest.fixture
def alice(tmp_path):
    side = Side(tmp_path, "Alice")
    yield side
    side.store.close()


@pytest.fixture
def bob(tmp_path):
    side = Side(tmp_path, "Bob")
    yield side
    side.store.close()


def run_in_thread(fn):
    result = {}

    def target():
        try:
            result["value"] = fn()
        except BaseException as exc:  # surfaced to the test
            result["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, result


@pytest.fixture
def pair(relay, alice, bob):
    """Pair Alice (A) and Bob (B) through the memory relay; returns (alice_peer, bob_peer)."""
    from nachalo_hermes.pairing import InitiatorPairing, ResponderPairing

    def do_pair():
        a = InitiatorPairing(runtime.transport_for(alice.host), runtime.own_payload(alice.host))
        words = a.start()
        b = ResponderPairing(runtime.transport_for(bob.host), runtime.own_payload(bob.host), words)
        thread, result = run_in_thread(b.wait_for_initiator)
        a.wait_for_peer()
        thread.join(10)
        safety = result["value"]
        assert a.verify(safety)
        paired_a = a.finish(alice.host, alice.store, "https://relay.test")
        paired_b = b.confirm(bob.host, bob.store, "https://relay.test")
        return paired_a, paired_b

    return do_pair
