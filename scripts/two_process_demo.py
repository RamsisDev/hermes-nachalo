"""Two separate processes pair over a real ntfy relay and exchange one message each way.

The "people" are simulated: the words and safety words pass through files in a temporary
directory instead of a voice call. Demo and smoke test only; never do this with real pairings.

    python scripts/two_process_demo.py [--relay https://ntfy.sh]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def wait_file(path: Path, timeout: float = 120) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists() and path.read_text().strip():
            return path.read_text().strip()
        time.sleep(0.1)
    raise SystemExit(f"timed out waiting for {path.name}")


def role(name: str, exchange: Path, relay: str) -> int:
    from nachalo_hermes import channel as channel_mod
    from nachalo_hermes.host import LocalHost, set_host
    from nachalo_hermes.listener import Listener
    from nachalo_hermes.pairing import InitiatorPairing, ResponderPairing
    from nachalo_hermes.runtime import own_payload, transport_for
    from nachalo_hermes.store import Store

    host = LocalHost(exchange / name, settings={"relay_url": relay, "display_name": f"{name} · demo"})
    set_host(host)
    store = Store(host)
    t0 = time.monotonic()
    if name == "alice":
        a = InitiatorPairing(transport_for(host), own_payload(host))
        (exchange / "words").write_text(a.start())
        a.wait_for_peer()
        if not a.verify(wait_file(exchange / "safety")):
            print("alice: safety words do not match")
            return 1
        paired = a.finish(host, store, relay)
        (exchange / "matched").write_text("yes")
    else:
        b = ResponderPairing(transport_for(host), own_payload(host), wait_file(exchange / "words"))
        (exchange / "safety").write_text(b.wait_for_initiator())
        wait_file(exchange / "matched")
        paired = b.confirm(host, store, relay)
    print(f"{name}: paired with {paired.peer.name} ({paired.display_id}) in {time.monotonic() - t0:.1f} s")

    listener = Listener(host, recheck=1, ack_every=2)
    listener.start()
    time.sleep(2)
    text = f"hello from {name}"
    draft = store.add_draft(paired.peer.peer_id, text, "note", None)
    sent = channel_mod.send_draft(host, store, transport_for(host), draft)
    print(f"{name}: sent {sent.frames} frame(s)")
    deadline = time.time() + 60
    while time.time() < deadline and not store.inbox():
        time.sleep(0.2)
    rows = store.inbox()
    listener.close()
    if not rows:
        print(f"{name}: nothing received")
        return 1
    print(f"{name}: received {rows[0]['text']!r} in {time.monotonic() - t0:.1f} s total")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--relay", default="https://ntfy.sh")
    parser.add_argument("--role", choices=["alice", "bob"])
    parser.add_argument("--exchange")
    args = parser.parse_args()
    if args.role:
        return role(args.role, Path(args.exchange), args.relay)
    with tempfile.TemporaryDirectory() as tmp:
        procs = [
            subprocess.Popen([sys.executable, __file__, "--role", r, "--exchange", tmp, "--relay", args.relay])
            for r in ("alice", "bob")
        ]
        codes = [p.wait(timeout=300) for p in procs]
    print("OK" if codes == [0, 0] else f"FAILED {codes}")
    return 0 if codes == [0, 0] else 1


if __name__ == "__main__":
    raise SystemExit(main())
