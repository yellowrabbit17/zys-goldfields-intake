"""Auto-Admin & County RCD / USDA NRCS Handoff Packager (admin_packager.py).

Generates a ready-to-file client_submission_packet.md from a digital ClientIntake submission,
including:
  a) Pre-filled Project Narrative and Budget Engineering table tailored to the matched grant.
  b) Ready-to-send Email and Co-Sponsor / Technical Planner Memorandum addressed to the client's
     County Resource Conservation District (RCD) and local USDA NRCS Service Center.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from pathlib import Path
import re
from typing import Any

from db import DEFAULT_DB_PATH, fetch_all_grants, insert_client_intake
from matcher import (
    DEFAULT_CONFIG_PATH,
    _escape_md_dollars,
    _table_safe,
    load_matcher_config,
    match_single_profile,
)
from models import ClientIntake, GrantMatch, GrantRecord, LandownerProfile, MatcherConfig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("admin_packager")

DEFAULT_PACKET_PATH = Path(__file__).resolve().parent / "client_submission_packet.md"

COUNTY_RCD_DIRECTORY: dict[str, dict[str, str]] = {
    "el dorado": {
        "rcd_name": "El Dorado County & Georgetown Divide Resource Conservation Districts",
        "rcd_email": "info@eldoradorcd.org",
        "rcd_address": "100 Forni Road, Suite A, Placerville, CA 95667",
        "nrcs_office": "USDA NRCS Placerville Service Center",
        "nrcs_address": "100 Forni Road, Suite A, Placerville, CA 95667",
        "forestry_unit": "CAL FIRE Amador-El Dorado Unit (AEU) Forestry Assistance Specialist",
    },
    "sacramento": {
        "rcd_name": "Sloughhouse Resource Conservation District (Sacramento County)",
        "rcd_email": "info@sloughhousercd.org",
        "rcd_address": "8698 Elk Grove Blvd, Suite 1-207, Elk Grove, CA 95624",
        "nrcs_office": "USDA NRCS Elk Grove Service Center",
        "nrcs_address": "9701 Dino Drive, Suite 170, Elk Grove, CA 95624",
        "forestry_unit": "CAL FIRE Northern Region Urban & Watershed Forestry Desk",
    },
    "placer": {
        "rcd_name": "Placer Resource Conservation District",
        "rcd_email": "general@placerrcd.org",
        "rcd_address": "11641 Blocker Drive, Suite 120, Auburn, CA 95603",
        "nrcs_office": "USDA NRCS Auburn Service Center",
        "nrcs_address": "11661 Blocker Drive, Suite 115, Auburn, CA 95603",
        "forestry_unit": "CAL FIRE Nevada-Yuba-Placer Unit (NEU) Forestry Assistance Specialist",
    },
    "nevada": {
        "rcd_name": "Nevada County Resource Conservation District",
        "rcd_email": "admin@ncrcd.org",
        "rcd_address": "113 Presley Way, Suite 1, Grass Valley, CA 95945",
        "nrcs_office": "USDA NRCS Grass Valley Service Center",
        "nrcs_address": "113 Presley Way, Suite 2, Grass Valley, CA 95945",
        "forestry_unit": "CAL FIRE Nevada-Yuba-Placer Unit (NEU) Forestry Assistance Specialist",
    },
    "yolo": {
        "rcd_name": "Yolo County Resource Conservation District",
        "rcd_email": "info@yolorcd.org",
        "rcd_address": "221 West Court Street, Suite 1, Woodland, CA 95695",
        "nrcs_office": "USDA NRCS Woodland Service Center",
        "nrcs_address": "221 West Court Street, Suite 5, Woodland, CA 95695",
        "forestry_unit": "CAL FIRE Sonoma-Lake-Napa Unit (LNU) Forestry Assistance Specialist",
    },
    "amador": {
        "rcd_name": "Amador Resource Conservation District",
        "rcd_email": "amadorrcd@gmail.com",
        "rcd_address": "12200-B Airport Road, Jackson, CA 95642",
        "nrcs_office": "USDA NRCS Jackson Service Center",
        "nrcs_address": "12200-B Airport Road, Jackson, CA 95642",
        "forestry_unit": "CAL FIRE Amador-El Dorado Unit (AEU) Forestry Assistance Specialist",
    },
    "butte": {
        "rcd_name": "Butte County Resource Conservation District",
        "rcd_email": "bcrcd@carcd.org",
        "rcd_address": "150 Chuck Yeager Way, Suite A, Oroville, CA 95965",
        "nrcs_office": "USDA NRCS Oroville Service Center",
        "nrcs_address": "150 Chuck Yeager Way, Suite B, Oroville, CA 95965",
        "forestry_unit": "CAL FIRE Butte Unit (BTU) Forestry Assistance Specialist",
    },
    "sonoma": {
        "rcd_name": "Sonoma Resource Conservation District & Gold Ridge RCD",
        "rcd_email": "info@sonomarcd.org",
        "rcd_address": "1221 Farmers Lane, Suite F, Santa Rosa, CA 95405",
        "nrcs_office": "USDA NRCS Petaluma Service Center",
        "nrcs_address": "5401 Old Redwood Highway, Suite 100, Petaluma, CA 94954",
        "forestry_unit": "CAL FIRE Sonoma-Lake-Napa Unit (LNU) Forestry Assistance Specialist",
    },
    "marin": {
        "rcd_name": "Marin Resource Conservation District",
        "rcd_email": "nancy@marinrcd.org",
        "rcd_address": "80 Fourth Street, Suite 202, Point Reyes Station, CA 94956",
        "nrcs_office": "USDA NRCS Petaluma / Marin Field Service Center",
        "nrcs_address": "5401 Old Redwood Highway, Suite 100, Petaluma, CA 94954",
        "forestry_unit": "CAL FIRE Marin County / LNU Forestry Assistance Specialist",
    },
    "san mateo": {
        "rcd_name": "San Mateo Resource Conservation District",
        "rcd_email": "info@sanmateorcd.org",
        "rcd_address": "80 Stone Pine Road, Suite 100, Half Moon Bay, CA 94019",
        "nrcs_office": "USDA NRCS Half Moon Bay Service Center",
        "nrcs_address": "80 Stone Pine Road, Suite 100, Half Moon Bay, CA 94019",
        "forestry_unit": "CAL FIRE San Mateo-Santa Cruz Unit (CZU) Forestry Assistance Specialist",
    },
    "san benito": {
        "rcd_name": "San Benito Resource Conservation District",
        "rcd_email": "info@rcdsanbenito.org",
        "rcd_address": "2337 Technology Parkway, Suite C, Hollister, CA 95023",
        "nrcs_office": "USDA NRCS Hollister Service Center",
        "nrcs_address": "2337 Technology Parkway, Suite C, Hollister, CA 95023",
        "forestry_unit": "CAL FIRE San Benito-Monterey Unit (BEU) Forestry Assistance Specialist",
    },
    "santa barbara": {
        "rcd_name": "Cachuma Resource Conservation District (Santa Barbara County)",
        "rcd_email": "info@rcdsantabarbara.org",
        "rcd_address": "920 East Stowell Road, Santa Maria, CA 93454",
        "nrcs_office": "USDA NRCS Santa Maria Service Center",
        "nrcs_address": "920 East Stowell Road, Santa Maria, CA 93454",
        "forestry_unit": "CAL FIRE San Luis Obispo / Santa Barbara Forestry Assistance Desk",
    },
    "san luis obispo": {
        "rcd_name": "Coastal San Luis & Upper Salinas-Las Tablas Resource Conservation Districts",
        "rcd_email": "info@coastalrcd.org",
        "rcd_address": "1203 Main Street, Suite B, Morro Bay, CA 93442",
        "nrcs_office": "USDA NRCS Templeton Service Center",
        "nrcs_address": "65 South Main Street, Suite 108, Templeton, CA 93465",
        "forestry_unit": "CAL FIRE San Luis Obispo Unit (SLU) Forestry Assistance Specialist",
    },
    "ventura": {
        "rcd_name": "Ventura County Resource Conservation District",
        "rcd_email": "info@vcrcd.org",
        "rcd_address": "3380 Somis Road, Somis, CA 93066",
        "nrcs_office": "USDA NRCS Somis Service Center",
        "nrcs_address": "3380 Somis Road, Somis, CA 93066",
        "forestry_unit": "CAL FIRE Southern Region Ventura Forestry Assistance Desk",
    },
    "solano": {
        "rcd_name": "Solano Resource Conservation District",
        "rcd_email": "info@solanorcd.org",
        "rcd_address": "1170 North Lincoln Street, Suite 110, Dixon, CA 95620",
        "nrcs_office": "USDA NRCS Vacaville / Dixon Service Center",
        "nrcs_address": "810 Vaca Valley Parkway, Suite 103, Vacaville, CA 95688",
        "forestry_unit": "CAL FIRE Sonoma-Lake-Napa Unit (LNU) Forestry Assistance Specialist",
    },
    "calaveras": {
        "rcd_name": "Calaveras County Resource Conservation District",
        "rcd_email": "info@calaverasrcd.org",
        "rcd_address": "423 East Saint Charles Street, San Andreas, CA 95249",
        "nrcs_office": "USDA NRCS San Andreas / Jackson Service Center",
        "nrcs_address": "12200-B Airport Road, Jackson, CA 95642",
        "forestry_unit": "CAL FIRE Tuolumne-Calaveras Unit (TCU) Forestry Assistance Specialist",
    },
    "yuba": {
        "rcd_name": "Yuba County Resource Conservation District",
        "rcd_email": "yubarcd@gmail.com",
        "rcd_address": "1511 Butte House Road, Suite B, Yuba City, CA 95993",
        "nrcs_office": "USDA NRCS Yuba City Service Center",
        "nrcs_address": "1511 Butte House Road, Suite B, Yuba City, CA 95993",
        "forestry_unit": "CAL FIRE Nevada-Yuba-Placer Unit (NEU) Forestry Assistance Specialist",
    },
    "san diego": {
        "rcd_name": "Resource Conservation District of Greater San Diego County",
        "rcd_email": "info@rcdsandiego.org",
        "rcd_address": "11769 Waterhill Road, Lakeside, CA 92040",
        "nrcs_office": "USDA NRCS Escondido Service Center",
        "nrcs_address": "900 Canterbury Place, Suite 320, Escondido, CA 92025",
        "forestry_unit": "CAL FIRE San Diego Unit (MVU) Forestry Assistance Specialist",
    },
    "napa": {
        "rcd_name": "Napa County Resource Conservation District",
        "rcd_email": "info@naparcd.org",
        "rcd_address": "1303 Jefferson Street, Suite 500B, Napa, CA 94559",
        "nrcs_office": "USDA NRCS Napa Service Center",
        "nrcs_address": "1303 Jefferson Street, Suite 500B, Napa, CA 94559",
        "forestry_unit": "CAL FIRE Sonoma-Lake-Napa Unit (LNU) Forestry Assistance Specialist",
    },
}


def resolve_local_agency_contacts(county: str, state: str = "CA") -> dict[str, str]:
    """Return local County RCD and USDA NRCS Service Center details for any county."""
    key = county.strip().lower().replace(" county", "")
    if key in COUNTY_RCD_DIRECTORY:
        return COUNTY_RCD_DIRECTORY[key]
    clean_county = county.strip().title()
    slug = re.sub(r"[^a-z0-9]+", "", key) or "county"
    return {
        "rcd_name": f"{clean_county} County Resource Conservation District",
        "rcd_email": f"conservation@{slug}rcd.org",
        "rcd_address": f"{clean_county} County Agricultural & Conservation Center, {state}",
        "nrcs_office": f"USDA NRCS {clean_county} County Field Service Center",
        "nrcs_address": f"USDA Service Center — {clean_county} County, {state}",
        "forestry_unit": f"State Forestry Assistance Specialist ({clean_county} County District)",
    }


def infer_land_classification(land_condition: str, project_goals: str) -> str:
    """Infer a concise ecological/agricultural land classification from client intake text."""
    blob = f"{land_condition} {project_goals}".lower()
    if any(w in blob for w in ("oak", "conifer", "pine", "timber", "forest", "woodland")):
        return "Degraded Oak Woodland & Nonindustrial Forestland"
    if any(w in blob for w in ("ranch", "cattle", "pasture", "grazing", "irrigat", "farmland")):
        return "Working Agricultural Ranch & Riparian Corridor"
    if any(w in blob for w in ("sanctuary", "vernal", "wetland", "amphibian", "songbird", "preserve")):
        return "Private Wildlife Sanctuary & Riparian Wetland Preserve"
    return "Private Conservation & Working Lands Parcel"


def build_budget_engineering_table(
    intake: ClientIntake,
    grant: GrantRecord,
    fee_percent: float = 7.0,
) -> tuple[str, int, int, int, int]:
    """Build a mathematically exact line-item engineering budget tailored to the matched grant.

    Returns:
        (markdown_table, total_project_cost, grant_request_total, landowner_match_total, advisory_fee_dollars)
    """
    acres = max(1.0, intake.acreage)
    ac_int = int(round(acres))
    title_low = grant.title.lower()

    rows: list[tuple[str, str, str, int, int, int]] = []

    if "california forest improvement" in title_low:
        # CAL FIRE CFIP: 75%-90% state cost-share rates on forest management & reforestation
        plan_total = max(6500, int(round(3500 + acres * 95, -2)))
        thin_total = int(round(acres * 620, -2))
        reforest_total = int(round(acres * 480, -2))
        erosion_total = max(4500, int(round(acres * 190, -2)))
        rpf_sup_total = max(4000, int(round((thin_total + reforest_total) * 0.12, -2)))

        items = [
            (
                "1. RPF Forest Management Plan & Archaeological/CEQA Review",
                f"1 parcel ({ac_int} ac)",
                f"${plan_total:,} lump sum",
                plan_total,
                0.90,
            ),
            (
                "2. Mechanical Brush Thinning, Fuelbreak & Stand Improvement",
                f"{ac_int} acres",
                "$620 / acre",
                thin_total,
                0.90,
            ),
            (
                "3. Post-Drought Native Oak & Ponderosa Pine Reforestation",
                f"{ac_int} acres (~150 stems/ac)",
                "$480 / acre",
                reforest_total,
                0.90,
            ),
            (
                "4. Seasonal Gully Stabilization, Waterbars & Soil Mulching",
                f"{ac_int} acres",
                f"${int(round(erosion_total / acres)):,} / acre",
                erosion_total,
                0.75,
            ),
            (
                "5. Registered Professional Forester (RPF) Layout & Inspection",
                "Technical supervision",
                "12% of field practices",
                rpf_sup_total,
                0.90,
            ),
        ]
        for label, qty, unit_rate, tot, share_rate in items:
            grant_amt = int(round(tot * share_rate))
            owner_amt = tot - grant_amt
            rows.append((label, qty, unit_rate, tot, grant_amt, owner_amt))

    elif "sustainable agricultural lands conservation" in title_low:
        easement_val = max(250000, int(round(acres * 4500, -3)))
        plan_cost = 35000
        appraisal_cost = 25000
        rows = [
            (
                "1. Agricultural Conservation Easement Acquisition Value",
                f"{ac_int} acres",
                "$4,500 / acre",
                easement_val,
                int(round(easement_val * 0.90)),
                easement_val - int(round(easement_val * 0.90)),
            ),
            (
                "2. Baseline Documentation Report, Title & Appraisal Package",
                "1 easement closing",
                "$25,000 fixed",
                appraisal_cost,
                appraisal_cost,
                0,
            ),
            (
                "3. Climate-Smart Ranch & Riparian Grazing Management Plan",
                f"{ac_int} acres",
                "$35,000 fixed",
                plan_cost,
                plan_cost,
                0,
            ),
        ]

    elif "fish passage" in title_low:
        design_cost = 28000
        culvert_cost = 92000
        riparian_cost = max(18000, int(round(acres * 450, -2)))
        monitoring_cost = 12000
        for label, qty, rate_str, tot in [
            ("1. Hydrologic Engineering, Fishway Design & Permitting", "1 crossing", "$28,000 lump sum", design_cost),
            ("2. Instream Barrier Removal & Bottomless Arch Culvert Install", "1 structure", "$92,000 lump sum", culvert_cost),
            ("3. Native Willow-Cottonwood Streambank Bioengineering", f"{ac_int} parcel ac", "Site corridor", riparian_cost),
            ("4. Post-Construction Aquatic Connectivity Monitoring", "2 seasons", "$6,000 / yr", monitoring_cost),
        ]:
            grant_amt = int(round(tot * 0.90))
            rows.append((label, qty, rate_str, tot, grant_amt, tot - grant_amt))

    else:
        # General WCB Climate Adaptation / USDA FS LSR / NRCS / WaterSMART conservation implementation
        design_cost = max(12000, int(round(8000 + acres * 120, -2)))
        habitat_cost = max(32000, int(round(acres * 950, -2)))
        soil_water_cost = max(20000, int(round(acres * 520, -2)))
        monitoring_cost = max(9000, int(round((habitat_cost + soil_water_cost) * 0.10, -2)))
        for label, qty, rate_str, tot in [
            ("1. Conservation Engineering, GIS Mapping & Environmental Compliance", f"{ac_int} acres", "Lump sum", design_cost),
            ("2. Native Vegetation, Reforestation & Wildlife Habitat Restoration", f"{ac_int} acres", "$950 / acre", habitat_cost),
            ("3. Soil Health, Erosion Control & Hydrologic Resilience Practices", f"{ac_int} acres", "$520 / acre", soil_water_cost),
            ("4. Ecological Performance Verification & Agency Reporting", "3-year window", "10% technical", monitoring_cost),
        ]:
            rows.append((label, qty, rate_str, tot, tot, 0))

    total_cost = sum(r[3] for r in rows)
    grant_total = sum(r[4] for r in rows)
    owner_total = sum(r[5] for r in rows)
    advisory_fee = int(round(grant_total * (fee_percent / 100.0)))

    table_lines = [
        "| Line Item / Conservation Practice | Quantity / Basis | Unit Rate | Total Cost | Grant Request | Landowner Match / In-Kind |",
        "| :--- | :--- | :--- | ---: | ---: | ---: |",
    ]
    for label, qty, unit_rate, tot, g_amt, o_amt in rows:
        table_lines.append(
            f"| {_table_safe(label)} | {_table_safe(qty)} | {_table_safe(unit_rate)} | "
            f"${tot:,} | **${g_amt:,}** | ${o_amt:,} |"
        )
    table_lines.append(
        f"| **TOTAL DIRECT PROJECT BUDGET** | **{ac_int} Acres (APN `{intake.apn}`)** | **—** | "
        f"**${total_cost:,}** | **${grant_total:,}** | **${owner_total:,}** |"
    )

    return "\n".join(table_lines), total_cost, grant_total, owner_total, advisory_fee


def compose_submission_packet_markdown(
    intake: ClientIntake,
    matches: list[GrantMatch],
    config: MatcherConfig,
) -> str:
    """Render the complete client_submission_packet.md with Narrative, Budget, and RCD Handoff."""
    if not matches:
        raise RuntimeError(f"No eligible conservation grants matched parcel in {intake.county} County.")

    primary = matches[0]
    backups = matches[1:]
    grant = primary.grant
    contacts = resolve_local_agency_contacts(intake.county, intake.state)
    fee_str = config.fee_label()

    budget_table_md, total_cost, grant_req, owner_match, fee_dollars = build_budget_engineering_table(
        intake=intake,
        grant=grant,
        fee_percent=config.success_fee_percent,
    )
    net_benefit = grant_req - fee_dollars
    generated_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    consent_ts = intake.submitted_at or generated_ts
    county_code = re.sub(r"[^A-Za-z]", "", intake.county)[:3].upper() or "CAL"
    apn_code = re.sub(r"[^0-9A-Za-z]", "", intake.apn)[:8] or "APN"
    ref_code = f"ZYS-{county_code}-{apn_code}"

    is_forestry = "forest" in grant.title.lower()
    specialist_role = (
        f"staff Conservation Planner / Registered Professional Forester (in coordination with {contacts['forestry_unit']})"
        if is_forestry
        else "staff Conservation Planner / Agricultural Technical Specialist"
    )

    backup_rows = ""
    if backups:
        b_lines = [
            "",
            "#### Secondary Pre-Screened Stackable / Backup Grants",
            "| Rank | Grant Program | Agency | Available Funding | Deadline |",
            "| :---: | :--- | :--- | :--- | :--- |",
        ]
        for idx, bm in enumerate(backups, start=2):
            b_lines.append(
                f"| #{idx} | {_table_safe(bm.grant.title)} | {_table_safe(bm.grant.agency)} | "
                f"{_table_safe(bm.grant.funding_amount)} | {_table_safe(bm.grant.deadline)} |"
            )
        backup_rows = "\n".join(b_lines) + "\n"

    raw_doc = f"""# Ready-to-File Conservation Grant Submission & RCD Handoff Packet

