"""Recursive-generation runner for ProgramBench.

Pipeline: probe (Flash) -> plan (main model) -> implement modules (parallel) -> integrate.
Each phase gets its own agent session; context flows only through structured artifacts
(probe findings, contracts, code). All framework code (agents/environments/models) is
vendored at harness/minisweagent — launched via harness/pb-recursive.

Usage:
    harness/pb-recursive \
        --model openai/GLM-5.3 --probe-model openai/GLM-5.3-Flash \
        --filter ariga__atlas -o runs/recursive-v1
"""

import concurrent.futures
import copy
import json
import subprocess
import time
import traceback
from pathlib import Path

import typer
from rich.live import Live

from minisweagent.agents.default import DefaultAgent
from minisweagent.config import builtin_config_dir, get_config_from_spec
from minisweagent.environments import get_environment
from minisweagent.models import get_model
from minisweagent.run.benchmarks.programbench import DEFAULT_CONFIG_FILE, ProgramBenchAgent, copy_submission
from minisweagent.run.benchmarks.utils.batch_progress import RunBatchProgressManager
from minisweagent.utils.log import add_file_handler, logger
from minisweagent.utils.serialize import UNSET, recursive_merge

_HELP_TEXT = """Recursive-generation runner on ProgramBench instances.

Phases: probe -> plan -> implement -> integrate. Output is compatible with
[bold green]programbench eval[/bold green].
"""

_IMAGE_TAG = "task_cleanroom_v6"

app = typer.Typer(rich_markup_mode="rich", add_completion=False)


def run_phase(
    env,
    phase_name: str,
    system_template: str,
    instance_template: str,
    model_config: dict,
    agent_config: dict,
    instance: dict,
    traj_path: Path,
    progress_manager=None,
) -> dict:
    """Run one agent session (a single phase) and return its exit info."""
    model = get_model(config=model_config)
    agent_cls = ProgramBenchAgent if progress_manager is not None else DefaultAgent
    extra = {"progress_manager": progress_manager, "instance_id": instance["instance_id"]} if progress_manager else {}
    agent = agent_cls(
        model,
        env,
        system_template=system_template,
        instance_template=instance_template,
        **agent_config,
        **extra,
    )
    agent.extra_template_vars = {"instance": instance, "phase": phase_name,
                                 **instance.get("_template_vars", {})}
    info = agent.run()
    agent.save(traj_path, {"info": {"phase": phase_name, **info}, "instance_id": instance["instance_id"]})
    return info


def container_pull(env, container_path: str, local_path: Path) -> None:
    """Copy a file out of the container to the host (used to extract phase artifacts)."""
    executable = getattr(getattr(env, "config", None), "executable", None)
    if not getattr(env, "container_id", None) or not executable:
        raise RuntimeError("container_pull requires a Docker environment with container_id")
    subprocess.run(
        [executable, "cp", f"{env.container_id}:{container_path}", str(local_path)],
        check=True, capture_output=True, text=True,
    )


def probe_phase(env, instance, model_config, out_dir, progress_manager=None) -> dict:
    """Phase 1: the probe session enumerates the whole CLI itself (help tree, error paths,
    edge cases) — no scripted pre-pass, the agent is more general and robust than a parser."""
    info = run_phase(
        env,
        "probe",
        system_template=PROBE_SYSTEM,
        instance_template=PROBE_INSTANCE,
        model_config=model_config,
        agent_config={
            "output_path": str(out_dir / "probe.traj.json"),
            "step_limit": 200,
            "cost_limit": 0,
            "wall_time_limit_seconds": 7200,
        },
        instance=instance,
        traj_path=out_dir / "probe.traj.json",
        progress_manager=progress_manager,
    )
    for artifact in ("probe_findings.json", "probe_checks.json"):
        try:
            container_pull(env, f"/workspace/{artifact}", out_dir / artifact)
            logger.info(f"Pulled {artifact} for {instance['instance_id']}")
        except Exception as e:
            logger.error(f"probe phase did not produce {artifact}: {e}")
    return info


