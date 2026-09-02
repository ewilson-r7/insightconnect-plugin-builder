## What this changes

<!-- One sentence. If you cannot write one sentence that covers the whole diff,
     the diff is doing more than one thing -- split it (SCOPE-7). -->

## Why

<!-- The diff shows what changed; explain the motivation. -->

## How it was verified

<!-- Name what you actually ran, not what you assume passes. "CI is green" is
     enough for the standard checks; call out anything you verified by hand,
     and anything you could NOT verify. -->

- [ ] `pytest`
- [ ] `flake8 icplugin_builder tests`
- [ ] `black --check icplugin_builder tests`
- [ ] `cd frontend && npm run typecheck && npm run lint && npm test && npm run build`
- [ ] Verified by hand (describe):

## Scope check

`.kiro/steering/ai-coding-discipline.md` is binding in this repo. Its `CHK-SCOPE`
reviewer checklist is reproduced here so it applies where review happens. Confirm
each, or explain the exception.

- [ ] Every changed line is **necessary** for the stated purpose -- not merely
      related cleanup, a rewrite, or a rename (SCOPE-1)
- [ ] No drive-by formatting or import reordering in files the task didn't need
      to touch (SCOPE-2, SCOPE-3)
- [ ] No refactor bundled with a feature or fix (SCOPE-4)
- [ ] No new abstraction without a second, task-required production caller (SCOPE-9)
- [ ] No public signature or other structural change without explicit human
      approval recorded outside this description (SCOPE-6)
- [ ] No dependency added, removed or upgraded as a side effect (SCOPE-12)
- [ ] Existing passing tests were not rewritten for style; coupled test updates
      are here only where this PR intentionally changes that behavior (SCOPE-11)
- [ ] Branch was finalized on current `origin/main`, and any overlap with
      in-flight work touching the same files is called out (SCOPE-13)
- [ ] No unrelated hunks from a shared or dirty working tree (SCOPE-10, SCOPE-14)

## Definition of done

For changes to the generated-plugin quality bar, `.kiro/steering/project-conventions.md`
defines what "done" means. If this PR moves that bar, say which requirement in
`.kiro/specs/` it changes and why.

## Outstanding

<!-- Anything deliberately left for a follow-up, with a reason. An empty list is a
     claim worth being able to check. -->