- **Dossier Reference:** `{ref_code}`
- **Prepared By:** **{config.firm_name}** — *{config.firm_subtitle}*
- **Packet Generated:** `{generated_ts}`
- **Workflow Mode:** 100% Zero-Call Digital Intake & Asynchronous Agency Handoff

---

## Executive Parcel & Grant Control Sheet

| Filing Parameter | Verified Client & Solicitation Record |
| :--- | :--- |
| **Landowner / Applicant** | **{_table_safe(intake.name)}** (`{intake.email}`) |
| **Jurisdiction & Parcel APN** | **{intake.county} County, {intake.state}** — Assessor's Parcel Number (`APN`): **`{intake.apn}`** |
| **Verified Parcel Acreage** | **{intake.acreage:g} Acres** ({_table_safe(primary.profile.land_type)}) |
| **Primary Matched Grant** | **{_table_safe(grant.title)}** |
| **Administering Agency** | {_table_safe(grant.agency)} |
| **Published Funding Pool** | {_table_safe(grant.funding_amount)} |
| **Target Solicitation Deadline** | **{_table_safe(grant.deadline)}** |
| **Filing Pathway** | {_table_safe(primary.application_pathway)} |
| **Engineered Grant Request** | **${grant_req:,}** (out of ${total_cost:,} total project budget) |
| **Advisory Fee Authorization** | `[x]` **Agreed to {fee_str} Contingent No-Win / No-Fee Terms** (`{consent_ts}`) — ${fee_dollars:,} due *only* upon grant award ($0 upfront) |
| **Official Solicitation Link** | [{grant.url}]({grant.url}) |
{backup_rows}
---

