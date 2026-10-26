"""Apply approved local drafts or delegate typed Odoo actions; never publish."""

import json
from typing import Any

from pydantic import JsonValue

from katip_ops.config import Settings
from katip_ops.core import policy
from katip_ops.core.approvals import Approvals
from katip_ops.core.contract import ProposedAction
from katip_ops.jobs.content import Draft, length_problems, linkedin_limits
from katip_ops.runtime_files import Documents, WorkFiles, front_matter


class ActionApplier:
    def __init__(
        self, settings: Settings, approvals: Approvals, odoo: Any, documents: Documents
    ) -> None:
        self.settings, self.approvals, self.odoo = settings, approvals, odoo
        self.documents = documents
        self.files = WorkFiles(settings.ops_dir)

    async def apply(self, action: ProposedAction, *, approval_id: str) -> dict[str, JsonValue]:
        action = ProposedAction.model_validate_json(action.model_dump_json())
        approval = self.approvals.get(approval_id)
        if (
            approval.status != "approved"
            or approval.digest != action.digest
            or self.approvals.clock() >= approval.expires_at
        ):
            raise PermissionError("İşlem onayla eşleşmiyor veya onay geçersiz.")
        policy.check_action(
            action, self.settings.policy.allowed_kinds, self.settings.policy.approval_rules
        )
        values = action.payload.get("values")
        if not isinstance(values, dict):
            raise ValueError("İşlem alanları doğrulanamadı.")
        text = json.dumps(values, ensure_ascii=False)
        if policy.check_pii(
            text, self.settings.policy.personal_email_domains
        ) or policy.check_banned_terms(
            text, [*self.settings.policy.banned_terms, *self.settings.policy.customer_names]
        ):
            raise policy.PolicyRejection("İşlem metni politika denetiminden geçmedi.")
        if action.kind.startswith("odoo."):
            return await self.odoo.apply(action, approval_id=approval_id)  # type: ignore[no-any-return]
        if action.kind != "file.write" or not action.target.startswith(
            ("content/approved/", "content/ideas/")
        ):
            raise policy.PolicyRejection("Bu yerel işleme izin verilmiyor.")
        body = values.get("body")
        if not isinstance(body, str) or not body.strip():
            raise ValueError("Taslak metni boş olamaz.")
        if not policy.check_claims(body, self.documents.read("claims.md")).allowed:
            raise policy.PolicyRejection("NEEDS-CLAIM: İddia denetimi başarısız.")
        self.files.path(action.target)
        if action.target.startswith("content/approved/"):
            draft = Draft.model_validate(
                {
                    "platform": action.payload["platform"],
                    "audience": "personal",
                    "theme": "Onay",
                    "sources": [],
                    "evidenced": True,
                    "text": body,
                    "thread": body.split("\n\n") if action.payload.get("thread") else [],
                }
            )
            if length_problems(draft, linkedin_limits(self.documents.read("VOICE.md"))):
                raise policy.PolicyRejection("Düzenlenen taslak uzunluk sınırını aşıyor.")
        # dry_run guards external systems (Odoo). Local draft files are internal and always written.
        metadata = action.payload.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError("Taslak üst bilgisi doğrulanamadı.")
        if action.target.startswith("content/approved/"):
            metadata = {
                **metadata,
                "claims": list(
                    policy.check_claims(body, self.documents.read("claims.md")).claims_used
                ),
            }
        self.files.write(action.target, front_matter(dict(metadata), body))
        source = action.payload.get("draft")
        if isinstance(source, str) and source.startswith("content/drafts/"):
            path = self.files.path(source)
            if path.exists():
                original = path.read_text(encoding="utf-8")
                if approval.edited:
                    import difflib

                    feedback = self.files.path("content/voice-feedback.md")
                    previous = feedback.read_text(encoding="utf-8") if feedback.exists() else ""
                    diff = "\n".join(difflib.unified_diff(original.splitlines(), body.splitlines()))
                    self.files.write("content/voice-feedback.md", previous + "\n" + diff + "\n")
                path.unlink()
        return {"path": action.target, "dry_run": False}
