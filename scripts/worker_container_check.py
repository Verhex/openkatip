"""Check an already-built worker image without secrets, meetings or provider calls.

python scripts/worker_container_check.py --image katip-validation-worker:20261007
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import uuid

from katip.bot.supervisor import docker_command, docker_environment
from katip.config import Settings, get_settings

PROBE = r"""
import asyncio, json, os, subprocess
from pathlib import Path
from playwright.async_api import async_playwright
from katip.bot.runner import launch_browser

async def main():
    assert os.getuid() == 1000
    assert os.statvfs('/').f_flag & os.ST_RDONLY
    assert not Path('/var/run/docker.sock').exists()
    assert not Path('/app/.env').exists()
    assert not Path('/data').exists()
    for key in ('SONIOX_API_KEY', 'ANTHROPIC_API_KEY', 'KATIP_ADMIN_TOKEN',
                'KATIP_WORKER_MANAGER_KEY', 'KATIP_DATABASE_URL'):
        assert not os.environ.get(key), key
    status = Path('/proc/self/status').read_text()
    assert 'NoNewPrivs:\t1' in status
    assert 'CapEff:\t0000000000000000' in status
    assert 'Seccomp:\t2' in status
    limits = {key: Path('/sys/fs/cgroup/' + key).read_text().strip()
              for key in ('memory.max', 'pids.max', 'cpu.max')}
    assert int(limits['memory.max']) == 1024 * 1024 * 1024
    assert int(limits['pids.max']) == 256
    quota, period = map(int, limits['cpu.max'].split())
    assert quota / period == 1
    async with async_playwright() as pw:
        browser = await launch_browser(pw, headless=True)
        page = await browser.new_page()
        await page.set_content('<html><body>Katip worker check</body></html>')
        assert await page.locator('body').inner_text() == 'Katip worker check'
        await browser.close()
        display = subprocess.Popen(
            ['Xvfb', ':99', '-screen', '0', '1280x720x24', '-nolisten', 'tcp'])
        try:
            for _ in range(30):
                ready = subprocess.run(['xdpyinfo', '-display', ':99'],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if ready.returncode == 0:
                    break
                await asyncio.sleep(0.1)
            assert ready.returncode == 0
            browser = await launch_browser(pw, headless=False)
            page = await browser.new_page()
            await page.set_content('<html><body>Headed worker check</body></html>')
            assert await page.locator('body').inner_text() == 'Headed worker check'
            await browser.close()
        finally:
            display.terminate()
            display.wait(timeout=5)
    print(json.dumps({'ok': True, 'sandbox': 'on', 'uid': os.getuid(),
                      'read_only': True, 'headed_sandbox': True, 'limits': limits}))

asyncio.run(main())
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--seccomp", default="docker/worker-seccomp.json")
    parser.add_argument("--trace-log")
    args = parser.parse_args()
    Settings.model_config["env_file"] = None
    get_settings.cache_clear()
    settings = get_settings()
    settings.bot_sandbox = "on"
    settings.bot_site_isolation = True
    settings.bot_image = args.image
    settings.bot_network = "none"
    settings.bot_memory_mb = 1024
    settings.bot_cpus = 1
    name = "katip-validation-" + uuid.uuid4().hex
    audio_name = name + "-audio"
    env = docker_environment("validation-token-no-provider-access-0000000000")
    probe_args = ["-c", PROBE]
    if args.trace_log:
        probe_args = ["-f", "python", *probe_args]
    command = docker_command(name, probe_args, env)
    command[command.index("--security-opt=seccomp=docker/worker-seccomp.json")] = (
        "--security-opt=seccomp=" + args.seccomp
    )
    pos = command.index(args.image)
    command[pos:pos] = ["--entrypoint", "strace" if args.trace_log else "python"]
    try:
        subprocess.run(
            command, env={**os.environ, **env}, check=True, capture_output=True, timeout=30
        )
        result = subprocess.run(
            ["docker", "start", "--attach", name], capture_output=True, text=True, timeout=90
        )
        if args.trace_log:
            from pathlib import Path

            Path(args.trace_log).write_text(result.stderr, encoding="utf-8")
        if result.returncode:
            raise RuntimeError(result.stderr[-6000:] + result.stdout[-2000:])
        # Logs may precede the single result record.
        outcome = json.loads(result.stdout.strip().splitlines()[-1])
        # Exercise the real Xvfb/PulseAudio entrypoint without joining a meeting.
        audio_command = docker_command(audio_name, ["--help"], env)
        audio_command[audio_command.index("--security-opt=seccomp=docker/worker-seccomp.json")] = (
            "--security-opt=seccomp=" + args.seccomp
        )
        subprocess.run(
            audio_command, env={**os.environ, **env}, check=True, capture_output=True, timeout=30
        )
        audio = subprocess.run(
            ["docker", "start", "--attach", audio_name], capture_output=True, text=True, timeout=90
        )
        if audio.returncode or "usage:" not in audio.stdout:
            raise RuntimeError(audio.stderr[-6000:] + audio.stdout[-2000:])
        outcome["audio_display_entrypoint"] = True
        print(json.dumps(outcome))
    finally:
        subprocess.run(["docker", "rm", "--force", name], capture_output=True, timeout=20)
        subprocess.run(["docker", "rm", "--force", audio_name], capture_output=True, timeout=20)


if __name__ == "__main__":
    main()