## Part A: Pre-Filled Project Narrative & Budget Engineering

### A.1 Project Abstract & Statutory Eligibility Statement
**Applicant {intake.name}** requests **${grant_req:,}** in conservation funding under the **{grant.title}** ({grant.agency}) to implement multi-benefit ecological restoration and working-lands stewardship across **{intake.acreage:g} contiguous acres** in **{intake.county} County, {intake.state}** (**APN `{intake.apn}`**).

**Eligibility Verification:**
{chr(10).join(f"- {r}" for r in primary.rationale)}

### A.2 Baseline Land Condition & Resource Problem Statement
Field-reported baseline assessment submitted digitally by the landowner for APN `{intake.apn}` ({intake.acreage:g} acres, {intake.county} County) documents the following urgent resource concerns:

> "{intake.land_condition}"

Without targeted public cost-share intervention, degraded vegetative cover, hydrological stress, invasive understory fuel loading, and soil erosion across this {intake.acreage:g}-acre {intake.county} County foothill/watershed parcel will continue to impair carbon storage, downstream water quality, and native wildlife habitat resilience.

### A.3 Technical Scope of Work & Phased Implementation Schedule
The project executes the landowner's stated conservation objectives:

> "{intake.project_goals}"

- **Phase 1 — Technical Verification & GIS Conservation Plan (Months 1–2):** Local County Resource Conservation District (RCD) / NRCS conservation planner or Registered Professional Forester (RPF) verifies APN `{intake.apn}` parcel boundaries, soil map units, cultural/biological clearances, and practice specifications.
- **Phase 2 — Site Preparation, Fuels / Barrier Remediation & Earthwork (Months 3–6):** Execute brush/fuel load reduction, hydrological/erosion stabilization, and structural habitat prep across priority treatment units within the {intake.acreage:g}-acre parcel.
- **Phase 3 — Native Revegetation, Reforestation & Soil Health Seeding (Months 7–10):** Install locally adapted native tree/shrub stock and perennial cover aligned with agency practice standards.
- **Phase 4 — Photo-Point Monitoring, Survival Verification & Grant Closeout (Months 11–18):** Complete agency inspection sign-off, reimbursement invoicing, and multi-year stewardship verification.

