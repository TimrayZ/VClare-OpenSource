"""Local web server for the VClare human arbitration console.

Two modes are supported:

``demo`` (default)
    Serve the synthetic demo questions. Decisions are stored in
    ``webui/decisions.json``.

``--state-dir <pipeline run dir>``
    Attach to a running multi-stage pipeline. Questions are read from
    ``<run>/arbitration_pending.json`` and answers are appended to
    ``<run>/arbitration_decisions.json``, which is exactly the result JSON the
    pipeline reads when it resumes.

Run from anywhere::

    python webui/server.py --port 8770
    python webui/server.py --port 8770 --state-dir saves/experiments/run_001
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

WEBUI_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = WEBUI_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from vclare.arbiter import (  # noqa: E402
    ArbitrationQuestion,
    HumanArbiter,
    question_from_dict,
)
from vclare.sim_repair import Candidate, SimRepair  # noqa: E402
from vclare.spec_repair import InconsistencyPair, SpecRepair  # noqa: E402

DEMO_FILE = PROJECT_ROOT / "examples" / "demo_cases.json"
DEMO_DECISIONS_FILE = WEBUI_DIR / "decisions.json"
PENDING_NAME = "arbitration_pending.json"
DECISIONS_NAME = "arbitration_decisions.json"

STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
}

_lock = threading.Lock()
_questions: Dict[str, ArbitrationQuestion] = {}
_order: List[str] = []
_state_dir: Optional[Path] = None


# --------------------------------------------------------------------- sources
def _load_demo() -> Dict[str, Any]:
    with open(DEMO_FILE, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _candidate_from_dict(payload: Dict[str, Any]) -> Candidate:
    return Candidate(
        candidate_id=payload["candidate_id"],
        source=payload["source"],
        outputs=payload.get("outputs", {}),
        compiled=bool(payload.get("compiled", True)),
        meta=payload.get("meta", {}),
    )


def build_demo_questions() -> List[Dict[str, Any]]:
    """Build the two demo confirmation questions from the synthetic dataset."""
    demo = _load_demo()
    questions: List[Dict[str, Any]] = []

    for case in demo.get("spec_level", []):
        pairs = case.get("pairs", [])
        total = len(pairs)
        for pair_payload in pairs:
            pair = InconsistencyPair(
                a1=pair_payload.get("a1", ""),
                a2=pair_payload.get("a2", ""),
                defect_type=pair_payload.get("defect_type", ""),
                rationale=pair_payload.get("rationale", ""),
                index=int(pair_payload.get("index", 1)),
            )
            if pair.is_standalone:
                continue
            question = SpecRepair.build_question(case["task_id"], pair, total)
            payload = question.to_dict()
            payload["context"]["spec"] = case.get("spec", "")
            payload["expected_decision"] = case.get("expected_decision", "")
            questions.append(payload)

    for case in demo.get("sim_level", []):
        repair = SimRepair(case.get("test_cases", []))
        candidates = [_candidate_from_dict(item) for item in case.get("candidates", [])]
        clusters = repair.cluster(candidates)
        question = repair.build_question(
            case["task_id"], clusters, defect_type=case.get("defect_type", "")
        )
        if question is None:
            continue
        payload = question.to_dict()
        payload["context"]["spec"] = case.get("spec", "")
        payload["context"]["test_case_details"] = case.get("test_case_details", {})
        payload["expected_decision"] = case.get("expected_decision", "")
        questions.append(payload)

    return questions


def _reset_registry() -> None:
    _questions.clear()
    _order.clear()
    for payload in build_demo_questions():
        question = question_from_dict(payload)
        _questions[question.question_id] = question
        _order.append(question.question_id)


def _state_pending() -> List[Dict[str, Any]]:
    if _state_dir is None:
        return []
    path = _state_dir / PENDING_NAME
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return []
    return list(payload.get("questions", []))


def _active_questions() -> List[Dict[str, Any]]:
    if _state_dir is not None:
        return _state_pending()
    return [_questions[qid].to_dict() for qid in _order]


def _decisions_path() -> Path:
    if _state_dir is not None:
        return _state_dir / DECISIONS_NAME
    return DEMO_DECISIONS_FILE


def _read_decisions() -> List[Dict[str, Any]]:
    path = _decisions_path()
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(payload, dict):
        return list(payload.get("decisions", []))
    return list(payload)


def _write_decisions(decisions: List[Dict[str, Any]]) -> None:
    path = _decisions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(
            {"version": 1, "updated_at": time.time(), "decisions": decisions},
            handle,
            indent=2,
            ensure_ascii=False,
        )


def _enrich(questions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    enriched = []
    for payload in questions:
        item = dict(payload)
        context = dict(item.get("context", {}))
        ui = context.pop("_ui", {})
        item["context"] = context
        item["spec"] = ui.get("spec") or context.get("spec", "")
        item["test_case_details"] = (
            ui.get("test_case_details") or context.get("test_case_details", {})
        )
        enriched.append(item)
    return enriched


# ---------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "VClareArbitration/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        sys.stderr.write("[webui] " + fmt % args + "\n")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _read_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw else {}

    # --------------------------------------------------------------------- GET
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path

        if path in STATIC_FILES:
            filename, content_type = STATIC_FILES[path]
            data = (WEBUI_DIR / filename).read_bytes()
            self._send(200, data, content_type)
            return

        if path == "/api/questions":
            with _lock:
                questions = _enrich(_active_questions())
                decisions = _read_decisions()
            resolved = {d.get("question_id") for d in decisions}
            self._send_json(
                200,
                {
                    "questions": questions,
                    "decisions": decisions,
                    "source": "pipeline" if _state_dir is not None else "demo",
                    "state_dir": str(_state_dir) if _state_dir else "",
                    "summary": {
                        "total": len(questions),
                        "resolved": len(
                            [
                                q
                                for q in questions
                                if q.get("question_id") in resolved
                            ]
                        ),
                    },
                },
            )
            return

        if path == "/api/decisions":
            with _lock:
                self._send_json(200, {"decisions": _read_decisions()})
            return

        self._send_json(404, {"error": "not found"})

    # -------------------------------------------------------------------- POST
    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            payload = self._read_body()
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid JSON body"})
            return

        if path == "/api/decision":
            self._decide(payload)
            return

        if path == "/api/clear":
            with _lock:
                _write_decisions([])
            self._send_json(200, {"decisions": []})
            return

        if path == "/api/reset":
            with _lock:
                _reset_registry()
            self._send_json(200, {"ok": True})
            return

        self._send_json(404, {"error": "not found"})

    def _decide(self, payload: Dict[str, Any]) -> None:
        question_id = payload.get("question_id", "")
        value = payload.get("value", "")

        with _lock:
            question_payload = next(
                (
                    item
                    for item in _active_questions()
                    if item.get("question_id") == question_id
                ),
                None,
            )
            if question_payload is None:
                self._send_json(404, {"error": "unknown question_id"})
                return

            allowed = [option["value"] for option in question_payload["options"]]
            if value not in allowed:
                self._send_json(400, {"error": "invalid value", "allowed": allowed})
                return

            option = next(
                option for option in question_payload["options"] if option["value"] == value
            )
            decision = {
                "question_id": question_id,
                "kind": question_payload.get("kind", ""),
                "task_id": question_payload.get("task_id", ""),
                "value": value,
                "label": option.get("label", value),
                "options": allowed,
                "defect_type": question_payload.get("defect_type", ""),
                "source": payload.get("source", "human"),
                "notes": payload.get("notes", ""),
                "context": question_payload.get("context", {}),
                "timestamp": time.time(),
            }
            decisions = [
                item
                for item in _read_decisions()
                if item.get("question_id") != question_id
            ]
            decisions.append(decision)
            _write_decisions(decisions)
        self._send_json(200, {"decision": decision})


def main() -> int:
    global _state_dir

    parser = argparse.ArgumentParser(description="VClare arbitration console")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--open", action="store_true", help="open the browser")
    parser.add_argument(
        "--state-dir",
        default=None,
        help="attach to a pipeline run directory instead of the demo data",
    )
    args = parser.parse_args()

    if args.state_dir:
        _state_dir = Path(args.state_dir).resolve()
        if not _state_dir.exists():
            print("[webui] state dir does not exist: {}".format(_state_dir), file=sys.stderr)
            return 2
    _reset_registry()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = "http://{}:{}/".format(args.host, args.port)
    print("[webui] VClare arbitration console: {}".format(url))
    if _state_dir:
        print("[webui] pipeline run   : {}".format(_state_dir))
        print("[webui] pending file   : {}".format(_state_dir / PENDING_NAME))
        print("[webui] decisions file : {}".format(_state_dir / DECISIONS_NAME))
    else:
        print("[webui] demo data      : {}".format(DEMO_FILE))
        print("[webui] decisions      : {}".format(DEMO_DECISIONS_FILE))
    if args.open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[webui] stopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
