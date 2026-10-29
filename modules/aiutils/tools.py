"""AI utilities: calculator, converters and text tools (namespaced aiutils.*).

Pure stdlib, no network, no storage. Every handler is a pure function of
its arguments: (user, args, drive=None) -> dict. Errors are returned as
{"error": ...} so the agent can recover and explain them to the user.
Inputs are validated and size-capped so one bad call cannot burn the
whole step budget or context window.
"""

import ast
import base64
import binascii
import difflib
import hashlib
import math
import re
import statistics
import unicodedata
from datetime import UTC, datetime
from fractions import Fraction
from urllib.parse import quote, unquote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.services.agent_service import register_tool

MAX_TEXT = 100_000
MAX_ITEMS = 1000
MAX_LINES = 1000


# --------------------------------------------------------------------------
# Validation helpers
# --------------------------------------------------------------------------
def _err(msg):
    return {"error": msg}


def _is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _num(x, name, lo=None, hi=None, integer=False):
    """Coerce a JSON number, with range/type checks. Returns (ok, value|msg)."""
    if not _is_num(x):
        return False, f"'{name}' must be a number."
    if isinstance(x, float) and not math.isfinite(x):
        return False, f"'{name}' must be finite."
    if integer and (isinstance(x, bool) or int(x) != x):
        return False, f"'{name}' must be a whole number."
    v = int(x) if integer else x
    if lo is not None and v < lo:
        return False, f"'{name}' must be >= {lo}."
    if hi is not None and v > hi:
        return False, f"'{name}' must be <= {hi}."
    return True, v


def _str(x, name, max_len=MAX_TEXT, allow_empty=False):
    if not isinstance(x, str):
        return False, f"'{name}' must be a string."
    if not allow_empty and not x.strip():
        return False, f"'{name}' must not be empty."
    if len(x) > max_len:
        return False, f"'{name}' is too long (max {max_len} chars)."
    return True, x


def _num_list(x, name):
    if not isinstance(x, list) or not x:
        return False, f"'{name}' must be a non-empty list of numbers."
    if len(x) > MAX_ITEMS:
        return False, f"'{name}' has too many items (max {MAX_ITEMS})."
    if any(not _is_num(v) for v in x):
        return False, f"'{name}' must contain only numbers."
    if any(isinstance(v, float) and not math.isfinite(v) for v in x):
        return False, f"'{name}' must contain only finite numbers."
    return True, [float(v) for v in x]


def _r2(x):
    return round(float(x), 2)


# --------------------------------------------------------------------------
# Math: safe expression evaluator (never eval())
# --------------------------------------------------------------------------
def _checked_round(x, ndigits=0):
    ok, n = _num(ndigits, "ndigits", lo=-10, hi=15, integer=True)
    if not ok:
        raise ValueError(n)
    return round(float(x), n)


def _checked_factorial(x):
    ok, n = _num(x, "value", lo=0, hi=1000, integer=True)
    if not ok:
        raise ValueError(n)
    return math.factorial(int(n))


def _checked_gcd(*args):
    if len(args) < 2:
        raise ValueError("gcd needs at least 2 numbers.")
    ints = []
    for a in args:
        ok, n = _num(a, "value", integer=True)
        if not ok:
            raise ValueError(n)
        ints.append(int(n))
    return math.gcd(*ints)


def _checked_lcm(*args):
    if len(args) < 2:
        raise ValueError("lcm needs at least 2 numbers.")
    ints = []
    for a in args:
        ok, n = _num(a, "value", integer=True)
        if not ok:
            raise ValueError(n)
        ints.append(abs(int(n)))
    return math.lcm(*ints)


def _checked_minmax(fn, args):
    if len(args) < 2:
        raise ValueError("min/max need at least 2 numbers.")
    for a in args:
        if not _is_num(a):
            raise ValueError("min/max need numbers.")
    return fn(args)


_CALC_FUNCS = {
    "sqrt": (math.sqrt, 1, 1), "cbrt": (lambda x: -(-x) ** (1 / 3) if x < 0 else x ** (1 / 3), 1, 1),
    "sin": (math.sin, 1, 1), "cos": (math.cos, 1, 1), "tan": (math.tan, 1, 1),
    "asin": (math.asin, 1, 1), "acos": (math.acos, 1, 1), "atan": (math.atan, 1, 1),
    "log": (math.log, 1, 2), "ln": (math.log, 1, 1), "log10": (math.log10, 1, 1),
    "log2": (math.log2, 1, 1), "exp": (math.exp, 1, 1),
    "abs": (abs, 1, 1), "round": (_checked_round, 1, 2),
    "floor": (math.floor, 1, 1), "ceil": (math.ceil, 1, 1),
    "factorial": (_checked_factorial, 1, 1), "gcd": (_checked_gcd, 2, None),
    "lcm": (_checked_lcm, 2, None), "min": (lambda *a: _checked_minmax(min, a), 2, None),
    "max": (lambda *a: _checked_minmax(max, a), 2, None),
    "degrees": (math.degrees, 1, 1), "radians": (math.radians, 1, 1),
    "pow": (math.pow, 2, 2),
}
_CALC_NAMES = {"pi": math.pi, "e": math.e, "tau": math.tau}
_CALC_NODES = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Call, ast.Name,
               ast.Load, ast.Constant, ast.Add, ast.Sub, ast.Mult, ast.Div,
               ast.FloorDiv, ast.USub, ast.UAdd, ast.Pow)