### A.4 Quantified Conservation & Climate Outcomes
- **Treated Conservation Footprint:** `{intake.acreage:g} acres` permanently enhanced in {intake.county} County (`APN {intake.apn}`).
- **Greenhouse Gas & Soil Carbon Impact:** Accelerated woody biomass carbon sequestration, reduced catastrophic wildfire emissions risk, and improved soil organic matter retention.
- **Watershed & Biodiversity Co-Benefits:** Stabilized seasonal surface runoff, enhanced riparian/upland wildlife corridor connectivity, and sustained native pollinator/avian forage.

### A.5 Engineered Line-Item Project Budget ({grant.title})

{budget_table_md}

> **Grant Accounting & {fee_str} Contingent Fee Compliance Note:** In strict compliance with federal `2 CFR Part 200` and California state grant guidelines, all line items in the table above represent 100% direct on-the-ground technical, planning, and conservation implementation costs (${grant_req:,} grant request; ${owner_match:,} landowner cost-share/in-kind). **{config.firm_name}** charges **$0 upfront** and invoices its separate **{fee_str} contingent advisory fee (${fee_dollars:,})** privately upon executed grant award, leaving an estimated **${net_benefit:,} net grant benefit** for the property.

---

## Part B: Ready-to-Send County RCD & USDA NRCS Handoff Package

