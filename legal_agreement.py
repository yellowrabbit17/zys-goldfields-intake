"""California UETA & Federal ESIGN Contingency Fee Agreement & Cryptographic Audit Generator (legal_agreement.py).

Implements the legally binding 'Conservation Advisory & Origination Agreement' for ZYS GoldFields PRO LLC,
including:
  1. California Uniform Electronic Transactions Act (UETA, Cal. Civ. Code § 1633.1 et seq.) and
     federal ESIGN Act (15 U.S.C. § 7001 et seq.) electronic execution governance.
  2. Strict Federal & State Grant Accounting Protection (2 CFR § 200.459): the 7% "Deferred Administrative
     Origination & Packaging Fee (100% Waived if Grant is Not Awarded)" is payable only from the Client's
     general non-grant funds and is never billed to or deducted from restricted federal or state grant funds.
  3. Payment Triggers & Remedies: 7% invoice triggered upon official Agency Award Notice (or execution
     of the RCD/Agency Grant Agreement), payable within 5 business days of initial tranche disbursement,
     1.5% monthly late fee, Sacramento County, California exclusive venue, and prevailing-party
     attorneys' fees (Cal. Civ. Code § 1717).
  4. CAL FIRE RPF / RCD / agency coordination disclosure (Advisor is not an RPF under Cal. Pub. Resources
     Code § 750 et seq. and is not affiliated with CAL FIRE, any RCD, or USDA NRCS; no award guarantee).
  5. Cryptographic SHA-256 contract digest computation and downloadable signed_advisory_agreement.md
     receipt generation.
  6. CAN-SPAM Act (15 U.S.C. § 7704) compliant footer for every outbound email template.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re

from models import ClientIntake, ESignatureAuditRecord, GrantRecord, MatcherConfig

AGREEMENT_VERSION = "CA-UETA-ESIGN-2026.2"
DEFAULT_SIGNED_AGREEMENT_PATH = Path(__file__).resolve().parent / "signed_advisory_agreement.md"


def _escape_md_dollars(text: str) -> str:
    """Escape literal dollar signs in Markdown prose/tables while leaving code blocks untouched."""
    parts = re.split(r"(```[\s\S]*?```|`[^`\n]+`)", text)
    out: list[str] = []
    for idx, part in enumerate(parts):
        if idx % 2 == 1:
            out.append(part)
        else:
            out.append(re.sub(r"(?<!\\)\$", r"\$", part))
    return "".join(out)


FEE_DISPLAY_NAME = "Deferred Administrative Origination & Packaging Fee (100% Waived if Grant is Not Awarded)"


def render_canonical_contract_text(
    firm_name: str = "ZYS GoldFields PRO LLC",
    firm_subtitle: str = "Conservation & Agricultural Grant Advisory",
    fee_percent: float = 7.0,
    client_name: str = "[CLIENT / LANDOWNER LEGAL NAME]",
    client_email: str = "[CLIENT EMAIL]",
    county: str = "[CALIFORNIA COUNTY]",
    state: str = "CA",
    apn: str = "[PARCEL APN]",
    acreage: float = 0.0,
    matched_grant_title: str = "[MATCHED CONSERVATION GRANT PROGRAM]",
    engineered_grant_request: int = 0,
    contingent_fee_dollars: int = 0,
) -> str:
    """Render the canonical plain-text Conservation Advisory & Origination Agreement.

    This canonical text is displayed in the scrollable agreement box in intake_app.py and
    hashed via SHA-256 at the exact moment of electronic signature.
    """
    fee_str = f"{int(fee_percent)}%" if float(fee_percent).is_integer() else f"{fee_percent:g}%"
    acreage_str = f"{acreage:g} acres" if acreage > 0 else "[PARCEL ACREAGE]"
    req_str = f"${engineered_grant_request:,}" if engineered_grant_request > 0 else "[ENGINEERED GRANT REQUEST]"
    fee_amt_str = f"${contingent_fee_dollars:,}" if contingent_fee_dollars > 0 else f"({fee_str} of Gross Approved Grant Award)"

    return f"""CONSERVATION ADVISORY & ORIGINATION AGREEMENT
Document Version: {AGREEMENT_VERSION}
Advisory Firm: {firm_name} ({firm_subtitle})
Governing Statutes: California Uniform Electronic Transactions Act (Cal. Civ. Code § 1633.1 et seq.); Federal ESIGN Act (15 U.S.C. § 7001 et seq.); Federal Uniform Guidance (2 CFR § 200.459)

