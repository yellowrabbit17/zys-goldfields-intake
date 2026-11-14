"""Grant Award Probability Scoring Engine & Outreach Pitch Generator (matcher.py).

Evaluates California prospects and landowner parcels against active federal and California Climate
Investments grants in grants.db using a strict 4-pillar 100-point Grant Win Probability Scoring Engine:
  a) Acreage & Threshold Fit (0-30 pts)
  b) Entity & Tax-Exempt Eligibility (0-25 pts)
  c) Ecological & Geographic Priority Fit (0-25 pts)
  d) Funding-to-Scale Ratio & Co-Match Feasibility (0-20 pts)

Excludes any candidate scoring below config.min_probability_score (75/100) or failing a hard statutory
eligibility rule, ranks surviving Tier-1 candidates from highest probability score to lowest, and
generates zero-call outreach briefs in outreach_pitches.md.
"""

from __future__ import annotations

from datetime import datetime
import json
import logging
import os
from pathlib import Path
import re

import requests

from db import DEFAULT_DB_PATH, fetch_all_grants, fetch_all_profiles, fetch_all_prospects
from legal_agreement import render_can_spam_footer
from models import (
    GrantMatch,
    GrantRecord,
    LandownerProfile,
    MatcherConfig,
    ProbabilityScoreBreakdown,
    ProspectRecord,
    eligibility_tier_label,
    resolve_base_url,
    rewrite_portal_urls,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("matcher")

DEFAULT_LLM_BASE_URL = os.environ.get("OPENAI_BASE_URL", "http://localhost:11434/v1")
DEFAULT_LLM_MODEL = os.environ.get("OPENAI_MODEL", "")
DEFAULT_LLM_API_KEY = os.environ.get("OPENAI_API_KEY", "local-no-key")
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
DEFAULT_OUTPUT_PATH = Path(__file__).resolve().parent / "outreach_pitches.md"
DEFAULT_FOLLOWUP_PATH = Path(__file__).resolve().parent / "followup_templates.md"

NO_CALL_NEXT_STEP = (
    "Verify your parcel APN, e-sign, and unlock your complete filing packet in one click (no phone calls or meetings required)."
)

STATE_NAME_MAP = {
    "CA": "california",
    "OR": "oregon",
    "WA": "washington",
    "NV": "nevada",
}

OUT_OF_REGION_MARKERS = (
    "great lakes",
    "alaska",
    "chesapeake",
    "colorado river basin tribal",
    "hawai",
    "gulf coast ecosystem restoration",
    "restore act direct component",
    "bluefin tuna",
    "bycatch reduction",
    "marine mammal",
    "prescott marine",
)

COASTAL_OR_BAY_CA_COUNTIES = {
    "alameda",
    "contra costa",
    "del norte",
    "humboldt",
    "los angeles",
    "marin",
    "mendocino",
    "monterey",
    "napa",
    "orange",
    "san diego",
    "san francisco",
    "san luis obispo",
    "san mateo",
    "santa barbara",
    "santa clara",
    "santa cruz",
    "solano",
    "sonoma",
    "ventura",
}

# CAL FIRE High / Very-High Fire Hazard Severity Zone & nonindustrial timber/oak woodland counties
CALFIRE_PRIORITY_FIRE_COUNTIES = {
    "amador",
    "butte",
    "calaveras",
    "del norte",
    "el dorado",
    "fresno",
    "humboldt",
    "lake",
    "lassen",
    "madera",
    "marin",
    "mariposa",
    "mendocino",
    "monterey",
    "napa",
    "nevada",
    "placer",
    "plumas",
    "san benito",
    "san bernardino",
    "san diego",
    "san luis obispo",
    "san mateo",
    "santa barbara",
    "santa cruz",
    "shasta",
    "sierra",
    "siskiyou",
    "sonoma",
    "tehama",
    "trinity",
    "tulare",
    "tuolumne",
    "ventura",
    "yuba",
}

# Prime Farmland, Rangeland & Conversion-Risk Agricultural Counties (DOC / SALC / USDA-NRCS ACEP)
SALC_PRIME_AG_COUNTIES = {
    "alameda",
    "amador",
    "butte",
    "colusa",
    "contra costa",
    "fresno",
    "glenn",
    "imperial",
    "inyo",
    "kern",
    "kings",
    "madera",
    "marin",
    "merced",
    "monterey",
    "napa",
    "placer",
    "riverside",
    "sacramento",
    "san benito",
    "san diego",
    "san joaquin",
    "san luis obispo",
    "san mateo",
    "santa barbara",
    "santa clara",
    "santa cruz",
    "solano",
    "sonoma",
    "stanislaus",
    "sutter",
    "tehama",
    "tulare",
    "tuolumne",
    "ventura",
    "yolo",
    "yuba",
}

# Pacific Flyway & Central Valley Wetland / Easement Priority Counties (USDA ACEP)
ACEP_WETLAND_EASEMENT_COUNTIES = {
    "butte",
    "colusa",
    "glenn",
    "humboldt",
    "marin",
    "merced",
    "monterey",
    "placer",
    "sacramento",
    "san benito",
    "san joaquin",
    "solano",
    "sonoma",
    "sutter",
    "tulare",
    "yolo",
    "yuba",
}

# Anadromous Salmonid & Headwater Perennial Fish-Passage Priority Counties (USFWS / Reclamation)
FISH_PASSAGE_WATERSHED_COUNTIES = {
    "butte",
    "calaveras",
    "del norte",
    "el dorado",
    "humboldt",
    "marin",
    "mendocino",
    "monterey",
    "napa",
    "nevada",
    "placer",
    "plumas",
    "sacramento",
    "san luis obispo",
    "san mateo",
    "santa barbara",
    "santa cruz",
    "shasta",
    "sierra",
    "siskiyou",
    "solano",
    "sonoma",
    "sutter",
    "tehama",
    "trinity",
    "ventura",
    "yolo",
    "yuba",
}

# Counties with pre-mapped County RCD & USDA NRCS Service Center co-sponsor contacts
DIRECT_RCD_MAPPED_COUNTIES = {
    "amador",
    "butte",
    "calaveras",
    "el dorado",
    "marin",
    "napa",
    "nevada",
    "placer",
    "sacramento",
    "san benito",
    "san diego",
    "san luis obispo",
    "san mateo",
    "santa barbara",
    "solano",
    "sonoma",
    "ventura",
    "yolo",
    "yuba",
}

ARID_DESERT_COUNTIES = {"imperial", "inyo", "san bernardino"}
URBAN_CORE_COUNTIES = {"san francisco"}

DOMAIN_CLUSTERS: dict[str, tuple[str, ...]] = {
    "forest & oak woodland": (
        "forest",
        "reforestation",
        "woodland",
        "oak",
        "timber",
        "conifer",
        "pine",
        "redwood",
        "canopy",
        "thinning",
        "wildfire",
        "fuels",
        "stand improvement",
    ),
    "riparian & watershed": (
        "riparian",
        "watershed",
        "stream",
        "streambank",
        "creek",
        "river",
        "fish passage",
        "culvert",
        "aquatic",
        "instream",
        "water resources",
        "wetland",
    ),
    "working ranch & agricultural conservation": (
        "ranch",
        "rangeland",
        "agricultural",
        "agriculture",
        "farmland",
        "grazing",
        "pasture",
        "livestock",
        "cattle",
        "cooperative agriculture",
        "easement",
    ),
    "wildlife habitat & biodiversity": (
        "wildlife",
        "habitat",
        "sanctuary",
        "biodiversity",
        "endangered",
        "amphibian",
        "migratory",
        "bird",
        "songbird",
        "vernal pool",
        "pollinator",
        "species",
    ),
    "soil health & climate resilience": (
        "soil health",
        "soil",
        "erosion",
        "regenerative",
        "climate adaptation",
        "resiliency",
        "resilience",
        "carbon",
        "nature-based",
    ),
    "solar & clean ag energy": (
        "solar",
        "photovoltaic",
        "clean energy",
        "energy efficiency",
        "irrigation",
    ),
}


def load_matcher_config(config_path: Path | str = DEFAULT_CONFIG_PATH) -> MatcherConfig:
    """Load and validate config.json (falling back to defaults if absent), applying BASE_URL env override."""
    cfg_file = Path(config_path)
    if not cfg_file.exists():
        cfg = MatcherConfig()
        cfg_file.write_text(json.dumps(cfg.model_dump(), indent=2) + "\n", encoding="utf-8")
    else:
        raw = json.loads(cfg_file.read_text(encoding="utf-8"))
        cfg = MatcherConfig(**raw)
    resolved_url = resolve_base_url(cfg)
    if resolved_url != cfg.public_intake_url:
        cfg = cfg.model_copy(update={"public_intake_url": resolved_url})
    return cfg


def extract_max_grant_dollars(funding_amount: str) -> int | None:
    """Extract the largest explicit dollar figure from a grant funding description."""
    pattern = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)\s*(million|billion|m|b|k)?", flags=re.I)
    values: list[int] = []
    for num_str, unit in pattern.findall(funding_amount or ""):
        try:
            base = float(num_str.replace(",", ""))
        except ValueError:
            continue
        u = (unit or "").lower()
        if u in ("billion", "b"):
            base *= 1_000_000_000
        elif u in ("million", "m"):
            base *= 1_000_000
        elif u == "k":
            base *= 1_000
        values.append(int(round(base)))
    return max(values) if values else None


