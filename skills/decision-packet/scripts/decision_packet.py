# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@a4110e30c8637ea99d79f752499d00e4cd65fafb",
# ]
# ///
"""Gate everything that crosses to or from the human: check a decision packet is complete, render
it as a review page, and parse the human's reply back into typed state.

Deterministic: no model call, no network. Modes and exit codes are in SKILL.md.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import re
from pathlib import Path

from weather_skills_core import SkillError, UsageError, weather_skill

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

HERE = Path(__file__).resolve().parent
VENDOR_UI = HERE.parent / "vendor" / "build_review_ui.py"
GOAL_CHECK = HERE.parents[1] / "goal-check" / "scripts" / "goal_check.py"
MAX_CHAT_QUESTIONS = 2
MAX_OPTIONS = 8  # the review page refuses more: past that it is a search problem, not a decision
PRESELECT_KEYS = ("preselected", "current", "selected", "recommended")
CANT_TELL = "CANT_TELL"
CSV_HEADER = ["packet_id", "slot", "question", "DECISION", "correction_notes"]
_INSUFFICIENT = re.compile(
    r"\b(can'?t tell|cannot tell|not sure|unsure|don'?t know|do not know|no idea|"
    r"need more (info|information|detail|details|context)|not enough (info|information)|"
    r"what do you mean|unclear|depends)\b",
    re.I,
)


class Incomplete(SkillError):
    """A packet is missing an element. Exit 1: go and get it; never send an under-specified ask."""

    exit_code = 1


class RuleDecidable(SkillError):
    """A rule or check already settles the question. Exit 4: do not ask the human."""

    exit_code = 4


class NeedsClarification(SkillError):
    """The reply is ambiguous or says the information was insufficient. Exit 3."""

    exit_code = 3


def _load(path: Path, name: str):
    if not path.is_file():
        raise SkillError(f"required sibling file missing: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _goal_check():
    return _load(GOAL_CHECK, "rhiza_goal_check")


def _blank(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip()) or v == [] or v == {}


# ------------------------------------------------------------------------------------- check ---
def check_packet(p: dict) -> dict:
    """Completeness + premature-ask guard. Returns {status, missing[], rule_decidable[]}."""
    missing: list[str] = []
    decidable: list[str] = []
    if not isinstance(p, dict):
        return {"id": None, "status": "incomplete", "missing": ["packet"], "rule_decidable": []}
    q = p.get("question")
    if _blank(q):
        missing.append("question")
    elif q.count("?") != 1 or not q.rstrip().endswith("?"):
        missing.append("question: must be exactly one question, ending in '?'")

    opts = p.get("options")
    if not isinstance(opts, list) or not opts:
        missing.append("options")
        opts = []
    elif len(opts) == 1:
        decidable.append("only one option: there is nothing for the human to choose")
    elif len(opts) > MAX_OPTIONS:
        missing.append(f"options: {len(opts)} > {MAX_OPTIONS}; narrow them before asking")
    codes = []
    for i, o in enumerate(opts):
        if not isinstance(o, dict):
            missing.append(f"options[{i}]")
            continue
        for k in ("code", "label", "definition", "downstream"):
            if _blank(o.get(k)):
                missing.append(f"options[{i}].{k}")
        if not _blank(o.get("definition")) and not _blank(o.get("label")):
            if str(o["definition"]).strip().casefold() == str(o["label"]).strip().casefold():
                missing.append(f"options[{i}].definition: repeats the label instead of defining it")
        ev = o.get("evidence")
        if not isinstance(ev, list) or not ev:
            missing.append(f"options[{i}].evidence")
        else:
            for j, e in enumerate(ev):
                if not isinstance(e, dict) or _blank(e.get("text")):
                    missing.append(f"options[{i}].evidence[{j}].text")
                if not isinstance(e, dict) or _blank(e.get("source")):
                    missing.append(f"options[{i}].evidence[{j}].source")
        codes.append(o.get("code"))
        if any(o.get(k) for k in PRESELECT_KEYS):
            missing.append(f"nothing pre-selected: options[{i}] is marked pre-selected")
    if len(codes) != len(set(map(str, codes))):
        missing.append("options: duplicate codes")
    if CANT_TELL in map(str, codes):
        missing.append(f"options: {CANT_TELL!r} is reserved for 'I can't tell from this'")
    if any(p.get(k) for k in PRESELECT_KEYS):
        missing.append("nothing pre-selected: the packet names a pre-selected answer")

    d = p.get("default")
    if not isinstance(d, dict):
        missing.append("default")
    else:
        if _blank(d.get("if_no_reply")):
            missing.append("default.if_no_reply")
        if d.get("option") is not None:
            if str(d["option"]) not in map(str, codes):
                missing.append("default.option: not one of the option codes")
            if _blank(d.get("source")):
                missing.append("default.source")

    checks = p.get("checks_run")
    if not isinstance(checks, list) or not checks:
        missing.append("checks_run: name the cheap checks already run, so a rule cannot settle it")
    else:
        for i, c in enumerate(checks):
            if not isinstance(c, dict) or _blank(c.get("check")):
                missing.append(f"checks_run[{i}].check")
                continue
            if _blank(c.get("result")):
                missing.append(f"checks_run[{i}].result")
            if not isinstance(c.get("settled"), bool):
                missing.append(f"checks_run[{i}].settled (true/false)")
            elif c["settled"]:
                decidable.append(f"check '{c['check']}' already settled it: {c.get('result')}")

    if p.get("kind") == "goal_slot":
        if _blank(p.get("slot")):
            missing.append("slot")
        if _blank(p.get("request")):
            missing.append("request: a goal question must carry the human's own words")
        else:
            settled = _goal_check().rule_settles(p.get("slot"), p["request"], p.get("goal"))
            if settled is not None:
                decidable.append(
                    f"a deterministic rule settles {p.get('slot')} = {settled!r} for this request"
                )
    status = "rule_decidable" if decidable else ("incomplete" if missing else "ready")
    return {"id": p.get("id"), "status": status, "missing": missing, "rule_decidable": decidable}


# -------------------------------------------------------------------------------------- html ---
def _body_text(p: dict) -> str:
    L = []
    if p.get("context"):
        L += [p["context"], ""]
    for n, o in enumerate(p["options"], 1):
        L.append(f"{n}. {o['label']}")
        L.append(f"   What it means: {o['definition']}")
        for e in o["evidence"]:
            L.append(f"   Evidence: {e['text']}  [source: {e['source']}]")
        L.append(f"   What changes if you choose it: {o['downstream']}")
        L.append("")
    d = p["default"]
    L.append(f"IF YOU DO NOTHING: {d['if_no_reply']}")
    if d.get("option") is not None:
        lab = next(o["label"] for o in p["options"] if str(o["code"]) == str(d["option"]))
        L.append(f"Default: {lab} (source: {d.get('source')})")
    L += ["", "ALREADY CHECKED, SO YOU ARE NOT ASKED WHAT A RULE CAN SETTLE:"]
    L += [f"- {c['check']}: {c['result']}" for c in p["checks_run"]]
    return "\n".join(L)


def to_spec(p: dict) -> dict:
    """Map a ready packet onto the vendored review tool's spec (ground_truth: nothing pre-set)."""
    digest = hashlib.sha256(json.dumps(p, sort_keys=True).encode()).hexdigest()[:8]
    item = {
        "id": p["id"],
        "title": p.get("title") or p["question"],
        "meta": [{"label": "about", "text": p.get("slot") or p.get("kind") or "decision"}],
        "body": {
            "heading": "the options, the evidence and what each one changes",
            "text": _body_text(p),
        },
        "row": {
            "packet_id": p["id"],
            "slot": p.get("slot") or "",
            "question": p["question"],
            "DECISION": "",
            "correction_notes": "",
        },
    }
    if p.get("request"):
        item["claim"] = {"heading": "your request", "quote": [p["request"]]}
    return {
        "title": p.get("title") or "A decision only you can make",
        "batch": f"rhiza-{p['id']}-{digest}",
        "question": p["question"],
        "options": [
            {"code": str(o["code"]), "label": o["label"], "definition": o["definition"]}
            for o in p["options"]
        ],
        "defer": {
            "code": CANT_TELL,
            "label": "I can't tell from this",
            "definition": "What is shown is not enough to decide. Say in the note what is "
            "missing; the question is improved before you are asked again.",
        },
        "note": "anything we should know, or what is missing (optional)",
        "purpose": "ground_truth",
        "csv": {
            "header": CSV_HEADER,
            "decision_column": "DECISION",
            "correction_column": "correction_notes",
            "filename": f"decision-{p['id']}.csv",
        },
        "items": [item],
    }


