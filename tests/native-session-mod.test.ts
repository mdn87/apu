import { expect, test } from 'claude-code/testing'

const prompt = 'Continue the existing authorized task; do not change durable policy.'
const iid = 'native-' + 'a'.repeat(64)
const PANE = { plugin: 'apu-session', component: 'Pane', requestId: 'apu-continuation',
  viewport: { columns: 100, rows: 30 }, props: { title: 'APU continuation', isFocused: true,
    bodyColumns: 80, placement: 'inline', scroll: { offset: 0, bodyRows: 10 }, view: {} } } as const

function fixture(on, options = {}) {
  const calls = []
  let session = 'native-session'
  let cwd = '/native-work'
  on('session.id', () => ({ value: session }))
  on('session.cwd', () => ({ value: cwd }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('process.run', ($, e) => {
    calls.push(e.argv)
    const action = e.argv[e.argv.indexOf('--native-action') + 1]
    let value
    if (e.argv[0] === 'apu-wtf') value = {
      provider: 'claude-code', diagnosis_id: 'd', incident_id: 'i', status: 'likely-autonomy-loss',
      recommended_intervention: { durable_policy_mutation: false }
    }
    else if (e.argv.includes('--dry-run')) value = {
      provider: 'claude-code', diagnosis_id: 'd', intervention_id: 'prepared', session_id: session,
      status: 'planned', executed: false, durable_policy_mutation: false, command: ['claude', '--resume', session]
    }
    else value = { provider: 'claude-code', intervention_id: iid, diagnosis_id: 'd',
      session_id: session, executed: false, durable_policy_mutation: false,
      native_delivery: { state: action === 'claim' ? 'reserved' : action === 'admit' ? 'admitted' : action },
      should_submit: options.replay !== true, should_enter: options.refuse !== true, prompt }
    if (options.afterProcess) options.afterProcess(action, { changeSession: () => { session = 'other' }, changeCwd: () => { cwd = '/other' } })
    if (options.failAction === action) return { value: { exitCode: 1, stdout: '', stderr: 'refused' } }
    return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify(value) } }
  })
  return calls
}

async function prepare($) {
  await $.command.run({ command: 'apu-diagnose', args: '' })
  await $.command.run({ command: 'apu-prepare', args: '' })
}

async function panel($, surface: 'terminal' | 'desktop' = 'terminal') {
  expect((await $.command.run({ command: 'apu-continue', args: '' })).text).toContain('opening it submits nothing')
  return $.ui.mount({ ...PANE, surface })
}

for (const surface of ['terminal', 'desktop'] as const) {
  test(`native ${surface} button reserves once and never executes the resume argv`, async ($, on) => {
    const calls = fixture(on, { replay: true })
    let submits = 0
    on('prompt.submit', ($, e) => { submits += 1; return { text: e.text, origin: e.origin } })
    await prepare($)
    const ui = await panel($, surface)
    expect(calls.some(c => c.includes('claim'))).toBe(false)
    await ui.press({ key: 'continue' })
    expect(await ui.find({ type: 'Text', text: /already reserved/ })).toBeDefined()
    expect(submits).toBe(0)
    expect(calls.some(c => c[0] === 'claude')).toBe(false)
    expect(calls[calls.length - 1]).toEqual(['apu-intervene', '--provider', 'claude-code', '--native-action', 'claim',
      '--diagnosis', 'd', '--session-id', 'native-session', '--cwd', '/native-work', '--json'])
    await ui.unmount()
  })
}

test('continue requires preparation and is not an immediate command', async ($, on) => {
  const registrations = []
  on('command.register', ($, e) => { registrations.push(e); return { value: undefined } })
  on('session.start', ($, e) => e)
  fixture(on)
  await $.session.start({ cwd: '/native-work' })
  expect((await $.command.run({ command: 'apu-continue', args: '' })).text).toContain('/apu-prepare')
  expect(registrations.find(x => x.name === 'apu-continue').immediate).not.toBe(true)
})

test('session change while reserving prevents submission', async ($, on) => {
  const calls = fixture(on, { afterProcess(action, target) { if (action === 'claim') target.changeSession() } })
  let submits = 0
  on('prompt.submit', ($, e) => { submits += 1; return { text: e.text, origin: e.origin } })
  await prepare($)
  const ui = await panel($)
  await ui.press({ key: 'continue' })
  expect(await ui.find({ type: 'Text', text: /session changed/ })).toBeDefined()
  expect(submits).toBe(0)
  expect(calls.map(c => c[c.indexOf('--native-action') + 1])).toContain('outcome-unknown')
})

test('native admission and exact turn observations preserve unverified recovery', async ($, on) => {
  const calls = fixture(on)
  let submits = 0
  on('prompt.submit', ($, e) => { submits += 1; expect(e.origin.name).toBe('apu-session'); expect(e.origin.asUser).toBe(undefined); return { text: e.text, origin: e.origin } })
  on('turn.start', ($, e) => ({ turnId: e.turnId }))
  on('turn.complete', ($, e) => ({ text: e.answer }))
  await prepare($)
  const ui = await panel($)
  await ui.press({ key: 'continue' })
  expect(calls.map(c => c[c.indexOf('--native-action') + 1])).toContain('admit')
  expect(calls.some(c => c.includes('queued'))).toBe(true)
  await $.turn.start({ text: `The apu-session plugin sent a message:\n${prompt}\n\nThis is how Claude Code surfaces a prompt a plugin submits between turns — it starts this turn in the user's place. Address the message above.`, turnId: 'owned' })
  await $.turn.complete({ answer: '', durationMs: 1, isAborted: false, reason: 'answer', turnId: 'owned', agentId: 'child' })
  expect(calls.some(c => c.includes('turn-ended'))).toBe(false)
  await $.turn.complete({ answer: 'Finished turn', durationMs: 1, isAborted: false, reason: 'answer', turnId: 'owned' })
  expect(await ui.find({ type: 'Text', text: /turn-ended.*Recovery outcome remains unverified/ })).toBeDefined()
  expect(await ui.find({ type: 'Button', key: 'continue' })).toBeUndefined()
  expect(submits).toBe(1)
  expect(calls.filter(c => c.includes('claim')).length).toBe(1)
})