1. PARTIES & PROPERTY IDENTIFICATION
This Conservation Advisory & Origination Agreement ("Agreement") is entered into by and between {firm_name} ({firm_subtitle}) ("Advisor") and {client_name} ({client_email}) ("Client"), owner or authorized steward of real property located in {county} County, {state}, identified as Assessor's Parcel Number (APN) {apn} comprising approximately {acreage_str} ("Property"), in connection with conservation grant eligibility screening and application packaging for {matched_grant_title} (Estimated Grant Request: {req_str}).

2. SCOPE OF ORIGINATION & TECHNICAL PACKAGING SERVICES ("SIGN-TO-UNLOCK" WORK PRODUCT)
Advisor provides conservation grant eligibility screening, multi-benefit project narrative drafting, preliminary line-item cost-share budget estimates, and asynchronous coordination packaging for the County Resource Conservation District (RCD), USDA Natural Resources Conservation Service (NRCS), and the administering agency. Client acknowledges that Advisor's grant selection, solicitation routing, budget estimates, and RCD Co-Sponsor Memorandum constitute proprietary work product unlocked upon execution of this Agreement. Client agrees not to circumvent Advisor by submitting, adapting, or filing Advisor's unlocked narrative, solicitation routing, or RCD handoff dossier directly or through any third party without satisfying the deferred fee obligation herein.

3. FEDERAL & STATE GRANT COMPLIANCE CLAUSE (2 CFR § 200.459 — GENERAL NON-GRANT FUNDS ONLY)
Client and Advisor expressly agree that the {fee_str} {FEE_DISPLAY_NAME} is a private administrative origination and packaging fee payable solely from Client's general, operating, or personal non-grant funds, and is NOT billed to, charged against, budgeted in, reimbursed by, or deducted from any restricted federal or state grant funds. Advisor's fee shall not appear as a line item in any agency grant budget. Consistent with federal Uniform Guidance (2 CFR § 200.459 — Professional Service Costs) and California Climate Investments / state conservation grant guidelines, every line item in the agency grant budget shall represent eligible conservation, forestry, habitat, and technical implementation costs only.

4. {fee_str} DEFERRED ADMINISTRATIVE ORIGINATION & PACKAGING FEE — PAYMENT TRIGGERS & REMEDIES
(a) $0 Upfront; 100% Waived if Grant is Not Awarded: Client pays $0 upfront for eligibility screening, narrative drafting, budget estimates, and RCD handoff packaging. If no conservation grant is awarded for the Property, the fee is waived in full and Client owes Advisor $0.
(b) Fee Calculation & Invoice Trigger: Upon official Agency Award Notice (or execution of the County RCD / Agency Grant Agreement or landowner cost-share contract for the Property, whichever occurs first), Advisor shall invoice the Deferred Administrative Origination & Packaging Fee equal to {fee_str} of the gross approved grant award amount (estimated at {fee_amt_str} based on a {req_str} award).
(c) Payment Due Date (5 Business Days): The fee is payable in full from Client's general non-grant funds within five (5) business days of initial grant tranche disbursement (or execution of the funding/reimbursement disbursement authorization).
(d) Late Fee, Venue & Prevailing-Party Attorneys' Fees: Any balance unpaid after the 5-business-day payment window shall accrue a late charge of 1.5% per month (18.0% per annum, or the maximum rate permitted under California law, whichever is less). This Agreement shall be governed by and construed under the laws of the State of California, with exclusive jurisdiction and venue vested in the state and federal courts located in Sacramento County, California. In any action or proceeding arising out of or relating to this Agreement or collection of the fee, the prevailing party shall recover its reasonable attorneys' fees, court costs, and collection expenses pursuant to California Civil Code § 1717.

5. CAL FIRE RPF, RCD & AGENCY COORDINATION DISCLOSURE
(a) Independent Packaging Consultant: Advisor is an independent private grant-packaging consultant. Advisor is NOT a Registered Professional Forester (RPF) licensed under the California Professional Foresters Law (Cal. Pub. Resources Code § 750 et seq.), and is not affiliated with, endorsed by, or acting on behalf of the California Department of Forestry and Fire Protection (CAL FIRE), the State Board of Forestry and Fire Protection, any Resource Conservation District (RCD), USDA NRCS, or any grantor agency.
(b) Licensed Forestry Work: Any forest management plan, silvicultural prescription, timber or fuels treatment layout, or other professional forestry service required by the California Forest Improvement Program (CFIP) or any similar program must be prepared and signed by a California-licensed RPF retained by Client or provided through the applicable RCD or agency. Advisor's budget figures are preliminary estimates subject to RPF, RCD, and agency verification.
(c) Agency Discretion; No Guarantee: RCD co-sponsorship, NRCS technical assistance, eligibility determinations, scoring, ranking, and award decisions rest solely with the applicable public agency. Advisor's "Tier-1 Statutory Eligibility Match" is an internal screening opinion, not an agency determination, and Advisor does not guarantee any co-sponsorship, ranking, or award.
(d) No Lobbying: Advisor does not lobby or otherwise attempt to influence any agency officer or employee regarding any award decision, and no portion of the fee is paid for such activity.

