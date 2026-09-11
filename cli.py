from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import signal
import shutil
import sys
import threading
import time
from pathlib import Path

from wmdcfg.actuator import ActuatorError, ControlClient
from wmdcfg.compiler import compile_scenario
from wmdcfg.inventory import discover
from wmdcfg.model import ScenarioError
from wmdcfg.observers import mesh_health
from wmdcfg.parser import parse
from wmdcfg.runner import Runner
from wmdcfg.rf_contract import capability_manifest
from wmdcfg.world import _hash, export_wmd, load_json, verify_world_plan

from .conductor import LiveConductor, load_manifest
from .engine import RoomEngine
from .events import EventStore
from .interactions import InteractiveMediumSession
from .recovery import RecoveryJournal, inventory_identity, load_recovery, recover_medium
from .server import RoomDemoServer
from .worlds import BoundWorlds
from .client_wifi import disconnected_client, resume_bound_client
from .backhaul import BackhaulManager, RdkBackhaulAdapter


DEMO_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = DEMO_ROOT.parents[1]
CONFIGURATOR = REPO_ROOT / "gen/wmediumd/configurator"
DEFAULT_MANIFEST = DEMO_ROOT / "manifests/private-client-room-walk.json"
DEFAULT_VIEWER = CONFIGURATOR / "worlds/viewer"


def _address(value: str) -> tuple[str, int]:
    try:
        host, raw_port = value.rsplit(":", 1)
        port = int(raw_port)
    except (ValueError, TypeError) as error:
        raise argparse.ArgumentTypeError("use HOST:PORT") from error
    if not host or not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("use HOST:PORT")
    return host, port


def _prepare(world_path: Path, binding_path: Path):
    world = load_json(world_path)
    verify_world_plan(world)
    source = export_wmd(world, "all")
    scenario = parse(source)
    inventory = discover()
    binding_doc = json.loads(binding_path.read_text(encoding="utf-8"))
    bindings = binding_doc.get("roles")
    if not isinstance(bindings, dict):
        raise ScenarioError(f"{binding_path}: roles must be an object")
    plan = compile_scenario(scenario, source, inventory, bindings)
    return world, source, inventory, binding_doc, plan


def _status(socket_path: str) -> dict:
    with ControlClient(socket_path) as client:
        status = client.status()
    return {
        "instance_id": status.instance_id,
        "generation": status.generation,
        "num_stations": status.num_stations,
        "capabilities": sorted(status.capabilities),
        "rf_contract": capability_manifest("userspace", status),
    }


def _paths(args) -> tuple[dict, Path, Path]:
    manifest_path = args.manifest.resolve()
    manifest = load_manifest(manifest_path, REPO_ROOT)
    world = args.world.resolve() if args.world else REPO_ROOT / manifest["world"]
    bindings = args.bindings.resolve() if args.bindings else REPO_ROOT / manifest["bindings"]
    return manifest, world, bindings


def _layout_for(world: dict) -> tuple[dict, Path]:
    path = CONFIGURATOR / "worlds/layouts" / f"{world['layout']}.json"
    layout = load_json(path)
    if _hash(layout) != world.get("layout_sha256"):
        raise ScenarioError(f"{path}: layout hash does not match the signed Golden World")
    return layout, path