def _eval_calc(node, depth=0):
    if depth > 60:
        raise ValueError("Expression is too deeply nested.")
    if isinstance(node, ast.Expression):
        return _eval_calc(node.body, depth + 1)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError("Only plain numbers are allowed.")
        return node.value
    if isinstance(node, ast.Name):
        if node.id in _CALC_NAMES:
            return _CALC_NAMES[node.id]
        raise ValueError(f"Unknown name '{node.id}'. Use pi, e or tau.")
    if isinstance(node, ast.UnaryOp):
        v = _eval_calc(node.operand, depth + 1)
        if isinstance(node.op, ast.USub):
            return -v
        if isinstance(node.op, ast.UAdd):
            return +v
        raise ValueError("Operator not allowed.")
    if isinstance(node, ast.BinOp):
        left = _eval_calc(node.left, depth + 1)
        right = _eval_calc(node.right, depth + 1)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, (ast.Div, ast.FloorDiv)):
            if right == 0:
                raise ValueError("Division by zero.")
            return left / right if isinstance(node.op, ast.Div) else left // right
        if isinstance(node.op, ast.Pow):
            if abs(right) > 10000:
                raise ValueError("Exponent is too large.")
            try:
                return left ** right
            except (OverflowError, ValueError) as exc:
                raise ValueError(f"Cannot compute that power: {exc}.") from exc
        raise ValueError("Operator not allowed.")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _CALC_FUNCS:
            raise ValueError("Only math functions (sqrt, sin, log, ...) may be called.")
        if node.keywords:
            raise ValueError("Keyword arguments are not allowed.")
        fn, lo, hi = _CALC_FUNCS[node.func.id]
        if len(node.args) < lo or (hi is not None and len(node.args) > hi):
            raise ValueError(f"{node.func.id} takes {lo}" + (f" to {hi} arguments." if hi != lo else " argument(s)."))
        vals = [_eval_calc(a, depth + 1) for a in node.args]
        try:
            return fn(*vals)
        except ValueError as exc:
            raise ValueError(str(exc) or "Invalid math operation.") from exc
    raise ValueError("Expression element not allowed.")


def _tool_calculate(user, args, drive=None):
    ok, expr = _str(args.get("expression"), "expression", max_len=500)
    if not ok:
        return _err(expr)
    # Friendly syntax: ^ means power, trailing % means percent.
    expr = expr.replace("^", "**")
    expr = re.sub(r"(\d+(?:\.\d+)?)%", r"(\1/100)", expr)
    expr = expr.replace(")%", ")/100")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return _err("Could not parse that expression. Use numbers, +-*/%^, parentheses and math functions (sqrt, sin, log, ...).")
    if sum(1 for _ in ast.walk(tree)) > 400:
        return _err("Expression is too complex.")
    try:
        result = _eval_calc(tree)
    except (ValueError, ArithmeticError, OverflowError) as exc:
        return _err(str(exc) or "Could not evaluate that expression.")
    if isinstance(result, float):
        if not math.isfinite(result):
            return _err("Result is not a finite number.")
        if result.is_integer() and abs(result) < 1e15:
            result = int(result)
    return {"expression": args.get("expression"), "result": result}


# --------------------------------------------------------------------------
# Math: statistics, bases, fractions
# --------------------------------------------------------------------------
def _tool_statistics(user, args, drive=None):
    ok, values = _num_list(args.get("values"), "values")
    if not ok:
        return _err(values)
    op = str(args.get("operation", "all")).lower()
    if op not in ("all", "mean", "median", "stdev", "min", "max", "sum"):
        return _err("'operation' must be one of: all, mean, median, stdev, min, max, sum.")
    out = {"count": len(values)}
    try:
        all_stats = {
            "mean": statistics.mean(values),
            "median": statistics.median(values),
            "stdev": statistics.stdev(values) if len(values) >= 2 else 0.0,
            "min": min(values),
            "max": max(values),
            "sum": sum(values),
        }
    except statistics.StatisticsError as exc:
        return _err(f"Cannot compute statistics: {exc}.")
    if op == "all":
        out["results"] = all_stats
    else:
        out[op] = all_stats[op]
    return out


_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"


def _tool_base_convert(user, args, drive=None):
    ok, raw = _str(args.get("value"), "value", max_len=64)
    if not ok:
        return _err(raw)
    for key in ("from_base", "to_base"):
        ok, _ = _num(args.get(key), key, lo=2, hi=36, integer=True)
        if not ok:
            return _err(_)
    from_base, to_base = int(args["from_base"]), int(args["to_base"])
    text = raw.strip()
    negative = text.startswith("-")
    if negative:
        text = text[1:]
    for prefix, base in (("0x", 16), ("0b", 2), ("0o", 8)):
        if base == from_base and text.lower().startswith(prefix):
            text = text[len(prefix):]
            break
    if not text or any(c.lower() not in _DIGITS[:from_base] for c in text):
        return _err(f"'{raw.strip()}' is not a valid base-{from_base} number.")
    try:
        number = int(text, from_base)
    except ValueError:
        return _err(f"'{raw.strip()}' is not a valid base-{from_base} number.")
    if negative:
        number = -number
    if number == 0:
        return {"result": "0"}
    sign = "-" if number < 0 else ""
    number = abs(number)
    out = ""
    while number:
        number, rem = divmod(number, to_base)
        out = _DIGITS[rem] + out
    return {"result": (sign + out).upper()}


def _tool_fraction(user, args, drive=None):
    raw = args.get("value")
    if not _is_num(raw) and not isinstance(raw, str):
        return _err("'value' must be a number or a string like '3/4'.")
    if isinstance(raw, str):
        if not raw.strip() or len(raw) > 64:
            return _err("'value' must be a non-empty number or fraction string.")
    try:
        frac = Fraction(str(raw)).limit_denominator(1_000_000)
    except (ValueError, ZeroDivisionError):
        return _err(f"Could not read '{raw}' as a fraction or decimal.")
    out = {"fraction": str(frac), "decimal": float(frac)}
    if abs(frac.numerator) > frac.denominator:
        whole, rem = divmod(abs(frac.numerator), frac.denominator)
        sign = "-" if frac.numerator < 0 else ""
        out["mixed"] = f"{sign}{whole} {rem}/{frac.denominator}" if rem else f"{sign}{whole}"
    return out