6. CALIFORNIA UETA & FEDERAL ESIGN ACT ELECTRONIC EXECUTION
By typing their Full Legal Name and Title, checking "I agree to the ZYS GoldFields PRO LLC Advisory Agreement," and submitting the digital intake form, the undersigned represents and warrants that they have authority to bind Client and Property (APN {apn}), and consents to execute this Agreement electronically pursuant to the California Uniform Electronic Transactions Act (UETA, Cal. Civ. Code § 1633.1 et seq.) and the federal Electronic Signatures in Global and National Commerce Act (ESIGN Act, 15 U.S.C. § 7001 et seq.). Client may request a paper copy of this Agreement at no charge by emailing Advisor. The system automatically records the UTC timestamp, Client IP address, browser User-Agent string, and cryptographic SHA-256 digest of this Agreement as a permanent, tamper-evident audit record."""


def render_can_spam_footer(config: MatcherConfig, recipient_email: str = "", segment: str = "") -> str:
    """CAN-SPAM Act (15 U.S.C. § 7704) compliant footer appended to every outbound commercial email.

    Includes: clear sender identification, advertisement disclosure, a valid physical postal address,
    and a functioning opt-out mechanism honored within 10 business days.
    """
    postal = config.firm_postal_address.strip() or "[REQUIRED BEFORE SENDING: set firm_postal_address in config.json]"
    optout_mailbox = config.unsubscribe_email.strip() or config.notifications.admin_email.strip()
    if optout_mailbox:
        optout = (
            f'reply with "UNSUBSCRIBE" or email {optout_mailbox} with the subject "UNSUBSCRIBE"'
        )
    else:
        optout = 'reply to this email with "UNSUBSCRIBE" in the subject line'
    recipient_line = f"This message was sent to {recipient_email} " if recipient_email else "This message was sent "
    reason = (
        "because your organization is publicly listed as a California land trust that partners with private landowners."
        if segment == "Channel_Partner"
        else "because your organization is publicly listed as a California conservation property steward."
    )
    return (
        "---\n"
        f"*This is a commercial message (advertisement) from **{config.firm_name}** — {config.firm_subtitle}.*  \n"
        f"*Mailing address: {postal}*  \n"
        f"*{recipient_line}{reason} To stop receiving emails from us, {optout}. "
        "Opt-out requests are honored within 10 business days, and we will not sell or transfer your address.*  \n"
        f"*{config.firm_name} is an independent consultant and is not affiliated with CAL FIRE, any Resource "
        "Conservation District, USDA NRCS, or any grantor agency. Eligibility screening is not a guarantee of award.*"
    )



def compute_contract_sha256(contract_text: str) -> str:
    """Return the lowercase hexadecimal SHA-256 digest of the canonical contract text."""
    normalized = contract_text.replace("\r\n", "\n").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def build_esignature_audit_record(
    intake: ClientIntake,
    grant: GrantRecord,
    engineered_grant_request: int,
    contingent_fee_dollars: int,
    config: MatcherConfig,
    signer_ip_address: str = "127.0.0.1",
    signer_user_agent: str = "ZYS-GoldFields-Digital-Portal/1.0",
    signature_timestamp_utc: str | None = None,
    signed_agreement_path: Path | str = DEFAULT_SIGNED_AGREEMENT_PATH,
) -> tuple[ESignatureAuditRecord, str]:
    """Create a validated ESignatureAuditRecord and canonical contract text with SHA-256 hash."""
    ts_utc = signature_timestamp_utc or intake.signature_timestamp_utc or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    signer_name = (intake.signer_full_name or intake.name).strip()
    signer_title = (intake.signer_title or "Owner / Authorized Representative").strip()
    ip_addr = (signer_ip_address or intake.signer_ip_address or "127.0.0.1").strip()
    ua_str = (signer_user_agent or intake.signer_user_agent or "Mozilla/5.0 (Digital Intake Portal)").strip()

    contract_text = render_canonical_contract_text(
        firm_name=config.firm_name,
        firm_subtitle=config.firm_subtitle,
        fee_percent=config.success_fee_percent,
        client_name=intake.name,
        client_email=intake.email,
        county=intake.county,
        state=intake.state,
        apn=intake.apn,
        acreage=intake.acreage,
        matched_grant_title=grant.title,
        engineered_grant_request=engineered_grant_request,
        contingent_fee_dollars=contingent_fee_dollars,
    )
    sha256_hash = compute_contract_sha256(contract_text)

    audit = ESignatureAuditRecord(
        intake_id=intake.id,
        client_name=intake.name,
        client_email=intake.email,
        county=intake.county,
        apn=intake.apn,
        signer_full_name=signer_name,
        signer_title=signer_title,
        agreement_version=AGREEMENT_VERSION,
        agreement_signed=True,
        signature_timestamp_utc=ts_utc,
        signer_ip_address=ip_addr,
        signer_user_agent=ua_str,
        contract_sha256_hash=sha256_hash,
        matched_grant_id=grant.id,
        matched_grant_title=grant.title,
        engineered_grant_request=engineered_grant_request,
        contingent_fee_dollars=contingent_fee_dollars,
        signed_agreement_path=str(signed_agreement_path),
    )
    return audit, contract_text


def render_signed_agreement_markdown(
    audit: ESignatureAuditRecord,
    contract_text: str,
    config: MatcherConfig,
    acreage: float,
    state: str = "CA",
) -> str:
    """Compose the downloadable signed_advisory_agreement.md receipt with cryptographic audit trail."""
    fee_str = config.fee_label()
    raw_md = f"""# Executed Conservation Advisory & Origination Agreement Receipt