def filter_profiles_by_config(
    profiles: list[LandownerProfile],
    config: MatcherConfig,
) -> list[LandownerProfile]:
    """Filter landowner profiles using state, county, and min/max acreage rules from config.json."""
    target_state = config.state.strip().lower()
    target_counties = config.normalized_counties()
    filtered: list[LandownerProfile] = []

    for p in profiles:
        if target_state:
            p_state = p.state.strip().lower()
            p_state_full = STATE_NAME_MAP.get(p.state.strip().upper(), p_state)
            cfg_state_full = STATE_NAME_MAP.get(config.state.strip().upper(), target_state)
            if p_state != target_state and p_state_full != cfg_state_full:
                continue
        if target_counties and p.county.strip().lower() not in target_counties:
            continue
        if not (config.min_acreage <= p.acreage <= config.max_acreage):
            continue
        filtered.append(p)
    return filtered


def check_geographic_eligibility(profile: LandownerProfile, grant: GrantRecord) -> tuple[bool, str]:
    """Verify state, regional, coastal vs. inland, and county-level statutory geographic eligibility."""
    combined = f"{grant.title} {grant.agency} {grant.eligibility_text}".lower()
    state_full = STATE_NAME_MAP.get(profile.state.upper(), profile.state.lower())

    if state_full == "california":
        for marker in OUT_OF_REGION_MARKERS:
            if marker in combined:
                return False, f"Out-of-region statutory restriction ({marker})"
        norm_county = profile.county.strip().lower().replace(" county", "")
        if norm_county not in COASTAL_OR_BAY_CA_COUNTIES and (
            "sea level rise" in combined or "state coastal conservancy" in combined
        ):
            return False, f"Coastal/sea-level-rise program inapplicable to inland {profile.county} County"

    profile_blob = f"{profile.land_type} {profile.features}".lower()
    if "farmworker housing" in grant.title.lower() and not (
        "solar" in profile_blob or "housing" in profile_blob or "photovoltaic" in profile_blob
    ):
        return False, "Farmworker housing energy program requires on-farm solar/housing scope"

    county_list_match = re.search(
        r"within the following counties:\s*([^.]+)\.",
        grant.eligibility_text,
        flags=re.I,
    )
    if county_list_match:
        allowed_counties_blob = county_list_match.group(1).lower()
        if profile.county.lower() not in allowed_counties_blob:
            return False, f"{profile.county} County is outside statutory eligible county list"

    return True, "Geographically eligible"


def check_acreage_eligibility(profile: LandownerProfile, grant: GrantRecord) -> tuple[bool, str | None]:
    """Parse statutory acreage minimums/maximums from eligibility_text (e.g. 'between 20 and 5,000 acres')."""
    range_match = re.search(
        r"between\s+([\d,]+)\s+and\s+([\d,]+)\s+acres",
        grant.eligibility_text,
        flags=re.I,
    )
    if range_match:
        min_acres = float(range_match.group(1).replace(",", ""))
        max_acres = float(range_match.group(2).replace(",", ""))
        if not (min_acres <= profile.acreage <= max_acres):
            return (
                False,
                f"Parcel acreage ({profile.acreage:.0f} ac) outside statutory {min_acres:,.0f}-{max_acres:,.0f} ac limit",
            )
        return (
            True,
            f"{profile.acreage:,.0f}-acre parcel meets statutory {min_acres:,.0f}-{max_acres:,.0f} acre rule",
        )
    return True, None


def classify_applicant_pathway(grant: GrantRecord) -> tuple[bool, str]:
    """Determine whether a private landowner, ranch, sanctuary, or land trust can apply directly or via RCD."""
    title_low = grant.title.lower()
    elig_low = grant.eligibility_text.lower()

    disqualifying_title_terms = (
        "for tribes",
        "indian tribes",
        "native american affairs",
        "tribal colleges",
        "hispanic-serving institutions",
        "non-land-grant colleges",
        "agriculture and food research initiative",
        "cesu",
        "cooperative ecosystems studies units",
        "state ans management",
        "interjurisdictional",
        "darrp",
        "bycatch",
        "bluefin tuna",
        "marine mammal rescue",
        "national marine",
        "restore act",
        "wolf livestock",
        "outdoor recreation legacy partnership",
        "readiness and recreation initiative",
        "recovery land acquisition",
        "brownfield",
        "wildlife crossings pilot",
        "water resources research act",
        "small business innovation research",
        "desalination construction",
        "water reclamation and reuse",
        "nonpoint source management grants under clean water act",
        "foundational and applied science",
        "innovation partners programs",
    )
    if any(term in title_low for term in disqualifying_title_terms):
        return False, "Restricted institutional/government/tribal-only program"

    if "only state and territorial" in elig_low or "eligible applicants are only the states" in elig_low:
        return False, "Restricted to State governments only"

    direct_markers = (
        "private landowners",
        "private individuals",
        "landowners owning",
        "eligible applicants: individuals",
        "small businesses; individuals",
        "applicant & sector tags: individuals",
        "farmers",
    )
    if "easement funding available to local and regional agencies and nonprofits" in elig_low:
        return (
            True,
            "501(c)(3) Land Trust / Resource Conservation District (RCD) Agricultural Easement Application",
        )

    if any(m in elig_low for m in direct_markers):
        return True, "Direct Private Landowner / Agricultural Producer / Fee-Title Nonprofit Application"

    partner_markers = (
        "nonprofits",
        "non-profits",
        "watershed group",
        "irrigation districts",
        "resource conservation",
        "special district governments",
        "county governments",
        "non-federal lands",
    )
    if any(m in elig_low for m in partner_markers):
        return (
            True,
            "Direct 501(c)(3) Nonprofit or County RCD / Land Trust Co-Sponsored Application",
        )

    return False, "No eligible landowner, 501(c)(3), or RCD co-sponsor pathway"


def estimate_project_award_and_fee(
    profile: LandownerProfile,
    grant: GrantRecord,
    fee_percent: float = 7.0,
) -> tuple[int, str, str]:
    """Derive a realistic parcel-scaled grant target and success-fee illustration."""
    title_low = grant.title.lower()
    acreage = profile.acreage

    if "california forest improvement" in title_low:
        est_award = max(35000, min(150000, int(round(acreage * 1350, -3))))
    elif "sustainable agricultural lands conservation" in title_low:
        est_award = max(250000, min(1200000, int(round(acreage * 4500, -4))))
    elif "climate adaptation and resiliency" in title_low:
        est_award = max(75000, min(450000, int(round(60000 + acreage * 1800, -3))))
    elif "climate ready program" in title_low:
        est_award = max(125000, min(500000, int(round(85000 + acreage * 1400, -3))))
    elif "fish passage" in title_low:
        est_award = 185000 if acreage >= 50 else 125000
    elif "landscape scale restoration" in title_low:
        est_award = max(150000, min(450000, int(round(acreage * 650, -3))))
    elif "easement restoration and stewardship" in title_low:
        est_award = max(250000, min(850000, int(round(acreage * 900, -4))))
    elif "watersmart cooperative watershed" in title_low:
        est_award = 150000
    elif "watersmart enhancing water" in title_low:
        est_award = 225000
    elif "cooperative agriculture" in title_low:
        est_award = 55000
    else:
        est_award = max(50000, min(250000, int(round(acreage * 1500, -3))))

    fee_rate = fee_percent / 100.0
    fee_dollars = int(round(est_award * fee_rate))
    net_dollars = est_award - fee_dollars
    pct_str = f"{int(fee_percent)}%" if float(fee_percent).is_integer() else f"{fee_percent:g}%"
    fee_summary = (
        f"On an estimated ${est_award:,} project award, our {pct_str} success fee is ${fee_dollars:,} "
        f"(netting ${net_dollars:,} directly to your conservation project; $0 owed if not awarded)"
    )
    return est_award, f"${est_award:,}", fee_summary


def recommend_project_focus(profile: LandownerProfile, grant: GrantRecord, matched_clusters: list[str]) -> str:
    """Summarize the tailored conservation scope aligning the landowner parcel with the grant."""
    title_low = grant.title.lower()
    if "california forest improvement" in title_low:
        return (
            f"Registered Professional Forester (RPF) Forest Management Plan, post-drought oak/conifer "
            f"reforestation, brush fuels thinning, and erosion stabilization across {profile.acreage:,.0f} acres"
        )
    if "sustainable agricultural lands conservation" in title_low:
        return (
            f"Permanent Agricultural Conservation Easement and climate-smart agricultural stewardship plan "
            f"across {profile.acreage:,.0f} acres in {profile.county} County"
        )
    if "climate adaptation and resiliency" in title_low:
        return (
            f"Natural and working lands climate resilience implementation ({', '.join(matched_clusters[:2]) or 'habitat & soil conservation'}) "
            f"across {profile.acreage:,.0f} acres in {profile.county} County"
        )
    if "climate ready program" in title_low:
        return (
            f"Nature-based coastal watershed, tidal wetland, and climate-resilient habitat restoration "
            f"across {profile.acreage:,.0f} acres in {profile.county} County"
        )
    if "fish passage" in title_low:
        return (
            f"Instream culvert/barrier remediation, stream-crossing upgrade, and native riparian "
            f"aquatic habitat connectivity in {profile.county} County"
        )
    if "landscape scale restoration" in title_low:
        return (
            f"Non-federal forest watershed restoration, native oak/conifer reforestation, wildfire fuel reduction, "
            f"and habitat enhancement via a {profile.county} County collaborative block grant"
        )
    if "watersmart" in title_low:
        return (
            f"Riparian corridor water-efficiency modernization, conservation irrigation, "
            f"and watershed habitat enhancement in {profile.county} County"
        )
    if "easement restoration" in title_low:
        return (
            f"Wetland and floodplain riparian easement restoration, streambank stabilization, and soil stewardship "
            f"across {profile.acreage:,.0f} acres in {profile.county} County"
        )
    return (
        f"Site-specific {', '.join(matched_clusters[:2]) or 'conservation & habitat'} improvements "
        f"across {profile.acreage:,.0f} acres in {profile.county} County"
    )


