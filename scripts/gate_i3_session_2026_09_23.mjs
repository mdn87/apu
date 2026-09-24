// End-to-end I3 subject: a scripted operator session against the live gate hook.
// Case record: docs/cases/2026-09-09-start-of-turn-gate.md ("I3 detector").
//
// The gate hook writes prompt, tool, gate, and stop lines to its latency log;
// `apu behavior gate-cost` reads them back. This driver plays the operator's
// side of the 2026-09-09 before-state (a request, three read-only turns
// while the plan is pending, an approval, a halt, another read-only turn,
// re-approval, work) through the same hook modes Claude Code runs:
//   UserPromptSubmit -> `gate`     (writes the prompt line and the decision)
//   PreToolUse       -> `gate`     (writes the tool line; denies while armed)
//   Stop             -> (no mode)  (writes the stop line; exits before speech
//                                   because the transcript path does not exist)
// Then it runs `apu behavior gate-cost --session-id` on the result and checks
// the I3 values the detector should report. Run from anywhere:
//
//   node scripts/gate_i3_session_2026_09_23.mjs
//
// The session id is unique per run so its 8-char prefix never collides with a
// real session. The live tts.json is never written; the hook is driven with a
// copy that forces gate: "all". Temp markers are removed when done.
import { spawnSync } from 'node:child_process';
import { readFileSync, writeFileSync, unlinkSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir, homedir } from 'node:os';

const HOOK = process.env.GATE_HOOK || join(homedir(), '.claude', 'hooks', 'speak-response.mjs');
const SID = 'i3-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8);
const CWD = 'C:\\Users\\Matt\\Desktop\\MyDocs\\apu';
const STATE_DIR = join(tmpdir(), 'claude-tts');
const gateFile = join(STATE_DIR, 'gate-' + SID);
const busyFile = join(STATE_DIR, 'busy-' + SID);
mkdirSync(STATE_DIR, { recursive: true });

const liveConfig = join(homedir(), '.claude', 'tts.json');
const proofConfig = join(STATE_DIR, 'tts-i3-' + SID + '.json');
let cfg = {};
try { cfg = JSON.parse(readFileSync(liveConfig, 'utf-8')); } catch {}
writeFileSync(proofConfig, JSON.stringify({ ...cfg, gate: 'all' }));
const HOOK_ENV = { ...process.env, LUGOS_SPEECH_CONFIG: process.env.GATE_CONFIG || proofConfig };

function hook(mode, payload) {
  const r = spawnSync('node', mode ? [HOOK, mode] : [HOOK], {
    input: JSON.stringify({ session_id: SID, cwd: CWD, transcript_path: join(STATE_DIR, 'no-such-transcript-' + SID + '.jsonl'), ...payload }),
    encoding: 'utf-8', env: HOOK_ENV,
  });
  if (r.status !== 0 || r.stderr) throw new Error(`hook ${mode || 'stop'} failed exit=${r.status} ${String(r.stderr).slice(0, 200)}`);
  return (r.stdout || '').trim();
}
function gate() { try { return JSON.parse(readFileSync(gateFile, 'utf-8')).state; } catch { return 'none'; } }
const sleep = ms => Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);

const turns = [];
function turn(label, prompt, tools, expectState) {
  hook('gate', { hook_event_name: 'UserPromptSubmit', prompt });
  const state = gate();
  const results = [];
  for (const [name, input] of tools) {
    const out = hook('gate', { hook_event_name: 'PreToolUse', tool_name: name, tool_input: input });
    results.push(name + ':' + (/"permissionDecision":"deny"/.test(out) ? 'deny' : 'allow'));
  }
  hook(null, { hook_event_name: 'Stop', stop_hook_active: false });
  turns.push({ label, state, expect: expectState, ok: state === expectState, tools: results });
  sleep(400); // keep turns apart by more than the detector's join slack
}

// Objective 0: the request and three read-only turns while pending.
turn('1 new request (plan turn, no tools)', 'Select the intervention case and write the record', [], 'pending');
turn('2 question (read-only)', 'what would the audit actually count?', [['Read', { file_path: CWD + '\\README.md' }], ['Grep', { pattern: 'gate' }]], 'pending');
turn('3 statement (read-only)', 'the voice commands are going to another session on the desktop app', [['Glob', { pattern: '**/*.md' }]], 'pending');
// Objective 1: approval, work, a halt, one read-only turn while pending.
turn('4 approval + work', 'go ahead', [['Bash', { command: 'git status' }], ['Edit', { file_path: CWD + '\\src\\apu\\cli.py' }]], 'approved');
turn('5 halt (read-only)', 'hold on', [['Read', { file_path: CWD + '\\README.md' }]], 'pending');
// Objective 2: re-approval and work.
turn('6 re-approval + work', 'go ahead', [['Bash', { command: 'git status' }]], 'approved');

for (const f of [gateFile, busyFile, proofConfig]) { try { unlinkSync(f); } catch {} }

const apu = spawnSync('apu', ['behavior', 'gate-cost', '--since', '1h', '--session-id', SID, '--json'], { encoding: 'utf-8', shell: true });
if (apu.status !== 0) throw new Error('apu behavior gate-cost failed: ' + apu.stderr);
const report = JSON.parse(apu.stdout);
const session = (report.sessions || []).find(s => s.session_id === SID) || null;
const objectives = session ? Object.fromEntries(session.objectives.map(o => [o.index, o.pending_readonly_turns])) : {};
const expected = { 0: 3, 1: 1, 2: 0 };
const checks = Object.entries(expected).map(([index, want]) => ({ objective: Number(index), want, got: objectives[index] ?? null, ok: objectives[index] === want }));
const badTurns = turns.filter(t => !t.ok).length;
const badChecks = checks.filter(c => !c.ok).length;
console.log(JSON.stringify({
  session: SID, hook: HOOK, liveGateMode: cfg.gate === undefined ? 'all' : cfg.gate,
  turns, i3: session ? { i3_max: session.i3_max, i3_total: session.i3_total, decisions_joined: session.decisions_joined, completed: session.completed_turn_count + '/' + session.turn_count } : null,
  checks, mismatches: badTurns + badChecks,
}, null, 1));
process.exit(badTurns + badChecks ? 1 : 0);