- **Advisory Practice:** **{config.firm_name}** — *{config.firm_subtitle}*
- **Agreement Version:** `{audit.agreement_version}`
- **Execution Status:** **`EXECUTED & LEGALLY BINDING (agreement_signed = True)`**
- **Governing Law & Venue:** State of California (California UETA `Cal. Civ. Code § 1633.1 et seq.` & Federal ESIGN Act `15 U.S.C. § 7001 et seq.`) · Exclusive Venue: **Sacramento County, California**
- **Grant Accounting Compliance:** `2 CFR § 200.459` Protected — {fee_str} Deferred Fee Payable Only from General Non-Grant Funds (100% Waived if Grant is Not Awarded)
- **Agency Disclosure:** Advisor is not a CAL FIRE Registered Professional Forester and is not affiliated with CAL FIRE, any RCD, or USDA NRCS

---

## 1. Cryptographic E-Signature Audit Trail Certificate

| Audit Parameter | Verified Electronic Execution Record |
| :--- | :--- |
| **Client / Landowning Entity** | **{audit.client_name}** (`{audit.client_email}`) |
| **Property Jurisdiction & APN** | **{audit.county} County, {state}** · Assessor's Parcel Number (`APN`): **`{audit.apn}`** ({acreage:g} Acres) |
| **Executed By (Full Legal Name)** | **`/s/ {audit.signer_full_name}`** |
| **Signer Title / Capacity** | **{audit.signer_title}** |
| **Affirmative Consent Attestation** | `[x]` *"I agree to the ZYS GoldFields PRO LLC Advisory Agreement."* |
| **UTC Execution Timestamp** | `{audit.signature_timestamp_utc}` |
| **Client IP Address** | `{audit.signer_ip_address}` |
| **Browser User-Agent String** | `{audit.signer_user_agent}` |
| **Matched Grant Program** | **{audit.matched_grant_title}** (`{audit.matched_grant_id}`) |
| **Engineered Grant Request** | **${audit.engineered_grant_request:,}** |
| **{fee_str} Deferred Administrative Origination & Packaging Fee** | **${audit.contingent_fee_dollars:,}** (*$0 upfront; 100% waived if grant is not awarded; invoiced upon Agency Award Notice; due within 5 business days of initial tranche disbursement from general non-grant funds*) |
| **SHA-256 Contract Text Digest** | `{audit.contract_sha256_hash}` |

---

## 2. Full Executed Contract Text (Hashed via SHA-256)

```text
{contract_text}
```

---

## 3. Electronic Signature Execution Block