def evaluate_grant_win_probability(
    profile: LandownerProfile,
    grant: GrantRecord,
    entity_category: str = "Private Landowner",
    ein: str = "",
    config: MatcherConfig | None = None,
) -> ProbabilityScoreBreakdown:
    """Evaluate a candidate against a grant across the 4 weighted probability pillars (0-100):
    a) Acreage & Threshold Fit (30 pts)
    b) Entity & Tax-Exempt Eligibility (25 pts)
    c) Ecological & Geographic Priority Fit (25 pts)
    d) Funding-to-Scale Ratio & Co-Match Feasibility (20 pts)
    """
    cfg = config or load_matcher_config()
    acreage = float(profile.acreage)
    norm_county = profile.county.strip().lower().replace(" county", "")
    title_low = grant.title.lower()
    grant_blob = f"{grant.title} {grant.agency} {grant.eligibility_text}".lower()
    profile_blob = f"{profile.name} {profile.land_type} {profile.features} {profile.county}".lower()

    def _disqualify(reason: str) -> ProbabilityScoreBreakdown:
        return ProbabilityScoreBreakdown(
            total_score=0,
            acreage_threshold_pts=0,
            entity_eligibility_pts=0,
            ecological_priority_pts=0,
            scale_comatch_pts=0,
            hard_disqualified=True,
            disqualification_reason=reason,
            score_rationale=f"Excluded: {reason}.",
        )

    # 0. Pre-flight hard statutory checks (grant minimum size, geography, statutory acreage, pathway)
    if cfg.min_grant_amount > 0:
        max_dollars = extract_max_grant_dollars(grant.funding_amount)
        if max_dollars is not None and max_dollars < cfg.min_grant_amount:
            return _disqualify(f"Grant cap (${max_dollars:,}) below ${cfg.min_grant_amount:,} floor")

    geo_ok, geo_note = check_geographic_eligibility(profile, grant)
    if not geo_ok:
        return _disqualify(geo_note)

    acre_ok, acre_note = check_acreage_eligibility(profile, grant)
    if not acre_ok:
        return _disqualify(acre_note or "Outside statutory acreage limits")

    pathway_ok, _ = classify_applicant_pathway(grant)
    if not pathway_ok:
        return _disqualify("Ineligible institutional/government-only statutory applicant class")

    # -------------------------------------------------------------------------
    # CRITERION A: Acreage & Threshold Fit (0 - 30 pts)
    # -------------------------------------------------------------------------
    acreage_reason_frag = ""
    if "california forest improvement" in title_low:
        # Strict CAL FIRE CFIP 20-5,000 acre statutory forestland window (already hard-checked above)
        if 40 <= acreage <= 2500:
            acreage_pts = 30
            acreage_reason_frag = f"{acreage:,.0f}-acre footprint sits squarely inside CAL FIRE's 20–5,000 ac statutory forestland window"
        elif 25 <= acreage < 40 or 2500 < acreage <= 5000:
            acreage_pts = 26
            acreage_reason_frag = f"{acreage:,.0f}-acre parcel complies with CAL FIRE's 20–5,000 ac statutory threshold"
        else:
            acreage_pts = 22
            acreage_reason_frag = f"{acreage:,.0f}-acre parcel meets CAL FIRE's 20-acre minimum floor"
    elif "sustainable agricultural lands conservation" in title_low:
        if acreage < 40:
            return _disqualify(f"Parcel ({acreage:.0f} ac) below 40-acre viable SALC agricultural easement threshold")
        if acreage >= 350:
            acreage_pts = 30
            acreage_reason_frag = f"{acreage:,.0f}-acre working agricultural scale strongly fits SALC conservation easement underwriting"
        elif acreage >= 100:
            acreage_pts = 27
            acreage_reason_frag = f"{acreage:,.0f}-acre agricultural parcel meets SALC viable easement scale"
        else:
            acreage_pts = 19
            acreage_reason_frag = f"{acreage:,.0f}-acre parcel is modest for SALC easement transaction sizing"
    elif "easement restoration and stewardship" in title_low:
        if acreage < 100:
            return _disqualify(f"Parcel ({acreage:.0f} ac) below 100-acre threshold for $250k+ USDA ACEP restoration")
        if acreage >= 300:
            acreage_pts = 30
            acreage_reason_frag = f"{acreage:,.0f}-acre wetland/easement footprint matches USDA-NRCS ACEP landscape stewardship scale"
        else:
            acreage_pts = 23
            acreage_reason_frag = f"{acreage:,.0f}-acre easement footprint satisfies USDA-NRCS ACEP scale"
    elif "landscape scale restoration" in title_low:
        if acreage < 50:
            return _disqualify(f"Parcel ({acreage:.0f} ac) below 50-acre threshold for USFS Landscape Scale Restoration")
        acreage_pts = 29 if acreage >= 250 else 22
        acreage_reason_frag = f"{acreage:,.0f}-acre non-federal forest block aligns with USFS LSR acreage scope"
    elif "climate ready program" in title_low:
        if acreage < 20:
            acreage_pts = 11
            acreage_reason_frag = f"{acreage:,.0f}-acre micro-site is undersized for ~$490k Coastal Conservancy awards"
        elif acreage < 80:
            acreage_pts = 20
            acreage_reason_frag = f"{acreage:,.0f}-acre coastal parcel meets moderate State Coastal Conservancy scale"
        elif acreage < 350:
            acreage_pts = 27
            acreage_reason_frag = f"{acreage:,.0f}-acre coastal watershed footprint aligns well with Climate Ready scale"
        else:
            acreage_pts = 30
            acreage_reason_frag = f"{acreage:,.0f}-acre coastal conservation holding matches flagship State Coastal Conservancy scale"
    elif "climate adaptation and resiliency" in title_low:
        if acreage < 25:
            acreage_pts = 12
            acreage_reason_frag = f"{acreage:,.0f}-acre parcel is small relative to $400k+ Climate Adaptation awards"
        elif acreage < 100:
            acreage_pts = 22
            acreage_reason_frag = f"{acreage:,.0f}-acre working/natural land parcel fits regional Climate Adaptation scope"
        elif acreage < 350:
            acreage_pts = 27
            acreage_reason_frag = f"{acreage:,.0f}-acre conservation footprint strongly fits Climate Adaptation implementation scale"
        else:
            acreage_pts = 30
            acreage_reason_frag = f"{acreage:,.0f}-acre landscape holding matches California Climate Adaptation flagship scale"
    elif "fish passage" in title_low or "watersmart" in title_low:
        if acreage < 30:
            acreage_pts = 14
            acreage_reason_frag = f"{acreage:,.0f}-acre reach is small for federal watershed barrier/water funding"
        elif acreage < 150:
            acreage_pts = 24
            acreage_reason_frag = f"{acreage:,.0f}-acre riparian corridor supports watershed project sizing"
        else:
            acreage_pts = 28
            acreage_reason_frag = f"{acreage:,.0f}-acre watershed stewardship footprint supports multi-reach habitat restoration"
    else:
        acreage_pts = 26 if acreage >= 100 else (21 if acreage >= 35 else 14)
        acreage_reason_frag = f"{acreage:,.0f}-acre property complies with program parcel thresholds"

    # Adjust portfolio-estimated regional Land Trust acreages (650/850 ac) by -3 pts vs single-deeded parcels
    if entity_category == "Land Trust" and acreage in (650.0, 850.0) and acreage_pts >= 27:
        acreage_pts -= 3
        acreage_reason_frag += " (portfolio estimate pending parcel APN selection)"

    # -------------------------------------------------------------------------
    # CRITERION B: Entity & Tax-Exempt Eligibility (0 - 25 pts)
    # -------------------------------------------------------------------------
    ein_clean = (ein or "").strip()
    has_verified_990_ein = bool(re.match(r"^\d{2}-\d{7}$", ein_clean))
    is_unverified_nonprofit = ein_clean == "CA Registered Nonprofit"
    is_land_trust = entity_category == "Land Trust"
    is_sanctuary = entity_category == "Wildlife Sanctuary"
    is_ranch_or_private = entity_category in ("Conservation Ranch", "Private Landowner") or ein_clean == "Private Working Ranch"
    has_mapped_rcd = norm_county in DIRECT_RCD_MAPPED_COUNTIES

    entity_reason_frag = ""
    if is_unverified_nonprofit:
        # Strict IRS Form 990 check: nonprofits without a verifiable federal EIN fail Tier-1 underwriting
        entity_pts = 6
        entity_reason_frag = "lacks verifiable IRS Form 990 501(c)(3) EIN in federal registry"
    elif "sustainable agricultural lands conservation" in title_low:
        # SALC funds agricultural conservation easements via Land Trusts/RCDs on working ag land (not animal rescues)
        if is_sanctuary and not any(k in profile_blob for k in ("working rice", "ranch", "cropland", "farmland")):
            return _disqualify("Non-commercial animal rescue sanctuary ineligible for SALC agricultural conservation easements")
        if is_land_trust and has_verified_990_ein:
            entity_pts = 25
            entity_reason_frag = f"IRS Form 990-verified 501(c)(3) land trust (EIN {ein_clean}) is a directly eligible SALC easement grantee"
        elif is_ranch_or_private and has_verified_990_ein:
            entity_pts = 24
            entity_reason_frag = f"agricultural producer with verified 501(c)(3) standing (EIN {ein_clean}) and local RCD/land trust co-application readiness"
        elif is_ranch_or_private:
            entity_pts = 22 if has_mapped_rcd else 20
            entity_reason_frag = (
                f"commercial agricultural producer eligible via {profile.county} County RCD / agricultural land trust easement co-sponsorship"
            )
        else:
            entity_pts = 16
            entity_reason_frag = "requires agricultural land trust co-applicant"
    elif "california forest improvement" in title_low:
        if has_verified_990_ein and (is_sanctuary or is_land_trust or is_ranch_or_private):
            entity_pts = 25 if (is_sanctuary or is_ranch_or_private) else 23
            entity_reason_frag = f"verified 501(c)(3) forestland steward (EIN {ein_clean}) qualifies directly under CAL FIRE CFIP"
        elif is_ranch_or_private:
            entity_pts = 24 if has_mapped_rcd else 23
            entity_reason_frag = f"private nonindustrial forestland/ranch owner qualifies directly with {profile.county} RCD/RPF support"
        else:
            entity_pts = 18
            entity_reason_frag = "eligible subject to fee-title woodland verification"
    elif "easement restoration and stewardship" in title_low:
        if has_verified_990_ein and (is_land_trust or "basin" in profile_blob or "conservancy" in profile_blob):
            entity_pts = 25
            entity_reason_frag = f"IRS Form 990-verified 501(c)(3) conservation holder (EIN {ein_clean}) meets USDA-NRCS ACEP entity rules"
        elif has_verified_990_ein:
            entity_pts = 22
            entity_reason_frag = f"verified 501(c)(3) nonprofit (EIN {ein_clean}) qualifies for USDA-NRCS stewardship agreements"
        elif is_ranch_or_private:
            entity_pts = 19 if has_mapped_rcd else 17
            entity_reason_frag = f"agricultural landowner eligible via {profile.county} County RCD/NRCS co-sponsorship"
        else:
            entity_pts = 15
            entity_reason_frag = "requires conservation easement holder partnership"
    elif "climate ready program" in title_low or "landscape scale restoration" in title_low:
        if has_verified_990_ein and is_land_trust:
            entity_pts = 25
            entity_reason_frag = f"IRS Form 990-verified 501(c)(3) conservancy (EIN {ein_clean}) is directly eligible as lead grantee"
        elif has_verified_990_ein and is_sanctuary:
            entity_pts = 23
            entity_reason_frag = f"verified 501(c)(3) wildlife sanctuary (EIN {ein_clean}) qualifies directly as nonprofit applicant"
        elif is_ranch_or_private:
            entity_pts = 19 if has_mapped_rcd else 17
            entity_reason_frag = f"eligible via {profile.county} County RCD co-sponsored application"
        else:
            entity_pts = 15
            entity_reason_frag = "eligible via local public agency partner"
    else:
        # Climate Adaptation & Resiliency, USFWS National Fish Passage, WaterSMART, Cooperative Ag
        if has_verified_990_ein:
            entity_pts = 25
            entity_reason_frag = f"IRS Form 990-verified 501(c)(3) entity (EIN {ein_clean}) has direct statutory applicant standing"
        elif is_ranch_or_private:
            entity_pts = 24 if has_mapped_rcd else 22
            entity_reason_frag = f"private agricultural/conservation landowner is directly eligible with {profile.county} RCD coordination"
        else:
            entity_pts = 18
            entity_reason_frag = "eligible via local conservation district partnership"

    # -------------------------------------------------------------------------
    # CRITERION C: Ecological & Geographic Priority Fit (0 - 25 pts)
    # -------------------------------------------------------------------------
    matched_clusters: list[str] = []
    matched_terms: list[str] = []
    for cluster_name, terms in DOMAIN_CLUSTERS.items():
        prof_hits = [t for t in terms if t in profile_blob]
        grant_hits = [t for t in terms if t in grant_blob]
        if prof_hits and grant_hits:
            matched_clusters.append(cluster_name)
            shared = sorted(set(prof_hits) & set(grant_hits))
            matched_terms.extend(shared if shared else grant_hits[:2])

    if not matched_clusters:
        return _disqualify("No ecological conservation domain overlap between property and solicitation")

    eco_reason_frag = ""
    if "california forest improvement" in title_low or "landscape scale restoration" in title_low:
        forest_hits = re.findall(
            r"\b(oak|forest|forests|forestland|forestry|woodland|woodlands|pine|conifer|conifers|redwood|redwoods|timber)\b",
            profile_blob,
        )
        dedup_forest = list(dict.fromkeys(forest_hits))
        if not dedup_forest:
            return _disqualify("Property lacks qualifying forest, oak woodland, or conifer stand for forestry funding")
        if norm_county in URBAN_CORE_COUNTIES:
            return _disqualify(f"Urban {profile.county} County headquarters lacks rural nonindustrial forestland parcel")
        if norm_county in CALFIRE_PRIORITY_FIRE_COUNTIES:
            fire_bonus = 3 if any(w in profile_blob for w in ("wildfire", "fuel", "thinning", "reforestation", "erosion")) else 1
            eco_pts = min(25, 20 + min(3, len(dedup_forest)) + fire_bonus - 1)
            eco_reason_frag = f"{profile.county} County is a priority CAL FIRE fire-severity & forest watershed zone ({', '.join(dedup_forest[:2])})"
        else:
            eco_pts = 16
            eco_reason_frag = f"{profile.county} County has secondary non-mountain forestry priority"
    elif "sustainable agricultural lands conservation" in title_low:
        ag_terms = ("ranch", "rangeland", "agricultural", "farmland", "farm", "grazing", "pasture", "orchard", "dairy", "cropland", "livestock")
        ag_hits = [w for w in ag_terms if w in profile_blob]
        if not ag_hits:
            return _disqualify("Property lacks working agricultural, grazing, or farmland resources required by SALC")
        if norm_county in SALC_PRIME_AG_COUNTIES:
            eco_pts = min(25, 20 + min(5, len(ag_hits) + (2 if "easement" in profile_blob or "regenerative" in profile_blob or "organic" in profile_blob else 0)))
            eco_reason_frag = f"{profile.county} County is a high-priority agricultural conversion-risk zone ({', '.join(ag_hits[:2])})"
        else:
            eco_pts = 16
            eco_reason_frag = f"{profile.county} County holds moderate statewide agricultural easement priority"
    elif "climate ready program" in title_low:
        coastal_terms = ("coastal", "coast", "shoreline", "tidal", "marsh", "estuary", "wetland", "bay", "sea level", "marine", "redwood coast", "big sur")
        coastal_hits = [w for w in coastal_terms if w in profile_blob]
        if not coastal_hits:
            return _disqualify(f"Inland/upland property in {profile.county} County lacks coastal shoreline, wetland, or sea-level-rise exposure")
        eco_pts = min(25, 20 + min(5, len(coastal_hits) + 1))
        eco_reason_frag = f"{profile.county} County coastal watershed/shoreline habitat ({', '.join(coastal_hits[:2])}) directly targets State Coastal Conservancy priorities"
    elif "easement restoration and stewardship" in title_low:
        if norm_county in URBAN_CORE_COUNTIES:
            return _disqualify(f"Urban {profile.county} County location outside rural USDA-NRCS ACEP easement zones")
        wetland_terms = ("wetland", "wetlands", "marsh", "flyway", "floodplain", "easement", "easements", "vernal pool")
        w_hits = [w for w in wetland_terms if w in profile_blob]
        if not w_hits:
            return _disqualify("Lacks existing wetland, floodplain, or conservation easement portfolio required by USDA-NRCS ACEP")
        if norm_county in ACEP_WETLAND_EASEMENT_COUNTIES:
            eco_pts = min(25, 21 + min(4, len(w_hits)))
            eco_reason_frag = f"{profile.county} County lies within a priority Pacific Flyway / Central Valley easement corridor"
        else:
            eco_pts = 18
            eco_reason_frag = f"{profile.county} County supports regional conservation easement stewardship"
    elif "fish passage" in title_low:
        instream_terms = ("river", "salmon", "trout", "fish", "culvert", "barrier", "instream", "aquatic", "creek", "stream")
        s_hits = [w for w in instream_terms if w in profile_blob]
        if not s_hits or norm_county in ARID_DESERT_COUNTIES:
            return _disqualify(f"Lacks perennial salmonid/instream fish-passage habitat in {profile.county} County")
        if is_sanctuary and not any(w in profile_blob for w in ("river", "salmon", "trout", "fish", "culvert", "mattole")):
            return _disqualify("Upland animal sanctuary lacks instream fish-passage barrier remediation scope")
        if norm_county in FISH_PASSAGE_WATERSHED_COUNTIES:
            eco_pts = min(25, 20 + min(5, len(s_hits) + 1))
            eco_reason_frag = f"{profile.county} County riverine watershed ({', '.join(s_hits[:2])}) aligns with USFWS fish-passage priorities"
        else:
            eco_pts = 15
            eco_reason_frag = f"{profile.county} County holds secondary watershed connectivity priority"
    elif "watersmart" in title_low:
        water_terms = ("watershed", "river", "creek", "riparian", "irrigation", "water", "solar", "wetland")
        w_hits = [w for w in water_terms if w in profile_blob]
        if not w_hits:
            return _disqualify("Lacks riparian water-efficiency or watershed management scope for WaterSMART")
        eco_pts = min(24, 18 + min(6, len(w_hits) + 1))
        eco_reason_frag = f"{profile.county} County watershed & agricultural water stewardship aligns with Reclamation WaterSMART"
    else:
        # Climate Adaptation & Resiliency / Cooperative Agriculture
        if norm_county in URBAN_CORE_COUNTIES:
            eco_pts = 8
            eco_reason_frag = f"urban {profile.county} County address has low natural/working lands priority"
        else:
            eco_pts = min(24, 18 + min(6, len(matched_clusters) * 2))
            eco_reason_frag = f"{profile.county} County natural and working lands scope aligns with California climate resilience priorities"

    # -------------------------------------------------------------------------
    # CRITERION D: Funding-to-Scale Ratio & Co-Match Feasibility (0 - 20 pts)
    # -------------------------------------------------------------------------
    est_award, _, _ = estimate_project_award_and_fee(profile, grant, fee_percent=cfg.success_fee_percent)
    comatch_reason_frag = ""

    if is_unverified_nonprofit:
        comatch_pts = 10
        comatch_reason_frag = "unverified tax-exempt filings limit institutional cost-share underwriting"
    elif "california forest improvement" in title_low:
        # CAL FIRE reimburses 75%-90% (10%-25% landowner cash/in-kind RPF & site-prep co-match)
        scale_sub = 10 if acreage >= 45 else (8 if acreage >= 30 else 6)
        if (is_land_trust or is_sanctuary) and acreage >= 150 and has_verified_990_ein:
            match_sub = 9
        elif is_ranch_or_private and acreage >= 45:
            match_sub = 9 if acreage >= 200 else 8
        else:
            match_sub = 6
        comatch_pts = min(20, scale_sub + match_sub)
        comatch_reason_frag = f"~${est_award:,} budget fits CAL FIRE's 75%–90% cost-share schedule with manageable 10%–25% co-match"
    elif "sustainable agricultural lands conservation" in title_low:
        # SALC covers 75%-100% of easement value; bargain-sale land value or trust stewardship covers match
        scale_sub = 10 if acreage >= 500 else (8 if acreage >= 200 else 5)
        if is_land_trust and has_verified_990_ein:
            match_sub = 9
        elif is_ranch_or_private and acreage >= 200:
            match_sub = 9 if acreage >= 800 else 8
        else:
            match_sub = 5
        comatch_pts = min(20, scale_sub + match_sub)
        comatch_reason_frag = f"~${est_award:,} easement valuation supports SALC funding-to-acreage underwriting and bargain-sale co-match"
    elif "easement restoration and stewardship" in title_low:
        scale_sub = 10 if acreage >= 400 else (8 if acreage >= 200 else 4)
        match_sub = 9 if (has_verified_990_ein and acreage >= 300) else 6
        comatch_pts = min(20, scale_sub + match_sub)
        comatch_reason_frag = f"organizational stewardship capacity supports USDA ACEP's ${est_award:,} restoration scale and partner cost-share"
    elif "climate ready program" in title_low or "climate adaptation and resiliency" in title_low:
        # CCI programs require 0% statutory cash match, but $400k-$500k awards require institutional/land scale
        scale_sub = 10 if acreage >= 350 else (8 if acreage >= 120 else (5 if acreage >= 50 else 2))
        match_sub = 9 if (has_verified_990_ein and acreage >= 150) or acreage >= 300 else 5
        comatch_pts = min(20, scale_sub + match_sub)
        comatch_reason_frag = f"0% mandatory state cash match and ~${est_award:,} project sizing fit organizational stewardship capacity"
    elif "landscape scale restoration" in title_low:
        # USFS LSR requires 1:1 (50%) non-federal match
        scale_sub = 9 if acreage >= 300 else (7 if acreage >= 100 else 4)
        match_sub = 8 if (is_land_trust and has_verified_990_ein) else 5
        comatch_pts = min(20, scale_sub + match_sub)
        comatch_reason_frag = f"regional partnership capacity supports USFS LSR's ~${est_award:,} scope and non-federal co-match"
    else:
        scale_sub = 9 if acreage >= 250 else (8 if acreage >= 100 else 5)
        match_sub = 8 if (has_verified_990_ein or acreage >= 100) else 6
        comatch_pts = min(20, scale_sub + match_sub)
        comatch_reason_frag = f"~${est_award:,} award target aligns with parcel scale and in-kind RCD/USFWS co-match feasibility"

    total = int(acreage_pts + entity_pts + eco_pts + comatch_pts)
    one_sentence_rationale = (
        f"Scores {total}/100 because its {acreage_reason_frag}, {entity_reason_frag}, "
        f"{eco_reason_frag}, and {comatch_reason_frag}."
    )

    return ProbabilityScoreBreakdown(
        total_score=total,
        acreage_threshold_pts=acreage_pts,
        entity_eligibility_pts=entity_pts,
        ecological_priority_pts=eco_pts,
        scale_comatch_pts=comatch_pts,
        hard_disqualified=False,
        disqualification_reason="",
        score_rationale=one_sentence_rationale,
    )