def plan_phase(env, instance, model_config, out_dir, progress_manager=None) -> dict:
    """Phase 2: architecture + frozen interface contracts (main model)."""
    info = run_phase(
        env,
        "plan",
        system_template=PLAN_SYSTEM,
        instance_template=PLAN_INSTANCE,
        model_config=model_config,
        agent_config={
            "output_path": str(out_dir / "plan.traj.json"),
            "step_limit": 160,
            "cost_limit": 0,
            "wall_time_limit_seconds": 3600,
        },
        instance=instance,
        traj_path=out_dir / "plan.traj.json",
    )
    try:
        container_pull(env, "/workspace/contracts.json", out_dir / "contracts.json")
        logger.info(f"Pulled contracts.json for {instance['instance_id']}")
    except Exception as e:
        logger.error(f"plan phase did not produce contracts.json: {e}")
    return info


def implement_phase(env, instance, model_config, out_dir, workers: int, progress_manager=None) -> list:
    """Phase 3: read the module list from the plan, implement each module in its own session."""
    contracts_file = out_dir / "contracts.json"
    if not contracts_file.is_file():
        logger.error("plan phase did not produce contracts.json; falling back to single-session implement")
        return [run_phase(env, "implement:fallback", system_template=MONOLITHIC_SYSTEM,
                          instance_template=MONOLITHIC_INSTANCE,
                          model_config=model_config,
                          agent_config={"output_path": str(out_dir / "implement.traj.json"),
                                        "step_limit": 600, "cost_limit": 0,
                                        "wall_time_limit_seconds": 10800},
                          instance=instance,
                          traj_path=out_dir / "implement.traj.json", progress_manager=progress_manager)]

    # A module session that ends with one of these statuses did real work and then
    # stopped for a limit reason - retrying it from scratch would just repeat that.
    # Any OTHER outcome (exception, network death, format loop) is treated as a
    # transient failure worth one retry.
    TERMINAL_OK = {"Submitted", "LimitsExceeded", "TimeExceeded"}

    def _run_module_session(module: dict, attempt: int) -> dict:
        traj = out_dir / f"implement_{module['name']}.traj.json"
        return run_phase(
            env,
            f"implement:{module['name']}",
            system_template=IMPLEMENT_SYSTEM,
            instance_template=IMPLEMENT_INSTANCE,
            model_config=model_config,
            agent_config={
                "output_path": str(traj),
                "step_limit": 150,
                "cost_limit": 0,
                "wall_time_limit_seconds": 5400,
            },
            instance={**instance, "_template_vars": {"module": module}},
            traj_path=traj,
            progress_manager=progress_manager,
        )

    def implement_one(module: dict) -> dict:
        name = module["name"]
        traj = out_dir / f"implement_{name}.traj.json"
        # Resume: a prior run that reached a terminal-OK status is not redone.
        if traj.is_file():
            try:
                if json.loads(traj.read_text()).get("info", {}).get("exit_status") in TERMINAL_OK:
                    logger.info(f"Skipping {name}: already implemented")
                    return {"module": name, "exit_status": "Skipped"}
            except Exception:
                pass
        # One retry on transient failure; a failure in one module must NOT abort
        # the whole batch (ex.map would otherwise cancel the remaining modules).
        last: dict = {"module": name, "exit_status": "NotStarted"}
        for attempt in (1, 2):
            try:
                info = _run_module_session(module, attempt)
                status = info.get("exit_status")
                last = {"module": name, "exit_status": status, "attempt": attempt}
                if status in TERMINAL_OK:
                    break
                logger.warning(f"Module {name} attempt {attempt} ended as {status}; retrying")
            except Exception as e:
                last = {"module": name, "exit_status": type(e).__name__, "attempt": attempt,
                        "error": str(e)[:300]}
                logger.error(f"Module {name} attempt {attempt} raised {type(e).__name__}: {e}")
        return last

    modules = json.loads(contracts_file.read_text())["modules"]
    checks_file = out_dir / "probe_checks.json"
    if checks_file.is_file():
        checks = json.loads(checks_file.read_text())
        assigned = sum(len(m.get("checks") or []) for m in modules)
        logger.info(f"Checks: {len(checks)} probed, {assigned} assigned across {len(modules)} modules")
        if any(not m.get("checks") for m in modules):
            logger.warning("Some modules have no checks assigned by plan - "
                           f"they will implement without verification: "
                           f"{[m['name'] for m in modules if not m.get('checks')]}")
    else:
        logger.warning("No probe_checks.json - implement runs without checks (legacy behavior)")

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(implement_one, modules))

    # Module-level status ledger: integrate reads this to know which modules are
    # complete vs. unfinished. A module is "unfinished" unless it reached a
    # terminal-OK status (or was skipped from a prior good run).
    ledger = {r["module"]: r["exit_status"] for r in results}
    (out_dir / "module_status.json").write_text(json.dumps(ledger, indent=1))
    unfinished = [m for m, s in ledger.items() if s not in TERMINAL_OK and s != "Skipped"]
    if unfinished:
        logger.warning(f"Unfinished modules (kept as-is for integrate to repair): {unfinished}")
    return results


