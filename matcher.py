"""Match landowner profiles against active grants in grants.db and generate outreach_pitches.md."""

from __future__ import annotations

from datetime import datetime
import json
import logging
import os
from pathlib import Path
import re

import requests

from db import DEFAULT_DB_PATH, fetch_all_grants, fetch_all_profiles
from models import GrantMatch, GrantRecord, LandownerProfile, MatcherConfig

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

NO_CALL_NEXT_STEP = (
    "Reply 'READY' to receive our 2-minute digital property intake link—no phone calls or meetings required."
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
    "del norte",
    "humboldt",
    "mendocino",
    "sonoma",
    "marin",
    "napa",
    "solano",
    "contra costa",
    "alameda",
    "santa clara",
    "san mateo",
    "san francisco",
    "santa cruz",
    "monterey",
    "san luis obispo",
    "santa barbara",
    "ventura",
    "los angeles",
    "orange",
    "san diego",
}

DOMAIN_CLUSTERS: dict[str, tuple[str, ...]] = {
    "forest & oak woodland": (
        "forest",
        "reforestation",
        "woodland",
        "oak",
        "timber",
        "conifer",
        "pine",
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
    """Load and validate config.json (falling back to defaults if absent)."""
    cfg_file = Path(config_path)
    if not cfg_file.exists():
        cfg = MatcherConfig()
        cfg_file.write_text(json.dumps(cfg.model_dump(), indent=2) + "\n", encoding="utf-8")
        return cfg
    raw = json.loads(cfg_file.read_text(encoding="utf-8"))
    return MatcherConfig(**raw)


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
    """Verify state, regional, coastal vs. inland, and county-level geographic eligibility."""
    combined = f"{grant.title} {grant.agency} {grant.eligibility_text}".lower()
    state_full = STATE_NAME_MAP.get(profile.state.upper(), profile.state.lower())

    if state_full == "california":
        for marker in OUT_OF_REGION_MARKERS:
            if marker in combined:
                return False, f"Out-of-region restriction ({marker})"
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
            return False, f"{profile.county} County is not in program's eligible county list"

    return True, "Geographically eligible"


def check_acreage_eligibility(profile: LandownerProfile, grant: GrantRecord) -> tuple[bool, str | None]:
    """Parse acreage minimums/maximums from eligibility_text (e.g. 'between 20 and 5,000 acres')."""
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
                f"Parcel acreage ({profile.acreage:.0f} ac) outside required {min_acres:,.0f}-{max_acres:,.0f} ac range",
            )
        return (
            True,
            f"Parcel size ({profile.acreage:.0f} acres) satisfies statutory {min_acres:,.0f}-{max_acres:,.0f} acre requirement",
        )
    return True, None


def classify_applicant_pathway(grant: GrantRecord) -> tuple[bool, str]:
    """Determine whether a private landowner can apply directly or via local RCD/Land Trust partner."""
    title_low = grant.title.lower()
    elig_low = grant.eligibility_text.lower()

    disqualifying_title_terms = (
        "for tribes",
        "tribal colleges",
        "hispanic-serving institutions",
        "non-land-grant colleges",
        "cesu",
        "cooperative ecosystems studies units",
        "state ans management",
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
    )
    if any(term in title_low for term in disqualifying_title_terms):
        return False, "Restricted institutional/government-only program"

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
            "Land Trust / Resource Conservation District (RCD) Co-Application (Agricultural Conservation Easement)",
        )

    if any(m in elig_low for m in direct_markers):
        return True, "Direct Private Landowner / Rancher Application"

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
            "Sponsored Co-Application via Local Resource Conservation District (RCD) / Land Trust Partner",
        )

    return False, "No clear private-landowner or RCD co-sponsor pathway"


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
    elif "fish passage" in title_low:
        est_award = 185000 if acreage >= 50 else 125000
    elif "landscape scale restoration" in title_low:
        est_award = 150000
    elif "easement restoration and stewardship" in title_low:
        est_award = 250000
    elif "watersmart cooperative watershed" in title_low:
        est_award = 120000
    elif "watersmart enhancing water" in title_low:
        est_award = 175000
    elif "cooperative agriculture" in title_low:
        est_award = 45000
    else:
        est_award = max(50000, min(250000, int(round(acreage * 1500, -3))))

    fee_rate = fee_percent / 100.0
    fee_dollars = int(round(est_award * fee_rate))
    net_dollars = est_award - fee_dollars
    pct_str = f"{int(fee_percent)}%" if float(fee_percent).is_integer() else f"{fee_percent:g}%"
    fee_summary = (
        f"On an estimated ${est_award:,} project award, our {pct_str} success fee is ${fee_dollars:,} "
        f"(netting ${net_dollars:,} directly to your property conservation project; $0 owed if not awarded)"
    )
    return est_award, f"${est_award:,}", fee_summary


