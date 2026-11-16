"""Numbers of the calculator: exact where possible, 42 digits where not (standard library only).

A value is a ``fractions.Fraction``. The four operations are exact, so
``1 / 3 * 3`` is 1 and ``0.1 + 0.2`` is 0.3. A result that cannot be exact
(a root that does not come out, a logarithm, a sine) is evaluated with
``decimal`` at 60 digits and kept at 42 significant digits; the display
shows at most 32, so ten guard digits lie behind every digit shown.

``normalize`` is applied to every result. It

* reports an overflow at 10**10000 (the limit of Windows Calculator) and
  turns an underflow below 10**-10000 into 0;
* cuts a fraction whose denominator has grown past 160 bits to 42
  significant digits, so that a long calculation cannot grow without bound;
* snaps a value that lies within 1 part in 10**38 of a number with at most
  34 significant digits to that number: that is where the square of the
  square root of 2 becomes exactly 2 again.

A sum or difference that is 38 orders of magnitude smaller than its terms
is 0 (``add``, ``subtract``): no value carries digits that far down.

Angles: degrees and gradians are reduced exactly before any use of pi, and
the sines and cosines of the multiples of 30 and 45 degrees are returned
exactly; in radians a result smaller than 10**-38 for an argument that is
not itself that small is taken as 0 (the sine of the 42-digit pi).
"""

from __future__ import annotations

import decimal
import math
from decimal import Decimal
from fractions import Fraction
from functools import lru_cache

WORK_DIGITS = 42
CALC_DIGITS = 60
SNAP_DIGITS = 34
SNAP_EXPONENT = 38
OVERFLOW_EXPONENT = 10000
MAX_FACTORIAL = 3248                  # 3249! is the first factorial above 10**10000
MAX_TRIG_EXPONENT = 100               # |x| >= 10**100 is refused by the trigonometric functions
_DENOMINATOR_BITS = 160
_EXACT_POWER_BITS = 4096              # a rational power is evaluated exactly up to this size
_OVERFLOW_BITS = 33220                # 10**10000 has 33220 bits
_LN10 = 2.302585092994046
_LOG10_2 = 0.30102999566398120

DEG, RAD, GRAD = "deg", "rad", "grad"
ANGLE_UNITS = (DEG, RAD, GRAD)

DIVIDE_BY_ZERO = "divide_by_zero"
UNDEFINED = "undefined"
DOMAIN = "domain"
OVERFLOW = "overflow"
NO_RESULT = "no_result"
MESSAGES = {
    DIVIDE_BY_ZERO: "Cannot divide by zero",
    UNDEFINED: "Result is undefined",
    DOMAIN: "Invalid input",
    OVERFLOW: "Overflow",
    NO_RESULT: "Result not defined",
}


class CalcError(ArithmeticError):
    """A calculation has no result; ``code`` says why and ``message`` is what the display shows."""

    def __init__(self, code: str) -> None:
        super().__init__(MESSAGES[code])
        self.code = code
        self.message = MESSAGES[code]


def _context(precision: int = CALC_DIGITS) -> decimal.Context:
    return decimal.Context(prec=precision, rounding=decimal.ROUND_HALF_EVEN, Emax=decimal.MAX_EMAX,
                           Emin=decimal.MIN_EMIN, capitals=1, clamp=0,
                           traps=[decimal.InvalidOperation, decimal.DivisionByZero, decimal.Overflow])


_CTX = _context()
_WORK_CTX = _context(WORK_DIGITS)
_ZERO = Fraction(0)
_ONE = Fraction(1)


# --- digits ------------------------------------------------------------------------------------------------

def _digit_count(n: int) -> int:
    """The number of decimal digits of a positive integer (without converting it to text)."""
    count = int((n.bit_length() - 1) * _LOG10_2) + 1
    if n >= 10 ** count:
        count += 1
    elif n < 10 ** (count - 1):
        count -= 1
    return count


def _decimal_exponent(n: int, d: int) -> int:
    """E with 10**(E-1) <= n/d < 10**E for positive integers n and d."""
    estimate = _digit_count(n) - _digit_count(d)
    # n/d lies in (10**(estimate-1), 10**(estimate+1)): one comparison decides.
    if estimate >= 0:
        return estimate + 1 if n >= d * 10 ** estimate else estimate
    return estimate + 1 if n * 10 ** (-estimate) >= d else estimate