def _file_index(run_dir: Path) -> dict:
    files = {}
    for path in sorted(run_dir.iterdir()):
        if not path.is_file() or path.name == "evidence-index.json":
            continue
        files[path.name] = {
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    return {"schema": "easymesh.room-demo.evidence.v1", "files": files}


def _copy_inputs(runner, source, inventory, binding_doc, world_path, manifest) -> None:
    runner.run_dir.mkdir(parents=True, exist_ok=True)
    (runner.run_dir / "world.wmd").write_text(source, encoding="utf-8")
    (runner.run_dir / "inventory.json").write_text(
        json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (runner.run_dir / "bindings.json").write_text(
        json.dumps(binding_doc, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (runner.run_dir / "demo-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    shutil.copy2(world_path, runner.run_dir / "world.json")


def _check(args) -> int:
    manifest, world_path, bindings_path = _paths(args)
    status = _status(args.socket)
    world, _source, _inventory, _binding_doc, plan = _prepare(world_path, bindings_path)
    hero = manifest["hero"]["role"]
    print(json.dumps({
        "status": "ready",
        "manifest": manifest["name"],
        "world": world["name"],
        "duration_ms": world["duration_ms"],
        "roles": len(world["roles"]),
        "hero": {
            "role": hero,
            "container": plan["bindings"][hero]["container"],
            "radio_permanent_mac": plan["bindings"][hero]["radio_permanent_mac"],
        },
        "wmediumd": status,
        "plan_generations": sum(bool(item["updates"]) for item in plan["events"]),
    }, indent=2, sort_keys=True))
    return 0


def _run(args) -> int:
    if args.mode == "act" and not args.yes_act:
        raise ActuatorError("act mode requires --yes-act")
    manifest, world_path, bindings_path = _paths(args)
    _status(args.socket)
    world, source, inventory, binding_doc, plan = _prepare(world_path, bindings_path)
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{timestamp}-{manifest['name']}"
    runner = Runner(plan, args.socket, args.output_root, run_id=run_id,
                    clock_interval_ms=args.clock_interval_ms)
    store = EventStore(run_id, world, runner.run_dir / "live-events.jsonl")
    runner.event_callback = store.ingest
    conductor = LiveConductor(store, plan, manifest, mode=args.mode,
                              repo_root=REPO_ROOT, base_url=args.base_url)
    server = RoomDemoServer(args.listen, store, DEFAULT_VIEWER)
    args.lock.parent.mkdir(parents=True, exist_ok=True)
    lock_stream = args.lock.open("a+")
    try:
        fcntl.flock(lock_stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        lock_stream.close()
        raise ActuatorError(f"another room demo owns {args.lock}") from error
    lock_stream.seek(0)
    lock_stream.truncate()
    lock_stream.write(run_id + "\n")
    lock_stream.flush()

    server_started = False
    try:
        _copy_inputs(runner, source, inventory, binding_doc, world_path, manifest)
        server.start()
        server_started = True
        host, port = server.address
        display_host = "127.0.0.1" if host == "0.0.0.0" else host
        print(f"room-demo: immersive viewer http://{display_host}:{port}/viewer/?mode=live")
        print(f"room-demo: run {run_id}; mode={args.mode}; hero={conductor.hero_mac}; "
              "RF writer=wmdcfg Runner")
        failure: Exception | None = None
        conductor.preflight()
        conductor.start()
        try:
            runner.execute()
        except Exception as error:
            failure = error
            print(f"room-demo: run failed: {error}", file=sys.stderr)
        finally:
            conductor.stop()
        runner_summary = json.loads(
            (runner.run_dir / "summary.json").read_text(encoding="utf-8")
        )
        conductor_summary = conductor.summary()
        if conductor_summary["worker_errors"] and failure is None:
            failure = RuntimeError("one or more live workers failed")
        if args.mode == "act" and (
            conductor_summary["action_attempts"] != 1
            or conductor_summary["action_successes"] != 1
            or conductor_summary["verification_successes"] != 1
        ) and failure is None:
            failure = RuntimeError("act mode did not complete one verified steering action")
        outcome = "passed" if failure is None and runner_summary.get("outcome") == "passed" else "failed"
        restored = bool(runner_summary.get("restored"))
        demo_summary = {
            "schema": "easymesh.room-demo.summary.v1",
            "run_id": run_id,
            "outcome": outcome,
            "restored": restored,
            "error": str(failure) if failure else runner_summary.get("error"),
            "runner": runner_summary,
            "conductor": conductor_summary,
        }
        (runner.run_dir / "demo-summary.json").write_text(
            json.dumps(demo_summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        store.emit("run.completed", world["duration_ms"], {
            "outcome": outcome,
            "restored": restored,
            "error": demo_summary["error"],
            "summary": conductor_summary,
        }, producer="conductor")
        (runner.run_dir / "evidence-index.json").write_text(
            json.dumps(_file_index(runner.run_dir), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"room-demo: outcome={outcome} restored={str(restored).lower()} "
              f"actions={conductor.action_attempts} verified={conductor.verification_successes}")
        print(f"room-demo: evidence {runner.run_dir}")
        if args.linger_seconds > 0:
            print(f"room-demo: completed view remains available for {args.linger_seconds}s")
            time.sleep(args.linger_seconds)
        return 0 if outcome == "passed" else 1
    except Exception as error:
        conductor.stop()
        runner.run_dir.mkdir(parents=True, exist_ok=True)
        restored = not (runner.run_dir / "event-plan.json").exists()
        failed_summary = {
            "schema": "easymesh.room-demo.summary.v1",
            "run_id": run_id,
            "outcome": "failed",
            "restored": restored,
            "error": str(error),
            "runner": None,
            "conductor": conductor.summary(),
        }
        (runner.run_dir / "demo-summary.json").write_text(
            json.dumps(failed_summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        store.emit("run.completed", store.current()["world_time_ms"], {
            "outcome": "failed", "restored": restored, "error": str(error),
            "summary": conductor.summary(),
        }, producer="conductor")
        (runner.run_dir / "evidence-index.json").write_text(
            json.dumps(_file_index(runner.run_dir), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"room-demo: {error}", file=sys.stderr)
        return 2
    finally:
        if server_started:
            server.close()
        else:
            server.httpd.server_close()
        fcntl.flock(lock_stream, fcntl.LOCK_UN)
        lock_stream.close()


def _interactive_preflight(conductor, expected_agents, expected_clients, recovered, stop_event):
    deadline = time.monotonic() + (30 if recovered else 0)
    while True:
        try:
            health = mesh_health(expected_agents, expected_clients)
            Runner._require_healthy(health, "interactive preflight")
            conductor.preflight()
            return health
        except (ActuatorError, RuntimeError) as error:
            expected_failure = isinstance(error, ActuatorError) or str(error).startswith("demo preflight failed:")
            if not recovered or not expected_failure or time.monotonic() >= deadline:
                raise
            if stop_event.wait(0.5):
                raise ActuatorError("recovery preflight cancelled") from error


def _interactive(args) -> int:
    if args.model_backhaul and (not args.profiling or args.mode != "stimulus"):
        raise ActuatorError("--model-backhaul requires --profiling --mode stimulus: no external client or parent steering")
    if args.profiling and args.adaptive_backhaul:
        raise ActuatorError("--profiling excludes --adaptive-backhaul: external parent selection is not native EasyMesh policy")
    if args.mode == "act" and not args.yes_act:
        raise ActuatorError("interactive act mode requires --yes-act")
    if args.max_actions is not None and args.max_actions < 1:
        raise ActuatorError("interactive --max-actions must be a positive integer")
    manifest, world_path, bindings_path = _paths(args)
    world, source, inventory, binding_doc, plan = _prepare(world_path, bindings_path)
    layout, layout_path = _layout_for(world)
    runtime_world = InteractiveMediumSession.runtime_world(world, layout)
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{timestamp}-{manifest['name']}-interactive"
    runner = Runner(plan, args.socket, args.output_root, run_id=run_id)
    store = EventStore(run_id, runtime_world, runner.run_dir / "live-events.jsonl", asynchronous=True)
    recovery = RecoveryJournal(args.recovery_file, run_id, _hash(inventory),
                               inventory_identity_sha256=inventory_identity(inventory))
    backhaul_adapter = RdkBackhaulAdapter(plan) if args.adaptive_backhaul and args.mode == "act" else None
    if args.adaptive_backhaul and backhaul_adapter is None:
        raise ActuatorError("--adaptive-backhaul requires interactive --mode act --yes-act")
    interactions = RoomEngine(
        InteractiveMediumSession(
            store, world, layout, plan, args.socket,
            lease_seconds=args.lease_seconds,
            traffic_probe_role=manifest["hero"]["role"],
            worlds=BoundWorlds(world, layout, CONFIGURATOR / "worlds"),
            disconnect_client=lambda role: disconnected_client(plan, role, recovery),
            reconnect_client=lambda role: resume_bound_client(plan, role, recovery),
            recovery=recovery,
            adaptive_backhaul=backhaul_adapter is not None,
            model_backhaul=args.model_backhaul,
        )
    )
    # Interactive act mode is a continuously running reconciler. Keep a
    # generous explicit circuit breaker rather than inheriting the scripted
    # demonstration's single-action budget.
    maximum_actions = args.max_actions
    if maximum_actions is None and args.mode == "act":
        maximum_actions = 100
    conductor = LiveConductor(
        store, plan, manifest, mode=args.mode, repo_root=REPO_ROOT,
        base_url=args.base_url, room_state=interactions.snapshot,
        room_projection=interactions.projection_snapshot,
        interactive=True, profiling=args.profiling, maximum_actions=maximum_actions,
        steering_transaction=interactions.steering_action,
        backhaul_manager=BackhaulManager(backhaul_adapter, interactions.backhaul_action, store)
        if backhaul_adapter is not None else None,
    )
    server = RoomDemoServer(
        args.listen,
        store,
        DEFAULT_VIEWER,
        interactions,
    )
    stop_event = threading.Event()
    clock_thread: threading.Thread | None = None
    old_handlers = {}
    lock_stream = None
    server_started = False
    session_started = False
    outcome = "failed"
    restored = False
    error_text = None
    started = time.monotonic()

    def stop_requested(_signum=None, _frame=None):
        stop_event.set()

    try:
        args.lock.parent.mkdir(parents=True, exist_ok=True)
        lock_stream = args.lock.open("a+")
        try:
            fcntl.flock(lock_stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ActuatorError(f"another room demo owns {args.lock}") from error
        lock_stream.seek(0)
        lock_stream.truncate()
        lock_stream.write(run_id + "\n")
        lock_stream.flush()
        for signum in (signal.SIGINT, signal.SIGTERM):
            old_handlers[signum] = signal.signal(signum, stop_requested)

        _copy_inputs(runner, source, inventory, binding_doc, world_path, manifest)
        shutil.copy2(layout_path, runner.run_dir / "layout.json")
        (runner.run_dir / "runtime-world.json").write_text(
            json.dumps(runtime_world, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        expected = plan.get("expected_lab") or {}
        expected_agents = int(expected.get("mesh_devices", 5))
        expected_clients = int(expected.get("clients", 20))
        recovered = False
        if args.recovery_file.exists():
            previous_run = load_recovery(args.recovery_file)
            if previous_run.get("state") != "restored" or previous_run.get("paused_clients"):
                if previous_run.get("inventory_identity_sha256") != inventory_identity(inventory):
                    raise ActuatorError("recovery inventory does not match this lab")
                recovery_result = recover_medium(args.recovery_file, args.socket)
                store.emit("room.recovery.completed", 0, recovery_result, producer="interaction")
                recovered = True
        initial_health = _interactive_preflight(
            conductor, expected_agents, expected_clients, recovered, stop_event)
        (runner.run_dir / "health-preflight.json").write_text(
            json.dumps(initial_health, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        interactions.start()
        session_started = True
        store.emit(
            "demo.state", 0,
            {"state": "running", "mode": f"interactive-{args.mode}"},
            producer="interaction",
        )
        conductor.start()

        def clock_worker():
            while not stop_event.wait(0.25):
                elapsed = round((time.monotonic() - started) * 1000)
                store.emit(
                    "scenario.clock", min(world["duration_ms"], elapsed),
                    {"interactive": True}, producer="interaction-clock",
                )

        clock_thread = threading.Thread(
            target=clock_worker, name="room-demo-interaction-clock", daemon=True
        )
        clock_thread.start()
        server.start()
        server_started = True
        host, port = server.address
        display_host = "127.0.0.1" if host == "0.0.0.0" else host
        print(
            f"room-demo: interactive viewer "
            f"http://{display_host}:{port}/viewer/"
        )
        print("room-demo: interactive control has no authentication; restrict access to a trusted lab or authenticated gateway")
        print(
            f"room-demo: run {run_id}; authority={args.mode}; "
            "RF writer=interactive wmdcfg session; Ctrl-C restores the exact baseline"
        )
        deadline = (
            None if args.serve_seconds <= 0
            else time.monotonic() + args.serve_seconds
        )
        while not stop_event.wait(0.5):
            if deadline is not None and time.monotonic() >= deadline:
                break
            if conductor.errors:
                raise RuntimeError("one or more live workers failed")
        outcome = "passed"
    except Exception as error:
        error_text = str(error)
        print(f"room-demo: interactive session failed: {error}", file=sys.stderr)
    finally:
        stop_event.set()
        if server_started:
            server.close()
        else:
            server.httpd.server_close()
        conductor.stop()
        if clock_thread is not None:
            clock_thread.join(timeout=2)
        try:
            restored = interactions.close()
        except Exception as error:
            restored = False
            outcome = "failed"
            error_text = f"{error_text + '; ' if error_text else ''}restore: {error}"
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)
        if outcome == "passed" and not restored:
            outcome = "failed"
            error_text = "interactive RF baseline restoration was not verified"
        try:
            if session_started:
                expected = plan.get("expected_lab") or {}
                health_deadline = time.monotonic() + 60
                while True:
                    final_health = mesh_health(
                        int(expected.get("mesh_devices", 5)),
                        int(expected.get("clients", 20)),
                    )
                    try:
                        Runner._require_healthy(final_health, "interactive postflight")
                        break
                    except ActuatorError:
                        if time.monotonic() >= health_deadline:
                            raise
                        time.sleep(2)
                (runner.run_dir / "health-postflight.json").write_text(
                    json.dumps(final_health, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
        except Exception as error:
            outcome = "failed"
            error_text = f"{error_text + '; ' if error_text else ''}postflight: {error}"
        recorded_mobility, recorded_world = interactions.recorded_documents()
        if recorded_mobility is not None:
            (runner.run_dir / "recorded-mobility.json").write_text(
                json.dumps(recorded_mobility, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        if recorded_world is not None:
            (runner.run_dir / "recorded-world.json").write_text(
                json.dumps(recorded_world, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        if args.recovery_file.exists():
            shutil.copy2(args.recovery_file, runner.run_dir / "recovery.json")
        summary = {
            "schema": "easymesh.room-demo.interactive-summary.v1",
            "run_id": run_id,
            "outcome": outcome,
            "restored": restored,
            "error": error_text,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
            "interactions": interactions.snapshot(),
            "conductor": conductor.summary(),
        }
        runner.run_dir.mkdir(parents=True, exist_ok=True)
        (runner.run_dir / "interactive-summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        store.emit(
            "run.completed", store.current()["world_time_ms"],
            {"outcome": outcome, "restored": restored, "error": error_text},
            producer="interaction",
        )
        store.close()
        summary["evidence_storage"] = store.storage_status()
        if not summary["evidence_storage"]["journal"]["complete"]:
            outcome = "failed"
            error_text = summary["evidence_storage"]["journal"]["error"]
            summary.update(outcome=outcome, error=error_text)
        (runner.run_dir / "interactive-summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (runner.run_dir / "storage-summary.json").write_text(
            json.dumps(store.storage_status(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (runner.run_dir / "evidence-index.json").write_text(
            json.dumps(_file_index(runner.run_dir), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if lock_stream is not None:
            fcntl.flock(lock_stream, fcntl.LOCK_UN)
            lock_stream.close()
    print(f"room-demo: outcome={outcome} restored={str(restored).lower()}")
    print(f"room-demo: evidence {runner.run_dir}")
    return 0 if outcome == "passed" else 1


def _replay(args) -> int:
    store = EventStore.from_evidence(args.run_directory.resolve())
    server = RoomDemoServer(args.listen, store, DEFAULT_VIEWER, replay=True)
    server.start()
    host, port = server.address
    display_host = "127.0.0.1" if host == "0.0.0.0" else host
    print(f"room-demo: replay http://{display_host}:{port}/viewer/")
    print(f"room-demo: evidence {args.run_directory.resolve()}")
    try:
        if args.serve_seconds > 0:
            time.sleep(args.serve_seconds)
        else:
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        return 0
    finally:
        server.close()
    return 0


def _recover(args) -> int:
    args.lock.parent.mkdir(parents=True, exist_ok=True)
    lock_stream = args.lock.open("a+")
    try:
        try:
            fcntl.flock(lock_stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ActuatorError(f"another room demo owns {args.lock}") from error
        result = recover_medium(args.recovery_file, args.socket)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    finally:
        fcntl.flock(lock_stream, fcntl.LOCK_UN)
        lock_stream.close()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="room-demo",
        description="Run and present the closed-loop RDK EasyMesh room demonstration.",
    )
    commands = result.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("check", "compile and validate the live radio bindings without changing RF"),
        ("run", "run, present, verify and restore one live room experiment"),
        ("interactive", "serve a lease-protected live room that controls wmediumd"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST,
                             help="demo contract JSON (default: private client room walk)")
        command.add_argument("--world", type=Path, help="override the manifest Golden World")
        command.add_argument("--bindings", type=Path, help="override manifest role bindings")
        command.add_argument("--socket", default="/run/wmediumd-control.sock",
                             help="wmediumd control socket")
    run = commands.choices["run"]
    run.add_argument("--mode", choices=("stimulus", "recommend", "act"), default="recommend",
                     help="presentation authority (default: recommend; no network action)")
    run.add_argument("--yes-act", action="store_true",
                     help="required second confirmation for the bounded act mode")
    run.add_argument("--base-url", default="http://127.0.0.1:8888")
    run.add_argument("--listen", type=_address, default=("127.0.0.1", 8891), metavar="HOST:PORT")
    run.add_argument("--output-root", type=Path, default=Path("/tmp/easymesh-room-demo-runs"))
    run.add_argument("--linger-seconds", type=int, default=60,
                     help="retain completed live view before exit (default: 60)")
    run.add_argument("--clock-interval-ms", type=int, default=250)
    run.add_argument("--lock", type=Path, default=Path("/run/lock/easymesh-room-demo.lock"))
    interactive = commands.choices["interactive"]
    interactive.add_argument(
        "--mode", choices=("stimulus", "recommend", "act"), default="recommend",
        help="optimizer authority (default: recommend)",
    )
    interactive.add_argument("--yes-act", action="store_true")
    interactive.add_argument("--profiling", action="store_true",
                             help="unassisted native BTM; no RF steering boost, forced scans, escalation or RF-stability gate")
    interactive.add_argument("--adaptive-backhaul", action="store_true",
                             help="model mesh RF and select loop-free RDK OneWifi parents (act only)")
    interactive.add_argument("--model-backhaul", action="store_true",
                             help="model backhaul RF without choosing parents (profiling stimulus only)")
    interactive.add_argument(
        "--max-actions", type=int,
        help="automatic BTM circuit breaker in act mode (default: 100)",
    )
    interactive.add_argument("--base-url", default="http://127.0.0.1:8888")
    interactive.add_argument(
        "--listen", type=_address, default=("127.0.0.1", 8891), metavar="HOST:PORT"
    )
    interactive.add_argument(
        "--output-root", type=Path, default=Path("/tmp/easymesh-room-demo-runs")
    )
    interactive.add_argument(
        "--serve-seconds", type=int, default=0,
        help="exit and restore after N seconds; zero runs until Ctrl-C",
    )
    interactive.add_argument("--lease-seconds", type=int, default=30)
    interactive.add_argument(
        "--lock", type=Path, default=Path("/run/lock/easymesh-room-demo.lock")
    )
    interactive.add_argument(
        "--recovery-file",
        type=Path,
        default=Path("/run/easymesh-room-demo/recovery.json"),
        help="checksummed crash-recovery record",
    )
    recover = commands.add_parser(
        "recover", help="restore the RF baseline retained by an interrupted session"
    )
    recover.add_argument("--socket", default="/run/wmediumd-control.sock")
    recover.add_argument(
        "--recovery-file",
        type=Path,
        default=Path("/run/easymesh-room-demo/recovery.json"),
    )
    recover.add_argument(
        "--lock", type=Path, default=Path("/run/lock/easymesh-room-demo.lock")
    )
    replay = commands.add_parser("replay", help="serve a completed evidence directory offline")
    replay.add_argument("run_directory", type=Path)
    replay.add_argument("--listen", type=_address, default=("127.0.0.1", 8891), metavar="HOST:PORT")
    replay.add_argument("--serve-seconds", type=int, default=0,
                        help="exit after N seconds; zero serves until Ctrl-C")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "check":
            return _check(args)
        if args.command == "replay":
            return _replay(args)
        if args.command == "recover":
            return _recover(args)
        if args.command == "interactive":
            return _interactive(args)
        return _run(args)
    except (OSError, json.JSONDecodeError, ScenarioError, ActuatorError, ValueError) as error:
        print(f"room-demo: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