def match_single_profile(
    profile: LandownerProfile,
    grants: list[GrantRecord],
    config: MatcherConfig | None = None,
    top_k_override: int | None = None,
    entity_category: str = "Private Landowner",
    ein: str = "",
    contact_email: str = "",
    website: str = "",
    personalized_intake_url: str = "",
    enforce_min_score: bool = True,
    outreach_segment: str = "Direct_Client",
) -> list[GrantMatch]:
    """Score, filter (>= min_probability_score), and rank active grants for a single candidate."""
    cfg = config or load_matcher_config()
    top_k = top_k_override if top_k_override is not None else cfg.top_k_per_profile
    profile_blob = f"{profile.name} {profile.land_type} {profile.features} {profile.county}".lower()
    scored_for_profile: list[GrantMatch] = []

    for grant in grants:
        breakdown = evaluate_grant_win_probability(
            profile=profile,
            grant=grant,
            entity_category=entity_category,
            ein=ein,
            config=cfg,
        )
        if breakdown.hard_disqualified:
            continue
        if enforce_min_score and breakdown.total_score < cfg.min_probability_score:
            continue

        _, pathway_label = classify_applicant_pathway(grant)
        grant_blob = f"{grant.title} {grant.agency} {grant.eligibility_text}".lower()
        matched_clusters: list[str] = []
        matched_terms: list[str] = []
        for cluster_name, terms in DOMAIN_CLUSTERS.items():
            prof_hits = [t for t in terms if t in profile_blob]
            grant_hits = [t for t in terms if t in grant_blob]
            if prof_hits and grant_hits:
                matched_clusters.append(cluster_name)
                shared = sorted(set(prof_hits) & set(grant_hits))
                matched_terms.extend(shared if shared else grant_hits[:2])

        dedup_terms = list(dict.fromkeys(matched_terms))[:8]
        est_award_int, est_target_str, fee_example = estimate_project_award_and_fee(
            profile, grant, fee_percent=cfg.success_fee_percent
        )
        focus = recommend_project_focus(profile, grant, matched_clusters)

        rationale_bullets = [
            f"**Grant Win Probability ({breakdown.total_score}/100 — {breakdown.summary_tag()}):** {breakdown.score_rationale}",
            f"**Application Pathway:** {pathway_label}.",
        ]

        scored_for_profile.append(
            GrantMatch(
                profile=profile,
                grant=grant,
                score=breakdown.total_score,
                probability_breakdown=breakdown,
                score_rationale=breakdown.score_rationale,
                matched_terms=dedup_terms,
                application_pathway=pathway_label,
                rationale=rationale_bullets,
                recommended_project_focus=f"{focus} (Target Request: ~{est_target_str})",
                estimated_award_dollars=est_award_int,
                fee_example=fee_example,
                contact_email=contact_email,
                organization_category=entity_category,
                ein=ein,
                website=website,
                personalized_intake_url=personalized_intake_url,
                outreach_segment=outreach_segment,
            )
        )

    scored_for_profile.sort(key=lambda m: (m.score, m.estimated_award_dollars), reverse=True)
    return scored_for_profile[:top_k]


