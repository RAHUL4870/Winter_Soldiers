"""Financial impact calculations for shipment routing decisions.

Pure functions with no I/O, database access, or side effects — fully
deterministic and unit-testable. Used by rerouting decision logic
(T-054) and financial analytics (T-068a).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OrderFinancialInput:
    """Input data for calculating one order's financial impact."""

    revenue: float
    penalty_pct: float  # e.g., 0.05 for 5%
    days_late: int
    extra_days: int  # Days beyond planned delivery
    free_days: int  # Free demurrage days
    daily_rate: float  # Demurrage cost per day
    co2_delta_kg: float  # CO2 change (positive = increase, negative = decrease)


@dataclass(frozen=True)
class OrderFinancialImpact:
    """Calculated financial impact for one order."""

    sla_penalty: float
    demurrage: float
    carbon_cost: float
    total_impact_usd: float


@dataclass(frozen=True)
class ShipmentFinancialImpact:
    """Aggregated financial impact for a shipment with multiple orders."""

    per_order_impacts: list[OrderFinancialImpact]
    total_impact_usd: float


def calculate_sla_penalty(revenue: float, penalty_pct: float, days_late: int) -> float:
    """Calculate SLA penalty for late delivery.

    Args:
        revenue: Order revenue (USD)
        penalty_pct: Penalty percentage per day late (e.g., 0.05 for 5%)
        days_late: Number of days past SLA deadline (negative or zero = no penalty)

    Returns:
        Penalty amount in USD (0.0 if not late)

    Formula:
        If days_late > 0: revenue * penalty_pct * days_late
        Otherwise: 0.0
    """
    if days_late <= 0:
        return 0.0
    return revenue * penalty_pct * days_late


def calculate_demurrage(extra_days: int, free_days: int, daily_rate: float) -> float:
    """Calculate demurrage charges for container detention.

    Args:
        extra_days: Total days container held beyond planned delivery
        free_days: Number of free days before demurrage starts
        daily_rate: Cost per day after free days (USD)

    Returns:
        Demurrage cost in USD (0.0 if within free days)

    Formula:
        If extra_days > free_days: (extra_days - free_days) * daily_rate
        Otherwise: 0.0

    Note:
        Result is always >= 0.0 (clamped if inputs would produce negative).
    """
    if extra_days <= free_days:
        return 0.0

    billable_days = extra_days - free_days
    cost = billable_days * daily_rate

    return max(0.0, cost)  # Never negative


def calculate_carbon_cost(co2_delta_kg: float, cost_per_kg: float = 0.08) -> float:
    """Calculate carbon cost/savings for route change.

    Args:
        co2_delta_kg: Change in CO2 emissions (kg).
                     Positive = increase (cost)
                     Negative = decrease (savings, represented as negative cost)
        cost_per_kg: Carbon cost per kg CO2 (default $0.08/kg)

    Returns:
        Carbon cost in USD. Positive = cost, negative = savings.

    Design decision:
        Negative CO2 delta (improvement) is allowed and returns negative cost
        (representing savings). This enables accurate net impact calculation
        when comparing routes — e.g., air freight may have higher CO2 cost
        but faster delivery may avoid SLA penalties, and the net comparison
        requires both costs and savings to be properly signed.
    """
    return co2_delta_kg * cost_per_kg


def calculate_order_financial_impact(order_input: OrderFinancialInput) -> OrderFinancialImpact:
    """Calculate complete financial impact for one order.

    Args:
        order_input: All financial parameters for the order

    Returns:
        OrderFinancialImpact with individual components and total
    """
    sla_penalty = calculate_sla_penalty(
        revenue=order_input.revenue,
        penalty_pct=order_input.penalty_pct,
        days_late=order_input.days_late,
    )

    demurrage = calculate_demurrage(
        extra_days=order_input.extra_days,
        free_days=order_input.free_days,
        daily_rate=order_input.daily_rate,
    )

    carbon_cost = calculate_carbon_cost(
        co2_delta_kg=order_input.co2_delta_kg,
    )

    total = sla_penalty + demurrage + carbon_cost

    return OrderFinancialImpact(
        sla_penalty=sla_penalty,
        demurrage=demurrage,
        carbon_cost=carbon_cost,
        total_impact_usd=total,
    )


def calculate_shipment_financial_impact(
    orders: list[OrderFinancialInput],
) -> ShipmentFinancialImpact:
    """Calculate aggregated financial impact for a multi-order shipment.

    Args:
        orders: List of order financial inputs

    Returns:
        ShipmentFinancialImpact with per-order breakdown and shipment total
    """
    per_order_impacts = [calculate_order_financial_impact(order) for order in orders]

    total = sum(impact.total_impact_usd for impact in per_order_impacts)

    return ShipmentFinancialImpact(
        per_order_impacts=per_order_impacts,
        total_impact_usd=total,
    )