def digits_of(value: Fraction, significant: int) -> tuple[str, int]:
    """``value`` (not 0) rounded half away from zero to ``significant`` digits.

    Returns the digits without trailing zeros and the exponent E: the
    magnitude is ``0.digits * 10**E``, so E is the number of digits before
    the decimal point (0 or less for a number below 1).
    """
    if value == 0:
        raise ValueError("zero has no significant digits")
    n, d = abs(value.numerator), value.denominator
    exponent = _decimal_exponent(n, d)
    shift = significant - exponent
    if shift >= 0:
        scaled, remainder = divmod(n * 10 ** shift, d)
        divisor = d
    else:
        divisor = d * 10 ** (-shift)
        scaled, remainder = divmod(n, divisor)
    if remainder * 2 >= divisor:
        scaled += 1
    text = str(scaled)
    if len(text) > significant:          # 9.99 rounded up to 10.0
        exponent += 1
    text = text.rstrip("0")
    return text, exponent


def round_significant(value: Fraction, significant: int) -> Fraction:
    """``value`` rounded half away from zero to ``significant`` digits."""
    if value == 0:
        return _ZERO
    digits, exponent = digits_of(value, significant)
    result = Fraction(int(digits)) * Fraction(10) ** (exponent - len(digits))
    return -result if value < 0 else result


def normalize(value: Fraction | int) -> Fraction:
    """Bring a result into the range and precision of the calculator (see the module text)."""
    value = Fraction(value)
    n, d = value.numerator, value.denominator
    if n == 0:
        return _ZERO
    bits = abs(n).bit_length() - d.bit_length()
    if bits > _OVERFLOW_BITS + 2 or (bits >= _OVERFLOW_BITS - 2 and abs(n) >= d * 10 ** OVERFLOW_EXPONENT):
        raise CalcError(OVERFLOW)
    if bits < -_OVERFLOW_BITS - 2 or (bits <= -_OVERFLOW_BITS + 2 and abs(n) * 10 ** OVERFLOW_EXPONENT < d):
        return _ZERO
    if d.bit_length() > _DENOMINATOR_BITS:
        value = round_significant(value, WORK_DIGITS)
    if value.denominator == 1 and abs(value.numerator) < 10 ** SNAP_DIGITS:
        return value                      # an integer the display can show in full: nothing to snap
    snapped = round_significant(value, SNAP_DIGITS)
    if snapped != value and abs(value - snapped) * 10 ** SNAP_EXPONENT <= abs(value):
        return snapped
    return value


def to_decimal(value: Fraction, context: decimal.Context = _CTX) -> Decimal:
    if value.denominator == 1:
        return context.create_decimal(value.numerator)
    return context.divide(Decimal(value.numerator), Decimal(value.denominator))


def from_decimal(value: Decimal) -> Fraction:
    """A decimal result as a value: rounded to the working precision, then normalised."""
    if not value.is_finite():
        raise CalcError(OVERFLOW if value.is_infinite() else DOMAIN)
    if value.adjusted() >= OVERFLOW_EXPONENT:
        raise CalcError(OVERFLOW)
    if value.adjusted() < -OVERFLOW_EXPONENT - 1:
        return _ZERO
    return normalize(Fraction(_WORK_CTX.plus(value)))


def is_integer(value: Fraction) -> bool:
    return value.denominator == 1


# --- constants ---------------------------------------------------------------------------------------------