# --------------------------------------------------------------------------
# Science: units, constants, elements
# --------------------------------------------------------------------------
# Linear categories: alias -> factor in base units.
_LINEAR_UNITS = {
    "length": ("m", {
        "mm": 0.001, "millimeter": 0.001, "millimeters": 0.001,
        "cm": 0.01, "centimeter": 0.01, "centimeters": 0.01,
        "m": 1.0, "meter": 1.0, "meters": 1.0, "metre": 1.0, "metres": 1.0,
        "km": 1000.0, "kilometer": 1000.0, "kilometers": 1000.0,
        "in": 0.0254, "inch": 0.0254, "inches": 0.0254,
        "ft": 0.3048, "foot": 0.3048, "feet": 0.3048,
        "yd": 0.9144, "yard": 0.9144, "yards": 0.9144,
        "mi": 1609.344, "mile": 1609.344, "miles": 1609.344,
        "au": 149597870700.0, "astronomical unit": 149597870700.0,
        "ly": 9.4607304725808e15, "lightyear": 9.4607304725808e15,
        "lightyears": 9.4607304725808e15, "light year": 9.4607304725808e15,
        "pc": 3.0856775814913673e16, "parsec": 3.0856775814913673e16,
        "parsecs": 3.0856775814913673e16,
    }),
    "mass": ("kg", {
        "mg": 1e-6, "milligram": 1e-6, "milligrams": 1e-6,
        "g": 1e-3, "gram": 1e-3, "grams": 1e-3,
        "kg": 1.0, "kilogram": 1.0, "kilograms": 1.0,
        "t": 1000.0, "tonne": 1000.0, "tonnes": 1000.0,
        "oz": 0.028349523125, "ounce": 0.028349523125, "ounces": 0.028349523125,
        "lb": 0.45359237, "pound": 0.45359237, "pounds": 0.45359237,
    }),
    "volume": ("l", {
        "ml": 0.001, "milliliter": 0.001, "milliliters": 0.001,
        "l": 1.0, "liter": 1.0, "liters": 1.0, "litre": 1.0, "litres": 1.0,
        "gal": 3.785411784, "gallon": 3.785411784, "gallons": 3.785411784,
        "qt": 0.946352946, "quart": 0.946352946,
        "pt": 0.473176473, "pint": 0.473176473,
        "cup": 0.24, "cups": 0.24,
        "floz": 0.0295735295625, "fl oz": 0.0295735295625,
    }),
    "area": ("m2", {
        "mm2": 1e-6, "cm2": 1e-4, "m2": 1.0, "ha": 10000.0, "hectare": 10000.0,
        "km2": 1e6, "in2": 0.00064516, "ft2": 0.09290304,
        "acre": 4046.8564224, "acres": 4046.8564224, "mi2": 2589988.110336,
    }),
    "speed": ("m/s", {
        "m/s": 1.0, "ms": 1.0, "kph": 1000.0 / 3600.0, "km/h": 1000.0 / 3600.0,
        "mph": 1609.344 / 3600.0, "fps": 0.3048, "ft/s": 0.3048,
        "knot": 1852.0 / 3600.0, "knots": 1852.0 / 3600.0,
    }),
    "pressure": ("Pa", {
        "pa": 1.0, "kpa": 1000.0, "bar": 100000.0,
        "psi": 6894.757293168, "atm": 101325.0, "mmhg": 133.322387415,
    }),
    "energy": ("J", {
        "j": 1.0, "kj": 1000.0, "cal": 4.184, "kcal": 4184.0,
        "wh": 3600.0, "kwh": 3600000.0, "btu": 1055.05585262,
    }),
    "power": ("W", {
        "w": 1.0, "kw": 1000.0, "mw": 1000000.0, "hp": 745.699872,
    }),
    "data": ("B", {  # binary: KB = 1024 bytes
        "b": 1 / 8, "bit": 1 / 8, "bits": 1 / 8,
        "B": 1.0, "byte": 1.0, "bytes": 1.0,
        "KB": 1024.0, "MB": 1024.0 ** 2, "GB": 1024.0 ** 3,
        "TB": 1024.0 ** 4, "PB": 1024.0 ** 5,
    }),
    "time": ("s", {
        "ms": 0.001, "millisecond": 0.001, "milliseconds": 0.001,
        "s": 1.0, "sec": 1.0, "second": 1.0, "seconds": 1.0,
        "min": 60.0, "minute": 60.0, "minutes": 60.0,
        "h": 3600.0, "hour": 3600.0, "hours": 3600.0,
        "day": 86400.0, "days": 86400.0,
        "week": 604800.0, "weeks": 604800.0,
    }),
}

# Temperature pivots through Kelvin.
_TEMP_TO_K = {
    "c": lambda v: v + 273.15, "celsius": lambda v: v + 273.15,
    "f": lambda v: (v + 459.67) * 5 / 9, "fahrenheit": lambda v: (v + 459.67) * 5 / 9,
    "k": lambda v: v, "kelvin": lambda v: v,
    "r": lambda v: v * 5 / 9, "rankine": lambda v: v * 5 / 9,
}
_TEMP_FROM_K = {
    "c": lambda v: v - 273.15, "celsius": lambda v: v - 273.15,
    "f": lambda v: v * 9 / 5 - 459.67, "fahrenheit": lambda v: v * 9 / 5 - 459.67,
    "k": lambda v: v, "kelvin": lambda v: v,
    "r": lambda v: v * 9 / 5, "rankine": lambda v: v * 9 / 5,
}


def _tool_unit_convert(user, args, drive=None):
    ok, value = _num(args.get("value"), "value")
    if not ok:
        return _err(value)
    ok, from_u = _str(args.get("from_unit"), "from_unit", max_len=32)
    if not ok:
        return _err(from_u)
    ok, to_u = _str(args.get("to_unit"), "to_unit", max_len=32)
    if not ok:
        return _err(to_u)

    def lookup(unit):
        unit = unit.strip()
        if unit in _TEMP_TO_K:  # exact match first (keeps b/B distinct)
            return ("temperature", unit)
        lowered = unit.lower()
        if lowered in _TEMP_TO_K:
            return ("temperature", lowered)
        for cat, (_, table) in _LINEAR_UNITS.items():
            if unit in table:
                return (cat, unit)
            if lowered in table:
                return (cat, lowered)
        return (None, unit)

    from_cat, from_key = lookup(from_u)
    to_cat, to_key = lookup(to_u)
    all_aliases = (list(_TEMP_TO_K)
                   + [a for _, t in _LINEAR_UNITS.values() for a in t])
    if from_cat is None:
        hint = difflib.get_close_matches(from_u.strip(), all_aliases, n=3, cutoff=0.55)
        msg = f"Unknown unit '{from_u.strip()}'."
        if hint:
            msg += f" Did you mean: {', '.join(hint)}?"
        return _err(msg)
    if to_cat is None:
        hint = difflib.get_close_matches(to_u.strip(), all_aliases, n=3, cutoff=0.55)
        msg = f"Unknown unit '{to_u.strip()}'."
        if hint:
            msg += f" Did you mean: {', '.join(hint)}?"
        return _err(msg)
    if from_cat != to_cat:
        return _err(f"Cannot convert {from_cat} to {to_cat} — pick two units of the same kind.")
    if from_cat == "temperature":
        if value < 0 and from_key in ("k", "kelvin", "r", "rankine"):
            return _err("Negative Kelvin/Rankine temperatures do not exist.")
        kelvin = _TEMP_TO_K[from_key](value)
        if kelvin < 0:
            return _err("That is below absolute zero.")
        result = _TEMP_FROM_K[to_key](kelvin)
    else:
        _, table = _LINEAR_UNITS[from_cat]
        result = value * table[from_key] / table[to_key]
    if not math.isfinite(result):
        return _err("Conversion produced a non-finite number.")
    return {"result": result, "from": from_u.strip(), "to": to_u.strip(),
            "category": from_cat}


