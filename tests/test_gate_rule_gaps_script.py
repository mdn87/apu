"""The 2026-09-23 gate script must produce a hook that runs, not just parses.

The fixture mirrors the shape of ``speak-response.mjs`` as it stood on
2026-09-23: the CLI dispatch calls ``gateMain`` at the top of the module, the
2026-09-09 objective-bound approval is present with its wide steer list, and
the 2026-09-22 conversational "chat" state exists. Every anchor the script
replaces appears here exactly as it does in the live hook, so a drifted live
hook fails the script's --check rather than this test passing by accident.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "gate_intervention_2026_09_23.py"

FIXTURE = r"""
import { readFileSync, writeFileSync, mkdirSync, appendFileSync } from 'fs';
const STATE_DIR = process.env.STATE_DIR;
function lat(event, detail = '') { appendFileSync(process.env.LAT_FILE, `${Date.now()}	${event}	${detail}
`); }
const GATE_ALLOW = ['Read', 'Glob', 'Grep'];
const STRONG_OK = /^(go ahead|go for it|proceed|do it|do that|approved|approve[d]?|confirm(ed|ing)?|make it so|carry on|green light|ship it|sounds good|sounds ok(ay)?|sounds fine|sounds right|looks good|looks fine|fine by me|works for me|that works|let'?s do it|let'?s go|affirmative|permission granted|you may proceed|execute)\b/;
const WEAK_OK = /^(yes|yep|yeah|yup|ok|okay|sure|fine|alright|all right|go|continue|please do|please proceed)\b/;
const STRONG_OK_ANY = /\b(go ahead|go for it|do it|approved|make it so|green light|ship it|permission granted|you may proceed|carry on)\b/;
const REFUSAL = /\b(don'?t|do not|not|never|wait|hold off|hold on|stop|cancel|abort)\b/;
// A mid-turn message withdraws an approval only when it says to halt.
const HALT = /\b(stop|halt|wait|hold on|hold off|cancel|abort|don'?t|do not|never ?mind|undo|revert)\b/;

const CHAT_LEAD = /^(hi|hello|hey there|thanks|thank you|explain|describe|summari[sz]e|recap|repeat|remind me|walk me through|tell me (about|what|how|why)|what do you think)\b/;
const ACTION_VERB = /\b(add|apply|build|change|commit|create|delete|deploy|edit|fix|implement|install|kill|launch|make|merge|move|open|patch|push|relaunch|remove|rename|restart|revert|run|send|set|start|switch|turn|update|upgrade|write)\b/;
const SAY_COMMAND = /speak-response\.mjs["']?\s+say\b/;
function isConversational(cmd, prompt, cfg) {
  const t = cmd.toLowerCase().replace(/^[\s,.!?:;-]+/, '').replace(/^(hey |ok |okay |alright |all right )?claude[,.!? ]+/, '').trim();
  if (!t) return false;
  if (ACTION_VERB.test(t)) return false;
  return CHAT_LEAD.test(t);
}

// Objective-bound approval: after an approval, a completed turn does not re-arm
// the gate on its own.
const APPROVAL_TTL_MS = 2 * 60 * 60 * 1000;
const STEER_LEAD = /^(ok|okay|yes|yep|yeah|yup|sure|fine|right|good|great|cool|also|and|then|now|next|plus|keep|continue|just|actually|instead|but|oh|hmm|no|nope|not that|the|that|this|it|use|try|make|change|update|fix|add|remove|drop|skip|include|check|run|rerun|retry|again|same|more|less|put|move|rename|swap|switch|start|finish|wrap|land|commit|push|test)\b/;
function continuesObjective(cmd) {
  const t = cmd.toLowerCase().replace(/^[\s,.!?:;-]+/, '').replace(/^(hey |ok |okay |alright |all right )?claude[,.!? ]+/, '').trim();
  if (!t) return false;
  const words = t.split(/\s+/).length;
  if (words <= 12) return true;
  return STEER_LEAD.test(t);
}

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
  if (!m || REFUSAL.test(t.slice(0, m.index))) return false;
  const wordsBefore = t.slice(0, m.index).trim().split(/\s+/).filter(Boolean).length;
  if (wordsBefore < 10) return true;
  return words <= 24 && !isQuestion(t);
}
function isQuestion(cmd) {
  const q = cmd.toLowerCase();
  return /\?\s*$/.test(q) || /^(what|what's|why|how|is|are|was|were|did|do|does|can|could|would|will|where|when|which|who|have|has|tell me)\b/.test(q);
}
function gateMain(args) {
  const cfg = {};
  let data = {};
  try { data = JSON.parse(readFileSync(0, 'utf-8')); } catch { process.exit(0); }
  const sid = data.session_id;
  const ev = data.hook_event_name || (data.tool_name ? 'PreToolUse' : 'UserPromptSubmit');
  const midTurn = !!data.mid_turn;
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
    if (g0 && g0.state === 'approved') {
      const low = cmd.toLowerCase().replace(/\bstop asking\b/g, '');
      const fresh = Date.now() - (g0.ts || 0) < APPROVAL_TTL_MS;
      if (!HALT.test(low) && midTurn) {
        console.log('Start-of-turn gate: mid-turn message while the plan is approved; the gate stays open. Treat it as a steer on the running work, not a new plan request.');
        process.exit(0);
      }
      if (!HALT.test(low) && fresh && continuesObjective(cmd)) {
        // Objective-bound approval (2026-09-09 gate intervention): the turn
        // finished, but this reads as a steer or affirmation on the same
        // objective, so the approval carries forward instead of resetting.
        writeGate(sid, 'approved', { cmd: g0.cmd || cmd, ts: g0.ts });
        console.log('Start-of-turn gate: the approved objective carries forward; this message steers it. Continue without restating the plan. Say "stop" or "hold on" to withdraw approval.');
        process.exit(0);
      }
    }
    if (isConversational(cmd, prompt, cfg)) {
      writeGate(sid, 'chat', { cmd });
      console.log('Start-of-turn gate: conversational prompt.');
      process.exit(0);
    }
    writeGate(sid, 'pending', { cmd });
    console.log('Start-of-turn gate is armed for this prompt.');
    process.exit(0);
  }

  if (ev === 'PreToolUse') {
    const g = readGate(sid);
    if (!g || (g.state !== 'pending' && g.state !== 'chat')) process.exit(0);
    const allow = Array.isArray(cfg.gateAllow) ? cfg.gateAllow : GATE_ALLOW;
    const tool = String(data.tool_name || '');
    if (allow.includes(tool)) process.exit(0);
    if (g.state === 'chat' && tool === 'Bash' && SAY_COMMAND.test(String((data.tool_input || {}).command || ''))) process.exit(0);
    const reason = 'denied';
    console.log(JSON.stringify({ hookSpecificOutput: { hookEventName: 'PreToolUse', permissionDecision: 'deny', permissionDecisionReason: reason } }));
    process.exit(0);
  }
  process.exit(0);
}
"""

CWD = "C:\\Users\\Matt\\Desktop\\MyDocs\\apu"


def _load_script():
    spec = importlib.util.spec_from_file_location("gate_rule_gaps", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    return node


def _drive(node: str, hook: Path, gate_file: Path):
    def event(payload: dict[str, object]) -> tuple[int, str, str]:
        result = subprocess.run(
            [node, str(hook), "gate"],
            input=json.dumps({"session_id": "proof", "cwd": CWD, **payload}),
            capture_output=True,
            text=True,
            env={
                **dict(os.environ),
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

    def tool(name: str, **tool_input: object) -> str:
        code, out, err = event({"hook_event_name": "PreToolUse", "tool_name": name, "tool_input": tool_input})
        assert code == 0 and not err, err
        return "deny" if '"permissionDecision":"deny"' in out else "allow"

    return prompt, tool


@pytest.fixture
def patched_hook(tmp_path: Path) -> Path:
    hook = tmp_path / "hook.mjs"
    hook.write_text(FIXTURE, encoding="utf-8")
    module = _load_script()
    assert module.main(["--check", "--hook", str(hook)]) == 1  # anchors present, not applied
    hook.write_text(module.apply(FIXTURE), encoding="utf-8")
    assert module.main(["--check", "--hook", str(hook)]) == 0
    with pytest.raises(SystemExit, match="already applied"):
        module.apply(hook.read_text(encoding="utf-8"))
    return hook


def test_check_reports_missing_and_ambiguous_anchors(tmp_path: Path) -> None:
    module = _load_script()
    hook = tmp_path / "hook.mjs"
    hook.write_text(FIXTURE.replace("const HALT = ", "const HALT_RENAMED = "), encoding="utf-8")
    assert module.main(["--check", "--hook", str(hook)]) == 2
    hook.write_text(FIXTURE + module.ALLOW_OLD, encoding="utf-8")
    assert module.main(["--check", "--hook", str(hook)]) == 2


def test_patched_hook_runs_the_gap_sequence(patched_hook: Path, tmp_path: Path) -> None:
    node = _node()
    assert subprocess.run([node, "--check", str(patched_hook)]).returncode == 0
    prompt, tool = _drive(node, patched_hook, tmp_path / "gate")

    assert prompt("Select the intervention case and write the record") == "pending"
    assert tool("Bash", command="git status") == "deny"
    assert tool("Read", file_path=CWD + "\\README.md") == "allow"
    # I4: local notes pass while pending; everything else still waits.
    assert tool("Write", file_path=CWD + "\\.claude\\handoff.md") == "allow"
    assert tool("Write", file_path=CWD + "/.claude/handoff-history/handoff-20260923.md") == "allow"
    assert tool("Edit", file_path=CWD + "\\incidents\\2026-09-23-gate.md") == "allow"
    assert tool("Write", file_path=".claude/notes/scratch.md") == "allow"
    assert tool("Write", file_path=CWD + "\\.claude\\settings.local.json") == "deny"
    assert tool("Write", file_path=CWD + "\\src\\apu\\cli.py") == "deny"
    assert tool("Write", file_path="C:\\Users\\Matt\\other\\.claude\\handoff.md") == "deny"
    assert tool("Write", file_path=CWD + "\\incidents\\..\\src\\x.py") == "deny"
    assert tool("Bash", command="echo note > .claude/handoff.md") == "deny"
    # S1 in chat state too: notes pass, code does not.
    assert prompt("explain the gate to me") == "chat"
    assert tool("Write", file_path=CWD + "\\.claude\\handoff.md") == "allow"
    assert tool("Write", file_path=CWD + "\\src\\apu\\cli.py") == "deny"

    assert prompt("go ahead") == "approved"
    assert tool("Bash", command="git status") == "allow"
    # Halt over-match closed: narrative "do not" / "dont" / "wait" carry forward.
    assert prompt("the voice commands are going to another session because we do not have routing yet") == "approved"
    assert prompt("we dont have routing") == "approved"
    assert prompt("the wait times are long on the listener side") == "approved"
    assert prompt("I don't think the listener is the problem") == "approved"
    # S2: leading and imperative halts still withdraw; tools are denied again.
    assert prompt("wait, also do the codename thing") == "pending"
    assert tool("Bash", command="git status") == "deny"
    assert prompt("go ahead") == "approved"
    assert prompt("do not proceed") == "pending"
    assert prompt("go ahead") == "approved"
    assert prompt("actually don't push that yet") == "pending"
    assert prompt("go ahead") == "approved"
    assert prompt("please wait, I need to check something") == "pending"
    assert prompt("go ahead") == "approved"
    assert prompt("hold on, do not proceed") == "pending"
    assert tool("Bash", command="git status") == "deny"
    assert prompt("go ahead") == "approved"
    assert prompt("the listener should never mind the mic") == "pending"  # hard halt word anywhere
    assert prompt("go ahead") == "approved"
    # Mid-turn steer while approved stays open; a mid-turn halt closes.
    assert prompt("its still not running", mid_turn=True) == "approved"
    assert prompt("stop", mid_turn=True) == "pending"
    assert prompt("go ahead") == "approved"
    # Steer list narrowed: a steer-led new objective does not inherit approval.
    assert prompt("Now build a completely new voice routing service with codenames for every session across all my machines and deploy it") == "pending"
    assert prompt("go ahead") == "approved"
    assert prompt("Fix the listener, then build a completely new voice routing service with codenames for every session and deploy it") == "pending"
    assert prompt("go ahead") == "approved"
    # Short steers and affirmation-led continuations still carry forward.
    assert prompt("use zira for the voice") == "approved"
    assert prompt("ok, keep this session on APU using its existing project directory and carry the completed proof forward without reopening it") == "approved"
    # The decision log records every state write, content-free.
    log = (tmp_path / "latency.log").read_text(encoding="utf-8")
    assert "	gate	proof none->pending" in log
    assert "	gate	proof approved->pending" in log
    assert "	gate	proof pending->chat" in log
    assert "routing" not in log and "Select" not in log and "codename" not in log


def test_plan_mode_installs_through_apu_apply_and_rolls_back(tmp_path: Path) -> None:
    """The durable path: the script renders an APU plan; apply and rollback own the mutation."""

    from apu.apply import apply_plan
    from apu.models import Plan
    from apu.rollback import rollback_receipt

    home = tmp_path / "home"
    hook = home / "hooks" / "speak-response.mjs"
    hook.parent.mkdir(parents=True)
    hook.write_text(FIXTURE, encoding="utf-8")
    module = _load_script()
    assert module.main(["--plan", str(tmp_path / "out"), "--hook", str(hook)]) == 0
    assert hook.read_text(encoding="utf-8") == FIXTURE  # rendering touches nothing

    plan_path = tmp_path / "out" / "gate-rule-gaps-plan.json"
    plan = Plan.from_dict(json.loads(plan_path.read_text(encoding="utf-8")))
    plan.validate()
    assert plan.status == "approved"
    (operation,) = plan.operations
    assert operation.action == "merge" and operation.strategy == "full_file"
    assert operation.target == str(hook)

    state = tmp_path / "state"
    receipt = apply_plan(plan, state_home=state, installation_id="install-gaps-test")
    patched = hook.read_text(encoding="utf-8")
    assert module.MARKER in patched
    assert patched == (tmp_path / "out" / "speak-response.rendered.mjs").read_text(encoding="utf-8")
    assert module.main(["--check", "--hook", str(hook)]) == 0

    node = _node()
    prompt, tool = _drive(node, hook, tmp_path / "gate")
    assert prompt("Select the intervention case and write the record") == "pending"
    assert tool("Write", file_path=CWD + "\\.claude\\handoff.md") == "allow"
    assert tool("Bash", command="git status") == "deny"
    assert prompt("sounds OK for this task") == "approved"
    assert prompt("we do not have routing yet") == "approved"
    assert prompt("hold on, do not proceed") == "pending"
    assert tool("Bash", command="git status") == "deny"

    rollback_receipt(receipt)
    assert hook.read_text(encoding="utf-8") == FIXTURE

    hook.write_text(FIXTURE + "\n// drift\n", encoding="utf-8")
    with pytest.raises(Exception):
        apply_plan(plan, state_home=state, installation_id="install-gaps-test-2")
    assert hook.exists()
