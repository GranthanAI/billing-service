"""Plan fixtures, loaded from config/plans.yaml so they cannot drift from the real catalog."""

from pathlib import Path
from uuid import UUID

from billing.adapters.catalog.yaml_loader import load_catalog
from billing.app.catalog_spec import PlanBlock, blocks
from billing.domain.catalog import OrgVariant, PlanVersion

ROOT = Path(__file__).resolve().parents[2]
SPEC = load_catalog(ROOT / "config" / "plans.yaml")
GROUP_OF = SPEC.group_of()
_BY_KEY: dict[tuple[str, OrgVariant | None], PlanBlock] = {b.key: b for b in blocks(SPEC)}


def _version(code: str, variant: OrgVariant | None, n: int) -> PlanVersion:
    return _BY_KEY[(code, variant)].to_plan_version(UUID(int=n), 1)


FREE = _version("free", None, 1)
PRO = _version("pro", None, 2)
MAX = _version("max", None, 3)
ORG_PRO = _version("org_pro", OrgVariant.COLLEGE, 4)
PAYG = _version("payg", None, 5)

FREE_LIMITS = FREE.limits
PRO_LIMITS = PRO.limits
MAX_LIMITS = MAX.limits