def process_instance(instance: dict, output_dir: Path, config: dict, workers: int,
                     progress_manager: RunBatchProgressManager) -> None:
    iid = instance["instance_id"]
    instance_dir = output_dir / iid
    instance_dir.mkdir(parents=True, exist_ok=True)

    progress_manager.on_instance_start(iid)
    progress_manager.update_instance_status(iid, "Starting environment")

    inst_config = copy.deepcopy(config)
    env_cfg = inst_config.setdefault("environment", {})
    if not env_cfg.get("image"):
        env_cfg["image"] = f"{instance['image_name']}:{_IMAGE_TAG}"

    env = None
    exit_status = None
    extra_info: dict = {}
    try:
        env = get_environment(inst_config.get("environment", {}), default_type="docker")
        env.execute({"command": 'git config user.name "mini-swe-agent" && git config user.email "mini-swe-agent@proton.me"'})

        probe_cfg = dict(inst_config.get("model", {}))
        if probe_model := inst_config.get("probe_model_name"):
            probe_cfg["model_name"] = probe_model

        phases = inst_config.get("phases", ["probe", "plan", "implement", "integrate"])
        if "probe" in phases:
            probe_phase(env, instance, probe_cfg, instance_dir, progress_manager)
        if "plan" in phases:
            progress_manager.update_instance_status(iid, "Planning")
            plan_phase(env, instance, inst_config.get("model", {}), instance_dir, progress_manager)
        if "implement" in phases:
            progress_manager.update_instance_status(iid, "Implementing")
            implement_phase(env, instance, inst_config.get("model", {}), instance_dir, workers, progress_manager)
        if "integrate" in phases:
            progress_manager.update_instance_status(iid, "Integrating")
            # Push the authoritative module ledger into the container so integrate
            # knows which modules actually finished vs. died mid-way.
            ledger_file = instance_dir / "module_status.json"
            if ledger_file.is_file():
                import base64
                b64 = base64.b64encode(ledger_file.read_text().encode()).decode()
                env.execute({"command": f"echo {b64} | base64 -d > /workspace/module_ledger.json"})
            # Integration: one final session that builds, runs regressions, fixes issues.
            run_phase(
                env,
                "integrate",
                system_template=INTEGRATE_SYSTEM,
                instance_template=INTEGRATE_INSTANCE,
                model_config=inst_config.get("model", {}),
                agent_config={"output_path": str(instance_dir / "integrate.traj.json"), "step_limit": 300, "cost_limit": 0, "wall_time_limit_seconds": 7200},
                instance=instance,
                traj_path=instance_dir / "integrate.traj.json",
            )
        exit_status = "Submitted"
    except Exception as e:
        logger.error(f"Error processing instance {iid}: {e}", exc_info=True)
        exit_status = type(e).__name__
        extra_info = {"traceback": traceback.format_exc(), "exception_str": str(e)}
    finally:
        if env is not None:
            try:
                copy_submission(env, instance_dir / "submission.tar.gz")
            except Exception as e:
                logger.error(f"Failed to copy submission for {iid}: {e}", exc_info=True)
                extra_info["submission_copy_error"] = str(e)
        progress_manager.on_instance_end(iid, exit_status)


