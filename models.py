"""Pydantic data models for the Automated Conservation Grant & Subsidy Hunter."""

from __future__ import annotations

import html
import re
from typing import Optional
from pydantic import BaseModel, Field, field_validator


def _clean_text(value: str) -> str:
    """Unescape HTML entities and collapse redundant whitespace."""
    unescaped = html.unescape(value or "")
    return re.sub(r"\s+", " ", unescaped).strip()


class NotificationSettings(BaseModel):
    """Non-secret admin notification & backup settings (secrets are read ONLY from environment variables).

    Environment variable overrides (take precedence over config.json):
      ADMIN_NOTIFICATION_EMAIL, NOTIFICATION_FROM_EMAIL, NOTIFICATION_PROVIDER (resend | smtp | none),
      RESEND_API_KEY, SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, SMTP_USE_TLS,
      BACKUP_WEBHOOK_URL, BACKUP_WEBHOOK_SECRET
    """

    provider: str = Field(default="auto", description="auto | resend | smtp | none")
    admin_email: str = Field(default="", description="Admin inbox that receives every signed submission")
    from_email: str = Field(default="", description="Verified sender address (Resend domain or SMTP mailbox)")
    smtp_host: str = Field(default="")
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_use_tls: bool = Field(default=True)
    backup_webhook_url: str = Field(default="", description="Persistent JSON backup endpoint (POST)")
    notify_on_preview: bool = Field(default=True, description="Also back up Stage-A (unsigned) lead previews")
    timeout_seconds: float = Field(default=12.0, gt=0, le=60)


class MatcherConfig(BaseModel):
    """Configuration controlling matcher filters, fee structure, and firm signature."""

    state: str = Field(default="CA", description="Target US state filter (empty string for any)")
    county: str | list[str] = Field(
        default_factory=list,
        description="Optional county name or list of counties (empty for all counties)",
    )
    min_acreage: float = Field(default=0.0, ge=0.0, description="Minimum landowner parcel acreage")
    max_acreage: float = Field(default=100000.0, gt=0.0, description="Maximum landowner parcel acreage")
    min_grant_amount: int = Field(default=0, ge=0, description="Minimum grant award/program dollar amount")
    min_probability_score: int = Field(
        default=75,
        ge=0,
        le=100,
        description="Minimum Grant Win Probability Score (0-100) required to retain a Tier-1 candidate",
    )
    top_k_per_profile: int = Field(default=3, ge=1, le=20, description="Max matches generated per landowner")
    success_fee_percent: float = Field(default=7.0, gt=0.0, le=50.0, description="Contingent success fee %")
    firm_name: str = Field(default="ZYS GoldFields PRO LLC")
    firm_subtitle: str = Field(default="Conservation & Agricultural Grant Advisory")
    public_intake_url: str = Field(
        default="https://zys-goldfields-intake.onrender.com",
        description="Public HTTPS URL for the Zero-Call Digital Client Intake Portal",
    )
    firm_postal_address: str = Field(
        default="",
        description="Valid physical postal address required by CAN-SPAM (15 U.S.C. § 7704(a)(5)) in every email",
    )
    unsubscribe_email: str = Field(
        default="",
        description="Mailbox that receives opt-out requests (honored within 10 business days)",
    )
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)

    def normalized_counties(self) -> set[str]:
        if isinstance(self.county, str):
            val = self.county.strip().lower()
            return {val} if val else set()
        return {c.strip().lower() for c in self.county if c and c.strip()}

    def fee_label(self) -> str:
        if float(self.success_fee_percent).is_integer():
            return f"{int(self.success_fee_percent)}%"
        return f"{self.success_fee_percent:g}%"

    def fee_name(self) -> str:
        """Client-facing name of the deferred, award-contingent fee."""
        return (
            f"{self.fee_label()} Deferred Administrative Origination & Packaging Fee "
            "(100% Waived if Grant is Not Awarded)"
        )