- **CLIENT / AUTHORIZED SIGNATORY:** `/s/ {audit.signer_full_name}` ({audit.signer_title})
- **LANDOWNER / ENTITY:** {audit.client_name} (`APN {audit.apn}`, {audit.county} County, {state})
- **EXECUTED TIMESTAMP (UTC):** `{audit.signature_timestamp_utc}`
- **CRYPTOGRAPHIC SHA-256 HASH:** `{audit.contract_sha256_hash}`
- **ADVISOR COUNTER-ACCEPTANCE:** **{config.firm_name}** — *{config.firm_subtitle}*
"""
    return _escape_md_dollars(raw_md)


def render_notice_of_authorized_representation(
    audit: ESignatureAuditRecord,
    config: MatcherConfig,
    rcd_name: str,
    nrcs_office: str,
    acreage: float,
    state: str = "CA",
) -> str:
    """Render the 'Notice of Authorized Advisory Representation' appended to unlocked client_submission_packet.md."""
    fee_str = config.fee_label()
    raw_notice = f"""---

## Part C: Notice of Authorized Advisory Representation & E-Signature Certificate

**NOTICE TO COUNTY RESOURCE CONSERVATION DISTRICT (RCD), USDA NRCS & GRANTING AGENCY**

- **TO:** **{rcd_name}** · **{nrcs_office}** · Administering Grant Agency
- **FROM:** **{config.firm_name}** (*{config.firm_subtitle}*) & **{audit.client_name}** (*Landowner / Applicant*)
- **SUBJECT PARCEL:** **APN `{audit.apn}`** ({acreage:g} Acres — {audit.county} County, {state})
- **MATCHED SOLICITATION:** **{audit.matched_grant_title}** (`{audit.matched_grant_id}`)

### 1. Notice of Authorized Grant Advisory & Technical Packaging Representation
Please take notice that **{audit.client_name}** (by and through its authorized signatory **{audit.signer_full_name}**, *{audit.signer_title}*) has formally executed the **Conservation Advisory & Origination Agreement** (`{audit.agreement_version}`) appointing **{config.firm_name}** (*{config.firm_subtitle}*) to prepare, package, and coordinate this conservation grant dossier and County RCD / USDA NRCS technical handoff for **APN `{audit.apn}`**.

### 2. Federal & State Grant Accounting Compliance Certification (`2 CFR § 200.459`)
Both Landowner and Advisor certify to **{rcd_name}**, **USDA NRCS**, and the administering state/federal agency that:
1. **100% Direct Conservation Project Budget:** Every dollar requested in Section A.5 (**${audit.engineered_grant_request:,}** grant request) is allocated strictly to eligible on-the-ground conservation practices, Registered Professional Forester (RPF) / NRCS technical planning, and ecological restoration. Advisor's fee does not appear in the grant budget.
3. **CAL FIRE RPF / RCD Coordination Disclosure:** **{config.firm_name}** is an independent packaging consultant, is **not** a Registered Professional Forester (Cal. Pub. Resources Code § 750 et seq.), and is not affiliated with CAL FIRE, **{rcd_name}**, or USDA NRCS. All forestry prescriptions and plans will be prepared and signed by a California-licensed RPF, and all eligibility, co-sponsorship, and award decisions remain at the sole discretion of the RCD and administering agency.
2. **General Non-Grant Funds Only (`2 CFR § 200.459`):** Advisor's **{fee_str} Deferred Administrative Origination & Packaging Fee** (**${audit.contingent_fee_dollars:,}**, 100% waived if the grant is not awarded) is a private fee paid solely from the Client's **general non-grant funds** upon official Agency Award Notice (payable within 5 business days of initial tranche disbursement, with a 1.5% monthly late fee and Sacramento County, California venue), and is **never** billed to, reimbursed by, or deducted from restricted federal or state grant funds.

### 3. Tamper-Evident E-Signature Audit Record (California UETA & Federal ESIGN Act)

| Cryptographic Audit Field | Verified Record in `grants.db` |
| :--- | :--- |
| **Electronic Signatory** | **`/s/ {audit.signer_full_name}`** — *{audit.signer_title}* |
| **Affirmative Agreement Consent** | `[x]` *"I agree to the ZYS GoldFields PRO LLC Advisory Agreement."* (`agreement_signed = True`) |
| **UTC Execution Timestamp** | `{audit.signature_timestamp_utc}` |
| **Client IP Address** | `{audit.signer_ip_address}` |
| **Browser User-Agent** | `{audit.signer_user_agent}` |
| **SHA-256 Signed Contract Hash** | `{audit.contract_sha256_hash}` |
| **Signed Receipt File** | `{Path(audit.signed_agreement_path).name}` |
"""
    return _escape_md_dollars(raw_notice)