PROBE_SYSTEM = """You are a systematic reverse-engineering probe. You interact with a black-box
executable and record its behavior. You do NOT write an implementation.

You may run ./executable with any inputs. You must NOT decompile it or use strace/ltrace on it.

OUTPUT FORMAT (hard requirement - the downstream phase parses this file):
/workspace/probe_findings.json MUST be a JSON ARRAY, each element exactly:
{"command": "<the invocation>", "stdout": "<raw stdout>", "stderr": "<raw stderr>", "exit_code": <int>}
- One element per invocation you ran. Append as you go; the file is the evidence log.
- command: use the literal prefix "./executable <args>" (never the tool's own name,
  absolute paths, or wrappers) so entries match uniformly downstream.
- stdout/stderr: copy the raw bytes. Do NOT paraphrase, summarize, or write prose.
- Do NOT emit a dict/report/document with narrative keys (tool, key_behaviors, ...) -
  a prose summary is unusable downstream. Structured per-command entries ONLY.

SECOND OUTPUT - /workspace/probe_checks.json (this is what verifies the reimplementation):
Alongside the findings log, distill the most important behaviors you observed into a
JSON ARRAY of executable CHECKS. Each check re-runs a command and asserts the real
observed result. The implement phase runs these to decide whether its code is correct.
Each element:
{
  "command": "./executable <args>",      // the invocation to (re)run
  "setup": "<shell|null>",               // shell commands to create any needed state
                                          // (scratch dir, input files, initial DB) BEFORE the run; null if none
  "expect": {
    "exit_code": <int>,                  // exact expected exit code
    "stdout_contains": ["...", ...],     // substrings that MUST appear (use sparingly, stable ones)
    "stderr_contains": ["...", ...],     // substrings that MUST appear
    "stdout_regex": ["...", ...],        // regexes for parts that VARY (timestamps, versions, paths)
    "files_exist": ["...", ...]          // paths that MUST exist after the run (side-effect proof)
  }
}
Rules for good checks:
- Derive every expectation from a REAL run you recorded, never invent one.
- Prefer exit_code + one or two stable substrings over giant verbatim blobs.
- For volatile parts (timestamps like 20261016034007, version strings, absolute paths),
  use "stdout_regex"/"stderr_regex" patterns instead of exact substrings - a check that
  fails because a timestamp changed is worse than no check.
- Every check must be reproducible from a clean scratch dir: put all setup in "setup".
- Aim for breadth across the tool's command surface and success/error paths, but only
  include checks you have actually run and seen pass. Order them simplest-first.

Work through ALL of these dimensions - each one matters, do not sacrifice one for another.
The METHOD is fixed; WHICH things to try under each dimension you must DERIVE from the
tool itself (its README, its own sample files, and what its --help and error messages
reveal). Do not assume any particular tool type.

1. SUCCESS-PATH WORKFLOWS
   - The richest behavior is what the tool does when it SUCCEEDS, not when it errors.
     Make the tool actually WORK end to end, and exercise the SUCCESS output as hard as
     you exercise errors.
   - Identify the tool's primary object (the thing it creates/reads/transforms) and the
     minimal offline path that exercises it fully. Prefer inputs that need no network:
     local files, in-memory/embedded stores, scratch dirs. If the tool talks to external
     services, use any embedded/local/offline backend it supports; if none exists, say so.
   - Run a COMPLETE workflow as a real multi-step sequence in one scratch dir, e.g.
     create -> apply/run -> inspect/read-back -> update -> re-run. Do NOT stop after the
     first successful command.
   - Vary the OBJECT richness, not just one toy example: exercise the full feature set of
     the tool's input domain - many entities/records, optional and required fields,
     defaults, keys/constraints/relationships/uniqueness, nested or compound structures,
     ordering, and both add and remove/drop operations. A single trivial example hides most
     of the successful output (e.g. the exact DDL/format the tool emits for each feature).
   - Capture the full stdout (verbatim, including any headers, notices, tables or
     formatting) and exit code of EVERY successful step. When a step's stdout is long
     but important, keep it in full - success output IS the spec.
   - Also observe SIDE EFFECTS: files/dirs created (names + content), records written to
     any embedded store, state that changes across steps, whether existing data survives
     an update. You cannot always see these in stdout - read the files/store back with
     an independent command (inspect/list/status) or inspect the artifacts, and there too
     record the command you ran and its full output.
   - Everything goes into the SAME per-command log (the JSON array above): each read-back
     and each artifact-inspecting command is just another element. Never replace the log
     with a prose write-up.

2. COMMAND SURFACE
   - Start at ./executable --help and recursively expand every subcommand's --help
     (all depths). Note hidden/internal commands you discover along the way.
   - For each subcommand: try missing args, invalid args, invalid flag values, --help
     variants. Enumerate every flag you can find and exercise each one.
   - Things --help cannot show: error messages, env vars, stdin handling, and any
     real execution you can do safely.

3. INPUT SURFACE  (this is where most hidden behavior lives)
   - Identify every KIND of input the tool consumes: command arguments, files it reads,
     directory layouts, connection strings / identifiers, config files, stdin, env vars.
     Read the README and the tool's own bundled sample/example files to find these.
   - For each KIND of structured input, figure out the FORMAT FAMILY from the samples,
     then enumerate the VARIANTS you can infer (alternate syntaxes, naming conventions,
     optional vs required fields, with/without auxiliary files). For each variant, run
     the tool and record these cases:
       * a VALID instance  -> success output
       * a MALFORMED instance -> the exact error text (these error strings are commonly
         graded; capture them verbatim)
       * an EMPTY / missing instance -> the exact error text
   - If inputs reference external resources (URLs, schemes, providers, credentials,
     remote services), enumerate each referenced family WITHOUT real credentials and
     record the exact error. Infer the families from the README, not from guesswork.

4. OUTPUT SURFACE
   - For each command, capture stdout, stderr and exit code under: normal input, error
     input, empty input, boundary values, and any --format/--output variants the tool
     offers.

5. STATE SURFACE
   - For commands that persist state (write files, create dirs, modify config), run them
     in a scratch dir and record what they create/modify.

Budget discipline: cover BOTH the success paths (dimension 1) AND the breadth of
subcommands x input kinds (dimensions 2-5). Do not let one starve the other - a tool
you have not made succeed is under-probed, but so is one whose commands you never ran.
Say 10-20 checks in probe_checks.json is a good target; only include ones you verified pass.
HOW TO FINISH: every response MUST include at least one bash tool call. When BOTH
probe_findings.json (the full log) and probe_checks.json (the executable checks) are
complete, submit with exactly:
echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT
as your final command - never end with a plain text summary."""

