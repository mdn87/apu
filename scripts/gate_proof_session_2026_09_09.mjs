// Proof driver for the 2026-09-09 start-of-turn gate intervention.
// Case record: docs/cases/2026-09-09-start-of-turn-gate.md
//
// Drives the live gate hook exactly as Claude Code does (JSON on stdin, one
// process per event) under a synthetic session id, through the scripted
// prompt sequence from the case record, and reads the gate state after each
// event. It never touches a real session's gate state and removes its own
// temp markers when done. Run from anywhere:
//
//   node scripts/gate_proof_session_2026_09_09.mjs
//
// Prints one JSON object. "mismatches" must be 0.
import { spawnSync } from 'node:child_process';
import { readFileSync, writeFileSync, unlinkSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir, homedir } from 'node:os';

const HOOK = process.env.GATE_HOOK || join(homedir(), '.claude', 'hooks', 'speak-response.mjs');
const SID = 'proof-gate-2026-09-09';
const STATE_DIR = join(tmpdir(), 'claude-tts');
const gateFile = join(STATE_DIR, 'gate-' + SID);
const busyFile = join(STATE_DIR, 'busy-' + SID);
mkdirSync(STATE_DIR, { recursive: true });
for (const f of [gateFile, busyFile]) { try { unlinkSync(f); } catch {} }

function hook(payload) {
  const r = spawnSync('node', [HOOK, 'gate'], { input: JSON.stringify({ session_id: SID, ...payload }), encoding: 'utf-8' });
  if (r.status !== 0 || r.stderr) return `HOOK ERROR exit=${r.status} ${String(r.stderr).slice(0, 120)}`;
  return (r.stdout || '').trim();
}
function gate() { try { return JSON.parse(readFileSync(gateFile, 'utf-8')).state; } catch { return 'none'; } }
function turnDone() { // what the Stop hook does: mark the session idle
  let b = {}; try { b = JSON.parse(readFileSync(busyFile, 'utf-8')); } catch {}
  writeFileSync(busyFile, JSON.stringify({ ...b, state: 'idle', touched: Date.now(), finished: Date.now() }));
}
function tool(name) {
  const out = hook({ hook_event_name: 'PreToolUse', tool_name: name });
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

// 1. Actionable request in a fresh session: the gate must arm (S1 baseline).
prompt('1 new request', 'Select the intervention case and write the record', 'pending');
check('1a Bash while pending (S1)', tool('Bash'), 'deny');
check('1b Read while pending', tool('Read'), 'allow');
check('1c Write of a note while pending (I4, candidate 3 not applied)', tool('Write'), 'deny');
// 2. The 2026-09-09 near-miss: must now approve (I1).
prompt('2 off-list affirmation', 'sounds OK for this task. re-explain the gate challenge and how to do the human response turn end effectively', 'approved');
check('2a Bash after affirmation', tool('Bash'), 'allow');
// 3. A question leaves the state alone.
prompt('3 question', 'what would the audit actually count?', 'approved');
// 4. A long statement after a completed turn carries the objective forward (I2).
// The wording avoids every halt-vocabulary token ("don't", "do not", "wait", ...).
prompt('4 long statement', 'the voice commands are going to another session on the desktop app not this one so you cannot hear me because routing is not entirely buttoned up yet', 'approved');
// 4b. The verbatim 2026-09-09 utterance contains "dont", which the gate's halt
//     vocabulary treats as a halt. Documented over-match; expected to re-arm.
prompt('4b verbatim statement with a contraction the halt list matches', 'the voice commands are going to another session on the desktop app not this one so you cant hear me say go ahead because we dont have routing entirely buttoned up', 'pending');
// 5. The second near-miss: approval phrase early in a long message (I1).
prompt('5 early phrase, long tail', 'yeah mr wizard make it so. the apu-wtf ignoring anything is concerning to me, because it should take the entire picture into consideration but you are the boss right now.', 'approved');
// 6. Halt words withdraw approval (S2) and tools are denied again (S1).
prompt('6 halt', 'hold on, do not proceed', 'pending');
check('6a Bash after halt (S1)', tool('Bash'), 'deny');
// 7. A new objective while pending stays pending.
prompt('7 new objective while pending', 'Now build a completely new voice routing service with codenames for every session across all my machines and deploy it', 'pending');
// 8. Plain approval, then a long new objective without a steer lead re-arms.
prompt('8 go ahead', 'go ahead', 'approved');
prompt('9 long new objective after approval (boundary probe)', 'Build a completely new voice routing service with codenames for every session across all my machines and deploy it to production tonight', 'pending');
// 10. Known gap: after an approval, a steer-led long new objective carries forward.
prompt('10a go ahead', 'go ahead', 'approved');
prompt('10 steer-led long new objective (known gap)', 'Now build a completely new voice routing service with codenames for every session across all my machines and deploy it', 'approved');
// 11. "wait" is both a steer trap and a halt: it must withdraw (S2).
prompt('11 wait as halt', 'wait, also do the codename thing', 'pending');

for (const f of [gateFile, busyFile]) { try { unlinkSync(f); } catch {} }
const bad = rows.filter(r => !r.ok).length;
console.log(JSON.stringify({ session: SID, hook: HOOK, rows, mismatches: bad }, null, 1));
process.exit(bad ? 1 : 0);
