// Runs .claude/workflows/build-phase.js with stubbed agents against a real repo (no model calls).
// The runner stub really executes harness/referee.py, so the whole control flow — plan, prepare,
// freeze, retries, triage, review, integrate, close — runs for real.
// usage: node sim_workflow.mjs <workflow.js> <repo> <scenario.json>
import fs from 'node:fs'
import path from 'node:path'
import { execSync } from 'node:child_process'

const [, , wfPath, repo, scenarioPath] = process.argv
const sc = JSON.parse(fs.readFileSync(scenarioPath, 'utf8'))
const calls = []
const attempts = {}
const flaky = new Set()
const write = (p, s) => { fs.mkdirSync(path.dirname(p), { recursive: true }); fs.writeFileSync(p, s) }

async function agent(prompt, opts) {
  const role = opts.agentType
  const id = (prompt.match(/task (\d{4})/i) || [])[1]
  calls.push({ role, id, model: opts.model || null, label: opts.label })
  const wt = id ? path.join(repo, '.worktrees', `task-${id}`) : null
  if (role === 'runner') {
    const cmd = prompt.match(/python3 harness\/referee\.py ([^\n]+)/)[1]
    // a free runner that garbles one step: the workflow must relay that step again on the rescue tier
    if (sc.runner_garbles && cmd.startsWith(sc.runner_garbles) && !opts.model && !flaky.has(cmd)) {
      flaky.add(cmd)
      return { stdout: 'Ran it. Everything looks fine!' }
    }
    return { stdout: execSync(`${sc.python} harness/referee.py ${cmd}`, { cwd: repo, env: { ...process.env, DEVTEAM_INLINE: '1' } }).toString() }
  }
  if (role === 'tech-lead') {
    const key = prompt.match(/key: "([^"]+)"/)[1]
    const out = { key, blocking: sc.precheck_blocking || [], advisories: [] }
    write(path.join(repo, '.loop/precheck', `${sc.phase}.json`), JSON.stringify(out))
    return out
  }
  if (role === 'qa-designer') {
    for (const [p, body] of Object.entries(sc.tasks[id].tests)) write(path.join(wt, p), body)
    return { tests: Object.keys(sc.tasks[id].tests), done_when_map: [] }
  }
  if (role === 'implementer') {
    attempts[id] = (attempts[id] || 0) + 1
    const versions = sc.tasks[id].impl
    const files = versions[Math.min(attempts[id], versions.length) - 1]
    for (const [p, body] of Object.entries(files)) write(path.join(wt, p), body)
    return { summary: 'ok', files_changed: Object.keys(files), tests_run: 'stub' }
  }
  if (role === 'triage' && sc.tasks[id].triage_fails_once && !opts.model) return null // schema retries exhausted
  if (role === 'triage') return { cause: sc.tasks[id].triage || 'impl', confidence: 0.9, evidence: 'stub' }
  if (role === 'reviewer' && sc.tasks[id].reviewer_fails) return null
  if (role === 'reviewer') {
    calls[calls.length - 1].cross = /second reviewer/.test(prompt)
    const r = Number((opts.label.match(/r(\d+)/) || [])[1] || 1)
    const blocking = (sc.tasks[id].review_blocking_rounds || 0) >= r ? [{ file: 'src/x.py', line: 1, scenario: 'stub defect' }] : []
    const v = { verdict: blocking.length ? 'request_changes' : 'approve', blocking, minor: [] }
    write(path.join(repo, '.loop/runs', id, `review-r${r}.json`), JSON.stringify(v))
    return v
  }
  if (role === 'cross-reviewer') {
    const r = Number((opts.label.match(/r(\d+)/) || [])[1] || 1)
    const out = { candidates: (sc.tasks[id].cross_candidates || []) }
    write(path.join(repo, '.loop/runs', id, `cross-r${r}.json`), JSON.stringify(out))
    return out
  }
  if (role === 'qa-functional') return { scenarios_run: 3, defects: [] }
  if (role === 'security-prober' || role === 'ui-reviewer') return { verdict: 'approve', blocking: [] }
  if (role === 'phase-reviewer') {
    const out = { findings: [], follow_ups: [{ title: 'stub follow-up', why: 'sim' }], learnings: ['sim learning'] }
    write(path.join(repo, '.loop/runs', `phase-${sc.phase}`, 'review.json'), JSON.stringify(out))
    return out
  }
  throw new Error(`unexpected agent type ${role}`)
}
const parallel = async thunks => Promise.all(thunks.map(t => t().catch(() => null)))
let spent = 0
const budget = { total: null, spent: () => (spent += 1000), remaining: () => Infinity }
const logs = []
const src = fs.readFileSync(wfPath, 'utf8').replace('export const meta', 'const meta')
const run = new Function('agent', 'parallel', 'pipeline', 'phase', 'log', 'args', 'budget', 'workflow',
  `return (async () => {\n${src}\n})()`)
const result = await run(agent, parallel, null, () => {}, m => logs.push(m), sc.args, budget, null)
console.log(JSON.stringify({ result, calls, logs }))