def eligibility_tier_label(breakdown: "ProbabilityScoreBreakdown | None", score: int = 0) -> str:
    """Translate the internal 0-100 score into client-facing statutory eligibility language.

    A core criterion counts as "met" when it earns at least 60% of its pillar maximum.
    """
    if breakdown is None:
        return "Tier-1 Statutory Eligibility Match" if score >= 75 else "Statutory Eligibility Review Pending"
    pillars = (
        (breakdown.acreage_threshold_pts, 30),
        (breakdown.entity_eligibility_pts, 25),
        (breakdown.ecological_priority_pts, 25),
        (breakdown.scale_comatch_pts, 20),
    )
    met = sum(1 for pts, mx in pillars if pts >= 0.6 * mx)
    tier = "Tier-1" if (breakdown.total_score >= 75 and not breakdown.hard_disqualified) else "Tier-2"
    return f"{tier} Statutory Eligibility Match ({met}/4 Core Criteria Met)"


class GrantRecord(BaseModel):
    """Validated conservation grant or subsidy opportunity stored in grants.db."""

    id: str = Field(..., min_length=1, description="Unique grant identifier")
    title: str = Field(..., min_length=2, description="Official grant title")
    agency: str = Field(..., min_length=2, description="Administering federal or state agency")
    funding_amount: str = Field(..., min_length=2, description="Available award range or program funding")
    deadline: str = Field(..., min_length=2, description="Application closing date or solicitation schedule")
    eligibility_text: str = Field(..., min_length=5, description="Applicant eligibility and program scope")
    url: str = Field(..., min_length=8, description="Canonical opportunity URL")

    @field_validator("id", "title", "agency", "funding_amount", "deadline", "eligibility_text", "url")
    @classmethod
    def normalize_strings(cls, v: str) -> str:
        cleaned = _clean_text(str(v))
        if not cleaned:
            raise ValueError("Field cannot be empty after normalization.")
        return cleaned

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError(f"Invalid URL scheme: {v}")
        return v


class LandownerProfile(BaseModel):
    """Validated landowner property profile stored in grants.db."""

    id: Optional[int] = Field(default=None, description="Primary key ID in landowner_profiles")
    name: str = Field(..., min_length=2, description="Landowner or property name")
    state: str = Field(..., min_length=2, max_length=32, description="US State code or name")
    county: str = Field(..., min_length=2, description="County name")
    acreage: float = Field(..., gt=0, description="Total parcel size in acres")
    land_type: str = Field(..., min_length=2, description="Primary ecological or agricultural land type")
    features: str = Field(..., min_length=5, description="Resource conditions, goals, and conservation features")

    @field_validator("name", "state", "county", "land_type", "features")
    @classmethod
    def normalize_profile_fields(cls, v: str) -> str:
        cleaned = _clean_text(str(v))
        if not cleaned:
            raise ValueError("Profile text field cannot be empty.")
        return cleaned


class ProbabilityScoreBreakdown(BaseModel):
    """4-pillar weighted Grant Win Probability Score (0-100) for statutory & competitive evaluation."""

    total_score: int = Field(..., ge=0, le=100, description="Composite Grant Win Probability Score (0-100)")
    acreage_threshold_pts: int = Field(..., ge=0, le=30, description="Criterion A: Acreage & Threshold Fit (0-30)")
    entity_eligibility_pts: int = Field(..., ge=0, le=25, description="Criterion B: Entity & Tax-Exempt Eligibility (0-25)")
    ecological_priority_pts: int = Field(..., ge=0, le=25, description="Criterion C: Ecological & Geographic Priority Fit (0-25)")
    scale_comatch_pts: int = Field(..., ge=0, le=20, description="Criterion D: Funding-to-Scale & Co-Match Feasibility (0-20)")
    hard_disqualified: bool = Field(default=False, description="True if candidate violates a hard statutory limit")
    disqualification_reason: str = Field(default="", description="Reason if hard_disqualified is True")
    score_rationale: str = Field(..., min_length=5, description="1-sentence rationale explaining win probability")

    def summary_tag(self) -> str:
        return (
            f"Acreage {self.acreage_threshold_pts}/30 | "
            f"Entity {self.entity_eligibility_pts}/25 | "
            f"Eco/Geo {self.ecological_priority_pts}/25 | "
            f"Co-Match {self.scale_comatch_pts}/20"
        )


