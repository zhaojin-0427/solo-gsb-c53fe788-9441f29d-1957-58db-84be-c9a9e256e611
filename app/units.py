"""Mass and concentration unit conversion.

Mass units are normalized to grams, concentrations to ppm (parts per million).
"""

MASS_TO_GRAMS = {
    "mg": 0.001,
    "g": 1.0,
    "kg": 1000.0,
    "oz": 28.349523125,
    "lb": 453.59237,
}

CONCENTRATION_TO_PPM = {
    "ppb": 0.001,
    "ppm": 1.0,
    "mg/kg": 1.0,
    "g/kg": 1000.0,
    "%": 10000.0,
}

#: ppm value equivalent to a 100% mass fraction.
PURE_SUBSTANCE_PPM = 1_000_000.0


class UnknownUnitError(ValueError):
    pass


def mass_to_grams(quantity: float, unit: str) -> float:
    try:
        factor = MASS_TO_GRAMS[unit]
    except KeyError:
        raise UnknownUnitError(f"unknown mass unit: {unit!r}") from None
    return quantity * factor


def concentration_to_ppm(value: float, unit: str) -> float:
    try:
        factor = CONCENTRATION_TO_PPM[unit]
    except KeyError:
        raise UnknownUnitError(f"unknown concentration unit: {unit!r}") from None
    return value * factor


def ppm_to_fraction(ppm: float) -> float:
    return ppm / PURE_SUBSTANCE_PPM
