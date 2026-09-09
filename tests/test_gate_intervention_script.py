"""The gate intervention script must produce a hook that runs, not just parses.

The fixture mirrors the shape of ``speak-response.mjs`` that matters: the CLI
dispatch calls ``gateMain`` at the top of the module, before later top-level
constants are initialized. The first version of the script placed its new
constants after that dispatch, so every post-approval prompt threw a
ReferenceError and halt words stopped withdrawing approval. This test drives
the patched fixture through the proof sequence with Node and would have
caught that.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "gate_intervention_2026_09_09.py"

FIXTURE = r"""
import { readFileSync, writeFileSync, mkdirSync, appendFileSync } from 'fs';
const STATE_DIR = process.env.STATE_DIR;
function lat(event, detail = '') { appendFileSync(process.env.LAT_FILE, `${Date.now()}	${event}	${detail}
`); }
const GATE_ALLOW = ['Read', 'Glob', 'Grep'];
const STRONG_OK = /^(go ahead|go for it|proceed|do it|do that|approved|approve[d]?|confirm(ed|ing)?|make it so|carry on|green light|ship it|sounds good|looks good|affirmative|permission granted|you may proceed|execute)\b/;
const WEAK_OK = /^(yes|yep|yeah|yup|ok|okay|sure|fine|alright|all right|go|continue|please do|please proceed)\b/;
const STRONG_OK_ANY = /\b(go ahead|go for it|do it|approved|make it so|green light|ship it|permission granted|you may proceed|carry on)\b/;
const REFUSAL = /\b(don'?t|do not|not|never|wait|hold off|hold on|stop|cancel|abort)\b/;
const HALT = /\b(stop|halt|wait|hold on|hold off|cancel|abort|don'?t|do not|never ?mind|undo|revert)\b/;

const [mode, ...rest] = process.argv.slice(2);
if (mode === 'gate') {
  gateMain(rest);
}

function gateStateFile(sid) { return process.env.GATE_FILE; }
function readGate(sid) { try { return JSON.parse(readFileSync(gateStateFile(sid), 'utf-8')); } catch { return null; } }
function writeGate(sessionId, state, extra = {}) {
  mkdirSync(STATE_DIR, { recursive: true });
  writeFileSync(gateStateFile(sessionId), JSON.stringify({ state, ts: Date.now(), ...extra }), 'utf-8');
}
function bareCommand(prompt) { return prompt.split('(Voice context:')[0].replace(/\s+/g, ' ').trim(); }
function isApproval(cmd) {
  const t = cmd.toLowerCase().replace(/^[\s,.!?:;-]+/, '').replace(/^(hey |ok |okay |alright |all right )?claude[,.!? ]+/, '').trim();
  if (!t) return false;
  const words = t.split(' ').length;
  if (STRONG_OK.test(t)) return true;
  if (WEAK_OK.test(t)) {
    if (words <= 6) return true;
    const after = t.replace(WEAK_OK, '').replace(/^[\s,.!-]+/, '');
    if (STRONG_OK.test(after)) return true;
  }
  const m = STRONG_OK_ANY.exec(t);
  return !!(m && words <= 24 && !isQuestion(t) && !REFUSAL.test(t.slice(0, m.index)));
}
function isQuestion(cmd) {
  const q = cmd.toLowerCase();
  return /\?\s*$/.test(q) || /^(what|what's|why|how|is|are|was|were|did|do|does|can|could|would|will|where|when|which|who|have|has|tell me)\b/.test(q);
}
function gateMain(args) {
  let data = {};
  try { data = JSON.parse(readFileSync(0, 'utf-8')); } catch { process.exit(0); }
  const sid = data.session_id;
  const ev = data.hook_event_name || (data.tool_name ? 'PreToolUse' : 'UserPromptSubmit');
  const midTurn = !!data.mid_turn;
  const cfg = {};
  const gateMode = 'all';
  if (ev === 'UserPromptSubmit') {
    const prompt = String(data.prompt || '');
    const cmd = bareCommand(prompt);
    if (!cmd || cmd.startsWith('/')) process.exit(0);
    const fromVoice = false;
    if (isApproval(cmd)) {
      writeGate(sid, 'approved', { cmd });
      console.log('Start-of-turn gate: Matt approved. Carry out the plan you stated, then report.');
      process.exit(0);
    }
    if (gateMode === 'voice' && !fromVoice) process.exit(0);
    if (readGate(sid) && isQuestion(cmd)) process.exit(0);
    if (/<(?:task-notification|cross-session-message)(?:\s|>)/.test(prompt)) process.exit(0);
    const g0 = readGate(sid);
    if (midTurn && g0 && g0.state === 'approved') {
      const low = cmd.toLowerCase().replace(/\bstop asking\b/g, '');
      if (!HALT.test(low)) {
        console.log('Start-of-turn gate: mid-turn message while the plan is approved; the gate stays open. Treat it as a steer on the running work, not a new plan request.');
        process.exit(0);
      }
    }
    writeGate(sid, 'pending', { cmd });
    console.log('Start-of-turn gate is armed for this prompt: every tool except read-only ones (Read, Grep, Glob, web search) will be denied until Matt approves. '
      + 'If the request needs actions, reply with a short numbered plan, end with a Spoken: line that states the plan and asks for a go-ahead, and end the turn. '
      + 'A question that needs no actions can just be answered.');
    process.exit(0);
  }
  if (ev === 'PreToolUse') {
    const g = readGate(sid);
    if (!g || g.state !== 'pending') process.exit(0);
    const tool = String(data.tool_name || '');
    if (GATE_ALLOW.includes(tool)) process.exit(0);
    console.log(JSON.stringify({ hookSpecificOutput: { hookEventName: 'PreToolUse', permissionDecision: 'deny' } }));
    process.exit(0);
  }
  process.exit(0);
}
"""


def _load_script():
    spec = importlib.util.spec_from_file_location("gate_intervention", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def patched_hook(tmp_path: Path) -> Path:
    hook = tmp_path / "hook.mjs"
    hook.write_text(FIXTURE, encoding="utf-8")
    module = _load_script()
    assert module.main(["--check", "--hook", str(hook)]) == 1  # anchors present, not applied
    assert module.main(["--apply", "--hook", str(hook)]) == 0
    assert module.main(["--check", "--hook", str(hook)]) == 0
    assert module.main(["--apply", "--hook", str(hook)]) == 0  # idempotent
    return hook


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    return node


def _drive(node: str, hook: Path, gate_file: Path):
    def event(payload: dict[str, object]) -> tuple[int, str, str]:
        result = subprocess.run(
            [node, str(hook), "gate"],
            input=json.dumps({"session_id": "proof", **payload}),
            capture_output=True,
            text=True,
            env={
                **dict(__import__("os").environ),
                "GATE_FILE": str(gate_file),
                "STATE_DIR": str(gate_file.parent),
                "LAT_FILE": str(gate_file.parent / "latency.log"),
            },
        )
        return result.returncode, result.stdout, result.stderr

    def state() -> str:
        try:
            return json.loads(gate_file.read_text(encoding="utf-8"))["state"]
        except (OSError, ValueError, KeyError):
            return "none"

    def prompt(text: str, mid_turn: bool = False) -> str:
        code, _, err = event({"hook_event_name": "UserPromptSubmit", "prompt": text, "mid_turn": mid_turn})
        assert code == 0 and not err, err
        return state()

    def tool(name: str) -> str:
        code, out, err = event({"hook_event_name": "PreToolUse", "tool_name": name})
        assert code == 0 and not err, err
        return "deny" if '"permissionDecision":"deny"' in out else "allow"

    return prompt, tool


def test_patched_hook_runs_the_proof_sequence(patched_hook: Path, tmp_path: Path) -> None:
    node = _node()
    assert subprocess.run([node, "--check", str(patched_hook)]).returncode == 0
    prompt, tool = _drive(node, patched_hook, tmp_path / "gate")

    assert prompt("Select the intervention case and write the record") == "pending"
    assert tool("Bash") == "deny"
    assert tool("Read") == "allow"
    # I1: the 2026-09-09 near-misses now approve.
    assert prompt("sounds OK for this task. re-explain the gate challenge") == "approved"
    assert tool("Bash") == "allow"
    assert (
        prompt(
            "yeah mr wizard make it so. the apu-wtf ignoring anything is concerning to me, "
            "because it should take the entire picture into consideration but you are the boss right now."
        )
        == "approved"
    )
    # A question leaves the state alone.
    assert prompt("what would the audit actually count?") == "approved"
    # I2: a statement after a completed turn carries the objective forward.
    assert prompt("the voice commands are going to another session on the desktop app") == "approved"
    # A halt-vocabulary token anywhere in a statement still re-arms (documented over-match).
    assert prompt("the voice commands are going to another session because we do not have routing yet") == "pending"
    assert prompt("go ahead") == "approved"
    # S2: halt words withdraw approval; S1: tools are denied again.
    assert prompt("hold on, do not proceed") == "pending"
    assert tool("Bash") == "deny"
    # A new objective while pending stays pending.
    assert prompt("Now build a completely new voice routing service and deploy it") == "pending"
    # A plain approval, then a long new objective without a steer lead re-arms.
    assert prompt("go ahead") == "approved"
    assert (
        prompt("Build a completely new voice routing service with codenames for every session across all my machines and deploy it to production tonight")
        == "pending"
    )
    # Operator decision 2026-09-09: a steer-led long new objective does NOT inherit approval.
    assert prompt("go ahead") == "approved"
    assert prompt("Now build a completely new voice routing service with codenames for every session across all my machines and deploy it") == "pending"
    # A long steering message that continues the objective still carries forward.
    assert prompt("go ahead") == "approved"
    assert prompt("Keep this session on APU using its existing project directory and carry the completed proof forward without reopening it") == "approved"
    # The decision log records every state write, content-free.
    log = (tmp_path / "gate").parent.joinpath("latency.log").read_text(encoding="utf-8")
    assert "	gate	proof none->pending" in log
    assert "	gate	proof approved->pending" in log
    assert "routing" not in log and "Select" not in log
    # "wait" is both a steer trap and a halt: it must withdraw.
    assert prompt("wait, also do the codename thing") == "pending"


def test_revert_and_reapply_round_trip(tmp_path: Path) -> None:
    hook = tmp_path / "hook.mjs"
    hook.write_text(FIXTURE, encoding="utf-8")
    module = _load_script()
    assert module.main(["--apply", "--hook", str(hook)]) == 0
    assert module.main(["--reapply", "--hook", str(hook)]) == 0
    assert module.MARKER in hook.read_text(encoding="utf-8")
    assert module.main(["--revert", "--hook", str(hook)]) == 0
    assert hook.read_text(encoding="utf-8") == FIXTURE