def recommend_project_focus(profile: LandownerProfile, grant: GrantRecord, matched_clusters: list[str]) -> str:
    """Summarize the tailored conservation scope aligning the landowner parcel with the grant."""
    title_low = grant.title.lower()
    if "california forest improvement" in title_low:
        return (
            f"Registered Professional Forester (RPF) Forest Management Plan, post-drought oak/conifer "
            f"reforestation, brush fuels thinning, and erosion stabilization across all {profile.acreage:.0f} acres"
        )
    if "sustainable agricultural lands conservation" in title_low:
        return (
            f"Permanent Agricultural Conservation Easement and climate-smart ranch conservation plan "
            f"protecting your {profile.acreage:.0f}-acre {profile.county} County working ranch"
        )
    if "climate adaptation and resiliency" in title_low:
        return (
            f"Natural and working lands climate resilience implementation ({', '.join(matched_clusters[:2]) or 'habitat & soil conservation'}) "
            f"on your {profile.acreage:.0f}-acre {profile.county} County parcel"
        )
    if "fish passage" in title_low:
        return (
            f"Instream agricultural culvert/barrier removal, stream-crossing upgrade, and native riparian "
            f"aquatic wildlife habitat connectivity on your {profile.county} County watercourse"
        )
    if "landscape scale restoration" in title_low:
        return (
            f"Non-federal forest watershed restoration, native oak/pine reforestation, wildfire fuel reduction, "
            f"and wildlife habitat enhancement via a {profile.county} County / Sierra RCD co-sponsored block grant"
        )
    if "watersmart" in title_low:
        return (
            f"Riparian corridor water-efficiency modernization, solar-assisted conservation irrigation, "
            f"and watershed habitat enhancement in {profile.county} County"
        )
    if "easement restoration" in title_low:
        return (
            "Wetland and floodplain riparian easement restoration, streambank stabilization, and soil health stewardship"
        )
    return (
        f"Site-specific {', '.join(matched_clusters[:2]) or 'conservation & habitat'} improvements "
        f"across your {profile.acreage:.0f}-acre {profile.county} County property"
    )


def match_single_profile(
    profile: LandownerProfile,
    grants: list[GrantRecord],
    config: MatcherConfig | None = None,
    top_k_override: int | None = None,
) -> list[GrantMatch]:
    """Score and rank active grants for a single LandownerProfile respecting config.json."""
    cfg = config or load_matcher_config()
    top_k = top_k_override if top_k_override is not None else cfg.top_k_per_profile
    profile_blob = f"{profile.land_type} {profile.features} {profile.county} {profile.state}".lower()
    scored_for_profile: list[GrantMatch] = []

    for grant in grants:
        if cfg.min_grant_amount > 0:
            max_dollars = extract_max_grant_dollars(grant.funding_amount)
            if max_dollars is not None and max_dollars < cfg.min_grant_amount:
                continue

        geo_ok, geo_note = check_geographic_eligibility(profile, grant)
        if not geo_ok:
            logger.debug("Skipping %s for %s: %s", grant.id, profile.name, geo_note)
            continue

        acre_ok, acre_note = check_acreage_eligibility(profile, grant)
        if not acre_ok:
            logger.info("Acreage filter excluded [%s] for %s: %s", grant.id, profile.name, acre_note)
            continue

        pathway_ok, pathway_label = classify_applicant_pathway(grant)
        if not pathway_ok:
            continue

        grant_blob = f"{grant.title} {grant.agency} {grant.eligibility_text}".lower()
        score = 0.0
        matched_terms: list[str] = []
        matched_clusters: list[str] = []
        rationale: list[str] = []

        for cluster_name, terms in DOMAIN_CLUSTERS.items():
            prof_hits = [t for t in terms if t in profile_blob]
            grant_hits = [t for t in terms if t in grant_blob]
            if prof_hits and grant_hits:
                shared = sorted(set(prof_hits) & set(grant_hits))
                cluster_pts = 14.0 + 4.0 * min(3, len(shared) or 1)
                score += cluster_pts
                matched_clusters.append(cluster_name)
                matched_terms.extend(shared if shared else grant_hits[:2])

        if not matched_clusters:
            continue

        rationale.append(f"Strong ecological alignment across {', '.join(matched_clusters)}.")

        if "california forest improvement" in grant.title.lower() or "landscape scale restoration" in grant.title.lower():
            if not any(w in profile_blob for w in ("oak", "forest", "woodland", "pine", "conifer", "timber")):
                continue
            score += 18.0

        if "sustainable agricultural lands conservation" in grant.title.lower():
            if not any(w in profile_blob for w in ("ranch", "agricultural", "pasture", "cattle", "farm")):
                continue
            score += 20.0

        if profile.state.upper() == "CA" and "california" in grant_blob:
            score += 18.0
            rationale.append(
                f"Dedicated California state conservation funding applicable to {profile.county} County."
            )
        if profile.county.lower() in grant_blob:
            score += 10.0
            rationale.append(f"Explicitly references {profile.county} regional watershed/jurisdiction.")

        if acre_note:
            score += 16.0
            rationale.append(acre_note + ".")

        if "Direct Private Landowner" in pathway_label:
            score += 15.0
            rationale.append("Private landowners/individuals are directly eligible applicants.")
        else:
            score += 6.0
            rationale.append(f"Accessible via {pathway_label}.")

        dedup_terms = list(dict.fromkeys(matched_terms))[:8]
        est_award_int, est_target_str, fee_example = estimate_project_award_and_fee(
            profile, grant, fee_percent=cfg.success_fee_percent
        )
        focus = recommend_project_focus(profile, grant, matched_clusters)

        scored_for_profile.append(
            GrantMatch(
                profile=profile,
                grant=grant,
                score=min(99.0, round(score, 1)),
                matched_terms=dedup_terms,
                application_pathway=pathway_label,
                rationale=rationale,
                recommended_project_focus=f"{focus} (Target Request: ~{est_target_str})",
                estimated_award_dollars=est_award_int,
                fee_example=fee_example,
            )
        )

    scored_for_profile.sort(key=lambda m: m.score, reverse=True)
    return scored_for_profile[:top_k]


