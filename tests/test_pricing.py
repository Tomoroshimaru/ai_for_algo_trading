"""Offline tests for the pricing engine (Step 10): reference cases, sign
conventions, limiting cases, euro/american agreement, perf benchmark."""
import math
import time

import numpy as np
import pytest

from pricing.engine import (
    PricingRequest, PricingResult, price, european_price, american_price,
    european_price_array, CALL, PUT, EUROPEAN, AMERICAN,
)
from iv.inversion import black76_price


# --- reference cases (Hull textbook) --------------------------------------- #
def test_reference_atm_call():
    # S=100,K=100,T=1,r=5%,q=0,sigma=20% -> call ~ 10.4506
    px = european_price(100, 100, 1.0, 0.20, CALL, rate=0.05)
    assert abs(px - 10.4506) < 1e-3


def test_put_call_parity_european():
    S, K, T, r, q, sig = 100, 95, 0.75, 0.03, 0.01, 0.25
    c = european_price(S, K, T, sig, CALL, r, q)
    p = european_price(S, K, T, sig, PUT, r, q)
    lhs = c - p
    rhs = S * math.exp(-q * T) - K * math.exp(-r * T)
    assert abs(lhs - rhs) < 1e-9


def test_consistent_with_inversion_black76():
    # european(spot, q) == black76(F=S e^{(r-q)T}, DF=e^{-rT})
    S, K, T, r, q, sig = 100, 110, 0.5, 0.04, 0.02, 0.3
    F = S * math.exp((r - q) * T)
    df = math.exp(-r * T)
    for right in (CALL, PUT):
        assert abs(european_price(S, K, T, sig, right, r, q)
                   - black76_price(F, K, T, sig, right, df)) < 1e-10


# --- Greeks sign conventions ----------------------------------------------- #
def test_greek_signs_and_ranges():
    r = price(PricingRequest(100, 100, 1.0, 0.2, CALL, rate=0.05))
    assert 0 < r.delta < 1 and r.gamma > 0 and r.vega > 0
    assert r.theta < 0 and r.rho > 0          # call: theta<0, rho>0
    rp = price(PricingRequest(100, 100, 1.0, 0.2, PUT, rate=0.05))
    assert -1 < rp.delta < 0 and rp.rho < 0    # put: delta<0, rho<0


def test_delta_matches_finite_difference():
    S, K, T, sig, r = 100, 105, 0.5, 0.25, 0.03
    ana = price(PricingRequest(S, K, T, sig, CALL, rate=r)).delta
    h = 1e-4 * S
    fd = (european_price(S + h, K, T, sig, CALL, r)
          - european_price(S - h, K, T, sig, CALL, r)) / (2 * h)
    assert abs(ana - fd) < 1e-6


def test_vega_unit_helpers():
    r = price(PricingRequest(100, 100, 1.0, 0.2, CALL, rate=0.05))
    assert abs(r.vega_per_pct - r.vega / 100) < 1e-12
    assert abs(r.theta_per_day - r.theta / 365) < 1e-12


# --- limiting cases -------------------------------------------------------- #
def test_zero_ttm_returns_intrinsic():
    assert european_price(120, 100, 0.0, 0.2, CALL) == 20.0
    assert european_price(80, 100, 0.0, 0.2, PUT) == 20.0
    assert american_price(120, 100, 0.0, 0.2, CALL) == 20.0


def test_zero_vol_is_discounted_intrinsic():
    S, K, T, r = 100, 90, 1.0, 0.05
    px = european_price(S, K, T, 0.0, CALL, r)
    assert abs(px - (S * math.exp(0) - K * math.exp(-r * T))) < 1e-9  # q=0, F=S e^{rT}


# --- European vs American agreement ---------------------------------------- #
def test_american_call_no_dividend_equals_european():
    # No dividends -> never optimal to exercise American call early.
    S, K, T, sig, r = 100, 100, 1.0, 0.2, 0.05
    eu = european_price(S, K, T, sig, CALL, r, 0.0)
    am = american_price(S, K, T, sig, CALL, r, 0.0, steps=400)
    assert abs(eu - am) < 1e-2


def test_american_put_premium_over_european():
    # American put >= European put (early exercise has value).
    S, K, T, sig, r = 100, 100, 1.0, 0.2, 0.08
    eu = european_price(S, K, T, sig, PUT, r, 0.0)
    am = american_price(S, K, T, sig, PUT, r, 0.0, steps=400)
    assert am > eu - 1e-9 and am >= eu


def test_american_greeks_reasonable():
    res = price(PricingRequest(100, 100, 1.0, 0.2, PUT, AMERICAN, rate=0.06, steps=200))
    assert -1 < res.delta < 0 and res.gamma > 0 and res.vega > 0
    assert res.method == "crr_binomial_fd" and res.n_steps == 200


# --- typed API validation -------------------------------------------------- #
def test_invalid_inputs_raise():
    with pytest.raises(ValueError):
        PricingRequest(100, 100, 1.0, 0.2, right="X")
    with pytest.raises(ValueError):
        PricingRequest(100, 100, -1.0, 0.2, CALL)


# --- vectorized + perf ----------------------------------------------------- #
def test_vectorized_matches_scalar():
    Ks = np.array([80, 90, 100, 110, 120], float)
    vec = european_price_array(100.0, Ks, 1.0, 0.2, CALL, rate=0.05)
    sca = [european_price(100, k, 1.0, 0.2, CALL, 0.05) for k in Ks]
    assert np.allclose(vec, sca, atol=1e-10)


def test_perf_vectorized_10k():
    Ks = np.linspace(50, 150, 10_000)
    t0 = time.perf_counter()
    european_price_array(100.0, Ks, 1.0, 0.2, CALL, rate=0.05)
    assert time.perf_counter() - t0 < 2.0   # generous CI-safe budget
