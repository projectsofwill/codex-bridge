import { describe, expect, mock, test } from 'claude-code/testing'

const BAND = { component: 'AbovePrompt', props: { hasSurvey: false, isWorking: false, maxRows: 10, bodyColumns: 140 } }

// Stands for the engine beneath the mod. `answers` maps a bridge subcommand to its JSON reply
// (a function gets the parsed stdin and the call count).
const CONFIG = { ok: true, models: { cheap: 'm-cheap', standard: 'm-std', strong: 'm-strong' }, stakes: ['irreversible', 'policy', 'trust-boundary', 'unattended'], stakes_aliases: { backbone: 'unattended' }, review: { R0: 'none', R1: 'sonnet', R2: 'opus' }, protected: ['.claude/', 'agents.md'] }

// opts.raw: a bridge subcommand whose process prints this instead of JSON; opts.commandDeny: command.register throws.
function engine(on: any, answers: Record<string, any>, opts: { spawnDeny?: string; raw?: Record<string, string>; commandDeny?: boolean } = {}) {
  const calls: { cmd: string; args: string[]; stdin: any }[] = []
  const toasts: string[] = []
  const prompts: string[] = []
  const spawns: any[] = []
  const order: string[] = [] // bridge commands and 'prompt' submits, in call order
  const store: Record<string, unknown> = {}
  on('session.start', (_$: any, e: any) => ({ sessionId: 's', cwd: e.cwd }))
  on('process.run', (_$: any, e: any) => {
    if (!String(e.argv[1] ?? '').endsWith('bridge.py')) return { value: { exitCode: 0, stdout: 'Python 3.13.2', stderr: '' } } // python probe
    const [, , cmd, ...args] = e.argv
    const stdin = e.init?.stdin ? JSON.parse(e.init.stdin) : undefined
    calls.push({ cmd, args, stdin })
    order.push(cmd)
    if (opts.raw?.[cmd] !== undefined) return { value: { exitCode: 1, stdout: '', stderr: opts.raw[cmd] } }
    const a = answers[cmd] ?? (cmd === 'report-begin' ? { ok: true, won: true } : cmd === 'claim' ? { ok: true, claimed: true } : cmd === 'mark' ? { ok: true, marked: true } : cmd === 'config' ? CONFIG : undefined) // default: this session reports
    const n = calls.filter(c => c.cmd === cmd).length
    const body = typeof a === 'function' ? a(stdin, n, args) : (a ?? { ok: true })
    return { value: { exitCode: 0, stdout: JSON.stringify(body), stderr: '' } }
  })
  on('fs.read', () => ({ value: 'REVIEW {{PACKET}} {{WORKTREE}} {{SNAPSHOT}}' }))
  on('store.get', (_$: any, e: any) => ({ value: store[e.key] }))
  on('store.set', (_$: any, e: any) => ((store[e.key] = e.value), { value: undefined }))
  on('tool.register', () => ({ value: undefined }))
  on('command.register', (_$: any, e: any) => { if (opts.commandDeny) throw new Error('name taken'); return { value: { command: e.name } } })
  on('agent.register', () => ({ value: undefined }))
  const clock = mock.clock(on, { now: Date.now() })
  on('agent.spawn', (_$: any, e: any) => (spawns.push(e), opts.spawnDeny ? { deny: opts.spawnDeny } : { model: 'opus', agentId: 'rev-1' }))
  on('prompt.submit', (_$: any, e: any) => (prompts.push(e.text), order.push('prompt'), { text: e.text }))
  on('ui.toast', (_$: any, e: any) => (toasts.push(e.text), { value: undefined }))
  on('turn.complete', () => ({ text: '' }))
  on('ui.render', ($: any, e: any) => $.ui.resolve(e).Text({ children: 'engine band' }))
  on('tool.call', { tool: 'Agent' }, () => ({ result: 'agent ran' }))
  return { calls, toasts, prompts, spawns, store, clock, order }
}

const START = { surface: 'terminal', isInteractive: true, cwd: '/work' } as any

