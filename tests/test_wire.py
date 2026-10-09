"""The JSON-lines protocol (modkeel/core/wire.py) and `modkeel serve --stdio`.

The contract: a request through the protocol gives the same events, questions and result as
the in-process call. Then the rules that keep a front end from hanging: bad lines never stop
the server, wrong answers and a closed input fall back to the safe defaults, one request at
a time, cancel.
"""

import json
import os
import queue
import threading

import pytest

from modkeel.core.decisions import Cancelled, ChangeTarget, NeedToken
from modkeel.core.engine import get_mod
from modkeel.core.wire import Server, get_result_dict
from tests.test_engine import REQUEST, create_only_on_1_21_1, isolated  # noqa: F401


class _Lines:
    """The server's output stream: each protocol line goes to a queue the test reads."""

    def __init__(self):
        self.q = queue.Queue()
        self._buf = ""

    def write(self, text):
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self.q.put(json.loads(line))

    def flush(self):
        pass


class Session:
    """A server on a pipe, driven by the test like a front end would."""

    def __init__(self, methods=None):
        read_fd, write_fd = os.pipe()
        self.server_in = os.fdopen(read_fd, "r", encoding="utf-8")
        self.client_out = os.fdopen(write_fd, "w", encoding="utf-8")
        self.out = _Lines()
        self.server = Server(self.server_in, self.out, methods)
        self.thread = threading.Thread(target=self.server.serve, daemon=True)
        self.thread.start()
        self.hello = self.next()

    def send(self, message):
        self.client_out.write((message if isinstance(message, str) else json.dumps(message))
                              + "\n")
        self.client_out.flush()

    def next(self, timeout=20):
        return self.out.q.get(timeout=timeout)

    def until_done(self, answer=lambda q: None):
        """Messages up to the request's result or error, answering questions on the way."""
        seen = []
        while True:
            m = self.next()
            seen.append(m)
            if m["type"] == "question":
                self.send({"type": "answer", "qid": m["qid"], "value": answer(m["question"])})
            elif m["type"] in ("result", "error") and "id" in m:
                return seen

    def close(self):
        self.client_out.close()
        self.thread.join(timeout=20)
        assert not self.thread.is_alive(), "the server must exit when its input closes"


@pytest.fixture
def session():
    sessions = []

    def start(methods=None):
        s = Session(methods)
        sessions.append(s)
        return s

    yield start
    for s in sessions:
        if not s.client_out.closed:
            s.close()


def answer_like_the_cli(question):
    """Accept the version change, give no token (the in-process run below does the same)."""
    return question["kind"] == "change_target"


class TestContract:
    def test_same_events_questions_and_result_as_in_process(self, session, tmp_path,
                                                             monkeypatch):
        # in process
        events, asked = [], []

        def decide(q):
            asked.append(q.kind)
            return isinstance(q, ChangeTarget)

        (tmp_path / "local").mkdir()
        monkeypatch.chdir(tmp_path / "local")
        with create_only_on_1_21_1():
            local = get_mod(REQUEST, events=events.append, decide=decide)
        expected_events = [e.to_dict() for e in events]
        expected_result = get_result_dict(local)

        # through the protocol, from a fresh folder (same relative paths)
        (tmp_path / "wire").mkdir()
        monkeypatch.chdir(tmp_path / "wire")
        s = session()
        params = {"query": REQUEST.query, "mc_version": REQUEST.mc_version,
                  "loader": REQUEST.loader}
        with create_only_on_1_21_1():
            s.send({"type": "request", "id": "r1", "method": "get", "params": params})
            seen = s.until_done(answer_like_the_cli)

        assert [m["event"] for m in seen if m["type"] == "event"] == expected_events
        assert [m["question"]["kind"] for m in seen if m["type"] == "question"] == asked
        assert seen[-1] == {"type": "result", "id": "r1", "result": expected_result}
        assert expected_result["retargeted"] and expected_result["delivered"]["mod_version"] \
            == "6.0.6"
        assert all(m.get("id") == "r1" for m in seen)

    def test_a_question_carries_the_sentence_to_show(self, session):
        s = session()
        with create_only_on_1_21_1():
            s.send({"type": "request", "id": 7, "method": "get",
                    "params": {"query": "Create", "mc_version": "1.21.10", "loader": "neoforge"}})
            seen = s.until_done()
        change = next(m for m in seen if m["type"] == "question"
                      and m["question"]["kind"] == "change_target")
        assert change["question"]["option"]["summary"] == \
            "MC 1.21.1 has an official build of Create"
        assert change["question"]["current"] == "1.21.10" and change["qid"].startswith("7.")
        # no answer accepted it (null is not a bool): the version was kept
        assert seen[-1]["result"]["retargeted"] is False


