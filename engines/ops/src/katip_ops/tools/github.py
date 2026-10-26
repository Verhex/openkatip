"""Read-only GitHub collection. No search API or arbitrary URL fetching."""

import logging
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

from katip_ops.config import GitHubSettings
from katip_ops.secrets import read_secret

logger = logging.getLogger(__name__)
# Empty repositories answer 409 and repositories with pull requests unavailable answer 404.
_EMPTY_STATUSES = {404, 409}


class GitHubReader:
    def __init__(self, settings: GitHubSettings, *, client: Any = None) -> None:
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=30, follow_redirects=False)

    async def close(self) -> None:
        await self.client.aclose()

    async def _get(self, endpoint: str, *, empty_ok: bool = False, **params: Any) -> Any:
        response = await self.client.get(
            f"https://api.github.com{endpoint}",
            params=params,
            headers={
                "Authorization": f"Bearer {read_secret(self.settings.token_file)}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        if empty_ok and response.status_code in _EMPTY_STATUSES:
            return []
        if not response.is_success:
            raise RuntimeError(f"GitHub okuması tamamlanamadı (HTTP {response.status_code}).")
        return response.json()

    async def _pages(
        self, endpoint: str, *, empty_ok: bool = False, **params: Any
    ) -> list[dict[str, Any]]:
        rows = []
        for page in range(1, 1001):
            batch = await self._get(endpoint, empty_ok=empty_ok, per_page=100, page=page, **params)
            if not isinstance(batch, list):
                raise ValueError("GitHub yanıtı liste olmalı.")
            rows.extend(batch)
            if len(batch) < 100:
                return rows
        raise ValueError("GitHub sayfalama sınırı aşıldı; eksik veri kullanılmadı.")

    async def collect(self, since: datetime, excluded: list[str]) -> list[dict[str, Any]]:
        org = self.settings.org
        if not org or any(
            char not in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in org.lower()
        ):
            raise ValueError("GitHub kuruluşu yapılandırılmalı.")
        skip = {item.casefold() for item in excluded}
        repositories = await self._pages(f"/orgs/{quote(org, safe='')}/repos", type="all")
        result: list[dict[str, Any]] = []
        for repository in repositories:
            name = repository["name"]
            full = f"{org}/{name}"
            if name.casefold() in skip or full.casefold() in skip:
                continue
            base = f"/repos/{quote(org, safe='')}/{quote(name, safe='')}"
            try:
                result.extend(await self._repository(base, since))
            except RuntimeError:
                # One unreadable repository must not cancel the whole weekly pack.
                logger.warning("github_repository_skipped", extra={"kind": name})
        return result

    async def _repository(self, base: str, since: datetime) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        commits = await self._pages(f"{base}/commits", empty_ok=True, since=since.isoformat())
        for commit in commits:
            result.append({"source": commit["html_url"], "text": commit["commit"]["message"]})
            # DECISIONS.md may live at any depth. Inspect patches from this
            # window's commits instead of restricting GitHub's path filter.
            for page in range(1, 31):
                detail = await self._get(
                    f"{base}/commits/{quote(commit['sha'], safe='')}", per_page=100, page=page
                )
                files = detail.get("files", [])
                for file in files:
                    filename = file["filename"]
                    if filename == "DECISIONS.md" or filename.endswith("/DECISIONS.md"):
                        if file.get("patch"):
                            result.append(
                                {
                                    "source": commit["html_url"] + "#" + filename,
                                    "text": file["patch"],
                                }
                            )
                        else:
                            raise ValueError(
                                "Karar dosyası diff'i alınamadı; eksik veri kullanılmadı."
                            )
                if len(files) < 100:
                    break
            else:
                raise ValueError("Commit dosya sınırı aşıldı; eksik veri kullanılmadı.")
        # PRs sorted by update allow stopping once the seven-day window is exhausted.
        for page in range(1, 1001):
            pulls = await self._get(
                f"{base}/pulls",
                empty_ok=True,
                state="closed",
                sort="updated",
                direction="desc",
                per_page=100,
                page=page,
            )
            for pull in pulls:
                if pull.get("merged_at") and datetime.fromisoformat(pull["merged_at"]) >= since:
                    result.append(
                        {
                            "source": pull["html_url"],
                            "text": f"{pull['title']}\n{pull.get('body') or ''}",
                        }
                    )
            if len(pulls) < 100 or datetime.fromisoformat(pulls[-1]["updated_at"]) < since:
                break
        else:
            raise ValueError("GitHub PR sayfalama sınırı aşıldı.")
        return result