_CONSTANTS = [
    {"key": "c", "name": "speed of light", "value": 299792458, "unit": "m/s"},
    {"key": "G", "name": "gravitational constant", "value": 6.6743e-11, "unit": "m^3 kg^-1 s^-2"},
    {"key": "h", "name": "Planck constant", "value": 6.62607015e-34, "unit": "J s"},
    {"key": "hbar", "name": "reduced Planck constant", "value": 1.054571817e-34, "unit": "J s"},
    {"key": "e", "name": "elementary charge", "value": 1.602176634e-19, "unit": "C"},
    {"key": "kB", "name": "Boltzmann constant", "value": 1.380649e-23, "unit": "J/K"},
    {"key": "NA", "name": "Avogadro constant", "value": 6.02214076e23, "unit": "mol^-1"},
    {"key": "R", "name": "gas constant", "value": 8.314462618, "unit": "J mol^-1 K^-1"},
    {"key": "alpha", "name": "fine-structure constant", "value": 0.0072973525693, "unit": ""},
    {"key": "mu0", "name": "vacuum permeability", "value": 1.25663706127e-6, "unit": "N A^-2"},
    {"key": "eps0", "name": "vacuum permittivity", "value": 8.8541878128e-12, "unit": "F/m"},
    {"key": "me", "name": "electron mass", "value": 9.1093837015e-31, "unit": "kg"},
    {"key": "mp", "name": "proton mass", "value": 1.67262192369e-27, "unit": "kg"},
    {"key": "mn", "name": "neutron mass", "value": 1.67492749804e-27, "unit": "kg"},
    {"key": "sigma", "name": "Stefan-Boltzmann constant", "value": 5.670374419e-8, "unit": "W m^-2 K^-4"},
    {"key": "au", "name": "astronomical unit", "value": 149597870700, "unit": "m"},
    {"key": "ly", "name": "light year", "value": 9.4607304725808e15, "unit": "m"},
    {"key": "pc", "name": "parsec", "value": 3.0856775814913673e16, "unit": "m"},
    {"key": "eV", "name": "electronvolt", "value": 1.602176634e-19, "unit": "J"},
    {"key": "u", "name": "atomic mass unit", "value": 1.66053906892e-27, "unit": "kg"},
    {"key": "g0", "name": "standard gravity", "value": 9.80665, "unit": "m/s^2"},
]


def _tool_constants(user, args, drive=None):
    query = args.get("name")
    if query is None or (isinstance(query, str) and not query.strip()):
        return {"constants": [c["key"] for c in _CONSTANTS]}
    if not isinstance(query, str):
        return _err("'name' must be a string.")
    q = query.strip().lower()
    hits = [c for c in _CONSTANTS
            if q == c["key"].lower() or q in c["name"].lower()]
    if not hits:
        return _err(f"No constant matches '{query.strip()}'. Omit 'name' to list all keys.")
    return {"constants": hits[:5]} if len(hits) > 1 else hits[0]


# First 40 elements (H..Zr): number, symbol, name, standard atomic weight.
_ELEMENTS = [
    (1, "H", "hydrogen", 1.008, "nonmetal"), (2, "He", "helium", 4.0026, "noble gas"),
    (3, "Li", "lithium", 6.94, "metal"), (4, "Be", "beryllium", 9.0122, "metal"),
    (5, "B", "boron", 10.81, "metalloid"), (6, "C", "carbon", 12.011, "nonmetal"),
    (7, "N", "nitrogen", 14.007, "nonmetal"), (8, "O", "oxygen", 15.999, "nonmetal"),
    (9, "F", "fluorine", 18.998, "nonmetal"), (10, "Ne", "neon", 20.180, "noble gas"),
    (11, "Na", "sodium", 22.990, "metal"), (12, "Mg", "magnesium", 24.305, "metal"),
    (13, "Al", "aluminium", 26.982, "metal"), (14, "Si", "silicon", 28.085, "metalloid"),
    (15, "P", "phosphorus", 30.974, "nonmetal"), (16, "S", "sulfur", 32.06, "nonmetal"),
    (17, "Cl", "chlorine", 35.45, "nonmetal"), (18, "Ar", "argon", 39.948, "noble gas"),
    (19, "K", "potassium", 39.098, "metal"), (20, "Ca", "calcium", 40.078, "metal"),
    (21, "Sc", "scandium", 44.956, "metal"), (22, "Ti", "titanium", 47.867, "metal"),
    (23, "V", "vanadium", 50.942, "metal"), (24, "Cr", "chromium", 51.996, "metal"),
    (25, "Mn", "manganese", 54.938, "metal"), (26, "Fe", "iron", 55.845, "metal"),
    (27, "Co", "cobalt", 58.933, "metal"), (28, "Ni", "nickel", 58.693, "metal"),
    (29, "Cu", "copper", 63.546, "metal"), (30, "Zn", "zinc", 65.38, "metal"),
    (31, "Ga", "gallium", 69.723, "metal"), (32, "Ge", "germanium", 72.630, "metalloid"),
    (33, "As", "arsenic", 74.922, "metalloid"), (34, "Se", "selenium", 78.971, "nonmetal"),
    (35, "Br", "bromine", 79.904, "nonmetal"), (36, "Kr", "krypton", 83.798, "noble gas"),
    (37, "Rb", "rubidium", 85.468, "metal"), (38, "Sr", "strontium", 87.62, "metal"),
    (39, "Y", "yttrium", 88.906, "metal"), (40, "Zr", "zirconium", 91.224, "metal"),
]


def _tool_element(user, args, drive=None):
    ok, query = _str(args.get("query"), "query", max_len=32)
    if not ok:
        return _err(query)
    q = query.strip()
    if q.isdigit():
        hit = next((e for e in _ELEMENTS if e[0] == int(q)), None)
    else:
        hit = next((e for e in _ELEMENTS if e[1].lower() == q.lower()), None)
        if hit is None:
            hit = next((e for e in _ELEMENTS if q.lower() in e[2].lower()), None)
    if hit is None:
        return _err(f"No element matches '{q}' (first 40 elements, H to Zr).")
    n, symbol, name, mass, kind = hit
    return {"number": n, "symbol": symbol, "name": name,
            "atomic_mass": mass, "category": kind}


