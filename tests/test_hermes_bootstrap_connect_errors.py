"""The process-wide Happy Eyeballs wrapper preserves socket's all_errors contract."""

import errno
import socket
from contextlib import ExitStack

import pytest


def _resolve_to(monkeypatch, sockets):
    infos = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sock.getsockname())
             for sock in sockets]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: infos)


def _track_candidates(monkeypatch):
    real_socket = socket.socket
    candidates = []

    def create(*args, **kwargs):
        candidate = real_socket(*args, **kwargs)
        candidates.append(candidate)
        return candidate

    monkeypatch.setattr(socket, "socket", create)
    return candidates


def _mixed_deadline(monkeypatch):
    """One immediate refusal, then an in-flight attempt that exhausts the shared deadline."""
    import hermes_bootstrap

    clock = [0.0]
    candidates = []
    registered = set()

    class Candidate:
        def __init__(self, *args):
            self.closed = False
            candidates.append(self)

        def setblocking(self, value):
            pass

        def connect_ex(self, address):
            return errno.ECONNREFUSED if len(candidates) == 1 else errno.EINPROGRESS

        def close(self):
            self.closed = True

        def fileno(self):
            return -1 if self.closed else 1

    class Selector:
        def register(self, candidate, events):
            registered.add(candidate)

        def unregister(self, candidate):
            registered.remove(candidate)

        def select(self, timeout):
            clock[0] += timeout
            return []

        def close(self):
            assert not registered

    monkeypatch.setattr(socket, "socket", Candidate)
    monkeypatch.setattr(hermes_bootstrap.selectors, "DefaultSelector", Selector)
    monkeypatch.setattr(hermes_bootstrap.time, "monotonic", lambda: clock[0])
    return candidates


def _unresolved(*args, **kwargs):
    raise socket.gaierror(socket.EAI_NONAME, "unknown host")


@pytest.mark.parametrize("all_errors", [False, True])
@pytest.mark.parametrize("mode, expected_members", [
    ("one_refused", (ConnectionRefusedError,)),
    ("two_refused", (ConnectionRefusedError, ConnectionRefusedError)),
    ("deadline", (TimeoutError,)),
    ("mixed_deadline", (ConnectionRefusedError, TimeoutError)),
    ("dns", (socket.gaierror,)),
])
def test_failed_connect_preserves_error_mode_and_closes_candidates(monkeypatch, all_errors, mode, expected_members):
    import hermes_bootstrap  # installs the process-wide wrapper

    with ExitStack() as stack, monkeypatch.context() as patcher:
        # Held but not listening: real connect failures without a free-port race.
        reserved = [stack.enter_context(socket.socket()) for _ in range(2)]
        for sock in reserved:
            sock.bind(("127.0.0.1", 0))
        _resolve_to(patcher, reserved[:1] if mode == "one_refused" else reserved)
        candidates = _mixed_deadline(patcher) if mode == "mixed_deadline" else _track_candidates(patcher)
        if mode == "dns":
            patcher.setattr(socket, "getaddrinfo", _unresolved)
        aggregate = all_errors and mode != "dns"
        expected = ExceptionGroup if aggregate else expected_members[-1]
        with pytest.raises(expected) as caught:
            socket.create_connection(("connect-errors.invalid", 1), timeout=0 if mode == "deadline" else 5,
                                     all_errors=all_errors)
        if aggregate:
            assert tuple(type(error) for error in caught.value.exceptions) == expected_members
        expected_count = 0 if mode in {"deadline", "dns"} else 1 if mode == "one_refused" else 2
        assert len(candidates) == expected_count
        assert all(candidate.fileno() == -1 for candidate in candidates)


def test_all_errors_returns_success_after_refused_candidate(monkeypatch):
    import hermes_bootstrap  # installs the process-wide wrapper

    with socket.socket() as refused, socket.socket() as listener:
        refused.bind(("127.0.0.1", 0))
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(5)
        _resolve_to(monkeypatch, (refused, listener))
        candidates = _track_candidates(monkeypatch)
        with socket.create_connection(("connect-errors.invalid", 1), timeout=5, all_errors=True) as client:
            assert client.gettimeout() == 5
            assert candidates[0].fileno() == -1
            with listener.accept()[0] as server:
                server.settimeout(5)
                client.sendall(b"x")
                assert server.recv(1) == b"x"
        assert all(candidate.fileno() == -1 for candidate in candidates)
