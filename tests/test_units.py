import pytest

from app.units import (
    UnknownUnitError,
    concentration_to_ppm,
    mass_to_grams,
    ppm_to_fraction,
)


@pytest.mark.parametrize(
    "qty,unit,expected",
    [
        (1, "g", 1.0),
        (1000, "mg", 1.0),
        (1, "kg", 1000.0),
        (1, "oz", 28.349523125),
        (1, "lb", 453.59237),
    ],
)
def test_mass_conversion(qty, unit, expected):
    assert mass_to_grams(qty, unit) == expected


@pytest.mark.parametrize(
    "value,unit,expected",
    [
        (1, "ppm", 1.0),
        (1000, "ppb", 1.0),
        (1, "mg/kg", 1.0),
        (1, "g/kg", 1000.0),
        (1, "%", 10000.0),
    ],
)
def test_concentration_conversion(value, unit, expected):
    assert concentration_to_ppm(value, unit) == expected


def test_unknown_units():
    with pytest.raises(UnknownUnitError):
        mass_to_grams(1, "stone")
    with pytest.raises(UnknownUnitError):
        concentration_to_ppm(1, "carrots")


def test_ppm_to_fraction():
    assert ppm_to_fraction(1_000_000) == 1.0
    assert ppm_to_fraction(10) == 1e-5
