"""Conservative, deterministic policy heuristics; no network or model calls.

Claims are checked per sentence/line. A public citation permits that sentence,
but semantic entailment is left to editorial review. Capability keyword coverage
is intentionally small; this is a draft gate, not a factual verification model.
"""

import html
import re
from collections.abc import Sequence
from dataclasses import dataclass

from katip_ops.config import ApprovalRules
from katip_ops.core.contract import ProposedAction


class PolicyRejection(ValueError):
    """A scope rejection must never trigger provider fallback."""


@dataclass(frozen=True)
class Claim:
    id: str
    text: str
    status: str


@dataclass(frozen=True)
class ClaimsReport:
    needs_claim: tuple[str, ...]
    rejected_claims: tuple[str, ...]
    claims_used: tuple[str, ...]

    @property
    def allowed(self) -> bool:
        return not self.needs_claim and not self.rejected_claims


def parse_claims(markdown: str) -> dict[str, Claim]:
    """Read ID | claim | evidence | status | last checked tables, ignoring prose."""
    claims = {}
    for line in markdown.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) >= 4 and re.fullmatch(r"C-\d{3,}", cells[0]):
            claims[cells[0]] = Claim(cells[0], cells[1], cells[3].strip("`*").lower())
    return claims


_CLAIM_ID = re.compile(r"\bC-\d{3,}\b")
_CAPABILITY = re.compile(
    r"\b(?:can|supports?|provides?|automates?|integrates?|certified|capabilit\w*|"
    r"builds?|offers?|enables?|delivers?|reduces?|improves?|"
    r"geliştir\w*|sağla\w*|destekl\w*|entegre\w*|otomatik\w*|"
    r"dönüştür\w*|çevir\w*|kaydeder|platform\w*|asistan\w*|"
    r"partner\w*|sertifika\w*|müşteri\w*|sunar|sunuyor|azalt\w*|artır\w*|yönet\w*)\b",
    re.IGNORECASE,
)


def check_claims(text: str, claims: str | dict[str, Claim]) -> ClaimsReport:
    table = parse_claims(claims) if isinstance(claims, str) else claims
    cited = set(_CLAIM_ID.findall(text))
    rejected = tuple(sorted(cid for cid in cited if cid in table and table[cid].status != "public"))
    flagged = []
    # Do not split decimal numbers on '.', or citations from their preceding sentence.
    for sentence in re.split(r"(?<=[.!?])\s+(?=[^\[])|\n+", text):
        ids = set(_CLAIM_ID.findall(sentence))
        without_ids = _CLAIM_ID.sub("", sentence)
        risky = bool(re.search(r"\d|%", without_ids) or _CAPABILITY.search(without_ids))
        backed = any(cid in table and table[cid].status == "public" for cid in ids)
        if (risky and not backed) or any(cid not in table for cid in ids):
            flagged.append(f"NEEDS-CLAIM: {sentence.strip()}")
    return ClaimsReport(tuple(flagged), rejected, tuple(sorted(cited)))


def check_banned_terms(text: str, rules: Sequence[str]) -> tuple[str, ...]:
    return tuple(
        term
        for term in rules
        if term
        and re.search(
            rf"(?<!\w){re.escape(term)}(?!\w)",
            text,
            re.IGNORECASE,
        )
    )


_PERSONAL_DOMAINS = {
    "gmail.com",
    "googlemail.com",
    "hotmail.com",
    "hotmail.com.tr",
    "live.com",
    "yahoo.com",
    "yahoo.com.tr",
    "outlook.com",
    "outlook.com.tr",
    "icloud.com",
    "yandex.com",
    "yandex.com.tr",
    "yandex.ru",
}
_EMAIL = re.compile(r"[\w.!#$%&'*+/=?^`{|}~-]+@([\w.-]+\.[a-z]{2,})", re.IGNORECASE)
_PHONE = re.compile(
    r"(?<!\w)(?:(?:\+|00)\d(?:[\s().-]*\d){6,14}|"
    r"0[\s(.-]*5(?:[\s().-]*\d){9})(?!\w)",
)


def check_pii(text: str, personal_domains: Sequence[str] = ()) -> tuple[str, ...]:
    """Return Turkish category labels, never copy the sensitive value into errors."""
    domains = _PERSONAL_DOMAINS | {domain.lower() for domain in personal_domains}
    problems = []
    if any(match.group(1).lower() in domains for match in _EMAIL.finditer(text)):
        problems.append("Kişisel e-posta adresi içeriyor.")
    if _PHONE.search(text):
        problems.append("Telefon numarası içeriyor.")
    return tuple(problems)


def check_action(
    action: ProposedAction,
    allowed_kinds: Sequence[str],
    approval_rules: ApprovalRules,
) -> bool:
    """Return approval requirement; configuration may tighten mandatory rules only."""
    if action.kind not in allowed_kinds:
        raise PolicyRejection("Bu işlem türüne izin verilmiyor.")
    boundary = f"{action.kind} {action.target}".lower()
    mandatory = re.search(
        r"delete|unlink|account|invoice|payment|setting|config|res\.users|ir\.|"
        r"outbound|external|publish|send|contact|spend|webhook|slack[._]|"
        r"post|upload|notify|export|email|sms|https?://|smtp://|s3://|ftp://",
        boundary,
    )
    if mandatory or action.kind in approval_rules.always_approval:
        return True
    if action.requires_approval:
        return True
    if approval_rules.stage == 2 and action.kind in approval_rules.routine_kinds:
        return False
    return approval_rules.stage1.get(action.kind, True)


def wrap_untrusted(content: str, source: str) -> str:
    """The caller must still disable tools when summarizing untrusted content."""
    escaped = re.sub(r"</\s*untrusted\b", "&lt;/untrusted", content, flags=re.IGNORECASE)
    return f'<untrusted source="{html.escape(source, quote=True)}">\n{escaped}\n</untrusted>'
