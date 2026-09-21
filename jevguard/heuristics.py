"""Free, local, sub-millisecond checks that run before any Jev call.

Obvious attacks never cost a Jev call, and when Jev is unavailable these are what the guard
falls back on. Every rule has a severity:

- ``block``  - unambiguous; the guard blocks without asking Jev
- ``high`` / ``medium`` / ``low`` - suspicious; makes the scan "uncertain" so Jev decides
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable

from jevguard.types import Stage

SEVERITY_SCORE = {"block": 1.0, "high": 0.75, "medium": 0.5, "low": 0.3}


@dataclass(frozen=True)
class Rule:
    check: str
    severity: str
    pattern: re.Pattern[str]
    detail: str
    stages: frozenset[Stage]


@dataclass
class Hit:
    check: str
    severity: str
    detail: str

    @property
    def score(self) -> float:
        return SEVERITY_SCORE[self.severity]


@dataclass
class LocalScan:
    hits: list[Hit] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        if any(h.severity == "block" for h in self.hits):
            return "block"
        if any(h.severity in ("high", "medium") for h in self.hits):
            return "uncertain"
        return "allow"

    @property
    def score(self) -> float:
        return max((h.score for h in self.hits), default=0.0)


@dataclass
class SensitiveMatch:
    kind: str      # email, phone, credit_card, aws_key, ...
    category: str  # "pii" | "secret"
    start: int
    end: int


ALL = frozenset(Stage)
PROMPTS = frozenset({Stage.INPUT, Stage.TOOL_RESULT, Stage.RETRIEVAL})
ACTIONS = frozenset({Stage.TOOL_CALL})
OUTPUTS = frozenset({Stage.OUTPUT})
_I = re.IGNORECASE


def _r(check: str, severity: str, pattern: str, detail: str, stages: frozenset[Stage] = ALL) -> Rule:
    return Rule(check, severity, re.compile(pattern, _I | re.DOTALL), detail, stages)


RULES: list[Rule] = [
    # --- prompt injection / instruction override ------------------------------------------
    _r("prompt_injection", "block",
       r"\b(ignore|disregard|forget|override)\b.{0,30}\b(all|any|the|your|previous|prior|above|earlier)\b.{0,20}"
       r"\b(instructions?|rules|prompts?|directives|guidelines)\b",
       "instruction override phrase", PROMPTS),
    _r("prompt_injection", "high", r"\b(new|updated|real|actual)\s+(system\s+)?instructions?\s*[:\-]", "injected instruction header", PROMPTS),
    _r("prompt_injection", "high", r"(<\|im_start\|>|<\|system\|>|\[/?INST\]|<<SYS>>|^\s*#{2,}\s*system\b)", "chat-template control tokens", PROMPTS),
    _r("prompt_injection", "medium", r"\byou\s+are\s+now\b|\bfrom\s+now\s+on\s*,?\s+you\b", "persona reassignment", PROMPTS),
    _r("prompt_injection", "high",
       r"\b(assistant|ai|agent|model|llm)\b.{0,40}\b(must|should|shall)\b.{0,40}\b(call|invoke|run|execute|send|email|forward)\b",
       "instructions addressed to the AI", frozenset({Stage.TOOL_RESULT, Stage.RETRIEVAL})),
    # --- jailbreaks ---------------------------------------------------------------------------
    _r("jailbreak", "block", r"\b(DAN|do anything now)\b.{0,60}\b(mode|jailbreak|prompt)\b|\bjailbreak(ed)?\s+mode\b", "known jailbreak persona", PROMPTS),
    _r("jailbreak", "block", r"\byou\s+are\s+(now\s+)?(DAN|STAN|DUDE|AIM|EvilBot)\b", "known jailbreak persona", PROMPTS),
    _r("jailbreak", "high", r"\b(developer|god|unrestricted|unfiltered|evil)\s+mode\b", "unrestricted-mode request", PROMPTS),
    _r("jailbreak", "medium", r"\bwithout\s+(any\s+)?(restrictions|filters|limits|censorship|safety)\b", "asks to drop safety limits", PROMPTS),
    _r("jailbreak", "medium", r"\b(hypothetically|in a fictional world|for a novel|pretend)\b.{0,80}\b(how to|steps|instructions)\b", "fictional framing for instructions", PROMPTS),
    # --- secret / system prompt extraction ---------------------------------------------------
    _r("secret_extraction", "high",
       r"\b(reveal|print|show|repeat|output|tell me|what (is|are))\b.{0,30}\b(your|the)\s+(system\s+prompt|initial\s+instructions|hidden\s+(prompt|instructions)|api\s+keys?|credentials|password)",
       "asks for system prompt or secrets", PROMPTS),
    _r("secret_extraction", "medium", r"\brepeat\s+(everything|the\s+text)\s+above\b", "repeat-the-above extraction", PROMPTS),
    # --- obfuscation / smuggling ---------------------------------------------------------------
    _r("obfuscation", "block", r"[\U000E0000-\U000E007F]", "unicode tag characters (ASCII smuggling)"),
    _r("obfuscation", "medium", r"[​-‏⁠-⁤﻿]{3,}", "zero-width character run"),
    _r("obfuscation", "medium", r"\b(base64|rot13|hex)[- ]?(decode|encoded)\b.{0,40}\b(then|and)\s+(follow|execute|run|do)\b", "decode-then-execute", PROMPTS),
    _r("obfuscation", "low", r"(?:[A-Za-z0-9+/]{4}){40,}={0,2}", "long base64 blob"),
    # --- dangerous tool actions -----------------------------------------------------------------
    _r("destructive_command", "block", r"\brm\s+-[a-z]*r[a-z]*f?[a-z]*\s+(--no-preserve-root\s+)?(/|~|\*|\$HOME)(\s|$|\")", "recursive delete of root/home", ACTIONS),
    _r("destructive_command", "block", r"\b(mkfs(\.\w+)?|dd\s+if=.{0,40}of=/dev/(sd|nvme|disk))|:\(\)\s*\{\s*:\|:&\s*\};:", "disk wipe / fork bomb", ACTIONS),
    _r("destructive_command", "block", r"(bash\s+-i\s*>&\s*/dev/tcp/|\bnc\b.{0,20}\s-e\s|/bin/sh\s+-i|socat\s+.*exec:)", "reverse shell", ACTIONS),
    _r("destructive_command", "high", r"\b(curl|wget)\b[^|;&]{0,200}\|\s*(sudo\s+)?(ba|z)?sh\b", "pipe remote script to shell", ACTIONS),
    _r("destructive_command", "high", r"\b(drop\s+(table|database|schema)|truncate\s+table)\b", "destructive SQL", ACTIONS),
    _r("destructive_command", "high", r"\bdelete\s+from\s+\w+\s*(;|$|\")", "DELETE without WHERE", ACTIONS),
    _r("destructive_command", "medium", r"\b(chmod\s+-R\s+777|chown\s+-R|shutdown|reboot|kill\s+-9\s+1\b|git\s+push\s+(-f|--force))", "high-impact system command", ACTIONS),
    _r("credential_access", "high", r"(~|\$HOME|/home/\w+|/root)/\.(ssh|aws|kube|docker|gnupg)\b|/etc/(shadow|passwd|sudoers)|\.env\b|id_rsa", "reads credential files", ACTIONS),
    _r("credential_access", "high", r"\*?\.(pem|key|p12|pfx|keystore|jks)\b|\bcredentials?\.(json|ya?ml|ini)\b", "reads key or credential files", ACTIONS),
    _r("credential_access", "high", r"-exec\s+(cat|cp|curl|scp)\b|\bfind\b[^|;]{0,80}-name\s+['\"]?\*\.(pem|key|env)", "searches the disk for secrets", ACTIONS),
    # --- exfiltration channels -----------------------------------------------------------------------
    _r("exfiltration", "high", r"https?://[^\s\"')]*(webhook\.site|requestbin|pipedream\.net|ngrok(-free)?\.(io|app)|pastebin\.com|burpcollaborator|interact\.sh|oast\.)", "known exfiltration endpoint", ACTIONS | OUTPUTS | PROMPTS),
    _r("exfiltration", "block", r"!\[[^\]]*\]\(https?://[^)\s]+\?[^)\s]*=[^)\s]{16,}\)", "markdown image carrying data in URL", OUTPUTS | PROMPTS),
    _r("exfiltration", "medium", r"https?://\d{1,3}(\.\d{1,3}){3}(:\d+)?/", "raw IP URL", ACTIONS | OUTPUTS),
    # --- output issues -------------------------------------------------------------------------------
    _r("system_prompt_leak", "medium", r"\b(my|the)\s+(system\s+prompt|instructions\s+(i\s+was|i've\s+been)\s+given)\s+(is|are|say)\b", "discusses its own system prompt", OUTPUTS),
]

# --- PII & secrets (used for redaction as well as scoring) -------------------------------------------
SENSITIVE_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    ("private_key", "secret", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----")),
    ("aws_access_key", "secret", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", "secret", re.compile(r"\b(ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}\b")),
    ("openai_key", "secret", re.compile(r"\bsk-(proj-|ant-)?[A-Za-z0-9_\-]{20,}\b")),
    ("slack_token", "secret", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("google_api_key", "secret", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("jwt", "secret", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("password_assignment", "secret", re.compile(r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|token)\s*[:=]\s*[\"']?[^\s\"']{6,}")),
    ("email", "pii", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("credit_card", "pii", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("us_ssn", "pii", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("india_pan", "pii", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")),
    ("india_aadhaar", "pii", re.compile(r"\b[2-9]\d{3}\s?\d{4}\s?\d{4}\b")),
    # Covers +1 (415) 555-0100, +44 20 7946 0958 and +91 98765 43210; needs >= 10 digits (checked below).
    ("phone", "pii", re.compile(r"(?<![\w.])(?:\+\d{1,3}[\s.-]?)?(?:\(?\d{2,5}\)?[\s.-]?){1,3}\d{3,5}(?![\w.])")),
    ("ip_address", "pii", re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")),
]


def _luhn_ok(number: str) -> bool:
    digits = [int(c) for c in number if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def normalize(text: str) -> str:
    """NFKC-normalise and drop zero-width characters so look-alike tricks don't evade the rules."""
    text = unicodedata.normalize("NFKC", text)
    return re.sub(r"[​-‏⁠-⁤﻿]", "", text)


