import { expect, test } from 'claude-code/testing'

test('diagnosis passes exact identity and preparation reuses the saved diagnosis without execution', async ($, on) => {
  let calls = 0
  on('session.id', () => ({ value: 'exact-session' }))
  on('session.cwd', () => ({ value: '/work tree' }))
  on('process.run', ($, e) => {
    calls += 1
    if (calls === 1) {
      expect(e.argv).toEqual(['apu-wtf', '--provider', 'claude-code', '--session-id', 'exact-session', '--cwd', '/work tree', '--fresh', '--json'])
      return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({ provider: 'claude-code',
        diagnosis_id: 'diagnosis-one', incident_id: 'incident-one', status: 'likely-autonomy-loss',
        recommended_intervention: { durable_policy_mutation: false } }) } }
    }
    expect(e.argv).toEqual(['apu-intervene', '--provider', 'claude-code', '--diagnosis', 'diagnosis-one', '--dry-run', '--json'])
    return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({ provider: 'claude-code',
      diagnosis_id: 'diagnosis-one', intervention_id: 'intervention-one', status: 'planned',
      session_id: 'exact-session', executed: false, durable_policy_mutation: false, command: ['claude', '--resume', 'exact-session'] }) } }
  })
  expect((await $.command.run({ command: 'apu-diagnose', args: '' })).text).toContain('likely-autonomy-loss')
  const prepared = await $.command.run({ command: 'apu-prepare', args: '' })
  expect(prepared.text).toContain('outcome remains unverified')
  expect((await $.command.run({ command: 'apu-prepare', args: '' })).text).toBe(prepared.text)
  expect(calls).toBe(2)
})

test('a barrier refusal is visible and never submits a prompt', async ($, on) => {
  on('session.id', () => ({ value: 'exact-session' }))
  on('session.cwd', () => ({ value: '/work' }))
  on('process.run', ($, e) => ({ value: e.argv[0] === 'apu-wtf'
    ? { exitCode: 0, stderr: '', stdout: JSON.stringify({ provider: 'claude-code', diagnosis_id: 'd',
      incident_id: 'i', status: 'possible-legitimate-barrier', recommended_intervention: { durable_policy_mutation: false } }) }
    : { exitCode: 1, stderr: 'legitimate barrier may exist', stdout: '' } }))
  expect((await $.command.run({ command: 'apu-diagnose', args: '' })).text).toContain('possible-legitimate-barrier')
  expect((await $.command.run({ command: 'apu-prepare', args: '' })).text).toContain('APU refused')
})

test('resume and directory changes invalidate the prior diagnosis', async ($, on) => {
  let cwd = '/one'
  let calls = 0
  on('session.id', () => ({ value: 'same-session' }))
  on('session.cwd', () => ({ value: cwd }))
  on('classic.SessionStart', () => ({}))
  on('process.run', () => {
    calls += 1
    return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({ provider: 'claude-code',
      diagnosis_id: 'd', incident_id: 'i', status: 'insufficient-evidence' }) } }
  })
  await $.command.run({ command: 'apu-diagnose', args: '' })
  cwd = '/two'
  expect((await $.command.run({ command: 'apu-prepare', args: '' })).text).toContain('before preparing')
  await $.command.run({ command: 'apu-diagnose', args: '' })
  await $.classic.SessionStart({ source: 'resume' })
  expect((await $.command.run({ command: 'apu-prepare', args: '' })).text).toContain('before preparing')
  expect(calls).toBe(2)
})

test('missing journal attribution reports the reason without choosing a nearby session', async ($, on) => {
  let calls = 0
  on('session.id', () => ({ value: 'new-session' }))
  on('session.cwd', () => ({ value: '/work' }))
  on('process.run', () => {
    calls += 1
    return { value: { exitCode: 2, stderr: '', stdout: JSON.stringify({ kind: 'no_attribution', reason_code: 'no_matching_session' }) } }
  })
  expect((await $.command.run({ command: 'apu-diagnose', args: '' })).text).toContain('no_matching_session')
  expect((await $.command.run({ command: 'apu-prepare', args: '' })).text).toContain('before preparing')
  expect(calls).toBe(1)
})

test('a preparation for a different session cannot become the cached continuation', async ($, on) => {
  let calls = 0
  on('session.id', () => ({ value: 'current-session' }))
  on('session.cwd', () => ({ value: '/work' }))
  on('process.run', ($, e) => {
    calls += 1
    const value = e.argv[0] === 'apu-wtf'
      ? { provider: 'claude-code', diagnosis_id: 'd', incident_id: 'i', status: 'recovery-requested' }
      : { provider: 'claude-code', diagnosis_id: 'd', intervention_id: 'i', session_id: 'other-session',
          status: 'planned', executed: false, durable_policy_mutation: false, command: ['claude', '--resume', 'other-session'] }
    return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify(value) } }
  })
  await $.command.run({ command: 'apu-diagnose', args: '' })
  expect((await $.command.run({ command: 'apu-prepare', args: '' })).text).toContain('result unavailable')
  expect((await $.command.run({ command: 'apu-prepare', args: '' })).text).toContain('before preparing')
  expect(calls).toBe(2)
})

test('an unavailable executable is reported without retrying the process', async ($, on) => {
  let calls = 0
  on('session.id', () => ({ value: 'current-session' }))
  on('session.cwd', () => ({ value: '/work' }))
  on('process.run', () => { calls += 1; return { deny: 'executable unavailable' } })
  expect((await $.command.run({ command: 'apu-diagnose', args: '' })).text).toContain('No automatic retry')
  expect(calls).toBe(1)
})