test('admission failure is fail-closed and recorded without replay', async ($, on) => {
  const calls = fixture(on, { failAction: 'admit' })
  let submits = 0
  on('prompt.submit', ($, e) => { submits += 1; return { text: e.text } })
  await prepare($)
  const ui = await panel($)
  await ui.press({ key: 'continue' })
  expect(calls.map(c => c[c.indexOf('--native-action') + 1])).toContain('dropped')
  expect(submits).toBe(0)
  expect(await ui.find({ type: 'Button', key: 'continue' })).toBeUndefined()
  expect(calls.filter(c => c.includes('claim')).length).toBe(1)
})

test('cwd change during admission prevents entering the prompt', async ($, on) => {
  const calls = fixture(on, { afterProcess(action, target) { if (action === 'admit') target.changeCwd() } })
  let submits = 0
  on('prompt.submit', ($, e) => { submits += 1; return { text: e.text } })
  await prepare($)
  const ui = await panel($)
  await ui.press({ key: 'continue' })
  expect(calls.map(c => c[c.indexOf('--native-action') + 1])).toContain('outcome-unknown')
  expect(submits).toBe(0)
})

test('a running turn removes the action without reserving', async ($, on) => {
  const calls = fixture(on)
  on('turn.start', ($, e) => ({ turnId: e.turnId }))
  await prepare($)
  const ui = await panel($)
  await $.turn.start({ text: 'another request', turnId: 'other' })
  // A rendered action from before turn.start cannot reserve while it waits
  // for a redraw; the new rendering then contains no actionable button.
  await ui.press({ key: 'continue' })
  expect(calls.some(c => c.includes('claim'))).toBe(false)
  await ui.redraw()
  expect(await ui.find({ type: 'Button', key: 'continue' })).toBeUndefined()
})

test('a stale rendered action cannot continue after a session reset', async ($, on) => {
  const calls = fixture(on)
  on('classic.SessionStart', () => ({}))
  await prepare($)
  const ui = await panel($)
  expect(await ui.find({ type: 'Button', key: 'continue' })).toBeDefined()
  // The existing button closure survives until the next draw. Its captured
  // generation must reject the press even when the session id is reused.
  await $.classic.SessionStart({ source: 'resume' })
  await ui.press({ key: 'continue' })
  expect(calls.some(c => c.includes('claim'))).toBe(false)
  await ui.redraw()
  expect(await ui.find({ type: 'Button', key: 'continue' })).toBeUndefined()
})

test('a subagent view does not offer the main-session continuation action', async ($, on) => {
  const calls = fixture(on)
  await prepare($)
  const ui = await panel($)
  expect(await ui.find({ type: 'Button', key: 'continue' })).toBeDefined()
  await ui.redraw({ ...PANE.props, view: { agentId: 'other-agent' } })
  expect(await ui.find({ type: 'Button', key: 'continue' })).toBeUndefined()
  expect(calls.some(c => c.includes('claim'))).toBe(false)
})

test('a refused native prompt records a drop and cannot be resent', async ($, on) => {
  const calls = fixture(on)
  let submits = 0
  on('prompt.submit', () => { submits += 1; return { drop: 'the current session declined this prompt' } })
  await prepare($)
  const ui = await panel($)
  await ui.press({ key: 'continue' })
  expect(calls.some(c => c.includes('dropped'))).toBe(true)
  expect(calls.some(c => c.includes('queued'))).toBe(false)
  expect(await ui.find({ type: 'Button', key: 'continue' })).toBeUndefined()
  expect(submits).toBe(1)
  expect(calls.filter(c => c.includes('claim')).length).toBe(1)
})

test('a failed observation write remains visible and does not reopen submission', async ($, on) => {
  const calls = fixture(on, { failAction: 'queued' })
  let submits = 0
  on('prompt.submit', ($, e) => { submits += 1; return { text: e.text, origin: e.origin } })
  await prepare($)
  const ui = await panel($)
  await ui.press({ key: 'continue' })
  expect(await ui.find({ type: 'Text', text: /observation could not be saved/ })).toBeDefined()
  expect(await ui.find({ type: 'Button', key: 'continue' })).toBeUndefined()
  expect(submits).toBe(1)
  expect(calls.filter(c => c.includes('claim')).length).toBe(1)
})

test('an unavailable submit response records uncertainty without another attempt', async ($, on) => {
  const calls = fixture(on)
  let submits = 0
  // The test's engine boundary cannot say whether the effect occurred before
  // its response was lost. It must not be treated as a safe-to-retry refusal.
  on('prompt.submit', () => { submits += 1; throw new Error('submit response unavailable') })
  await prepare($)
  const ui = await panel($)
  await ui.press({ key: 'continue' })
  expect(calls.some(c => c.includes('outcome-unknown'))).toBe(true)
  expect(calls.some(c => c.includes('dropped'))).toBe(false)
  expect(await ui.find({ type: 'Text', text: /outcome-unknown.*Recovery outcome remains unverified/ })).toBeDefined()
  expect(await ui.find({ type: 'Button', key: 'continue' })).toBeUndefined()
  expect(submits).toBe(1)
  expect(calls.filter(c => c.includes('claim')).length).toBe(1)
})