# --------------------------------------------------------------------------
# Finance: loans, compounding, percentages, currency (explicit rate)
# --------------------------------------------------------------------------
def _tool_loan(user, args, drive=None):
    ok, principal = _num(args.get("principal"), "principal", lo=0.01, hi=1e12)
    if not ok:
        return _err(principal)
    ok, rate = _num(args.get("annual_rate_pct"), "annual_rate_pct", lo=0, hi=100)
    if not ok:
        return _err(rate)
    ok, years = _num(args.get("years"), "years", lo=0.1, hi=50)
    if not ok:
        return _err(years)
    months = max(1, round(years * 12))
    monthly_rate = rate / 100 / 12
    if monthly_rate == 0:
        payment = principal / months
    else:
        payment = principal * monthly_rate / (1 - (1 + monthly_rate) ** -months)
    total_paid = payment * months
    return {"monthly_payment": round(payment, 2),
            "num_payments": months,
            "total_paid": round(total_paid, 2),
            "total_interest": round(total_paid - principal, 2)}


def _tool_compound(user, args, drive=None):
    ok, principal = _num(args.get("principal"), "principal", lo=0, hi=1e12)
    if not ok:
        return _err(principal)
    ok, rate = _num(args.get("annual_rate_pct"), "annual_rate_pct", lo=0, hi=100)
    if not ok:
        return _err(rate)
    ok, years = _num(args.get("years"), "years", lo=0, hi=100)
    if not ok:
        return _err(years)
    n = args.get("compounds_per_year", 12)
    ok, n = _num(n, "compounds_per_year", lo=1, hi=365, integer=True)
    if not ok:
        return _err(n)
    n = int(n)
    ok, pmt = _num(args.get("contribution", 0), "contribution", lo=0, hi=1e9)
    if not ok:
        return _err(pmt)
    r = rate / 100
    if r == 0:
        final = principal + pmt * n * years
    else:
        growth = (1 + r / n) ** (n * years)
        final = principal * growth + pmt * (growth - 1) / (r / n)
    contributed = pmt * n * years
    return {"final": round(final, 2),
            "contributed": round(principal + contributed, 2),
            "interest_earned": round(final - principal - contributed, 2)}


def _tool_percent(user, args, drive=None):
    mode = str(args.get("mode", "")).lower()
    if mode == "tip":
        ok, bill = _num(args.get("bill"), "bill", lo=0.01, hi=1e9)
        if not ok:
            return _err(bill)
        ok, pct = _num(args.get("pct", 15), "pct", lo=0, hi=100)
        if not ok:
            return _err(pct)
        ok, people = _num(args.get("people", 1), "people", lo=1, hi=1000, integer=True)
        if not ok:
            return _err(people)
        tip = bill * pct / 100
        return {"tip": round(tip, 2), "total": round(bill + tip, 2),
                "per_person": round((bill + tip) / int(people), 2)}
    if mode == "discount":
        ok, price = _num(args.get("price"), "price", lo=0.01, hi=1e9)
        if not ok:
            return _err(price)
        ok, pct = _num(args.get("pct"), "pct", lo=0, hi=100)
        if not ok:
            return _err(pct)
        off = price * pct / 100
        return {"discount": round(off, 2), "final_price": round(price - off, 2)}
    if mode == "change":
        ok, old = _num(args.get("old"), "old")
        if not ok:
            return _err(old)
        ok, new = _num(args.get("new"), "new")
        if not ok:
            return _err(new)
        if old == 0:
            return _err("Cannot compute change from zero.")
        return {"pct_change": round((new - old) / abs(old) * 100, 2)}
    if mode == "split":
        ok, total = _num(args.get("total"), "total", lo=0.01, hi=1e9)
        if not ok:
            return _err(total)
        ok, people = _num(args.get("people"), "people", lo=2, hi=1000, integer=True)
        if not ok:
            return _err(people)
        ok, pct = _num(args.get("tip_pct", 0), "tip_pct", lo=0, hi=100)
        if not ok:
            return _err(pct)
        return {"per_person": round(total * (1 + pct / 100) / int(people), 2)}
    return _err("'mode' must be one of: tip, discount, change, split.")


def _tool_currency_convert(user, args, drive=None):
    for key in ("from", "to"):
        ok, code = _str(args.get(key), key, max_len=8)
        if not ok:
            return _err(code)
        if not re.fullmatch(r"[A-Za-z]{3}", code.strip()):
            return _err(f"'{key}' must be a 3-letter currency code.")
    ok, amount = _num(args.get("amount"), "amount")
    if not ok:
        return _err(amount)
    ok, rate = _num(args.get("rate"), "rate", lo=0, hi=1e12)
    if not ok:
        return _err(rate)
    if rate <= 0:
        return _err("'rate' must be positive.")
    return {"result": round(amount * rate, 4),
            "from": args["from"].strip().upper(),
            "to": args["to"].strip().upper(), "rate": rate}


# --------------------------------------------------------------------------
# Converters: datetime/timezone, roman numerals, encodings
# --------------------------------------------------------------------------
def _tool_datetime_convert(user, args, drive=None):
    ok, value = _str(args.get("value"), "value", max_len=128)
    if not ok:
        return _err(value)
    text = value.strip()
    epoch = None
    try:
        epoch = float(text)
    except ValueError:
        epoch = None
    unit = str(args.get("epoch_unit", "") or "").lower()
    if epoch is not None and unit not in ("s", "ms"):
        unit = "ms" if abs(epoch) >= 1e12 else "s"
    try:
        from_tz = ZoneInfo(str(args.get("from_tz") or "UTC"))
        to_tz = ZoneInfo(str(args.get("to_tz") or "UTC"))
    except ZoneInfoNotFoundError as exc:
        return _err(f"Unknown timezone: {exc}. Use IANA names, e.g. 'Europe/Lisbon', 'America/New_York'.")
    if epoch is not None:
        try:
            moment = datetime.fromtimestamp(
                epoch / 1000 if unit == "ms" else epoch, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return _err("That epoch value is out of range.")
    else:
        iso = text.replace("Z", "+00:00") if text.endswith(("Z", "z")) else text
        try:
            moment = datetime.fromisoformat(iso)
        except ValueError:
            return _err("Could not parse that date. Use ISO format like '2026-03-15 14:30' or an epoch number.")
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=from_tz)
    moment = moment.astimezone(to_tz)
    return {"iso": moment.isoformat(),
            "formatted": moment.strftime("%Y-%m-%d %H:%M %Z"),
            "epoch": int(moment.timestamp())}