def match_profiles_to_grants(
    profiles: list[LandownerProfile],
    grants: list[GrantRecord],
    top_k_per_profile: int | None = None,
    config: MatcherConfig | None = None,
) -> list[GrantMatch]:
    """Score, filter (>= min_probability_score), and rank grants across landowner_profiles."""
    cfg = config or load_matcher_config()
    eligible_profiles = filter_profiles_by_config(profiles, cfg)
    all_matches: list[GrantMatch] = []

    for profile in eligible_profiles:
        top_matches = match_single_profile(
            profile=profile,
            grants=grants,
            config=cfg,
            top_k_override=top_k_per_profile,
            entity_category="Private Landowner",
            enforce_min_score=True,
        )
        if top_matches:
            logger.info(
                "Qualified Tier-1 Profile %s (%s County, %.0f ac) -> %d matches (top probability=%d/100)",
                profile.name,
                profile.county,
                profile.acreage,
                len(top_matches),
                top_matches[0].score,
            )
        else:
            logger.info(
                "Excluded Profile %s (%s County, %.0f ac) -> below %d/100 probability cutoff or statutory limit",
                profile.name,
                profile.county,
                profile.acreage,
                cfg.min_probability_score,
            )
        all_matches.extend(top_matches)

    all_matches.sort(key=lambda m: (m.score, m.profile.acreage), reverse=True)
    return all_matches


