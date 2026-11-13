#!/usr/bin/env python3
"""Print planning budgets from a local JSON file; no network or credentials."""

import argparse
import json
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path


def money(value: Decimal) -> str:
    return f"${value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}"


def estimate(config: dict, meetings: int) -> dict:
    usage = config["usage_per_meeting"]
    whatsapp = config["whatsapp"]
    shared = config["shared"]
    templates = meetings * usage["whatsapp_template_messages"]
    services = meetings * usage["service_messages"]
    calls = meetings * usage["ai_calls"]
    free_allowance = whatsapp["free_service_messages_per_number_per_month"]
    service_rate = Decimal(whatsapp["service_usd_per_delivered_message"])
    service_cost = Decimal(max(0, services - free_allowance)) * service_rate
    utility_cost = (
        Decimal(templates)
        * Decimal(whatsapp["utility_usd_per_delivered_message"])
        + service_cost
    )
    marketing_cost = (
        Decimal(templates)
        * Decimal(whatsapp["marketing_usd_per_delivered_message"])
        + service_cost
    )
    ai_cost = Decimal(calls) * (
        Decimal(usage["input_tokens_per_call"])
        * Decimal(shared["ai_input_usd_per_million_tokens"])
        + Decimal(usage["billable_output_tokens_per_call"])
        * Decimal(shared["ai_output_usd_per_million_tokens"])
    ) / Decimal(1_000_000)
    infrastructure = Decimal(shared["vm_usd_per_month"]) * (
        Decimal(1) + Decimal(shared["daily_backup_fraction_of_vm"])
    )
    base_cost = infrastructure + ai_cost
    return {
        "templates": templates,
        "services": services,
        "ai": ai_cost,
        "wa_utility": utility_cost,
        "wa_marketing": marketing_cost,
        "telegram_total": base_cost,
        "wa_utility_total": base_cost + utility_cost,
        "wa_marketing_total": base_cost + marketing_cost,
        "extra_if_no_service_allowance": Decimal(min(services, free_allowance))
        * service_rate,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--assumptions",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "docs"
        / "pricing-assumptions.json",
    )
    args = parser.parse_args()
    config = json.loads(args.assumptions.read_text())
    print(f"Planning estimates as of {config['as_of']}; {config['currency']} monthly.")
    print(config["status"])
    print("WhatsApp utility/marketing cases change ALL template messages' category.")
    print("Shared hosting and AI are counted once in each alternative deployment.")
    print()
    print("| Meetings | Templates | Service | AI | WA messages: utility | "
          "WA messages: marketing | Telegram total | WA total: utility | "
          "WA total: marketing | Extra if allowance unavailable |")
    print("|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for scenario in config["scenarios"]:
        meetings = scenario["meetings_per_month"]
        result = estimate(config, meetings)
        values = [str(meetings), str(result["templates"]), str(result["services"])]
        values.extend(money(result[key]) for key in (
            "ai", "wa_utility", "wa_marketing", "telegram_total",
            "wa_utility_total", "wa_marketing_total",
            "extra_if_no_service_allowance",
        ))
        print("| " + " | ".join(values) + " |")
    print()
    print("Excluded: " + "; ".join(config["exclusions"]) + ".")


if __name__ == "__main__":
    main()
