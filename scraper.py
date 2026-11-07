"""Live conservation grant ingestion from Grants.gov REST API and California Climate Investments."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
import logging
import re
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from db import DEFAULT_DB_PATH, upsert_grants
from models import GrantRecord

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("scraper")

GRANTS_GOV_SEARCH_URL = "https://apply07.grants.gov/grantsws/rest/opportunities/search/"
GRANTS_GOV_SEARCH_FALLBACK_URL = "https://api.grants.gov/v1/api/search2"
GRANTS_GOV_DETAIL_URL = "https://apply07.grants.gov/grantsws/rest/opportunity/details"
GRANTS_GOV_DETAIL_PAGE_FMT = "https://www.grants.gov/search-results-detail/{opp_id}"

CCI_BASE_URL = "https://www.caclimateinvestments.ca.gov"
CCI_OPEN_JSON_URL = f"{CCI_BASE_URL}/all-programs-2?category=Open+solicitation&format=json"
CCI_PROGRAM_TSV_URL = f"{CCI_BASE_URL}/data4programpages"

TARGET_KEYWORDS: list[str] = [
    "conservation",
    "reforestation",
    "soil health",
    "wildlife habitat",
    "EQIP",
    "regenerative agriculture",
]

EXCLUDED_AGENCY_PREFIXES = ("IMLS", "NEH", "NEA")

CCI_AGENCY_CODE_MAP = {
    "(CAL FIRE)": "California Department of Forestry and Fire Protection (CAL FIRE)",
    "(WCB)": "California Wildlife Conservation Board (WCB)",
    "(SGC)": "California Strategic Growth Council (SGC)",
    "(CDFA)": "California Department of Food and Agriculture (CDFA)",
    "(CDFW)": "California Department of Fish and Wildlife (CDFW)",
    "(CARB)": "California Air Resources Board (CARB)",
    "(CEC)": "California Energy Commission (CEC)",
    "(CPUC)": "California Public Utilities Commission (CPUC)",
    "(CSD)": "California Department of Community Services and Development (CSD)",
    "(CSWR)": "California State Water Resources Control Board (SWRCB)",
    "(DWR)": "California Department of Water Resources (DWR)",
    "(OPC)": "California Ocean Protection Council (OPC)",
    "SCC": "California State Coastal Conservancy (SCC)",
}


def build_http_session() -> requests.Session:
    """Configure a resilient requests Session with retry backoff."""
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(
        {
            "User-Agent": "ConservationGrantHunterMVP/1.0 (+https://grants.gov)",
            "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
        }
    )
    return session


def strip_html(raw_html: str | None) -> str:
    """Convert HTML snippet to normalized plain text using BeautifulSoup."""
    if not raw_html:
        return ""
    soup = BeautifulSoup(str(raw_html), "html.parser")
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()


def find_matched_keywords(text: str) -> list[str]:
    """Return all TARGET_KEYWORDS found in text (exact phrase or co-occurring conservation terms)."""
    lowered = text.lower()
    matched: list[str] = []
    for kw in TARGET_KEYWORDS:
        kw_low = kw.lower()
        if kw.upper() == "EQIP":
            if re.search(r"\beqip\b", lowered) or "environmental quality incentives program" in lowered:
                matched.append(kw)
        elif kw_low in lowered:
            matched.append(kw)
        elif " " in kw_low:
            tokens = kw_low.split()
            if all(re.search(rf"\b{re.escape(tok)}\b", lowered) for tok in tokens):
                matched.append(kw)
    return matched


def parse_mmddyyyy(raw_date: str | None) -> date | None:
    """Parse MM/DD/YYYY date strings returned by Grants.gov search."""
    if not raw_date:
        return None
    raw_date = raw_date.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%b %d, %Y %I:%M:%S %p %Z"):
        try:
            return datetime.strptime(raw_date, fmt).date()
        except ValueError:
            continue
    # Handle strings like "Jan 26, 2027 12:00:00 AM EST" by stripping trailing timezone
    cleaned = re.sub(r"\s+[A-Z]{2,4}$", "", raw_date)
    try:
        return datetime.strptime(cleaned, "%b %d, %Y %I:%M:%S %p").date()
    except ValueError:
        return None


def format_grants_gov_funding(synopsis: dict[str, Any], desc_text: str) -> str:
    """Build a human-readable funding summary from Grants.gov synopsis numbers."""
    def _to_int(val: Any) -> int:
        if val is None:
            return 0
        digits = re.sub(r"[^\d]", "", str(val))
        return int(digits) if digits else 0

    ceiling = _to_int(synopsis.get("awardCeiling"))
    floor = _to_int(synopsis.get("awardFloor"))
    est_total = _to_int(synopsis.get("estimatedFunding"))

    parts: list[str] = []
    if floor > 1 and ceiling > 0 and floor < ceiling:
        parts.append(f"${floor:,} - ${ceiling:,} per award")
    elif ceiling > 0:
        parts.append(f"Up to ${ceiling:,} per award")
    elif floor > 1:
        parts.append(f"Minimum ${floor:,} per award")

    if est_total > 0 and est_total != ceiling:
        parts.append(f"${est_total:,} total program funding")

    if parts:
        return "; ".join(parts)

    # Fallback: extract explicit dollar figures >= $1,000 from synopsis description
    dollar_matches = re.findall(r"\$\d[\d,]*(?:\.\d+)?(?:\s*(?:million|billion))?", desc_text, flags=re.I)
    meaningful = [
        d for d in dollar_matches
        if any(w in d.lower() for w in ("million", "billion"))
        or int(re.sub(r"[^\d]", "", d.split(".")[0]) or "0") >= 1000
    ]
    if meaningful:
        return f"Up to {meaningful[0]} (see NOFO for project tiers)"

    return "Varies by cooperative agreement / project scope (see NOFO)"


def _search_grants_gov_keyword(session: requests.Session, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """POST a search query to apply07.grants.gov (falling back to api.grants.gov if needed)."""
    try:
        resp = session.post(GRANTS_GOV_SEARCH_URL, json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data.get("oppHits", []) or []
    except Exception as exc:
        logger.warning("Primary apply07 search warning (%s); trying api.grants.gov fallback.", exc)
        resp = session.post(GRANTS_GOV_SEARCH_FALLBACK_URL, json=payload, timeout=15)
        resp.raise_for_status()
        body = resp.json()
        inner = body.get("data", body)
        return inner.get("oppHits", []) or []


def _fetch_grants_gov_detail(
    session: requests.Session,
    hit: dict[str, Any],
    api_matched_kws: list[str],
    today: date,
) -> GrantRecord | None:
    """Fetch full opportunity details from apply07.grants.gov and construct a GrantRecord."""
    opp_id = str(hit.get("id", "")).strip()
    if not opp_id:
        return None

    agency_code = str(hit.get("agencyCode") or "")
    if agency_code.startswith(EXCLUDED_AGENCY_PREFIXES):
        return None

    close_dt = parse_mmddyyyy(hit.get("closeDate"))
    if close_dt is not None and close_dt < today:
        return None

    try:
        resp = session.post(GRANTS_GOV_DETAIL_URL, data={"oppId": opp_id}, timeout=15)
        resp.raise_for_status()
        detail = resp.json()
    except Exception as exc:
        logger.warning("Skipping Grants.gov ID %s due to detail fetch error: %s", opp_id, exc)
        return None

    synopsis = detail.get("synopsis") or {}
    title = strip_html(detail.get("opportunityTitle") or hit.get("title") or "")
    agency = (
        (detail.get("agencyDetails") or {}).get("agencyName")
        or hit.get("agency")
        or (detail.get("topAgencyDetails") or {}).get("agencyName")
        or "U.S. Federal Agency"
    )

    desc_text = strip_html(synopsis.get("synopsisDesc"))
    elig_desc = strip_html(synopsis.get("applicantEligibilityDesc"))
    cfda_titles = " ".join(
        strip_html(c.get("programTitle")) for c in (detail.get("cfdas") or []) if isinstance(c, dict)
    )
    applicant_types = [
        strip_html(t.get("description"))
        for t in (synopsis.get("applicantTypes") or [])
        if t.get("description")
    ]

    combined_text = f"{title} {agency} {cfda_titles} {desc_text} {elig_desc}"
    matched_kws = list(dict.fromkeys(find_matched_keywords(combined_text) + api_matched_kws))
    if not matched_kws:
        return None

    if close_dt is None:
        close_dt = parse_mmddyyyy(synopsis.get("responseDate") or detail.get("originalDueDate"))
        if close_dt is not None and close_dt < today:
            return None

    deadline_str = (
        close_dt.strftime("%Y-%m-%d")
        if close_dt
        else strip_html(synopsis.get("responseDateDesc")) or "Rolling / Open Until Filled"
    )
    funding_str = format_grants_gov_funding(synopsis, desc_text)

    types_summary = "; ".join(applicant_types) if applicant_types else "See NOFO eligibility criteria"
    elig_parts = [f"Eligible Applicants: {types_summary}."]
    if elig_desc:
        elig_parts.append(f"Eligibility Details: {elig_desc[:1000]}")
    if desc_text:
        elig_parts.append(f"Program Scope: {desc_text[:1000]}")
    elig_parts.append(f"Matched Keywords: {', '.join(matched_kws)}.")

    opp_num = str(detail.get("opportunityNumber") or hit.get("number") or opp_id).strip()
    return GrantRecord(
        id=f"GRANTSGOV-{opp_id}",
        title=f"{title} ({opp_num})" if opp_num and opp_num not in title else title,
        agency=f"{agency} (Federal)",
        funding_amount=funding_str,
        deadline=deadline_str,
        eligibility_text=" ".join(elig_parts),
        url=GRANTS_GOV_DETAIL_PAGE_FMT.format(opp_id=opp_id),
    )


def fetch_grants_gov_opportunities(session: requests.Session) -> list[GrantRecord]:
    """Query Grants.gov REST API across conservation keywords and enrich active opportunities."""
    today = date.today()
    candidates: dict[str, dict[str, Any]] = {}
    api_kws_by_id: dict[str, list[str]] = {}

    search_payloads = [
        {
            "keyword": "conservation",
            "oppStatuses": "posted",
            "fundingCategories": "NR|AG|ENV",
            "rows": 50,
        },
        {"keyword": "reforestation", "oppStatuses": "posted", "rows": 25},
        {"keyword": '"soil health"', "oppStatuses": "posted", "rows": 25},
        {"keyword": '"wildlife habitat"', "oppStatuses": "posted", "rows": 25},
        {"keyword": "EQIP", "oppStatuses": "posted", "rows": 25},
        {"keyword": '"regenerative agriculture"', "oppStatuses": "posted", "rows": 25},
    ]

    for payload in search_payloads:
        kw = payload["keyword"]
        clean_kw = kw.strip('"')
        hits = _search_grants_gov_keyword(session, payload)
        logger.info("Grants.gov search %-28s -> %d posted hits", kw, len(hits))
        for hit in hits:
            oid = str(hit.get("id", "")).strip()
            if oid:
                candidates[oid] = hit
                api_kws_by_id.setdefault(oid, [])
                if clean_kw not in api_kws_by_id[oid]:
                    api_kws_by_id[oid].append(clean_kw)

    logger.info("Fetching synopsis details for %d unique Grants.gov candidates...", len(candidates))
    records: list[GrantRecord] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(_fetch_grants_gov_detail, session, hit, api_kws_by_id.get(oid, []), today): oid
            for oid, hit in candidates.items()
        }
        for fut in as_completed(futures):
            rec = fut.result()
            if rec is not None:
                records.append(rec)

    records.sort(key=lambda r: r.id)
    logger.info("Retained %d verified active Grants.gov conservation opportunities.", len(records))
    return records


def _load_cci_funding_tsv(session: requests.Session) -> dict[str, dict[str, str]]:
    """Load CCI's open funding data table from /data4programpages via BeautifulSoup."""
    funding_by_name: dict[str, dict[str, str]] = {}
    try:
        resp = session.get(CCI_PROGRAM_TSV_URL, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        block = soup.find(id="programpages-data")
        if not block:
            return funding_by_name
        lines = [ln for ln in block.get_text().strip().splitlines() if ln.strip()]
        if len(lines) < 2:
            return funding_by_name
        headers = [h.strip() for h in lines[0].split("\t")]
        for line in lines[1:]:
            cols = [c.strip() for c in line.split("\t")]
            row = dict(zip(headers, cols))
            sub_name = re.sub(r"[^a-z0-9]+", " ", row.get("SubProgramName", "").lower()).strip()
            if sub_name:
                funding_by_name[sub_name] = row
    except Exception as exc:
        logger.warning("Unable to load CCI funding TSV enrichment: %s", exc)
    return funding_by_name


def _lookup_cci_tsv_row(title: str, tsv_map: dict[str, dict[str, str]]) -> dict[str, str] | None:
    """Fuzzy-match a CCI program title against the /data4programpages open-data TSV."""
    norm_title = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    if norm_title in tsv_map:
        return tsv_map[norm_title]
    for key, row in tsv_map.items():
        if key in norm_title or norm_title in key:
            return row
    return None


def _compute_next_quarterly_deadline(today: date) -> str:
    """Compute the next quarterly solicitation date for Jan 2, Apr 1, Jul 1, Oct 1 schedules."""
    quarterly_dates = [
        date(today.year, 1, 2),
        date(today.year, 4, 1),
        date(today.year, 7, 1),
        date(today.year, 10, 1),
        date(today.year + 1, 1, 2),
    ]
    for qd in quarterly_dates:
        if qd >= today:
            return f"{qd.strftime('%Y-%m-%d')} (Quarterly cycle: Jan 2, Apr 1, Jul 1, Oct 1)"
    return f"{today.year + 1}-01-02"


def _extract_cci_section(segments: list[str], header_pattern: str, stop_patterns: list[str]) -> str:
    """Extract text between a section heading and the next heading in a CCI program page."""
    start_idx = -1
    for idx, seg in enumerate(segments):
        if re.search(header_pattern, seg, flags=re.I):
            start_idx = idx + 1
            break
    if start_idx == -1:
        return ""
    collected: list[str] = []
    for seg in segments[start_idx:]:
        if any(re.search(pat, seg, flags=re.I) for pat in stop_patterns):
            break
        collected.append(seg)
    return " ".join(collected).strip()


def fetch_cci_opportunities(session: requests.Session) -> list[GrantRecord]:
    """Query and scrape California Climate Investments (caclimateinvestments.ca.gov) open funding data."""
    today = date.today()
    tsv_map = _load_cci_funding_tsv(session)

    raw_items: list[dict[str, Any]] = []
    next_url: str | None = CCI_OPEN_JSON_URL
    while next_url:
        resp = session.get(next_url, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
        raw_items.extend(payload.get("items") or [])
        pagination = payload.get("pagination") or {}
        if pagination.get("nextPage") and pagination.get("nextPageUrl"):
            rel = pagination["nextPageUrl"]
            sep = "&" if "?" in rel else "?"
            next_url = urljoin(CCI_BASE_URL, f"{rel}{sep}format=json")
        else:
            next_url = None

    logger.info("Retrieved %d open solicitations from California Climate Investments.", len(raw_items))

    records: list[GrantRecord] = []
    stop_headings = [
        r"^What does it fund\?$",
        r"^Who is eligible for funds\?$",
        r"^How does this program provide benefits",
        r"^How do I access funds\?$",
        r"^Funding timeline$",
        r"^Project Profiles$",
        r"^Cumulative Statistics$",
        r"^Visit Program website$",
    ]

    for item in raw_items:
        title = strip_html(item.get("title"))
        excerpt = strip_html(item.get("excerpt"))
        cats = item.get("categories") or []
        tags = item.get("tags") or []

        raw_url = str(item.get("sourceUrl") or item.get("fullUrl") or "").strip()
        if raw_url.startswith("http://"):
            program_url = "https://" + raw_url[len("http://") :]
        elif raw_url.startswith("/"):
            program_url = urljoin(CCI_BASE_URL, raw_url)
        elif raw_url.startswith("https://"):
            program_url = raw_url
        else:
            program_url = CCI_BASE_URL

        page_body = ""
        what_it_funds = ""
        who_is_eligible = ""
        timeline_text = ""
        agency_from_page = ""

        try:
            page_resp = session.get(program_url, timeout=12)
            if page_resp.status_code == 200:
                soup = BeautifulSoup(page_resp.text, "html.parser")
                for chrome in soup.find_all(["nav", "header", "footer", "script", "style"]):
                    chrome.decompose()
                segments = [
                    s.strip()
                    for s in soup.get_text("\n", strip=True).splitlines()
                    if s.strip()
                ]
                for idx, seg in enumerate(segments):
                    if re.search(r"^What does it fund\?$", seg, flags=re.I) and idx >= 1:
                        candidate_agency = segments[idx - 1]
                        if "California" in candidate_agency or "(" in candidate_agency:
                            agency_from_page = candidate_agency
                        page_body = " ".join(segments[idx:])
                        break
                what_it_funds = _extract_cci_section(segments, r"^What does it fund\?$", stop_headings)
                who_is_eligible = _extract_cci_section(segments, r"^Who is eligible for funds\?$", stop_headings)
                timeline_text = _extract_cci_section(segments, r"^Funding timeline$", stop_headings)
        except Exception as exc:
            logger.warning("CCI detail scrape warning for %s: %s", program_url, exc)

        combined_text = f"{title} {excerpt} {page_body}"
        matched_kws = find_matched_keywords(combined_text)
        if not matched_kws:
            continue

        # Determine administering agency
        agency = agency_from_page
        if not agency:
            for c in cats:
                if c in CCI_AGENCY_CODE_MAP:
                    agency = CCI_AGENCY_CODE_MAP[c]
                    break
        if not agency:
            agency = "California Climate Investments State Agency"

        # Determine funding amount from TSV + program page award examples
        tsv_row = _lookup_cci_tsv_row(title, tsv_map)
        funding_bits: list[str] = []
        if "forest improvement" in title.lower():
            funding_bits.append("75% - 90% state cost-share assistance for forest management & reforestation")
        dollar_examples = re.findall(
            r"\$\d[\d,]*(?:\.\d+)?(?:\s*(?:million|billion))?",
            f"{what_it_funds} {page_body}",
            flags=re.I,
        )
        # Filter out tiny utility bill examples like "$3.00"
        meaningful_dollars = [
            d for d in dollar_examples
            if any(word in d.lower() for word in ("million", "billion"))
            or int(re.sub(r"[^\d]", "", d.split(".")[0]) or "0") >= 10000
        ]
        if meaningful_dollars:
            unique_examples = list(dict.fromkeys(meaningful_dollars))[:2]
            funding_bits.append(f"Featured awards: {', '.join(unique_examples)}")
        if tsv_row and tsv_row.get("Implemented_$M"):
            funding_bits.append(
                f"{tsv_row['Implemented_$M']} CCI cumulative program funding ({tsv_row.get('ProjectsImplemented', 'multiple')} projects)"
            )
        funding_amount = "; ".join(funding_bits) if funding_bits else "Open State Solicitation (varies by project scope)"

        # Determine deadline from scraped timeline text
        tl_lower = timeline_text.lower()
        if "rolling" in tl_lower:
            deadline = "Rolling (Open Solicitation - applications accepted continuously)"
        elif "january 2" in tl_lower and "april 1" in tl_lower:
            deadline = _compute_next_quarterly_deadline(today)
        elif timeline_text:
            clean_tl = re.sub(r"\s*Visit Program website.*$", "", timeline_text, flags=re.I).strip()
            deadline = f"Open Solicitation ({clean_tl[:110]})"
        else:
            deadline = "Open Solicitation (Active CCI Funding Round)"

        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40]
        elig_summary_parts = [
            "Jurisdiction: California Statewide (California Climate Investments).",
            f"Eligible Applicants: {who_is_eligible or ', '.join(tags) or 'See program guidelines'}.",
            f"Program Scope: {what_it_funds or excerpt}.",
            f"Applicant & Sector Tags: {', '.join(tags)}." if tags else "",
            f"Matched Keywords: {', '.join(matched_kws)}.",
        ]

        records.append(
            GrantRecord(
                id=f"CCI-{slug}",
                title=title,
                agency=f"{agency} — California Climate Investments",
                funding_amount=funding_amount,
                deadline=deadline,
                eligibility_text=" ".join(p for p in elig_summary_parts if p),
                url=program_url,
            )
        )

    logger.info("Retained %d active California Climate Investments conservation programs.", len(records))
    return records


def ingest_all_grants(db_path=DEFAULT_DB_PATH) -> list[GrantRecord]:
    """Run live ingestion across Grants.gov and California Climate Investments and persist to SQLite."""
    session = build_http_session()
    federal_grants = fetch_grants_gov_opportunities(session)
    california_grants = fetch_cci_opportunities(session)

    all_grants = california_grants + federal_grants
    if len(all_grants) < 10:
        raise RuntimeError(
            f"Expected at least 10 active conservation grants, but only found {len(all_grants)}."
        )

    saved_count = upsert_grants(all_grants, db_path=db_path)
    logger.info(
        "Successfully stored %d active conservation grants into %s (%d CCI + %d Grants.gov).",
        saved_count,
        db_path,
        len(california_grants),
        len(federal_grants),
    )
    return all_grants


if __name__ == "__main__":
    grants = ingest_all_grants()
    print(f"\n=== Ingested {len(grants)} Active Conservation Grants into grants.db ===")
    for idx, g in enumerate(grants[:15], start=1):
        print(f"{idx:2d}. [{g.id}] {g.title}")
        print(f"    Agency:   {g.agency}")
        print(f"    Funding:  {g.funding_amount}")
        print(f"    Deadline: {g.deadline}")
    if len(grants) > 15:
        print(f"... plus {len(grants) - 15} additional active grants stored in grants.db.")
