# AI Agent Instructions (Context Keeper)

This project uses Context Keeper (ck) as its memory layer.

- Active tasks live in `.ck/PLAN.md` (`[ ]` open, `[>]` focused,
  `[~]` paused). Read it to see the current work.
- Past context lives in `.ck/HISTORY.md` (`[x]` completed entries
  with timestamps). Read it before asking what was already done.
- NEVER delete the `<!-- id: ... -->` comment at the end of a task
  line — it is the task's stable reference (survives renumbering).
- Do not edit `.ck/PLAN.md` by hand when a command exists:
  use `ck add <text>`, `ck done <ID>`, `ck focus <ID>`,
  `ck note <ID> <text>`.
- Global spaces (`~/.config/ck/spaces/<name>.md`, including the daily
  `today` journal) are also managed via ck: `ck <space> <command>`,
  e.g. `ck todo add <text>`, `ck today done 1`.
- Completed work is recorded automatically; do not write `[x]`
  lines into `.ck/PLAN.md`.