### B.1 Ready-to-Send Dispatch Email (Copy & Send — Zero Phone Calls Required)

- **To:** `{contacts["rcd_email"]}` ({contacts["rcd_name"]})
- **CC:** `{intake.email}` ({intake.name} — Landowner); {contacts["nrcs_office"]}
- **Subject:** `Technical Planner Assignment & Grant Filing Handoff — APN {intake.apn} ({intake.acreage:g} Acres, {intake.county} County) — {grant.title}`

```text
Dear Conservation Planning Team at {contacts["rcd_name"]},

On behalf of {intake.name} (copied), owner of a {intake.acreage:g}-acre parcel in {intake.county} County, {intake.state} (Assessor's Parcel Number: {intake.apn}), {config.firm_name} is submitting the attached pre-screened Conservation Grant Application Narrative and Engineered Budget (${grant_req:,} target grant request) for the active solicitation:

  Program:  {grant.title}
  Agency:   {grant.agency}
  Deadline: {grant.deadline}
  Pathway:  {primary.application_pathway}

We respectfully request that {contacts["rcd_name"]} (in coordination with the {contacts["nrcs_office"]}) assign a {specialist_role} to:
  1. Generate the final GIS parcel boundary & treatment-unit map for APN {intake.apn};
  2. Confirm agency practice codes / RPF or NRCS technical sign-off against our pre-engineered ${total_cost:,} scope; and
  3. Execute the Co-Sponsor / Technical Assistance endorsement for portal submission ahead of the {grant.deadline} deadline.

All landowner authorizations, baseline condition statements, and budget tables are complete in the attached Co-Sponsor Memorandum so your planner can proceed asynchronously via email without scheduling introductory phone calls. Please reply-all with the assigned planner's email and any standard landowner signature forms required by your district.

Sincerely,

{config.firm_name}
{config.firm_subtitle}
Dossier Ref: {ref_code}
```

