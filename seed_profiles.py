"""Seed realistic Northern California landowner test profiles into grants.db."""

from __future__ import annotations

from db import DEFAULT_DB_PATH, fetch_all_profiles, seed_landowner_profiles
from models import LandownerProfile


SAMPLE_PROFILES: list[LandownerProfile] = [
    LandownerProfile(
        name="Elena & Marcus Vance (Sierra Ridge Oak Woodland)",
        state="CA",
        county="El Dorado",
        acreage=45.0,
        land_type="Degraded Oak Woodland & Mixed Conifer Forest",
        features=(
            "45-acre degraded blue oak, black oak, and ponderosa pine woodland in El Dorado County "
            "experiencing post-drought canopy mortality, invasive brush encroachment, wildfire fuel "
            "accumulation, and seasonal gully soil erosion; seeking Registered Professional Forester "
            "management plan, native oak reforestation, understory thinning, soil health stabilization, "
            "and deer/songbird wildlife habitat restoration."
        ),
    ),
    LandownerProfile(
        name="David & Rosa Morales (Cosumnes Valley Family Ranch)",
        state="CA",
        county="Sacramento",
        acreage=120.0,
        land_type="Working Family Ranch & Riparian Agricultural Corridor",
        features=(
            "120-acre multi-generational cattle and irrigated pasture family ranch in Sacramento County "
            "bordering a Cosumnes River tributary; seeking solar photovoltaic irrigation pumping, "
            "riparian streambank restoration, instream culvert replacement for native fish passage, "
            "regenerative agriculture rotational grazing, soil health carbon sequestration, and "
            "long-term agricultural conservation easement protection against suburban conversion."
        ),
    ),
    LandownerProfile(
        name="Dr. Priya Nair (Bear River Foothill Wildlife Sanctuary)",
        state="CA",
        county="Placer",
        acreage=15.0,
        land_type="Private Wildlife Sanctuary & Vernal Pool Riparian Preserve",
        features=(
            "15-acre dedicated wildlife sanctuary in Placer County containing a perennial foothill "
            "stream corridor, native willow-cottonwood riparian woodland, seasonal vernal pool wetlands, "
            "threatened amphibian and migratory bird wildlife habitat, aquatic barrier removal needs, "
            "and native perennial grassland soil health restoration."
        ),
    ),
]


def run_seed(db_path=DEFAULT_DB_PATH) -> list[LandownerProfile]:
    """Insert the 3 Northern California landowner profiles and return stored rows."""
    seed_landowner_profiles(SAMPLE_PROFILES, db_path=db_path, reset_existing=True)
    stored = fetch_all_profiles(db_path=db_path)
    return stored


if __name__ == "__main__":
    profiles = run_seed()
    print(f"=== Seeded {len(profiles)} Northern California Landowner Profiles into grants.db ===")
    for p in profiles:
        print(
            f"[{p.id}] {p.name} — {p.acreage:.0f} acres | {p.county} County, {p.state} | {p.land_type}"
        )
