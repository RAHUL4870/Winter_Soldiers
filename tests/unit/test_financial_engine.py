"""Unit tests for financial impact calculation engine.

Tests use exact known numeric values, not just structural assertions,
to establish baseline correctness that later ML/rerouting tasks will
trust without re-verifying the math.
"""

from __future__ import annotations

from nexafreight.services.financial_engine import (
    OrderFinancialInput,
    calculate_carbon_cost,
    calculate_demurrage,
    calculate_order_financial_impact,
    calculate_shipment_financial_impact,
    calculate_sla_penalty,
)


def test_calculate_sla_penalty_zero_days_late() -> None:
    """No penalty when delivery is on time or early."""
    # Arithmetic: days_late <= 0 → penalty = 0
    result = calculate_sla_penalty(revenue=10000.0, penalty_pct=0.05, days_late=0)
    assert result == 0.0

    result_early = calculate_sla_penalty(revenue=10000.0, penalty_pct=0.05, days_late=-3)
    assert result_early == 0.0


def test_calculate_sla_penalty_positive_days_late() -> None:
    """Penalty calculated correctly for late delivery."""
    # Arithmetic: 1000 * 0.05 * 3 = 150.0
    result = calculate_sla_penalty(revenue=1000.0, penalty_pct=0.05, days_late=3)
    assert result == 150.0

    # Arithmetic: 50000 * 0.10 * 7 = 35000.0
    result_large = calculate_sla_penalty(revenue=50000.0, penalty_pct=0.10, days_late=7)
    assert result_large == 35000.0


def test_calculate_demurrage_within_free_days() -> None:
    """No demurrage when within free days."""
    # Arithmetic: extra_days (2) <= free_days (5) → cost = 0
    result = calculate_demurrage(extra_days=2, free_days=5, daily_rate=100.0)
    assert result == 0.0

    # Boundary case: exactly at free days
    result_boundary = calculate_demurrage(extra_days=5, free_days=5, daily_rate=100.0)
    assert result_boundary == 0.0


def test_calculate_demurrage_beyond_free_days() -> None:
    """Demurrage charged correctly for days beyond free period."""
    # Arithmetic: (10 - 3) * 50.0 = 350.0
    result = calculate_demurrage(extra_days=10, free_days=3, daily_rate=50.0)
    assert result == 350.0

    # Arithmetic: (8 - 5) * 200.0 = 600.0
    result_higher_rate = calculate_demurrage(extra_days=8, free_days=5, daily_rate=200.0)
    assert result_higher_rate == 600.0


def test_calculate_carbon_cost_positive_delta() -> None:
    """Positive CO2 delta produces positive cost (increase)."""
    # Arithmetic: 1000 * 0.08 = 80.0
    result = calculate_carbon_cost(co2_delta_kg=1000.0, cost_per_kg=0.08)
    assert result == 80.0


def test_calculate_carbon_cost_custom_rate() -> None:
    """Custom cost_per_kg scales result correctly."""
    # Arithmetic: 500 * 0.12 = 60.0
    result = calculate_carbon_cost(co2_delta_kg=500.0, cost_per_kg=0.12)
    assert result == 60.0


def test_calculate_carbon_cost_negative_delta() -> None:
    """Negative CO2 delta (improvement) produces negative cost (savings).

    Design decision verification: negative delta is allowed and represents
    savings, enabling accurate net impact comparison between routes.
    """
    # Arithmetic: -200 * 0.08 = -16.0 (savings)
    result = calculate_carbon_cost(co2_delta_kg=-200.0, cost_per_kg=0.08)
    assert result == -16.0


def test_calculate_order_financial_impact_full_scenario() -> None:
    """Full order impact calculation with all components."""
    order_input = OrderFinancialInput(
        revenue=20000.0,
        penalty_pct=0.05,
        days_late=2,  # SLA penalty: 20000 * 0.05 * 2 = 2000.0
        extra_days=7,
        free_days=3,
        daily_rate=100.0,  # Demurrage: (7-3) * 100 = 400.0
        co2_delta_kg=500.0,  # Carbon: 500 * 0.08 = 40.0
    )

    result = calculate_order_financial_impact(order_input)

    # Individual components
    assert result.sla_penalty == 2000.0
    assert result.demurrage == 400.0
    assert result.carbon_cost == 40.0

    # Total: 2000 + 400 + 40 = 2440.0
    assert result.total_impact_usd == 2440.0


