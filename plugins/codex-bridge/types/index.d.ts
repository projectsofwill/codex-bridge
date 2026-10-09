export type JobRow = { id: string; state: string; outcome: string | null; created: string; task: string }
export type AskRow = { mode: string; since: number }
export type LimitsRow = { summary: string; max: number; at: number }

declare module 'claude-code' {
  interface PluginState {
    'codex-bridge': { jobs: JobRow[]; ask: AskRow | null; limits: LimitsRow | null }
  }
}
