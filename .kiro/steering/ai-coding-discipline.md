---
inclusion: always
name: ai-coding-discipline
description: "Language-agnostic diff-discipline and scope rules for AI-assisted development on shared repos: keep changes minimal and in-scope, prevent drive-by revision, coordinate parallel work, and escalate rule conflicts instead of silently violating them. Includes a reviewer checklist."
---

<!--
Vendored from rapid7/ai-vault -- do not edit here.
  asset:   rules/ai-coding-discipline
  version: 0.14.2
  source:  https://github.com/rapid7/ai-vault (commit 56b4497, 2026-08-14)

To update, re-copy from the vault and bump the version above. The only local
change is the added `inclusion: always` front-matter key, which the vault omits
because it ships cross-harness (Kiro steering defaults to always-loaded, but
stating it explicitly keeps the intent obvious).
-->

# AI Coding Discipline

Behavioral rules for AI-assisted development across teams that share repositories. Everything here is binding; there is no non-binding filler to trim.

This rule is the language-agnostic core: scope and diff discipline (`SCOPE`), the scope half of the reviewer checklist (`CHK-SCOPE`), and escalation (`ESC`). Go-specific architectural defaults live in the companion **go-ai-standards** rule; install it alongside this one for Go repos. Every section and rule carries a stable prefixed ID (`SCOPE-6`, `GO-ARCH-2.5`, `CHK-GOARCH`) so cross-references resolve regardless of which rule you are reading, and IDs are append-only — never renumbered or reused. The scope rules are `SCOPE-1` through `SCOPE-14` and all live in this rule; the companion rule refers back to them by ID.

## How AI Should Apply This Rule

This file is a set of instructions for you, the AI assistant, during code generation and review. Apply every section, weighted by its strength:

- **`SCOPE`** - hard rules (`SCOPE-1`–`SCOPE-14`). Follow them.
- **`CHK-SCOPE`** - reviewer checklist (scope items). Self-check against it before producing output, in addition to the human reviewer applying it.
- **`ESC`** - escalation discipline. Hard rule on surfacing rule conflicts as questions. No silent violations.

(`GO-ARCH` and `GO-PAT-3` - Go architectural defaults and recommended patterns - live in the **go-ai-standards** rule.)

---

## SCOPE - Scope and Diff Discipline

These are **hard rules**, `SCOPE-1` through `SCOPE-14` in the list below. They constrain what an AI assistant may touch in a single change. They are language-agnostic.

**In one line:** a good PR is boring. It touches only what the task names, adds no abstraction without a second caller, changes no dependencies as a side effect, and is explainable in a single sentence. That sentence is a floor, not the whole test: being *related* to it is not enough - each changed line must be *necessary* for the requested behavior, its tests, or an unavoidable build/format consequence. Related-but-optional cleanup, rewrites, and renames are out of scope even when they fit the sentence. If your diff fails that test, cut it back before committing.