PROBE_INSTANCE = """Task instance: {{ instance.instance_id }}
Probe the executable and complete /workspace/probe_findings.json."""

PLAN_SYSTEM = """You are a software architect reverse-engineering a black-box CLI tool.
Based on the probe results (/workspace/probe_findings.json — structured behavioral
observations) and README.md, produce:

1. /workspace/ARCHITECTURE.md: command tree, module list, dependency graph, topological order.
2. /workspace/contracts.json: {"modules": [{"name": ..., "cli_contract": ..., "depends_on": [...],
   "golden_samples": [...]}]} — one entry per module, interface frozen.
3. A compilable skeleton: entry dispatcher + stub modules + **compile.sh that already
   builds the skeleton into ./executable offline** (vendored/replaced deps, GOPROXY=off).
   Later phases only edit modules; compile.sh keeps working throughout.
4. Keep the module list to 8-10 modules (merge tiny related commands into one module);
   smaller count = fewer fixed overheads.
5. Derive the module list from what the probe actually found, not from a fixed template.
   For every KIND of input the probe identified (each config/file/identifier family and
   its variants), make sure some module owns it and its cli_contract lists the exact
   behaviors observed - including the verbatim error texts for malformed/missing inputs.
   A family the probe observed but no module covers is a scoring gap.
6. In each module's cli_contract, specify a LEAN regression script: a handful (~5-10)
   of representative cases, not an exhaustive suite. Exhaustive per-module test suites
   are the main cost driver and are not needed - the official grader tests behavior.
7. ASSIGN THE CHECKS. /workspace/probe_checks.json contains executable checks distilled
   from real runs. Attribute EVERY check to exactly one module (the module that owns the
   command it exercises) and put it in that module as a "checks" list. A check whose
   command spans modules goes to the module owning the command's primary entry point.
   Do NOT drop checks and do NOT invent new ones - redistribute the given ones verbatim
   (you may adjust only the "module" attribution). The implement phase runs each module's
   checks to decide if its code is correct, so a missing attribution = an unverified module.

CRITICAL ORDERING: write /workspace/contracts.json FIRST (before the skeleton, before
anything else) - the next phase reads it and fails without it. Then the skeleton, then
compile.sh.

Discipline: contracts are frozen once written. Do NOT implement module internals, do NOT
run the module test suites, do NOT debug the executable in this phase - that is the
implement phase's job. Your only outputs are contracts.json, ARCHITECTURE.md, the skeleton
and compile.sh. Stop as soon as compile.sh builds the skeleton offline.
HOW TO FINISH: every response MUST include a bash tool call. When done, submit with
exactly: echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT as your final command."""

