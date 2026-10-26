"""CLI: one asyncio process for Slack, jobs and the scheduler."""

import argparse
import asyncio
import json
import logging
from contextlib import suppress
from typing import Any

from katip_ops.applier import ActionApplier
from katip_ops.config import Config, load_config
from katip_ops.core.approvals import Approvals
from katip_ops.core.contract import DataClass
from katip_ops.core.ledger import Ledger
from katip_ops.core.router import Router
from katip_ops.executors.registry import build_executors
from katip_ops.fakes import FakeExecutor, FakeGitHub, FakeOdoo, PrintSlack
from katip_ops.logging_config import configure_logging
from katip_ops.runner import Runner
from katip_ops.runtime_files import JOB_ALIASES, SOPS, Documents
from katip_ops.scheduler import build_scheduler
from katip_ops.slack.bridge import SlackBridge
from katip_ops.slack.commands import Commands
from katip_ops.tools.github import GitHubReader
from katip_ops.tools.odoo.client import OdooClient
from katip_ops.tools.odoo.tools import OdooTools


def check_config(config: Config, documents: Documents) -> None:
    documents.check()
    if set(config.settings.slack.channels) != {"ops", "ops_approvals", "pipeline", "social"}:
        raise ValueError("Dört Slack kanalı da yapılandırılmalı.")
    if not config.settings.github.org:
        raise ValueError("GitHub kuruluşu yapılandırılmalı.")
    if not config.settings.geo.mention_names or not config.settings.geo.citation_domains:
        raise ValueError("GEO ölçüm adları yapılandırılmalı.")
    if not {"file.write", "odoo.create_lead"} <= set(config.settings.policy.allowed_kinds):
        raise ValueError("İşler için dosya ve aday oluşturma önerileri etkin olmalı.")
    from katip_ops.core.contract import Task

    router = Router(
        config.executors,
        {
            name: FakeExecutor(name, entry.provider, entry.model)
            for name, entry in config.executors.executors.items()
            if entry.enabled
        },
    )
    checks: list[tuple[str, DataClass]] = [
        ("content.weekly", "personal"),
        ("pipeline.research", "public"),
        ("growth.summary", "public"),
        ("review.friday", "customer"),
    ]
    for task_type, data_class in checks:
        candidates = router.candidates(
            Task(
                type=task_type, data_class=data_class, source="manual", requested_by="config-check"
            )
        )
        if task_type == "pipeline.research" and not any(e.provider == "claude" for e in candidates):
            raise ValueError("Araştırma rotasında Claude gerekli.")
        if task_type == "review.friday" and "opus" not in candidates[0].model.lower():
            raise ValueError("Cuma değerlendirmesinin ilk yürütücüsü Opus olmalı.")


async def run(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    documents = Documents()
    check_config(config, documents)
    if args.command == "check-config":
        print("Yapılandırma geçerli; ağ bağlantısı kurulmadı.")
        return
    if args.command == "dry-run":
        config = config.model_copy(
            update={"settings": config.settings.model_copy(update={"dry_run": True})}
        )
    config.settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(config.settings.database_path)
    approvals = Approvals(
        config.settings.database_path,
        config.settings.owner_slack_id,
        config.settings.approval_ttl_hours,
    )
    offline = bool(getattr(args, "fake", False))
    executors = (
        {
            name: FakeExecutor(name, entry.provider, entry.model)
            for name, entry in config.executors.executors.items()
            if entry.enabled
        }
        if offline
        else build_executors(config.executors).available
    )
    client = None if offline else OdooClient(config.settings.odoo)
    odoo: Any = FakeOdoo() if client is None else OdooTools(client, config.settings, approvals)
    github = FakeGitHub() if offline else GitHubReader(config.settings.github)
    runner = Runner(
        config, Router(config.executors, executors), ledger, approvals, documents, odoo, github
    )
    commands = Commands(config.settings, ledger, approvals, runner)
    # Fake services cannot apply live actions; no decision listeners in dry-run.
    applier = ActionApplier(config.settings, approvals, odoo, documents)
    bridge = (
        SlackBridge(config.settings, approvals, commands, applier, client=PrintSlack())
        if args.command == "dry-run"
        else SlackBridge.from_settings(config.settings, approvals, commands, applier)
    )
    runner.slack = bridge
    scheduler = None
    worker = None
    try:
        if args.command == "dry-run":
            result = await runner.execute(args.job)
            print(
                json.dumps({"run_id": result.run_id, "status": result.status}, ensure_ascii=False)
            )
        else:
            scheduler = build_scheduler(config.settings, runner)
            worker = asyncio.create_task(runner.worker())
            scheduler.start()
            await bridge.start()
    finally:
        if scheduler is not None and scheduler.running:
            scheduler.shutdown(wait=False)
        if worker is not None:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
        await bridge.close()
        await github.close()
        if client is not None:
            await client.close()
        for executor in executors.values():
            sdk = getattr(executor, "client", None)
            if sdk is not None:
                await sdk.close()
        approvals.close()
        ledger.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Kâtip operasyon motoru")
    parser.add_argument("--config", help="Motor ayarlarının dizini")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("serve", help="Slack köprüsünü ve zamanlayıcıyı başlat")
    dry = subcommands.add_parser("dry-run", help="İşi deneme modunda çalıştır")
    dry.add_argument("job", choices=[*SOPS, *JOB_ALIASES])
    dry.add_argument("--fake", action="store_true", help="Tüm istemcileri çevrimdışı çalıştır")
    subcommands.add_parser("check-config", help="Yapılandırmayı ağsız denetle")
    configure_logging()
    try:
        asyncio.run(run(parser.parse_args()))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logging.getLogger("katip_ops").exception(
            "cli_failed", extra={"error_type": type(exc).__name__}
        )
        parser.exit(1, "İşlem tamamlanamadı; yapılandırmayı ve çalışma kayıtlarını kontrol edin.\n")


if __name__ == "__main__":
    main()
