"""JSON-lines wire format of the engine: `modkeel serve --stdio` (IDEA-028 step 3).

A front end in another process (the desktop app, a script, later an HTTP wrapper) runs the
engine through this protocol and sees exactly what an in-process caller sees: the same
events, the same questions, the same result. One JSON object per line, UTF-8.

    server -> client  {"type": "hello", "protocol": 1, "modkeel": "0.1.2", "methods": [...]}
    client -> server  {"type": "request", "id": "1", "method": "get", "params": {...}}
    server -> client  {"type": "event", "id": "1", "event": {"kind": "saved", ...}}
    server -> client  {"type": "question", "id": "1", "qid": "1.1",
                       "question": {"kind": "change_target", ...}}
    client -> server  {"type": "answer", "qid": "1.1", "value": true}
    client -> server  {"type": "cancel", "id": "1"}
    server -> client  {"type": "result", "id": "1", "result": {...}}
                  or  {"type": "error", "id": "1", "error": {"code": "...", "message": "..."}}

Rules that keep a front end from hanging or guessing:

- One request at a time; a second one while the first runs gets error "busy".
- A question waits for its answer. An answer of the wrong type, or the client closing its
  input, answers it with the engine's safe default (decisions.safe_default).
- Closing the input also cancels the running request; the server exits when it is done.
  A client that stops reading (its end of the output closed) cancels the request too.
- Lines that are not JSON objects, unknown message types and unknown methods get an error
  (with the request id when there is one) and never stop the server.
- Error codes: bad_message, unknown_method, bad_params, busy, cancelled, internal.

PROTOCOL (modkeel.core.events) names this vocabulary; it changes when a field is renamed or
removed, never when one is added.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, is_dataclass
from typing import Any, Callable, Dict, Optional, TextIO

from modkeel.core.decisions import (
    Cancelled,
    ChangeTarget,
    NeedToken,
    Question,
    safe_default,
)
from modkeel.core.events import PROTOCOL, Event, _plain

# A method: (params, events, decide, cancelled) -> JSON-ready result.
Handler = Callable[[Dict[str, Any], Callable, Callable, Callable[[], bool]], Dict[str, Any]]


def encode(message: Dict[str, Any]) -> str:
    """One protocol line (no newline), pure ASCII: anything else is a JSON \\u escape.

    The stream's encoding then never matters. On Windows a pipe defaults to the ANSI code
    page (cp1252), which cannot encode the emoji in progress lines; with raw UTF-8 text the
    write failed and the run stopped.
    """
    return json.dumps(message, ensure_ascii=True, separators=(",", ":"))


def question_dict(question: Question) -> Dict[str, Any]:
    """A question on the wire. A proposed target carries its summary (the sentence to show)."""
    data = {"kind": question.kind, **_plain(asdict(question))}
    option = getattr(question, "option", None)
    if option is not None and hasattr(option, "summary"):
        data["option"]["summary"] = option.summary
    return data


def accepted_answer(question: Question, value: Any) -> Any:
    """The client's answer if it has the type the question needs, else the safe default."""
    if isinstance(question, ChangeTarget):
        return value if isinstance(value, bool) else safe_default(question)
    if isinstance(question, NeedToken):
        return value if isinstance(value, str) and value else safe_default(question)
    return safe_default(question)


def get_result_dict(result) -> Dict[str, Any]:
    """engine.GetResult on the wire."""
    from modkeel.core.engine import _delivery

    delivery = _delivery(result.delivered)
    proposal = result.proposal
    return {
        "mod": result.mod.title,
        "identified": result.mod.project is not None,
        "target": result.target,
        "output_dir": str(result.output_dir),
        "retargeted": result.retargeted,
        "delivered": _plain(asdict(delivery)) if delivery else None,
        "proposal": ({**_plain(asdict(proposal)), "summary": proposal.summary}
                     if proposal is not None and is_dataclass(proposal) else None),
    }


def _get(params, events, decide, cancelled) -> Dict[str, Any]:
    from modkeel.core.engine import GetRequest, get_mod

    request = GetRequest(**params)   # unknown or missing fields: TypeError -> bad_params
    if not (request.query and request.mc_version and request.loader):
        raise TypeError("query, mc_version and loader are required")
    return get_result_dict(get_mod(request, events=events, decide=decide,
                                   cancelled=cancelled))


METHODS: Dict[str, Handler] = {"get": _get}


class _Run:
    """The request in progress: its cancel flag and the question waiting for an answer."""

    def __init__(self, request_id: str):
        self.id = request_id
        self.cancelled = threading.Event()
        self.questions = 0
        self.waiting: Dict[str, Dict[str, Any]] = {}   # qid -> {"done", "value", "question"}


