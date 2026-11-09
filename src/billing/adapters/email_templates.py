"""Renders a billing email (subject, text, html) from templates/email/<name>.*."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import jinja2

from billing.app.invoices import _date as ist_date_label
from billing.domain.money import Paise
from billing.domain.words import format_inr

TEMPLATES = Path(__file__).resolve().parent / "templates"
SIMULATED_PREFIX = "[SIMULATED] "
PRODUCT = "GranthanAI"
PLAN_NAMES = {"free": "Free", "pro": "Pro", "max": "Max", "payg": "Pay as you Go"}


def inr(value: object) -> str:
    """₹1,49,900.00: the invoice code's Indian digit grouping, with the rupee sign."""
    return "₹" + format_inr(Paise(int(value)))  # type: ignore[call-overload]


def ist_date(value: object) -> str:
    """'9 Nov 2026' in IST, from an ISO timestamp or a datetime."""
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return str(ist_date_label(dt))


_ENV = jinja2.Environment(
    loader=jinja2.FileSystemLoader(TEMPLATES),
    # html is escaped, subjects and text are not (matched by the template's file name)
    autoescape=jinja2.select_autoescape(["html"]),
    undefined=jinja2.StrictUndefined,
    keep_trailing_newline=True,
)
_ENV.filters["inr"] = inr
_ENV.filters["ist_date"] = ist_date


@dataclass(frozen=True, slots=True)
class RenderedEmail:
    subject: str
    text: str
    html: str


def plan_name(code: str) -> str:
    return PLAN_NAMES.get(code, code.replace("_", " ").title())


def render_email(
    template: str, data: Mapping[str, Any], *, simulated: bool = False
) -> RenderedEmail:
    """`data` must hold the common keys (account_email, support_email, billing_url) and the
    template's own. Simulation: subject prefix and a first line saying no money moved."""
    ctx: dict[str, Any] = {"product": PRODUCT, "simulated": simulated, **data}
    if "plan_code" in ctx and "plan_name" not in ctx:
        ctx["plan_name"] = plan_name(str(ctx["plan_code"]))
    subject = _ENV.get_template(f"email/{template}.subject.txt").render(**ctx).strip()
    if simulated:
        subject = SIMULATED_PREFIX + subject
    ctx["subject"] = subject
    text = _ENV.get_template(f"email/{template}.txt").render(**ctx)
    html = _ENV.get_template(f"email/{template}.html").render(**ctx)
    return RenderedEmail(subject=subject, text=text, html=html)