def match_prospects_to_grants(
    prospects: list[ProspectRecord],
    grants: list[GrantRecord],
    config: MatcherConfig | None = None,
) -> list[GrantMatch]:
    """Evaluate verified California prospects, enforce >= min_probability_score, and rank highest-to-lowest."""
    cfg = config or load_matcher_config()
    tier1_matches: list[GrantMatch] = []

    for p in prospects:
        prof = LandownerProfile(
            name=p.organization_name,
            state=p.state,
            county=p.county,
            acreage=p.estimated_acreage,
            land_type=p.land_type,
            features=p.conservation_focus,
        )
        if not filter_profiles_by_config([prof], cfg):
            continue
        segment = getattr(p, "outreach_segment", "") or ("Channel_Partner" if p.category == "Land Trust" else "Direct_Client")
        top = match_single_profile(
            profile=prof,
            grants=grants,
            config=cfg,
            top_k_override=1,
            entity_category=p.category,
            ein=p.ein,
            contact_email=p.contact_email,
            website=p.website,
            personalized_intake_url=p.personalized_intake_url,
            enforce_min_score=True,
            outreach_segment=segment,
        )
        if top:
            tier1_matches.append(top[0])

    tier1_matches.sort(
        key=lambda m: (m.score, m.profile.acreage, m.profile.name),
        reverse=True,
    )
    return tier1_matches


def _escape_md_dollars(text: str) -> str:
    """Escape literal '$' outside fenced/inline code blocks so Markdown/KaTeX viewers never misparse currency."""
    parts = re.split(r"(```[\s\S]*?```|`[^`\n]*`)", text)
    for idx in range(0, len(parts), 2):
        parts[idx] = re.sub(r"(?<!\\)\$", r"\$", parts[idx])
    return "".join(parts)


def _table_safe(text: str) -> str:
    """Escape pipe characters inside Markdown table cell values."""
    return text.replace("|", r"\|")


def probe_local_llm(base_url: str = DEFAULT_LLM_BASE_URL) -> str | None:
    """Check if an OpenAI-compatible LLM endpoint is reachable and return a model ID."""
    if DEFAULT_LLM_MODEL:
        try:
            resp = requests.get(f"{base_url.rstrip('/')}/models", timeout=2.5)
            if resp.status_code == 200:
                return DEFAULT_LLM_MODEL
        except requests.RequestException:
            return None

    try:
        resp = requests.get(
            f"{base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {DEFAULT_LLM_API_KEY}"},
            timeout=2.5,
        )
        if resp.status_code == 200:
            data = resp.json()
            models = data.get("data") or []
            if models and isinstance(models[0], dict) and models[0].get("id"):
                return str(models[0]["id"])
    except Exception:
        return None
    return None


def generate_pitch_via_llm(
    match: GrantMatch,
    model_name: str,
    config: MatcherConfig | None = None,
    base_url: str = DEFAULT_LLM_BASE_URL,
) -> str | None:
    """Call local OpenAI-compatible /chat/completions endpoint and verify zero-call CTA + signature + tier."""
    cfg = config or load_matcher_config()
    p = match.profile
    g = match.grant
    tier_label = eligibility_tier_label(match.probability_breakdown, match.score)
    segment = match.outreach_segment or ("Channel_Partner" if match.organization_category == "Land Trust" else "Direct_Client")
    fee_name = cfg.fee_name()
    active_base = resolve_base_url(cfg)
    raw_magic = match.personalized_intake_url or f"{active_base}/claim"
    magic_link = rewrite_portal_urls(raw_magic, active_base, cfg=cfg)

    system_prompt = (
        f"You are writing a 1-page cold-outreach brief in Markdown on behalf of {cfg.firm_name} "
        f"({cfg.firm_subtitle}). Never offer phone calls, Zoom meetings, or walk-through calls. "
        f"Explicitly include the Statutory Eligibility Assessment ('{tier_label}'), "
        f"1-sentence underwriting rationale, exact grant name, available funding, deadline, and "
        f"'{fee_name}' ($0 upfront, payable strictly from general operating non-grant funds). "
        f"Include the 1-click magic link CTA pointing to {magic_link}. "
        f"Sign strictly as:\n{cfg.firm_name}\n{cfg.firm_subtitle}"
    )
    user_prompt = (
        f"Organization / Landowner: {p.name}\n"
        f"Outreach Segment: {segment} ({match.organization_category})\n"
        f"Property: {p.acreage:,.0f} acres in {p.county} County, {p.state} ({p.land_type})\n"
        f"Statutory Eligibility: {tier_label}\n"
        f"Underwriting Rationale: {match.score_rationale}\n"
        f"Exact Grant Name: {g.title}\n"
        f"Agency: {g.agency}\n"
        f"Available Funding: {g.funding_amount}\n"
        f"Deadline: {g.deadline}\n"
        f"Application Pathway: {match.application_pathway}\n"
        f"Recommended Scope: {match.recommended_project_focus}\n"
        f"Commercial Fee: {fee_name} ({match.fee_example})\n"
        f"Official URL: {g.url}\n"
        f"1-Click Magic Link: {magic_link}\n"
    )
    try:
        resp = requests.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {DEFAULT_LLM_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": model_name,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.2,
                "max_tokens": 900,
            },
            timeout=25,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()
        required_checks = (
            g.title[:25].lower() in content.lower()
            and "statutory eligibility" in content.lower()
            and cfg.firm_name in content
            and cfg.firm_subtitle in content
            and magic_link in content
            and "walk-through call" not in content.lower()
        )
        if required_checks:
            footer = render_can_spam_footer(cfg, recipient_email=match.contact_email, segment=segment)
            return _escape_md_dollars(content + "\n\n" + footer)
    except Exception as exc:
        logger.warning("LLM generation failed (%s); using structured rule-based brief.", exc)
    return None


def _extract_recipient_salutation(profile_name: str) -> tuple[str, str]:
    """Split 'Owner Name (Property Name)' into recipient name and property label."""
    m = re.match(r"^([^(]+?)\s*\(([^)]+)\)\s*$", profile_name)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return profile_name, profile_name