class Server:
    """Serves the protocol over a pair of text streams (stdin/stdout, or pipes in tests)."""

    def __init__(self, reader: TextIO, writer: TextIO,
                 methods: Optional[Dict[str, Handler]] = None):
        self.reader, self.writer = reader, writer
        self.methods = METHODS if methods is None else methods
        self._lock = threading.Lock()
        self._run: Optional[_Run] = None
        self._worker: Optional[threading.Thread] = None
        self.gone = False                 # the client stopped reading our output

    def send(self, message: Dict[str, Any]) -> None:
        """Write one line. If the client stopped reading (closed pipe), the run is cancelled
        and later lines are dropped: nobody is left to show them to."""
        with self._lock:
            if self.gone:
                return
            try:
                self.writer.write(encode(message) + "\n")
                self.writer.flush()
            except (BrokenPipeError, ConnectionError, OSError):   # the reader went away
                self.gone = True
            except ValueError as e:   # a closed file; anything else is a bug, not a client
                if "closed file" not in str(e):
                    raise
                self.gone = True
        if self.gone and self._run is not None:
            self._run.cancelled.set()
            self._release(self._run)

    def serve(self) -> None:
        """Until the input closes: read messages, run requests, route answers and cancels."""
        from modkeel.constants import MODKEEL_VERSION

        self.send({"type": "hello", "protocol": PROTOCOL, "modkeel": MODKEEL_VERSION,
                   "methods": sorted(self.methods)})
        for line in self.reader:
            if line.strip():
                self._handle(line)
        self._close()

    def _handle(self, line: str) -> None:
        try:
            message = json.loads(line)
        except json.JSONDecodeError as e:
            return self._error(None, "bad_message", f"not JSON: {e}")
        if not isinstance(message, dict):
            return self._error(None, "bad_message", "a message is a JSON object")
        kind = message.get("type")
        if kind == "request":
            self._start(message)
        elif kind == "answer":
            self._answer(message)
        elif kind == "cancel":
            run = self._run
            if run is not None and run.id == str(message.get("id")):
                run.cancelled.set()
                self._release(run)
        else:
            self._error(message.get("id"), "bad_message", f"unknown message type {kind!r}")

    def _start(self, message: Dict[str, Any]) -> None:
        request_id = message.get("id")
        if not isinstance(request_id, (str, int)) or isinstance(request_id, bool):
            return self._error(None, "bad_message", "a request needs an id (string or number)")
        request_id = str(request_id)
        method = self.methods.get(message.get("method"))
        if method is None:
            return self._error(request_id, "unknown_method",
                               f"unknown method {message.get('method')!r}; "
                               f"known: {', '.join(sorted(self.methods))}")
        params = message.get("params", {})
        if not isinstance(params, dict):
            return self._error(request_id, "bad_params", "params is a JSON object")
        if self._worker is not None and self._worker.is_alive():
            return self._error(request_id, "busy", "one request at a time")
        run = self._run = _Run(request_id)
        self._worker = threading.Thread(target=self._execute, args=(run, method, params),
                                        daemon=True)
        self._worker.start()

    def _execute(self, run: _Run, method: Handler, params: Dict[str, Any]) -> None:
        def events(event: Event) -> None:
            self.send({"type": "event", "id": run.id, "event": event.to_dict()})

        def decide(question: Question) -> Any:
            if run.cancelled.is_set():
                return safe_default(question)
            run.questions += 1
            qid = f"{run.id}.{run.questions}"
            slot = {"done": threading.Event(), "value": None, "question": question}
            run.waiting[qid] = slot
            self.send({"type": "question", "id": run.id, "qid": qid,
                       "question": question_dict(question)})
            slot["done"].wait()
            run.waiting.pop(qid, None)
            return slot["value"]

        try:
            result = method(dict(params), events, decide, run.cancelled.is_set)
        except Cancelled:
            self._error(run.id, "cancelled", "the request was cancelled")
        except TypeError as e:
            self._error(run.id, "bad_params", str(e))
        except Exception as e:  # the server outlives any one request
            self._error(run.id, "internal", f"{type(e).__name__}: {e}")
        else:
            self.send({"type": "result", "id": run.id, "result": result})

    def _answer(self, message: Dict[str, Any]) -> None:
        run = self._run
        slot = run.waiting.get(str(message.get("qid"))) if run else None
        if slot is None:
            return self._error(None, "bad_message", f"no question {message.get('qid')!r}")
        slot["value"] = accepted_answer(slot["question"], message.get("value"))
        slot["done"].set()

    def _release(self, run: _Run) -> None:
        """Answer every pending question with its safe default (cancel, end of input)."""
        for slot in list(run.waiting.values()):
            slot["value"] = safe_default(slot["question"])
            slot["done"].set()

    def _close(self) -> None:
        run, worker = self._run, self._worker
        if run is not None and worker is not None and worker.is_alive():
            run.cancelled.set()
            self._release(run)
            worker.join()

    def _error(self, request_id, code: str, text: str) -> None:
        message: Dict[str, Any] = {"type": "error"}
        if request_id is not None:
            message["id"] = str(request_id)
        message["error"] = {"code": code, "message": text}
        self.send(message)