def match_profiles_to_grants(
    profiles: list[LandownerProfile],
    grants: list[GrantRecord],
    top_k_per_profile: int | None = None,
    config: MatcherConfig | None = None,
) -> list[GrantMatch]:
    """Score and rank active grants across all config-eligible landowner profiles."""
    cfg = config or load_matcher_config()
    eligible_profiles = filter_profiles_by_config(profiles, cfg)
    all_matches: list[GrantMatch] = []

    for profile in eligible_profiles:
        top_matches = match_single_profile(
            profile=profile,
            grants=grants,
            config=cfg,
            top_k_override=top_k_per_profile,
        )
        logger.info(
            "Matched %s (%s County, %.0f ac) -> %d top active grants (best score=%.1f)",
            profile.name,
            profile.county,
            profile.acreage,
            len(top_matches),
            top_matches[0].score if top_matches else 0.0,
        )
        all_matches.extend(top_matches)

    return all_matches


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
    """Call local OpenAI-compatible /chat/completions endpoint and verify zero-call CTA + signature."""
    cfg = config or load_matcher_config()
    fee_str = cfg.fee_label()
    p = match.profile
    g = match.grant
    system_prompt = (
        f"You are writing a 1-page cold-outreach brief in Markdown on behalf of {cfg.firm_name} "
        f"({cfg.firm_subtitle}). Never offer phone calls, Zoom meetings, or walk-through calls. "
        f"State the exact grant name, available funding, deadline, and {fee_str} contingent success fee "
        f"($0 upfront). End with the exact Next Step: \"{NO_CALL_NEXT_STEP}\" and sign strictly as:\n"
        f"{cfg.firm_name}\n{cfg.firm_subtitle}"
    )
    user_prompt = (
        f"Landowner: {p.name}\n"
        f"Property: {p.acreage:.0f} acres in {p.county} County, {p.state} ({p.land_type})\n"
        f"Property Features & Goals: {p.features}\n"
        f"Exact Grant Name: {g.title}\n"
        f"Agency: {g.agency}\n"
        f"Available Funding: {g.funding_amount}\n"
        f"Deadline: {g.deadline}\n"
        f"Application Pathway: {match.application_pathway}\n"
        f"Recommended Scope: {match.recommended_project_focus}\n"
        f"{fee_str} Success Fee Math: {match.fee_example}\n"
        f"Official URL: {g.url}\n"
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
            and fee_str in content
            and NO_CALL_NEXT_STEP in content
            and cfg.firm_name in content
            and cfg.firm_subtitle in content
            and "walk-through call" not in content.lower()
        )
        if required_checks:
            return _escape_md_dollars(content)
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
    """Deterministic rule-based 1-page cold-outreach brief composer with 100% digital onboarding."""
    cfg = config or load_matcher_config()
    fee_str = cfg.fee_label()
    p = match.profile
    g = match.grant
    owner_name, property_label = _extract_recipient_salutation(p.name)

    elig_match = re.search(r"Eligible Applicants:\s*(.+?)(?:\s+Program Scope:|$)", g.eligibility_text)
    concise_elig = (elig_match.group(1).strip() if elig_match else g.eligibility_text)[:280].rstrip(".")
    if len(concise_elig) >= 275:
        concise_elig = concise_elig.rsplit(" ", 1)[0] + "..."
    else:
        concise_elig = concise_elig + "."

    rationale_bullets = "\n".join(f"- {r}" for r in match.rationale)
    matched_kw_str = ", ".join(f"`{t}`" for t in match.matched_terms) if match.matched_terms else "`conservation`"

    co_sponsor_sentence = (
        f"Because your property qualifies for **direct private landowner submission**, our automated advisory desk "
        f"prepares your complete project narrative, budget engineering table, and technical handoff packet for "
        f"your local {p.county} County Resource Conservation District (RCD) or agency forester/planner—entirely asynchronously."
        if "Direct Private Landowner" in match.application_pathway
        else f"Because this program utilizes a **{match.application_pathway}**, our advisory desk drafts the "
        f"complete Co-Sponsor Memorandum and dispatches the technical handoff package directly to your {p.county} "
        f"County Resource Conservation District (RCD) or USDA NRCS Service Center—zero administrative burden on your family."
    )

    raw_brief = f"""## Outreach Brief #{pitch_index}: {owner_name} — *{property_label}*

| Field | Verified Opportunity Details |
| :--- | :--- |
| **Landowner & Property** | **{_table_safe(p.name)}** ({p.acreage:.0f} acres — {p.county} County, {p.state}) |
| **Land Classification** | {_table_safe(p.land_type)} |
| **Exact Grant Name** | **{_table_safe(g.title)}** |
| **Administering Agency** | {_table_safe(g.agency)} |
| **Available Funding** | **{_table_safe(g.funding_amount)}** |
| **Application Deadline** | **{_table_safe(g.deadline)}** |
| **Filing Pathway** | {_table_safe(match.application_pathway)} |
| **Match Score & Signals** | **{match.score}/100** ({matched_kw_str}) |
| **Official Solicitation** | [{g.url}]({g.url}) |

### Cold-Outreach Letter (1-Page Landowner Brief)

**Subject:** Funding Alert for *{property_label}* ({p.acreage:.0f} Acres, {p.county} County): **{g.title}**

Dear {owner_name},

While reviewing conservation-eligible parcels in **{p.county} County, {p.state}**, we identified your **{p.acreage:.0f}-acre {p.land_type.lower()}** (*{property_label}*) as a high-priority match for active public conservation funding under the **{g.title}**, administered by **{g.agency}**.

#### 1. Grant Funding & Deadline Summary
- **Exact Grant Opportunity:** {g.title}
- **How Much Funding Is Available:** **{g.funding_amount}**
- **Application Deadline:** **{g.deadline}**
- **Recommended Parcel Scope:** {match.recommended_project_focus}

#### 2. Why *{property_label}* Qualifies
{rationale_bullets}
- **Published Eligibility Basis:** {concise_elig}

#### 3. Turnkey Application Paperwork Offer — Contingent {fee_str} Success Fee ($0 Upfront)
Public conservation grants leave millions of dollars unclaimed each cycle because federal/state portals, GIS parcel mapping, technical scoring rubrics, and agency filings take 35–60 hours of specialized preparation. **We handle 100% of the application paperwork from start to finish for a strictly contingent {fee_str} success fee:**
- **$0 Upfront / No-Win, No-Fee:** You pay **$0** for eligibility verification, project narrative drafting, budget engineering, and agency submission packaging.
- **Zero-Call Digital Workflow & RCD Coordination:** {co_sponsor_sentence}
- **Transparent {fee_str} Fee Structure:** Our **{fee_str} success fee** is earned *only* if and when grant funds are officially awarded to your project ({match.fee_example}).

**Next Step:** {NO_CALL_NEXT_STEP}

Sincerely,  
**{cfg.firm_name}**  
*{cfg.firm_subtitle}*
"""
    return _escape_md_dollars(raw_brief)


