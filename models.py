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

    def normalized_counties(self) -> set[str]:
        if isinstance(self.county, str):
            val = self.county.strip().lower()
            return {val} if val else set()
        return {c.strip().lower() for c in self.county if c and c.strip()}

    def fee_label(self) -> str:
        if float(self.success_fee_percent).is_integer():
            return f"{int(self.success_fee_percent)}%"
        return f"{self.success_fee_percent:g}%"


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
    agreed_to_terms: bool = Field(..., description="Agreement to 7% no-win/no-fee advisory terms")
    submitted_at: Optional[str] = Field(default=None, description="ISO UTC submission timestamp")

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

    @field_validator("agreed_to_terms")
    @classmethod
    def must_agree_to_terms(cls, v: bool) -> bool:
        if v is not True:
            raise ValueError("You must agree to the contingent no-win/no-fee advisory terms to proceed.")
        return v


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

    @field_validator("contact_email")
    @classmethod
    def validate_prospect_email(cls, v: str) -> str:
        cleaned = _clean_text(v).lower()
        if not re.match(r"^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,12}$", cleaned):
            raise ValueError(f"Invalid prospect email: {v}")
        return cleaned

