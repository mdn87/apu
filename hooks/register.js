let diagnosis = null;
let generation = 0;
let busy = false;
let nativeAttempt = null;
let activeTurn = null;
let turnGeneration = 0;
let paneReady = false;
let paneText = 'Prepare a diagnosis before continuing this session.';
const PANE = 'apu-continuation';

function isNativeTurn(text, prompt) {
  // Claude Code 2.1.296 preserves plugin attribution by wrapping its prompt.
  // Match the complete observed wrapper, never a substring of another turn.
  return text === prompt || text === `The apu-session plugin sent a message:\n${prompt}\n\nThis is how Claude Code surfaces a prompt a plugin submits between turns — it starts this turn in the user's place. Address the message above.`;
}

function reset() {
  generation += 1;
  diagnosis = null;
  busy = false;
  nativeAttempt = null;
  paneReady = false;
  activeTurn = null;
  turnGeneration += 1;
  paneText = 'Session changed. Run /apu-diagnose and /apu-prepare again.';
}

async function sameSession($, attempt) {
  const session = await $.session.id();
  const cwd = await $.session.cwd();
  return attempt.epoch === generation && session === attempt.session && cwd === attempt.cwd;
}

async function nativeCall($, action, args) {
  const result = await $.process.run(
    ['apu-intervene', '--provider', 'claude-code', '--native-action', action, ...args, '--json'],
    { timeoutMs: 15000 });
  if (result.exitCode !== 0) throw new Error('native operation refused');
  return JSON.parse(result.stdout);
}

function observe($, attempt, state, args = []) {
  attempt.recording = attempt.recording.then(async () => {
    const value = await nativeCall($, state, ['--intervention', attempt.id, ...args]);
    attempt.state = value.native_delivery.state;
    $.ui.invalidate('ui.render');
  }).catch(() => { attempt.recordingFailed = true; });
  return attempt.recording;
}

async function continueNative($) {
  if (busy) return { text: 'APU: the previous request is still running.' };
  if (!paneReady || activeTurn !== null) return { text: 'APU: wait for this session to become idle and reopen /apu-continue.' };
  busy = true;
  const epoch = generation;
  try {
    const session = await $.session.id();
    const cwd = await $.session.cwd();
    if (epoch !== generation || !diagnosis || diagnosis.session !== session || diagnosis.cwd !== cwd || !diagnosis.prepared)
      return { text: 'Run /apu-diagnose and /apu-prepare in this session before /apu-continue.' };
    if (nativeAttempt) return { text: `APU native delivery: ${nativeAttempt.state}. No resend.\nIntervention: ${nativeAttempt.id}\nRecovery outcome remains unverified.${nativeAttempt.recordingFailed ? '\nAn observation could not be saved; inspect APU artifacts.' : ''}` };
    const diagnosisId = diagnosis.id;
    const idleGeneration = turnGeneration;
    const value = await nativeCall($, 'claim', ['--diagnosis', diagnosisId, '--session-id', session, '--cwd', cwd]);
    if (value.should_submit === false)
      return { text: `APU: this continuation is already reserved (${value.native_delivery.state}); no resend.\nIntervention: ${value.intervention_id}\nInspect saved APU evidence; outcome remains unverified.` };
    if (value.should_submit !== true || value.provider !== 'claude-code' || value.session_id !== session
      || value.diagnosis_id !== diagnosisId || value.executed !== false || value.durable_policy_mutation !== false
      || !/^native-[a-f0-9]{64}$/.test(value.intervention_id) || typeof value.prompt !== 'string' || !value.prompt)
      throw new Error('invalid native reservation');
    const attempt = { id: value.intervention_id, session, cwd, epoch, prompt: value.prompt,
      idleGeneration, state: 'reserved', admitted: false, turn: null, recording: Promise.resolve(), recordingFailed: false };
    if (!await sameSession($, attempt) || activeTurn !== null || turnGeneration !== idleGeneration) {
      await observe($, attempt, 'outcome-unknown');
      return { text: 'APU: session changed while reserving. No prompt submitted; reservation retained to prevent replay.' };
    }
    nativeAttempt = attempt;
    // Calls originating in this plugin can skip its prompt.submit hook in the
    // native runtime. Perform admission in the button action itself, not in a
    // middleware hook that may never see our submission.
    try {
      const admitted = await nativeCall($, 'admit', ['--intervention', attempt.id,
        '--session-id', session, '--cwd', cwd]);
      if (admitted.should_enter !== true || !await sameSession($, attempt)
        || activeTurn !== null || turnGeneration !== idleGeneration) {
        await observe($, attempt, 'outcome-unknown');
        return { text: 'APU refused: reservation or session changed before submission. No resend.' };
      }
      attempt.admitted = true;
    } catch (_) {
      await observe($, attempt, 'dropped');
      return { text: 'APU refused: native admission could not be verified. No prompt submitted.' };
    }
    // A native button is a fresh ui.press event. The host explicitly forbids
    // submitting from command.run, even without awaiting it. Never execute --resume.
    await $.prompt.submit({ text: attempt.prompt }).then(async result => {
      if (!await sameSession($, attempt)) return observe($, attempt, 'outcome-unknown');
      return observe($, attempt, typeof result.drop === 'string' ? 'dropped' : 'queued');
    }).catch(() => observe($, attempt, 'outcome-unknown'));
    return { text: `APU native delivery: ${attempt.state}.\nIntervention: ${attempt.id}\nRecovery outcome remains unverified. No automatic resend.` };
  } catch (_) {
    return { text: 'APU native continuation unavailable. No automatic retry; inspect saved APU artifacts before taking further action.' };
  } finally {
    if (epoch === generation) busy = false;
  }
}

