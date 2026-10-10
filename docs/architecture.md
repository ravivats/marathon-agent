# How marathon fits together

Three diagrams: the parts and how they talk, what happens during one run, and what happens at a
single checkpoint. GitHub draws them from the Mermaid source below, and the same diagrams are
saved as images next to this file for the README. Every box names the file that does the work,
so you can jump from a diagram to the code. The numbers (every N steps) come from `.env`; see
`.env.example`.

Back to the [README](../README.md).

## The parts

```mermaid
flowchart TB
    you(["You"]) -->|"answers"| startq["start.py<br/>asks: a platform you have a key for,<br/>a model, the goal"]
    env[(".env<br/>API keys, every N")] --> config["config.py<br/>settings"]
    config --> cli["__main__.py<br/>command line"]
    startq -->|"model, goal.md"| cli
    cli --> harness["loop.py<br/>Harness"]

    harness --> git["gitops.py<br/>worktrees, [step N] commits,<br/>restore protected files"]
    harness --> state[(".marathon/<br/>log.jsonl, state.json, attempts.md")]
    harness -->|"runs"| roles

    subgraph roles["Agents (agents.py, prompts.py)"]
        direction LR
        worker["Worker<br/>long task, long context"]
        tw["Test writer<br/>fresh context, writes tests/ only"]
        helper["ask_subagent helper<br/>fresh context, 5-line answer + notes file"]
        judge["Judge<br/>sees only the goal and the diff"]
    end

    roles -->|"model calls"| router["llm.py<br/>Router: one model per role"]
    router --> anthropic["AnthropicLLM<br/>Messages API, prompt caching"]
    router --> responses["ResponsesLLM<br/>Responses API, reasoning passed back"]
    router --> chat["ChatLLM<br/>chat/completions"]
    anthropic --> claude[("Anthropic API")]
    responses --> openai[("OpenAI API")]
    chat --> others[("OpenRouter, Ollama,<br/>any compatible server")]

    roles -->|"tool calls"| tools["tools.py<br/>Workspace: read, write, edit,<br/>list, search, run"]
    harness -->|"run the tests"| checks["checks.py<br/>pass and fail counts"]
    tools -->|"run"| sandbox["sandbox.py"]
    checks --> sandbox
    sandbox --> local["LocalShell<br/>this machine, keys removed"]
    sandbox --> containers["Containers: docker, podman, nerdctl<br/>one per attempt + a fresh one per test run<br/>no network, no keys"]
```

| File | Job | Fixes |
|---|---|---|
| `loop.py` | The harness: attempts, checkpoints, re-reads, the switch nudge, drops, finish | 1, 2, 3, 9, 10 |
| `agents.py` | The three helpers with a fresh context: test writer, `ask_subagent`, judge | 6, 7, 10 |
| `prompts.py` | The short prompts and the re-read message | 1, 2, 3, 4 |
| `tools.py` | Six general tools, kept inside one folder; long output goes to a file | 4, 5 |
| `sandbox.py` | Where commands run: this machine, or containers | 8, 9 |
| `checks.py` | Runs the tests (and an optional score command) and reads the counts | 9 |
| `gitops.py` | Worktrees and branches, `[step N]` commits, putting protected files back | 1, 8, 9 |
| `llm.py` | Model clients (Anthropic, OpenAI Responses API, any OpenAI-compatible server) and the per-role router | |
| `start.py` | The questions at start: a platform you have a key for, a model, the goal | |
| `config.py` | Every setting, its default, and reading `.env` (flag > environment > `.env` > default) | |
| `demo.py` | The demo: a prepared run, replayed in a throwaway repo | |

## One run

```mermaid
flowchart TD
    start(["marathon run"]) --> ask["Ask: a platform with a key in .env,<br/>a model, the goal in your words<br/>save goal.md after a yes"]
    ask --> prep["Commit goal.md<br/>check the container engine"]
    prep --> has{"Tests in tests/ already?"}
    has -- "no" --> tw["Test writer writes them<br/>commit: tests [step 0]"]
    has -- "yes" --> base["Base commit<br/>every attempt starts here"]
    tw --> base

    base --> attempt["New attempt<br/>own worktree, branch and container<br/>fresh context, notes from dropped attempts"]
    attempt --> turn["Worker turn<br/>one model call"]
    turn --> what{"What did it call?"}

    what -- "read, write, edit,<br/>list, search, run" --> tool["Run the tool<br/>in the worktree or the container"]
    tool --> turn
    what -- "ask_subagent" --> help["Helper with a fresh context<br/>5 lines back, the rest in a notes file"]
    help --> turn

    what -- "checkpoint" --> cp["Checkpoint<br/>see the next diagram"]
    cp --> stuck{"No progress for<br/>MARATHON_PATIENCE test runs?"}
    stuck -- "yes" --> drop["Drop the attempt<br/>remove the worktree, keep the branch<br/>save a short note"]
    drop --> more{"Attempts left?"}
    more -- "yes" --> attempt
    more -- "no" --> gaveup(["Stop: gave up"])
    stuck -- "no" --> reread{"step % MARATHON_REREAD_EVERY == 0?"}
    reread -- "yes" --> goal["Put goal.md and PROGRESS.md<br/>back at the end of the context"]
    goal --> turn
    reread -- "no" --> turn

    what -- "finish" --> pass{"Every test passes?"}
    pass -- "no" --> turn
    pass -- "yes" --> judge{"Judge, given only<br/>the goal and the diff:<br/>PASS?"}
    judge -- "FAIL, with reasons" --> turn
    judge -- "PASS" --> done(["Done<br/>git merge the attempt's branch"])

    turn -. "out of hours or turns" .-> budget(["Stop: out of budget<br/>work so far is on the branch"])
```

## One checkpoint

```mermaid
sequenceDiagram
    participant W as Worker (model)
    participant H as Harness (loop.py)
    participant G as git (gitops.py)
    participant C as Fresh test container

    W->>H: checkpoint("what I changed")
    H->>G: put tests/, goal.md and --protect paths back as in the base commit
    H->>G: commit "what I changed [step N]"
    opt every MARATHON_TEST_EVERY steps (1 = at every checkpoint)
        H->>C: run the tests: isolated Python, no network, no keys
        C-->>H: exit code and output
        H->>H: more tests passing? count test runs without progress
    end
    opt every MARATHON_SWITCH_AFTER test runs in a row without progress
        H-->>W: note why in PROGRESS.md, then switch approach
    end
    opt step % MARATHON_REREAD_EVERY == 0
        H-->>W: the goal and the checklist again
    end
    H-->>W: "Committed as step N. 3/4 tests pass." plus the failing output's path
```

## Where things live

The README's [What it leaves behind](../README.md#what-it-leaves-behind) shows the files and
branches a run creates.
