let diagnosis = null;
let generation = 0;
let busy = false;

function reset() {
  generation += 1;
  diagnosis = null;
  busy = false;
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
    return next(e);
  });
  on('classic.SessionStart', async ($, e, next) => { reset(); return next(e); });
  on('classic.CwdChanged', async ($, e, next) => { reset(); return next(e); });
  on('session.end', async ($, e, next) => { reset(); return next(e); });
  on('command.run', { command: 'apu-diagnose' }, async ($, e) =>
    e.args.trim() ? { text: 'Usage: /apu-diagnose (targets the current session only)' } : act($, false));
  on('command.run', { command: 'apu-prepare' }, async ($, e) =>
    e.args.trim() ? { text: 'Usage: /apu-prepare (uses this session’s saved diagnosis)' } : act($, true));
}