PLAN_INSTANCE = """Task instance: {{ instance.instance_id }}
Produce ARCHITECTURE.md, contracts.json and a skeleton WITH a working compile.sh."""

IMPLEMENT_SYSTEM = """You implement ONE module of a reverse-engineered codebase.
The skeleton and contracts are already in /workspace. Read /workspace/contracts.json
and implement ONLY the module named in the task.

Rules:
- Only modify files belonging to your module (and its tests).
- Probe ./executable to capture golden outputs for your module's behaviors; add a
  regression script under your module's tests/ directory. SCALE IT TO YOUR CONTRACT:
  at least one case per frozen behavior in your cli_contract (every subcommand,
  every flag you committed to, every error path in the contract) - NOT a handful of
  self-chosen easy cases. Do NOT build an exhaustive suite beyond the contract either.
- FIXTURE DIVERSITY (critical): a self-test passing on ONE hand-made fixture proves
  little. Vary your test data the way real users differ along the dimensions that apply
  to YOUR module's inputs - for example: with and without any auxiliary/companion files
  the format allows, alternate naming or syntax conventions the format permits, empty
  vs populated, malformed entries, files with and without trailing newline. If your
  module takes a file or directory input, test at least 3 DIFFERENT fixture shapes.
  The grader runs inputs you have not seen, and failures like "checksum mismatch"
  come from fixtures that differ from the one you happened to pick.
- FIX BUDGET (hard rule): implement, run your regression script ONCE, then fix at most
  TWO rounds. After that, STOP - even if some cases still fail. Do not chase perfection:
  you cannot see the other modules or the real grader, so polishing here is blind.
- When you stop, write /workspace/module_status.md (append, keep it short) with:
  module name, files touched, what works, and each known-failing case with its exact
  input/output. The integrate phase reads this to fix things globally.
- Be precise and honest in module_status.md: list exact commands + expected vs actual.
  A good status file lets integrate fix your gaps in few steps; a vague one wastes
  integrate's budget re-discovering them.
- MANDATORY CHECKS (this is how correctness is judged - do NOT skip): your module entry
  in /workspace/contracts.json has a "checks" list. These are REAL observations of the
  reference executable, distilled into executable assertions. After your code compiles,
  run every check for your module and make them pass. A check runs a command (after
  optional "setup" shell) and asserts the "expect" block: exit_code, stdout/stderr
  contains-substrings, stdout/stderr regexes, and files that must exist afterwards.
  Build a small runner script (e.g. checks/run_<module>.sh) that executes each check from
  a CLEAN scratch dir (re-running "setup" each time), prints PASS/FAIL per check with the
  expected-vs-actual diff on failure, and exits non-zero if any fail. Use it to drive your
  fixes - the expected values ARE the ground truth from the real binary.
  VOLATILE fields: match with the regexes the check provides; do NOT hardcode a timestamp
  or version you saw once. If a check genuinely cannot pass because it encodes a
  non-reproducible value, note it in module_status.md and move on - but first assume you
  must implement the behavior, not that the check is wrong.
- FIX BUDGET (hard rule): implement, run your regression script and your CHECKS, then fix
  at most TWO rounds. After that, STOP - even if some cases still fail. Do not chase
  perfection: you cannot see the other modules or the real grader, so polishing is blind.
- DONE CRITERIA (strict): compiles + your module's CHECKS all pass (or the 2-round fix
  budget is exhausted) + your regression script covers EVERY frozen behavior in your
  contract + module_status.md written. Finishing with only part of the contract covered
  is a FAILED module - use your remaining steps on uncovered behaviors instead.
- module_status.md MUST report, per check: command, PASS/FAIL, and on failure the exact
  expected vs actual. The integrate phase uses this to fix what you could not.
- HOW TO FINISH (critical): every response MUST include at least one bash tool call.
  When your module is done, write module_status.md, then submit with exactly:
  echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT
  as your final command. NEVER end with a plain text summary - the session aborts
  after 3 consecutive text-only responses."""