1. **Stay in scope.** Edit only the lines required by the assigned task, where "the task" is the developer's actual request (the ticket's acceptance criteria or the change they asked for) - not what looks improvable nearby, and not a title you chose or widened after the fact to cover changes you already made. If a fix is in `parser.go:42`, do not touch `parser.go:90` "while you're here." If the request is too broad to bound the diff to one explainable purpose, ask the developer to narrow it before editing - a vague title is not license for a wide diff, and you may not self-authorize by editing the title or description. Unrelated improvements are out of scope: surface them to the developer, do not apply them. Separation is not authorization - moving unrequested work into a `TODO` comment, a follow-up ticket, or a separate PR does not make it authorized; it still needs an explicit request, so keep unrequested observations in your handoff message, not committed to the repo. (The one sanctioned exception is ESC's unattended fallback, where leaving a `TODO` marking a surfaced conflict is the policy-authorized way to record it with no human to ask.)
2. **Preserve unrelated code verbatim.** Do not reorder imports, rename locals, tighten conditionals, or "improve" naming in code outside the task scope. Even when a nearby line looks wrong, leave it.
3. **No drive-by formatting.** Do not run a formatter or linter over files you didn't already need to touch. Running the standard formatter (`gofmt` or equivalent) over the lines your task touches is expected and not a violation - what the rule forbids is sweeping files you had no other task reason to open. Keep formatter output off lines outside your hunk: if the formatter would also churn pre-existing drift elsewhere in the file (committed, or another developer's uncommitted lines per SCOPE-10), do not fold those hunks into your commit. Stop and surface the drift; do not silently reformat it as a separate change of your own, since that is still unrequested work (SCOPE-1). Where SCOPE-2 (preserve unrelated lines) and this rule appear to collide, SCOPE-2 wins outside your hunk. CI will catch genuine issues - do not pre-empt it with a sweep.
4. **Refactors are separate PRs.** If refactoring is genuinely required to land the task, stop, surface the proposal to the developer, and wait for approval before landing the refactor as its own commit/PR before the feature work. Do not bundle. This is also how large work is decomposed: land the enabling refactor first as its own PR, then the feature on top - not one PR that does both.
5. **Respect existing patterns.** If a module follows a convention (error wrapping style, struct layout, table-driven tests), follow it. Do not introduce a new convention because it is "cleaner." Three similar lines beat a premature abstraction.
6. **Ask before structural changes.** Splitting files, moving functions across packages, changing public function signatures, renaming exported symbols - require explicit human approval first. Do not infer permission from the task description.
7. **One PR, one purpose.** A bugfix PR contains only the bugfix. A feature PR contains only the feature. Mixed-purpose PRs are rejected at review.
8. **Cross-team boundaries.** When the task touches code owned by another team (per CODEOWNERS or module documentation), surface it. Request review from that owner before merging. Do not "helpfully" fix code outside your team's ownership.
9. **No speculative abstraction.** Do not extract interfaces, helpers, generics, or "future-flexibility" layers without a concrete current call site. Add abstraction when a second *independent, task-required* production caller appears, not before - a caller you introduce as part of the abstraction, or a test that merely exercises it, does not count as the second caller.
10. **Diff hygiene before commit.** Check `git status` before you start so you can tell your changes from pre-existing ones. Before committing *or handing the work back* (even with nothing staged), re-read everything you changed: staged and unstaged hunks (`git diff` and `git diff --staged`) **and** any new untracked files (`git status --short` - `git diff` does not show these, so a stray new file will otherwise escape review). If any hunk or new file is unrelated to the task, revert or remove it - but only work you introduced during this task. Never discard pre-existing uncommitted changes you did not author; surface those instead, and if such a change overlaps your edit in the same hunk, stop and ask. The final diff should be 100% explainable in one sentence.
11. **Trust the existing tests.** Do not rewrite passing tests for style. Add new tests for new behavior. If an existing test is genuinely wrong, that is its own PR with its own justification - unless the test encodes behavior you are intentionally changing, in which case the coupled test update belongs in the same PR as that change (splitting them leaves one side red and unmergeable). Reserve the separate-PR rule for test corrections that stand on their own and keep CI green.
12. **No silent dependency changes.** Do not add, remove, or upgrade Go modules / npm packages / Python deps as a side effect of another task. Dependency changes are their own PR.
13. **Account for concurrent work.** Do not assume `main` is static. A green CI run on a stale base does not guarantee the merged result is green - other AI-assisted PRs land in parallel. Before finalizing, integrate current `origin/main` and re-run the affected gates; do not rely on an earlier green run against an older base. Rebase only your own unshared branch - if others may have based work on it, merge rather than rebase so you do not rewrite shared history. A client-side re-check narrows but does not close the merge-order race (the real backstop is a serialized merge queue); surface any semantic overlap with in-flight changes to the same files or shared contracts rather than assuming your branch merges cleanly.
14. **Isolate parallel work.** Do a task on its own branch, and do not edit in a working tree or index that another active AI session or developer is using. A shared checkout lets two sessions overwrite each other's files, reformat each other's edits, or stage each other's hunks - which no after-the-fact `git status` check (SCOPE-10) can untangle. If you cannot get an exclusive working tree, stop and surface that rather than editing a shared one.

---

## CHK-SCOPE - Reviewer Checklist (Scope)

Before approving any AI-generated PR, the human reviewer scans the scope items below. The architecture and patterns items live in the **go-ai-standards** rule; apply those too when the PR touches Go.

### Scope (SCOPE)

- Does any changed line fail the necessity test - merely related cleanup, a rewrite, or a rename that isn't required for the requested behavior, its tests, or an unavoidable build/format consequence? (out of scope even if it fits the PR's one-sentence purpose)
- Did the diff bring pre-existing code into architectural compliance (context, metrics, fixtures, etc.; see go-ai-standards) beyond what the task introduces or necessarily changes?
- Are there hunks unrelated to the PR title? (revert them)
- Did formatting / whitespace change in files the PR didn't need to touch?
- Did imports get reordered in untouched files?
- Are there new abstractions (interfaces, helpers) without a second caller?
- Did public signatures change (or other structural changes per SCOPE-6) without documented explicit human approval - not merely a description in the PR, which an AI can author?
- Were any dependencies added, removed, or upgraded silently?
- Does the diff modify another team's owned files (per CODEOWNERS) without that owner's review?
- Was the branch finalized on a stale base, or does it overlap in-flight PRs touching the same files or shared contracts without that being surfaced? (SCOPE-13)
- Does the PR show signs of a shared/dirty working tree - unrelated hunks from another session, or edits the author cannot attribute to this task? (SCOPE-14)

If any answer under Scope is "yes", request changes before approving.

---

## ESC - Escalation

When a rule conflicts with the task - for example, "the bug really is the structure, you cannot fix it without splitting the file" - the AI must surface the conflict as a question, not silently violate the rule. The developer decides whether to:

- accept the larger change and update the PR scope, or
- land a minimal workaround now and open a separate refactor PR.

Accepting the larger change authorizes the work; it does not waive the separation rules. If what was approved is a refactor (SCOPE-4) or another change a hard rule requires to land on its own, it still lands as its own PR first, with the task on top - approval widens scope, it does not license bundling. The developer may override that explicitly, but silence does not.

When the AI is running non-interactively (CI, batch, or headless) and no developer is available to answer, the default is the minimal workaround: make the smallest in-scope change, and record the surfaced conflict where it will be seen - the run's handoff/output, and a `TODO` at the conflict site only if the change already touches that spot. This narrow, policy-sanctioned `TODO` is the exception to SCOPE-1's "separation is not authorization"; it is not license to leave `TODO`s for unrelated observations elsewhere. Never take the larger structural change unattended. Fail closed instead of working around when the conflict touches security, data loss, or another team's ownership - stop and leave that decision for a human rather than landing an unattended patch.

**Silent rule violations are not allowed even when the violation would be technically correct.**