def find_sensitive(text: str, categories: Iterable[str] = ("pii", "secret")) -> list[SensitiveMatch]:
    wanted = set(categories)
    found: list[SensitiveMatch] = []
    taken: list[tuple[int, int]] = []
    for kind, category, pattern in SENSITIVE_PATTERNS:
        if category not in wanted:
            continue
        for m in pattern.finditer(text):
            if kind == "credit_card" and not _luhn_ok(m.group()):
                continue
            if kind == "phone" and sum(c.isdigit() for c in m.group()) < 10:
                continue
            if any(m.start() < e and s < m.end() for s, e in taken):
                continue  # earlier (more specific) pattern already covers this span
            taken.append((m.start(), m.end()))
            found.append(SensitiveMatch(kind, category, m.start(), m.end()))
    return sorted(found, key=lambda s: s.start)


def redact(text: str, matches: list[SensitiveMatch] | None = None) -> str:
    matches = find_sensitive(text) if matches is None else matches
    out, last = [], 0
    for m in sorted(matches, key=lambda s: s.start):
        out.append(text[last:m.start])
        out.append(f"[REDACTED:{m.kind}]")
        last = m.end
    out.append(text[last:])
    return "".join(out)


def scan_local(text: str, stage: Stage = Stage.INPUT, canaries: Iterable[str] = ()) -> LocalScan:
    """Run every rule that applies to ``stage``. Pure function, no I/O."""
    scan = LocalScan()
    raw = text
    text = normalize(text)
    for rule in RULES:
        if stage not in rule.stages:
            continue
        # Smuggling rules must see the raw text; normalisation strips what they look for.
        target = raw if rule.check == "obfuscation" else text
        if rule.pattern.search(target):
            scan.hits.append(Hit(rule.check, rule.severity, rule.detail))
    for canary in canaries:
        if canary and canary in text and stage in (Stage.OUTPUT, Stage.TOOL_CALL):
            scan.hits.append(Hit("canary_leak", "block", "canary token left the trust boundary"))
    return scan