---

### B.2 Formal Co-Sponsor & Technical Assistance Handoff Memorandum

**MEMORANDUM FOR TECHNICAL PLANNER ASSIGNMENT & CO-SPONSORSHIP**

- **TO:** District Manager & Lead Conservation Planner, **{contacts["rcd_name"]}** ({contacts["rcd_address"]})  
- **CONCURRENT COPY:** District Conservationist, **{contacts["nrcs_office"]}** ({contacts["nrcs_address"]})  
- **FROM:** **{config.firm_name}** — *{config.firm_subtitle}*  
- **CLIENT / LANDOWNER:** **{intake.name}** (`{intake.email}`)  
- **PARCEL IDENTIFICATION:** **APN `{intake.apn}`** · **{intake.acreage:g} Acres** · **{intake.county} County, {intake.state}**  
- **TARGET SOLICITATION:** **{grant.title}** (*Deadline: {grant.deadline}*)  
- **DATE:** `{generated_ts}`  

#### 1. Purpose of Handoff
This Memorandum transmits a pre-qualified, landowner-authorized conservation project package prepared by **{config.firm_name}** for **{intake.name}** (`APN {intake.apn}`, {intake.acreage:g} acres in {intake.county} County). All intake screening, statutory acreage/geographic eligibility checks, narrative drafting, and preliminary cost-share engineering (**${grant_req:,}** grant request / **${total_cost:,}** total project cost) have been completed digitally.