IMPLEMENT_INSTANCE = """Task instance: {{ instance.instance_id }}
Implement module: {{ module.name }}
Contract: {{ module.cli_contract }}
Depends on: {{ module.depends_on | join(', ') }}

Your module's CHECKS (executable assertions distilled from the real reference binary;
make these pass - see the MANDATORY CHECKS rule in your system prompt):
{{ module.checks | tojson(indent=2) if module.checks else "[]  (no checks assigned to this module)" }}"""

INTEGRATE_SYSTEM = """You are integrating independently-implemented modules of a reverse-engineered
codebase in /workspace. The skeleton already has a working compile.sh - never rewrite
it from scratch, only adjust module source until ./compile.sh builds ./executable
offline.

You have the GLOBAL view the module sessions lacked. Work in this order:
1. Read /workspace/module_ledger.json FIRST. It maps each module to how its session
   ended. "Submitted"/"LimitsExceeded"/"TimeExceeded" = the module ran (it may still
   have gaps it reported); ANYTHING ELSE (an exception name, "NotStarted") = that module
   DIED mid-way and its source is probably incomplete or missing - treat those as the
   highest priority, because a whole feature area may be absent. Then read
   /workspace/module_status.md for the per-module known failures. Do NOT re-verify what
   modules reported as working - trust those and go straight to the listed failures and
   the dead modules. Build a prioritized fix list: dead modules and failures affecting
   many commands first, exotic edge cases last.
2. Fix integration breakage first (compile errors, contract mismatches between modules).
3. Then spend remaining budget on the highest-value failures from the list. You will
   not fix everything - that is expected and fine. Prefer breadth (many commands
   basically right) over depth (one command perfect).
4. STRESS WITH DIVERSE DATA: before finishing, run the core commands against fixture
   shapes the modules may not have tested - vary the input-family dimensions listed
   in the contract (alternate naming/syntax conventions, with/without auxiliary files,
   empty vs populated, malformed entries). Modules self-test on their own fixtures;
   your job is to catch where those fixtures were unrepresentative (e.g. failures on
   real-world input shapes the module never synthesized).

Respect frozen contracts; if a contract is wrong, fix it centrally and note the change
in /workspace/CHANGELOG.md.
Done = ./compile.sh produces ./executable and all regressions pass.
IMPORTANT: every response must include at least one bash tool call. When you are
finished, run `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` as your final command -
never end with a plain text summary (that aborts the session)."""

INTEGRATE_INSTANCE = """Task instance: {{ instance.instance_id }}
Integrate and finalize the codebase."""

# Fallback when plan produced no contracts.json: one full-session implement
# (mirrors the official single-session prompt but seeds it with the probe artifacts).
MONOLITHIC_SYSTEM = """You are re-implementing a black-box CLI tool from behavioral observation.
The executable at ./executable is observation-only; write an original codebase that
reproduces its behavior. Do not decompile it, do not strace/ltrace it, do not fetch or
wrap the original source.

Prior work exists: /workspace/probe_findings.json records structured observations
{command, stdout, stderr, exit_code}. Mine it first instead of re-probing from
scratch. Also read README.md and ./assets/.

Deliverables: source code + compile.sh that builds ./executable OFFLINE (vendored deps,
no network). Commit your work. Finish with `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`."""