def asks(question):
    """A method that asks one question and returns the answer it got."""
    def method(params, events, decide, cancelled):
        answer = decide(question)
        if cancelled():
            raise Cancelled()
        return {"answer": answer}
    return {"ask": method}


class TestProtocolRules:
    def test_hello_names_the_protocol_and_methods(self, session):
        hello = session().hello
        assert hello["type"] == "hello" and hello["protocol"] == 1
        assert hello["methods"] == ["get", "instances", "move"] and hello["modkeel"]

    def test_bad_lines_get_errors_and_never_stop_the_server(self, session):
        s = session({"echo": lambda params, *_: params})
        for line, code in [("not json", "bad_message"), ("[1, 2]", "bad_message"),
                           ('{"type": "dance"}', "bad_message"),
                           ('{"type": "request", "method": "echo"}', "bad_message"),
                           ('{"type": "request", "id": "x", "method": "nope"}', "unknown_method"),
                           ('{"type": "request", "id": "y", "method": "echo", "params": 3}',
                            "bad_params")]:
            s.send(line)
            assert s.next()["error"]["code"] == code
        s.send({"type": "request", "id": "z", "method": "echo", "params": {"a": 1}})
        assert s.next() == {"type": "result", "id": "z", "result": {"a": 1}}

    def test_get_with_missing_or_unknown_params(self, session):
        s = session()
        s.send({"type": "request", "id": "1", "method": "get", "params": {"query": "x"}})
        assert s.next()["error"]["code"] == "bad_params"
        s.send({"type": "request", "id": "2", "method": "get",
                "params": {"query": "x", "mc_version": "1.21.10", "loader": "fabric",
                           "colour": "red"}})
        assert s.next()["error"]["code"] == "bad_params"

    @pytest.mark.parametrize("question, value, expected", [
        (ChangeTarget(None, "1.21.10"), True, True),
        (ChangeTarget(None, "1.21.10"), "yes", False),       # not a bool: keep the version
        (NeedToken(), "ghp_token", "ghp_token"),
        (NeedToken(), 42, None),                             # not a string: no token
        (NeedToken(), "", None),
    ])
    def test_answers_of_the_wrong_type_get_the_safe_default(self, session, question, value,
                                                            expected):
        s = session(asks(question))
        s.send({"type": "request", "id": "q", "method": "ask"})
        seen = s.until_done(lambda q: value)
        assert seen[-1]["result"] == {"answer": expected}

    def test_one_request_at_a_time(self, session):
        s = session(asks(NeedToken()))
        s.send({"type": "request", "id": "first", "method": "ask"})
        question = s.next()
        assert question["type"] == "question"
        s.send({"type": "request", "id": "second", "method": "ask"})
        assert s.next() == {"type": "error", "id": "second",
                            "error": {"code": "busy", "message": "one request at a time"}}
        s.send({"type": "answer", "qid": question["qid"], "value": "tok"})
        assert s.next()["result"] == {"answer": "tok"}

    def test_a_query_answers_while_a_request_runs(self, session, monkeypatch):
        from modkeel.instances import Instance

        monkeypatch.setattr("modkeel.instances.find_instances",
                            lambda: [Instance("prism", "ATM", "/i", "/i/mods", "1.21.1",
                                              "neoforge", "21.1.77", 3)])
        s = session(asks(NeedToken()))
        s.send({"type": "request", "id": "run", "method": "ask"})
        question = s.next()
        s.send({"type": "request", "id": "list", "method": "instances"})
        listed = s.next()
        assert listed["type"] == "result" and listed["id"] == "list"
        assert listed["result"]["instances"] == [{
            "launcher": "prism", "name": "ATM", "path": "/i", "mods_dir": "/i/mods",
            "mc_version": "1.21.1", "loader": "neoforge", "loader_version": "21.1.77",
            "mods": 3}]
        s.send({"type": "answer", "qid": question["qid"], "value": "tok"})
        assert s.next()["result"] == {"answer": "tok"}       # the run was not disturbed

    def test_a_query_with_params_it_does_not_take(self, session):
        s = session({})
        s.send({"type": "request", "id": "q", "method": "instances", "params": {"x": 1}})
        assert s.next()["error"]["code"] == "bad_params"

    def test_cancel_releases_the_question_and_ends_the_request(self, session):
        s = session(asks(ChangeTarget(None, "1.21.10")))
        s.send({"type": "request", "id": "c", "method": "ask"})
        assert s.next()["type"] == "question"
        s.send({"type": "cancel", "id": "c"})
        assert s.next()["error"]["code"] == "cancelled"

    def test_closing_the_input_answers_and_stops(self, session):
        s = session(asks(ChangeTarget(None, "1.21.10")))
        s.send({"type": "request", "id": "e", "method": "ask"})
        assert s.next()["type"] == "question"
        s.close()                         # joins: the server exited
        assert s.next()["error"]["code"] == "cancelled"

    def test_a_crash_in_a_method_is_an_error_not_a_dead_server(self, session):
        def boom(*_):
            raise RuntimeError("disk full")

        s = session({"boom": boom, "echo": lambda params, *_: params})
        s.send({"type": "request", "id": "b", "method": "boom"})
        assert s.next()["error"] == {"code": "internal", "message": "RuntimeError: disk full"}
        s.send({"type": "request", "id": "ok", "method": "echo", "params": {}})
        assert s.next()["result"] == {}


