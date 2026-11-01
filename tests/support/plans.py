"""Plan fixtures matching the LLD's plans.yaml (Phase 3 will load these from YAML)."""

from uuid import UUID

from billing.domain.catalog import Limits, ModelGroup, OrgVariant, PlanVersion
from billing.domain.money import Paise

FREE_LIMITS = Limits(
    messages_per_day=25,
    best_per_day=0,
    branches_per_tree=20,
    messages_per_conversation=50,
    memory_facts=50,
    context_tokens=16000,
    documents_per_tree=3,
    file_size_mb=10,
    youtube_per_month=3,
    storage_mb=100,
    custom_modes=3,
    mode_instruction_chars=1000,
)

PRO_LIMITS = Limits(
    messages_per_day=60,
    best_per_day=0,
    branches_per_tree=200,
    messages_per_conversation=200,
    memory_facts=500,
    context_tokens=64000,
    documents_per_tree=25,
    file_size_mb=50,
    youtube_per_month=20,
    storage_mb=2048,
    custom_modes=10,
    mode_instruction_chars=4000,
)

MAX_LIMITS = Limits(
    messages_per_day=80,
    best_per_day=12,
    branches_per_tree=-1,
    messages_per_conversation=500,
    memory_facts=2000,
    context_tokens=128000,
    documents_per_tree=100,
    file_size_mb=100,
    youtube_per_month=60,
    storage_mb=10240,
    custom_modes=20,
    mode_instruction_chars=4000,
)

FREE = PlanVersion(
    id=UUID(int=1),
    plan_code="free",
    version=1,
    rank=0,
    variant=None,
    price_month=None,
    price_year=None,
    limits=FREE_LIMITS,
    allowed_groups=frozenset({ModelGroup.FAST}),
    extra_models=frozenset({"openai/gpt-oss-120b"}),
)
PRO = PlanVersion(
    id=UUID(int=2),
    plan_code="pro",
    version=1,
    rank=1,
    variant=None,
    price_month=Paise(49900),
    price_year=Paise(499000),
    limits=PRO_LIMITS,
    allowed_groups=frozenset({ModelGroup.FAST, ModelGroup.SMART}),
)
MAX = PlanVersion(
    id=UUID(int=3),
    plan_code="max",
    version=1,
    rank=2,
    variant=None,
    price_month=Paise(149900),
    price_year=Paise(1499000),
    limits=MAX_LIMITS,
    allowed_groups=frozenset({ModelGroup.FAST, ModelGroup.SMART, ModelGroup.BEST}),
)
ORG_PRO = PlanVersion(
    id=UUID(int=4),
    plan_code="org_pro",
    version=1,
    rank=1,
    variant=OrgVariant.COLLEGE,
    price_month=None,
    price_year=Paise(298800),
    limits=PRO_LIMITS,
    allowed_groups=frozenset({ModelGroup.FAST, ModelGroup.SMART}),
)
PAYG = PlanVersion(
    id=UUID(int=5),
    plan_code="payg",
    version=1,
    rank=0,
    variant=None,
    price_month=None,
    price_year=None,
    limits=FREE_LIMITS,
    allowed_groups=frozenset(ModelGroup),
)