MONOLITHIC_INSTANCE = """Task instance: {{ instance.instance_id }}
No module contracts were produced by the planning phase. Implement the full codebase
yourself: study the probe artifacts, design your own module layout, and produce a
working ./compile.sh and ./executable."""


# fmt: off
@app.command(help=_HELP_TEXT)
def main(
    slice_spec: str = typer.Option("", "--slice", help="Slice specification", rich_help_panel="Data selection"),
    filter_spec: str = typer.Option("", "--filter", help="Filter instance IDs by regex", rich_help_panel="Data selection"),
    shuffle: bool = typer.Option(False, "--shuffle", help="Shuffle instances", rich_help_panel="Data selection"),
    output: str = typer.Option("", "-o", "--output", help="Output directory", rich_help_panel="Basic"),
    workers: int = typer.Option(1, "-w", "--workers", help="Worker threads (instances AND modules)", rich_help_panel="Basic"),
    model: str | None = typer.Option(None, "-m", "--model", help="Main model (plan/implement/integrate)", rich_help_panel="Basic"),
    probe_model: str | None = typer.Option(None, "--probe-model", help="Cheap model for probe phase", rich_help_panel="Basic"),
    redo_existing: bool = typer.Option(False, "--redo-existing", help="Redo existing instances", rich_help_panel="Data selection"),
    config_spec: list[str] = typer.Option([str(DEFAULT_CONFIG_FILE)], "-c", "--config", help="Config files (merged left to right)", rich_help_panel="Basic"),
    phases: str = typer.Option("probe,plan,implement,integrate", "--phases", help="Comma-separated phases to run (debug/partial runs)", rich_help_panel="Advanced"),
    environment_class: str | None = typer.Option(None, "--environment-class", help="Environment type", rich_help_panel="Advanced"),
) -> None:
    # fmt: on
    from programbench.utils.instance_filters import filter_instances
    from programbench.utils.load_data import load_all_instances

    output_path = Path(output) if output else Path(f"recursive_results_{int(time.time())}")
    output_path.mkdir(parents=True, exist_ok=True)
    logger.info(f"Results will be saved to {output_path}")
    add_file_handler(output_path / "minisweagent.log")

    instances = load_all_instances(include_tests=False)
    instances = filter_instances(instances, filter_spec=filter_spec, slice_spec=slice_spec, shuffle=shuffle)

    if not redo_existing:
        existing = {i["instance_id"] for i in instances if (output_path / i["instance_id"] / "submission.tar.gz").exists()}
        if existing:
            logger.info(f"Skipping {len(existing)} existing instances")
            instances = [i for i in instances if i["instance_id"] not in existing]

    logger.info(f"Running on {len(instances)} instances...")

    configs = [get_config_from_spec(spec) for spec in config_spec]
    configs.append({
        "environment": {"environment_class": environment_class or UNSET},
        "model": {"model_name": model or UNSET},
        "probe_model_name": probe_model or UNSET,
        "phases": [p.strip() for p in phases.split(",") if p.strip()],
    })
    config = recursive_merge(*configs)

    progress_manager = RunBatchProgressManager(len(instances), output_path / f"exit_statuses_{int(time.time())}.yaml")

    def process_futures(futures: dict[concurrent.futures.Future, str]):
        for future in concurrent.futures.as_completed(futures):
            try:
                future.result()
            except concurrent.futures.CancelledError:
                pass
            except Exception as e:
                instance_id = futures[future]
                logger.error(f"Error in future for instance {instance_id}: {e}", exc_info=True)
                progress_manager.on_uncaught_exception(instance_id, e)

    with Live(progress_manager.render_group, refresh_per_second=4):
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(process_instance, instance, output_path, config, workers, progress_manager): instance[
                    "instance_id"
                ]
                for instance in instances
            }
            try:
                process_futures(futures)
            except KeyboardInterrupt:
                logger.info("Cancelling all pending jobs. Press ^C again to exit immediately.")
                for future in futures:
                    if not future.running() and not future.done():
                        future.cancel()
                process_futures(futures)


if __name__ == "__main__":
    app()