async function act($, prepare) {
  if (busy) return { text: 'APU: the previous request is still running.' };
  busy = true;
  const epoch = generation;
  try {
    const session = await $.session.id();
    const cwd = await $.session.cwd();
    if (!session || !cwd) return { text: 'APU: exact session identity is unavailable.' };
    if (diagnosis && (diagnosis.session !== session || diagnosis.cwd !== cwd)) diagnosis = null;
    if (prepare && !diagnosis) return { text: 'Run /apu-diagnose in this session before preparing a continuation.' };
    if (prepare && diagnosis.prepared) return { text: diagnosis.prepared };
    if (!prepare) diagnosis = null;
    const argv = prepare
      ? ['apu-intervene', '--provider', 'claude-code', '--diagnosis', diagnosis.id, '--dry-run', '--json']
      : ['apu-wtf', '--provider', 'claude-code', '--session-id', session, '--cwd', cwd, '--fresh', '--json'];
    const result = await $.process.run(argv, { timeoutMs: 15000 });
    if (epoch !== generation) return { text: 'APU: session changed; discard this view and diagnose the current session.' };
    if (result.exitCode !== 0) {
      diagnosis = null;
      let reason = `exit ${result.exitCode}`;
      try {
        const failure = JSON.parse(result.stdout);
        if (failure.kind === 'no_attribution' && /^[a-z_]+$/.test(failure.reason_code)) reason = failure.reason_code;
      } catch (_) {}
      return { text: `APU refused or could not complete the request (${reason}). No turn was submitted.\nSession: ${session}\nDirectory: ${cwd}\nRun apu-wtf with this exact provider, session ID and cwd for details.` };
    }
    const value = JSON.parse(result.stdout);
    if (value.provider !== 'claude-code' || typeof value.status !== 'string') throw new Error('invalid result');
    if (!prepare) {
      if (typeof value.diagnosis_id !== 'string' || typeof value.incident_id !== 'string') throw new Error('invalid diagnosis');
      // The CLI owns attribution, evidence, barrier classification and saved artifacts.
      diagnosis = { id: value.diagnosis_id, session, cwd, prepared: null };
      const allowed = ['likely-autonomy-loss', 'recovery-requested'].includes(value.status)
        && value.recommended_intervention?.durable_policy_mutation === false;
      return { text: `APU: ${value.status}\nSession: ${session}\nDiagnosis: ${value.diagnosis_id}\nIncident: ${value.incident_id}\n`
        + (allowed ? 'Use /apu-prepare to revalidate and prepare a continuation.' : 'The evidence does not authorize continuation. Resolve any barrier or gather more evidence.') };
    }
    if (value.session_id !== session || value.diagnosis_id !== diagnosis.id
      || value.executed !== false || value.durable_policy_mutation !== false
      || value.status !== 'planned' || !Array.isArray(value.command)
      || !value.command.every(part => typeof part === 'string')) throw new Error('invalid preparation');
    diagnosis.prepared = `APU: ${value.status}\nIntervention: ${value.intervention_id}\nNo turn was submitted; outcome remains unverified.\nContinuation argv:\n${JSON.stringify(value.command)}`;
    return { text: diagnosis.prepared };
  } catch (_) {
    if (epoch === generation) diagnosis = null;
    return { text: 'APU result unavailable. No automatic retry or continuation was attempted; inspect saved APU artifacts before retrying.' };
  } finally {
    if (epoch === generation) busy = false;
  }
}