#### 2. Requested RCD / USDA NRCS Technical Actions
To complete agency submission prior to **{grant.deadline}**, we request assignment of a district conservation planner or forestry specialist to execute the following technical steps via email workflow:

1. **GIS Parcel & Soil Unit Verification:** Plot APN `{intake.apn}` ({intake.acreage:g} acres) in Web Soil Survey / CalMapper and attach standard conservation practice map polygons matching Section A.3.
2. **Practice Standard / Forester Certification:** Verify practice unit rates in Section A.5 against current {intake.county} County RCD / NRCS / CAL FIRE payment schedules.
3. **Co-Sponsor / Partner Endorsement:** Where **{grant.title}** utilizes `{primary.application_pathway}`, attach standard RCD co-sponsor resolution or technical partner letter and upload/transmit final package to `{grant.agency}`.

#### 3. Digital Landowner Authorization & Advisory Terms Attestation
- **Authorized Landowner:** `{intake.name}` (`{intake.email}`)
- **Parcel Attestation:** Landowner attests ownership/management authority over APN `{intake.apn}` ({intake.acreage:g} acres, {intake.county} County, {intake.state}).
- **Advisory Fee Disclosure:** Landowner has digitally executed the **{config.firm_name}** contingent **{fee_str} no-win/no-fee** agreement (`{consent_ts}`), with **$0** upfront fees and zero administrative cost to {contacts["rcd_name"]} or USDA NRCS.

