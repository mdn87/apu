// Proof driver for the 2026-09-23 gate rule gaps intervention.
// Case record: docs/cases/2026-09-09-start-of-turn-gate.md ("Gate rule gaps")
//
// Drives the live gate hook exactly as Claude Code does (JSON on stdin, one
// process per event) under a synthetic session id and reads the gate state
// after each event. It never touches a real session's gate state and removes
// its own temp markers when done. Run from anywhere:
//
//   node scripts/gate_proof_session_2026_09_23.mjs
//
// Prints one JSON object. "mismatches" must be 0.
import { spawnSync } from 'node:child_process';
import { readFileSync, writeFileSync, unlinkSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir, homedir } from 'node:os';

const HOOK = process.env.GATE_HOOK || join(homedir(), '.claude', 'hooks', 'speak-response.mjs');
const SID = 'proof-gate-2026-09-23';
const CWD = 'C:\\Users\\Matt\\Desktop\\MyDocs\\apu';
const STATE_DIR = join(tmpdir(), 'claude-tts');
const gateFile = join(STATE_DIR, 'gate-' + SID);
const busyFile = join(STATE_DIR, 'busy-' + SID);
mkdirSync(STATE_DIR, { recursive: true });
for (const f of [gateFile, busyFile]) { try { unlinkSync(f); } catch {} }

// The proof is about the gate's rules, not its current mode. The live config
// may have the gate in "voice" mode, where typed prompts never arm it, so the
// hook is driven with a copy of the live config that forces gate: "all". The
// live tts.json is never written. Override with GATE_CONFIG to use another file.
const liveConfig = join(homedir(), '.claude', 'tts.json');
const proofConfig = join(STATE_DIR, 'tts-proof-' + SID + '.json');
let cfg = {};
try { cfg = JSON.parse(readFileSync(liveConfig, 'utf-8')); } catch {}
writeFileSync(proofConfig, JSON.stringify({ ...cfg, gate: 'all' }));
const HOOK_ENV = { ...process.env, LUGOS_SPEECH_CONFIG: process.env.GATE_CONFIG || proofConfig };

function hook(payload) {
  const r = spawnSync('node', [HOOK, 'gate'], { input: JSON.stringify({ session_id: SID, cwd: CWD, ...payload }), encoding: 'utf-8', env: HOOK_ENV });
  if (r.status !== 0 || r.stderr) return `HOOK ERROR exit=${r.status} ${String(r.stderr).slice(0, 120)}`;
  return (r.stdout || '').trim();
}
function gate() { try { return JSON.parse(readFileSync(gateFile, 'utf-8')).state; } catch { return 'none'; } }
function turnDone() { // what the Stop hook does: mark the session idle
  let b = {}; try { b = JSON.parse(readFileSync(busyFile, 'utf-8')); } catch {}
  writeFileSync(busyFile, JSON.stringify({ ...b, state: 'idle', touched: Date.now(), finished: Date.now() }));
}
function tool(name, tool_input = {}) {
  const out = hook({ hook_event_name: 'PreToolUse', tool_name: name, tool_input });
  return /"permissionDecision":"deny"/.test(out) ? 'deny' : 'allow';
}

const rows = [];
function prompt(label, text, expectState) {
  const before = gate();
  const out = hook({ hook_event_name: 'UserPromptSubmit', prompt: text });
  const after = gate();
  rows.push({ step: label, before, after, expect: expectState, ok: after === expectState, hookSaid: out.slice(0, 70) });
  turnDone();
}
function check(label, got, want) { rows.push({ step: label, before: '', after: got, expect: want, ok: got === want, hookSaid: '' }); }

// 1. Actionable request in a fresh session: the gate arms (S1 baseline).
prompt('1 new request', 'Select the intervention case and write the record', 'pending');
check('1a Bash while pending (S1)', tool('Bash', { command: 'git status' }), 'deny');
check('1b Read while pending', tool('Read', { file_path: CWD + '\\README.md' }), 'allow');
// I4: local notes pass while the gate is armed; code and settings do not.
check('1c Write handoff note while pending (I4)', tool('Write', { file_path: CWD + '\\.claude\\handoff.md' }), 'allow');
check('1d Edit incident record while pending (I4)', tool('Edit', { file_path: CWD + '\\incidents\\2026-09-23-gate.md' }), 'allow');
check('1e Write source while pending (S1)', tool('Write', { file_path: CWD + '\\src\\apu\\cli.py' }), 'deny');
check('1f Write project settings while pending (S1)', tool('Write', { file_path: CWD + '\\.claude\\settings.local.json' }), 'deny');
check('1g Write note in another directory while pending (S1)', tool('Write', { file_path: 'C:\\Users\\Matt\\other\\.claude\\handoff.md' }), 'deny');
check('1h Write without a path while pending (S1)', tool('Write'), 'deny');
// 2. Approval, then the halt over-match is closed: narrative tokens carry forward (I2).
prompt('2 go ahead', 'go ahead', 'approved');
check('2a Bash after approval', tool('Bash', { command: 'git status' }), 'allow');
prompt('3 statement with "do not" mid-sentence', 'the voice commands are going to another session on the desktop app not this one so you cannot hear me because we do not have routing entirely buttoned up', 'approved');
prompt('3b verbatim 2026-09-09 statement with "dont"', 'the voice commands are going to another session on the desktop app not this one so you cant hear me say go ahead because we dont have routing entirely buttoned up', 'approved');
prompt('3c statement with "wait" mid-sentence', 'the wait times on the listener side are long but that is a separate problem', 'approved');
// 4. S2: leading and imperative halts still withdraw approval; tools are denied again (S1).
prompt('4 leading wait', 'wait, also do the codename thing', 'pending');
check('4a Bash after halt (S1)', tool('Bash', { command: 'git status' }), 'deny');
prompt('4b go ahead', 'go ahead', 'approved');
prompt('4c bare "do not proceed" (was read as a question)', 'do not proceed', 'pending');
prompt('4d go ahead', 'go ahead', 'approved');
prompt('4e imperative "don\'t push"', 'actually don\'t push that yet', 'pending');
prompt('4f go ahead', 'go ahead', 'approved');
prompt('4g hold on', 'hold on, do not proceed', 'pending');
check('4h Bash after halt (S1)', tool('Bash', { command: 'git status' }), 'deny');
// 5. Steer list narrowed (operator decision 2026-09-09): a steer-led new objective does not inherit approval.
prompt('5 go ahead', 'go ahead', 'approved');
prompt('5a steer-led long new objective re-arms', 'Now build a completely new voice routing service with codenames for every session across all my machines and deploy it', 'pending');
prompt('5b go ahead', 'go ahead', 'approved');
prompt('5c long steer on the same objective carries forward', 'ok, keep this session on APU using its existing project directory and carry the completed proof forward without reopening it', 'approved');
prompt('5d short steer carries forward', 'use zira for the voice', 'approved');
// 6. A question still leaves the state alone; an off-list affirmation still approves (I1 holds).
prompt('6 question', 'what would the audit actually count?', 'approved');
prompt('6a halt then affirmation', 'stop', 'pending');
prompt('6b off-list affirmation', 'sounds OK for this task. re-explain the gate challenge', 'approved');

for (const f of [gateFile, busyFile, proofConfig]) { try { unlinkSync(f); } catch {} }
const bad = rows.filter(r => !r.ok).length;
console.log(JSON.stringify({ session: SID, hook: HOOK, liveGateMode: cfg.gate === undefined ? 'all' : cfg.gate, rows, mismatches: bad }, null, 1));
process.exit(bad ? 1 : 0);