_ROMAN_RE = re.compile(r"^M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})$")
_ROMAN_VALS = (("M", 1000), ("CM", 900), ("D", 500), ("CD", 400),
               ("C", 100), ("XC", 90), ("L", 50), ("XL", 40),
               ("X", 10), ("IX", 9), ("V", 5), ("IV", 4), ("I", 1))


def _tool_roman(user, args, drive=None):
    direction = str(args.get("direction", "")).lower()
    ok, value = _str(args.get("value"), "value", max_len=32)
    if not ok:
        return _err(value)
    text = value.strip().upper()
    if direction == "to_roman":
        if not text.isdigit():
            return _err("For to_roman, 'value' must be an integer string.")
        n = int(text)
        if not 1 <= n <= 3999:
            return _err("Roman numerals only cover 1 to 3999.")
        out = ""
        for numeral, val in _ROMAN_VALS:
            while n >= val:
                out += numeral
                n -= val
        return {"result": out}
    if direction == "from_roman":
        if not _ROMAN_RE.match(text):
            return _err(f"'{value.strip()}' is not a valid Roman numeral.")
        total, i = 0, 0
        table = dict(_ROMAN_VALS)
        while i < len(text):
            pair = text[i:i + 2]
            if pair in table:
                total += table[pair]
                i += 2
            else:
                total += table[text[i]]
                i += 1
        return {"result": total}
    return _err("'direction' must be 'to_roman' or 'from_roman'.")


def _tool_encoding(user, args, drive=None):
    op = str(args.get("operation", "")).lower()
    ok, text = _str(args.get("text"), "text")
    if not ok:
        return _err(text)
    try:
        if op == "base64_encode":
            return {"result": base64.b64encode(text.encode("utf-8")).decode("ascii")}
        if op == "base64_decode":
            return {"result": base64.b64decode(text.strip(), validate=True).decode("utf-8")}
        if op == "url_encode":
            return {"result": quote(text, safe="")}
        if op == "url_decode":
            return {"result": unquote(text)}
        if op == "hex_encode":
            return {"result": text.encode("utf-8").hex()}
        if op == "hex_decode":
            return {"result": bytes.fromhex(text).decode("utf-8")}
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return _err(f"Could not {op.replace('_', ' ')} that text — check it is valid input.")
    return _err("'operation' must be one of: base64_encode, base64_decode, url_encode, url_decode, hex_encode, hex_decode.")


# --------------------------------------------------------------------------
# Text: case, stats, lines, regex, hash, slug
# --------------------------------------------------------------------------
def _tool_text_case(user, args, drive=None):
    ok, text = _str(args.get("text"), "text")
    if not ok:
        return _err(text)
    style = str(args.get("style", "")).lower()
    if style == "upper":
        out = text.upper()
    elif style == "lower":
        out = text.lower()
    elif style == "title":
        out = text.title()
    elif style == "sentence":
        parts = re.split(r"([.!?]+\s*)", text)
        out = "".join(p[:1].upper() + p[1:] if i % 2 == 0 else p
                      for i, p in enumerate(parts))
    elif style == "camel":
        words = [w for w in re.split(r"[\W_]+", text, flags=re.UNICODE) if w]
        out = (words[0][:1].lower() + words[0][1:] + "".join(w.capitalize() for w in words[1:])) if words else ""
    elif style in ("snake", "kebab"):
        sep = "_" if style == "snake" else "-"
        s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", sep, text)
        s = re.sub(r"[\W_]+", sep, s, flags=re.UNICODE).strip(sep).lower()
        out = s
    else:
        return _err("'style' must be one of: upper, lower, title, sentence, camel, snake, kebab.")
    return {"result": out}


def _tool_text_stats(user, args, drive=None):
    ok, text = _str(args.get("text"), "text", allow_empty=True)
    if not ok:
        return _err(text)
    words = text.split()
    return {"chars": len(text), "chars_no_spaces": len(re.sub(r"\s", "", text)),
            "words": len(words), "lines": len(text.splitlines()),
            "reading_minutes": round(len(words) / 200, 1)}


def _tool_text_lines(user, args, drive=None):
    ok, text = _str(args.get("text"), "text")
    if not ok:
        return _err(text)
    op = str(args.get("operation", "")).lower()
    ignore_case = bool(args.get("ignore_case", False))
    lines = text.splitlines()[:MAX_LINES]
    removed = 0
    if op == "sort":
        lines = sorted(lines, key=str.lower if ignore_case else None)
    elif op == "dedupe":
        seen, deduped = set(), []
        for line in lines:
            key = line.lower() if ignore_case else line
            if key not in seen:
                seen.add(key)
                deduped.append(line)
        removed = len(lines) - len(deduped)
        lines = deduped
    elif op == "reverse":
        lines = lines[::-1]
    elif op == "number":
        lines = [f"{i + 1}. {line}" for i, line in enumerate(lines)]
    else:
        return _err("'operation' must be one of: sort, dedupe, reverse, number.")
    return {"result": "\n".join(lines), "lines": len(lines), "removed": removed}


def _tool_text_regex(user, args, drive=None):
    ok, text = _str(args.get("text"), "text")
    if not ok:
        return _err(text)
    ok, pattern = _str(args.get("pattern"), "pattern", max_len=500)
    if not ok:
        return _err(pattern)
    op = str(args.get("operation", "extract")).lower()
    flags = re.IGNORECASE if args.get("ignore_case") else 0
    try:
        rx = re.compile(pattern, flags)
    except re.error as exc:
        return _err(f"Invalid regular expression: {exc}.")
    if op == "extract":
        matches = [m.group(0) for m in rx.finditer(text)][:100]
        return {"matches": matches, "count": len(matches)}
    if op == "count":
        return {"count": sum(1 for _ in rx.finditer(text))}
    if op == "replace":
        new, n = rx.subn(str(args.get("replacement", "")), text, count=1000)
        if len(new) > MAX_TEXT:
            return _err("Replacement result is too large.")
        return {"result": new, "replacements": n}
    return _err("'operation' must be one of: extract, count, replace.")


def _tool_text_hash(user, args, drive=None):
    ok, text = _str(args.get("text"), "text")
    if not ok:
        return _err(text)
    algo = str(args.get("algorithm", "sha256")).lower()
    try:
        digest = hashlib.new(algo, text.encode("utf-8")).hexdigest()
    except ValueError:
        return _err("'algorithm' must be one of: md5, sha1, sha256, sha512.")
    return {"hex": digest, "algorithm": algo}