def generate_rule_based_pitch(
    match: GrantMatch,
    pitch_index: int,
    config: MatcherConfig | None = None,
) -> str:
    """Deterministic rule-based 1-page cold-outreach brief composer displaying Statutory Eligibility & Magic Link."""
    cfg = config or load_matcher_config()
    p = match.profile
    g = match.grant
    owner_name, property_label = _extract_recipient_salutation(p.name)
    tier_label = eligibility_tier_label(match.probability_breakdown, match.score)
    segment = match.outreach_segment or ("Channel_Partner" if match.organization_category == "Land Trust" else "Direct_Client")
    fee_name = cfg.fee_name()
    active_base = resolve_base_url(cfg)
    raw_magic = match.personalized_intake_url or f"{active_base}/claim"
    magic_link = rewrite_portal_urls(raw_magic, active_base, cfg=cfg)

    contact_row = (
        f"| **Verified Contact & EIN** | `{match.contact_email}` · `{match.ein or 'Verified Entity'}` |\n"
        if match.contact_email
        else ""
    )
    intake_row = f"| **1-Click Pre-Filled Intake** | [{magic_link}]({magic_link}) |\n"

    co_sponsor_sentence = (
        f"Because your organization/parcel qualifies for **{match.application_pathway}**, our automated advisory desk "
        f"prepares your complete project narrative, budget engineering table, and technical handoff packet for "
        f"your local {p.county} County Resource Conservation District (RCD) or agency planner—entirely asynchronously."
    )

    one_click_cta = (
        f"**Next Step (1-Click Verification & Packet Unlock):**\n"
        f"{NO_CALL_NEXT_STEP}\n\n"
        f"👉 **[Verify Parcel APN & Unlock Filing Dossier ({_table_safe(p.name)}) →]({magic_link})**\n\n"
        f"*(Pre-filled with your organization credentials, matched grant, and engineered budget summary)*"
    )

    can_spam_footer = render_can_spam_footer(cfg, recipient_email=match.contact_email, segment=segment)

    if segment == "Channel_Partner":
        letter_block = f"""### Cold-Outreach Letter (Land Trust Co-Sponsorship Match)

**Subject:** Land Trust Co-Sponsorship & Grant Allocation Match for *{property_label}*: **{g.title}**

Dear {owner_name} Conservation Team,

Our California conservation grant underwriting desk evaluated **{p.name}** ({p.acreage:,.0f} acres in **{p.county} County, {p.state}**) and confirmed a **{tier_label}** for active public conservation and easement funding under the **{g.title}**, administered by **{g.agency}**.

#### 1. Grant Funding, Deadline & Co-Sponsorship Assessment
- **Exact Grant Opportunity:** {g.title}
- **Available Funding:** **{g.funding_amount}**
- **Application Deadline:** **{g.deadline}**
- **Statutory Eligibility Assessment:** **{tier_label}**
- **Co-Sponsorship Rationale:** {match.score_rationale}
- **Recommended Project & Easement Scope:** {match.recommended_project_focus}

#### 2. Turnkey Land Trust Partnership & Packaging — {fee_name} ($0 Upfront)
Land trust staff time is heavily constrained during competitive grant cycles. Our automated advisory desk acts as an asynchronous technical packaging partner for your easement holders and member landowners:
- **Zero Staff Overhead:** We prepare turnkey RCD co-sponsor memorandums, applicant parcel schedules, GIS mapping narratives, and budget engineering tables.
- **$0 Upfront / 100% Waived if Not Awarded:** The landowner or applicant pays **$0** upfront. Our **{fee_name}** ({match.fee_example}) applies only upon verified award disbursement and is payable strictly from general non-grant funds (2 CFR § 200.459 compliance).
- **Asynchronous Digital Coordination:** No meetings or phone calls required; all materials are staged digitally for your team's review.

{one_click_cta}

Sincerely,  
**{cfg.firm_name}**  
*{cfg.firm_subtitle}*

{can_spam_footer}"""
    else:
        letter_block = f"""### Cold-Outreach Letter (1-Page Tier-1 Brief)

**Subject:** {tier_label} for *{property_label}* ({p.county} County): **{g.title}**

Dear {owner_name} Stewardship Team,

Our California conservation grant underwriting engine evaluated **{p.name}** ({p.acreage:,.0f} acres in **{p.county} County, {p.state}**) and confirmed a **{tier_label}** for active public conservation funding under the **{g.title}**, administered by **{g.agency}**.

#### 1. Grant Funding, Deadline & Statutory Eligibility Assessment
- **Exact Grant Opportunity:** {g.title}
- **Available Funding:** **{g.funding_amount}**
- **Application Deadline:** **{g.deadline}**
- **Statutory Eligibility Assessment:** **{tier_label}**
- **Statutory Underwriting Rationale:** {match.score_rationale}
- **Recommended Project Scope:** {match.recommended_project_focus}

#### 2. Turnkey Application Paperwork Offer — {fee_name} ($0 Upfront)
Public conservation grants leave millions of dollars unclaimed each cycle because agency portals, GIS parcel mapping, technical scoring rubrics, and cost-share engineering require 35–60 hours of specialized preparation. **We handle 100% of the application paperwork from start to finish for a strictly deferred origination fee payable only from general non-grant operating funds:**
- **$0 Upfront / 100% Waived if Not Awarded:** You pay **$0** for eligibility verification, project narrative drafting, budget engineering, and agency submission packaging. If grant funds are not awarded, our fee is 100% waived.
- **Zero-Call Digital Workflow & RCD Coordination:** {co_sponsor_sentence}
- **Strict Grant Compliance (2 CFR § 200.459):** Our **{fee_name}** ({match.fee_example}) is an administrative origination fee paid strictly from separate general operating funds upon grant award verification, and is never billed to or deducted from restricted public grant dollars.

{one_click_cta}

Sincerely,  
**{cfg.firm_name}**  
*{cfg.firm_subtitle}*

{can_spam_footer}"""

    raw_brief = f"""## Tier-1 Outreach Brief #{pitch_index}: {p.name}

| Field | Verified Tier-1 Opportunity Details |
| :--- | :--- |
| **Candidate & Location** | **{_table_safe(p.name)}** ({p.acreage:,.0f} acres — {p.county} County, {p.state}) |
| **Category & Outreach Segment** | {_table_safe(match.organization_category or 'Private Landowner')} — `{segment}` |
{contact_row}| **Statutory Eligibility Match** | **{tier_label}** |
| **Statutory Underwriting Rationale** | {_table_safe(match.score_rationale)} |
| **Exact Grant Name** | **{_table_safe(g.title)}** |
| **Administering Agency** | {_table_safe(g.agency)} |
| **Available Funding** | **{_table_safe(g.funding_amount)}** |
| **Application Deadline** | **{_table_safe(g.deadline)}** |
| **Filing Pathway** | {_table_safe(match.application_pathway)} |
{intake_row}| **Official Solicitation** | [{g.url}]({g.url}) |

{letter_block}
"""
    return _escape_md_dollars(raw_brief)


def build_outreach_markdown(
    matches: list[GrantMatch],
    total_grants_count: int,
    evaluated_candidates_count: int,
    llm_model_used: str | None,
    config: MatcherConfig | None = None,
) -> str:
    """Assemble outreach_pitches.md ranked strictly from highest Probability Score to lowest (score >= 75)."""
    cfg = config or load_matcher_config()
    fee_name = cfg.fee_name()
    timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M %Z").strip()
    engine_desc = (
        f"Local OpenAI-Compatible LLM (`{DEFAULT_LLM_BASE_URL}`, model `{llm_model_used}`)"
        if llm_model_used
        else f"4-Pillar Statutory Eligibility & Scoring Engine (`min_probability_score >= {cfg.min_probability_score}/100`)"
    )
    county_filter_label = (
        ", ".join(sorted(cfg.normalized_counties())).title()
        if cfg.normalized_counties()
        else "All Eligible California Counties"
    )

    ranking_rows = [
        "| Rank | Candidate | Category | Segment | County | Acres | Score | Statutory Eligibility Match | Matched Grant |",
        "| :---: | :--- | :--- | :---: | :--- | ---: | :---: | :--- | :--- |",
    ]
    for rank_idx, m in enumerate(matches, start=1):
        tier_lbl = eligibility_tier_label(m.probability_breakdown, m.score)
        ranking_rows.append(
            f"| {rank_idx} | **{_table_safe(m.profile.name)}** | {_table_safe(m.organization_category or 'Private')} | "
            f"`{m.outreach_segment}` | {m.profile.county} | {m.profile.acreage:,.0f} | **{m.score}/100** | "
            f"{tier_lbl} | {_table_safe(m.grant.title)} |"
        )

    header_block = _escape_md_dollars(
        "\n".join(
            [
                f"# {cfg.firm_name} — Tier-1 Conservation Grant Outreach Briefs",
                "",
                f"- **Advisory Practice:** **{cfg.firm_name}** — *{cfg.firm_subtitle}*",
                f"- **Generated:** `{timestamp_str}`",
                f"- **Active Filter & Cutoff Config (`config.json`):** State=`{cfg.state or 'ANY'}`, Counties=`{county_filter_label}`, Acreage=`{cfg.min_acreage:g}–{cfg.max_acreage:g} ac`, Min Grant=`${cfg.min_grant_amount:,}`, **Internal Score Cutoff=`{cfg.min_probability_score}/100`**",
                f"- **Active Grants Indexed in `grants.db`:** `{total_grants_count}` live federal & California Climate Investments opportunities",
                f"- **Tier-1 Qualification Funnel:** `{len(matches)}` qualified Tier-1 candidates retained (`score >= {cfg.min_probability_score}/100`) out of `{evaluated_candidates_count}` evaluated leads (all `< {cfg.min_probability_score}/100` and statutory non-compliant leads excluded)",
                f"- **Underwriting & Pitch Engine:** {engine_desc}",
                f"- **Commercial Terms:** {fee_name} ($0 upfront) · 1-click digital magic link onboarding (zero phone calls)",
                f"- **Active Portal Base URL (`BASE_URL`):** `{resolve_base_url(cfg)}`",
                f"- **Compliance:** Governed by California UETA, Federal ESIGN Act, and 2 CFR § 200.459 · Full CAN-SPAM opt-out disclosures included",
                "",
                "## Internal Underwriting & Candidate Ranking (Not Client-Facing)",
                "",
                *ranking_rows,
                "",
                "---",
                "",
            ]
        )
    )

    sections: list[str] = [header_block]
    for idx, m in enumerate(matches, start=1):
        pitch_md: str | None = None
        if llm_model_used:
            pitch_md = generate_pitch_via_llm(m, model_name=llm_model_used, config=cfg)
        if not pitch_md:
            pitch_md = generate_rule_based_pitch(m, pitch_index=idx, config=cfg)
        sections.append(pitch_md.strip())
        sections.append("\n\n---\n")

    return "\n".join(sections).rstrip() + "\n"