class GrantMatch(BaseModel):
    """Scored pairing between a landowner/prospect profile and an active conservation grant."""

    profile: LandownerProfile
    grant: GrantRecord
    score: int = Field(..., ge=0, le=100, description="Grant Win Probability Score (0-100)")
    probability_breakdown: Optional[ProbabilityScoreBreakdown] = Field(default=None)
    score_rationale: str = Field(default="", description="1-sentence win probability rationale")
    matched_terms: list[str] = Field(default_factory=list)
    application_pathway: str
    rationale: list[str] = Field(default_factory=list)
    recommended_project_focus: str
    estimated_award_dollars: int = Field(default=100000, ge=0)
    fee_example: str
    contact_email: str = Field(default="")
    organization_category: str = Field(default="")
    ein: str = Field(default="")
    website: str = Field(default="")
    personalized_intake_url: str = Field(default="")
    outreach_segment: str = Field(default="Direct_Client")


class ESignatureAuditRecord(BaseModel):
    """Cryptographic legal audit trail record for California UETA / Federal ESIGN Act execution."""

    id: Optional[int] = Field(default=None)
    intake_id: Optional[int] = Field(default=None)
    client_name: str = Field(..., min_length=2)
    client_email: str = Field(..., min_length=5)
    county: str = Field(..., min_length=2)
    apn: str = Field(..., min_length=3)
    signer_full_name: str = Field(..., min_length=2, description="Full Legal Name typed in digital signature block")
    signer_title: str = Field(..., min_length=2, description="Signer Title or capacity")
    agreement_version: str = Field(default="CA-UETA-ESIGN-2026.1")
    agreement_signed: bool = Field(default=True)
    signature_timestamp_utc: str = Field(..., min_length=10, description="UTC ISO-8601 execution timestamp")
    signer_ip_address: str = Field(..., min_length=3, description="Client IP address captured at signature")
    signer_user_agent: str = Field(..., min_length=2, description="Browser User-Agent string captured at signature")
    contract_sha256_hash: str = Field(
        ...,
        min_length=64,
        max_length=64,
        description="64-char hex SHA-256 hash of the signed contract text",
    )
    matched_grant_id: str = Field(default="")
    matched_grant_title: str = Field(default="")
    engineered_grant_request: int = Field(default=0, ge=0)
    contingent_fee_dollars: int = Field(default=0, ge=0)
    signed_agreement_path: str = Field(default="")


