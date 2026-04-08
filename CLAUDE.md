# Polymarket MVP — Claude Code Instructions

## Auto-Resume Protocol

When starting a new session, **immediately** check for the file `.claude/resume_task.md`.

- If it exists and contains uncompleted work (items not marked `[x]`), **announce to the user that you found unfinished work and will resume it**, then continue executing from the first uncompleted step. Do NOT ask for permission — just inform and proceed.
- If all items are completed or the file is empty/missing, proceed normally with whatever the user asks.

### Checkpoint rules (CRITICAL — follow on every multi-step task)

Whenever you are working on a task with more than 2 steps:

1. **Before starting work**, write a checkpoint to `.claude/resume_task.md` with this format:

```markdown
# Resume Task

## Original Request
> (paste the user's original request or a faithful summary)

## Context
- Branch: (current git branch)
- Key files: (list the main files involved)
- Any important decisions or constraints

## Steps
- [ ] Step 1 description
- [ ] Step 2 description
- [ ] Step 3 description
...
```

2. **After completing each step**, update the file: change `- [ ]` to `- [x]` for that step and add a one-line note of what was done if useful.

3. **When ALL steps are done**, replace the file contents with just:
```
(empty — all work completed)
```

4. **If the plan changes** (new steps discovered, steps removed, reordering), update the file immediately to reflect the current plan.

### Why this matters
The usage limit can cut a session at any time without warning. The checkpoint file is the only way to ensure continuity. Keep it updated aggressively — better to write too often than to lose progress context.