def test_calculate_shipment_financial_impact_multi_order() -> None:
    """Multi-order shipment aggregation is correct."""
    orders = [
        # Order 1: Late with demurrage
        OrderFinancialInput(
            revenue=10000.0,
            penalty_pct=0.05,
            days_late=3,  # SLA: 10000 * 0.05 * 3 = 1500.0
            extra_days=5,
            free_days=2,
            daily_rate=50.0,  # Demurrage: (5-2) * 50 = 150.0
            co2_delta_kg=200.0,  # Carbon: 200 * 0.08 = 16.0
        ),  # Total: 1666.0
        # Order 2: On time, no demurrage
        OrderFinancialInput(
            revenue=5000.0,
            penalty_pct=0.05,
            days_late=0,  # SLA: 0.0
            extra_days=1,
            free_days=3,
            daily_rate=50.0,  # Demurrage: 0.0
            co2_delta_kg=100.0,  # Carbon: 100 * 0.08 = 8.0
        ),  # Total: 8.0
        # Order 3: Very late, high demurrage
        OrderFinancialInput(
            revenue=30000.0,
            penalty_pct=0.10,
            days_late=5,  # SLA: 30000 * 0.10 * 5 = 15000.0
            extra_days=10,
            free_days=3,
            daily_rate=200.0,  # Demurrage: (10-3) * 200 = 1400.0
            co2_delta_kg=1000.0,  # Carbon: 1000 * 0.08 = 80.0
        ),  # Total: 16480.0
    ]

    result = calculate_shipment_financial_impact(orders)

    # Verify per-order totals
    assert result.per_order_impacts[0].total_impact_usd == 1666.0
    assert result.per_order_impacts[1].total_impact_usd == 8.0
    assert result.per_order_impacts[2].total_impact_usd == 16480.0

    # Verify shipment-level total: 1666 + 8 + 16480 = 18154.0
    assert result.total_impact_usd == 18154.0


def test_split_reroute_savings_scenario() -> None:
    """Comparing two routing scenarios produces correct net savings.

    Anticipates T-054's evaluate_split() need: financial engine output
    must be directly comparable to calculate rerouting savings.
    """
    # Scenario A: Stay on current slow route (all orders late)
    current_route_orders = [
        OrderFinancialInput(
            revenue=10000.0,
            penalty_pct=0.05,
            days_late=5,  # SLA: 10000 * 0.05 * 5 = 2500.0
            extra_days=8,
            free_days=3,
            daily_rate=100.0,  # Demurrage: (8-3) * 100 = 500.0
            co2_delta_kg=0.0,  # Carbon: 0.0 (baseline)
        ),  # Total: 3000.0
        OrderFinancialInput(
            revenue=15000.0,
            penalty_pct=0.05,
            days_late=5,  # SLA: 15000 * 0.05 * 5 = 3750.0
            extra_days=8,
            free_days=3,
            daily_rate=100.0,  # Demurrage: (8-3) * 100 = 500.0
            co2_delta_kg=0.0,  # Carbon: 0.0 (baseline)
        ),  # Total: 4250.0
    ]
    current_route_impact = calculate_shipment_financial_impact(current_route_orders)
    # Total: 3000 + 4250 = 7250.0
    assert current_route_impact.total_impact_usd == 7250.0

    # Scenario B: Reroute high-priority order via air (faster, higher CO2, avoids penalties)
    reroute_orders = [
        OrderFinancialInput(
            revenue=10000.0,
            penalty_pct=0.05,
            days_late=5,  # Still late (not priority)
            extra_days=8,
            free_days=3,
            daily_rate=100.0,
            co2_delta_kg=0.0,  # Total: 3000.0 (unchanged)
        ),
        OrderFinancialInput(
            revenue=15000.0,
            penalty_pct=0.05,
            days_late=0,  # SLA: 0.0 (on time via air)
            extra_days=0,
            free_days=3,
            daily_rate=100.0,  # Demurrage: 0.0 (on time)
            co2_delta_kg=800.0,  # Carbon: 800 * 0.08 = 64.0 (higher CO2)
        ),  # Total: 64.0
    ]
    reroute_impact = calculate_shipment_financial_impact(reroute_orders)
    # Total: 3000 + 64 = 3064.0
    assert reroute_impact.total_impact_usd == 3064.0

    # Net savings: 7250 - 3064 = 4186.0
    net_savings = current_route_impact.total_impact_usd - reroute_impact.total_impact_usd
    assert net_savings == 4186.0