class ClientIntake(BaseModel):
    """Validated digital client onboarding submission from intake_app.py."""

    id: Optional[int] = Field(default=None)
    name: str = Field(..., min_length=2, description="Landowner full name or entity")
    email: str = Field(..., min_length=5, description="Landowner email address")
    county: str = Field(..., min_length=2, description="Property county")
    state: str = Field(default="CA", min_length=2, max_length=32, description="Property state")
    apn: str = Field(..., min_length=3, description="Assessor's Parcel Number (APN)")
    acreage: float = Field(..., gt=0, description="Total parcel acreage")
    land_condition: str = Field(..., min_length=5, description="Current land & resource condition")
    project_goals: str = Field(..., min_length=5, description="Desired conservation / agricultural upgrades")
    agreed_to_terms: bool = Field(default=False, description="Agreement to 7% ZYS GoldFields PRO LLC Advisory Agreement")
    agreement_signed: bool = Field(
        default=False,
        description="True when California UETA / ESIGN e-signature block is executed to unlock final filing dossier",
    )
    signer_full_name: str = Field(default="", description="Full Legal Name typed by landowner in signature block")
    signer_title: str = Field(
        default="Owner / Authorized Representative",
        description="Title / capacity typed by landowner in signature block",
    )
    signature_timestamp_utc: str = Field(default="", description="UTC ISO-8601 timestamp of electronic signature")
    signer_ip_address: str = Field(default="127.0.0.1", description="Client IP address at time of signature")
    signer_user_agent: str = Field(
        default="Mozilla/5.0 (ZYS-GoldFields-Intake-Portal)",
        description="Client browser User-Agent string at time of signature",
    )
    contract_sha256_hash: str = Field(default="", description="SHA-256 hash of the signed contract text")
    signed_agreement_path: str = Field(default="", description="Path to generated signed_advisory_agreement.md")
    submitted_at: Optional[str] = Field(default=None, description="ISO UTC submission timestamp")
    prospect_id: str = Field(default="", description="Prospect ID when submitted via a /claim magic link")
    ein: str = Field(default="", description="IRS EIN (optional; pre-filled from california_prospects.csv)")

    @field_validator("name", "email", "county", "state", "apn", "land_condition", "project_goals")
    @classmethod
    def clean_intake_strings(cls, v: str) -> str:
        cleaned = _clean_text(str(v))
        if not cleaned:
            raise ValueError("Intake text field cannot be empty.")
        return cleaned

    @field_validator("email")
    @classmethod
    def validate_email_address(cls, v: str) -> str:
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v):
            raise ValueError("Please enter a valid email address.")
        return v

    def model_post_init(self, __context: object) -> None:
        if self.agreed_to_terms and not self.agreement_signed:
            object.__setattr__(self, "agreement_signed", True)
        if self.agreement_signed and not self.agreed_to_terms:
            object.__setattr__(self, "agreed_to_terms", True)
        if self.agreement_signed and not self.signer_full_name.strip():
            object.__setattr__(self, "signer_full_name", self.name)
        if not self.signer_title.strip():
            object.__setattr__(self, "signer_title", "Owner / Authorized Representative")


class ProspectRecord(BaseModel):
    """Verified Tier-1 California wildlife sanctuary, land trust, or conservation ranch prospect."""

    prospect_id: str = Field(..., min_length=2)
    organization_name: str = Field(..., min_length=2)
    category: str = Field(..., min_length=2, description="Wildlife Sanctuary | Land Trust | Conservation Ranch")
    county: str = Field(..., min_length=2, description="Primary California county")
    service_counties: str = Field(default="", description="All California counties served")
    city: str = Field(default="", description="City in California")
    state: str = Field(default="CA")
    contact_email: str = Field(..., min_length=5, description="Live-verified public contact email")
    phone: str = Field(default="")
    website: str = Field(default="")
    ein: str = Field(default="", description="IRS EIN from ProPublica 990 registry or Private Working Ranch")
    estimated_acreage: float = Field(default=100.0, gt=0)
    land_type: str = Field(..., min_length=2)
    conservation_focus: str = Field(..., min_length=5)
    top_matched_grant: str = Field(default="")
    matched_grant_deadline: str = Field(default="")
    probability_score: int = Field(default=0, ge=0, le=100, description="Grant Win Probability Score (0-100)")
    acreage_score: int = Field(default=0, ge=0, le=30)
    entity_score: int = Field(default=0, ge=0, le=25)
    ecological_score: int = Field(default=0, ge=0, le=25)
    comatch_score: int = Field(default=0, ge=0, le=20)
    score_breakdown: str = Field(default="")
    score_rationale: str = Field(default="", description="1-sentence rationale explaining win probability")
    personalized_intake_url: str = Field(default="")
    registry_source: str = Field(..., min_length=3)
    outreach_segment: str = Field(
        default="Direct_Client",
        description="Channel_Partner (Land Trusts) | Direct_Client (Sanctuaries, Ranches, Farms)",
    )

    @field_validator("contact_email")
    @classmethod
    def validate_prospect_email(cls, v: str) -> str:
        cleaned = _clean_text(v).lower()
        if not re.match(r"^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,12}$", cleaned):
            raise ValueError(f"Invalid prospect email: {v}")
        return cleaned

