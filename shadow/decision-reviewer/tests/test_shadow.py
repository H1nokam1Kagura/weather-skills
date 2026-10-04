"""Stub backend, shadow gating, logging, record-outcome, fail-closed backends, CLI."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from decision_shadow import backends, cli, shadow
from decision_shadow.shadow import read_log, record_outcome, shadow_score

ESC_STATE = "Plot rainfall somewhere in east Africa recently, not sure which product?"
CALM_STATE = "Fetch CHIRPS precip for Kenya 2026-03-01 to 2026-05-31 and plot seasonal totals"


# ------------------------------------------------------------------ stub
def test_stub_is_deterministic_and_typed():
    a = backends.score("escalate", ESC_STATE, backend="stub")
    b = backends.score("escalate", ESC_STATE, backend="stub")
    assert (a["choice"], a["probs"]) == (b["choice"], b["probs"])
    assert a["choice"] == "escalate" and a["probs"]["escalate"] > 0.5
    assert abs(sum(a["probs"].values()) - 1) < 1e-6
    assert a["backend"] == "stub" and a["model_sha"] == backends.STUB_VERSION
    assert backends.score("escalate", CALM_STATE, backend="stub")["choice"] == "proceed"


def test_stub_review_and_next_skill():
    r = backends.score("review_verdict", "pipeline: fetch -> plot. rules ok.", backend="stub")
    assert r["choice"] == "approve" and set(r["probs"]) == {"approve", "reject"}
    r = backends.score("review_verdict", "pipeline violates rule: plot before convert-to-totals", backend="stub")
    assert r["choice"] == "reject"
    n = backends.score("next_skill", "goal", ["select", "clip-region", "plot"], backend="stub")
    assert n["choice"] == "clip-region"                      # alphabetically first valid skill
    assert set(n["probs"]) == {"select", "clip-region", "plot"}
    assert abs(sum(n["probs"].values()) - 1) < 1e-6


def test_next_skill_needs_candidates():
    with pytest.raises(ValueError):
        backends.score("next_skill", "goal", [], backend="stub")


# ------------------------------------------------------------------ gating + logging
def test_off_by_default_logs_nothing(shadow_log):
    assert shadow_score("escalate", ESC_STATE) is None
    assert not shadow_log.exists()


def test_env_enables_and_logs_hash_not_text(shadow_log, monkeypatch):
    monkeypatch.setenv("WS_DECISION_SHADOW", "stub")
    did = shadow_score("escalate", ESC_STATE)
    (rec,) = read_log(shadow_log)
    assert rec["decision_id"] == did and rec["kind"] == "decision" and rec["status"] == "ok"
    assert rec["input_sha256"] == shadow.input_hash("escalate", ESC_STATE, None)
    assert "text" not in rec and ESC_STATE not in shadow_log.read_text(encoding="utf-8")
    for k in ("ts", "decision_point", "backend", "choice", "probs", "latency_ms", "model_sha"):
        assert k in rec


def test_log_text_opt_in(shadow_log, monkeypatch):
    monkeypatch.setenv("WS_DECISION_SHADOW", "stub")
    monkeypatch.setenv("WS_DECISION_SHADOW_LOG_TEXT", "1")
    shadow_score("escalate", ESC_STATE)
    assert read_log(shadow_log)[0]["text"] == ESC_STATE


def test_inline_actual_and_record_outcome(shadow_log, monkeypatch):
    monkeypatch.setenv("WS_DECISION_SHADOW", "stub")
    d1 = shadow_score("escalate", ESC_STATE, actual="yes")
    d2 = shadow_score("review_verdict", "fine", actual="approved")
    assert record_outcome(d2, "reject", "review_verdict") is True
    assert record_outcome(d1, "maybe", "escalate") is False     # invalid outcome: refused, not raised
    rows = read_log(shadow_log)
    assert rows[0]["actual"] == "escalate" and rows[1]["actual"] == "approve"
    assert rows[2] == {**rows[2], "kind": "outcome", "decision_id": d2, "actual": "reject"}
    assert len(rows) == 3


def test_bad_input_is_logged_unavailable_never_raised(shadow_log):
    did = shadow_score("not-a-point", "x", backend="stub")
    (rec,) = read_log(shadow_log)
    assert rec["decision_id"] == did and rec["status"] == "unavailable" and rec["choice"] is None


def test_background_returns_immediately_and_logs(shadow_log):
    did = shadow.shadow_score_background("escalate", ESC_STATE, backend="stub")
    for t in threading.enumerate():
        if t is not threading.current_thread() and t.daemon:
            t.join(timeout=5)
    assert any(r["decision_id"] == did for r in read_log(shadow_log))


def test_torn_log_line_is_skipped(shadow_log, monkeypatch):
    monkeypatch.setenv("WS_DECISION_SHADOW", "stub")
    shadow_score("escalate", ESC_STATE)
    with open(shadow_log, "a", encoding="utf-8") as f:
        f.write('{"kind": "decis')
    assert len(read_log(shadow_log)) == 1


# ------------------------------------------------------------------ fail-closed backends
@pytest.mark.parametrize("backend", ["kev", "clm"])
def test_http_backends_fail_closed_without_endpoint(shadow_log, backend):
    with pytest.raises(backends.BackendUnavailable):
        backends.score("escalate", ESC_STATE, backend=backend)
    did = shadow_score("escalate", ESC_STATE, backend=backend)
    (rec,) = read_log(shadow_log)
    assert rec["decision_id"] == did and rec["status"] == "unavailable"
    assert f"{backend.upper()}_URL is not set" in rec["error"]


@pytest.mark.parametrize("backend", ["kev", "clm"])
def test_http_backends_fail_closed_when_endpoint_down(shadow_log, monkeypatch, backend):
    monkeypatch.setenv(f"{backend.upper()}_URL", "http://127.0.0.1:9")    # discard port: refused
    monkeypatch.setenv(f"{backend.upper()}_TIMEOUT_S", "1")
    shadow_score("escalate", ESC_STATE, backend=backend)
    (rec,) = read_log(shadow_log)
    assert rec["status"] == "unavailable" and "unreachable" in rec["error"]


class _FakeSystemOne(BaseHTTPRequestHandler):
    """Mimics clm-serve / kev.serve POST /v1/systemone (shapes from their src/*/schema.py, api.py)."""
    seen: list = []
    status = 200

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        type(self).seen.append({"path": self.path, "auth": self.headers.get("authorization"), "body": body})
        if type(self).status != 200:
            self.send_response(type(self).status)
            self.end_headers()
            return
        answers = {}
        for qid, q in body["questions"].items():
            if q["type"] == "noul":
                answers[qid] = {"type": "noul", "noul": 0.83}
            else:
                keys = list(q["criteria"])
                p = [0.7] + [0.3 / (len(keys) - 1)] * (len(keys) - 1)
                answers[qid] = {"type": "choice", "choice": keys[0], "confidence": 0.6,
                                "probabilities": dict(zip(keys, p))}
        out = json.dumps({"model": body["model"], "answers": answers, "usage": {}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


@pytest.fixture
def fake_server():
    _FakeSystemOne.seen = []
    _FakeSystemOne.status = 200
    srv = HTTPServer(("127.0.0.1", 0), _FakeSystemOne)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_clm_mocked_endpoint_request_response_mapping(fake_server, monkeypatch):
    monkeypatch.setenv("CLM_URL", fake_server)
    monkeypatch.setenv("CLM_API_KEY", "test-key-not-secret")
    out = backends.score("escalate", ESC_STATE, backend="clm")
    assert out["choice"] == "escalate" and out["probs"] == {"escalate": 0.83, "proceed": 0.17}
    req = _FakeSystemOne.seen[-1]
    assert req["path"] == "/v1/systemone" and req["auth"] == "Bearer test-key-not-secret"
    assert req["body"]["model"] == "clm-latest" and req["body"]["state"] == ESC_STATE
    assert req["body"]["questions"]["decision"]["type"] == "noul"

    out = backends.score("next_skill", "goal", {"select": "pick a var", "plot": None}, backend="clm")
    assert out["choice"] == "select" and set(out["probs"]) == {"select", "plot"}
    q = _FakeSystemOne.seen[-1]["body"]["questions"]["decision"]
    assert q == {"type": "choice", "instructions": q["instructions"], "criteria": {"select": "pick a var", "plot": None}}

    out = backends.score("review_verdict", "pipeline ...", backend="clm")
    assert out["choice"] == "approve" and set(out["probs"]) == {"approve", "reject"}


def test_clm_scale_to_zero_503_is_unavailable(fake_server, shadow_log, monkeypatch):
    monkeypatch.setenv("CLM_URL", fake_server)
    _FakeSystemOne.status = 503
    shadow_score("escalate", ESC_STATE, backend="clm")
    (rec,) = read_log(shadow_log)
    assert rec["status"] == "unavailable" and "HTTP 503" in rec["error"]


def test_api_key_never_logged(fake_server, shadow_log, monkeypatch):
    monkeypatch.setenv("CLM_URL", fake_server)
    monkeypatch.setenv("CLM_API_KEY", "sekrit-value-123")
    shadow_score("escalate", ESC_STATE, backend="clm")
    assert "sekrit-value-123" not in shadow_log.read_text(encoding="utf-8")


# ------------------------------------------------------------------ CLI
def test_cli_score_hides_choice_and_always_exits_zero(shadow_log, capsys):
    assert cli.main(["score", "--point", "escalate", "--state", ESC_STATE, "--backend", "stub"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out) == {"decision_id", "shadow"} and out["shadow"] == "stub"
    assert cli.main(["score", "--point", "escalate", "--state", "x", "--backend", "kev"]) == 0
    capsys.readouterr()
    assert cli.main(["record-outcome", "--id", out["decision_id"], "--actual", "yes", "--point", "escalate"]) == 0
    capsys.readouterr()
    assert cli.main(["report"]) == 0
    text = capsys.readouterr().out
    assert "escalate" in text and "advisory only; insufficient n" in text


def test_cli_off_by_default(shadow_log, capsys):
    assert cli.main(["score", "--point", "escalate", "--state", ESC_STATE]) == 0
    assert json.loads(capsys.readouterr().out) == {"decision_id": None, "shadow": "off"}
    assert not shadow_log.exists()