def build_outreach_markdown(
    matches: list[GrantMatch],
    total_grants_count: int,
    profiles_count: int,
    llm_model_used: str | None,
    config: MatcherConfig | None = None,
) -> str:
    """Assemble the complete outreach_pitches.md document."""
    cfg = config or load_matcher_config()
    fee_str = cfg.fee_label()
    timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M %Z").strip()
    engine_desc = (
        f"Local OpenAI-Compatible LLM (`{DEFAULT_LLM_BASE_URL}`, model `{llm_model_used}`)"
        if llm_model_used
        else f"Rule-Based Conservation Extraction Engine (probed `{DEFAULT_LLM_BASE_URL}` — offline fallback active)"
    )
    county_filter_label = (
        ", ".join(sorted(cfg.normalized_counties())).title()
        if cfg.normalized_counties()
        else "All Eligible Counties"
    )

    header_block = _escape_md_dollars(
        "\n".join(
            [
                f"# {cfg.firm_name} — Conservation Grant Cold-Outreach Briefs",
                "",
                f"- **Advisory Practice:** **{cfg.firm_name}** — *{cfg.firm_subtitle}*",
                f"- **Generated:** `{timestamp_str}`",
                f"- **Active Filter Config (`config.json`):** State=`{cfg.state or 'ANY'}`, Counties=`{county_filter_label}`, Acreage=`{cfg.min_acreage:g}–{cfg.max_acreage:g} ac`, Min Grant=`${cfg.min_grant_amount:,}`, Top-K=`{cfg.top_k_per_profile}`",
                f"- **Active Grants Indexed in `grants.db`:** `{total_grants_count}` live federal & California Climate Investments opportunities",
                f"- **Landowner Profiles Matched:** `{profiles_count}` parcels (`{len(matches)}` tailored 1-page outreach briefs)",
                f"- **Pitch Generation Engine:** {engine_desc}",
                f"- **Commercial Terms & Onboarding:** Contingent **{fee_str} success fee** ($0 upfront) · 100% digital intake (zero phone calls)",
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


def run_matcher(
    db_path: Path | str = DEFAULT_DB_PATH,
    output_path: Path | str = DEFAULT_OUTPUT_PATH,
    config_path: Path | str = DEFAULT_CONFIG_PATH,
    top_k_per_profile: int | None = None,
) -> tuple[list[GrantMatch], Path]:
    """Execute end-to-end landowner-to-grant matching using config.json and write outreach_pitches.md."""
    cfg = load_matcher_config(config_path)
    all_profiles = fetch_all_profiles(db_path=db_path)
    grants = fetch_all_grants(db_path=db_path)

    if not all_profiles:
        raise RuntimeError("No landowner profiles found in grants.db. Run seed_profiles.py first.")
    if not grants:
        raise RuntimeError("No grants found in grants.db. Run scraper.py first.")

    eligible_profiles = filter_profiles_by_config(all_profiles, cfg)
    llm_model = probe_local_llm(DEFAULT_LLM_BASE_URL)
    if llm_model:
        logger.info("Connected to local OpenAI-compatible LLM at %s (model=%s).", DEFAULT_LLM_BASE_URL, llm_model)
    else:
        logger.info(
            "No local LLM detected at %s; using deterministic rule-based extraction & pitch generator.",
            DEFAULT_LLM_BASE_URL,
        )

    matches = match_profiles_to_grants(
        eligible_profiles,
        grants,
        top_k_per_profile=top_k_per_profile,
        config=cfg,
    )
    markdown_doc = build_outreach_markdown(
        matches=matches,
        total_grants_count=len(grants),
        profiles_count=len(eligible_profiles),
        llm_model_used=llm_model,
        config=cfg,
    )

    out_file = Path(output_path)
    out_file.write_text(markdown_doc, encoding="utf-8")
    logger.info("Generated %d cold-outreach briefs -> %s (%d bytes)", len(matches), out_file, len(markdown_doc))
    return matches, out_file


if __name__ == "__main__":
    matched_list, out_md = run_matcher()
    print(f"\n=== Generated {len(matched_list)} Tailored Outreach Pitches in {out_md} ===")
    for i, m in enumerate(matched_list, start=1):
        print(
            f"{i:2d}. {m.profile.county:10s} ({m.profile.acreage:3.0f} ac) -> "
            f"[score={m.score:4.1f}] {m.grant.title} | Deadline: {m.grant.deadline}"
        )