@lru_cache(maxsize=32)
def pi_decimal(precision: int = CALC_DIGITS) -> Decimal:
    """Pi to ``precision`` significant digits (Machin's formula on integers)."""
    guard = precision + 10
    one = 10 ** guard

    def arctan_inverse(x: int) -> int:
        total = term = one // x
        square, n, sign = x * x, 3, -1
        while term:
            term //= square
            total += sign * (term // n)
            n += 2
            sign = -sign
        return total

    value = 4 * (4 * arctan_inverse(5) - arctan_inverse(239))
    # Decimal methods without a context argument round to the 28 digits of the default context.
    return _context(precision).scaleb(Decimal(value), Decimal(-guard))


PI = Fraction(_context(WORK_DIGITS).plus(pi_decimal(CALC_DIGITS)))
E = Fraction(_context(WORK_DIGITS).plus(_CTX.exp(Decimal(1))))


# --- elementary functions on decimals ---------------------------------------------------------------------

def _sin_cos_reduced(x: Decimal, context: decimal.Context) -> tuple[Decimal, Decimal]:
    """(sin x, cos x) for |x| <= pi, by Taylor series after halving the argument three times."""
    work = context.copy()
    work.prec = context.prec + 10
    y = work.divide(x, Decimal(8))
    # Unary minus and abs() of a Decimal round to the default context (28 digits): copy_negate and
    # copy_abs are used instead throughout.
    minus_y2 = work.multiply(y, y).copy_negate()
    # sine and cosine of y
    sine = term = y
    n = 1
    while True:
        term = work.divide(work.multiply(term, minus_y2), Decimal((n + 1) * (n + 2)))
        if term == 0 or term.adjusted() < sine.adjusted() - work.prec - 2:
            break
        sine = work.add(sine, term)
        n += 2
    cosine = term = Decimal(1)
    n = 0
    while True:
        term = work.divide(work.multiply(term, minus_y2), Decimal((n + 1) * (n + 2)))
        if term == 0 or term.adjusted() < -work.prec - 2:
            break
        cosine = work.add(cosine, term)
        n += 2
    for _ in range(3):                    # double the angle back
        sine, cosine = (work.multiply(Decimal(2), work.multiply(sine, cosine)),
                        work.subtract(work.multiply(cosine, cosine), work.multiply(sine, sine)))
    return context.plus(sine), context.plus(cosine)


def _atan_decimal(x: Decimal, context: decimal.Context) -> Decimal:
    work = context.copy()
    work.prec = context.prec + 12
    negative = x < 0
    x = x.copy_abs()
    invert = x > 1
    if invert:
        x = work.divide(Decimal(1), x)
    halvings = 0
    while x > Decimal("0.05"):            # atan x = 2 atan(x / (1 + sqrt(1 + x*x)))
        x = work.divide(x, work.add(Decimal(1), work.sqrt(work.add(Decimal(1), work.multiply(x, x)))))
        halvings += 1
    minus_x2 = work.multiply(x, x).copy_negate()
    total = term = x
    n = 1
    while True:
        term = work.multiply(term, minus_x2)
        n += 2
        piece = work.divide(term, Decimal(n))
        if piece == 0 or piece.adjusted() < total.adjusted() - work.prec - 2:
            break
        total = work.add(total, piece)
    total = work.multiply(total, Decimal(2 ** halvings))
    if invert:
        total = work.subtract(work.divide(pi_decimal(work.prec), Decimal(2)), total)
    return context.plus(total.copy_negate() if negative else total)


# --- roots and powers --------------------------------------------------------------------------------------

def _integer_root(n: int, k: int) -> int | None:
    """The exact k-th root of the non-negative integer n, or None."""
    if n < 2:
        return n
    if k == 2:
        root = math.isqrt(n)
        return root if root * root == n else None
    bits = n.bit_length()
    if bits < k:                          # 1 < n < 2**k has no integer k-th root
        return None
    low, high = 1 << ((bits - 1) // k), 1 << (bits // k + 1)
    while low <= high:
        middle = (low + high) // 2
        power = middle ** k
        if power == n:
            return middle
        if power < n:
            low = middle + 1
        else:
            high = middle - 1
    return None


def _exact_root(value: Fraction, k: int) -> Fraction | None:
    """The exact k-th root of a positive value, when numerator and denominator are both k-th powers."""
    if k > 4096 or value.numerator.bit_length() > 40000 or value.denominator.bit_length() > 40000:
        return None
    top = _integer_root(value.numerator, k)
    if top is None:
        return None
    bottom = _integer_root(value.denominator, k)
    if bottom is None:
        return None
    return Fraction(top, bottom)


def _log10_estimate(value: Fraction) -> float:
    """log10 of a positive value, good to a few digits, for overflow checks."""
    n, d = value.numerator, value.denominator
    shift_n, shift_d = max(0, n.bit_length() - 1000), max(0, d.bit_length() - 1000)
    return (math.log10(n >> shift_n) - math.log10(d >> shift_d)) + (shift_n - shift_d) * math.log10(2)


def _integer_power(base: Fraction, exponent: int) -> Fraction:
    if exponent == 0:
        return _ONE
    if base == 0:
        if exponent < 0:
            raise CalcError(DIVIDE_BY_ZERO)
        return _ZERO
    size = _log10_estimate(abs(base)) * exponent
    if size > OVERFLOW_EXPONENT + 1:
        raise CalcError(OVERFLOW)
    if size < -OVERFLOW_EXPONENT - 2:
        return _ZERO
    magnitude = abs(exponent)
    bits = max(abs(base.numerator).bit_length(), base.denominator.bit_length()) * magnitude
    if base.denominator == 1 or base.numerator in (1, -1) or bits <= _EXACT_POWER_BITS:
        return normalize(base ** exponent)
    result = _CTX.power(to_decimal(abs(base)), Decimal(exponent))
    if base < 0 and magnitude % 2:
        result = result.copy_negate()
    return from_decimal(result)


def power(base: Fraction, exponent: Fraction) -> Fraction:
    """``base`` to the ``exponent``: exact for integer exponents and exact roots, else 42 digits."""
    if exponent.denominator == 1:
        return _integer_power(base, exponent.numerator)
    if base == 0:
        if exponent < 0:
            raise CalcError(DIVIDE_BY_ZERO)
        return _ZERO
    p, q = exponent.numerator, exponent.denominator
    negative = False
    if base < 0:
        if q % 2 == 0:
            raise CalcError(DOMAIN)       # an even root of a negative number
        negative = bool(p % 2)
        base = -base
    size = _log10_estimate(base) * float(exponent)
    if size > OVERFLOW_EXPONENT + 1:
        raise CalcError(OVERFLOW)
    if size < -OVERFLOW_EXPONENT - 2:
        return _ZERO
    root = _exact_root(base, q)
    if root is not None and abs(p) * max(root.numerator.bit_length(), root.denominator.bit_length()) <= 40000:
        result = _integer_power(root, p)
    else:
        result = from_decimal(_CTX.power(to_decimal(base), to_decimal(exponent)))
    return -result if negative else result


def root(value: Fraction, degree: Fraction) -> Fraction:
    """The ``degree``-th root of ``value``."""
    if degree == 0:
        raise CalcError(DIVIDE_BY_ZERO)
    return power(value, 1 / degree)


def sqrt(value: Fraction) -> Fraction:
    if value < 0:
        raise CalcError(DOMAIN)
    if value == 0:
        return _ZERO
    exact = _exact_root(value, 2)
    if exact is not None:
        return exact
    return from_decimal(_CTX.sqrt(to_decimal(value)))


def cube_root(value: Fraction) -> Fraction:
    return power(value, Fraction(1, 3))


# --- logarithms and exponentials ----------------------------------------------------------------------------

def _power_of_ten(value: Fraction) -> int | None:
    """k when value is exactly 10**k."""
    n, d = value.numerator, value.denominator
    if d == 1 and n > 0:
        count = _digit_count(n) - 1
        return count if n == 10 ** count else None
    if n == 1:
        count = _digit_count(d) - 1
        return -count if d == 10 ** count else None
    return None


def log10(value: Fraction) -> Fraction:
    if value <= 0:
        raise CalcError(DOMAIN)
    exact = _power_of_ten(value)
    if exact is not None:
        return Fraction(exact)
    return from_decimal(_CTX.log10(to_decimal(value)))


def ln(value: Fraction) -> Fraction:
    if value <= 0:
        raise CalcError(DOMAIN)
    if value == 1:
        return _ZERO
    return from_decimal(_CTX.ln(to_decimal(value)))


def log_base(value: Fraction, base: Fraction) -> Fraction:
    """The logarithm of ``value`` to ``base``."""
    if value <= 0 or base <= 0:
        raise CalcError(DOMAIN)
    if base == 1:
        raise CalcError(DIVIDE_BY_ZERO)
    return from_decimal(_CTX.divide(_CTX.ln(to_decimal(value)), _CTX.ln(to_decimal(base))))


def exp(value: Fraction) -> Fraction:
    if value == 0:
        return _ONE
    if value == 1:
        return E
    if value > (OVERFLOW_EXPONENT + 1) * _LN10:
        raise CalcError(OVERFLOW)
    if value < -(OVERFLOW_EXPONENT + 2) * _LN10:
        return _ZERO
    return from_decimal(_CTX.exp(to_decimal(value)))


# --- factorial ------------------------------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _bernoulli() -> list[Fraction]:
    """B(2), B(4), ... B(60)."""
    count = 61
    values = [Fraction(0)] * count
    values[0] = Fraction(1)
    for m in range(1, count):
        total = Fraction(0)
        for k in range(m):
            total += math.comb(m + 1, k) * values[k]
        values[m] = -total / (m + 1)
    return [values[2 * k] for k in range(1, 31)]


def _gamma_decimal(x: Decimal, context: decimal.Context) -> Decimal:
    """Gamma(x) for a decimal x that is not an integer <= 0 (Stirling's series after shifting x past 60)."""
    work = context.copy()
    work.prec = context.prec + 15
    shift_product = Decimal(1)
    z = x
    while z < 60:
        shift_product = work.multiply(shift_product, z)
        z = work.add(z, Decimal(1))
    half_log_two_pi = work.divide(work.ln(work.multiply(Decimal(2), pi_decimal(work.prec))), Decimal(2))
    log_gamma = work.add(work.subtract(work.multiply(work.subtract(z, Decimal("0.5")), work.ln(z)), z),
                         half_log_two_pi)
    z_power = z
    z2 = work.multiply(z, z)
    for index, number in enumerate(_bernoulli(), start=1):
        coefficient = number / (2 * index * (2 * index - 1))
        term = work.divide(work.divide(Decimal(coefficient.numerator), Decimal(coefficient.denominator)), z_power)
        log_gamma = work.add(log_gamma, term)
        z_power = work.multiply(z_power, z2)
        if term != 0 and term.adjusted() < log_gamma.adjusted() - work.prec:
            break
    return context.divide(work.exp(log_gamma), shift_product)


def factorial(value: Fraction) -> Fraction:
    """n! for a whole n >= 0; Gamma(x + 1) for any other x that is not a negative whole number."""
    if value.denominator == 1:
        n = value.numerator
        if n < 0:
            raise CalcError(DOMAIN)
        if n > MAX_FACTORIAL:
            raise CalcError(OVERFLOW)
        return Fraction(math.factorial(n))
    if value > MAX_FACTORIAL + 1:
        raise CalcError(OVERFLOW)
    if value < -MAX_FACTORIAL:
        return _ZERO
    return from_decimal(_gamma_decimal(to_decimal(value + 1), _CTX))


# --- trigonometry ---------------------------------------------------------------------------------------------

_SIN_DEGREES = {0: 0, 30: Fraction(1, 2), 90: 1, 150: Fraction(1, 2), 180: 0, 210: Fraction(-1, 2), 270: -1,
                330: Fraction(-1, 2)}
_COS_DEGREES = {0: 1, 60: Fraction(1, 2), 90: 0, 120: Fraction(-1, 2), 180: -1, 240: Fraction(-1, 2), 270: 0,
                300: Fraction(1, 2)}
_TINY = Fraction(1, 10 ** SNAP_EXPONENT)
_NOT_TINY = Fraction(1, 10 ** 30)


def _check_trig_range(value: Fraction) -> None:
    if value != 0 and abs(value) >= 10 ** MAX_TRIG_EXPONENT:
        raise CalcError(DOMAIN)


def _sin_cos(value: Fraction, unit: str) -> tuple[Fraction, Fraction]:
    """(sin, cos) of an angle in ``unit``, each exact where the angle allows it."""
    _check_trig_range(value)
    if unit in (DEG, GRAD):
        degrees = (value * Fraction(9, 10) if unit == GRAD else value) % 360
        sine, cosine = _SIN_DEGREES.get(degrees), _COS_DEGREES.get(degrees)
        if sine is not None and cosine is not None:
            return Fraction(sine), Fraction(cosine)
        if degrees > 180:
            degrees -= 360
        radians = _CTX.divide(_CTX.multiply(to_decimal(degrees), pi_decimal(CALC_DIGITS + 5)), Decimal(180))
        s, c = _sin_cos_reduced(radians, _CTX)
        return (Fraction(sine) if sine is not None else from_decimal(s),
                Fraction(cosine) if cosine is not None else from_decimal(c))
    if value == 0:
        return _ZERO, _ONE
    # Radians: reduce modulo 2 pi with enough digits of pi for the size of the argument.
    extra = max(0, _decimal_exponent(abs(value.numerator), value.denominator))
    context = _context(CALC_DIGITS + extra + 5)
    two_pi = context.multiply(pi_decimal(CALC_DIGITS + extra + 5), Decimal(2))
    reduced = context.remainder_near(to_decimal(value, context), two_pi)
    s, c = _sin_cos_reduced(_CTX.plus(reduced), _CTX)
    sine, cosine = from_decimal(s), from_decimal(c)
    if abs(value) > _NOT_TINY:
        if abs(sine) < _TINY:
            sine = _ZERO
        if abs(cosine) < _TINY:
            cosine = _ZERO
    return sine, cosine


def sin(value: Fraction, unit: str = RAD) -> Fraction:
    return _sin_cos(value, unit)[0]


def cos(value: Fraction, unit: str = RAD) -> Fraction:
    return _sin_cos(value, unit)[1]


def tan(value: Fraction, unit: str = RAD) -> Fraction:
    sine, cosine = _sin_cos(value, unit)
    if cosine == 0:
        raise CalcError(DOMAIN)
    return normalize(sine / cosine)


def sec(value: Fraction, unit: str = RAD) -> Fraction:
    cosine = _sin_cos(value, unit)[1]
    if cosine == 0:
        raise CalcError(DIVIDE_BY_ZERO)
    return normalize(1 / cosine)


def csc(value: Fraction, unit: str = RAD) -> Fraction:
    sine = _sin_cos(value, unit)[0]
    if sine == 0:
        raise CalcError(DIVIDE_BY_ZERO)
    return normalize(1 / sine)


def cot(value: Fraction, unit: str = RAD) -> Fraction:
    sine, cosine = _sin_cos(value, unit)
    if sine == 0:
        raise CalcError(DIVIDE_BY_ZERO)
    return normalize(cosine / sine)


def _angle_from_radians(radians: Decimal, unit: str) -> Fraction:
    if unit == DEG:
        return from_decimal(_CTX.divide(_CTX.multiply(radians, Decimal(180)), pi_decimal(CALC_DIGITS + 5)))
    if unit == GRAD:
        return from_decimal(_CTX.divide(_CTX.multiply(radians, Decimal(200)), pi_decimal(CALC_DIGITS + 5)))
    return from_decimal(radians)


def _right_angles(quarters: Fraction, unit: str) -> Fraction:
    """``quarters`` right angles in ``unit``, exactly (in radians: a multiple of the calculator's pi)."""
    if unit == DEG:
        return normalize(quarters * 90)
    if unit == GRAD:
        return normalize(quarters * 100)
    return normalize(quarters * PI / 2)


def asin(value: Fraction, unit: str = RAD) -> Fraction:
    if abs(value) > 1:
        raise CalcError(DOMAIN)
    if value == 0:
        return _ZERO
    if abs(value) == 1:
        return _right_angles(Fraction(1 if value > 0 else -1), unit)
    # 1 - x*x is formed exactly, so an argument close to 1 loses no digits.
    radians = _atan_decimal(_CTX.divide(to_decimal(value), _CTX.sqrt(to_decimal(1 - value * value))), _CTX)
    return _angle_from_radians(radians, unit)


def acos(value: Fraction, unit: str = RAD) -> Fraction:
    if abs(value) > 1:
        raise CalcError(DOMAIN)
    if value == 1:
        return _ZERO
    if value == 0:
        return _right_angles(Fraction(1), unit)
    if value == -1:
        return _right_angles(Fraction(2), unit)
    # acos x = 2 atan(sqrt((1 - x) / (1 + x))): no cancellation anywhere in (-1, 1)
    half = _atan_decimal(_CTX.sqrt(to_decimal((1 - value) / (1 + value))), _CTX)
    return _angle_from_radians(_CTX.multiply(half, Decimal(2)), unit)


def atan(value: Fraction, unit: str = RAD) -> Fraction:
    if value == 0:
        return _ZERO
    if abs(value) == 1:
        return _right_angles(Fraction(1 if value > 0 else -1, 2), unit)
    return _angle_from_radians(_atan_decimal(to_decimal(value), _CTX), unit)


def asec(value: Fraction, unit: str = RAD) -> Fraction:
    if value == 0:
        raise CalcError(DOMAIN)
    return acos(1 / value, unit)


def acsc(value: Fraction, unit: str = RAD) -> Fraction:
    if value == 0:
        raise CalcError(DOMAIN)
    return asin(1 / value, unit)


def acot(value: Fraction, unit: str = RAD) -> Fraction:
    if value == 0:
        return _right_angles(Fraction(1), unit)
    return atan(1 / value, unit)


# --- hyperbolic functions ------------------------------------------------------------------------------------

def _check_exp_range(value: Fraction) -> None:
    if abs(value) > (OVERFLOW_EXPONENT + 2) * _LN10:
        raise CalcError(OVERFLOW)


def _exp_pair(value: Fraction) -> tuple[Decimal, Decimal, decimal.Context]:
    """(e**x, e**-x) with extra digits for a small x, where their difference cancels."""
    extra = 0
    if value != 0 and abs(value) < 1:
        extra = -_decimal_exponent(abs(value.numerator), value.denominator) + 2
    context = _context(CALC_DIGITS + min(extra, 200))
    x = to_decimal(value, context)
    grown = context.exp(x)
    return grown, context.divide(Decimal(1), grown), context


def sinh(value: Fraction) -> Fraction:
    if value == 0:
        return _ZERO
    _check_exp_range(value)
    if abs(value) < Fraction(1, 10 ** 100):
        return value                      # sinh x = x + x**3/6 + ...
    grown, shrunk, context = _exp_pair(value)
    return from_decimal(context.divide(context.subtract(grown, shrunk), Decimal(2)))


def cosh(value: Fraction) -> Fraction:
    if value == 0:
        return _ONE
    _check_exp_range(value)
    grown, shrunk, context = _exp_pair(value)
    return from_decimal(context.divide(context.add(grown, shrunk), Decimal(2)))


def tanh(value: Fraction) -> Fraction:
    if value == 0:
        return _ZERO
    if abs(value) > 200:
        return _ONE if value > 0 else -_ONE
    if abs(value) < Fraction(1, 10 ** 100):
        return value
    grown, shrunk, context = _exp_pair(value)
    return from_decimal(context.divide(context.subtract(grown, shrunk), context.add(grown, shrunk)))


def sech(value: Fraction) -> Fraction:
    if abs(value) > (OVERFLOW_EXPONENT + 2) * _LN10:
        return _ZERO
    return normalize(1 / cosh(value))


def csch(value: Fraction) -> Fraction:
    if value == 0:
        raise CalcError(DIVIDE_BY_ZERO)
    if abs(value) > (OVERFLOW_EXPONENT + 2) * _LN10:
        return _ZERO
    return normalize(1 / sinh(value))


def coth(value: Fraction) -> Fraction:
    if value == 0:
        raise CalcError(DIVIDE_BY_ZERO)
    return normalize(1 / tanh(value))


def asinh(value: Fraction) -> Fraction:
    if value == 0:
        return _ZERO
    magnitude = abs(value)
    if magnitude < Fraction(1, 10 ** 12):
        # x - x**3/6 + 3 x**5/40: the next term is below 10**-84 times x
        result = magnitude - magnitude ** 3 / 6 + 3 * magnitude ** 5 / 40
        result = normalize(result)
    else:
        x = to_decimal(magnitude)
        result = from_decimal(_CTX.ln(_CTX.add(x, _CTX.sqrt(_CTX.add(_CTX.multiply(x, x), Decimal(1))))))
    return -result if value < 0 else result


def acosh(value: Fraction) -> Fraction:
    if value < 1:
        raise CalcError(DOMAIN)
    if value == 1:
        return _ZERO
    x = to_decimal(value)
    return from_decimal(_CTX.ln(_CTX.add(x, _CTX.sqrt(_CTX.subtract(_CTX.multiply(x, x), Decimal(1))))))


def atanh(value: Fraction) -> Fraction:
    if abs(value) >= 1:
        raise CalcError(DOMAIN)
    if value == 0:
        return _ZERO
    if abs(value) < Fraction(1, 10 ** 12):
        return normalize(value + value ** 3 / 3 + value ** 5 / 5)
    ratio = to_decimal((1 + value) / (1 - value))
    return from_decimal(_CTX.divide(_CTX.ln(ratio), Decimal(2)))


def asech(value: Fraction) -> Fraction:
    if value <= 0 or value > 1:
        raise CalcError(DOMAIN)
    return acosh(1 / value)


def acsch(value: Fraction) -> Fraction:
    if value == 0:
        raise CalcError(DOMAIN)
    return asinh(1 / value)


def acoth(value: Fraction) -> Fraction:
    if abs(value) <= 1:
        raise CalcError(DOMAIN)
    return atanh(1 / value)


# --- the four operations and friends ----------------------------------------------------------------------

def _cancelled(result: Fraction, a: Fraction, b: Fraction) -> Fraction:
    """0 when a sum is 38 orders of magnitude below its terms: that is beyond the digits a value carries."""
    if result != 0 and abs(result) * 10 ** SNAP_EXPONENT < max(abs(a), abs(b)):
        return _ZERO
    return result


def add(a: Fraction, b: Fraction) -> Fraction:
    return normalize(_cancelled(a + b, a, b))


def subtract(a: Fraction, b: Fraction) -> Fraction:
    return normalize(_cancelled(a - b, a, b))


def multiply(a: Fraction, b: Fraction) -> Fraction:
    return normalize(a * b)


def divide(a: Fraction, b: Fraction) -> Fraction:
    if b == 0:
        raise CalcError(UNDEFINED if a == 0 else DIVIDE_BY_ZERO)
    return normalize(a / b)


def modulo(a: Fraction, b: Fraction) -> Fraction:
    """a mod b with the sign of the divisor (the modulus of scientific mode)."""
    if b == 0:
        raise CalcError(UNDEFINED)
    return normalize(a - b * math.floor(a / b))


def reciprocal(value: Fraction) -> Fraction:
    if value == 0:
        raise CalcError(DIVIDE_BY_ZERO)
    return normalize(1 / value)


def floor(value: Fraction) -> Fraction:
    return Fraction(math.floor(value))


def ceil(value: Fraction) -> Fraction:
    return Fraction(math.ceil(value))


def truncate(value: Fraction) -> Fraction:
    return Fraction(math.trunc(value))


def to_dms(value: Fraction) -> Fraction:
    """Decimal degrees to degrees.minutes-seconds: 2.5 becomes 2.30 (2 degrees 30 minutes)."""
    sign = -1 if value < 0 else 1
    value = abs(value)
    degrees = math.floor(value)
    minutes_full = (value - degrees) * 60
    minutes = math.floor(minutes_full)
    seconds = (minutes_full - minutes) * 60
    return normalize(sign * (degrees + Fraction(minutes, 100) + seconds / 10000))


def from_dms(value: Fraction) -> Fraction:
    """Degrees.minutes-seconds to decimal degrees: 2.30 becomes 2.5."""
    sign = -1 if value < 0 else 1
    value = abs(value)
    degrees = math.floor(value)
    minutes_full = (value - degrees) * 100
    minutes = math.floor(minutes_full)
    seconds = (minutes_full - minutes) * 100
    return normalize(sign * (degrees + Fraction(minutes, 60) + seconds / 3600))


# --- text --------------------------------------------------------------------------------------------------

FLOAT, SCIENTIFIC = "float", "scientific"
_MAX_ZEROS_AFTER_POINT = 2


def group_digits(digits: str, separator: str, size: int = 3) -> str:
    """Insert ``separator`` between groups of ``size`` digits, counted from the right."""
    if not separator or len(digits) <= size:
        return digits
    head = len(digits) % size or size
    parts = [digits[:head]] + [digits[index:index + size] for index in range(head, len(digits), size)]
    return separator.join(parts)


def format_number(value: Fraction, precision: int = 32, notation: str = FLOAT, *, decimal_point: str = ".",
                  group_separator: str = ",") -> str:
    """``value`` as the display shows it.

    At most ``precision`` significant digits, rounded half away from zero.
    In ``FLOAT`` notation the number is written out when it fits: a number
    with more than ``precision`` digits before the point, or with more than
    two zeros after the point and too many digits to show them all, is
    written with an exponent (``1.e+32``, ``3.333e-4``), as Windows
    Calculator does. ``SCIENTIFIC`` always writes the exponent (the F-E key).
    The integer part is grouped with ``group_separator``.
    """
    if value == 0:
        return "0" if notation == FLOAT else f"0{decimal_point}e+0"
    sign = "-" if value < 0 else ""
    digits, exponent = digits_of(value, precision)
    scientific = notation == SCIENTIFIC
    if not scientific:
        if exponent > precision:
            scientific = True
        elif len(digits) - exponent > precision:
            if exponent >= -_MAX_ZEROS_AFTER_POINT:
                # Room for the zeros after the point is taken from the digits.
                shorter, new_exponent = digits_of(value, precision + exponent)
                if new_exponent != exponent:
                    return format_number(round_significant(value, precision + exponent), precision, notation,
                                         decimal_point=decimal_point, group_separator=group_separator)
                digits = shorter
            else:
                scientific = True
    if scientific:
        shown = exponent - 1
        return f"{sign}{digits[0]}{decimal_point}{digits[1:]}e{'-' if shown < 0 else '+'}{abs(shown)}"
    if exponent <= 0:
        return f"{sign}0{decimal_point}{'0' * -exponent}{digits}"
    whole = digits[:exponent].ljust(exponent, "0")
    fraction = digits[exponent:]
    text = sign + group_digits(whole, group_separator)
    return f"{text}{decimal_point}{fraction}" if fraction else text


def parse_number(text: str, *, decimal_point: str = ".", group_separator: str = ",") -> Fraction | None:
    """A number from pasted text (``1,234.5``, ``-2.5e-3``, ``1.e+10``), or None when it is not one."""
    if not isinstance(text, str) or len(text) > 400:
        return None
    text = text.strip().replace("−", "-").replace("\u00a0", "").replace("\u202f", "").replace(" ", "")
    if group_separator and group_separator != decimal_point:
        text = text.replace(group_separator, "")
    if decimal_point != ".":
        if "." in text:
            return None
        text = text.replace(decimal_point, ".")
    if not text or text.count(".") > 1:
        return None
    mantissa, _, exponent = text.lower().partition("e")
    sign = 1
    if mantissa[:1] in ("+", "-"):
        sign = -1 if mantissa[0] == "-" else 1
        mantissa = mantissa[1:]
    whole, _, fraction = mantissa.partition(".")
    if not (whole or fraction) or not (whole + fraction).isascii() or not (whole + fraction).isdigit():
        return None
    power_of_ten = 0
    if "e" in text.lower():
        body = exponent[1:] if exponent[:1] in ("+", "-") else exponent
        if not body or not body.isascii() or not body.isdigit() or len(body) > 5:
            return None
        power_of_ten = int(exponent)
    try:
        return normalize(sign * Fraction(int(whole + fraction or "0")) * Fraction(10) ** (power_of_ten - len(fraction)))
    except CalcError:
        return None