def build_pages(packets: list[dict], out_dir: Path) -> list[dict]:
    ui = _load(VENDOR_UI, "rhiza_build_review_ui")
    out_dir.mkdir(parents=True, exist_ok=True)
    built = []
    for p in packets:
        spec = to_spec(p)
        (out_dir / f"review_spec_{p['id']}.json").write_text(
            json.dumps(spec, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        try:
            page = ui.build(spec, out_dir / f"decide_{p['id']}.html")
        except SystemExit as exc:  # the vendored tool refuses by SystemExit("REFUSED: ...")
            raise Incomplete(str(exc), prefix=False) from None
        built.append({"id": p["id"], "page": str(page), "csv": spec["csv"]["filename"]})
    return built


# ------------------------------------------------------------------------------------ import ---
def _norm(s) -> str:
    return re.sub(r"[^\w\s']", " ", str(s).casefold()).strip()


def match_reply(p: dict, reply: str) -> list[int]:
    """Indices of options the reply names: number, letter, code, label, or the label as a phrase."""
    r = _norm(reply)
    opts = p["options"]
    exact = [
        i
        for i, o in enumerate(opts)
        if r in (_norm(o["code"]), _norm(o["label"]), str(i + 1), "abcdefgh"[i])
        or r in (f"option {i + 1}", f"option {'abcdefgh'[i]}")
    ]
    if exact:
        return exact
    padded = f" {r} "
    return [
        i
        for i, o in enumerate(opts)
        if f" {_norm(o['label'])} " in padded or f" {_norm(o['code'])} " in padded
    ]


def _coerce(code: str):
    try:
        return json.loads(code)
    except (json.JSONDecodeError, TypeError):
        return code


def _clarify(p: dict, idx: list[int]) -> str:
    labels = [p["options"][i]["label"] for i in (idx or range(len(p["options"])))]
    listed = "; ".join(f"{n}) {lab}" for n, lab in enumerate(labels, 1))
    return f"Just to be sure, which one do you mean: {listed}? Reply with the number."


def interpret(p: dict, decision: str | None, note: str = "", from_csv: bool = False) -> dict:
    out = {"packet_id": p.get("id"), "slot": p.get("slot"), "reply": decision, "note": note}
    asked = int(p.get("clarifications_asked") or 0)
    if _blank(decision):
        return out | {"status": "unanswered", "exit_code": 3, "next_action": "wait"}
    if from_csv:
        idx = [i for i, o in enumerate(p["options"]) if str(o["code"]) == decision.strip()]
        insufficient = decision.strip() == CANT_TELL
        if not idx and not insufficient:
            return out | {
                "status": "not_an_option",
                "exit_code": 1,
                "error": f"DECISION {decision!r} is not one of the packet's codes",
            }
    else:
        idx = match_reply(p, decision)
        insufficient = bool(_INSUFFICIENT.search(decision)) and len(idx) != 1
    if insufficient:
        return out | {
            "status": "insufficient_information",
            "exit_code": 3,
            "insufficient_information": True,
            "next_action": "improve_packet: the human could not decide from what was shown; "
            "gather the missing evidence (see note) and re-check the packet before asking again",
        }
    if len(idx) != 1:
        if asked >= 1:
            return out | {
                "status": "unresolved_after_clarification",
                "exit_code": 3,
                "next_action": "hold: one clarifying question was already asked; offer the "
                "review page instead of asking again",
            }
        return out | {
            "status": "ambiguous",
            "exit_code": 3,
            "clarifying_question": _clarify(p, idx),
            "next_action": "ask this one clarifying question, then set clarifications_asked=1",
        }
    o = p["options"][idx[0]]
    value = _coerce(str(o["code"]))
    res = out | {
        "status": "answered",
        "exit_code": 0,
        "decision": str(o["code"]),
        "label": o["label"],
        "hedged": bool(_INSUFFICIENT.search(decision)) if not from_csv else False,
    }
    if p.get("kind") == "goal_slot" and p.get("slot"):
        slot, goal = p["slot"], p.get("goal")
        before = None if goal is None else goal.get(slot)
        res["goal_patch"] = {slot: value}
        res["echo"] = (
            f"No change: {slot} stays {o['label']}."
            if before == value
            else f"Changed {slot}: {'(not set)' if before is None else repr(before)} -> "
            f"{o['label']} ({value!r}). Nothing else changed."
        )
        if goal is not None:
            gc = _goal_check()
            patched = dict(goal) | {slot: value}
            chk = gc.check_goal(patched, p.get("request"))
            if chk["errors"]:
                return res | {
                    "status": "inconsistent",
                    "exit_code": 1,
                    "error": "the answer contradicts the rest of the goal: "
                    + "; ".join(chk["errors"]),
                }
            res["updated_goal"] = chk["goal"]
            res["still_unresolved"] = [m["slot"] for m in chk["missing"]]
    else:
        res["echo"] = f"Recorded: {o['label']}. Nothing else changed."
    return res


def read_csv(path: Path) -> dict[str, dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return {row.get("packet_id", ""): row for row in csv.DictReader(f)}


# --------------------------------------------------------------------------------------- cli ---
@weather_skill(name="decision-packet", version=_SKILL_VERSION, output=False)
@weather_skill.argument(
    "--mode",
    choices=["check", "html", "import"],
    default="check",
    help="check: completeness gate; html: build review pages; import: parse the human's reply.",
)
@weather_skill.argument(
    "--packet", action="append", required=True, metavar="PATH", help="Packet JSON (repeatable)."
)
@weather_skill.argument(
    "--channel",
    choices=["chat", "page"],
    default="chat",
    help="chat allows at most 2 questions per round; page allows a batch.",
)
@weather_skill.argument("--html-dir", default=None, metavar="PATH", help="html mode: output dir.")
@weather_skill.argument(
    "--decisions", default=None, metavar="PATH", help="import mode: the CSV the page exported."
)
@weather_skill.argument("--reply", default=None, help="import mode: the human's chat reply.")
def decision_packet(mode, packet, channel, html_dir, decisions, reply, **kwargs):
    """Gate a decision packet before the human sees it, render it as a page, parse the reply."""
    packets = []
    for path in packet:
        try:
            packets.append(json.loads(Path(path).read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            raise UsageError(f"cannot read packet {path}: {exc}") from None

    if mode == "import":
        if (decisions is None) == (reply is None):
            raise UsageError("import mode needs exactly one of --decisions or --reply")
        if reply is not None and len(packets) != 1:
            raise UsageError("--reply answers one packet; split a two-question reply per packet")
        rows = read_csv(Path(decisions)) if decisions else {}
        results = []
        for p in packets:
            if decisions:
                row = rows.get(str(p.get("id")), {})
                results.append(
                    interpret(
                        p, row.get("DECISION"), row.get("correction_notes") or "", from_csv=True
                    )
                )
            else:
                results.append(interpret(p, reply))
        print(json.dumps(results if len(results) > 1 else results[0], indent=1, ensure_ascii=False))
        worst = max(r["exit_code"] for r in results)
        if worst == 1:
            raise SkillError("reply not usable: see 'error' in the report", prefix=False)
        if worst == 3:
            raise NeedsClarification("reply needs follow-up: see 'next_action'", prefix=False)
        return

    reports = [check_packet(p) for p in packets]
    round_problem = None
    if channel == "chat" and len(packets) > MAX_CHAT_QUESTIONS:
        round_problem = (
            f"round: {len(packets)} questions at once; chat allows {MAX_CHAT_QUESTIONS}. "
            "Send the first two, or use --channel page"
        )
    if mode == "html" and html_dir is None:
        raise UsageError("html mode needs --html-dir")
    ready = all(r["status"] == "ready" for r in reports) and round_problem is None
    out = {"ready": ready, "packets": reports}
    if round_problem:
        out["round"] = round_problem
    if mode == "html" and ready:
        out["pages"] = build_pages(packets, Path(html_dir))
        out["import"] = (
            "after the human clicks Download decisions: decision_packet.py --mode import "
            + " ".join(f"--packet {p}" for p in packet)
            + " --decisions <downloaded csv>"
        )
    print(json.dumps(out, indent=1, ensure_ascii=False))
    if any(r["status"] == "rule_decidable" for r in reports):
        raise RuleDecidable(
            "BLOCKED, do not ask: " + "; ".join(x for r in reports for x in r["rule_decidable"]),
            prefix=False,
        )
    if not ready:
        named = [f"{r['id']}: {m}" for r in reports for m in r["missing"]]
        named += [round_problem] if round_problem else []
        raise Incomplete("BLOCKED, incomplete packet: " + "; ".join(named), prefix=False)


if __name__ == "__main__":
    decision_packet()