export function register(on) {
  on('session.start', async ($, e, next) => {
    reset();
    await $.command.register({ name: 'apu-diagnose', immediate: true,
      description: 'Save an APU diagnosis for this exact Claude session' });
    await $.command.register({ name: 'apu-prepare', immediate: true,
      description: 'Revalidate the saved diagnosis and print a continuation without executing it' });
    await $.command.register({ name: 'apu-continue',
      description: 'Open the native continuation panel for this prepared session' });
    return next(e);
  });
  on('classic.SessionStart', async ($, e, next) => { reset(); return next(e); });
  on('classic.CwdChanged', async ($, e, next) => { reset(); return next(e); });
  on('session.end', async ($, e, next) => { reset(); return next(e); });
  on('command.run', { command: 'apu-diagnose' }, async ($, e) =>
    e.args.trim() ? { text: 'Usage: /apu-diagnose (targets the current session only)' } : act($, false));
  on('command.run', { command: 'apu-prepare' }, async ($, e) =>
    e.args.trim() ? { text: 'Usage: /apu-prepare (uses this session’s saved diagnosis)' } : act($, true));
  on('command.run', { command: 'apu-continue' }, async ($, e) => {
    if (e.args.trim()) return { text: 'Usage: /apu-continue (uses this session’s prepared diagnosis)' };
    const epoch = generation;
    const session = await $.session.id();
    const cwd = await $.session.cwd();
    if (epoch !== generation || !diagnosis?.prepared || diagnosis.session !== session || diagnosis.cwd !== cwd)
      return { text: 'Run /apu-diagnose and /apu-prepare in this session before /apu-continue.' };
    paneReady = true;
    paneText = 'Continue the prepared recovery instruction once in this session. This does not change policy or attest recovery.';
    const opened = await $.ui.open({ id: PANE, title: 'APU continuation', focus: true, closeOnEscape: true, rows: 8 });
    if (!opened.isPlaced) { paneReady = false; return { text: 'APU continuation panel is unavailable on this surface. No prompt submitted.' }; }
    return { text: 'APU continuation panel opened. Use Continue this session when ready; opening it submits nothing.' };
  });
  on('ui.close', async ($, e, next) => {
    if (e.id === PANE) paneReady = false;
    return next(e);
  });
  on('ui.render', { component: 'Pane', requestId: PANE }, ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e);
    const status = nativeAttempt ? `Native delivery: ${nativeAttempt.state}. Recovery outcome remains unverified.\nIntervention: ${nativeAttempt.id}${nativeAttempt.recordingFailed ? '\nAn observation could not be saved; inspect APU artifacts.' : ''}` : paneText;
    const epoch = generation;
    const idleGeneration = turnGeneration;
    const eligible = paneReady && !busy && activeTurn === null && nativeAttempt === null && !e.props.view?.agentId;
    const children = [Text({ children: [status] })];
    // ButtonProps has no disabled property. Ineligible views expose no action.
    if (eligible) children.push(Button({ key: 'continue', label: 'Continue this session', hotkey: '1',
      onPress: async () => {
        if (epoch !== generation || idleGeneration !== turnGeneration) return;
        paneText = (await continueNative($)).text;
        $.ui.invalidate('ui.render');
      } }));
    return Box({ flexDirection: 'column', children });
  });
  on('turn.start', async ($, e, next) => {
    const attempt = nativeAttempt;
    activeTurn = e.turnId;
    turnGeneration += 1;
    if (attempt?.admitted && attempt.turn === null && isNativeTurn(e.text, attempt.prompt) && await sameSession($, attempt)) {
      attempt.turn = e.turnId;
      await observe($, attempt, 'started', ['--turn-id', e.turnId]);
    }
    return next(e);
  });
  on('turn.complete', async ($, e, next) => {
    const attempt = nativeAttempt;
    if (attempt?.turn === e.turnId && !e.agentId && await sameSession($, attempt))
      await observe($, attempt, 'turn-ended', ['--turn-id', e.turnId, '--reason', e.reason]);
    if (!e.agentId && activeTurn === e.turnId) activeTurn = null;
    $.ui.invalidate('ui.render');
    return next(e);
  });
}
