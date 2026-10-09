# marathon

A bare-bones loop for coding agents that run for hours on one goal, with nobody watching.

It's the code version of the talk *Agents That Don't Lose the Plot* by Ravi Vats (XConf APAC 2026).
[The fixes from the talk](#the-fixes-from-the-talk) shows how each fix works here.
Plain Python 3.10+, standard library only, about 1,200 lines. It talks to any
OpenAI-compatible `/chat/completions` endpoint.

## Try it in 10 seconds (no API key)

```bash
python3 -m marathon demo
```

No real model runs in the demo, which is why it needs no API key. A scripted fake model replays a
fixed list of tool calls in a throwaway repo. Everything the harness does around those calls is real:
the `[step N]` commits, the test runs, the goal re-reads, undoing edits to the tests, dropping the
stuck attempt and starting a fresh worktree. For real reasoning you need a model, see
[Run it for real](#run-it-for-real).

```
No tests yet. A separate test-writer agent is writing them on marathon/<run>/tests.
Attempt 1 starts on marathon/<run>/attempt-1. Right now: 0/1 tests pass.
[attempt 1 | step 1] 0/4 tests pass (no progress 1/3)
[attempt 1 | step 2] 0/4 tests pass (no progress 2/3)
[attempt 1 | step 2] step % 2 == 0: the goal goes back in front of the agent
[attempt 1 | step 3] 0/4 tests pass (no progress 3/3) [undid edits to read-only files: tests/test_slug.py]
[attempt 1] No progress in 3 checkpoints. Dropping this plan. The next attempt starts in a fresh worktree.
Attempt 2 starts on marathon/<run>/attempt-2. Right now: 0/1 tests pass.
[attempt 2 | step 1] 2/4 tests pass (progress)
[attempt 2 | step 2] 4/4 tests pass (progress)
[attempt 2 | step 3] tests pass, judge says PASS
Done at step 3: 4/4 tests pass, judge PASS.
```

## Run it for real

```bash
# 1. a project folder with a goal
python3 -m marathon init ~/work/slugify
#    edit ~/work/slugify/goal.md

# 2. pick a model (any OpenAI-compatible endpoint)
export MARATHON_MODEL=<model name>
export MARATHON_API_KEY=<key>
export MARATHON_BASE_URL=https://api.openai.com/v1        # the default
# Anthropic:  https://api.anthropic.com/v1/
# Ollama:     http://localhost:11434/v1   (any key works, pick a model with tool calling)
# OpenRouter: https://openrouter.ai/api/v1

# 3. start it and go to bed
python3 -m marathon run --repo ~/work/slugify --hours 8
```

You can also point `run` at an existing repo. Put a `goal.md` in it. If it already has tests in
`tests/`, those are used and no tests are written.

When it's done, the work is on a branch: `git merge marathon/<run>/attempt-<n>`.

## How a run works

1. **Tests first, by someone else.** If there are no tests, a separate test-writer agent, with its
   own fresh context, reads `goal.md` and writes them. It can only write inside `tests/`. That
   commit is the base for every attempt.
2. **One attempt = one worktree and one branch.** The worker gets a short system prompt, the goal,
   and six small tools: `read_file`, `write_file`, `edit_file`, `list_files`, `search`, `run`.
   Plus `ask_subagent`, `checkpoint` and `finish`.
3. **Checkpoints are steps.** When the worker calls `checkpoint`, the harness undoes any edits to
   `tests/` and `goal.md`, commits everything as `"<summary> [step N]"`, and runs the tests itself.
   If the worker forgets, the harness makes a checkpoint every 15 turns.
4. **The goal comes back every N steps.** When `step % N == 0`, `goal.md` is read from disk again
   and put at the end of the context, with the checklist in `PROGRESS.md`. There is a backup
   schedule every 20 model turns too.
5. **Plans are temporary.** Progress means more tests passing (or a higher score, see below). After
   `--patience` checkpoints in a row with no progress (25 by default), the attempt is dropped:
   its folder goes, its branch stays, a short note is kept, and the next attempt starts in a
   fresh worktree with a fresh context.
6. **"Done" has to be earned.** `finish` only counts when every test passes and a judge, which sees
   only the goal and the final diff, says PASS. Otherwise the reason goes back to the worker.

## The fixes from the talk

The talk gives ten fixes for long runs, each one tied to a problem from its case studies, plus one
"going further" idea. Slide 33 of the deck points to this repo and lists five of them. Those five come
first below, with the code that does the work and the unit tests that check it. The other five are in
the table after them. You can watch all five happen in `python3 -m marathon demo`.

### Fix 1. Re-read the goal on a schedule

**Problem in the talk: goal drift.** Hours into a run, the goal sits far back in the context and the
agent follows whatever it was doing last.

- Every checkpoint is a git commit whose message ends in `[step N]`. The step number is read back from
  the commit log, so the commits are the record of how far the run got.
- When `step % N == 0`, the harness reads `goal.md` from disk again and puts it at the end of the
  context, with the checklist from `PROGRESS.md`, the last and best test results, and the number of
  checkpoints without progress. N is `--reread-every`: the default is 5, and the talk's example uses 25.
- A backup re-read happens every 20 model turns (`--reread-every-turns`), in case the agent rarely
  checkpoints.

Code: `gitops.is_reread_step`, `gitops.last_step`, `Harness.reread`, `prompts.reread_message`.
Tests: `test_reread_when_step_mod_n_is_zero`, `test_reread_puts_goal_and_checklist_at_the_end`.

### Fix 3. Tell the agent it can change the plan

**Problem in the talk: stuck on a bad plan.** The agent keeps patching its first plan long after it
stopped working.

- The system prompt says the plan and the steps are temporary and can be dropped at any time. If the
  same approach fails 3 times in a row, the agent writes down why in `PROGRESS.md` and tries another.
- The harness backs this up. Progress means more tests passing, or the same tests passing with a
  better score when you set `--score-cmd`. After `--patience` checkpoints in a row with no progress
  (default 25), it drops the attempt. The worktree is removed, the branch is kept so you can look at
  it later, and a short note goes into `.marathon/attempts.md`.
- The next attempt starts on a new branch, in a fresh worktree, with a fresh context. It gets the
  notes from the last three dropped attempts so it doesn't repeat them. After `--max-attempts`
  (default 5) the run stops.

Code: `prompts.WORKER`, `Harness.record`, `Harness.drop`.
Tests: `test_demo_script_end_to_end`, `test_worktree_removed_but_branch_kept`,
`test_gives_up_after_max_attempts`.

### Fix 6. Hand slices of work to sub-agents

**Problem in the talk: prompt too full.** One agent doing every job carries every detail in one
context.

Two helpers start with an empty context and do one job each:

- **The test writer.** If the repo has no tests, it reads `goal.md` and writes them before any code
  exists. It can write only inside `tests/`, and only `tests/` goes into its commit. That commit
  (`[step 0]`) is the starting point for every attempt. If the repo already has tests in `tests/`,
  those are used and no tests are written.
- **`ask_subagent`.** The worker can hand off one focused question, like "where are dates parsed?".
  The helper can only read files. It is asked to answer in 10 lines or less, and the answer is capped
  at 2,000 characters.

Code: `Harness.ensure_tests`, `agents.write_tests`, `agents.ask`.
Tests: `test_test_writer_commits_only_tests`, `test_writable_only`.

### Fix 9. Run checks the agent can't skip

**Problem in the talk: "done" too early.** If the agent runs its own checks, it can skip them, weaken
them, or report a pass it didn't get.

- The harness runs the tests itself, with a fixed command (`--test-cmd`) and a timeout, at every
  checkpoint and on `finish`. It reads the pass and fail counts from unittest or pytest output.
- The worker's file tools refuse to write to `tests/` and `goal.md`. Since `run` can still execute
  any shell command, the harness also puts both back exactly as they are in the base commit before
  every commit. Edits, new files and deletions there are undone, and the agent is told.
- If the agent goes 15 model turns without a checkpoint (`--force-checkpoint-every`), the harness
  makes one and runs the tests anyway.
- The test writer is told to write deterministic tests: no network, no clocks, no randomness
  without a fixed seed.

Code: `checks.run_tests`, `gitops.restore`, `Harness.commit`, `Workspace.check_write`.
Tests: `test_restore_undoes_edits_adds_and_deletes`, `test_protected_paths_are_read_only`,
`test_harness_checkpoints_when_the_agent_forgets`.

### Fix 10. A judge that sees only the ask and the diff

**Problem in the talk: "done" too early.** A reviewer that has read the agent's reasoning tends to
agree with it.

- `finish` only counts when every test passes. Until then the agent gets the failing output back and
  carries on.
- Then a judge gets two things: the goal and the final diff (without `PROGRESS.md` and the tests).
  It sees no history, no reasoning and no earlier attempts. Its first line must be PASS or FAIL.
- On FAIL, the judge's reasons go back to the worker, and the attempt goes on.

Code: `Harness.finish`, `agents.judge`.
Tests: `test_finish_is_rejected_while_tests_fail`, `test_judge_sees_only_goal_and_diff`.

### The other fixes

| Fix | Problem in the talk | In the code | Code |
|---|---|---|---|
| 2. Keep a checklist, and check against it | Mistakes pile up | The agent keeps `PROGRESS.md`, and every re-read shows it next to the goal and the latest test results. The talk re-runs the tests every 10 steps. Here they run at every checkpoint. | `prompts.reread_message` |
| 4. Use a lighter harness | Light harness, composable tools | A system prompt of about 20 lines and six general tools: `read_file`, `write_file`, `edit_file`, `list_files`, `search`, `run`. Three more (`checkpoint`, `finish`, `ask_subagent`) are how the agent talks to the harness. | `prompts.py`, `tools.py` |
| 5. Point the agent at the data | Prompt too full | Tool output over 6,000 characters is cut to its start and end, the full text is saved to a file, and the agent gets the path. Failing test output is saved the same way. After 80 messages the oldest are dropped, keeping the system prompt and the task. | `Workspace.clip`, `Harness.test_report`, `Harness.trim` |
| 7. Hand sub-agents a pointer, too | Prompt too full | `ask_subagent` takes file paths, and the helper reads the files itself. | `agents.ask` |
| 8. Give each agent its own dev environment | "Done" too early | Each attempt gets its own git worktree and branch. The talk also gives each run its own container. Here you add that yourself, see [Be careful with `run`](#be-careful-with-run). | `gitops.add_worktree` |
| + Going further: train the model on the pattern | Niche tasks | Not in this repo. | |

## Settings

| Flag | Default | Meaning |
|---|---|---|
| `--reread-every` | 5 | re-read the goal when `step % N == 0` |
| `--reread-every-turns` | 20 | backup re-read, counted in model turns |
| `--patience` | 25 | checkpoints in a row with no progress before a fresh start |
| `--max-attempts` | 5 | fresh starts before giving up |
| `--hours` / `--max-turns` | 8 / 600 | budget for the whole run |
| `--force-checkpoint-every` | 15 | model turns without a checkpoint before the harness makes one |
| `--test-cmd` | `python3 -m unittest discover -s tests -v` | unittest and pytest output are both understood |
| `--score-cmd` | none | for objective goals: a command that prints a number, higher is better |
| `--min-delta` | 0 | smallest score gain that counts as progress |
| `--max-history` | 80 | messages kept before the oldest are dropped |

Objective goals, like "make this function faster", can use `--score-cmd`:
progress is more tests passing, or the same tests passing with a better score.

## What it leaves behind

```
your-repo/
  .marathon/             (git-ignored)
    log.jsonl             every checkpoint, re-read, drop and judge verdict
    state.json            where the run is now
    attempts.md           notes from dropped attempts
    worktrees/attempt-N/  the attempt in progress
  branches:
    marathon/<run>/tests       the test suite
    marathon/<run>/attempt-N   one per attempt, every commit ends in [step N]
```

## Code

```
marathon/
  loop.py      the main loop: attempts, checkpoints, re-reads, drops, finish
  agents.py    test writer, ask_subagent helper, judge
  tools.py     the six tools, kept inside one folder
  gitops.py    worktrees, [step N] commits, restoring protected files
  checks.py    runs the tests and reads the counts
  prompts.py   the short prompts
  llm.py       OpenAI-compatible client with retries, and a scripted fake
  demo.py      the offline demo
tests/         python3 -m unittest -v
```

## Be careful with `run`

The `run` tool executes shell commands in the attempt's folder. The harness keeps the tests and
the goal safe, but it can't stop a command from touching other files on your machine. For real
marathon runs, start it inside a container or a VM.

## Not in here yet

- Resuming a run after the process stops (the state file and branches are there to build on).
- Token-based context limits (it counts messages for now).
- A web search tool (add it to `tools.py` if your goal needs one).

## License

Copyright (c) 2026 Ravi Vats.

marathon-agent is licensed under the
[Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International License](https://creativecommons.org/licenses/by-nc-sa/4.0/)
(CC BY-NC-SA 4.0). The full text is in [LICENSE](LICENSE).

You may copy, change and share it for non-commercial purposes, as long as you:

- **give credit:** name Ravi Vats, link to this repo and to the license, and say if you changed it;
- **share alike:** release anything you build from it under CC BY-NC-SA 4.0 too.

For commercial use, contact Ravi Vats first.

Suggested credit:

> marathon-agent by Ravi Vats (https://github.com/ravivats/marathon-agent), licensed under CC BY-NC-SA 4.0.