describe('codex-bridge', () => {
  test('gate tool relays the receipt, findings verbatim and gate_satisfied, and logs a burn line', async ($, on) => {
    const env = engine(on, {
      status: { ok: true, jobs: [] },
      ask: { ok: true, mode: 'gate', codex_version: 'codex-cli 0.161.0', cwd: '/repo', base_rev: 'abc', thread_id: 't1',
        read_evidence: { 'app.py': 'quoted line 7 verbatim' }, context_objections: [], attempts: [{ exit: 0 }],
        effort: 'medium passed', usage: { input_tokens: 9 }, final: 'FINDING 1: race in lock()', gate_satisfied: true },
    })
    await $.session.start(START)
    const r: any = await $.tool.call({ tool: 'mcp__codex-bridge__codex', mode: 'gate', prompt: 'p', files: ['app.py'], diff: '+x', cwd: '/repo' } as any)
    expect(r.result).toContain('FINDING 1: race in lock()')
    expect(r.result).toContain('app.py: quoted line 7 verbatim')
    expect(r.result).toMatch(/gate_satisfied: true$/)
    const burn = env.calls.find(c => c.cmd === 'burn')!
    expect(burn.stdin.lane).toBe('codex-bridge-gate')
    expect(burn.stdin.total_claude_tokens).toBe(null) // never a fake 0: main-loop tokens are invisible to the mod
    expect(burn.stdin.result_chars).toBeGreaterThan(0)
  })

  test('the mod never blocks other agents (old-agent coordination is by convention, as today)', async ($, on) => {
    const env = engine(on, { status: { ok: true, jobs: [] } })
    await $.session.start(START)
    const r: any = await $.tool.call({ tool: 'Agent', subagent_type: 'codex-gate', prompt: 'x', description: 'd' } as any)
    expect(r.result).toBe('agent ran')
    expect(env.calls.some(c => c.cmd === 'lock' || c.cmd.startsWith('lease'))).toBe(false)
  })

  test('a clean job starts exactly one review; a failed job is reported once with no review', async ($, on) => {
    const claims = new Set<string>()
    const now = new Date().toISOString()
    const env = engine(on, {
      status: () => ({ ok: true, jobs: [
        { id: 'j-clean01', local: true, state: 'receipted', outcome: 'clean', created: now, task: 'clean task', reported: false },
        { id: 'j-red0002', local: true, state: 'receipted', outcome: 'verify-failed', created: now, task: 'red task', reported: claims.has('j-red0002') },
      ] }),
      'review-start': (_: any, n: number) => (n === 1 ? { ok: true, started: true, packet: '/p', worktree: '/w', snapshot: 's1' } : { ok: true, started: false }),
      claim: (_: any, __: number, args: string[]) => (claims.add(args[0]!), { ok: true, claimed: true }),
      result: { ok: true, state: { outcome: 'verify-failed' }, receipt: { computed: { codex_tokens: {}, changed: ['a'] } } },
    })
    await $.session.start(START)
    await env.clock.advance(5000)
    await env.clock.advance(5000)
    expect(env.spawns.length).toBe(1)
    expect(env.spawns[0].subagent_type).toBe('codex-bridge:reviewer')
    expect(env.prompts.length).toBe(1)
    expect(env.prompts[0]).toContain('j-red0002')
    expect(env.prompts[0]).toContain('verify-failed')
    const order = env.calls.map(c => c.cmd).filter(c => c === 'claim')
    expect(order.length).toBe(1) // recorded once, after the report was submitted
  })

  test('config drives reviewers per tier and the gate stakes enum', async ($, on) => {
    const now = new Date().toISOString()
    const marked = new Set<string>()
    const job = (id: string, tier: string) => ({ id, local: true, state: marked.has(id) ? 'notified' : 'receipted', outcome: 'clean', created: now, task: 't', reported: false, tier })
    const env = engine(on, {
      config: { ...CONFIG, review: { R0: 'none', R1: 'none', R2: 'sonnet' } },
      status: () => ({ ok: true, jobs: [job('j-r1bbbb', 'R1'), job('j-r2cccc', 'R2')] }),
      'review-start': (_: any, __: number, args: string[]) => (marked.add(args[0]!), { ok: true, started: true, packet: '/p', worktree: '/w', snapshot: 's1', verifier_changes: [] }),
      mark: (_: any, __: number, args: string[]) => (marked.add(args[0]!), { ok: true, marked: true }),
      result: { ok: true, state: { outcome: 'clean' }, receipt: { computed: { codex_tokens: {}, changed: ['a'] } } },
    })
    await $.session.start(START)
    await env.clock.advance(5000)
    expect(env.spawns.map(s => [s.description.split(' ').pop(), s.subagent_type])).toEqual([['j-r2cccc', 'codex-bridge:reviewer-sonnet']])
    expect(env.prompts.some(p => p.includes('j-r1bbbb') && p.includes('no auto-review'))).toBe(true) // R1 set to none
  })

  test('0.1.2: review depth by tier: R0 none (Sonnet if it changed a verifier), R1 Sonnet, R2/unknown Opus', async ($, on) => {
    const now = new Date().toISOString()
    const marked = new Set<string>()
    const job = (id: string, tier: string) => ({ id, local: true, state: marked.has(id) ? 'notified' : 'receipted', outcome: 'clean', created: now, task: 't', reported: false, tier })
    const vc: Record<string, string[]> = { 'j-r0vvvv': ['tests/t.py'] }
    const env = engine(on, {
      status: () => ({ ok: true, jobs: [job('j-r0aaaa', 'R0'), job('j-r0vvvv', 'R0'), job('j-r1bbbb', 'R1'), job('j-r2cccc', 'R2'), job('j-xxdddd', 'constructor')] }),
      'review-start': (_: any, __: number, args: string[]) => (marked.add(args[0]!), { ok: true, started: true, packet: '/p', worktree: '/w', snapshot: 's1', verifier_changes: vc[args[0]!] ?? [] }),
      mark: (_: any, __: number, args: string[]) => (marked.add(args[0]!), { ok: true }),
      result: { ok: true, state: { outcome: 'clean' }, receipt: { computed: { codex_tokens: {}, changed: ['a'] } } },
    })
    await $.session.start(START)
    await env.clock.advance(5000)
    await env.clock.advance(5000)
    // every tier passes review-start (snapshot check + exclusive claim), even R0
    expect(env.calls.filter(c => c.cmd === 'review-start').map(c => c.args[0]).sort()).toEqual(['j-r0aaaa', 'j-r0vvvv', 'j-r1bbbb', 'j-r2cccc', 'j-xxdddd'])
    const by = Object.fromEntries(env.spawns.map(s => [s.description.split(' ').pop(), s.subagent_type]))
    expect(by).toEqual({ 'j-r0vvvv': 'codex-bridge:reviewer-sonnet', 'j-r1bbbb': 'codex-bridge:reviewer-sonnet', 'j-r2cccc': 'codex-bridge:reviewer', 'j-xxdddd': 'codex-bridge:reviewer' })
    expect(env.prompts.length).toBe(1) // only the plain R0 job is reported without a review
    expect(env.prompts[0]).toContain('j-r0aaaa')
    expect(env.prompts[0]).toContain('no auto-review')
    expect(env.calls.find(c => c.cmd === 'burn')!.stdin.review_verdict).toBe('none (R0)')
  })

  test('the reviewer verdict is recorded against its snapshot and not re-submitted', async ($, on) => {
    const env = engine(on, { status: { ok: true, jobs: [] }, verdict: { ok: true, stale: false }, result: { ok: true, state: { outcome: 'clean' }, receipt: { computed: {} } } })
    await $.session.start(START)
    env.store.reviewing = { 'rev-1': { job: 'j-clean01', kind: 'review', snapshot: 's1', gen: 1 } }
    await $.turn.complete({ reason: 'answer', answer: 'ok\nVERDICT-JSON\n{"verdict": "accept", "snapshot": "s1"}\nEND-VERDICT-JSON', durationMs: 1, agentId: 'rev-1', usage: { input_tokens: 10, output_tokens: 2 } } as any)
    const v = env.calls.find(c => c.cmd === 'verdict')!
    expect(v.args).toEqual(['j-clean01'])
    expect(v.stdin.verdict).toBe('accept')
    expect(v.stdin.snapshot).toBe('s1')
    expect(env.prompts.length).toBe(0)
    expect(env.toasts.some(t => t.includes('accept'))).toBe(true)
  })

  test('0.1.2: an accepted review sends no separate resume prompt; a suggested patch still gets one', async ($, on) => {
    const VERDICT = 'ok\nVERDICT-JSON\n{"verdict": "accept", "snapshot": "s1"}\nEND-VERDICT-JSON'
    let computed: Record<string, unknown> = { resume: 'codex resume r1' }
    const env = engine(on, { status: { ok: true, jobs: [] }, verdict: { ok: true, stale: false }, result: () => ({ ok: true, state: { outcome: 'clean' }, receipt: { computed } }) })
    await $.session.start(START)
    env.store.reviewing = { 'rev-1': { job: 'j-clean01', kind: 'review', snapshot: 's1', gen: 1 } }
    await $.turn.complete({ reason: 'answer', answer: VERDICT, durationMs: 1, agentId: 'rev-1' } as any)
    expect(env.prompts.length).toBe(0) // resume alone lives in codex_result
    computed = { resume: 'codex resume r2', suggested_patch: { files: ['context/a.md'], lines: 2, path: '/j/s.patch' } }
    env.store.reviewing = { 'rev-1': { job: 'j-clean02', kind: 'review', snapshot: 's1', gen: 1 } }
    await $.turn.complete({ reason: 'answer', answer: VERDICT, durationMs: 1, agentId: 'rev-1' } as any)
    expect(env.prompts.length).toBe(1)
    expect(env.prompts[0]).toContain('context/a.md')
    expect(env.prompts[0]).toContain('codex resume r2')
  })

  test('a review without VERDICT-JSON is re-run once, not recorded', async ($, on) => {
    const env = engine(on, { status: { ok: true, jobs: [] }, 'review-retry': { ok: true, retried: true }, result: { ok: true, state: { outcome: 'clean' }, receipt: { computed: {} } } })
    await $.session.start(START)
    env.store.reviewing = { 'rev-1': { job: 'j-clean01', kind: 'review', snapshot: 's1', gen: 1 } }
    await $.turn.complete({ reason: 'answer', answer: 'Recommend accept. (no block)', durationMs: 1, agentId: 'rev-1', usage: { input_tokens: 5, output_tokens: 5 } } as any)
    expect(env.calls.find(c => c.cmd === 'review-retry')!.args).toEqual(['j-clean01', '1'])
    expect(env.calls.some(c => c.cmd === 'verdict')).toBe(false)
    expect(env.calls.some(c => c.cmd === 'mark')).toBe(false)
    expect(env.prompts.length).toBe(1) // the prose "accept" already reached the main session: correct it
    expect(env.prompts[0]).toContain('NO verdict was recorded')
    expect(env.prompts[0]).toContain('Do not treat this job as accepted')
    const b = env.calls.find(c => c.cmd === 'burn')!.stdin
    expect(b.review_verdict).toBe('retry (no VERDICT-JSON)')
    expect(b.reviewer_tokens).toBe(10)
  })

  test('a second missing VERDICT-JSON is recorded; the correction names the real cause, not a changed worktree', async ($, on) => {
    const env = engine(on, { status: { ok: true, jobs: [] }, 'review-retry': { ok: true, retried: false }, verdict: { ok: true, stale: true, worktree_changed: false, verdict: 'invalid', downgraded: ["unknown verdict 'unparseable'"] }, result: { ok: true, state: { outcome: 'clean' }, receipt: { computed: {} } } })
    await $.session.start(START)
    env.store.reviewing = { 'rev-1': { job: 'j-clean01', kind: 'review', snapshot: 's1', gen: 1 } }
    await $.turn.complete({ reason: 'answer', answer: 'Recommend accept. (no block)', durationMs: 1, agentId: 'rev-1' } as any)
    expect(env.calls.find(c => c.cmd === 'verdict')!.stdin.verdict).toBe('unparseable')
    expect(env.prompts.length).toBe(1)
    expect(env.prompts[0]).toContain('did not name the reviewed snapshot')
    expect(env.prompts[0]).not.toContain('worktree changed')
    expect(env.calls.some(c => c.cmd === 'mark' && c.args[1] === 'notified')).toBe(true)
  })

  test('a superseded reviewer prose accept is neutralized, and nothing is recorded or marked', async ($, on) => {
    const env = engine(on, { status: { ok: true, jobs: [] }, 'review-retry': { ok: true, retried: false, superseded: true } })
    await $.session.start(START)
    env.store.reviewing = { 'rev-1': { job: 'j-clean01', kind: 'review', snapshot: 's1', gen: 111.5 } }
    await $.turn.complete({ reason: 'answer', answer: 'Recommend accept.', durationMs: 1, agentId: 'rev-1' } as any)
    expect(env.calls.find(c => c.cmd === 'review-retry')!.args).toEqual(['j-clean01', '111.5'])
    expect(env.calls.some(c => c.cmd === 'verdict' || c.cmd === 'mark' || c.cmd === 'burn')).toBe(false)
    expect(env.prompts.length).toBe(1)
    expect(env.prompts[0]).toContain('SUPERSEDED')
  })

  test('a superseded parsed verdict is corrected and never marks the job notified', async ($, on) => {
    const env = engine(on, { status: { ok: true, jobs: [] }, verdict: { ok: false, error: 'superseded: a newer review of this job replaced this reviewer; nothing recorded' } })
    await $.session.start(START)
    env.store.reviewing = { 'rev-1': { job: 'j-clean01', kind: 'review', snapshot: 's1', gen: 111.5 } }
    await $.turn.complete({ reason: 'answer', answer: 'VERDICT-JSON\n{"verdict": "accept", "snapshot": "s1"}\nEND-VERDICT-JSON', durationMs: 1, agentId: 'rev-1' } as any)
    expect(env.calls.some(c => c.cmd === 'mark')).toBe(false)
    expect(env.prompts.length).toBe(1)
    expect(env.prompts[0]).toContain('SUPERSEDED')
  })

  test('marking notified carries the review generation', async ($, on) => {
    const env = engine(on, { status: { ok: true, jobs: [] }, verdict: { ok: true, stale: false }, result: { ok: true, state: { outcome: 'clean' }, receipt: { computed: {} } } })
    await $.session.start(START)
    env.store.reviewing = { 'rev-1': { job: 'j-clean01', kind: 'review', snapshot: 's1', gen: 111.5 } }
    await $.turn.complete({ reason: 'answer', answer: 'VERDICT-JSON\n{"verdict": "accept", "snapshot": "s1"}\nEND-VERDICT-JSON', durationMs: 1, agentId: 'rev-1' } as any)
    expect(env.calls.find(c => c.cmd === 'mark')!.args).toEqual(['j-clean01', 'notified', '111.5'])
  })

  test('the review generation rides on the verdict', async ($, on) => {
    const env = engine(on, { status: { ok: true, jobs: [] }, verdict: { ok: true, stale: false }, result: { ok: true, state: { outcome: 'clean' }, receipt: { computed: {} } } })
    await $.session.start(START)
    env.store.reviewing = { 'rev-1': { job: 'j-clean01', kind: 'review', snapshot: 's1', gen: 111.5 } }
    await $.turn.complete({ reason: 'answer', answer: 'VERDICT-JSON\n{"verdict": "accept", "snapshot": "s1"}\nEND-VERDICT-JSON', durationMs: 1, agentId: 'rev-1' } as any)
    expect(env.calls.find(c => c.cmd === 'verdict')!.stdin.review_gen).toBe(111.5)
  })

  test('a reviewer entry without a generation is treated as superseded, never recorded', async ($, on) => {
    const env = engine(on, { status: { ok: true, jobs: [] } })
    await $.session.start(START)
    env.store.reviewing = { 'rev-1': { job: 'j-clean01', kind: 'review', snapshot: 's1' } }
    await $.turn.complete({ reason: 'answer', answer: 'VERDICT-JSON\n{"verdict": "accept", "snapshot": "s1"}\nEND-VERDICT-JSON', durationMs: 1, agentId: 'rev-1' } as any)
    expect(env.calls.some(c => ['verdict', 'review-retry', 'mark'].includes(c.cmd))).toBe(false)
    expect(env.prompts[0]).toContain('SUPERSEDED')
  })

  test('a failed job another session is already reporting gets no second report, mark or burn row', async ($, on) => {
    const env = engine(on, {
      status: { ok: true, jobs: [{ id: 'j-fail01', local: true, state: 'receipted', outcome: 'verify-failed', created: new Date().toISOString(), task: 't', reported: false }] },
      'report-begin': { ok: true, won: false, reason: 'another session is reporting' },
      result: { ok: true, state: { outcome: 'verify-failed' }, receipt: { computed: {} } },
    })
    await $.session.start(START)
    await env.clock.advance(5000)
    expect(env.calls.some(c => c.cmd === 'report-begin' && c.args[0] === 'j-fail01')).toBe(true)
    expect(env.prompts.length).toBe(0)
    expect(env.calls.some(c => c.cmd === 'mark' || c.cmd === 'burn' || (c.cmd === 'claim' && c.args[1] === 'notify'))).toBe(false)
  })

  test('a verdict whose report lease is lost still logs its reviewer cost, but never corrects or marks', async ($, on) => {
    const env = engine(on, {
      status: { ok: true, jobs: [] }, 'report-begin': { ok: true, won: false },
      verdict: { ok: true, stale: false, verdict: 'fix-list', downgraded: ['accept with missing or unmet requirements'] },
      result: { ok: true, state: { outcome: 'clean' }, receipt: { computed: {} } },
    })
    await $.session.start(START)
    env.store.reviewing = { 'rev-1': { job: 'j-clean01', kind: 'review', snapshot: 's1', gen: 1 } }
    await $.turn.complete({ reason: 'answer', answer: 'VERDICT-JSON\n{"verdict": "accept", "snapshot": "s1"}\nEND-VERDICT-JSON', durationMs: 1, agentId: 'rev-1', usage: { input_tokens: 3, output_tokens: 4 } } as any)
    expect(env.prompts.length).toBe(0)
    expect(env.calls.some(c => c.cmd === 'mark')).toBe(false)
    expect(env.calls.find(c => c.cmd === 'burn')!.stdin.reviewer_tokens).toBe(7)
  })

  test('a reporter that loses the notify claim after a lease expiry neither marks nor burns', async ($, on) => {
    const env = engine(on, {
      status: { ok: true, jobs: [{ id: 'j-fail02', local: true, state: 'receipted', outcome: 'verify-failed', created: new Date().toISOString(), task: 't', reported: false }] },
      claim: { ok: true, claimed: false },
      result: { ok: true, state: { outcome: 'verify-failed' }, receipt: { computed: {} } },
    })
    await $.session.start(START)
    await env.clock.advance(5000)
    expect(env.prompts.length).toBe(1) // a duplicate delivery is the accepted direction
    expect(env.calls.some(c => c.cmd === 'mark' || c.cmd === 'burn')).toBe(false) // duplicate accounting is not
  })

  test('the suggested-patch prompt goes out before the job is marked notified', async ($, on) => {
    const env = engine(on, { status: { ok: true, jobs: [] }, verdict: { ok: true, stale: false }, result: { ok: true, state: { outcome: 'clean' }, receipt: { computed: { suggested_patch: { files: ['context/a.md'], lines: 2, path: '/j/s.patch' } } } } })
    await $.session.start(START)
    env.store.reviewing = { 'rev-1': { job: 'j-clean01', kind: 'review', snapshot: 's1', gen: 1 } }
    await $.turn.complete({ reason: 'answer', answer: 'VERDICT-JSON\n{"verdict": "accept", "snapshot": "s1"}\nEND-VERDICT-JSON', durationMs: 1, agentId: 'rev-1' } as any)
    expect(env.order.indexOf('prompt')).toBeGreaterThan(-1)
    expect(env.order.indexOf('prompt')).toBeLessThan(env.order.indexOf('mark'))
  })

  test('an old reviewer whose mark is superseded never claims the replacement review notification', async ($, on) => {
    const env = engine(on, { status: { ok: true, jobs: [] }, verdict: { ok: false, error: 'review lock busy; retry later' }, mark: { ok: true, marked: false, superseded: true }, result: { ok: true, state: { outcome: 'clean' }, receipt: { computed: {} } } })
    await $.session.start(START)
    env.store.reviewing = { 'rev-1': { job: 'j-clean01', kind: 'review', snapshot: 's1', gen: 1 } }
    await $.turn.complete({ reason: 'answer', answer: 'VERDICT-JSON\n{"verdict": "accept", "snapshot": "s1"}\nEND-VERDICT-JSON', durationMs: 1, agentId: 'rev-1' } as any)
    expect(env.calls.some(c => c.cmd === 'mark')).toBe(true)
    expect(env.calls.some(c => c.cmd === 'claim' && c.args[1] === 'notify')).toBe(false)
  })

  test('a downgraded accept is shown as recorded and corrected in the main session', async ($, on) => {
    const env = engine(on, { status: { ok: true, jobs: [] }, verdict: { ok: true, stale: false, verdict: 'fix-list', downgraded: ['accept with missing or unmet requirements'] }, result: { ok: true, state: { outcome: 'clean' }, receipt: { computed: {} } } })
    await $.session.start(START)
    env.store.reviewing = { 'rev-1': { job: 'j-clean01', kind: 'review', snapshot: 's1', gen: 1 } }
    await $.turn.complete({ reason: 'answer', answer: 'VERDICT-JSON\n{"verdict": "accept", "snapshot": "s1"}\nEND-VERDICT-JSON', durationMs: 1, agentId: 'rev-1' } as any)
    expect(env.toasts.some(t => t.includes('fix-list'))).toBe(true)
    expect(env.toasts.some(t => t.includes('accept'))).toBe(false)
    expect(env.prompts.length).toBe(1)
    expect(env.prompts[0]).toContain('Do not treat this job as accepted')
    expect(env.calls.find(c => c.cmd === 'burn')!.stdin.review_verdict).toBe('fix-list')
  })

  test('a verdict that fails to record is shown as unrecorded, never as the reviewer claim', async ($, on) => {
    const env = engine(on, { status: { ok: true, jobs: [] }, verdict: { ok: false, error: 'boom' }, result: { ok: true, state: { outcome: 'clean' }, receipt: { computed: {} } } })
    await $.session.start(START)
    env.store.reviewing = { 'rev-1': { job: 'j-clean01', kind: 'review', snapshot: 's1', gen: 1 } }
    await $.turn.complete({ reason: 'answer', answer: 'VERDICT-JSON\n{"verdict": "accept", "snapshot": "s1"}\nEND-VERDICT-JSON', durationMs: 1, agentId: 'rev-1' } as any)
    expect(env.toasts.some(t => t.includes('unrecorded'))).toBe(true)
    expect(env.prompts.length).toBe(1)
    expect(env.prompts[0]).toContain('could not be recorded: boom')
  })

  test('a verdict whose bridge call REJECTS still fails closed with a correction', async ($, on) => {
    const boom = () => { throw new Error('spawn EAGAIN') }
    const env = engine(on, { status: { ok: true, jobs: [] }, verdict: boom, result: boom })
    await $.session.start(START)
    env.store.reviewing = { 'rev-1': { job: 'j-clean01', kind: 'review', snapshot: 's1', gen: 1 } }
    await $.turn.complete({ reason: 'answer', answer: 'VERDICT-JSON\n{"verdict": "accept", "snapshot": "s1"}\nEND-VERDICT-JSON', durationMs: 1, agentId: 'rev-1' } as any)
    expect(env.toasts.some(t => t.includes('unrecorded'))).toBe(true)
    expect(env.prompts.length).toBe(1)
    expect(env.prompts[0]).toContain('could not be recorded')
    expect(env.prompts[0]).toContain('Do not treat this job as accepted')
  })

  test('a clean job whose review cannot start (stale receipt) is reported once and retired', async ($, on) => {
    const claims = new Set<string>()
    const env = engine(on, {
      status: () => ({ ok: true, jobs: [{ id: 'j-stale01', local: true, state: 'receipted', outcome: 'clean', created: new Date().toISOString(), task: 't', reported: claims.has('j-stale01') }] }),
      'review-start': { ok: false, error: 'refused: the worktree changed after its receipt' },
      claim: (_: any, __: number, args: string[]) => (claims.add(args[0]!), { ok: true, claimed: true }),
    })
    await $.session.start(START)
    await env.clock.advance(5000)
    await env.clock.advance(5000)
    expect(env.prompts.length).toBe(1)
    expect(env.prompts[0]).toContain('worktree changed after its receipt')
    expect(env.calls.some(c => c.cmd === 'mark' && c.args[0] === 'j-stale01')).toBe(true)
  })

  test('a clean job whose reviewer cannot spawn is put back and reported once', async ($, on) => {
    const claims = new Set<string>()
    const env = engine(on, {
      status: () => ({ ok: true, jobs: [{ id: 'j-clean01', local: true, state: 'receipted', outcome: 'clean', created: new Date().toISOString(), task: 't', reported: claims.has('j-clean01') }] }),
      'review-start': (_: any, n: number) => (n === 1 ? { ok: true, started: true, packet: '/p', worktree: '/w', snapshot: 's1' } : { ok: true, started: false }),
      claim: (_: any, __: number, args: string[]) => (claims.add(args[0]!), { ok: true, claimed: true }),
    }, { spawnDeny: 'opus limit reached' })
    await $.session.start(START)
    await env.clock.advance(5000)
    await env.clock.advance(5000)
    expect(env.calls.some(c => c.cmd === 'review-failed')).toBe(true)
    expect(env.prompts.length).toBe(1)
    expect(env.prompts[0]).toContain('could not start')
  })

  test('a verdict recorded but never reported is reported on the next tick', async ($, on) => {
    const claims = new Set<string>()
    const env = engine(on, {
      status: () => ({ ok: true, jobs: [{ id: 'j-rev0003', local: true, state: 'reviewed', outcome: 'clean', created: new Date().toISOString(), task: 't', reported: claims.has('j-rev0003') }] }),
      result: { ok: true, verdict: { verdict: 'accept', stale: false } },
      claim: (_: any, __: number, args: string[]) => (claims.add(args[0]!), { ok: true, claimed: true }),
    })
    await $.session.start(START)
    await env.clock.advance(5000)
    await env.clock.advance(5000)
    expect(env.prompts.length).toBe(1)
    expect(env.prompts[0]).toContain('accept')
    expect(env.calls.some(c => c.cmd === 'mark' && c.args[1] === 'notified')).toBe(true)
  })

  test('band shows running jobs, and the engine band when idle', async ($, on) => {
    engine(on, { status: { ok: true, jobs: [{ id: '20261008-x-abc123', local: true, state: 'running', created: new Date().toISOString(), task: 'refactor parser' }] } })
    await $.session.start(START)
    const band = await $.ui.mount({ plugin: 'codex-bridge', surface: 'terminal', ...BAND } as any)
    expect(await band.find({ type: 'Text', text: /Codex job abc123 · running/ })).toBeDefined()
    await band.unmount()
  })

  test('0.1.1: gate reply shows model, quota cost and resume; burn logs them', async ($, on) => {
    const env = engine(on, {
      status: { ok: true, jobs: [] }, usage: { ok: true, summary: '5h 10% (resets x) · wk 20% (resets y)', windows: { primary: { used: 10 } } },
      ask: { ok: true, mode: 'gate', thread_id: 't9', read_evidence: {}, context_objections: [], attempts: [{ exit: 0 }],
        model: 'gpt-6.1-sol', effort: 'high', model_reason: 'gpt-6.1-sol/high because trigger=trust-boundary',
        quota: { delta: { primary: 1, secondary: 0.5 } }, resume: 'codex resume t9', final: 'ok', gate_satisfied: true },
    })
    await $.session.start(START)
    const r: any = await $.tool.call({ tool: 'mcp__codex-bridge__codex', mode: 'gate', trigger: 'trust-boundary', prompt: 'p', diff: '+x', cwd: '/repo', junk: 1 } as any)
    expect(r.result).toContain('gpt-6.1-sol / high (gpt-6.1-sol/high because trigger=trust-boundary)')
    expect(r.result).toContain('5h +1 pts, wk +0.5 pts')
    expect(r.result).toContain('codex resume t9')
    const ask = env.calls.find(c => c.cmd === 'ask')!
    expect(ask.stdin.trigger).toBe('trust-boundary')
    expect(ask.stdin.junk).toBeUndefined()
    expect(ask.stdin.protocol).toBe(2)
    const burn = env.calls.find(c => c.cmd === 'burn')!
    expect(burn.stdin.quota_delta).toEqual({ primary: 1, secondary: 0.5 })
    expect(burn.stdin.model).toBe('gpt-6.1-sol')
    expect(burn.stdin.trigger).toBe('trust-boundary')
  })

  test('0.1.1: cancel tool forwards to the bridge', async ($, on) => {
    const env = engine(on, { status: { ok: true, jobs: [] }, cancel: { ok: true, cancelled: true, note: 'Codex stopped' } })
    await $.session.start(START)
    const r: any = await $.tool.call({ tool: 'mcp__codex-bridge__codex_cancel', job_id: 'j1' } as any)
    expect(r.result).toContain('Cancelled: Codex stopped')
    expect(env.calls.some(c => c.cmd === 'cancel' && c.args[0] === 'j1')).toBe(true)
  })

  test('0.1.1: the limits readout appears idle only once a window is hot', async ($, on) => {
    const env = engine(on, { status: { ok: true, jobs: [] }, usage: { ok: true, summary: '5h 82% (resets 18:00)', windows: { primary: { used: 82 } } } })
    await $.session.start(START)
    await env.clock.advance(10)
    const band = await $.ui.mount({ plugin: 'codex-bridge', surface: 'terminal', ...BAND } as any)
    expect(await band.find({ type: 'Text', text: /Codex 5h 82%/ })).toBeDefined()
    await band.unmount()
  })

  test('0.1.1: a cool idle band leaves the engine band alone', async ($, on) => {
    const env = engine(on, { status: { ok: true, jobs: [] }, usage: { ok: true, summary: '5h 10%', windows: { primary: { used: 10 } } } })
    await $.session.start(START)
    await env.clock.advance(10)
    const band = await $.ui.mount({ plugin: 'codex-bridge', surface: 'terminal', ...BAND } as any)
    expect(await band.find({ type: 'Text', text: /engine band/ })).toBeDefined()
    await band.unmount()
  })

  test('0.1.1: a failed job report carries the suggested patch and the resume command', async ($, on) => {
    const claims = new Set<string>()
    const env = engine(on, {
      status: () => ({ ok: true, jobs: [{ id: 'j-qq', local: true, state: 'receipted', outcome: 'quota-limited', created: new Date().toISOString(), task: 't', reported: claims.has('j-qq') }] }),
      claim: (_: any, __: number, args: string[]) => (claims.add(args[0]!), { ok: true, claimed: true }),
      result: { ok: true, state: { outcome: 'quota-limited' }, receipt: { computed: { resume: 'codex resume th1', suggested_patch: { files: ['context/a.md'], lines: 4, path: '/j/suggested.patch' }, quota: { delta: { primary: 3 } }, model: 'gpt-6-luna', effort: 'high' } } },
    })
    await $.session.start(START)
    await env.clock.advance(5000)
    expect(env.prompts.length).toBe(1)
    expect(env.prompts[0]).toContain('most likely a usage limit')
    expect(env.prompts[0]).toContain('context/a.md')
    expect(env.prompts[0]).toContain('NOT applied')
    expect(env.prompts[0]).toContain('codex resume th1')
    const burn = env.calls.find(c => c.cmd === 'burn')!
    expect(burn.stdin.quota_delta).toEqual({ primary: 3 })
    expect(burn.stdin.model).toBe('gpt-6-luna')
  })

  test('/codex-bridge setup reports config, limits and each selftest probe', async ($, on) => {
    const env = engine(on, {
      status: { ok: true, jobs: [] },
      config: { ...CONFIG, path: '/h/.codex-bridge/config.json' },
      usage: { ok: true, summary: '5h 12% (resets 3pm)', windows: { primary: { used: 12 } } },
      selftest: { ok: true, passed: false, codex_version: 'codex-cli 0.161.0', platform: 'Darwin',
        results: { runs: { pass: true, rc: 0 }, network: { pass: false, rc: 0, tail: 'connected' } } },
    })
    await $.session.start(START)
    const r: any = await $.command.run({ command: 'codex-bridge', args: 'setup' } as any)
    expect(r.text).toContain('/h/.codex-bridge/config.json')
    expect(r.text).toContain('5h 12% (resets 3pm)')
    expect(r.text).toContain('FAILED - worker jobs stay locked')
    expect(r.text).toContain('pass runs')
    expect(r.text).toMatch(/FAIL network \(rc 0\): connected/)
    expect(env.calls.some(c => c.cmd === 'selftest')).toBe(true)
  })

  test('/codex-bridge setup stops on an invalid config without running the selftest', async ($, on) => {
    const env = engine(on, { status: { ok: true, jobs: [] }, config: { ok: false, error: 'config x is invalid', problems: ['tiers needs exactly R0, R1, R2'] } })
    await $.session.start(START)
    const r: any = await $.command.run({ command: 'codex-bridge', args: 'setup' } as any)
    expect(r.text).toContain('config: INVALID')
    expect(r.text).toContain('tiers needs exactly R0, R1, R2')
    expect(env.calls.some(c => c.cmd === 'selftest')).toBe(false)
  })

  test('/codex-bridge status, result, cancel and usage', async ($, on) => {
    const env = engine(on, {
      status: { ok: true, lock: null, jobs: [
        { id: 'j-live', local: true, state: 'running', tier: 'R1', task: 'build x', worktree_retained: true },
        { id: 'j-old', local: true, state: 'discarded', tier: 'R0', task: 'old', worktree_retained: false },
      ] },
      usage: { ok: true, summary: '5h 40%' },
      result: { ok: true, verdict: { verdict: 'accept' } },
      cancel: { ok: true, cancelled: true, note: 'Codex stopped' },
    })
    await $.session.start(START)
    const st: any = await $.command.run({ command: 'codex-bridge', args: 'status' } as any)
    expect(st.text).toContain('codex slot: free')
    expect(st.text).toContain('j-live · running · R1 · build x')
    expect(st.text).not.toContain('j-old')
    const res: any = await $.command.run({ command: 'codex-bridge', args: 'result j-live' } as any)
    expect(res.text).toContain('"accept"')
    expect(env.calls.find(c => c.cmd === 'result')!.args).toEqual(['j-live'])
    const can: any = await $.command.run({ command: 'codex-bridge', args: ' cancel  j-live ' } as any)
    expect(can.text).toBe('Cancelled: Codex stopped')
    const bare: any = await $.command.run({ command: 'codex-bridge', args: 'cancel' } as any)
    expect(bare.text).toContain('exactly one job id')
    const two: any = await $.command.run({ command: 'codex-bridge', args: 'cancel j-a j-b' } as any)
    expect(two.text).toContain('exactly one job id')
    expect(env.calls.filter(c => c.cmd === 'cancel').length).toBe(1)
    const upper: any = await $.command.run({ command: 'codex-bridge', args: 'STATUS' } as any)
    expect(upper.text).toContain('codex slot: free')
    const help: any = await $.command.run({ command: 'codex-bridge', args: '' } as any)
    expect(help.text).toContain('Usage: /codex-bridge')
  })

  test('/codex-bridge setup says the bridge cannot run, not that the config is invalid, when Python fails', async ($, on) => {
    const env = engine(on, { status: { ok: true, jobs: [] } }, { raw: { config: 'Traceback: boom' } })
    await $.session.start(START)
    const r: any = await $.command.run({ command: 'codex-bridge', args: 'setup' } as any)
    expect(r.text).toContain('bridge: cannot run')
    expect(r.text).not.toContain('INVALID')
    expect(env.calls.some(c => c.cmd === 'selftest')).toBe(false)
  })

  test('a refused command name does not stop the job watcher', async ($, on) => {
    const env = engine(on, { status: { ok: true, jobs: [] } }, { commandDeny: true })
    await $.session.start(START)
    expect(env.calls.some(c => c.cmd === 'sweep')).toBe(true)
    const before = env.calls.filter(c => c.cmd === 'status').length
    await env.clock.advance(61_000) // idle watcher polls once a minute
    expect(env.calls.filter(c => c.cmd === 'status').length).toBeGreaterThan(before)
  })
})