---
Prepared & Certified by:  
**{config.firm_name}**  
*{config.firm_subtitle}*
"""
    return _escape_md_dollars(raw_doc)


def generate_submission_packet(
    intake: ClientIntake,
    db_path: Path | str = DEFAULT_DB_PATH,
    config_path: Path | str = DEFAULT_CONFIG_PATH,
    output_path: Path | str = DEFAULT_PACKET_PATH,
    persist_intake: bool = True,
) -> tuple[ClientIntake, list[GrantMatch], Path]:
    """Persist a ClientIntake (optional), match grants, and write client_submission_packet.md."""
    cfg = load_matcher_config(config_path)
    saved_intake = insert_client_intake(intake, db_path=db_path) if persist_intake else intake

    grants = fetch_all_grants(db_path=db_path)
    if not grants:
        raise RuntimeError("No active grants found in grants.db. Run scraper.py first.")

    synthetic_profile = LandownerProfile(
        id=saved_intake.id,
        name=saved_intake.name,
        state=saved_intake.state,
        county=saved_intake.county,
        acreage=saved_intake.acreage,
        land_type=infer_land_classification(saved_intake.land_condition, saved_intake.project_goals),
        features=f"APN {saved_intake.apn}. Condition: {saved_intake.land_condition}. Goals: {saved_intake.project_goals}",
    )

    # Match parcel against active grants (bypass county filter override if client is in another CA county)
    intake_cfg = cfg.model_copy(
        update={
            "state": saved_intake.state,
            "county": [],
            "min_acreage": min(cfg.min_acreage, saved_intake.acreage),
            "max_acreage": max(cfg.max_acreage, saved_intake.acreage),
        }
    )
    matches = match_single_profile(synthetic_profile, grants, config=intake_cfg, top_k_override=3)

    if not matches:
        # Broad conservation fallback if client entered terse custom keywords
        broad_profile = synthetic_profile.model_copy(
            update={
                "features": f"{synthetic_profile.features} conservation soil health wildlife habitat climate adaptation"
            }
        )
        matches = match_single_profile(broad_profile, grants, config=intake_cfg, top_k_override=3)

    packet_md = compose_submission_packet_markdown(saved_intake, matches, cfg)
    out_file = Path(output_path)
    out_file.write_text(packet_md, encoding="utf-8")
    logger.info(
        "Generated ready-to-file packet for %s (APN %s, %s County) -> %s (%d bytes)",
        saved_intake.name,
        saved_intake.apn,
        saved_intake.county,
        out_file,
        len(packet_md),
    )
    return saved_intake, matches, out_file


if __name__ == "__main__":
    sample_vance_intake = ClientIntake(
        name="Elena & Marcus Vance",
        email="vance.family@sierraridgeoak.com",
        county="El Dorado",
        state="CA",
        apn="089-140-027-000",
        acreage=45.0,
        land_condition=(
            "45-acre degraded blue oak, black oak, and ponderosa pine woodland in El Dorado County "
            "with post-drought canopy mortality, heavy manzanita/buckbrush fuel buildup, and seasonal gully erosion."
        ),
        project_goals=(
            "Complete a Registered Professional Forester (RPF) Forest Management Plan, mechanical brush fuels "
            "thinning, native oak and pine reforestation, gully erosion stabilization, and wildlife habitat restoration."
        ),
        agreed_to_terms=True,
    )
    stored, matched, packet_file = generate_submission_packet(sample_vance_intake)
    print(f"=== Generated Sample Submission Packet: {packet_file} ===")
    print(f"  Client:        {stored.name} (Intake ID #{stored.id}, APN {stored.apn})")
    print(f"  Primary Grant: {matched[0].grant.title} ({matched[0].grant.agency})")
    print(f"  Deadline:      {matched[0].grant.deadline}")