def _tool_text_slug(user, args, drive=None):
    ok, text = _str(args.get("text"), "text")
    if not ok:
        return _err(text)
    slug = unicodedata.normalize("NFKD", text)
    slug = slug.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")[:200].rstrip("-")
    if not slug:
        return _err("Nothing slugifiable in that text.")
    return {"result": slug}


# --------------------------------------------------------------------------
# Catalog: one entry per tool — OpenAI function schema, handler, UI label.
# The reference page (/m/aiutils/) renders this same list, so docs and
# behavior cannot drift apart.
# --------------------------------------------------------------------------
def _schema(name, description, properties, required=()):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": list(required)}}}


TOOL_SPECS = [
    {
        "name": "calculate",
        "label": "Calculated",
        "category": "Math",
        "example": "2^10 + sqrt(16) * 10%",
        "definition": _schema(
            "aiutils.calculate",
            "Evaluate a math expression. Use ^ for power and % for percent "
            "(e.g. '200*15%'). Supports +-*/ parentheses, sqrt/cbrt, sin/cos/tan "
            "(radians), asin/acos/atan, log(x[, base])/ln/log10/log2, exp, "
            "abs, round(x[, n]), floor/ceil, factorial/gcd/lcm/min/max, "
            "degrees/radians, and constants pi/e/tau.",
            {"expression": {"type": "string",
                            "description": "The expression, e.g. '(25*1.2^2)/3 + 5%'"}},
            ["expression"]),
        "handler": _tool_calculate,
    },
    {
        "name": "statistics",
        "label": "Computed statistics",
        "category": "Math",
        "example": "mean of [3, 5, 7, 9]",
        "definition": _schema(
            "aiutils.statistics",
            "Descriptive statistics over a list of numbers (up to 1000). "
            "operation 'all' returns mean, median, sample standard deviation, "
            "min, max and sum.",
            {"values": {"type": "array", "items": {"type": "number"},
                        "description": "The numbers, e.g. [3, 5, 7, 9]"},
             "operation": {"type": "string", "enum": ["all", "mean", "median", "stdev", "min", "max", "sum"]}},
            ["values"]),
        "handler": _tool_statistics,
    },
    {
        "name": "base_convert",
        "label": "Converted base",
        "category": "Math",
        "example": "0xFF to decimal",
        "definition": _schema(
            "aiutils.base_convert",
            "Convert an integer between bases 2 and 36 (e.g. hex to decimal, "
            "decimal to binary). 0x/0b/0o prefixes are accepted.",
            {"value": {"type": "string", "description": "The number, e.g. 'FF' or '1010'"},
             "from_base": {"type": "integer", "description": "Source base, 2-36"},
             "to_base": {"type": "integer", "description": "Target base, 2-36"}},
            ["value", "from_base", "to_base"]),
        "handler": _tool_base_convert,
    },
    {
        "name": "fraction",
        "label": "Converted fraction",
        "category": "Math",
        "example": "0.75 to a fraction",
        "definition": _schema(
            "aiutils.fraction",
            "Turn a decimal or 'a/b' string into a simplified fraction "
            "(plus decimal and mixed-number forms).",
            {"value": {"type": "string",
                       "description": "A decimal like '0.75' or a fraction like '3/4'"}},
            ["value"]),
        "handler": _tool_fraction,
    },
    {
        "name": "unit_convert",
        "label": "Converted units",
        "category": "Science",
        "example": "5 miles to km",
        "definition": _schema(
            "aiutils.unit_convert",
            "Convert between units of the same kind: length (mm..mi, au, ly, pc), "
            "mass, volume, area, speed, pressure, energy, power, data sizes "
            "(binary KB = 1024 bytes), time, temperature (C/F/K/R). "
            "Misspellings get a did-you-mean suggestion.",
            {"value": {"type": "number", "description": "The amount"},
             "from_unit": {"type": "string", "description": "Source unit, e.g. 'miles'"},
             "to_unit": {"type": "string", "description": "Target unit, e.g. 'km'"}},
            ["value", "from_unit", "to_unit"]),
        "handler": _tool_unit_convert,
    },
    {
        "name": "constants",
        "label": "Looked up constant",
        "category": "Science",
        "example": "speed of light",
        "definition": _schema(
            "aiutils.constants",
            "Look up a physical constant by name or symbol (c, G, h, e, kB, "
            "NA, R, alpha, me, mp, au, ly, pc, eV, ...). Omit 'name' to list "
            "all available keys.",
            {"name": {"type": "string",
                      "description": "Constant name or symbol, e.g. 'planck' or 'h'"}}),
        "handler": _tool_constants,
    },
    {
        "name": "element",
        "label": "Looked up element",
        "category": "Science",
        "example": "Fe",
        "definition": _schema(
            "aiutils.element",
            "Look up a chemical element (first 40, H to Zr) by name, symbol "
            "or atomic number. Returns number, symbol, name, atomic mass "
            "and category.",
            {"query": {"type": "string", "description": "Name, symbol or number, e.g. 'Fe'"}},
            ["query"]),
        "handler": _tool_element,
    },
    {
        "name": "loan",
        "label": "Computed loan",
        "category": "Finance",
        "example": "250k at 4% over 30 years",
        "definition": _schema(
            "aiutils.loan",
            "Mortgage/loan math: monthly payment, number of payments, total "
            "paid and total interest for a fixed-rate loan.",
            {"principal": {"type": "number", "description": "Loan amount"},
             "annual_rate_pct": {"type": "number", "description": "Yearly rate in percent, e.g. 4.5"},
             "years": {"type": "number", "description": "Term in years"}},
            ["principal", "annual_rate_pct", "years"]),
        "handler": _tool_loan,
    },
    {
        "name": "compound",
        "label": "Computed compound interest",
        "category": "Finance",
        "example": "10k at 7% for 20 years",
        "definition": _schema(
            "aiutils.compound",
            "Compound growth: final balance, total contributed and interest "
            "earned, with optional regular contributions.",
            {"principal": {"type": "number"},
             "annual_rate_pct": {"type": "number"},
             "years": {"type": "number"},
             "compounds_per_year": {"type": "integer", "description": "1, 4, 12 (default) or 365"},
             "contribution": {"type": "number", "description": "Regular contribution each period"}},
            ["principal", "annual_rate_pct", "years"]),
        "handler": _tool_compound,
    },
    {
        "name": "percent",
        "label": "Computed percentage",
        "category": "Finance",
        "example": "20% tip on an 85 bill split 3 ways",
        "definition": _schema(
            "aiutils.percent",
            "Everyday percentages. mode 'tip' (bill, pct, people?), "
            "'discount' (price, pct), 'change' (old, new) or 'split' "
            "(total, people, tip_pct?).",
            {"mode": {"type": "string", "enum": ["tip", "discount", "change", "split"]}},
            ["mode"]),
        "handler": _tool_percent,
    },
    {
        "name": "currency_convert",
        "label": "Converted currency",
        "category": "Finance",
        "example": "100 USD to EUR at 0.92",
        "definition": _schema(
            "aiutils.currency_convert",
            "Convert money using an EXPLICIT rate you provide (ask the user "
            "for today's rate if you do not have one — never invent rates). "
            "rate = units of 'to' per 1 unit of 'from'.",
            {"amount": {"type": "number"},
             "from": {"type": "string", "description": "3-letter code, e.g. USD"},
             "to": {"type": "string", "description": "3-letter code, e.g. EUR"},
             "rate": {"type": "number", "description": "Conversion rate to use"}},
            ["amount", "from", "to", "rate"]),
        "handler": _tool_currency_convert,
    },
    {
        "name": "datetime_convert",
        "label": "Converted date/time",
        "category": "Converters",
        "example": "2026-03-15 14:30 in Lisbon to UTC",
        "definition": _schema(
            "aiutils.datetime_convert",
            "Parse an ISO-like date or epoch timestamp and convert between "
            "IANA timezones (e.g. Europe/Lisbon, America/New_York). Epochs "
            ">= 1e12 are read as milliseconds unless epoch_unit says otherwise.",
            {"value": {"type": "string", "description": "Date text or epoch number as string"},
             "from_tz": {"type": "string", "description": "IANA zone the input is in (default UTC)"},
             "to_tz": {"type": "string", "description": "IANA zone to convert to (default UTC)"},
             "epoch_unit": {"type": "string", "enum": ["s", "ms"]}},
            ["value"]),
        "handler": _tool_datetime_convert,
    },
    {
        "name": "roman",
        "label": "Converted Roman numeral",
        "category": "Converters",
        "example": "2026 to Roman numerals",
        "definition": _schema(
            "aiutils.roman",
            "Convert between integers (1-3999) and Roman numerals, either "
            "direction.",
            {"value": {"type": "string", "description": "The number or numeral"},
             "direction": {"type": "string", "enum": ["to_roman", "from_roman"]}},
            ["value", "direction"]),
        "handler": _tool_roman,
    },
    {
        "name": "encoding",
        "label": "Encoded text",
        "category": "Converters",
        "example": "base64-decode a token",
        "definition": _schema(
            "aiutils.encoding",
            "Encode or decode text: base64, URL percent-encoding, or hex. "
            "Decoding validates strictly and errors cleanly on bad input.",
            {"text": {"type": "string"},
             "operation": {"type": "string", "enum": ["base64_encode", "base64_decode", "url_encode", "url_decode", "hex_encode", "hex_decode"]}},
            ["text", "operation"]),
        "handler": _tool_encoding,
    },
    {
        "name": "text_case",
        "label": "Changed text case",
        "category": "Text",
        "example": "title to snake_case",
        "definition": _schema(
            "aiutils.text_case",
            "Change text case: upper, lower, title, sentence, camelCase, "
            "snake_case or kebab-case.",
            {"text": {"type": "string"},
             "style": {"type": "string", "enum": ["upper", "lower", "title", "sentence", "camel", "snake", "kebab"]}},
            ["text", "style"]),
        "handler": _tool_text_case,
    },
    {
        "name": "text_stats",
        "label": "Counted text",
        "category": "Text",
        "example": "how long is this paragraph",
        "definition": _schema(
            "aiutils.text_stats",
            "Count characters (with/without spaces), words, lines and an "
            "estimated reading time in minutes.",
            {"text": {"type": "string"}},
            ["text"]),
        "handler": _tool_text_stats,
    },
    {
        "name": "text_lines",
        "label": "Reordered lines",
        "category": "Text",
        "example": "sort these lines and drop duplicates",
        "definition": _schema(
            "aiutils.text_lines",
            "Rework line-based text: sort (optional ignore_case), dedupe "
            "(keeps order, reports removed count), reverse, or number lines.",
            {"text": {"type": "string"},
             "operation": {"type": "string", "enum": ["sort", "dedupe", "reverse", "number"]},
             "ignore_case": {"type": "boolean"}},
            ["text", "operation"]),
        "handler": _tool_text_lines,
    },
    {
        "name": "text_regex",
        "label": "Matched pattern",
        "category": "Text",
        "example": "extract all emails",
        "definition": _schema(
            "aiutils.text_regex",
            "Apply a regular expression: extract matches, count them, or "
            "replace. Supports ignore_case. Invalid patterns return a clean "
            "error instead of failing the run.",
            {"text": {"type": "string"},
             "pattern": {"type": "string", "description": "The regex, e.g. '[\\w.-]+@[\\w.-]+'"},
             "operation": {"type": "string", "enum": ["extract", "count", "replace"]},
             "replacement": {"type": "string", "description": "For replace: may use \\1 groups"},
             "ignore_case": {"type": "boolean"}},
            ["text", "pattern", "operation"]),
        "handler": _tool_text_regex,
    },
    {
        "name": "text_hash",
        "label": "Hashed text",
        "category": "Text",
        "example": "sha256 of this string",
        "definition": _schema(
            "aiutils.text_hash",
            "Hash UTF-8 text with md5, sha1, sha256 or sha512.",
            {"text": {"type": "string"},
             "algorithm": {"type": "string", "enum": ["md5", "sha1", "sha256", "sha512"]}},
            ["text"]),
        "handler": _tool_text_hash,
    },
    {
        "name": "text_slug",
        "label": "Slugified text",
        "category": "Text",
        "example": "make this a URL slug",
        "definition": _schema(
            "aiutils.text_slug",
            "Turn text into a URL/filename slug: lowercase ASCII, words "
            "joined with hyphens.",
            {"text": {"type": "string"}},
            ["text"]),
        "handler": _tool_text_slug,
    },
]


for _spec in TOOL_SPECS:
    register_tool(_spec["name"], _spec["definition"], _spec["handler"],
                  label=_spec["label"], module="aiutils")