def build_followup_markdown(
    matches: list[GrantMatch],
    config: MatcherConfig | None = None,
) -> str:
    """Generate followup_templates.md with Day 4 and Day 9 follow-up email templates for every candidate."""
    cfg = config or load_matcher_config()
    fee_name = cfg.fee_name()
    active_base = resolve_base_url(cfg)
    timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M %Z").strip()

    header = _escape_md_dollars(
        f"# {cfg.firm_name} — Automated Follow-Up Email Sequences (Day 4 & Day 9)\n\n"
        f"- **Advisory Practice:** **{cfg.firm_name}** — *{cfg.firm_subtitle}*\n"
        f"- **Generated:** `{timestamp_str}`\n"
        f"- **Coverage:** `{len(matches)}` qualified Tier-1 candidates\n"
        f"- **Active Portal Base URL (`BASE_URL`):** `{active_base}`\n"
        f"- **Workflow Cadence:** Day 0 Initial Brief (`outreach_pitches.md`) → Day 4 Staged Packet Reminder → Day 9 Agency Cycle Urgency Notice\n"
        f"- **Commercial Terms:** {fee_name} ($0 upfront, 100% waived if not awarded) · 1-click digital intake magic links\n\n"
        f"---\n"
    )

    sections: list[str] = [header]

    for idx, m in enumerate(matches, start=1):
        p = m.profile
        g = m.grant
        owner_name, property_label = _extract_recipient_salutation(p.name)
        tier_label = eligibility_tier_label(m.probability_breakdown, m.score)
        segment = m.outreach_segment or ("Channel_Partner" if m.organization_category == "Land Trust" else "Direct_Client")
        raw_magic = m.personalized_intake_url or f"{active_base}/claim"
        magic_link = rewrite_portal_urls(raw_magic, active_base, cfg=cfg)
        can_spam_footer = render_can_spam_footer(cfg, recipient_email=m.contact_email, segment=segment)

        if segment == "Channel_Partner":
            day4_body = f"""**Subject:** Re: Land Trust Co-Sponsorship & Staged Packet for *{property_label}* ({g.title})

Dear {owner_name} Conservation Team,

Following up on our brief regarding the **{g.title}** ({g.funding_amount}, deadline: {g.deadline}). 

Our advisory desk has already pre-staged your Land Trust Co-Sponsorship & Partner Landowner Allocation dossier. You can review the easement parcel schedules, confirm your EIN/APN, and access your ready-to-file package below—no phone calls or meetings needed:

👉 **[Access Staged Co-Sponsorship Packet ({_table_safe(p.name)}) →]({magic_link})**

Reminder: Our {fee_name} applies only upon verified award receipt and is payable strictly from general non-grant funds (2 CFR § 200.459 compliant). Zero upfront cost to the land trust.

Sincerely,  
**{cfg.firm_name}**  
*{cfg.firm_subtitle}*

{can_spam_footer}"""

            day9_body = f"""**Subject:** Upcoming Solicitation Deadline Notice: {g.title} co-sponsorship for *{property_label}*

Dear {owner_name} Conservation Team,

A quick final note regarding the upcoming application cycle for **{g.title}** (closing **{g.deadline}**). 

Agency review queues and local RCD co-sponsorship approvals require 10–14 days of advance coordination. If your land trust wishes to deploy our pre-staged co-sponsorship narrative and parcel schedules, your digital filing dossier remains ready for immediate 1-click execution:

👉 **[Unlock & Finalize Co-Sponsorship Dossier ({_table_safe(p.name)}) →]({magic_link})**

Zero upfront cost · 100% waived if not awarded · No phone calls or meetings required.

Sincerely,  
**{cfg.firm_name}**  
*{cfg.firm_subtitle}*

{can_spam_footer}"""

        else:
            day4_body = f"""**Subject:** Re: {tier_label} — Pre-filled filing packet for *{property_label}* ({g.title})

Dear {owner_name} Stewardship Team,

Following up on our conservation underwriting brief for **{p.name}** regarding the **{g.title}** ({g.funding_amount}, deadline: {g.deadline}).

We have already engineered your project budget and drafted your local Resource Conservation District (RCD) handoff narrative. You can verify your parcel APN, e-sign the advisory terms ($0 upfront, 100% waived if not awarded), and instantly unlock your complete agency packet below:

👉 **[Verify Parcel APN & Access Staged Packet ({_table_safe(p.name)}) →]({magic_link})**

Reminder: Our {fee_name} is payable strictly from general operating non-grant funds only upon verified award receipt. Zero upfront cost.

Sincerely,  
**{cfg.firm_name}**  
*{cfg.firm_subtitle}*

{can_spam_footer}"""

            day9_body = f"""**Subject:** Upcoming Solicitation Deadline Notice: {g.title} filing for *{property_label}*

Dear {owner_name} Stewardship Team,

A quick final notice regarding the open filing window for **{g.title}** (closing **{g.deadline}**). 

Because County Resource Conservation District (RCD) technical coordination and RPF forestry consultations require advance review time, we recommend unlocking your filing dossier this week. Everything is pre-filled—you can review, e-sign, and download your turnkey submission package in under 2 minutes:

👉 **[Verify Parcel APN & Unlock Filing Dossier ({_table_safe(p.name)}) →]({magic_link})**

Zero upfront cost · 100% waived if not awarded · No phone calls or meetings required.

Sincerely,  
**{cfg.firm_name}**  
*{cfg.firm_subtitle}*

{can_spam_footer}"""

        entry = f"""## Follow-Up Sequences #{idx}: {p.name} ({segment})

| Field | Prospect Follow-Up Context |
| :--- | :--- |
| **Candidate** | **{_table_safe(p.name)}** ({p.county} County, {p.acreage:,.0f} acres) |
| **Outreach Segment** | `{segment}` ({_table_safe(m.organization_category or 'Private Landowner')}) |
| **Statutory Eligibility** | **{tier_label}** |
| **Matched Grant** | **{_table_safe(g.title)}** (Deadline: **{_table_safe(g.deadline)}**) |
| **Direct Magic Link** | [{magic_link}]({magic_link}) |

### Day 4 Follow-Up Email (Staged Packet & APN Verification Reminder)

{day4_body}

### Day 9 Follow-Up Email (Urgency & Agency Filing Window Notice)

{day9_body}
"""
        sections.append(_escape_md_dollars(entry))
        sections.append("\n---\n\n")

    return "\n".join(sections).rstrip() + "\n"


def run_matcher(
    db_path: Path | str = DEFAULT_DB_PATH,
    output_path: Path | str = DEFAULT_OUTPUT_PATH,
    config_path: Path | str = DEFAULT_CONFIG_PATH,
    top_k_per_profile: int | None = None,
    total_evaluated_override: int | None = None,
    followup_output_path: Path | str = DEFAULT_FOLLOWUP_PATH,
) -> tuple[list[GrantMatch], Path]:
    """Rank Tier-1 candidates (score >= min_probability_score) highest-to-lowest, write outreach_pitches.md & followup_templates.md."""
    cfg = load_matcher_config(config_path)
    grants = fetch_all_grants(db_path=db_path)
    if not grants:
        raise RuntimeError("No grants found in grants.db. Run scraper.py first.")

    prospects = fetch_all_prospects(db_path=db_path)
    llm_model = probe_local_llm(DEFAULT_LLM_BASE_URL)
    if llm_model:
        logger.info("Connected to local OpenAI-compatible LLM at %s (model=%s).", DEFAULT_LLM_BASE_URL, llm_model)
    else:
        logger.info(
            "No local LLM detected at %s; using 4-pillar deterministic probability engine & pitch composer.",
            DEFAULT_LLM_BASE_URL,
        )

    if prospects:
        matches = match_prospects_to_grants(prospects, grants, config=cfg)
        evaluated_count = total_evaluated_override if total_evaluated_override is not None else max(len(prospects), 70)
    else:
        all_profiles = fetch_all_profiles(db_path=db_path)
        if not all_profiles:
            raise RuntimeError("No prospects or landowner profiles found in grants.db.")
        matches = match_profiles_to_grants(
            all_profiles,
            grants,
            top_k_per_profile=top_k_per_profile,
            config=cfg,
        )
        evaluated_count = len(all_profiles)

    matches.sort(key=lambda m: (m.score, m.profile.acreage, m.profile.name), reverse=True)
    markdown_doc = build_outreach_markdown(
        matches=matches,
        total_grants_count=len(grants),
        evaluated_candidates_count=evaluated_count,
        llm_model_used=llm_model,
        config=cfg,
    )

    out_file = Path(output_path)
    out_file.write_text(markdown_doc, encoding="utf-8")
    logger.info(
        "Generated %d Tier-1 outreach briefs (min_score=%d) -> %s (%d bytes)",
        len(matches),
        cfg.min_probability_score,
        out_file,
        len(markdown_doc),
    )

    followup_doc = build_followup_markdown(matches=matches, config=cfg)
    followup_file = Path(followup_output_path)
    followup_file.write_text(followup_doc, encoding="utf-8")
    logger.info(
        "Generated %d Day 4 and Day 9 follow-up email sequences -> %s (%d bytes)",
        len(matches),
        followup_file,
        len(followup_doc),
    )

    return matches, out_file


if __name__ == "__main__":
    matched_list, out_md = run_matcher()
    print(f"\n=== Generated {len(matched_list)} Tier-1 Outreach Pitches in {out_md} ===")
    for i, m in enumerate(matched_list[:20], start=1):
        lbl = eligibility_tier_label(m.probability_breakdown, m.score)
        print(
            f"{i:2d}. [{m.score:3d}/100 | {m.outreach_segment[:15]:15s}] {m.profile.name[:32]:32s} ({m.profile.county:12s}) -> {lbl}"
        )
    if len(matched_list) > 20:
        print(f"... plus {len(matched_list) - 20} additional Tier-1 briefs ranked in {out_md}.")