class _GoneAfterHello(_Lines):
    """An output whose reader disappears after the hello (the client process died)."""

    def write(self, text):
        if self.q.qsize() >= 1:
            raise BrokenPipeError(32, "Broken pipe")
        super().write(text)


class TestClientGone:
    def test_a_closed_output_cancels_the_run_without_crashing(self):
        crashes, finished = [], threading.Event()
        old_hook = threading.excepthook
        threading.excepthook = lambda args: crashes.append(args.exc_value)

        def chatty(params, events, decide, cancelled):
            from modkeel.core.events import Message

            for _ in range(100):
                events(Message("working"))
                if cancelled():
                    finished.set()
                    raise Cancelled()
            return {}

        read_fd, write_fd = os.pipe()
        server_in = os.fdopen(read_fd, "r", encoding="utf-8")
        client = os.fdopen(write_fd, "w", encoding="utf-8")
        server = Server(server_in, _GoneAfterHello(), {"chatty": chatty})
        thread = threading.Thread(target=server.serve, daemon=True)
        try:
            thread.start()
            client.write(json.dumps({"type": "request", "id": "1", "method": "chatty"}) + "\n")
            client.flush()
            assert finished.wait(10), "the run must see the cancel"
            client.close()
            thread.join(10)
        finally:
            threading.excepthook = old_hook
        assert not thread.is_alive() and server.gone and crashes == []


class TestEncoding:
    def test_lines_are_ascii_whatever_the_text(self):
        from modkeel.core.wire import encode

        line = encode({"type": "event", "event": {"kind": "message",
                                                   "text": "    \U0001f4e6 Dependency: o\u03c9o"}})
        assert line.isascii()
        assert json.loads(line)["event"]["text"] == "    \U0001f4e6 Dependency: o\u03c9o"

    def test_a_cp1252_output_carries_emoji_and_the_run_finishes(self):
        """Windows pipes default to cp1252: an emoji event used to fail the write and stop
        the run silently (the error looked like a closed client)."""
        import io

        from modkeel.core.events import Message

        raw = io.BytesIO()
        out = io.TextIOWrapper(raw, encoding="cp1252", write_through=True)
        server = Server(io.StringIO(""), out, {})
        server.send({"type": "event", "id": "1",
                     "event": Message("    \U0001f4e6 Dependency: Fabric API").to_dict()})
        assert not server.gone
        line = raw.getvalue().decode("ascii").strip()
        assert json.loads(line)["event"]["text"].startswith("    \U0001f4e6")


class TestServeCommand:
    def test_requires_a_transport(self):
        from tests.test_cli import invoke

        result = invoke("serve")
        assert result.exit_code == 2 and "--stdio" in result.output

    def test_stdout_carries_only_protocol_lines(self):
        from tests.test_cli import runner
        from modkeel.cli import app

        result = runner.invoke(app, ["serve", "--stdio"],
                               input='{"type":"request","id":"1","method":"nope"}\n')
        lines = [json.loads(line) for line in result.stdout.splitlines()]
        assert [m["type"] for m in lines] == ["hello", "error"]
